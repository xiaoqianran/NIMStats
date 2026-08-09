#!/usr/bin/env python3
"""Rolling NIM fleet monitor — one small batch per GitHub Actions run.

Strategy:
  catalog → chat filter → stable sorted fleet → take next BATCH_SIZE models
  (cursor wraps). For each model: Health/TTFT request (also sets availability
  status) then Throughput request if AVAILABLE. Client rate limiter ≤40 rpm.
  Writes history.db + advances cursor. Pages regenerates after each batch.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_utils import (  # noqa: E402
    HISTORY_DB,
    STATUS_AVAILABLE,
    STATUS_ERROR,
    STATUS_GONE,
    STATUS_RATE_LIMITED,
    STATUS_TIMEOUT,
    STATUS_UNAUTHORIZED,
    ensure_models,
    export_fleet_snapshot,
    get_state,
    init_schema,
    set_state,
    utc_now,
    write_rolling_batch,
    STALE_AFTER_MINUTES,
)
from model_catalog import get_benchmark_models  # noqa: E402
from rate_limiter import RateLimiter  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
FLEET_OUT = SCRIPT_DIR / "fleet_snapshot.json"
RESULTS_OUT = SCRIPT_DIR / "results.json"

# ── prompts (only these two standard tests) ──────────────────────────────────
HEALTH_PROMPT = os.getenv("HEALTH_PROMPT", "Reply with exactly: OK")
# Fixed, deterministic, relatively stable output length for decode TPS
THROUGHPUT_PROMPT = os.getenv(
    "THROUGHPUT_PROMPT",
    "Write the integers from 1 to 40 inclusive, one number per line. "
    "Output only the numbers, nothing else.",
)

API_BASE = os.getenv("API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "9"))  # 8–10
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
HEALTH_MAX_TOKENS = int(os.getenv("HEALTH_MAX_TOKENS", "8"))
THROUGHPUT_MAX_TOKENS = int(os.getenv("THROUGHPUT_MAX_TOKENS", "120"))


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()
API_KEY = os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY") or ""


def classify_http(status_code: int, message: str) -> str:
    low = (message or "").lower()
    if status_code == 404:
        return STATUS_GONE
    if status_code in (401, 403):
        return STATUS_UNAUTHORIZED
    if status_code == 429:
        return STATUS_RATE_LIMITED
    if status_code in (408, 504):
        return STATUS_TIMEOUT
    if 200 <= status_code < 300:
        return STATUS_AVAILABLE
    if status_code >= 500:
        return STATUS_ERROR
    if "function" in low and "not found" in low:
        return STATUS_GONE
    return STATUS_ERROR


def decode_tps(
    completion_tokens: int | None,
    response_ms: int | None,
    ttft_ms: int | None,
    *,
    content: str | None = None,
) -> float | None:
    """Prefer completion_tokens / (response_time - TTFT) in seconds."""
    tokens = completion_tokens
    if not tokens and content:
        # rough fallback: ~1.3 tokens/word when provider omits usage
        words = len(content.split())
        tokens = max(1, int(words * 1.3)) if words else None
    if not tokens or not response_ms or response_ms <= 0:
        return None
    gen_ms = response_ms - (ttft_ms or 0)
    if gen_ms <= 0:
        gen_ms = response_ms
    return round(tokens / (gen_ms / 1000.0), 4)


def _parse_error_body(body: str, code: int) -> str:
    if not body:
        return f"HTTP {code}"
    try:
        data = json.loads(body)
        detail = data.get("detail")
        title = data.get("title")
        err = data.get("error")
        if title and detail:
            return f"HTTP {code}: {title}: {detail}"
        if isinstance(err, dict):
            return f"HTTP {code}: {err.get('message') or err}"
        if isinstance(err, str):
            return f"HTTP {code}: {err}"
        if detail:
            return f"HTTP {code}: {detail}"
        if title:
            return f"HTTP {code}: {title}"
    except json.JSONDecodeError:
        pass
    return f"HTTP {code}: {body[:240]}"


def chat_completion(
    *,
    model: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
    limiter: RateLimiter,
) -> dict[str, Any]:
    """One API call with rate limiting. Stream used for TTFT."""
    limiter.wait()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json" if not stream else "text/event-stream",
            "User-Agent": "NIMStats-rolling/2.0",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            http_status = getattr(resp, "status", 200) or 200
            if stream:
                content_parts: list[str] = []
                ttft_ms: int | None = None
                completion_tokens = 0
                total_tokens = 0
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        text = (
                            delta.get("content")
                            or delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or ""
                        )
                        if not text:
                            msg = choices[0].get("message") or {}
                            text = (
                                msg.get("content")
                                or msg.get("reasoning_content")
                                or msg.get("reasoning")
                                or ""
                            )
                        if text:
                            if ttft_ms is None:
                                ttft_ms = int((time.perf_counter() - started) * 1000)
                            content_parts.append(text)
                    usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else None
                    if usage:
                        completion_tokens = int(usage.get("completion_tokens") or 0) or completion_tokens
                        total_tokens = int(usage.get("total_tokens") or 0) or total_tokens
                latency = int((time.perf_counter() - started) * 1000)
                content = "".join(content_parts)
                ok = bool(content.strip()) or completion_tokens > 0
                status = STATUS_AVAILABLE if ok and 200 <= http_status < 300 else STATUS_ERROR
                return {
                    "success": ok,
                    "status": status if ok else STATUS_ERROR,
                    "httpStatus": http_status,
                    "responseTime": latency,
                    "timeToFirstToken": ttft_ms,
                    "tokensGenerated": completion_tokens or None,
                    "totalTokens": total_tokens or None,
                    "response": content[:500] if content else None,
                    "error": None if ok else "No content in stream response",
                }
            # non-stream
            raw = resp.read().decode("utf-8", errors="replace")
            latency = int((time.perf_counter() - started) * 1000)
            data = json.loads(raw) if raw else {}
            content = ""
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = (
                    msg.get("content")
                    or msg.get("reasoning_content")
                    or msg.get("reasoning")
                    or ""
                )
            usage = data.get("usage") or {}
            ct = int(usage.get("completion_tokens") or 0)
            tt = int(usage.get("total_tokens") or 0)
            ok = bool(str(content).strip()) or ct > 0
            return {
                "success": ok,
                "status": STATUS_AVAILABLE if ok else STATUS_ERROR,
                "httpStatus": http_status,
                "responseTime": latency,
                "timeToFirstToken": None,  # non-stream has no true TTFT
                "tokensGenerated": ct or None,
                "totalTokens": tt or None,
                "response": str(content)[:500] if content else None,
                "error": None if ok else "Empty response",
            }
    except urllib.error.HTTPError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        code = getattr(exc, "code", 0) or 0
        raw = exc.read().decode("utf-8", errors="replace")
        msg = _parse_error_body(raw, code)
        status = classify_http(code, msg)
        return {
            "success": False,
            "status": status,
            "httpStatus": code,
            "responseTime": latency,
            "timeToFirstToken": None,
            "tokensGenerated": None,
            "totalTokens": None,
            "response": None,
            "error": msg,
        }
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        msg = str(exc)
        status = STATUS_TIMEOUT if "timed out" in msg.lower() or "timeout" in msg.lower() else STATUS_ERROR
        return {
            "success": False,
            "status": status,
            "httpStatus": None,
            "responseTime": latency,
            "timeToFirstToken": None,
            "tokensGenerated": None,
            "totalTokens": None,
            "response": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def next_batch(fleet: list[str], cursor: int, batch_size: int) -> tuple[list[str], int, int, int]:
    """Return (batch, cursor_start, cursor_end_exclusive_mod, new_cursor)."""
    n = len(fleet)
    if n == 0:
        return [], 0, 0, 0
    batch_size = max(1, min(batch_size, n))
    start = cursor % n
    batch: list[str] = []
    for i in range(batch_size):
        batch.append(fleet[(start + i) % n])
    new_cursor = (start + batch_size) % n
    return batch, start, (start + batch_size), new_cursor


def run_model(model: str, limiter: RateLimiter) -> list[dict[str, Any]]:
    """Health (availability + TTFT) then Throughput if AVAILABLE."""
    rows: list[dict[str, Any]] = []

    print(f"  [health] {model}", flush=True)
    health = chat_completion(
        model=model,
        prompt=HEALTH_PROMPT,
        max_tokens=HEALTH_MAX_TOKENS,
        stream=True,
        limiter=limiter,
    )
    health_row = {
        "model": model,
        "testKind": "health",
        "success": health["success"],
        "status": health["status"],
        "httpStatus": health.get("httpStatus"),
        "error": health.get("error"),
        "responseTime": health.get("responseTime"),
        "timeToFirstToken": health.get("timeToFirstToken"),
        "tokensGenerated": health.get("tokensGenerated"),
        "totalTokens": health.get("totalTokens"),
        "decodeTps": decode_tps(
            health.get("tokensGenerated"),
            health.get("responseTime"),
            health.get("timeToFirstToken"),
            content=health.get("response"),
        ),
        "response": health.get("response"),
    }
    rows.append(health_row)
    mark = "OK" if health["success"] else health["status"]
    print(
        f"    → {mark} e2e={health.get('responseTime')}ms ttft={health.get('timeToFirstToken')} "
        f"{health.get('error') or ''}",
        flush=True,
    )

    if health["status"] != STATUS_AVAILABLE or not health["success"]:
        return rows

    print(f"  [throughput] {model}", flush=True)
    # Non-stream so usage.completion_tokens is reliable for decode TPS.
    thr = chat_completion(
        model=model,
        prompt=THROUGHPUT_PROMPT,
        max_tokens=THROUGHPUT_MAX_TOKENS,
        stream=False,
        limiter=limiter,
    )
    thr_row = {
        "model": model,
        "testKind": "throughput",
        "success": thr["success"],
        "status": thr["status"] if thr["success"] else thr["status"],
        "httpStatus": thr.get("httpStatus"),
        "error": thr.get("error"),
        "responseTime": thr.get("responseTime"),
        "timeToFirstToken": thr.get("timeToFirstToken"),
        "tokensGenerated": thr.get("tokensGenerated"),
        "totalTokens": thr.get("totalTokens"),
        "decodeTps": decode_tps(
            thr.get("tokensGenerated"),
            thr.get("responseTime"),
            thr.get("timeToFirstToken"),
            content=thr.get("response"),
        ),
        "response": thr.get("response"),
    }
    rows.append(thr_row)
    mark = "OK" if thr["success"] else thr["status"]
    print(
        f"    → {mark} e2e={thr.get('responseTime')}ms ttft={thr.get('timeToFirstToken')} "
        f"tok={thr.get('tokensGenerated')} tps={thr_row.get('decodeTps')} "
        f"{thr.get('error') or ''}",
        flush=True,
    )
    return rows


def main() -> int:
    if not API_KEY:
        print("Error: NIM_API_KEY / NVIDIA_API_KEY not set", file=sys.stderr)
        return 1

    import sqlite3

    # 1) Fleet from catalog (chat-filtered)
    fleet, catalog_meta = get_benchmark_models(api_key=API_KEY, verbose=True)
    fleet = sorted(set(fleet))
    if not fleet:
        print("No chat models in catalog", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(HISTORY_DB))
    init_schema(conn)
    ensure_models(conn, fleet)
    # Drop models no longer in chat fleet? keep history; optional mark — skip delete
    cursor = int(get_state(conn, "cursor", "0") or "0")
    set_state(conn, "fleet_size", str(len(fleet)))
    set_state(conn, "fleet_json", json.dumps(fleet))
    conn.commit()

    batch, c_start, c_end, new_cursor = next_batch(fleet, cursor, BATCH_SIZE)
    print(
        f"Rolling batch: cursor {c_start}→{new_cursor} "
        f"(size={len(batch)}/{len(fleet)}) models={batch}",
        flush=True,
    )

    limiter = RateLimiter()
    all_rows: list[dict[str, Any]] = []
    for model in batch:
        print(f"Testing: {model}", flush=True)
        all_rows.extend(run_model(model, limiter))

    timestamp = utc_now()
    # Combined prompt label for run row
    prompt_label = f"HEALTH: {HEALTH_PROMPT} | THROUGHPUT: {THROUGHPUT_PROMPT}"

    run_id = write_rolling_batch(
        timestamp=timestamp,
        prompt=prompt_label,
        models=all_rows,
        batch_meta={
            "batch_size": len(batch),
            "cursor_start": c_start,
            "cursor_end": c_end,
            "kind": "rolling",
        },
    )

    conn = sqlite3.connect(str(HISTORY_DB))
    init_schema(conn)
    set_state(conn, "cursor", str(new_cursor))
    set_state(conn, "last_batch_at", timestamp)
    set_state(conn, "last_run_id", str(run_id))
    set_state(conn, "stale_after_minutes", str(STALE_AFTER_MINUTES))
    conn.commit()
    conn.close()

    # Summaries
    health_rows = [r for r in all_rows if r.get("testKind") == "health"]
    thr_ok = [r for r in all_rows if r.get("testKind") == "throughput" and r.get("success")]
    by_status: dict[str, int] = {}
    for r in health_rows:
        st = r.get("status") or STATUS_ERROR
        by_status[st] = by_status.get(st, 0) + 1

    summary = {
        "timestamp": timestamp,
        "runId": run_id,
        "batchSize": len(batch),
        "cursorStart": c_start,
        "cursorEnd": new_cursor,
        "fleetSize": len(fleet),
        "healthByStatus": by_status,
        "throughputSuccess": len(thr_ok),
        "catalog": catalog_meta,
        "rateLimitRpm": int(os.getenv("NIM_MAX_REQUESTS_PER_MINUTE", "40")),
    }
    RESULTS_OUT.write_text(
        json.dumps({"summary": summary, "models": all_rows}, indent=2),
        encoding="utf-8",
    )
    snap = export_fleet_snapshot()
    FLEET_OUT.write_text(json.dumps(snap, indent=2), encoding="utf-8")

    print()
    print("=== Rolling batch complete ===")
    print(json.dumps(summary, indent=2))
    print(f"Fleet snapshot counts: {snap.get('counts')}")
    print(f"history.db → {HISTORY_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

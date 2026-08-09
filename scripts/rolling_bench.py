#!/usr/bin/env python3
"""Rolling NIM fleet monitor — one small batch per GitHub Actions run.

Strategy:
  catalog → chat filter → stable sorted fleet → take next BATCH_SIZE models
  (0 means the whole fleet). Each model gets one deterministic streaming call
  that measures availability, TTFT, end-to-end latency, and decode throughput.
  Requests run concurrently across a per-key 40 RPM pool. Writes history.db and
  advances the cursor; the workflow deploys Pages after each completed run.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    sanitize_error,
    set_state,
    utc_now,
    write_rolling_batch,
    STALE_AFTER_MINUTES,
)
from api_key_pool import ApiKeyPool, load_api_keys  # noqa: E402
from model_catalog import get_benchmark_models  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
FLEET_OUT = SCRIPT_DIR / "fleet_snapshot.json"
RESULTS_OUT = SCRIPT_DIR / "results.json"

# A single fixed payload makes results more comparable than asking models to
# count or write code. It is long enough for useful decode throughput while
# requiring no knowledge, reasoning, localization, or safety judgement.
BENCHMARK_PAYLOAD = (
    "amber apple autumn baker beach birch blue breeze brook candle cedar circle "
    "cloud coral dawn delta dune earth ember field flame forest frost garden "
    "glass gold grain green harbor hazel hill honey iris ivory jade lake leaf "
    "lemon light lilac linen maple meadow mist moon moss north oak ocean olive "
    "orange pearl pine plum quartz rain river rose silver sky snow south spring "
    "stone summer tide trail violet water west willow wind winter"
)
BENCHMARK_PROMPT = os.getenv(
    "BENCHMARK_PROMPT",
    "This is a deterministic transport benchmark, not a question. Copy the "
    "payload between <payload> and </payload> exactly once. Return only the "
    "payload: no tags, explanation, Markdown, quotation marks, or leading or "
    f"trailing text.\n<payload>\n{BENCHMARK_PAYLOAD}\n</payload>",
)

API_BASE = os.getenv("API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "0"))  # 0 = whole fleet
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
BENCHMARK_MAX_TOKENS = int(os.getenv("BENCHMARK_MAX_TOKENS", "192"))


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
API_KEYS = load_api_keys()


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
            return sanitize_error(f"HTTP {code}: {title}: {detail}") or f"HTTP {code}"
        if isinstance(err, dict):
            return sanitize_error(f"HTTP {code}: {err.get('message') or err}") or f"HTTP {code}"
        if isinstance(err, str):
            return sanitize_error(f"HTTP {code}: {err}") or f"HTTP {code}"
        if detail:
            return sanitize_error(f"HTTP {code}: {detail}") or f"HTTP {code}"
        if title:
            return sanitize_error(f"HTTP {code}: {title}") or f"HTTP {code}"
    except json.JSONDecodeError:
        pass
    return sanitize_error(f"HTTP {code}: {body[:240]}") or f"HTTP {code}"


def chat_completion(
    *,
    model: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
    key_pool: ApiKeyPool,
    preferred_key_indexes: list[int] | None = None,
) -> dict[str, Any]:
    """One API call using a rate-limited key from the shared pool."""
    api_key_index, api_key = key_pool.acquire_with_index(preferred_key_indexes)
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
            "Authorization": f"Bearer {api_key}",
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
                reasoning_parts: list[str] = []
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
                        content_text = delta.get("content") or ""
                        reasoning_text = (
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or ""
                        )
                        if not content_text and not reasoning_text:
                            msg = choices[0].get("message") or {}
                            content_text = msg.get("content") or ""
                            reasoning_text = (
                                msg.get("reasoning_content")
                                or msg.get("reasoning")
                                or ""
                            )
                        if content_text or reasoning_text:
                            if ttft_ms is None:
                                ttft_ms = int((time.perf_counter() - started) * 1000)
                            if content_text:
                                content_parts.append(str(content_text))
                            if reasoning_text:
                                reasoning_parts.append(str(reasoning_text))
                    usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else None
                    if usage:
                        completion_tokens = int(usage.get("completion_tokens") or 0) or completion_tokens
                        total_tokens = int(usage.get("total_tokens") or 0) or total_tokens
                latency = int((time.perf_counter() - started) * 1000)
                visible_content = "".join(content_parts)
                reasoning_content = "".join(reasoning_parts)
                content = visible_content or reasoning_content
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
                    "visibleResponse": visible_content[:500] if visible_content else None,
                    "apiKeyIndex": api_key_index,
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
                "visibleResponse": str(content)[:500] if content else None,
                "apiKeyIndex": api_key_index,
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
            "apiKeyIndex": api_key_index,
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
            "apiKeyIndex": api_key_index,
        }


def next_batch(fleet: list[str], cursor: int, batch_size: int) -> tuple[list[str], int, int, int]:
    """Return (batch, cursor_start, cursor_end_exclusive_mod, new_cursor)."""
    n = len(fleet)
    if n == 0:
        return [], 0, 0, 0
    batch_size = n if batch_size <= 0 else min(batch_size, n)
    start = cursor % n
    batch: list[str] = []
    for i in range(batch_size):
        batch.append(fleet[(start + i) % n])
    new_cursor = (start + batch_size) % n
    return batch, start, (start + batch_size), new_cursor


def run_model(
    model: str,
    key_pool: ApiKeyPool,
    preferred_key_indexes: list[int] | None = None,
) -> dict[str, Any]:
    """Run the unified availability + latency + throughput benchmark."""
    print(f"  [benchmark] {model}", flush=True)
    candidate_indexes = list(
        dict.fromkeys(preferred_key_indexes or range(key_pool.key_count))
    )
    tried_indexes: set[int] = set()
    result: dict[str, Any]
    while True:
        remaining = [index for index in candidate_indexes if index not in tried_indexes]
        result = chat_completion(
            model=model,
            prompt=BENCHMARK_PROMPT,
            max_tokens=BENCHMARK_MAX_TOKENS,
            stream=True,
            key_pool=key_pool,
            preferred_key_indexes=remaining,
        )
        tried_indexes.add(result["apiKeyIndex"])
        # A catalog entry can be entitled for one account but not another.
        # Only declare it GONE after every key that listed it has returned GONE.
        if result["status"] != STATUS_GONE or len(tried_indexes) >= len(candidate_indexes):
            break

    response = result.get("visibleResponse") or result.get("response") or ""
    row = {
        "model": model,
        "testKind": "throughput",
        "success": result["success"],
        "status": result["status"],
        "httpStatus": result.get("httpStatus"),
        "error": result.get("error"),
        "responseTime": result.get("responseTime"),
        "timeToFirstToken": result.get("timeToFirstToken"),
        "tokensGenerated": result.get("tokensGenerated"),
        "totalTokens": result.get("totalTokens"),
        "decodeTps": decode_tps(
            result.get("tokensGenerated"),
            result.get("responseTime"),
            result.get("timeToFirstToken"),
            content=response,
        ),
        "response": response or None,
        "responseMatchesPayload": response.strip() == BENCHMARK_PAYLOAD,
        "keyAttempts": len(tried_indexes),
    }
    mark = "OK" if result["success"] else result["status"]
    compliance = "exact" if row["responseMatchesPayload"] else "non-exact"
    print(
        f"    → {model} {mark} e2e={result.get('responseTime')}ms "
        f"ttft={result.get('timeToFirstToken')} tok={result.get('tokensGenerated')} "
        f"tps={row.get('decodeTps')} output={compliance} "
        f"keys_tried={len(tried_indexes)} {result.get('error') or ''}",
        flush=True,
    )
    return row


def main() -> int:
    if not API_KEYS:
        print("Error: NIM_API_KEYS / NIM_API_KEY / NVIDIA_API_KEY not set", file=sys.stderr)
        return 1

    import sqlite3

    # 1) Full retained fleet from the union of every key-specific catalog.
    key_pool = ApiKeyPool(API_KEYS)
    catalog_keys = [key_pool.acquire() for _ in range(key_pool.key_count)]
    fleet, catalog_meta = get_benchmark_models(api_keys=catalog_keys, verbose=True)
    model_key_indexes = catalog_meta.pop("model_key_indexes", {})
    fleet = sorted(set(fleet))
    if not fleet:
        print("No models in catalog", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(HISTORY_DB))
    init_schema(conn)
    ensure_models(conn, fleet)
    # Never delete models that disappeared from the latest catalog; retained
    # IDs are still probed so retired models receive an explicit status.
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

    all_rows: list[dict[str, Any]] = []
    default_workers = min(64, max(8, key_pool.key_count * 4))
    max_workers = max(1, int(os.getenv("NIM_MAX_IN_FLIGHT", str(default_workers))))
    print(
        f"Request pool: keys={key_pool.key_count} per_key_rpm="
        f"{os.getenv('NIM_MAX_REQUESTS_PER_MINUTE', '40')} workers={max_workers}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batch))) as executor:
        futures = {
            executor.submit(run_model, model, key_pool, model_key_indexes.get(model)): model
            for model in batch
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                all_rows.append(future.result())
            except Exception as exc:  # defensive: preserve the rest of the fleet run
                print(f"  [internal-error] {model}: {type(exc).__name__}: {exc}", flush=True)
                all_rows.append(
                    {
                        "model": model,
                        "testKind": "throughput",
                        "success": False,
                        "status": STATUS_ERROR,
                        "error": f"Internal worker error: {type(exc).__name__}: {exc}",
                    }
                )

    # Stable persistence and JSON output regardless of completion order.
    all_rows.sort(key=lambda row: row["model"])

    timestamp = utc_now()
    run_id = write_rolling_batch(
        timestamp=timestamp,
        prompt=BENCHMARK_PROMPT,
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
    successful = [r for r in all_rows if r.get("success")]
    exact = [r for r in successful if r.get("responseMatchesPayload")]
    by_status: dict[str, int] = {}
    for r in all_rows:
        st = r.get("status") or STATUS_ERROR
        by_status[st] = by_status.get(st, 0) + 1

    summary = {
        "timestamp": timestamp,
        "runId": run_id,
        "batchSize": len(batch),
        "cursorStart": c_start,
        "cursorEnd": new_cursor,
        "fleetSize": len(fleet),
        "byStatus": by_status,
        "successCount": len(successful),
        "exactPayloadCount": len(exact),
        "catalog": catalog_meta,
        "rateLimitRpm": int(os.getenv("NIM_MAX_REQUESTS_PER_MINUTE", "40")),
        "apiKeyCount": key_pool.key_count,
        "requestCount": key_pool.request_count,
        "maxInFlight": max_workers,
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

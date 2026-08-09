#!/usr/bin/env python3
"""Rolling NIM fleet monitor with separated health, speed, and capability tests.

Strategy:
  catalog → chat filter → stable sorted fleet → take next BATCH_SIZE models
  (0 means the whole fleet). Every catalog model receives exactly four calls:
  health, two controlled-throughput samples, and one locally graded capability
  workload. Requests run concurrently across a per-key 40
  RPM pool. Writes history.db and advances the cursor; the workflow deploys
  Pages after each completed run.
"""

from __future__ import annotations

import json
import os
import statistics
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
from benchmark_suite import (  # noqa: E402
    BENCHMARK_VERSION,
    CAPABILITY_PROMPT,
    HEALTH_MARKER,
    HEALTH_PROMPT,
    THROUGHPUT_MIN_VALID_TOKENS,
    THROUGHPUT_PROMPT,
    THROUGHPUT_TARGET_TOKENS,
    grade_capability_response,
)
from model_catalog import get_benchmark_models  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
FLEET_OUT = SCRIPT_DIR / "fleet_snapshot.json"
RESULTS_OUT = SCRIPT_DIR / "results.json"

BENCHMARK_PROMPT = "\n\n".join(
    (
        f"benchmark_version={BENCHMARK_VERSION}",
        f"[health]\n{HEALTH_PROMPT}",
        f"[throughput]\n{THROUGHPUT_PROMPT}",
        f"[capability]\n{CAPABILITY_PROMPT}",
    )
)

API_BASE = os.getenv("API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "0"))  # 0 = whole fleet
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
HEALTH_MAX_TOKENS = int(os.getenv("HEALTH_MAX_TOKENS", "24"))
THROUGHPUT_MAX_TOKENS = int(
    os.getenv("THROUGHPUT_MAX_TOKENS", str(THROUGHPUT_TARGET_TOKENS))
)
CAPABILITY_MAX_TOKENS = int(os.getenv("CAPABILITY_MAX_TOKENS", "384"))


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
) -> float | None:
    """Provider token usage divided by decode phase; never estimate tokens."""
    if not completion_tokens or not response_ms or response_ms <= 0:
        return None
    gen_ms = response_ms - (ttft_ms or 0)
    if gen_ms <= 0:
        gen_ms = response_ms
    return round(completion_tokens / (gen_ms / 1000.0), 4)


def chars_per_second(content: str | None, response_ms: int | None, ttft_ms: int | None) -> float | None:
    """Tokenizer-independent diagnostic fallback, explicitly not called TPS."""
    if not content or not response_ms or response_ms <= 0:
        return None
    gen_ms = response_ms - (ttft_ms or 0)
    if gen_ms <= 0:
        gen_ms = response_ms
    return round(len(content) / (gen_ms / 1000.0), 4)


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
    extra_payload: dict[str, Any] | None = None,
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
    if extra_payload:
        payload.update(extra_payload)
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
                    "response": content[:1000] if content else None,
                    "visibleResponse": visible_content[:1000] if visible_content else None,
                    "responseFull": content or None,
                    "visibleResponseFull": visible_content or None,
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
                "response": str(content)[:1000] if content else None,
                "visibleResponse": str(content)[:1000] if content else None,
                "responseFull": str(content) if content else None,
                "visibleResponseFull": str(content) if content else None,
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
) -> dict[str, Any]:
    """Use exactly four globally round-robin calls for every catalog model."""
    print(f"  [suite] {model}", flush=True)
    health = chat_completion(
        model=model,
        prompt=HEALTH_PROMPT,
        max_tokens=HEALTH_MAX_TOKENS,
        stream=True,
        key_pool=key_pool,
    )
    throughput_results = [
        chat_completion(
            model=model,
            prompt=THROUGHPUT_PROMPT,
            max_tokens=THROUGHPUT_MAX_TOKENS,
            stream=True,
            key_pool=key_pool,
            extra_payload={
                "ignore_eos": True,
                "stream_options": {"include_usage": True},
            },
        )
        for _ in range(2)
    ]
    capability = chat_completion(
        model=model,
        prompt=CAPABILITY_PROMPT,
        max_tokens=CAPABILITY_MAX_TOKENS,
        stream=False,
        key_pool=key_pool,
    )

    all_results = [health, *throughput_results, capability]
    successful_calls = [result for result in all_results if result.get("success")]
    available = bool(successful_calls)
    primary = health if health.get("success") else (successful_calls[0] if successful_calls else health)

    health_response = health.get("visibleResponseFull") or health.get("responseFull") or ""
    valid_throughput = []
    char_rates = []
    for result in throughput_results:
        response = result.get("visibleResponseFull") or result.get("responseFull") or ""
        char_rate = chars_per_second(
            response,
            result.get("responseTime"),
            result.get("timeToFirstToken"),
        )
        if char_rate is not None:
            char_rates.append(char_rate)
        generated = result.get("tokensGenerated")
        if result.get("success") and generated is not None and generated >= THROUGHPUT_MIN_VALID_TOKENS:
            sample_tps = decode_tps(
                generated,
                result.get("responseTime"),
                result.get("timeToFirstToken"),
            )
            if sample_tps is not None:
                valid_throughput.append((result, sample_tps))

    tps_samples = [sample[1] for sample in valid_throughput]
    throughput_valid = bool(tps_samples)
    aggregate_tps = round(statistics.median(tps_samples), 4) if tps_samples else None
    throughput_cv = None
    if len(tps_samples) >= 2 and statistics.mean(tps_samples) > 0:
        throughput_cv = round(statistics.pstdev(tps_samples) / statistics.mean(tps_samples), 4)
    throughput_latency = (
        round(statistics.median([sample[0]["responseTime"] for sample in valid_throughput]))
        if valid_throughput
        else None
    )
    throughput_ttfts = [
        sample[0].get("timeToFirstToken")
        for sample in valid_throughput
        if sample[0].get("timeToFirstToken") is not None
    ]
    throughput_ttft = round(statistics.median(throughput_ttfts)) if throughput_ttfts else None
    generated_samples = [sample[0].get("tokensGenerated") for sample in valid_throughput]
    total_samples = [
        sample[0].get("totalTokens")
        for sample in valid_throughput
        if sample[0].get("totalTokens") is not None
    ]

    capability_response = (
        capability.get("visibleResponseFull")
        or capability.get("responseFull")
        or ""
    )
    grade = grade_capability_response(capability_response)
    row = {
        "model": model,
        "testKind": "suite-v3",
        "benchmarkVersion": BENCHMARK_VERSION,
        "success": available,
        "status": STATUS_AVAILABLE if available else health["status"],
        "httpStatus": primary.get("httpStatus"),
        "error": None if available else health.get("error"),
        "responseTime": primary.get("responseTime"),
        "timeToFirstToken": primary.get("timeToFirstToken"),
        "response": health_response[:1000] or None,
        "healthMarkerExact": health_response.strip() == HEALTH_MARKER,
        "tokensGenerated": round(statistics.median(generated_samples)) if generated_samples else None,
        "totalTokens": round(statistics.median(total_samples)) if total_samples else None,
        "decodeTps": aggregate_tps,
        "charsPerSecond": round(statistics.median(char_rates), 4) if char_rates else None,
        "throughputValid": throughput_valid,
        "throughputSampleCount": len(tps_samples),
        "throughputCv": throughput_cv,
        "throughputResponseTime": throughput_latency,
        "throughputTtft": throughput_ttft,
        "throughputMode": "fixed-osl",
        "capabilityScore": grade["score"] if capability.get("success") else None,
        "capabilityPass": grade["pass"] if capability.get("success") else False,
        "formatPass": grade["formatPass"] if capability.get("success") else False,
        "capabilityChecks": grade["checks"] if capability.get("success") else None,
        "capabilityError": capability.get("error"),
        "capabilityResponse": capability_response[:1000] or None,
        "requestCount": 4,
        "apiKeyIndexes": [result["apiKeyIndex"] for result in all_results],
    }
    mark = "OK" if available else health["status"]
    print(
        f"    → {model} {mark} calls=4 keys={row['apiKeyIndexes']} "
        f"health={row.get('responseTime')}ms ttft={row.get('timeToFirstToken')} "
        f"valid_samples={row.get('throughputSampleCount')}/2 "
        f"tps={row.get('decodeTps')} cv={row.get('throughputCv')} "
        f"suite={row.get('capabilityScore')} {row.get('error') or ''}",
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
    # The user explicitly guarantees all ten keys have identical entitlements;
    # one catalog request is enough and leaves the pool for round-robin inference.
    catalog_keys = [key_pool.acquire()]
    fleet, catalog_meta = get_benchmark_models(api_keys=catalog_keys, verbose=True)
    catalog_meta.pop("model_key_indexes", None)
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
            executor.submit(run_model, model, key_pool): model
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
                        "testKind": "suite-v3",
                        "benchmarkVersion": BENCHMARK_VERSION,
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
            "kind": "suite-v3",
            "benchmark_version": BENCHMARK_VERSION,
        },
    )

    conn = sqlite3.connect(str(HISTORY_DB))
    init_schema(conn)
    set_state(conn, "cursor", str(new_cursor))
    set_state(conn, "last_batch_at", timestamp)
    set_state(conn, "last_run_id", str(run_id))
    set_state(conn, "benchmark_version", BENCHMARK_VERSION)
    set_state(conn, "stale_after_minutes", str(STALE_AFTER_MINUTES))
    conn.commit()
    conn.close()

    # Summaries
    successful = [r for r in all_rows if r.get("success")]
    valid_throughput = [r for r in successful if r.get("throughputValid")]
    capability_passes = [r for r in successful if r.get("capabilityPass")]
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
        "validThroughputCount": len(valid_throughput),
        "capabilityPassCount": len(capability_passes),
        "benchmarkVersion": BENCHMARK_VERSION,
        "throughputTargetTokens": THROUGHPUT_TARGET_TOKENS,
        "throughputMinValidTokens": THROUGHPUT_MIN_VALID_TOKENS,
        "catalog": catalog_meta,
        "rateLimitRpm": int(os.getenv("NIM_MAX_REQUESTS_PER_MINUTE", "40")),
        "apiKeyCount": key_pool.key_count,
        "requestCount": key_pool.request_count,
        "requestsPerModel": 4,
        "requestsByKey": {
            str(index): sum(row.get("apiKeyIndexes", []).count(index) for row in all_rows)
            for index in range(key_pool.key_count)
        },
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

#!/usr/bin/env python3
"""Rolling NIM fleet monitor with health, speed, and long-generation tests.

Strategy:
  catalog → chat filter → stable sorted fleet → take next BATCH_SIZE models
  (0 means the whole fleet). Every catalog model receives the same stage set:
  health, N controlled-throughput samples, and one natural-stop long-generation
  workload. Requests run concurrently across a per-key 30 RPM pool
  (10 keys → 300 RPM total, round-robin).

  A single hourly Actions job runs as many full-fleet suite rounds as fit in
  RUN_BUDGET_SECONDS so every model is retested equally within the hour
  (balanced coverage, no self-chaining workflows).
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
    HEALTH_MARKER,
    HEALTH_PROMPT,
    LONG_TASK_PROMPT,
    THROUGHPUT_MIN_VALID_TOKENS,
    THROUGHPUT_PROMPT,
    THROUGHPUT_TARGET_TOKENS,
    analyze_long_response,
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
        f"[long-generation]\n{LONG_TASK_PROMPT}",
    )
)

API_BASE = os.getenv("API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "0"))  # 0 = whole fleet
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
HEALTH_MAX_TOKENS = int(os.getenv("HEALTH_MAX_TOKENS", "24"))
THROUGHPUT_MAX_TOKENS = int(
    os.getenv("THROUGHPUT_MAX_TOKENS", str(THROUGHPUT_TARGET_TOKENS))
)
LONG_TASK_MAX_TOKENS = int(os.getenv("LONG_TASK_MAX_TOKENS", "3072"))
LONG_TASK_TIMEOUT = int(os.getenv("LONG_TASK_TIMEOUT_SECONDS", "300"))
# How many fixed-OSL throughput samples per model per suite round.
THROUGHPUT_SAMPLE_COUNT = max(1, int(os.getenv("THROUGHPUT_SAMPLE_COUNT", "4")))
# 0 = keep running suite rounds until RUN_BUDGET_SECONDS is nearly spent.
SUITE_ROUNDS = max(0, int(os.getenv("SUITE_ROUNDS", "0")))
# Leave headroom inside the hourly Actions job for commit / Pages.
RUN_BUDGET_SECONDS = max(60, int(os.getenv("RUN_BUDGET_SECONDS", "3000")))


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
    timeout_seconds: int | None = None,
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
    timeout = timeout_seconds or REQUEST_TIMEOUT
    started = time.perf_counter()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    ttft_ms: int | None = None
    completion_tokens = 0
    total_tokens = 0
    finish_reason: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            http_status = getattr(resp, "status", 200) or 200
            if stream:
                for raw_line in resp:
                    if time.perf_counter() - started > timeout:
                        raise TimeoutError(f"total request time exceeded {timeout}s")
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
                        if choices[0].get("finish_reason") is not None:
                            finish_reason = str(choices[0]["finish_reason"])
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
                    "finishReason": finish_reason,
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
                if choices[0].get("finish_reason") is not None:
                    finish_reason = str(choices[0]["finish_reason"])
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
                "finishReason": finish_reason,
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
        status = (
            STATUS_TIMEOUT
            if isinstance(exc, TimeoutError)
            or "timed out" in msg.lower()
            or "timeout" in msg.lower()
            or "time exceeded" in msg.lower()
            else STATUS_ERROR
        )
        visible_content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts)
        partial_content = visible_content or reasoning_content
        return {
            "success": False,
            "status": status,
            "httpStatus": None,
            "responseTime": latency,
            "timeToFirstToken": None,
            "tokensGenerated": completion_tokens or None,
            "totalTokens": total_tokens or None,
            "response": partial_content[:1000] if partial_content else None,
            "visibleResponse": visible_content[:1000] if visible_content else None,
            "responseFull": partial_content or None,
            "visibleResponseFull": visible_content or None,
            "finishReason": finish_reason,
            "error": f"{type(exc).__name__}: {exc}",
            "apiKeyIndex": api_key_index,
        }


def next_batch(fleet: list[str], cursor: int, batch_size: int) -> tuple[list[str], int, int, int]:
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


def get_stage_names(throughput_samples: int | None = None) -> tuple[str, ...]:
    """Build the per-model stage list. Every model gets the exact same set."""
    n = THROUGHPUT_SAMPLE_COUNT if throughput_samples is None else max(1, int(throughput_samples))
    return ("health",) + tuple(f"throughput-{i + 1}" for i in range(n)) + ("long-generation",)


# Default stage list at import time (env-driven). Tests may monkeypatch this.
STAGE_NAMES = get_stage_names()


def build_stage_jobs(
    models: list[str],
    stage_names: tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """Materialize every stage up front so response latency cannot gate starts."""
    names = stage_names or STAGE_NAMES
    return [(model, stage) for model in models for stage in names]


def run_stage(model: str, stage: str, key_pool: ApiKeyPool) -> dict[str, Any]:
    """Run one independently scheduled stage; the key pool controls its start."""
    if stage == "health":
        return chat_completion(
            model=model,
            prompt=HEALTH_PROMPT,
            max_tokens=HEALTH_MAX_TOKENS,
            stream=True,
            key_pool=key_pool,
        )
    if stage.startswith("throughput-"):
        return chat_completion(
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
    if stage == "long-generation":
        return chat_completion(
            model=model,
            prompt=LONG_TASK_PROMPT,
            max_tokens=LONG_TASK_MAX_TOKENS,
            stream=True,
            key_pool=key_pool,
            timeout_seconds=LONG_TASK_TIMEOUT,
            extra_payload={
                "stream_options": {"include_usage": True},
            },
        )
    raise ValueError(f"Unknown benchmark stage: {stage}")


def run_model(
    model: str,
    key_pool: ApiKeyPool | None,
    stage_results: dict[str, dict[str, Any]] | None = None,
    stage_names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Aggregate health + N throughput samples + long-generation for one model."""
    names = stage_names or STAGE_NAMES
    print(f"  [suite] {model}", flush=True)
    if stage_results is None:
        if key_pool is None:
            raise ValueError("key_pool is required when stage_results are not supplied")
        stage_results = {
            stage: run_stage(model, stage, key_pool)
            for stage in names
        }
    health = stage_results["health"]
    throughput_results = [
        stage_results[name]
        for name in names
        if name.startswith("throughput-") and name in stage_results
    ]
    long_generation = stage_results["long-generation"]
    calls_per_model = 1 + len(throughput_results) + 1

    all_results = [health, *throughput_results, long_generation]
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

    long_response = (
        long_generation.get("visibleResponseFull")
        or long_generation.get("responseFull")
        or ""
    )
    long_finish_reason = long_generation.get("finishReason")
    long_diagnostics = analyze_long_response(long_response, long_finish_reason)
    long_decode_tps = decode_tps(
        long_generation.get("tokensGenerated"),
        long_generation.get("responseTime"),
        long_generation.get("timeToFirstToken"),
    )
    long_char_rate = chars_per_second(
        long_response,
        long_generation.get("responseTime"),
        long_generation.get("timeToFirstToken"),
    )
    row = {
        "model": model,
        "testKind": "suite-v4-longgen",
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
        "longSuccess": bool(long_generation.get("success")),
        "longResponse": long_response or None,
        "longFinishReason": long_finish_reason,
        "longTokensGenerated": long_generation.get("tokensGenerated"),
        "longTotalTokens": long_generation.get("totalTokens"),
        "longResponseTime": long_generation.get("responseTime"),
        "longTtft": long_generation.get("timeToFirstToken"),
        "longDecodeTps": long_decode_tps,
        "longCharsPerSecond": long_char_rate,
        "longError": long_generation.get("error"),
        "longMaxTokens": LONG_TASK_MAX_TOKENS,
        **{f"long{key[0].upper()}{key[1:]}": value for key, value in long_diagnostics.items()},
        "requestCount": calls_per_model,
        "apiKeyIndexes": [result["apiKeyIndex"] for result in all_results],
    }
    mark = "OK" if available else health["status"]
    print(
        f"    → {model} {mark} calls={calls_per_model} keys={row['apiKeyIndexes']} "
        f"health={row.get('responseTime')}ms ttft={row.get('timeToFirstToken')} "
        f"valid_samples={row.get('throughputSampleCount')}/{len(throughput_results)} "
        f"tps={row.get('decodeTps')} cv={row.get('throughputCv')} "
        f"long={row.get('longTokensGenerated')}tok/{row.get('longResponseChars')}ch "
        f"files={row.get('longFilesComplete')}/6 finish={row.get('longFinishReason')} "
        f"{row.get('error') or ''}",
        flush=True,
    )
    return row


def run_suite_round(
    *,
    fleet: list[str],
    batch: list[str],
    key_pool: ApiKeyPool,
    stage_names: tuple[str, ...],
    cursor_start: int,
    cursor_end: int,
    round_index: int,
    max_workers: int,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """One full equal-coverage pass over ``batch``; every model gets the same stages."""
    stage_jobs = build_stage_jobs(batch, stage_names)
    total_stage_tasks = len(stage_jobs)
    print(
        f"=== Suite round {round_index}: models={len(batch)} "
        f"stages/model={len(stage_names)} jobs={total_stage_tasks} ===",
        flush=True,
    )
    stage_results_by_model: dict[str, dict[str, dict[str, Any]]] = {
        model: {} for model in batch
    }
    workers = min(max_workers, total_stage_tasks)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_stage, model, stage, key_pool): (model, stage)
            for model, stage in stage_jobs
        }
        for future in as_completed(futures):
            model, stage = futures[future]
            try:
                stage_results_by_model[model][stage] = future.result()
            except Exception as exc:  # defensive: preserve the rest of the fleet run
                print(
                    f"  [internal-error] {model}/{stage}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                stage_results_by_model[model][stage] = {
                    "success": False,
                    "status": STATUS_ERROR,
                    "httpStatus": None,
                    "responseTime": None,
                    "timeToFirstToken": None,
                    "tokensGenerated": None,
                    "totalTokens": None,
                    "error": f"Internal worker error: {type(exc).__name__}: {exc}",
                    "apiKeyIndex": -1,
                }

    all_rows = [
        run_model(model, None, stage_results_by_model[model], stage_names)
        for model in batch
    ]
    all_rows.sort(key=lambda row: row["model"])

    timestamp = utc_now()
    run_id = write_rolling_batch(
        timestamp=timestamp,
        prompt=BENCHMARK_PROMPT,
        models=all_rows,
        batch_meta={
            "batch_size": len(batch),
            "cursor_start": cursor_start,
            "cursor_end": cursor_end,
            "kind": "suite-v4-longgen",
            "benchmark_version": BENCHMARK_VERSION,
            "suite_round": round_index,
            "throughput_samples": sum(1 for s in stage_names if s.startswith("throughput-")),
        },
    )
    meta = {
        "timestamp": timestamp,
        "runId": run_id,
        "roundIndex": round_index,
        "batchSize": len(batch),
    }
    return all_rows, run_id, meta


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
    conn.close()

    batch, c_start, c_end, new_cursor = next_batch(fleet, cursor, BATCH_SIZE)
    stage_names = get_stage_names()
    calls_per_model = len(stage_names)
    total_stage_tasks = len(batch) * calls_per_model
    default_workers = total_stage_tasks
    max_workers = max(1, int(os.getenv("NIM_MAX_IN_FLIGHT", str(default_workers))))
    per_key_rpm = int(os.getenv("NIM_MAX_REQUESTS_PER_MINUTE", "30"))
    max_rounds = SUITE_ROUNDS if SUITE_ROUNDS > 0 else 10**9
    deadline = time.monotonic() + RUN_BUDGET_SECONDS

    print(
        f"Rolling plan: fleet={len(fleet)} batch={len(batch)} "
        f"stages/model={calls_per_model} ({', '.join(stage_names)}) "
        f"max_rounds={'auto' if SUITE_ROUNDS <= 0 else SUITE_ROUNDS} "
        f"budget_s={RUN_BUDGET_SECONDS}",
        flush=True,
    )
    print(
        f"Request pool: keys={key_pool.key_count} per_key_rpm={per_key_rpm} "
        f"total_rpm_budget={per_key_rpm * key_pool.key_count} "
        f"jobs/round={total_stage_tasks} workers={max_workers}",
        flush=True,
    )
    print(
        f"Rolling batch: cursor {c_start}→{new_cursor} "
        f"(size={len(batch)}/{len(fleet)}) models={batch}",
        flush=True,
    )

    rounds_done = 0
    last_rows: list[dict[str, Any]] = []
    run_ids: list[int] = []
    last_pass_seconds = 0.0
    job_started = time.monotonic()

    while rounds_done < max_rounds:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("Budget exhausted — stopping suite rounds.", flush=True)
            break
        # After the first pass, require enough time for another full pass.
        if rounds_done > 0 and last_pass_seconds > 0 and remaining < last_pass_seconds * 0.85:
            print(
                f"Remaining {remaining:.0f}s < 85% of last pass "
                f"({last_pass_seconds:.0f}s) — stopping for balanced cut.",
                flush=True,
            )
            break
        # Always leave a little floor so we can persist + exit cleanly.
        if rounds_done > 0 and remaining < 120:
            print(f"Remaining {remaining:.0f}s < 120s floor — stopping.", flush=True)
            break

        pass_started = time.monotonic()
        rows, run_id, _meta = run_suite_round(
            fleet=fleet,
            batch=batch,
            key_pool=key_pool,
            stage_names=stage_names,
            cursor_start=c_start,
            cursor_end=c_end,
            round_index=rounds_done + 1,
            max_workers=max_workers,
        )
        last_pass_seconds = time.monotonic() - pass_started
        rounds_done += 1
        last_rows = rows
        run_ids.append(run_id)
        print(
            f"=== Suite round {rounds_done} complete in {last_pass_seconds:.1f}s "
            f"(run_id={run_id}, remaining_budget="
            f"{max(0.0, deadline - time.monotonic()):.0f}s) ===",
            flush=True,
        )

    if rounds_done == 0:
        print("No suite rounds completed", file=sys.stderr)
        return 3

    timestamp = utc_now()
    conn = sqlite3.connect(str(HISTORY_DB))
    init_schema(conn)
    set_state(conn, "cursor", str(new_cursor))
    set_state(conn, "last_batch_at", timestamp)
    set_state(conn, "last_run_id", str(run_ids[-1]))
    set_state(conn, "benchmark_version", BENCHMARK_VERSION)
    set_state(conn, "stale_after_minutes", str(STALE_AFTER_MINUTES))
    set_state(conn, "suite_rounds_last_job", str(rounds_done))
    conn.commit()
    conn.close()

    # Summaries from the latest round (dashboard "current" view) + job totals.
    successful = [r for r in last_rows if r.get("success")]
    valid_throughput = [r for r in successful if r.get("throughputValid")]
    completed_long_outputs = [r for r in successful if r.get("longOutputComplete")]
    by_status: dict[str, int] = {}
    for r in last_rows:
        st = r.get("status") or STATUS_ERROR
        by_status[st] = by_status.get(st, 0) + 1

    elapsed = time.monotonic() - job_started
    summary = {
        "timestamp": timestamp,
        "runId": run_ids[-1],
        "runIds": run_ids,
        "suiteRounds": rounds_done,
        "batchSize": len(batch),
        "cursorStart": c_start,
        "cursorEnd": new_cursor,
        "fleetSize": len(fleet),
        "byStatus": by_status,
        "successCount": len(successful),
        "validThroughputCount": len(valid_throughput),
        "longOutputCompleteCount": len(completed_long_outputs),
        "longTaskMaxTokens": LONG_TASK_MAX_TOKENS,
        "benchmarkVersion": BENCHMARK_VERSION,
        "throughputTargetTokens": THROUGHPUT_TARGET_TOKENS,
        "throughputMinValidTokens": THROUGHPUT_MIN_VALID_TOKENS,
        "throughputSampleCountConfigured": sum(
            1 for s in stage_names if s.startswith("throughput-")
        ),
        "catalog": catalog_meta,
        "rateLimitRpm": per_key_rpm,
        "totalRpmBudget": per_key_rpm * key_pool.key_count,
        "apiKeyCount": key_pool.key_count,
        "requestCount": key_pool.request_count,
        "requestsPerModelPerRound": calls_per_model,
        "requestsPerModelThisJob": calls_per_model * rounds_done,
        "requestsByKey": {
            str(index): sum(
                row.get("apiKeyIndexes", []).count(index) for row in last_rows
            )
            for index in range(key_pool.key_count)
        },
        "maxInFlight": max_workers,
        "runBudgetSeconds": RUN_BUDGET_SECONDS,
        "elapsedSeconds": round(elapsed, 1),
        "stageNames": list(stage_names),
    }
    RESULTS_OUT.write_text(
        json.dumps({"summary": summary, "models": last_rows}, indent=2),
        encoding="utf-8",
    )
    snap = export_fleet_snapshot()
    FLEET_OUT.write_text(json.dumps(snap, indent=2), encoding="utf-8")

    print()
    print("=== Rolling job complete ===")
    print(json.dumps(summary, indent=2))
    print(f"Fleet snapshot counts: {snap.get('counts')}")
    print(f"history.db → {HISTORY_DB}")
    print(
        f"Balanced coverage: {rounds_done} equal suite round(s) × "
        f"{len(batch)} models × {calls_per_model} calls/model "
        f"= {rounds_done * len(batch) * calls_per_model} staged requests "
        f"(plus catalog).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

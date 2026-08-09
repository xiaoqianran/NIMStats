#!/usr/bin/env python3
"""Live availability probe for NVIDIA hosted NIM chat models.

Catalog listing != callable for this account. This module verifies with a tiny
chat/completions request and classifies the outcome.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
AVAIL_CACHE_PATH = SCRIPT_DIR / "availability_cache.json"

# Status vocabulary (stable for dashboard / history consumers)
STATUS_AVAILABLE = "AVAILABLE"
STATUS_GONE = "GONE"  # deprecated / function not found for account
STATUS_UNAUTHORIZED = "UNAUTHORIZED"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED = "SKIPPED"  # filtered out before probe (non-chat)

TERMINAL_UNAVAILABLE = {
    STATUS_GONE,
    STATUS_UNAUTHORIZED,
    STATUS_ERROR,
}


def _load_dotenv() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

API_BASE = os.getenv("API_BASE", "https://integrate.api.nvidia.com/v1")
API_KEY = os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY") or ""
PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT_SECONDS", "20"))
PROBE_MAX_TOKENS = int(os.getenv("PROBE_MAX_TOKENS", "4"))
PROBE_PROMPT = os.getenv("PROBE_PROMPT", "Reply with exactly: OK")


@dataclass
class ProbeResult:
    model: str
    status: str
    available: bool
    http_status: int | None
    latency_ms: int | None
    error: str | None
    detail: str | None = None
    probed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_error_text(body: str, status_code: int) -> str:
    if not body:
        return f"HTTP {status_code}"
    # SSE error
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("data: ") and s[6:] != "[DONE]":
            try:
                chunk = json.loads(s[6:])
                err = chunk.get("error")
                if isinstance(err, dict):
                    return f"HTTP {status_code}: {err.get('message') or err}"
                if isinstance(err, str):
                    return f"HTTP {status_code}: {err}"
            except json.JSONDecodeError:
                pass
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return f"HTTP {status_code}: {body[:240]}"

    for key in ("detail", "title", "message"):
        if isinstance(data.get(key), str) and data[key]:
            # combine title+detail when both present
            title = data.get("title") if key == "detail" else None
            if key == "detail" and isinstance(title, str) and title:
                return f"HTTP {status_code}: {title}: {data[key]}"
            return f"HTTP {status_code}: {data[key]}"
    err = data.get("error")
    if isinstance(err, dict):
        return f"HTTP {status_code}: {err.get('message') or err}"
    if isinstance(err, str):
        return f"HTTP {status_code}: {err}"
    return f"HTTP {status_code}: {body[:240]}"


def classify_http_error(status_code: int, message: str) -> str:
    """Map transport/API failures to availability status."""
    low = (message or "").lower()

    # Explicit function/account gone (the important NIM hosted case)
    if status_code == 404 and (
        "function" in low and ("not found" in low or "not found for account" in low)
    ):
        return STATUS_GONE
    if status_code == 404 and ("no longer" in low or "deprecated" in low or "end of life" in low or "eol" in low):
        return STATUS_GONE
    if status_code in (410, 451):
        return STATUS_GONE
    if status_code == 404:
        # generic 404 still means not callable for this key/route
        return STATUS_GONE

    if status_code in (401, 403):
        return STATUS_UNAUTHORIZED
    if status_code == 429:
        return STATUS_RATE_LIMITED
    if status_code in (408, 504):
        return STATUS_TIMEOUT
    if status_code >= 500:
        return STATUS_ERROR
    return STATUS_ERROR


def probe_model(
    model: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: int | None = None,
    prompt: str | None = None,
) -> ProbeResult:
    key = api_key if api_key is not None else API_KEY
    base = (api_base or API_BASE).rstrip("/")
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    prompt = PROBE_PROMPT if prompt is None else prompt
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not key:
        return ProbeResult(
            model=model,
            status=STATUS_UNAUTHORIZED,
            available=False,
            http_status=None,
            latency_ms=None,
            error="Missing API key",
            detail="Set NIM_API_KEY or NVIDIA_API_KEY",
            probed_at=now,
        )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NIMStats-probe/1.0",
        },
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            latency = int((time.perf_counter() - started) * 1000)
            status_code = getattr(resp, "status", 200) or 200
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return ProbeResult(
                    model=model,
                    status=STATUS_ERROR,
                    available=False,
                    http_status=status_code,
                    latency_ms=latency,
                    error="Invalid JSON in successful HTTP response",
                    detail=raw[:240],
                    probed_at=now,
                )

            # success path: any usable assistant payload means the function is live
            content = ""
            choices = data.get("choices") if isinstance(data, dict) else None
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message") or {}
                if isinstance(msg, dict):
                    content = (
                        msg.get("content")
                        or msg.get("reasoning_content")
                        or msg.get("reasoning")
                        or ""
                    )
                if not content:
                    content = choices[0].get("text") or ""

            # Some reasoning models return content=null but still 200 + usage
            usage = data.get("usage") if isinstance(data, dict) else None
            has_usage = isinstance(usage, dict) and (
                usage.get("completion_tokens") or usage.get("total_tokens")
            )

            if status_code < 400 and (str(content).strip() or has_usage):
                detail = str(content).strip()[:120] if str(content).strip() else "200 with usage (reasoning-only)"
                return ProbeResult(
                    model=model,
                    status=STATUS_AVAILABLE,
                    available=True,
                    http_status=status_code,
                    latency_ms=latency,
                    error=None,
                    detail=detail,
                    probed_at=now,
                )

            # empty content / weird 2xx
            err = f"HTTP {status_code}: empty or non-chat response"
            return ProbeResult(
                model=model,
                status=STATUS_ERROR,
                available=False,
                http_status=status_code,
                latency_ms=latency,
                error=err,
                detail=raw[:240],
                probed_at=now,
            )

    except urllib.error.HTTPError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        raw = exc.read().decode("utf-8", errors="replace")
        code = getattr(exc, "code", 0) or 0
        message = _extract_error_text(raw, code)
        status = classify_http_error(code, message)
        return ProbeResult(
            model=model,
            status=status,
            available=False,
            http_status=code,
            latency_ms=latency,
            error=message,
            detail=raw[:300],
            probed_at=now,
        )
    except TimeoutError:
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            model=model,
            status=STATUS_TIMEOUT,
            available=False,
            http_status=None,
            latency_ms=latency,
            error=f"Probe timed out after {timeout}s",
            probed_at=now,
        )
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        msg = str(exc)
        # urllib timeout often surfaces as URLError with timed out
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            status = STATUS_TIMEOUT
        else:
            status = STATUS_ERROR
        return ProbeResult(
            model=model,
            status=status,
            available=False,
            http_status=None,
            latency_ms=latency,
            error=f"Probe failed: {exc}",
            probed_at=now,
        )


def probe_models(
    models: list[str],
    *,
    api_key: str | None = None,
    sleep_s: float | None = None,
    verbose: bool = True,
) -> list[ProbeResult]:
    if sleep_s is None:
        sleep_s = float(os.getenv("PROBE_SLEEP_SECONDS", "0.05"))
    results: list[ProbeResult] = []
    for i, model in enumerate(models, 1):
        if verbose:
            print(f"[probe {i}/{len(models)}] {model} ...", flush=True)
        r = probe_model(model, api_key=api_key)
        results.append(r)
        if verbose:
            mark = "OK" if r.available else r.status
            extra = f"{r.latency_ms}ms" if r.latency_ms is not None else "-"
            err = f" | {r.error}" if r.error and not r.available else ""
            print(f"  → {mark} ({extra}){err}", flush=True)
        if sleep_s > 0:
            time.sleep(sleep_s)
    return results


def summarize_probes(results: list[ProbeResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    available = [r.model for r in results if r.available]
    return {
        "total_probed": len(results),
        "available_count": len(available),
        "available_models": available,
        "by_status": counts,
    }


def save_availability_cache(
    results: list[ProbeResult],
    *,
    catalog_meta: dict[str, Any] | None = None,
    path: Path = AVAIL_CACHE_PATH,
) -> None:
    doc = {
        "probed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catalog": catalog_meta or {},
        "summary": summarize_probes(results),
        "results": [r.to_dict() for r in results],
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    """CLI: probe models from argv or from catalog chat candidates."""
    sys.path.insert(0, str(SCRIPT_DIR))
    args = [a for a in sys.argv[1:] if a]
    if args:
        models = args
        meta = {"source": "cli"}
    else:
        from model_catalog import get_benchmark_models

        models, meta = get_benchmark_models(verbose=True)
    if not models:
        print("No models to probe", file=sys.stderr)
        return 1
    results = probe_models(models, verbose=True)
    summary = summarize_probes(results)
    save_availability_cache(results, catalog_meta=meta)
    print(json.dumps({"summary": summary, "cache": str(AVAIL_CACHE_PATH)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

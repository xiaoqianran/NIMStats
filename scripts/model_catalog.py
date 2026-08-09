#!/usr/bin/env python3
"""Fetch / filter NVIDIA NIM models for chat/completions benchmarks.

- Pulls https://integrate.api.nvidia.com/v1/models every run
- Writes scripts/models_cache.json on success
- Falls back to cache (then a tiny static list) if pull fails
- Filters out embeddings, rerankers, image-gen, reward, OCR/parse, safety-only, etc.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_PATH = SCRIPT_DIR / "models_cache.json"
DENYLIST_PATH = SCRIPT_DIR / "models_denylist.txt"
ALLOWLIST_PATH = SCRIPT_DIR / "models_allowlist.txt"

FETCH_TIMEOUT = int(os.getenv("MODELS_FETCH_TIMEOUT", "30"))

def _load_dotenv() -> None:
    env_path = SCRIPT_DIR.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()
API_BASE = os.getenv("API_BASE", "https://integrate.api.nvidia.com/v1")
API_KEY = os.getenv("NIM_API_KEY", "")

# Last-resort if both remote and cache are unavailable
FALLBACK_MODELS = [
    "nvidia/nemotron-mini-4b-instruct",
    "z-ai/glm-5.2",
    "minimaxai/minimax-m3",
    "meta/llama-3.3-70b-instruct",
    "moonshotai/kimi-k2.6",
    "openai/gpt-oss-120b",
    "stepfun-ai/step-3.7-flash",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
]

# Non-chat / wrong endpoint for chat.completions smoke tests
_EXCLUDE_RES = [
    # embeddings
    r"embed",
    r"\bbge[-_]",
    r"arctic-embed",
    r"nvclip",
    r"\bclip\b",
    r"e5-v",
    r"gte-",
    # rerank
    r"rerank",
    r"ranker",
    r"ranking",
    r"cross-?encoder",
    # image generation / video gen
    r"diffusion",
    r"\bflux\b",
    r"sdxl",
    r"stable-?diffusion",
    r"text-to-image",
    r"image-gen",
    r"\bimagen\b",
    r"kolors",
    r"playground-v",
    r"cosmos-predict",
    r"cosmos-transfer",
    r"text2img",
    r"img2img",
    # reward / scoring heads (not chat)
    r"reward",
    # OCR / document parse (non-chat schemas)
    r"nemoretriever-parse",
    r"nemotron-parse",
    r"\bocr\b",
    # safety / guard classifiers (not general chat)
    r"guard",
    r"content-safety",
    r"topic-control",
    r"moderation",
    r"jailbreak",
    # detectors / specialized non-chat
    r"synthetic-video-detector",
    r"ising-calibration",
    r"usdsearch",
]

_EXCLUDE_RE = re.compile("|".join(f"(?:{p})" for p in _EXCLUDE_RES), re.I)


def _read_name_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line)
    return names


def is_chat_model(model_id: str) -> bool:
    """Return True if model_id looks suitable for /v1/chat/completions."""
    if not model_id or "/" not in model_id:
        return False
    if _EXCLUDE_RE.search(model_id):
        return False
    return True


def classify_model(model_id: str) -> str:
    low = model_id.lower()
    if re.search(r"embed|bge[-_]|arctic-embed|nvclip|\bclip\b|e5-v|gte-", low):
        return "embedding"
    if re.search(r"rerank|ranker|ranking|cross-?encoder", low):
        return "rerank"
    if re.search(
        r"diffusion|flux|sdxl|stable-?diffusion|text-to-image|imagen|kolors|cosmos-predict|cosmos-transfer|text2img",
        low,
    ):
        return "image_gen"
    if re.search(r"reward", low):
        return "reward"
    if re.search(r"nemoretriever-parse|nemotron-parse|\bocr\b", low):
        return "parse_ocr"
    if re.search(r"guard|content-safety|topic-control|moderation|jailbreak", low):
        return "safety"
    if re.search(r"synthetic-video-detector|ising-calibration|usdsearch", low):
        return "specialized"
    return "chat"


def fetch_remote_models(api_key: str | None = None) -> list[dict[str, Any]]:
    key = api_key if api_key is not None else API_KEY
    if not key:
        raise RuntimeError("NIM_API_KEY not set")

    url = f"{API_BASE.rstrip('/')}/models"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "NIMStats-model-catalog/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        models = payload["data"]
    elif isinstance(payload, list):
        models = payload
    else:
        raise RuntimeError(f"Unexpected /v1/models payload keys: {list(payload)[:20]}")

    # Normalize
    out: list[dict[str, Any]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        out.append(
            {
                "id": mid,
                "object": m.get("object", "model"),
                "created": m.get("created"),
                "owned_by": m.get("owned_by") or (mid.split("/", 1)[0] if "/" in mid else ""),
                "category": classify_model(mid),
            }
        )
    out.sort(key=lambda x: x["id"])
    return out


def save_cache(models: list[dict[str, Any]], source: str = "remote") -> None:
    doc = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "api_base": API_BASE,
        "count": len(models),
        "models": models,
    }
    CACHE_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_cache() -> list[dict[str, Any]] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        doc = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    models = doc.get("models")
    if not isinstance(models, list) or not models:
        return None
    # reclassify in case filter rules updated
    for m in models:
        if isinstance(m, dict) and m.get("id"):
            m["category"] = classify_model(m["id"])
    return models


def refresh_models(api_key: str | None = None, verbose: bool = True) -> tuple[list[dict[str, Any]], str]:
    """Return (models, source) where source is remote|cache|fallback."""
    try:
        models = fetch_remote_models(api_key=api_key)
        save_cache(models, source="remote")
        if verbose:
            print(f"[model_catalog] Fetched {len(models)} models from {API_BASE}/models → {CACHE_PATH.name}")
        return models, "remote"
    except Exception as exc:  # noqa: BLE001 - network / API failures are expected
        if verbose:
            print(f"[model_catalog] Remote fetch failed: {exc}", file=sys.stderr)

    cached = load_cache()
    if cached is not None:
        if verbose:
            print(f"[model_catalog] Using local cache ({len(cached)} models) from {CACHE_PATH.name}", file=sys.stderr)
        return cached, "cache"

    if verbose:
        print(f"[model_catalog] No cache; using FALLBACK_MODELS ({len(FALLBACK_MODELS)})", file=sys.stderr)
    fallback = [
        {
            "id": mid,
            "object": "model",
            "created": None,
            "owned_by": mid.split("/", 1)[0],
            "category": classify_model(mid),
        }
        for mid in FALLBACK_MODELS
    ]
    return fallback, "fallback"


def filter_chat_models(models: list[dict[str, Any]]) -> list[str]:
    deny = _read_name_file(DENYLIST_PATH)
    allow = _read_name_file(ALLOWLIST_PATH)

    ids: list[str] = []
    for m in models:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid:
            continue
        if mid in deny:
            continue
        if mid in allow or is_chat_model(mid):
            ids.append(mid)

    # allowlist entries not in catalog still included (force)
    for mid in sorted(allow):
        if mid not in ids and mid not in deny:
            ids.append(mid)

    # stable unique
    seen: set[str] = set()
    out: list[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    out.sort()
    return out


def get_benchmark_models(
    api_key: str | None = None,
    verbose: bool = True,
    limit: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    models, source = refresh_models(api_key=api_key, verbose=verbose)
    chat_ids = filter_chat_models(models)
    chat_total = len(chat_ids)

    limit_env = os.getenv("MODEL_LIMIT", "").strip()
    if limit is None and limit_env:
        try:
            limit = int(limit_env)
        except ValueError:
            limit = None
    applied_limit = None
    if limit is not None and limit > 0:
        applied_limit = limit
        chat_ids = chat_ids[:limit]

    # breakdown
    by_cat: dict[str, int] = {}
    for m in models:
        cat = m.get("category") or classify_model(m.get("id", ""))
        by_cat[cat] = by_cat.get(cat, 0) + 1

    meta = {
        "source": source,
        "total_catalog": len(models),
        "chat_count": chat_total,
        "testing_count": len(chat_ids),
        "limit": applied_limit,
        "by_category": by_cat,
        "cache_path": str(CACHE_PATH),
    }
    if verbose:
        print(
            f"[model_catalog] source={source} catalog={len(models)} "
            f"chat_eligible={meta['chat_count']} categories={by_cat}"
        )
    return chat_ids, meta


def main() -> int:
    """CLI: refresh cache and print filtered chat models."""
    ids, meta = get_benchmark_models(verbose=True)
    print(json.dumps({"meta": meta, "models": ids}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

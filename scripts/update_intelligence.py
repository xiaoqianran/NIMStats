#!/usr/bin/env python3
"""Fetch and update model intelligence scores in history.db from Artificial Analysis.

If the API key is missing or the fetch fails, existing scores are left untouched.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DB = REPO_ROOT / "history.db"


def init_db_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(models)").fetchall()}
    if "intelligence_score" not in cols:
        conn.execute("ALTER TABLE models ADD COLUMN intelligence_score REAL DEFAULT NULL")
        conn.commit()


def fetch_intelligence_from_api(api_key: str) -> dict[str, float]:
    print("Fetching intelligence scores from Artificial Analysis API...")
    url = "https://artificialanalysis.ai/api/v2/data/llms/models"
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "User-Agent": "NIMStats Benchmark (GitHub Action)",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode())
    data = payload.get("data", [])
    api_scores: dict[str, float] = {}
    for item in data:
        slug = (item.get("slug") or "").lower()
        name = (item.get("name") or "").lower()
        evals = item.get("evaluations") or {}
        score = (
            evals.get("artificial_analysis_intelligence_index")
            or evals.get("intelligence_index")
            or evals.get("intelligence")
            or evals.get("quality_index")
            or item.get("intelligence_score")
            or evals.get("score")
        )
        if score is None:
            continue
        try:
            score_float = float(score)
        except (TypeError, ValueError):
            continue
        if slug:
            api_scores[slug] = score_float
        if name:
            api_scores[name] = score_float
    return api_scores


def fuzzy_match_score(model_name: str, api_scores: dict[str, float]) -> float | None:
    clean_name = model_name.split("/")[-1].lower() if "/" in model_name else model_name.lower()
    tokens = set(re.findall(r"[a-z0-9]+", clean_name))
    if not tokens:
        return None
    best_match = None
    best_score = 0.0
    for key, val in api_scores.items():
        key_tokens = set(re.findall(r"[a-z0-9]+", key.lower()))
        if not key_tokens:
            continue
        overlap = tokens.intersection(key_tokens)
        is_subset = key_tokens.issubset(tokens)
        ratio = len(overlap) / len(tokens)
        if not (is_subset or ratio >= 0.60):
            continue
        size_tokens_clean = [t for t in tokens if re.match(r"^\d+b$", t)]
        size_tokens_key = [t for t in key_tokens if re.match(r"^\d+b$", t)]
        if size_tokens_clean and size_tokens_key and size_tokens_clean[0] != size_tokens_key[0]:
            continue
        score = len(overlap) + ratio
        if score > best_score:
            best_score = score
            best_match = val
    return best_match


def main() -> int:
    if not HISTORY_DB.exists():
        print(f"Error: Database {HISTORY_DB} not found", file=sys.stderr)
        return 1

    api_key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
    if not api_key:
        print(
            "Warning: ARTIFICIAL_ANALYSIS_API_KEY missing — preserving existing intelligence scores.",
            file=sys.stderr,
        )
        return 0

    try:
        api_scores = fetch_intelligence_from_api(api_key)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Warning: Artificial Analysis fetch failed ({exc}) — preserving existing scores.",
            file=sys.stderr,
        )
        return 0

    if not api_scores:
        print("Warning: API returned no scores — preserving existing intelligence scores.", file=sys.stderr)
        return 0

    conn = sqlite3.connect(str(HISTORY_DB))
    try:
        init_db_schema(conn)
        models = [row[0] for row in conn.execute("SELECT name FROM models").fetchall()]
        matched = 0
        for model in models:
            score = fuzzy_match_score(model, api_scores)
            if score is None:
                continue  # keep previous value
            conn.execute(
                "UPDATE models SET intelligence_score = ? WHERE name = ?",
                (score, model),
            )
            matched += 1
        conn.commit()
        print(f"OK: matched intelligence scores for {matched}/{len(models)} models")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

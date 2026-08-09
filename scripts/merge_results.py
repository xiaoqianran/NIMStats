#!/usr/bin/env python3
"""Merge parallel group results and append a single run to history.db."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import write_run  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> int:
    all_models: list[dict] = []
    timestamp: str | None = None
    prompt: str | None = None
    catalog: dict = {}
    probe: dict = {}

    for group_file in ["results-group1.json", "results-group2.json"]:
        path = SCRIPT_DIR / group_file
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            all_models.extend(data.get("models", []))
            if not timestamp:
                timestamp = data.get("timestamp")
                prompt = data.get("prompt")
            # shallow-merge catalog/probe metadata when present
            if data.get("catalog"):
                catalog = {**catalog, **data["catalog"]}
            if data.get("probe"):
                # recompute probe later from models; keep last meta keys
                probe = {**probe, **{k: v for k, v in data["probe"].items() if k != "available_models"}}

    if not all_models:
        print("No results found!", file=sys.stderr)
        return 1

    # Deduplicate by model name (prefer benchmark phase over probe-only)
    by_name: dict[str, dict] = {}
    for m in all_models:
        name = m.get("model")
        if not name:
            continue
        prev = by_name.get(name)
        if prev is None:
            by_name[name] = m
            continue
        # prefer success / benchmark phase
        score = (1 if m.get("phase") == "benchmark" else 0) + (2 if m.get("success") else 0)
        prev_score = (1 if prev.get("phase") == "benchmark" else 0) + (2 if prev.get("success") else 0)
        if score >= prev_score:
            by_name[name] = m
    all_models = list(by_name.values())

    successful = [m for m in all_models if m.get("success")]
    success_count = len(successful)
    total_count = len(all_models)
    fastest_model = "N/A"
    fastest_time = 0
    if successful:
        fastest = min(successful, key=lambda x: x.get("responseTime") or float("inf"))
        fastest_model = fastest.get("model", "N/A")
        fastest_time = fastest.get("responseTime", 0) or 0

    status_counts: dict[str, int] = {}
    for m in all_models:
        st = m.get("status") or ("AVAILABLE" if m.get("success") else "ERROR")
        status_counts[st] = status_counts.get(st, 0) + 1

    live = sum(1 for m in all_models if m.get("status") == "AVAILABLE" or m.get("success"))
    merged_run = {
        "timestamp": timestamp,
        "prompt": prompt,
        "models": all_models,
        "catalog": catalog or None,
        "probe": probe or None,
        "summary": {
            "successCount": success_count,
            "totalModels": total_count,
            "fastestModel": fastest_model,
            "fastestTime": fastest_time,
            "byStatus": status_counts,
            "nvidiaCatalog": catalog.get("total_catalog"),
            "chatCandidates": catalog.get("chat_count") or catalog.get("testing_count"),
            "liveCallable": status_counts.get("AVAILABLE", live),
            "unavailableGone": status_counts.get("GONE", 0),
            "unauthorized": status_counts.get("UNAUTHORIZED", 0),
            "rateLimited": status_counts.get("RATE_LIMITED", 0),
            "timeouts": status_counts.get("TIMEOUT", 0),
            "errors": status_counts.get("ERROR", 0),
        },
    }

    write_run(merged_run)
    print(f"✓ Updated history.db with new run ({success_count}/{total_count} models passed)")
    print(f"  byStatus={status_counts}")

    for group_file in ["results-group1.json", "results-group2.json"]:
        path = SCRIPT_DIR / group_file
        if path.exists():
            path.unlink()
    print("✓ Cleaned up temporary group files")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

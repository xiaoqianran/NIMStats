"""Versioned, machine-verifiable workloads for the rolling NIM benchmark."""

from __future__ import annotations

import json
from typing import Any

BENCHMARK_VERSION = "nimstats-v3-2026-08"

HEALTH_MARKER = "NIM_OK_7F3A"
HEALTH_PROMPT = (
    "Availability probe. Reply with exactly NIM_OK_7F3A and nothing else."
)

# A fixed 128-token output target follows the controlled ISL/OSL approach used
# by NVIDIA's inference benchmark tooling. ``ignore_eos`` is sent when the
# hosted endpoint supports it; callers retain a compatibility fallback.
THROUGHPUT_TARGET_TOKENS = 128
THROUGHPUT_MIN_VALID_TOKENS = 116  # ceil(128 * 0.90)
THROUGHPUT_PROMPT = """Performance workload. Write one continuous plain-English paragraph about a fictional library moving its catalog from paper cards to a digital archive. Do not use Markdown, lists, headings, code, quotations, or a conclusion. Begin exactly with "At dawn, the Atlas library" and keep adding concrete operational details until the platform stops generation. Do not stop early."""

# This task needs no current facts and no judge model. Every requested value is
# derived from the records in the prompt and can be checked locally.
CAPABILITY_PROMPT = """Deterministic reasoning and instruction-following test.

Each record has: id, region, units, price, returned, priority.

A17,north,17,12,2,3
B04,west,23,8,5,4
C29,north,14,15,1,5
D11,south,31,7,8,2
E08,east,19,11,3,4
F22,west,16,13,0,1
G05,south,28,9,4,5
H31,east,21,10,2,3

Rules:
1. net_units = units - returned.
2. A record is eligible only when net_units >= 16 AND priority >= 3.
3. net_revenue = net_units * price.
4. ranking_score = net_revenue + 7 * priority.
5. Rank every eligible record by ranking_score descending; break ties by id ascending.
6. weighted_checksum = sum(1-based rank_position * ranking_score) over the full ranking.
7. verification_code is the first letter of each ranked id, concatenated in rank order, followed by "-", the sum of all eligible ranking_score values padded to four digits, followed by "-", weighted_checksum.

Return exactly one JSON object with exactly these keys in this order:
{"eligible_ids": [eligible ids sorted lexicographically], "ranked_ids": [all eligible ids in rank order], "top3_net_revenue": integer sum of net_revenue for the first three ranked records, "weighted_checksum": integer, "verification_code": string}

Do the reasoning silently. Return only the JSON object: no Markdown fence, explanation, or extra keys."""

CAPABILITY_EXPECTED = {
    "eligible_ids": ["B04", "E08", "G05", "H31"],
    "ranked_ids": ["G05", "H31", "E08", "B04"],
    "top3_net_revenue": 582,
    "weighted_checksum": 1973,
    "verification_code": "GHEB-0838-1973",
}


def extract_json_object(text: str | None) -> tuple[dict[str, Any] | None, bool]:
    """Return the first decodable object and whether the whole response is JSON."""
    raw = (text or "").strip()
    if not raw:
        return None, False
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            only_json = index == 0 and not raw[end:].strip()
            return value, only_json
    return None, False


def grade_capability_response(text: str | None) -> dict[str, Any]:
    """Programmatically score format and five independently verifiable fields."""
    value, only_json = extract_json_object(text)
    if value is None:
        return {
            "score": 0.0,
            "pass": False,
            "formatPass": False,
            "checks": {key: False for key in ("json_only", "exact_keys", *CAPABILITY_EXPECTED)},
        }

    exact_keys = list(value.keys()) == list(CAPABILITY_EXPECTED.keys())
    checks = {
        "json_only": only_json,
        "exact_keys": exact_keys,
        **{key: value.get(key) == expected for key, expected in CAPABILITY_EXPECTED.items()},
    }
    weights = {
        "json_only": 15,
        "exact_keys": 10,
        "eligible_ids": 15,
        "ranked_ids": 20,
        "top3_net_revenue": 15,
        "weighted_checksum": 15,
        "verification_code": 10,
    }
    score = float(sum(weights[key] for key, passed in checks.items() if passed))
    return {
        "score": score,
        "pass": score == 100.0,
        "formatPass": only_json and exact_keys,
        "checks": checks,
    }

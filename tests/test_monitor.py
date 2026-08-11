from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from api_key_pool import ApiKeyPool, load_api_keys  # noqa: E402
from benchmark_suite import (  # noqa: E402
    LONG_TASK_EXPECTED_FILES,
    LONG_TASK_PROMPT,
    analyze_long_response,
)
from build_pages import build_site  # noqa: E402
from db_utils import sanitize_error, write_rolling_batch  # noqa: E402
from model_catalog import classify_model, is_chat_model, refresh_models  # noqa: E402
from rate_limiter import RateLimiter  # noqa: E402
from rolling_bench import (  # noqa: E402
    build_stage_jobs,
    chat_completion,
    get_stage_names,
    next_batch,
    run_model,
)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class MonitorTests(unittest.TestCase):
    def test_rate_limiter_evenly_spaces_full_window(self) -> None:
        fake = FakeTime()
        limiter = RateLimiter(4, clock=fake.clock, sleep=fake.sleep)
        starts = []
        for _ in range(5):
            limiter.wait()
            starts.append(round(fake.now, 3))
        self.assertEqual(starts, [0.0, 15.0, 30.0, 45.0, 60.001])

    def test_key_loader_accepts_multiline_and_deduplicates(self) -> None:
        env = {
            "NIM_API_KEYS": "key-a\nkey-b,key-a",
            "NIM_API_KEY": "key-c",
            "NVIDIA_API_KEY": "key-b",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_api_keys(), ["key-a", "key-b", "key-c"])

    def test_four_hundred_calls_are_evenly_distributed_across_ten_keys(self) -> None:
        pool = ApiKeyPool([f"key-{i}" for i in range(10)], max_per_minute=1_000_000)
        counts = [0] * 10
        for _ in range(400):
            index, _ = pool.acquire_with_index()
            counts[index] += 1
        self.assertEqual(counts, [40] * 10)

    def test_stage_jobs_cover_every_model_equally(self) -> None:
        models = [f"org/model-{i}" for i in range(100)]
        stages = get_stage_names(4)  # health + 4 throughput + long = 6
        jobs = build_stage_jobs(models, stages)
        self.assertEqual(len(jobs), 600)
        self.assertEqual({stage for _, stage in jobs}, set(stages))
        self.assertTrue(
            all(sum(job[0] == model for job in jobs) == len(stages) for model in models)
        )

    def test_diffusiongemma_is_not_name_filtered(self) -> None:
        model = "google/diffusiongemma-26b-a4b-it"
        self.assertTrue(is_chat_model(model))
        self.assertEqual(classify_model(model), "chat")

    def test_provider_account_ids_are_redacted(self) -> None:
        self.assertEqual(
            sanitize_error("Function missing for account 'sensitive-id'"),
            "Function missing for account 'redacted'",
        )

    def test_catalogs_are_unioned_and_key_coverage_is_retained(self) -> None:
        catalogs = {
            "key-a": [{"id": "org/a", "category": "chat"}],
            "key-b": [
                {"id": "org/a", "category": "chat"},
                {"id": "org/b", "category": "chat"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models.json"
            cache.write_text(
                '{"models":[{"id":"org/retired","category":"chat"}]}',
                encoding="utf-8",
            )
            with (
                patch("model_catalog.CACHE_PATH", cache),
                patch(
                    "model_catalog.fetch_remote_models",
                    side_effect=lambda key: catalogs[key],
                ),
            ):
                models, source, meta = refresh_models(
                    api_keys=["key-a", "key-b"], verbose=False
                )
        self.assertEqual(source, "remote-pool")
        self.assertEqual([model["id"] for model in models], ["org/a", "org/b", "org/retired"])
        self.assertEqual(sorted(meta["model_key_indexes"]["org/a"]), [0, 1])
        self.assertEqual(meta["model_key_indexes"]["org/b"], [1])
        self.assertFalse(next(m for m in models if m["id"] == "org/retired")["catalog_active"])

    def test_zero_batch_means_complete_fleet(self) -> None:
        fleet = ["a", "b", "c"]
        batch, start, end, cursor = next_batch(fleet, 1, 0)
        self.assertEqual(batch, ["b", "c", "a"])
        self.assertEqual((start, end, cursor), (1, 4, 1))

    def test_network_timeout_is_reported_as_timeout(self) -> None:
        pool = ApiKeyPool(["test-key"], max_per_minute=1_000_000)
        with patch("rolling_bench.urllib.request.urlopen", side_effect=TimeoutError("deadline")):
            result = chat_completion(
                model="org/model",
                prompt="test",
                max_tokens=8,
                stream=True,
                key_pool=pool,
                timeout_seconds=1,
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "TIMEOUT")

    def test_long_generation_uses_objective_completion_diagnostics(self) -> None:
        response = "\n\n".join(
            f"<<<FILE:{path}>>>\n// complete {path}\n<<<END_FILE>>>"
            for path in LONG_TASK_EXPECTED_FILES
        )
        complete = analyze_long_response(response, "stop")
        self.assertEqual(complete["expectedFilesPresent"], 6)
        self.assertEqual(complete["filesComplete"], 6)
        self.assertTrue(complete["outputComplete"])
        self.assertFalse(complete["truncated"])
        self.assertNotIn("score", complete)
        self.assertIn("Do not omit code", LONG_TASK_PROMPT)

        truncated = analyze_long_response(response[:-20], "length")
        self.assertFalse(truncated["outputComplete"])
        self.assertTrue(truncated["truncated"])

    def test_model_suite_aggregates_configured_throughput_samples(self) -> None:
        long_response = "\n\n".join(
            f"<<<FILE:{path}>>>\n// complete {path}\n<<<END_FILE>>>"
            for path in LONG_TASK_EXPECTED_FILES
        )
        health = {
            "success": True, "status": "AVAILABLE", "httpStatus": 200,
            "responseTime": 120, "timeToFirstToken": 60,
            "visibleResponseFull": "NIM_OK_7F3A", "apiKeyIndex": 0,
        }
        throughput = {
            "success": True, "status": "AVAILABLE", "httpStatus": 200,
            "responseTime": 1100, "timeToFirstToken": 100,
            "tokensGenerated": 128, "totalTokens": 200,
            "visibleResponseFull": "a" * 512, "apiKeyIndex": 1,
        }
        stages = get_stage_names(4)
        side_effects = [health]
        for i in range(4):
            side_effects.append({
                **throughput,
                "responseTime": 1100 + i * 50,
                "timeToFirstToken": 100 + i * 20,
                "apiKeyIndex": i + 1,
            })
        side_effects.append({
            "success": True, "status": "AVAILABLE", "httpStatus": 200,
            "responseTime": 5000, "timeToFirstToken": 120,
            "tokensGenerated": 3072, "totalTokens": 3500,
            "visibleResponseFull": long_response, "finishReason": "stop",
            "apiKeyIndex": 5,
        })
        with patch("rolling_bench.chat_completion", side_effect=side_effects) as completion:
            with patch("rolling_bench.STAGE_NAMES", stages):
                row = run_model("org/model", object(), stage_names=stages)
        self.assertEqual(completion.call_count, 6)
        self.assertEqual(row["requestCount"], 6)
        self.assertEqual(row["throughputSampleCount"], 4)
        self.assertEqual(row["longTokensGenerated"], 3072)
        self.assertEqual(row["longFilesComplete"], 6)
        self.assertTrue(row["longOutputComplete"])
        self.assertNotIn("capabilityScore", row)
        long_call = completion.call_args_list[-1].kwargs
        self.assertTrue(long_call["stream"])
        self.assertNotIn("ignore_eos", long_call["extra_payload"])
        self.assertNotIn("top_p", long_call["extra_payload"])

    def test_latest_long_response_is_stored_without_historical_duplication(self) -> None:
        base_row = {
            "model": "org/model",
            "testKind": "suite-v4-longgen",
            "benchmarkVersion": "test-v4",
            "success": True,
            "status": "AVAILABLE",
            "httpStatus": 200,
            "responseTime": 100,
            "timeToFirstToken": 50,
            "throughputValid": True,
            "longSuccess": True,
            "longResponse": "first complete response",
            "longFinishReason": "stop",
            "longTokensGenerated": 100,
            "longTotalTokens": 120,
            "longResponseTime": 2000,
            "longTtft": 80,
            "longDecodeTps": 52.1,
            "longCharsPerSecond": 220.0,
            "longResponseChars": 23,
            "longFilesEmitted": 2,
            "longFilesComplete": 2,
            "longOutputComplete": False,
            "longTruncated": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "history.db"
            for timestamp, response in (
                ("2026-08-09T00:00:00Z", "first complete response"),
                ("2026-08-09T00:05:00Z", "newest complete response"),
            ):
                write_rolling_batch(
                    timestamp=timestamp,
                    prompt="long prompt",
                    models=[{**base_row, "longResponse": response}],
                    batch_meta={"batch_size": 1, "kind": "suite-v4-longgen"},
                    db_path=db_path,
                )
            with sqlite3.connect(db_path) as conn:
                output_rows = conn.execute(
                    "SELECT response_text, completion_tokens FROM model_outputs"
                ).fetchall()
                historical = conn.execute(
                    "SELECT long_tokens_generated FROM model_results ORDER BY run_id"
                ).fetchall()
        self.assertEqual(output_rows, [("newest complete response", 100)])
        self.assertEqual(historical, [(100,), (100,)])

    def test_pages_build_is_whitelisted_and_has_api_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site"
            files = build_site(ROOT, output)
            self.assertIn("top/speed/index.json", files)
            self.assertIn("top/speed/model", files)
            self.assertIn("top/generation/model", files)
            self.assertTrue((output / ".nojekyll").exists())
            self.assertFalse((output / "scripts" / "rolling_bench.py").exists())
            self.assertFalse((output / ".github").exists())


if __name__ == "__main__":
    unittest.main()

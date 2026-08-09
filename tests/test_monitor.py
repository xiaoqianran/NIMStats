from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from api_key_pool import ApiKeyPool, load_api_keys  # noqa: E402
from benchmark_suite import CAPABILITY_EXPECTED, grade_capability_response  # noqa: E402
from build_pages import build_site  # noqa: E402
from db_utils import sanitize_error  # noqa: E402
from model_catalog import classify_model, is_chat_model, refresh_models  # noqa: E402
from rate_limiter import RateLimiter  # noqa: E402
from rolling_bench import build_stage_jobs, next_batch, run_model  # noqa: E402


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

    def test_one_hundred_models_materialize_four_hundred_independent_jobs(self) -> None:
        models = [f"org/model-{i}" for i in range(100)]
        jobs = build_stage_jobs(models)
        self.assertEqual(len(jobs), 400)
        self.assertEqual({stage for _, stage in jobs}, {
            "health", "throughput-a", "throughput-b", "capability",
        })
        self.assertTrue(all(sum(job[0] == model for job in jobs) == 4 for model in models))

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

    def test_capability_suite_is_machine_graded(self) -> None:
        exact = __import__("json").dumps(CAPABILITY_EXPECTED, separators=(",", ":"))
        grade = grade_capability_response(exact)
        self.assertEqual(grade["score"], 100.0)
        self.assertTrue(grade["pass"])

        wrapped = grade_capability_response(f"```json\n{exact}\n```")
        self.assertEqual(wrapped["score"], 85.0)
        self.assertFalse(wrapped["pass"])

    def test_model_suite_always_makes_four_calls_and_aggregates_two_samples(self) -> None:
        exact = __import__("json").dumps(CAPABILITY_EXPECTED, separators=(",", ":"))
        health = {
            "success": True, "status": "AVAILABLE", "httpStatus": 200,
            "responseTime": 120, "timeToFirstToken": 60,
            "visibleResponseFull": "NIM_OK_7F3A", "apiKeyIndex": 0,
        }
        throughput_a = {
            "success": True, "status": "AVAILABLE", "httpStatus": 200,
            "responseTime": 1100, "timeToFirstToken": 100,
            "tokensGenerated": 128, "totalTokens": 200,
            "visibleResponseFull": "a" * 512, "apiKeyIndex": 1,
        }
        throughput_b = {
            **throughput_a, "responseTime": 1200, "timeToFirstToken": 200,
            "apiKeyIndex": 2,
        }
        capability = {
            "success": True, "status": "AVAILABLE", "httpStatus": 200,
            "responseTime": 500, "timeToFirstToken": None,
            "visibleResponseFull": exact, "apiKeyIndex": 3,
        }
        with patch(
            "rolling_bench.chat_completion",
            side_effect=[health, throughput_a, throughput_b, capability],
        ) as completion:
            row = run_model("org/model", object())
        self.assertEqual(completion.call_count, 4)
        self.assertEqual(row["apiKeyIndexes"], [0, 1, 2, 3])
        self.assertEqual(row["throughputSampleCount"], 2)
        self.assertEqual(row["capabilityScore"], 100.0)

    def test_pages_build_is_whitelisted_and_has_api_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site"
            files = build_site(ROOT, output)
            self.assertIn("top/speed/index.json", files)
            self.assertIn("top/speed/model", files)
            self.assertIn("top/capability/model", files)
            self.assertTrue((output / ".nojekyll").exists())
            self.assertFalse((output / "scripts" / "rolling_bench.py").exists())
            self.assertFalse((output / ".github").exists())


if __name__ == "__main__":
    unittest.main()

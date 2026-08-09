from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from api_key_pool import load_api_keys  # noqa: E402
from build_pages import build_site  # noqa: E402
from model_catalog import classify_model, is_chat_model, refresh_models  # noqa: E402
from rate_limiter import RateLimiter  # noqa: E402
from rolling_bench import next_batch  # noqa: E402


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

    def test_diffusiongemma_is_not_name_filtered(self) -> None:
        model = "google/diffusiongemma-26b-a4b-it"
        self.assertTrue(is_chat_model(model))
        self.assertEqual(classify_model(model), "chat")

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

    def test_pages_build_is_whitelisted_and_has_api_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site"
            files = build_site(ROOT, output)
            self.assertIn("top/speed/index.json", files)
            self.assertIn("top/speed/model/index.txt", files)
            self.assertTrue((output / ".nojekyll").exists())
            self.assertFalse((output / "scripts" / "rolling_bench.py").exists())
            self.assertFalse((output / ".github").exists())


if __name__ == "__main__":
    unittest.main()

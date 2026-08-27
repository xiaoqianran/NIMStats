#!/usr/bin/env python3
"""Build and validate the whitelisted static GitHub Pages artifact."""

from __future__ import annotations

import argparse
import shutil
import sqlite3

from build_site_data import build_site_data
from pathlib import Path


ROOT_FILES = ("index.html", "favicon.svg")
ROOT_DIRS = ("css", "js", "top")
OPTIONAL_ROOT_FILES = ("_headers", "_redirects", "robots.txt")

# GitHub Pages does not process Netlify/Cloudflare `_redirects`. Directory
# index files provide equivalent extensionless public API routes on Pages.
API_ROUTES = {
    "top/model": "top/model.txt",
    "top/speed/index.json": "top/speed.json",
    "top/speed/model": "top/speed.txt",
    "top/intelligence/index.json": "top/intelligence.json",
    "top/intelligence/model": "top/intelligence.txt",
    "top/generation/index.json": "top/generation.json",
    "top/generation/model": "top/generation.txt",
}


def validate_history(path: Path) -> None:
    # immutable avoids creating -wal/-shm sidecars beside the tracked database.
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as conn:
        quick_check = conn.execute("PRAGMA quick_check").fetchone()
        if not quick_check or quick_check[0] != "ok":
            raise RuntimeError(f"Invalid history.db: {quick_check}")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "models", "runs", "model_results", "model_outputs", "scheduler_state"
        }
        missing = required - tables
        if missing:
            raise RuntimeError(f"history.db is missing tables: {sorted(missing)}")


def build_site(source: Path, output: Path) -> list[str]:
    source = source.resolve()
    output = output.resolve()
    if output == source or output == source.parent:
        raise ValueError("Output must be a dedicated build directory")

    missing = [name for name in (*ROOT_FILES, *ROOT_DIRS) if not (source / name).exists()]
    if missing:
        raise FileNotFoundError(f"Missing required Pages inputs: {', '.join(missing)}")
    validate_history(source / "history.db")

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    for name in ROOT_FILES:
        shutil.copy2(source / name, output / name)
    for name in ROOT_DIRS:
        shutil.copytree(source / name, output / name)

    # Pre-aggregate SQLite into browser-sized JSON so the UI never needs
    # sql.js/WASM or a full 30MB+ database scan on page load.
    build_site_data(source / "history.db", output)
    for name in OPTIONAL_ROOT_FILES:
        path = source / name
        if path.is_file():
            shutil.copy2(path, output / name)
    for path in source.glob("google*.html"):
        if path.is_file():
            shutil.copy2(path, output / path.name)

    fleet = source / "scripts" / "fleet_snapshot.json"
    if fleet.is_file():
        destination = output / "scripts" / fleet.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fleet, destination)

    for destination_name, source_name in API_ROUTES.items():
        destination = output / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / source_name, destination)

    # Direct artifact deployments do not run Jekyll. Keeping this marker also
    # makes the generated directory safe to use with branch-based Pages.
    (output / ".nojekyll").touch()
    return sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, default=Path("_site"))
    args = parser.parse_args()
    files = build_site(args.source, args.output)
    print(f"Pages artifact ready: {len(files)} files in {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

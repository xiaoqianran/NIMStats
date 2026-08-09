#!/usr/bin/env python3
"""Manage models for dynamic NIM catalog + history.db.

Usage:
  python scripts/manage_models.py list
  python scripts/manage_models.py refresh          # pull /v1/models → cache
  python scripts/manage_models.py deny <model_id>  # add to denylist
  python scripts/manage_models.py allow <model_id> # force-include
  python scripts/manage_models.py undeny <model_id>
  python scripts/manage_models.py unallow <model_id>
  python scripts/manage_models.py purge            # drop DB models not in current chat set
  python scripts/manage_models.py probe            # live availability probe all chat candidates
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_utils import HISTORY_DB  # noqa: E402
from model_catalog import (  # noqa: E402
    ALLOWLIST_PATH,
    DENYLIST_PATH,
    get_benchmark_models,
    load_cache,
    refresh_models,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _write_names(path: Path, names: list[str], header: str) -> None:
    body = header.rstrip() + "\n" + "\n".join(sorted(set(names))) + ("\n" if names else "")
    path.write_text(body, encoding="utf-8")


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(HISTORY_DB))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_refresh() -> int:
    models, source = refresh_models(verbose=True)
    chat, meta = get_benchmark_models(verbose=True)
    print(f"source={source} catalog={len(models)} chat={len(chat)}")
    print(f"categories={meta['by_category']}")
    return 0


def cmd_list() -> int:
    chat, meta = get_benchmark_models(verbose=True)
    print(f"\n=== Chat-eligible models ({len(chat)}) source={meta['source']} ===")
    for i, m in enumerate(chat, 1):
        print(f"  {i:3}. {m}")

    cached = load_cache() or []
    excluded = [m["id"] for m in cached if m.get("id") and m["id"] not in chat]
    if excluded:
        print(f"\n=== Filtered out of catalog ({len(excluded)}) ===")
        for m in sorted(excluded):
            from model_catalog import classify_model

            print(f"  [{classify_model(m):10}] {m}")

    deny = _read_lines(DENYLIST_PATH)
    allow = _read_lines(ALLOWLIST_PATH)
    if deny:
        print(f"\n=== Denylist ({len(deny)}) ===")
        for m in deny:
            print(f"  - {m}")
    if allow:
        print(f"\n=== Allowlist ({len(allow)}) ===")
        for m in allow:
            print(f"  + {m}")

    conn = _db_connect()
    db_models = [r[0] for r in conn.execute("SELECT name FROM models ORDER BY name").fetchall()]
    print(f"\n=== Models in history.db ({len(db_models)}) ===")
    chat_set = set(chat)
    for m in db_models:
        flag = "CHAT" if m in chat_set else "STALE"
        count = conn.execute(
            "SELECT COUNT(*) FROM model_results mr JOIN models md ON mr.model_id = md.id WHERE md.name = ?",
            (m,),
        ).fetchone()[0]
        print(f"  {flag:5} {m:55} {count} results")
    conn.close()
    return 0


def cmd_deny(model_id: str) -> int:
    names = _read_lines(DENYLIST_PATH)
    if model_id in names:
        print(f"Already denied: {model_id}")
        return 1
    names.append(model_id)
    _write_names(
        DENYLIST_PATH,
        names,
        "# One model id per line. Lines starting with # are comments.",
    )
    print(f"Added to denylist: {model_id}")
    return 0


def cmd_undeny(model_id: str) -> int:
    names = _read_lines(DENYLIST_PATH)
    if model_id not in names:
        print(f"Not in denylist: {model_id}")
        return 1
    names = [n for n in names if n != model_id]
    _write_names(
        DENYLIST_PATH,
        names,
        "# One model id per line. Lines starting with # are comments.",
    )
    print(f"Removed from denylist: {model_id}")
    return 0


def cmd_allow(model_id: str) -> int:
    names = _read_lines(ALLOWLIST_PATH)
    if model_id in names:
        print(f"Already allowlisted: {model_id}")
        return 1
    names.append(model_id)
    _write_names(
        ALLOWLIST_PATH,
        names,
        "# Force-include model ids even if filter would drop them.",
    )
    print(f"Added to allowlist: {model_id}")
    return 0


def cmd_unallow(model_id: str) -> int:
    names = _read_lines(ALLOWLIST_PATH)
    if model_id not in names:
        print(f"Not in allowlist: {model_id}")
        return 1
    names = [n for n in names if n != model_id]
    _write_names(
        ALLOWLIST_PATH,
        names,
        "# Force-include model ids even if filter would drop them.",
    )
    print(f"Removed from allowlist: {model_id}")
    return 0




def cmd_probe() -> int:
    from model_probe import probe_models, save_availability_cache, summarize_probes
    chat, meta = get_benchmark_models(verbose=True)
    results = probe_models(chat, verbose=True)
    summary = summarize_probes(results)
    save_availability_cache(results, catalog_meta=meta)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0

def cmd_purge() -> int:
    chat, _meta = get_benchmark_models(verbose=True)
    configured = set(chat)
    conn = _db_connect()
    db_models = [r[0] for r in conn.execute("SELECT name FROM models ORDER BY name").fetchall()]
    orphans = [m for m in db_models if m not in configured]

    purged_models = 0
    total_results = 0

    if orphans:
        print(f"Found {len(orphans)} stale model(s) not in current chat set:")
        for m in orphans:
            row = conn.execute("SELECT id FROM models WHERE name = ?", (m,)).fetchone()
            mid = row[0]
            count = conn.execute(
                "SELECT COUNT(*) FROM model_results WHERE model_id = ?", (mid,)
            ).fetchone()[0]
            total_results += count
            print(f"  {m:55} {count} results")

        for m in orphans:
            row = conn.execute("SELECT id FROM models WHERE name = ?", (m,)).fetchone()
            mid = row[0]
            conn.execute(
                "UPDATE runs SET fastest_model_id = NULL, fastest_time = NULL WHERE fastest_model_id = ?",
                (mid,),
            )
            conn.execute("DELETE FROM model_results WHERE model_id = ?", (mid,))
            conn.execute("DELETE FROM models WHERE id = ?", (mid,))
            purged_models += 1

    cur_errors = conn.execute(
        "DELETE FROM errors WHERE id NOT IN (SELECT DISTINCT error_id FROM model_results WHERE error_id IS NOT NULL)"
    )
    cur_prompts = conn.execute(
        "DELETE FROM prompts WHERE id NOT IN (SELECT DISTINCT prompt_id FROM runs)"
    )

    any_changes = purged_models > 0 or cur_errors.rowcount > 0 or cur_prompts.rowcount > 0
    if any_changes:
        conn.commit()
        conn.execute("VACUUM")
        print("\nPurge complete:")
        if purged_models:
            print(f"  - Purged {purged_models} model(s), {total_results} results")
        if cur_errors.rowcount:
            print(f"  - Cleaned {cur_errors.rowcount} orphan error(s)")
        if cur_prompts.rowcount:
            print(f"  - Cleaned {cur_prompts.rowcount} orphan prompt(s)")
    else:
        print("DB already matches current chat set.")

    conn.close()
    return 0


USAGE = """Usage:
  python scripts/manage_models.py list
  python scripts/manage_models.py refresh
  python scripts/manage_models.py deny <model_id>
  python scripts/manage_models.py undeny <model_id>
  python scripts/manage_models.py allow <model_id>
  python scripts/manage_models.py unallow <model_id>
  python scripts/manage_models.py purge
  python scripts/manage_models.py probe"""


def main() -> int:
    # load .env like test_models
    env_path = SCRIPT_DIR.parent / ".env"
    if env_path.exists():
        import os

        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v

    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return 1
    cmd = args[0]
    if cmd == "list":
        return cmd_list()
    if cmd == "refresh":
        return cmd_refresh()
    if cmd == "deny" and len(args) == 2:
        return cmd_deny(args[1])
    if cmd == "undeny" and len(args) == 2:
        return cmd_undeny(args[1])
    if cmd == "allow" and len(args) == 2:
        return cmd_allow(args[1])
    if cmd == "unallow" and len(args) == 2:
        return cmd_unallow(args[1])
    if cmd == "purge":
        return cmd_purge()
    if cmd == "probe":
        return cmd_probe()
    print(USAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

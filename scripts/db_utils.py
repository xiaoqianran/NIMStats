"""Shared SQLite utilities for rolling NIM fleet monitoring."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DB = REPO_ROOT / "history.db"
MAX_RUNS = int(__import__("os").getenv("MAX_HISTORY_RUNS", "2000"))
# Models not re-checked within this window surface as STALE on the dashboard
STALE_AFTER_MINUTES = int(__import__("os").getenv("STALE_AFTER_MINUTES", "180"))

STATUS_AVAILABLE = "AVAILABLE"
STATUS_GONE = "GONE"
STATUS_UNAUTHORIZED = "UNAUTHORIZED"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR = "ERROR"
STATUS_STALE = "STALE"
STATUS_UNKNOWN = "UNKNOWN"


def sanitize_error(message: str | None) -> str | None:
    """Remove provider account identifiers from public history and logs."""
    if not message:
        return message
    return re.sub(
        r"(?i)(account\s+['\"])[^'\"]+(['\"])",
        r"\1redacted\2",
        message,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS models (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            intelligence_score REAL DEFAULT NULL,
            current_status TEXT DEFAULT 'UNKNOWN',
            last_checked_at TEXT,
            last_success_at TEXT,
            last_http_status INTEGER,
            last_error TEXT,
            last_ttft_ms INTEGER,
            last_latency_ms INTEGER,
            last_decode_tps REAL,
            last_test_kind TEXT,
            last_throughput_valid INTEGER,
            last_chars_per_second REAL,
            last_capability_score REAL,
            last_capability_pass INTEGER,
            last_benchmark_version TEXT,
            last_throughput_at TEXT,
            last_capability_at TEXT,
            last_throughput_sample_count INTEGER,
            last_throughput_cv REAL
        );
        CREATE TABLE IF NOT EXISTS errors (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT    NOT NULL,
            prompt_id        INTEGER NOT NULL REFERENCES prompts(id),
            fastest_model_id INTEGER          REFERENCES models(id),
            fastest_time     INTEGER,
            batch_size       INTEGER,
            cursor_start     INTEGER,
            cursor_end       INTEGER,
            kind             TEXT DEFAULT 'rolling'
        );
        CREATE TABLE IF NOT EXISTS model_results (
            run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            model_id              INTEGER NOT NULL REFERENCES models(id),
            success               INTEGER NOT NULL DEFAULT 0,
            error_id              INTEGER          REFERENCES errors(id),
            response_time         INTEGER,
            tokens_generated      INTEGER,
            total_tokens          INTEGER,
            time_to_first_token   INTEGER,
            status                TEXT,
            http_status           INTEGER,
            test_kind             TEXT,
            decode_tps            REAL,
            throughput_valid      INTEGER,
            chars_per_second      REAL,
            capability_score      REAL,
            capability_pass       INTEGER,
            format_pass           INTEGER,
            benchmark_version     TEXT,
            throughput_latency_ms INTEGER,
            throughput_ttft_ms    INTEGER,
            throughput_sample_count INTEGER,
            throughput_cv         REAL,
            long_tokens_generated INTEGER,
            long_total_tokens     INTEGER,
            long_latency_ms       INTEGER,
            long_ttft_ms          INTEGER,
            long_decode_tps       REAL,
            long_chars_per_second REAL,
            long_response_chars   INTEGER,
            long_files_emitted    INTEGER,
            long_files_complete   INTEGER,
            long_output_complete  INTEGER,
            long_truncated        INTEGER,
            long_finish_reason    TEXT,
            PRIMARY KEY (run_id, model_id, test_kind)
        );
        CREATE TABLE IF NOT EXISTS model_outputs (
            model_id              INTEGER PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
            run_id                INTEGER REFERENCES runs(id) ON DELETE SET NULL,
            updated_at            TEXT NOT NULL,
            benchmark_version     TEXT,
            response_text         TEXT,
            finish_reason         TEXT,
            completion_tokens     INTEGER,
            total_tokens          INTEGER,
            response_time_ms      INTEGER,
            ttft_ms               INTEGER,
            decode_tps            REAL,
            chars_per_second      REAL,
            response_chars        INTEGER,
            files_emitted         INTEGER,
            files_complete        INTEGER,
            output_complete       INTEGER,
            truncated             INTEGER,
            error_text            TEXT
        );
        CREATE TABLE IF NOT EXISTS scheduler_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_ts  ON runs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_mr_model ON model_results(model_id);
        """
    )
    _migrate_columns(conn)
    _sanitize_stored_errors(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_models_status ON models(current_status)"
    )


def _migrate_columns(conn: sqlite3.Connection) -> None:
    def cols(table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    mcols = cols("models")
    for col, decl in [
        ("intelligence_score", "REAL DEFAULT NULL"),
        ("current_status", "TEXT DEFAULT 'UNKNOWN'"),
        ("last_checked_at", "TEXT"),
        ("last_success_at", "TEXT"),
        ("last_http_status", "INTEGER"),
        ("last_error", "TEXT"),
        ("last_ttft_ms", "INTEGER"),
        ("last_latency_ms", "INTEGER"),
        ("last_decode_tps", "REAL"),
        ("last_test_kind", "TEXT"),
        ("last_throughput_valid", "INTEGER"),
        ("last_chars_per_second", "REAL"),
        ("last_capability_score", "REAL"),
        ("last_capability_pass", "INTEGER"),
        ("last_benchmark_version", "TEXT"),
        ("last_throughput_at", "TEXT"),
        ("last_capability_at", "TEXT"),
        ("last_throughput_sample_count", "INTEGER"),
        ("last_throughput_cv", "REAL"),
    ]:
        if col not in mcols:
            conn.execute(f"ALTER TABLE models ADD COLUMN {col} {decl}")

    rcols = cols("runs")
    for col, decl in [
        ("batch_size", "INTEGER"),
        ("cursor_start", "INTEGER"),
        ("cursor_end", "INTEGER"),
        ("kind", "TEXT DEFAULT 'rolling'"),
    ]:
        if col not in rcols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")

    mrcols = cols("model_results")
    for col, decl in [
        ("time_to_first_token", "INTEGER"),
        ("status", "TEXT"),
        ("http_status", "INTEGER"),
        ("test_kind", "TEXT DEFAULT 'legacy'"),
        ("decode_tps", "REAL"),
        ("throughput_valid", "INTEGER"),
        ("chars_per_second", "REAL"),
        ("capability_score", "REAL"),
        ("capability_pass", "INTEGER"),
        ("format_pass", "INTEGER"),
        ("benchmark_version", "TEXT"),
        ("throughput_latency_ms", "INTEGER"),
        ("throughput_ttft_ms", "INTEGER"),
        ("throughput_sample_count", "INTEGER"),
        ("throughput_cv", "REAL"),
        ("long_tokens_generated", "INTEGER"),
        ("long_total_tokens", "INTEGER"),
        ("long_latency_ms", "INTEGER"),
        ("long_ttft_ms", "INTEGER"),
        ("long_decode_tps", "REAL"),
        ("long_chars_per_second", "REAL"),
        ("long_response_chars", "INTEGER"),
        ("long_files_emitted", "INTEGER"),
        ("long_files_complete", "INTEGER"),
        ("long_output_complete", "INTEGER"),
        ("long_truncated", "INTEGER"),
        ("long_finish_reason", "TEXT"),
    ]:
        if col not in mrcols:
            conn.execute(f"ALTER TABLE model_results ADD COLUMN {col} {decl}")

    # Old DBs may have PRIMARY KEY (run_id, model_id) without test_kind.
    # SQLite cannot easily alter PK; new inserts use test_kind column.
    # For uniqueness on old schema, we tolerate duplicate risk by using REPLACE in write.


def _sanitize_stored_errors(conn: sqlite3.Connection) -> None:
    """One-way cleanup for account IDs stored by older benchmark versions."""
    for error_id, text in conn.execute("SELECT id, text FROM errors").fetchall():
        cleaned = sanitize_error(text)
        if cleaned == text:
            continue
        existing = conn.execute(
            "SELECT id FROM errors WHERE text = ?", (cleaned,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE model_results SET error_id = ? WHERE error_id = ?",
                (existing[0], error_id),
            )
            conn.execute("DELETE FROM errors WHERE id = ?", (error_id,))
        else:
            conn.execute(
                "UPDATE errors SET text = ? WHERE id = ?", (cleaned, error_id)
            )
    for model_id, error in conn.execute(
        "SELECT id, last_error FROM models WHERE last_error IS NOT NULL"
    ).fetchall():
        cleaned = sanitize_error(error)
        if cleaned != error:
            conn.execute(
                "UPDATE models SET last_error = ? WHERE id = ?", (cleaned, model_id)
            )


def _get_or_create(conn: sqlite3.Connection, table: str, col: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    row = conn.execute(f"SELECT id FROM {table} WHERE {col} = ?", (value,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(f"INSERT INTO {table} ({col}) VALUES (?)", (value,))
    return cur.lastrowid


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM scheduler_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO scheduler_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def ensure_models(conn: sqlite3.Connection, names: list[str]) -> None:
    for name in names:
        conn.execute(
            "INSERT OR IGNORE INTO models(name, current_status) VALUES(?, ?)",
            (name, STATUS_UNKNOWN),
        )


def compute_display_status(
    current_status: str | None,
    last_checked_at: str | None,
    *,
    now: datetime | None = None,
    stale_after_minutes: int = STALE_AFTER_MINUTES,
) -> str:
    """Per-model display status: STALE if last check too old."""
    status = current_status or STATUS_UNKNOWN
    if not last_checked_at:
        return STATUS_STALE if status == STATUS_AVAILABLE else status
    try:
        checked = datetime.strptime(last_checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return STATUS_STALE
    now = now or datetime.now(timezone.utc)
    age_min = (now - checked).total_seconds() / 60.0
    if age_min > stale_after_minutes:
        return STATUS_STALE
    return status


def write_rolling_batch(
    *,
    timestamp: str,
    prompt: str,
    models: list[dict[str, Any]],
    batch_meta: dict[str, Any] | None = None,
    db_path: Path = HISTORY_DB,
) -> int:
    """
    Persist one rolling batch run.
    ``models`` may contain legacy health/throughput pairs or one unified
    throughput row. The preferred row is persisted for dashboard history.
    """
    batch_meta = batch_meta or {}
    conn = sqlite3.connect(str(db_path))
    try:
        init_schema(conn)
        prompt_id = _get_or_create(conn, "prompts", "text", prompt)

        # Fastest among successful throughput (or health) latencies
        successful = [
            m
            for m in models
            if m.get("success") and isinstance(m.get("responseTime"), int)
        ]
        fastest_model_id = None
        fastest_time = None
        if successful:
            # Prefer throughput test for "fastest", else health
            thr = [m for m in successful if m.get("testKind") == "throughput"]
            pool = thr or successful
            best = min(pool, key=lambda x: x["responseTime"])
            fastest_model_id = _get_or_create(conn, "models", "name", best.get("model"))
            fastest_time = best["responseTime"]

        cur = conn.execute(
            """INSERT INTO runs
               (timestamp, prompt_id, fastest_model_id, fastest_time, batch_size, cursor_start, cursor_end, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp,
                prompt_id,
                fastest_model_id,
                fastest_time,
                batch_meta.get("batch_size"),
                batch_meta.get("cursor_start"),
                batch_meta.get("cursor_end"),
                batch_meta.get("kind", "rolling"),
            ),
        )
        run_id = cur.lastrowid

        # Group by model for current_status update. Legacy runs use health as
        # availability truth; unified runs use their only throughput row.
        by_model: dict[str, list[dict[str, Any]]] = {}
        for m in models:
            by_model.setdefault(m["model"], []).append(m)

        for model_name, rows in by_model.items():
            model_id = _get_or_create(conn, "models", "name", model_name)
            health = next((r for r in rows if r.get("testKind") == "health"), rows[0])
            thr = next((r for r in rows if r.get("testKind") == "throughput"), None)

            # One historical row per model per run (SQLite PK compat + simple charts).
            primary = dict(health)
            if thr and thr.get("success"):
                primary = {
                    **health,
                    "success": True if health.get("success") else thr.get("success"),
                    "responseTime": thr.get("responseTime"),
                    "tokensGenerated": thr.get("tokensGenerated"),
                    "totalTokens": thr.get("totalTokens"),
                    "timeToFirstToken": thr.get("timeToFirstToken") or health.get("timeToFirstToken"),
                    "decodeTps": thr.get("decodeTps"),
                    "testKind": "throughput",
                }
            elif thr and not health.get("success"):
                # health failed — keep health status as availability truth
                primary = dict(health)

            for m in [primary]:
                error_id = _get_or_create(
                    conn, "errors", "text", sanitize_error(m.get("error"))
                )
                test_kind = m.get("testKind") or "legacy"
                conn.execute(
                    "DELETE FROM model_results WHERE run_id=? AND model_id=?",
                    (run_id, model_id),
                )
                try:
                    conn.execute(
                        """INSERT INTO model_results
                           (run_id, model_id, success, error_id, response_time, tokens_generated,
                            total_tokens, time_to_first_token, status, http_status, test_kind, decode_tps,
                            throughput_valid, chars_per_second, capability_score, capability_pass,
                            format_pass, benchmark_version, throughput_latency_ms, throughput_ttft_ms,
                            throughput_sample_count, throughput_cv, long_tokens_generated,
                            long_total_tokens, long_latency_ms, long_ttft_ms, long_decode_tps,
                            long_chars_per_second, long_response_chars, long_files_emitted,
                            long_files_complete, long_output_complete, long_truncated,
                            long_finish_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            model_id,
                            1 if m.get("success") else 0,
                            error_id,
                            m.get("responseTime"),
                            m.get("tokensGenerated"),
                            m.get("totalTokens"),
                            m.get("timeToFirstToken"),
                            m.get("status"),
                            m.get("httpStatus"),
                            test_kind,
                            m.get("decodeTps"),
                            1 if m.get("throughputValid") else 0,
                            m.get("charsPerSecond"),
                            m.get("capabilityScore"),
                            1 if m.get("capabilityPass") else 0,
                            1 if m.get("formatPass") else 0,
                            m.get("benchmarkVersion"),
                            m.get("throughputResponseTime"),
                            m.get("throughputTtft"),
                            m.get("throughputSampleCount"),
                            m.get("throughputCv"),
                            m.get("longTokensGenerated"),
                            m.get("longTotalTokens"),
                            m.get("longResponseTime"),
                            m.get("longTtft"),
                            m.get("longDecodeTps"),
                            m.get("longCharsPerSecond"),
                            m.get("longResponseChars"),
                            m.get("longFilesEmitted"),
                            m.get("longFilesComplete"),
                            1 if m.get("longOutputComplete") else 0,
                            1 if m.get("longTruncated") else 0,
                            m.get("longFinishReason"),
                        ),
                    )
                except sqlite3.IntegrityError:
                    conn.execute(
                        """UPDATE model_results SET
                           success=?, error_id=?, response_time=?, tokens_generated=?,
                           total_tokens=?, time_to_first_token=?, status=?, http_status=?,
                           test_kind=?, decode_tps=?, throughput_valid=?, chars_per_second=?,
                           capability_score=?, capability_pass=?, format_pass=?, benchmark_version=?,
                           throughput_latency_ms=?, throughput_ttft_ms=?,
                           throughput_sample_count=?, throughput_cv=?,
                           long_tokens_generated=?, long_total_tokens=?, long_latency_ms=?,
                           long_ttft_ms=?, long_decode_tps=?, long_chars_per_second=?,
                           long_response_chars=?, long_files_emitted=?, long_files_complete=?,
                           long_output_complete=?, long_truncated=?, long_finish_reason=?
                           WHERE run_id=? AND model_id=?""",
                        (
                            1 if m.get("success") else 0,
                            error_id,
                            m.get("responseTime"),
                            m.get("tokensGenerated"),
                            m.get("totalTokens"),
                            m.get("timeToFirstToken"),
                            m.get("status"),
                            m.get("httpStatus"),
                            test_kind,
                            m.get("decodeTps"),
                            1 if m.get("throughputValid") else 0,
                            m.get("charsPerSecond"),
                            m.get("capabilityScore"),
                            1 if m.get("capabilityPass") else 0,
                            1 if m.get("formatPass") else 0,
                            m.get("benchmarkVersion"),
                            m.get("throughputResponseTime"),
                            m.get("throughputTtft"),
                            m.get("throughputSampleCount"),
                            m.get("throughputCv"),
                            m.get("longTokensGenerated"),
                            m.get("longTotalTokens"),
                            m.get("longResponseTime"),
                            m.get("longTtft"),
                            m.get("longDecodeTps"),
                            m.get("longCharsPerSecond"),
                            m.get("longResponseChars"),
                            m.get("longFilesEmitted"),
                            m.get("longFilesComplete"),
                            1 if m.get("longOutputComplete") else 0,
                            1 if m.get("longTruncated") else 0,
                            m.get("longFinishReason"),
                            run_id,
                            model_id,
                        ),
                    )

            if "longSuccess" in primary:
                conn.execute(
                    """INSERT INTO model_outputs
                       (model_id, run_id, updated_at, benchmark_version, response_text,
                        finish_reason, completion_tokens, total_tokens, response_time_ms,
                        ttft_ms, decode_tps, chars_per_second, response_chars,
                        files_emitted, files_complete, output_complete, truncated, error_text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(model_id) DO UPDATE SET
                         run_id=excluded.run_id,
                         updated_at=excluded.updated_at,
                         benchmark_version=excluded.benchmark_version,
                         response_text=excluded.response_text,
                         finish_reason=excluded.finish_reason,
                         completion_tokens=excluded.completion_tokens,
                         total_tokens=excluded.total_tokens,
                         response_time_ms=excluded.response_time_ms,
                         ttft_ms=excluded.ttft_ms,
                         decode_tps=excluded.decode_tps,
                         chars_per_second=excluded.chars_per_second,
                         response_chars=excluded.response_chars,
                         files_emitted=excluded.files_emitted,
                         files_complete=excluded.files_complete,
                         output_complete=excluded.output_complete,
                         truncated=excluded.truncated,
                         error_text=excluded.error_text""",
                    (
                        model_id,
                        run_id,
                        timestamp,
                        primary.get("benchmarkVersion"),
                        primary.get("longResponse"),
                        primary.get("longFinishReason"),
                        primary.get("longTokensGenerated"),
                        primary.get("longTotalTokens"),
                        primary.get("longResponseTime"),
                        primary.get("longTtft"),
                        primary.get("longDecodeTps"),
                        primary.get("longCharsPerSecond"),
                        primary.get("longResponseChars"),
                        primary.get("longFilesEmitted"),
                        primary.get("longFilesComplete"),
                        1 if primary.get("longOutputComplete") else 0,
                        1 if primary.get("longTruncated") else 0,
                        sanitize_error(primary.get("longError")),
                    ),
                )

            status = health.get("status") or (
                STATUS_AVAILABLE if health.get("success") else STATUS_ERROR
            )
            checked_at = timestamp
            last_success = timestamp if (health.get("success") or (thr and thr.get("success"))) else None
            prev = conn.execute(
                "SELECT last_success_at FROM models WHERE id=?", (model_id,)
            ).fetchone()
            if not last_success and prev and prev[0]:
                last_success = prev[0]

            metric_src = thr if (thr and thr.get("success")) else health
            conn.execute(
                """UPDATE models SET
                   current_status=?,
                   last_checked_at=?,
                   last_success_at=COALESCE(?, last_success_at),
                   last_http_status=?,
                   last_error=?,
                   last_ttft_ms=?,
                   last_latency_ms=?,
                   last_decode_tps=?,
                   last_test_kind=?,
                   last_throughput_valid=?,
                   last_chars_per_second=?,
                   last_capability_score=COALESCE(?, last_capability_score),
                   last_capability_pass=CASE WHEN ? IS NULL THEN last_capability_pass ELSE ? END,
                   last_benchmark_version=COALESCE(?, last_benchmark_version),
                   last_throughput_at=CASE WHEN ? IS NULL THEN last_throughput_at ELSE ? END,
                   last_capability_at=CASE WHEN ? IS NULL THEN last_capability_at ELSE ? END
                   ,last_throughput_sample_count=?,
                   last_throughput_cv=?
                   WHERE id=?""",
                (
                    status,
                    checked_at,
                    last_success,
                    health.get("httpStatus"),
                    sanitize_error(health.get("error")),
                    health.get("timeToFirstToken") if health.get("success") else None,
                    metric_src.get("responseTime") if metric_src.get("success") else health.get("responseTime"),
                    metric_src.get("decodeTps") if metric_src and metric_src.get("success") else None,
                    metric_src.get("testKind") if metric_src else health.get("testKind"),
                    1 if primary.get("throughputValid") else 0,
                    primary.get("charsPerSecond"),
                    primary.get("capabilityScore"),
                    primary.get("capabilityScore"),
                    1 if primary.get("capabilityPass") else 0,
                    primary.get("benchmarkVersion"),
                    primary.get("throughputResponseTime"),
                    timestamp,
                    primary.get("capabilityScore"),
                    timestamp,
                    primary.get("throughputSampleCount"),
                    primary.get("throughputCv"),
                    model_id,
                ),
            )

        conn.execute(
            f"DELETE FROM runs WHERE id NOT IN "
            f"(SELECT id FROM runs ORDER BY timestamp DESC LIMIT {MAX_RUNS})"
        )
        conn.commit()
        return int(run_id or 0)
    finally:
        conn.close()


def write_run(run: dict[str, Any], db_path: Path = HISTORY_DB) -> None:
    """Backward-compatible wrapper used by older scripts."""
    models = run.get("models") or []
    # normalize keys
    norm = []
    for m in models:
        norm.append(
            {
                "model": m.get("model"),
                "success": m.get("success"),
                "error": m.get("error"),
                "responseTime": m.get("responseTime"),
                "tokensGenerated": m.get("tokensGenerated"),
                "totalTokens": m.get("totalTokens"),
                "timeToFirstToken": m.get("timeToFirstToken"),
                "status": m.get("status")
                or (STATUS_AVAILABLE if m.get("success") else STATUS_ERROR),
                "httpStatus": m.get("httpStatus"),
                "testKind": m.get("testKind") or "legacy",
                "decodeTps": m.get("decodeTps"),
            }
        )
    write_rolling_batch(
        timestamp=run.get("timestamp") or utc_now(),
        prompt=run.get("prompt") or "",
        models=norm,
        batch_meta={"kind": "legacy", "batch_size": len({m["model"] for m in norm})},
        db_path=db_path,
    )


def export_fleet_snapshot(db_path: Path = HISTORY_DB) -> dict[str, Any]:
    """JSON-serializable fleet snapshot for debugging / optional static API."""
    conn = sqlite3.connect(str(db_path))
    try:
        init_schema(conn)
        rows = conn.execute(
            """SELECT name, current_status, last_checked_at, last_success_at,
                      last_http_status, last_error, last_ttft_ms, last_latency_ms,
                      last_decode_tps, intelligence_score, last_throughput_valid,
                      last_chars_per_second, last_benchmark_version,
                      last_throughput_at,
                      last_throughput_sample_count, last_throughput_cv
               FROM models ORDER BY name"""
        ).fetchall()
        models = []
        counts: dict[str, int] = {}
        now = datetime.now(timezone.utc)
        for r in rows:
            display = compute_display_status(r[1], r[2], now=now)
            counts[display] = counts.get(display, 0) + 1
            models.append(
                {
                    "name": r[0],
                    "current_status": r[1],
                    "display_status": display,
                    "last_checked_at": r[2],
                    "last_success_at": r[3],
                    "last_http_status": r[4],
                    "last_error": r[5],
                    "last_ttft_ms": r[6],
                    "last_latency_ms": r[7],
                    "last_decode_tps": r[8],
                    "intelligence_score": r[9],
                    "last_throughput_valid": bool(r[10]) if r[10] is not None else None,
                    "last_chars_per_second": r[11],
                    "last_benchmark_version": r[12],
                    "last_throughput_at": r[13],
                    "last_throughput_sample_count": r[14],
                    "last_throughput_cv": r[15],
                }
            )
        return {
            "generated_at": utc_now(),
            "stale_after_minutes": STALE_AFTER_MINUTES,
            "counts": counts,
            "models": models,
        }
    finally:
        conn.close()

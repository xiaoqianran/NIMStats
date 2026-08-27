#!/usr/bin/env python3
"""Build compact browser-facing JSON from history.db for GitHub Pages."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

STALE_AFTER_MINUTES_DEFAULT = 180
RECENT_RUN_LIMIT = 100
HEALTH_RUN_LIMIT = 30


def avg(values):
    return sum(values) / len(values) if values else None


def error_category(error: str | None) -> str:
    if not error:
        return "Unknown"
    if "timed out" in error:
        return "Timeout"
    if "JSON" in error:
        return "JSON Error"
    if "404" in error:
        return "Not Found (404)"
    if "410" in error:
        return "Gone (410)"
    if "closed connection" in error:
        return "Connection Closed"
    return "Other Error"


def candidate_rank(rec: dict) -> int:
    kind = rec.get("testKind") or ""
    suite = 3 if kind.startswith("suite-v") else 2 if kind == "throughput" else 1 if kind == "health" else 0
    return (4 if rec.get("success") else 0) + suite


def compact_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def build_site_data(db_path: Path, output: Path) -> tuple[Path, Path]:
    uri = f"file:{db_path.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row

        stale_after = STALE_AFTER_MINUTES_DEFAULT
        try:
            row = conn.execute("SELECT value FROM scheduler_state WHERE key='stale_after_minutes'").fetchone()
            if row and int(row[0]) > 0:
                stale_after = int(row[0])
        except (sqlite3.Error, ValueError, TypeError):
            pass

        model_meta: dict[str, dict] = {}
        for row in conn.execute(
            """SELECT id,name,intelligence_score,current_status,last_checked_at,last_success_at,
                      last_http_status,last_error,last_ttft_ms,last_latency_ms,last_decode_tps,
                      last_throughput_valid,last_chars_per_second,last_benchmark_version,
                      last_throughput_sample_count,last_throughput_cv
               FROM models ORDER BY name"""
        ):
            name = row["name"]
            model_meta[name] = {
                "intelligence": row["intelligence_score"] if row["intelligence_score"] is not None else 50.0,
                "currentStatus": row["current_status"] or "UNKNOWN",
                "lastCheckedAt": row["last_checked_at"],
                "lastSuccessAt": row["last_success_at"],
                "lastHttpStatus": row["last_http_status"],
                "lastError": row["last_error"],
                "lastTtftMs": row["last_ttft_ms"],
                "lastLatencyMs": row["last_latency_ms"],
                "lastDecodeTps": row["last_decode_tps"],
                "lastThroughputValid": row["last_throughput_valid"] == 1,
                "lastCharsPerSecond": row["last_chars_per_second"],
                "benchmarkVersion": row["last_benchmark_version"],
                "throughputSampleCount": row["last_throughput_sample_count"] or 0,
                "throughputCv": row["last_throughput_cv"],
            }

        try:
            for row in conn.execute(
                """SELECT m.name,o.updated_at,o.benchmark_version,o.response_text,o.finish_reason,
                          o.completion_tokens,o.total_tokens,o.response_time_ms,o.ttft_ms,o.decode_tps,
                          o.chars_per_second,o.response_chars,o.files_emitted,o.files_complete,
                          o.output_complete,o.truncated,o.error_text
                   FROM model_outputs o JOIN models m ON o.model_id=m.id"""
            ):
                model_meta.setdefault(row["name"], {})["longOutput"] = {
                    "updatedAt": row["updated_at"], "benchmarkVersion": row["benchmark_version"],
                    "responseText": row["response_text"] or "", "finishReason": row["finish_reason"],
                    "completionTokens": row["completion_tokens"], "totalTokens": row["total_tokens"],
                    "responseTimeMs": row["response_time_ms"], "ttftMs": row["ttft_ms"],
                    "decodeTps": row["decode_tps"], "charsPerSecond": row["chars_per_second"],
                    "responseChars": row["response_chars"] or 0, "filesEmitted": row["files_emitted"] or 0,
                    "filesComplete": row["files_complete"] or 0, "outputComplete": row["output_complete"] == 1,
                    "truncated": row["truncated"] == 1, "error": row["error_text"],
                }
        except sqlite3.Error:
            pass

        run_rows = list(conn.execute(
            """SELECT r.id,r.timestamp,m.name AS fastest_model,r.fastest_time,r.batch_size,r.kind
               FROM runs r LEFT JOIN models m ON r.fastest_model_id=m.id ORDER BY r.id ASC"""
        ))
        runs = {
            row["id"]: {
                "_dbId": row["id"], "timestamp": row["timestamp"], "models": [],
                "summary": {"fastestModel": row["fastest_model"] or "N/A", "fastestTime": row["fastest_time"] or 0,
                            "batchSize": row["batch_size"], "kind": row["kind"] or "legacy"},
            }
            for row in run_rows
        }

        collapsed: dict[tuple[int, str], dict] = {}
        query = """SELECT mr.run_id,m.name,mr.success,e.text AS error,mr.response_time,mr.tokens_generated,
                          mr.total_tokens,mr.time_to_first_token,mr.status,mr.http_status,mr.test_kind,mr.decode_tps,
                          mr.throughput_valid,mr.chars_per_second,mr.benchmark_version,mr.throughput_latency_ms,
                          mr.throughput_ttft_ms,mr.throughput_sample_count,mr.throughput_cv
                   FROM model_results mr JOIN models m ON mr.model_id=m.id
                   LEFT JOIN errors e ON mr.error_id=e.id ORDER BY mr.run_id ASC"""
        for row in conn.execute(query):
            rec = {
                "model": row["name"], "success": row["success"] == 1, "error": row["error"],
                "responseTime": row["response_time"], "tokensGenerated": row["tokens_generated"],
                "totalTokens": row["total_tokens"], "timeToFirstToken": row["time_to_first_token"],
                "status": row["status"], "httpStatus": row["http_status"], "testKind": row["test_kind"] or "legacy",
                "decodeTps": row["decode_tps"] if row["throughput_valid"] == 1 else None,
                "throughputValid": row["throughput_valid"] == 1, "charsPerSecond": row["chars_per_second"],
                "benchmarkVersion": row["benchmark_version"], "throughputLatency": row["throughput_latency_ms"],
                "throughputTtft": row["throughput_ttft_ms"], "throughputSampleCount": row["throughput_sample_count"] or 0,
                "throughputCv": row["throughput_cv"],
            }
            key = (row["run_id"], row["name"])
            prev = collapsed.get(key)
            if prev is None or candidate_rank(rec) >= candidate_rank(prev):
                collapsed[key] = rec

        per_model: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for (run_id, model), rec in collapsed.items():
            if run_id in runs:
                runs[run_id]["models"].append(rec)
            per_model[model].append((run_id, rec))

        for run in runs.values():
            run["summary"]["successCount"] = sum(1 for r in run["models"] if r["success"])
            run["summary"]["totalModels"] = len(run["models"])

        model_names = sorted(set(model_meta) | set(per_model))
        stats: dict[str, dict] = {}
        for model in model_names:
            entries = sorted(per_model.get(model, []), key=lambda x: x[0])
            records = [rec for _, rec in entries]
            successes = [r for r in records if r["success"]]
            times = [r["responseTime"] for r in successes if r["responseTime"] and r["responseTime"] > 0]
            ttfts = [r["timeToFirstToken"] for r in successes if r["timeToFirstToken"] and r["timeToFirstToken"] > 0]
            tps = [r["decodeTps"] for r in successes if r["throughputValid"] and r["decodeTps"] and r["decodeTps"] > 0]
            meta = model_meta.get(model, {})
            response_series = [r["responseTime"] if r["success"] and r["responseTime"] and r["responseTime"] > 0 else None for r in records]
            errors: dict[str, int] = defaultdict(int)
            for r in records:
                if not r["success"] and r["error"]:
                    errors[error_category(r["error"])] += 1
            half = len(response_series) // 2
            first = [v for v in response_series[:half] if v is not None]
            second = [v for v in response_series[half:] if v is not None]
            trend = "flat"
            if first and second:
                diff = avg(second) - avg(first)
                trend = "up" if diff < -500 else "down" if diff > 500 else "flat"
            last_seen = meta.get("lastSuccessAt")
            if not last_seen:
                for run_id, rec in reversed(entries):
                    if rec["success"]:
                        last_seen = runs[run_id]["timestamp"]
                        break
            long_output = meta.get("longOutput")
            stats[model] = {
                "totalRuns": len(records), "successCount": len(successes),
                "uptime": len(successes) / len(records) if records else 0,
                "responseTimes": response_series[-30:], "avgTime": avg(times), "bestTime": min(times) if times else None,
                "avgTtft": avg(ttfts) if ttfts else meta.get("lastTtftMs"),
                "avgTps": avg(tps) if tps else (meta.get("lastDecodeTps") if meta.get("lastThroughputValid") else None),
                "longOutput": long_output, "longCompletionTokens": long_output.get("completionTokens") if long_output else None,
                "charsPerSecond": meta.get("lastCharsPerSecond"), "benchmarkVersion": meta.get("benchmarkVersion"),
                "throughputSampleCount": meta.get("throughputSampleCount") or 0, "throughputCv": meta.get("throughputCv"),
                "wins": 0, "errors": dict(errors), "lastSeen": last_seen, "lastCheckedAt": meta.get("lastCheckedAt"),
                "currentStatus": meta.get("currentStatus") or "UNKNOWN", "lastHttpStatus": meta.get("lastHttpStatus"),
                "lastError": meta.get("lastError"), "intelligence": meta.get("intelligence"), "trend": trend,
            }

        for run in runs.values():
            fm = run["summary"]["fastestModel"]
            if fm in stats:
                stats[fm]["wins"] += 1

        valid_times = [s["avgTime"] for s in stats.values() if s["avgTime"] is not None]
        valid_tps = [s["avgTps"] for s in stats.values() if s["avgTps"] is not None]
        min_time, max_time = (min(valid_times), max(valid_times)) if valid_times else (0, 1)
        min_tps, max_tps = (min(valid_tps), max(valid_tps)) if valid_tps else (0, 1)
        for s in stats.values():
            speed_score = (1 - (s["avgTime"] - min_time) / max(max_time - min_time, 1)) * 100 if s["avgTime"] is not None else 0
            tps_score = ((s["avgTps"] - min_tps) / max(max_tps - min_tps, 1)) * 100 if s["avgTps"] is not None else 0
            intel = s["intelligence"] if s["intelligence"] is not None else 50
            s["speedScore"] = speed_score
            s["tpsScore"] = tps_score
            s["score"] = round(s["uptime"] * 35 + speed_score * .2 + tps_score * .2 + (intel / 100) * 25)

        ordered_runs = [runs[row["id"]] for row in run_rows]
        health_runs = [
            {"_dbId": r["_dbId"], "timestamp": r["timestamp"], "summary": r["summary"]}
            for r in ordered_runs[-HEALTH_RUN_LIMIT:]
        ]
        recent_runs = []
        for r in ordered_runs[-RECENT_RUN_LIMIT:]:
            recent_runs.append({
                "_dbId": r["_dbId"], "timestamp": r["timestamp"], "summary": r["summary"],
                "models": [
                    {k: rec.get(k) for k in ("model", "success", "status", "responseTime", "timeToFirstToken", "decodeTps", "testKind")}
                    for rec in r["models"]
                ],
            })

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    site_payload = {
        "generatedAt": generated_at, "staleAfterMinutes": stale_after, "modelNames": model_names,
        "modelStats": stats, "healthRuns": health_runs, "totalRunCount": len(run_rows),
    }
    runs_payload = {"generatedAt": generated_at, "totalRunCount": len(run_rows), "runs": recent_runs}
    site_path = output / "data" / "site.json"
    runs_path = output / "data" / "runs.json"
    compact_json(site_path, site_payload)
    compact_json(runs_path, runs_payload)
    return site_path, runs_path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    out = root / "_site"
    site, runs = build_site_data(root / "history.db", out)
    print(site)
    print(runs)

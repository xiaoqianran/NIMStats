// ─── SQLite Data Loader ───────────────────────────────────────────────────────
// Rolling monitor: availability comes from each model's latest check
// (models.current_status / last_checked_at), NOT the global last run.

const STALE_AFTER_MINUTES_DEFAULT = 180;

function parseUtc(ts) {
  if (!ts) return null;
  // history stores Zulu timestamps
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
  return Number.isNaN(d.getTime()) ? null : d;
}

function displayStatus(currentStatus, lastCheckedAt, staleAfterMinutes) {
  const status = currentStatus || 'UNKNOWN';
  const checked = parseUtc(lastCheckedAt);
  if (!checked) {
    return status === 'AVAILABLE' ? 'STALE' : status;
  }
  const ageMin = (Date.now() - checked.getTime()) / 60000;
  if (ageMin > staleAfterMinutes && status === 'AVAILABLE') return 'STALE';
  if (ageMin > staleAfterMinutes && status !== 'STALE') {
    // non-available statuses still shown, but mark stale-available only;
    // for long-unseen models that were GONE, keep GONE.
    return status;
  }
  return status;
}

function decodeTps(tokensGenerated, responseTime, ttft) {
  if (!tokensGenerated || !responseTime || responseTime <= 0) return null;
  let genMs = responseTime - (ttft != null && ttft > 0 ? ttft : 0);
  if (genMs <= 0) genMs = responseTime;
  return tokensGenerated / (genMs / 1000);
}

function loadFromDb(db) {
  let staleAfter = STALE_AFTER_MINUTES_DEFAULT;
  try {
    const st = db.exec("SELECT value FROM scheduler_state WHERE key = 'stale_after_minutes'");
    if (st.length && st[0].values.length) {
      const n = parseInt(st[0].values[0][0], 10);
      if (!Number.isNaN(n) && n > 0) staleAfter = n;
    }
  } catch (_) { /* scheduler_state may not exist on very old DBs */ }

  // Per-model current state (source of truth for "is it up right now")
  const modelMeta = {};
  try {
    const mq = db.exec(
      `SELECT name, intelligence_score, current_status, last_checked_at, last_success_at,
              last_http_status, last_error, last_ttft_ms, last_latency_ms, last_decode_tps,
              last_throughput_valid, last_chars_per_second, last_capability_score,
              last_capability_pass, last_benchmark_version,
              last_throughput_sample_count, last_throughput_cv
       FROM models ORDER BY name`
    );
    if (mq.length && mq[0].values.length) {
      for (const row of mq[0].values) {
        const [name, intel, cur, checked, successAt, httpSt, err, ttft, lat, tps,
          throughputValid, charsPerSecond, capabilityScore, capabilityPass, benchmarkVersion,
          throughputSampleCount, throughputCv] = row;
        modelMeta[name] = {
          intelligence: intel != null ? intel : 50.0,
          currentStatus: cur || 'UNKNOWN',
          displayStatus: displayStatus(cur, checked, staleAfter),
          lastCheckedAt: checked,
          lastSuccessAt: successAt,
          lastHttpStatus: httpSt,
          lastError: err,
          lastTtftMs: ttft,
          lastLatencyMs: lat,
          lastDecodeTps: tps,
          lastThroughputValid: throughputValid === 1,
          lastCharsPerSecond: charsPerSecond,
          capabilityScore,
          capabilityPass: capabilityPass === 1,
          benchmarkVersion,
          throughputSampleCount,
          throughputCv,
        };
      }
    }
  } catch (err) {
    console.warn('models meta load failed', err);
  }

  const runsQ = db.exec(
    `SELECT r.id, r.timestamp, p.text, m.name, r.fastest_time, r.batch_size, r.kind
     FROM runs r
     JOIN prompts p ON r.prompt_id = p.id
     LEFT JOIN models m ON r.fastest_model_id = m.id
     ORDER BY r.timestamp DESC`
  );
  if (!runsQ.length || !runsQ[0].values.length) {
    return { runs: [], modelIntel: {}, modelMeta, staleAfterMinutes: staleAfter };
  }

  const runs = runsQ[0].values.map(([id, timestamp, prompt, fm, ft, batchSize, kind]) => ({
    _dbId: id,
    timestamp,
    prompt,
    models: [],
    summary: {
      fastestModel: fm || 'N/A',
      fastestTime: ft || 0,
      batchSize: batchSize || null,
      kind: kind || 'legacy',
    },
  }));

  const runById = new Map(runs.map((r, i) => [r._dbId, i]));

  // Prefer throughput rows for chart metrics; fall back to health/legacy
  let resQ;
  try {
    resQ = db.exec(
      `SELECT mr.run_id, m.name, mr.success, e.text, mr.response_time, mr.tokens_generated,
              mr.total_tokens, mr.time_to_first_token, mr.status, mr.http_status, mr.test_kind, mr.decode_tps,
              mr.throughput_valid, mr.chars_per_second, mr.capability_score,
              mr.capability_pass, mr.format_pass, mr.benchmark_version,
              mr.throughput_latency_ms, mr.throughput_ttft_ms,
              mr.throughput_sample_count, mr.throughput_cv
       FROM model_results mr
       JOIN models m ON mr.model_id = m.id
       LEFT JOIN errors e ON mr.error_id = e.id
       ORDER BY mr.run_id ASC`
    );
  } catch (_) {
    resQ = db.exec(
      `SELECT mr.run_id, m.name, mr.success, e.text, mr.response_time, mr.tokens_generated,
              mr.total_tokens, mr.time_to_first_token
       FROM model_results mr
       JOIN models m ON mr.model_id = m.id
       LEFT JOIN errors e ON mr.error_id = e.id
       ORDER BY mr.run_id ASC`
    );
  }

  // Collapse multiple test_kind rows per (run, model) into one chart point
  const bucket = new Map(); // key runId|model -> best row
  if (resQ.length && resQ[0].values.length) {
    for (const row of resQ[0].values) {
      const run_id = row[0];
      const model = row[1];
      const success = row[2];
      const error = row[3];
      const rt = row[4];
      const tg = row[5];
      const tt = row[6];
      const ttft = row[7];
      const status = row[8] != null ? row[8] : null;
      const httpStatus = row[9] != null ? row[9] : null;
      const testKind = row[10] != null ? row[10] : 'legacy';
      const dps = row[11] != null ? row[11] : null;
      const throughputValid = row[12] === 1;
      const charsPerSecond = row[13] != null ? row[13] : null;
      const capabilityScore = row[14] != null ? row[14] : null;
      const capabilityPass = row[15] === 1;
      const formatPass = row[16] === 1;
      const benchmarkVersion = row[17] != null ? row[17] : null;
      const throughputLatency = row[18] != null ? row[18] : null;
      const throughputTtft = row[19] != null ? row[19] : null;
      const throughputSampleCount = row[20] != null ? row[20] : 0;
      const throughputCv = row[21] != null ? row[21] : null;
      const key = `${run_id}||${model}`;
      const cand = {
        model,
        success: success === 1,
        error: error || null,
        responseTime: rt,
        tokensGenerated: tg,
        totalTokens: tt,
        timeToFirstToken: ttft,
        status,
        httpStatus,
        testKind,
        decodeTps: throughputValid && dps != null ? dps : null,
        throughputValid,
        charsPerSecond,
        capabilityScore,
        capabilityPass,
        formatPass,
        benchmarkVersion,
        throughputLatency,
        throughputTtft,
        throughputSampleCount,
        throughputCv,
      };
      const prev = bucket.get(key);
      if (!prev) {
        bucket.set(key, cand);
        continue;
      }
      // Prefer successful throughput > successful health > any
      const rank = (r) =>
        (r.success ? 4 : 0) +
        (r.testKind === 'suite-v3' ? 3 : r.testKind === 'throughput' ? 2 : r.testKind === 'health' ? 1 : 0);
      if (rank(cand) >= rank(prev)) bucket.set(key, cand);
    }
  }

  for (const [key, rec] of bucket.entries()) {
    const run_id = Number(key.split('||')[0]);
    const idx = runById.get(run_id);
    if (idx !== undefined) runs[idx].models.push(rec);
  }

  // Intelligence map
  const modelIntel = {};
  for (const [name, meta] of Object.entries(modelMeta)) {
    modelIntel[name] = meta.intelligence != null ? meta.intelligence : 50.0;
  }

  for (const run of runs) {
    run.summary.successCount = run.models.filter((m) => m.success).length;
    run.summary.totalModels = run.models.length;
  }

  return { runs, modelIntel, modelMeta, staleAfterMinutes: staleAfter };
}

// ─── Data Processing ──────────────────────────────────────────────────────────
function buildModelStats(runs, modelNames, modelIntel, modelMeta) {
  const modelStats = {};

  for (const model of modelNames) {
    const results = runs.map((run) => run.models.find((m) => m.model === model) || null);
    const successes = results.filter((r) => r && r.success);
    const testedResults = results.filter((r) => r !== null);
    const times = successes.map((r) => r.responseTime).filter((t) => t > 0);
    const ttftArr = successes.map((r) => r.timeToFirstToken).filter((t) => t != null && t > 0);
    const tpsArr = successes
      .filter((r) => r.throughputValid)
      .map((r) => r.decodeTps)
      .filter((t) => t != null && t > 0);
    const capabilityArr = results
      .map((r) => r?.capabilityScore)
      .filter((score) => score != null);

    const meta = modelMeta[model] || {};
    modelStats[model] = {
      results,
      totalRuns: testedResults.length,
      successCount: successes.length,
      // Historical reliability over windows where this model was tested
      uptime: testedResults.length ? successes.length / testedResults.length : 0,
      responseTimes: results.map((r) => (r && r.success && r.responseTime > 0 ? r.responseTime : null)),
      throughputs: results.map((r) => {
        if (!(r && r.success && r.throughputValid)) return null;
        return r.decodeTps != null ? r.decodeTps : null;
      }),
      avgTime: times.length ? avg(times) : null,
      bestTime: times.length ? Math.min(...times) : null,
      avgTtft: ttftArr.length ? avg(ttftArr) : (meta.lastTtftMs || null),
      avgTps: tpsArr.length ? avg(tpsArr) : (meta.lastThroughputValid ? meta.lastDecodeTps : null),
      capabilityScore: capabilityArr.length ? avg(capabilityArr) : (meta.capabilityScore ?? null),
      capabilityPass: meta.capabilityPass || false,
      charsPerSecond: meta.lastCharsPerSecond || null,
      benchmarkVersion: meta.benchmarkVersion || null,
      throughputSampleCount: meta.throughputSampleCount || 0,
      throughputCv: meta.throughputCv ?? null,
      wins: 0,
      errors: {},
      lastSeen: meta.lastSuccessAt || null,
      lastCheckedAt: meta.lastCheckedAt || null,
      currentStatus: meta.currentStatus || 'UNKNOWN',
      displayStatus: meta.displayStatus || 'UNKNOWN',
      lastHttpStatus: meta.lastHttpStatus || null,
      lastError: meta.lastError || null,
      intelligence: modelIntel[model] != null ? modelIntel[model] : null,
    };

    // lastSeen fallback from series
    if (!modelStats[model].lastSeen) {
      for (let i = results.length - 1; i >= 0; i--) {
        if (results[i] && results[i].success) {
          modelStats[model].lastSeen = runs[i]?.timestamp || null;
          break;
        }
      }
    }

    results
      .filter((r) => r && !r.success && r.error)
      .forEach((r) => {
        const t = categorizeError(r.error);
        modelStats[model].errors[t] = (modelStats[model].errors[t] || 0) + 1;
      });
  }

  runs.forEach((run) => {
    const fm = run.summary?.fastestModel;
    if (fm && modelStats[fm]) modelStats[fm].wins++;
  });

  const validTimes = modelNames.filter((m) => modelStats[m].avgTime != null).map((m) => modelStats[m].avgTime);
  const validTps = modelNames.filter((m) => modelStats[m].avgTps != null).map((m) => modelStats[m].avgTps);
  const maxTime = validTimes.length ? Math.max(...validTimes) : 1;
  const minTime = validTimes.length ? Math.min(...validTimes) : 0;
  const maxTps = validTps.length ? Math.max(...validTps) : 1;
  const minTps = validTps.length ? Math.min(...validTps) : 0;

  for (const model of modelNames) {
    const s = modelStats[model];
    const speedScore =
      s.avgTime != null ? (1 - (s.avgTime - minTime) / Math.max(maxTime - minTime, 1)) * 100 : 0;
    const tpsScore =
      s.avgTps != null ? ((s.avgTps - minTps) / Math.max(maxTps - minTps, 1)) * 100 : 0;
    s.speedScore = speedScore;
    s.tpsScore = tpsScore;
    const intel = s.intelligence != null ? s.intelligence : 50;
    const suite = s.capabilityScore != null ? s.capabilityScore : 0;
    // Local verifiable suite is kept distinct from the external intelligence index.
    s.score = Math.round(s.uptime * 25 + speedScore * 0.15 + tpsScore * 0.15 + suite * 0.2 + (intel / 100) * 25);

    const half = Math.floor(s.responseTimes.length / 2);
    const firstHalf = s.responseTimes.slice(0, half).filter((v) => v != null);
    const secondHalf = s.responseTimes.slice(half).filter((v) => v != null);
    if (firstHalf.length && secondHalf.length) {
      const diff = avg(secondHalf) - avg(firstHalf);
      s.trend = diff < -500 ? 'up' : diff > 500 ? 'down' : 'flat';
    } else {
      s.trend = 'flat';
    }
  }

  return modelStats;
}

function processData(data) {
  const runs = [...data.runs].reverse(); // chronological
  const modelMeta = data.modelMeta || {};
  // Union of models table + any seen in runs
  const fromRuns = runs.flatMap((r) => r.models.map((m) => m.model));
  const modelNames = [...new Set([...Object.keys(modelMeta), ...fromRuns])].sort();
  const modelIntel = data.modelIntel || {};
  const modelStats = buildModelStats(runs, modelNames, modelIntel, modelMeta);
  return { runs, modelNames, modelStats, modelMeta, staleAfterMinutes: data.staleAfterMinutes };
}

function recomputeStats() {
  const limit = state.limit;
  let runsSubset = [...state.rawRuns];
  if (limit !== 'all') {
    const n = parseInt(limit, 10);
    runsSubset = state.rawRuns.slice(-n);
  }
  state.runs = runsSubset;
  const modelNames = state.modelNames;
  const modelMeta = state.modelMeta || {};
  state.modelStats = buildModelStats(state.runs, modelNames, state.modelIntel || {}, modelMeta);
}

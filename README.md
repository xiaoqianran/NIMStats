<div align="center">

[![NIMStats Banner](https://capsule-render.vercel.app/api?type=waving&color=76b900&height=220&section=header&text=NIMStats&fontSize=90&fontColor=ffffff&animation=fadeIn&fontAlignY=38&desc=Real-Time%20NVIDIA%20NIM%20Benchmark%20Dashboard&descSize=22&descAlignY=60&descAlign=50)](https://nimstats.maurodruwel.be/)

[![CI](https://github.com/MauroDruwel/NIMStats/actions/workflows/benchmark.yml/badge.svg)](https://github.com/MauroDruwel/NIMStats/actions)
[![Live Dashboard](https://img.shields.io/badge/🌐%20live-nimstats.maurodruwel.be-76b900?style=flat-square)](https://nimstats.maurodruwel.be/)
[![Models](https://img.shields.io/badge/models-dynamic%20catalog-blue?style=flat-square)](https://build.nvidia.com/models)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow?style=flat-square)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](https://github.com/MauroDruwel/NIMStats/pulls)
[![Stars](https://img.shields.io/github/stars/MauroDruwel/NIMStats?style=flat-square&color=gold)](https://github.com/MauroDruwel/NIMStats/stargazers)

<br/>

> **Community-driven benchmarking of NVIDIA NIM hosted models — dynamic `/v1/models` catalog, live availability probe, then bench only callable chat models.**

<br/>

**[🚀 View Live Dashboard](https://nimstats.maurodruwel.be/) · [📖 Docs](#-quick-start) · [🤝 Contribute](#-contributing) · [💬 Discussions](https://github.com/MauroDruwel/NIMStats/discussions)**

</div>

---

## ✨ What is NIMStats?

NIMStats discovers models from NVIDIA `GET /v1/models`, filters to chat candidates (drops embed/rerank/image/…), **live-probes** which are actually callable for your API key (many catalog entries are retired/`404 Function not found for account`), then benchmarks only **AVAILABLE** models. Results publish to a static dashboard via GitHub Actions — no servers required.

<div align="center">

| 🏎️ Hourly Benchmarks | 📊 Interactive Charts | 🔁 Zero Infrastructure | 🌍 Fully Open-Source |
|:---:|:---:|:---:|:---:|
| Automatic via GitHub Actions | Response time, throughput & trends | Static site + free CI/CD | Fork and self-host in minutes |

</div>

---


## 🔄 Rolling monitor (current strategy)

NIMStats is a **continuous rolling fleet monitor**, not an hourly full-fleet snapshot.

| Piece | Behavior |
|-------|----------|
| Schedule | GitHub Actions every **10 minutes** (`*/10 * * * *`) |
| Batch | Each run tests the next **9** chat models (cursor wraps) |
| Catalog | `GET /v1/models` → filter embed/rerank/image/… → stable sorted fleet |
| Requests | **No separate probe.** Health call sets availability; Throughput call measures TPS |
| Health | `Reply with exactly: OK`, `temperature=0`, `max_tokens=8`, stream → TTFT |
| Throughput | Fixed 1..40 number list, `temperature=0`, stream → e2e + decode TPS |
| Rate limit | Client limiter **≤ 40 req/min** (`NIM_MAX_REQUESTS_PER_MINUTE`) |
| Status | Per-model `current_status`, `last_checked_at`, `last_success_at` in `history.db` |
| STALE | If not re-checked within `STALE_AFTER_MINUTES` (default 180), UI shows **STALE** |
| Pages | Regenerates after **each batch** (not after full fleet cycle) |
| Intelligence | Artificial Analysis only — not derived from our prompts |

```bash
# Local one batch
export NIM_API_KEY=...
python3 -u scripts/rolling_bench.py

# Env knobs
BATCH_SIZE=9
NIM_MAX_REQUESTS_PER_MINUTE=40
STALE_AFTER_MINUTES=180
```

## ⚡ Quick Start

> Get your own benchmarking dashboard running in under 5 minutes.

### 1. Fork & Clone

```bash
git clone https://github.com/MauroDruwel/NIMStats.git
cd NIMStats
```

### 2. Get a Free API Key

Visit **[build.nvidia.com](https://build.nvidia.com)** → Create a free account → Copy your API key.

### 3. Add the Secret

In your forked repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|------|-------|
| `NIM_API_KEY` | Your NVIDIA NIM API key |

### 4. Deploy the Dashboard

#### GitHub Pages（推荐：用 Actions 部署）

1. 打开仓库 **Settings → Pages**
2. **Build and deployment → Source** 选 **GitHub Actions**（不要选 “Deploy from a branch”）
3. 推送 `main` 或手动跑 **Actions → Deploy GitHub Pages → Run workflow**
4. 几分钟后访问：`https://<你的用户名>.github.io/NIMStats/`

仓库已自带 [`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml)：
- `push` 到 `main` 且改动了看板文件 / `history.db` 时自动发布
- 也可 `workflow_dispatch` 手动发布
- 只上传 `index.html`、`css/`、`js/`、`history.db`、`top/` 等静态资源，**不会**把 `scripts/` 或 API key 打进站点

| 其他平台 | 步骤 |
|----------|------|
| **Cloudflare Pages** | Connect repo → auto-deploys on every push to `main` |
| **Netlify / Vercel** | Connect repo as static site (root) |

### 5. Run Your First Benchmark

**Actions → Benchmark NVIDIA NIM Models → Run workflow**

That's it — your dashboard auto-refreshes every hour. ✨

---

## 📊 Dashboard Features

<div align="center">

| Tab | What you get |
|-----|-------------|
| **📊 Overview** | 5 animated KPI cards · success trend charts · top-10 speed & throughput bars · model reliability pills |
| **🏆 Leaderboard** | Composite score rankings · sortable columns · SVG sparklines · trend indicators (↑↓→) · provider chips |
| **🔬 Explorer** | Per-model deep dive · response time history chart · error breakdown donut · availability heatmap |
| **⏱ Timeline** | Filterable run history (All / 24h / 48h / 7d) · expandable run cards with full per-model detail |
| **⚔️ Compare** | Head-to-head overlay chart · win-rate stats · side-by-side metric comparison |
| **🔗 Public APIs** | Multiple category endpoints: `/top` (balanced), `/top/speed` (speed & tps), and `/top/intelligence` (capabilities) in both JSON and raw `.txt` formats. Perfect for integration with local scripts, scripts, or apps |

</div>

---

## 🔌 Developer APIs

NIMStats exposes lightweight, static API endpoints for querying the #1 model in different performance categories. Every time the hourly benchmark completes, these endpoints are updated.

### Available Endpoints

| Category | Endpoint (JSON) | Endpoint (Plain Text) | Scoring Balance |
| :--- | :--- | :--- | :--- |
| **⚖️ Balanced (Overall)** | [`/top`](https://nimstats.maurodruwel.be/top) | [`/top/model`](https://nimstats.maurodruwel.be/top/model) | **30%** Uptime + **30%** Intelligence + **20%** Avg Time + **20%** Throughput |
| **🏎️ Speed & Throughput** | [`/top/speed`](https://nimstats.maurodruwel.be/top/speed) | [`/top/speed/model`](https://nimstats.maurodruwel.be/top/speed/model) | **50%** Avg Response Time + **50%** Throughput (TPS) |
| **🧠 Model Intelligence** | [`/top/intelligence`](https://nimstats.maurodruwel.be/top/intelligence) | [`/top/intelligence/model`](https://nimstats.maurodruwel.be/top/intelligence/model) | **70%** Artificial Analysis Score + **30%** Uptime |

### JSON Response Schema

```json
{
  "best_model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
  "provider": "nvidia",
  "score": 71,
  "intelligence": 14.9,
  "uptime": 90.6,
  "avg_response_time_ms": 4736.7,
  "best_response_time_ms": 432.0,
  "avg_time_to_first_token_ms": 312.5,
  "avg_throughput_tps": 163.3,
  "total_runs": 720,
  "success_count": 652,
  "wins": 364,
  "last_seen": "2026-07-07T10:00:08Z",
  "generated_at": "2026-07-07T10:08:49Z"
}
```

---

## 🤖 Benchmarked Models

<details>
<summary><b>22 models across 11 providers — click to expand</b></summary>

<br/>

| Provider | Model | Highlight |
|----------|-------|-----------|
| **DeepSeek** | `deepseek-ai/deepseek-v4-flash` | Fast MoE, optimized for speed |
| **DeepSeek** | `deepseek-ai/deepseek-v4-pro` | Professional-grade reasoning |
| **Z-AI** | `z-ai/glm-5.2` | Superior code understanding |
| **MiniMax** | `minimaxai/minimax-m2.7` | Efficient inference model |
| **MiniMax** | `minimaxai/minimax-m3` | Latest MiniMax generation |
| **NVIDIA** | `nvidia/nemotron-3-super-120b-a12b` | NVIDIA's 120B flagship |
| **NVIDIA** | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | Compact omni reasoning model |
| **NVIDIA** | `nvidia/llama-3.3-nemotron-super-49b-v1.5` | Nemotron Super 49B v1.5 |
| **Moonshot** | `moonshotai/kimi-k2.6` | Context-optimized model |
| **OpenAI** | `openai/gpt-oss-120b` | Open-source 120B |
| **Google** | `google/gemma-4-31b-it` | Lightweight edge inference |
| **Qwen** | `qwen/qwen3.5-397b-a17b` | Flagship Qwen (397B) |
| **Qwen** | `qwen/qwen3.5-122b-a10b` | Mid-range Qwen 3.5 MoE |
| **Qwen** | `qwen/qwen3-next-80b-a3b-instruct` | Next-gen Qwen (80B MoE) |
| **Mistral** | `mistralai/mistral-large-3-675b-instruct-2512` | Largest Mistral (675B) |
| **Mistral** | `mistralai/mistral-medium-3.5-128b` | Efficient medium-scale Mistral |
| **Mistral** | `mistralai/mistral-small-4-119b-2603` | Mistral Small 4 (119B) |
| **Meta** | `meta/llama-3.3-70b-instruct` | Llama 3.3 70B |
| **Meta** | `meta/llama-4-maverick-17b-128e-instruct` | Llama 4 Maverick (128 experts) |
| **Meta** | `meta/llama-3.2-90b-vision-instruct` | Multimodal 90B vision model |
| **StepFun** | `stepfun-ai/step-3.5-flash` | Ultra-fast flash model |
| **StepFun** | `stepfun-ai/step-3.7-flash` | Latest high-performance flash |

</details>

---

## 🏗️ How It Works

````
┌──────────────────── GitHub Actions (every hour) ──────────────────────┐
│                                                                       │
│   ┌─────────────────────┐        ┌─────────────────────┐              │
│   │  Job 1 — Group A    │        │  Job 2 — Group B    │ (parallel)   │
│   │  N/2 NIM models     │        │  N/2 NIM models     │              │
│   └──────────┬──────────┘        └──────────┬──────────┘              │
│              └──────────────┬───────────────┘                         │
│                    ┌────────▼────────┐                                │
│                    │  Merge + commit │ → history.db committed to repo │
│                    └─────────────────┘                                │
└───────────────────────────────────────────────────────────────────────┘
                              │
                   ┌──────────▼───────────┐
                   │  Cloudflare Pages    │ → auto-deploys on push
                   │  (static dashboard)  │   index.html + history.db
                   └──────────────────────┘
````

**Parallel jobs = ~50% faster benchmarks** ⚡

---

## 🛠️ Customization

<details>
<summary><b>Change the benchmark prompt</b></summary>

Edit `PROMPT` in `scripts/test_models.py`:
```python
PROMPT = "Your custom prompt here"
```
</details>

<details>
<summary><b>Model catalog (auto from NVIDIA /v1/models)</b></summary>

Every benchmark run pulls `GET {API_BASE}/models`, caches to `scripts/models_cache.json`, and filters to **chat-compatible** models only (drops embeddings, rerank, image-gen, reward, OCR/parse, safety-only, etc.). If the pull fails, the last local cache is used.

```bash
# Refresh cache + show chat-eligible models
python scripts/manage_models.py refresh
python scripts/manage_models.py list

# Permanently skip a model
python scripts/manage_models.py deny some-org/broken-model

# Force-include a model the filter would drop
python scripts/manage_models.py allow some-org/special-model

# Drop history.db rows for models not in the current chat set
python scripts/manage_models.py purge
```

Env knobs:
- `NIM_API_KEY` (required) — or put it in `.env`
- `MODEL_LIMIT=20` — only test first N chat models (local smoke)
- `STATIC_MODELS=a/b,c/d` — ignore catalog, use this fixed list
- `SKIP_HISTORY=1` — do not write history.db
- `models_denylist.txt` / `models_allowlist.txt` under `scripts/`
</details>



<details>
<summary><b>Live availability probe (catalog ≠ callable)</b></summary>

`/v1/models` is only a catalog. Many entries return `404 Function Not found for account` (retired / not entitled). NIMStats now:

1. Pull + filter **chat candidates**
2. **Probe** each with a tiny non-stream `/chat/completions`
3. Full stream benchmark **only `AVAILABLE`** models (default)

Statuses: `AVAILABLE` | `GONE` | `UNAUTHORIZED` | `RATE_LIMITED` | `TIMEOUT` | `ERROR`

```bash
# Probe only (fast fleet map)
PROBE_ONLY=1 python scripts/test_models.py
# or
python scripts/manage_models.py probe

# Skip probe (old behavior — not recommended)
SKIP_PROBE=1 python scripts/test_models.py

# Bench unavailable too
BENCH_ONLY_AVAILABLE=0 python scripts/test_models.py
```

Outputs: `scripts/availability_cache.json`, fleet snapshot in `results.json` summary.
</details>

<details>
<summary><b>Change the schedule</b></summary>

Edit `.github/workflows/benchmark.yml`:
```yaml
- cron: '0 */6 * * *'  # Every 6 hours instead of every hour
```
</details>

<details>
<summary><b>Run locally</b></summary>

```bash
# Serve the dashboard
python3 -m http.server 8000
# Open http://localhost:8000

# Run benchmarks manually (requires NIM_API_KEY env var)
export NIM_API_KEY=your_key_here
python3 scripts/test_models.py
```
</details>

---

## 📦 Data Storage

`history.db` is a SQLite database persisted in the repo — the single source of truth. The browser loads it via [sql.js](https://sql.js.org/) (WebAssembly) and queries it entirely client-side. `scripts/results.json` is a temporary per-job artifact that is never committed.

**Schema Architecture:**

```sql
CREATE TABLE prompts (
  id INTEGER PRIMARY KEY,
  text TEXT UNIQUE
);

CREATE TABLE models (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE,
  intelligence_score REAL DEFAULT NULL
);

CREATE TABLE errors (
  id INTEGER PRIMARY KEY,
  text TEXT UNIQUE
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL,
  prompt_id INTEGER REFERENCES prompts(id),
  fastest_model_id INTEGER REFERENCES models(id),
  fastest_time INTEGER
);

CREATE TABLE model_results (
  run_id INTEGER REFERENCES runs(id),
  model_id INTEGER REFERENCES models(id),
  success INTEGER NOT NULL,
  error_id INTEGER REFERENCES errors(id),
  response_time INTEGER,
  tokens_generated INTEGER,
  total_tokens INTEGER,
  time_to_first_token INTEGER,
  PRIMARY KEY (run_id, model_id)
);
```

**Benchmark parameters:** `temperature: 0.7` · `top_p: 0.9` · `max_tokens: 500` · OpenAI-compatible API

---

## 🤝 Contributing

Contributions are what make the open-source community amazing. Any contribution you make is **greatly appreciated**!

1. **Fork** the repository
2. Create your feature branch: `git checkout -b feat/amazing-feature`
3. Commit your changes: `git commit -m 'feat: add amazing feature'`
4. Push to the branch: `git push origin feat/amazing-feature`
5. Open a **Pull Request**

**Ideas for contributions:**
- 🆕 Add new NIM models to the benchmark list
- 📊 New chart types or dashboard widgets
- 🌐 Internationalization / translations
- 🐛 Bug fixes and performance improvements
- 📖 Improve documentation

Please read through open [Issues](https://github.com/MauroDruwel/NIMStats/issues) before starting — someone might already be working on it!

---

## 🔗 Resources

- [NVIDIA NIM API Documentation](https://docs.api.nvidia.com/nim/)
- [NVIDIA Model Catalog](https://build.nvidia.com/models)
- [GitHub Actions Docs](https://docs.github.com/en/actions)
- [sql.js — SQLite in the browser](https://sql.js.org/)

---

## 📄 License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for details.

---

<div align="center">

Made with ❤️ for the ML community · [⭐ Star this repo](https://github.com/MauroDruwel/NIMStats) if you find it useful!

[![footer](https://capsule-render.vercel.app/api?type=waving&color=76b900&height=100&section=footer)](https://nimstats.maurodruwel.be/)

</div>

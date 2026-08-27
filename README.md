# NIMStats（个人自用版）

这是 `xiaoqianran/NIMStats` 的个人 NVIDIA NIM 可用性与性能监控面板，用于学习、实验和自用数据观察，不是商业服务，也不代表 NVIDIA 或原项目作者。

- 在线面板：<https://xiaoqianran.github.io/NIMStats/>
- 当前仓库：<https://github.com/xiaoqianran/NIMStats>
- 原始项目：<https://github.com/MauroDruwel/NIMStats>
- 原项目作者：[MauroDruwel](https://github.com/MauroDruwel)

本仓库基于原项目进行学习性修改。原项目的创意、初始实现和历史贡献归原作者及其贡献者所有；此处仅保留个人维护版本，供学习教育目的使用。

## 当前版本做了什么

GitHub Actions **每小时定时跑一轮**（也可手动触发），**不会**在结束后再自 dispatch 下一轮 Benchmark（避免 Actions 次数爆炸）。一次 job 内会在约 50 分钟预算里连续跑多轮「全舰队对等 suite」，每轮所有模型测同样次数，保证 1 小时内样本更均衡。成功提交数据后由 `workflow_run` 触发 **一次** Pages 部署。

1. 在“10 把 Key 权限完全相同”的前提下，用一把轮询 Key 请求 NVIDIA `GET /v1/models`，并保留过去发现、后来下线的模型。
2. 对目录中的每个模型固定调用 `/v1/chat/completions` **6 次/轮**（health×1 + 吞吐×4 + 长输出×1），不再做跨 Key 权限确认，也不靠模型名称猜测它是否可用。
3. 约 100 模型 × 6 ≈ **600 次/轮** 的推理请求由全局轮询分给 10 把 Key；每把 Key 独立限流 **30/min**，合计 **300/min**。job 内会自动多轮，直到预算用尽。
4. 使用独立、互不混分的测试负载：
   - **Health**：极短标记回复，判断聊天接口是否真的可请求，并测量 TTFT 与响应时间；
   - **Throughput ×4**：四次固定目标 128 output tokens；只有 API 报告至少 116 tokens（90%）的样本才进入 TPS，取中位数并记录变异系数（CV）；
   - **Long Generation**：要求模型一次生成完整的 Next.js App Router 博客（6 个文件、25 项约束），自然停止或达到 3072 token 上限；保留原始回复并只记录 token、字符数、文件块、停止原因和是否截断等客观事实，不给内容质量打分。
5. 更新 `history.db`、排行榜和公开静态端点，再部署到 GitHub Pages。

前端按“观测 → 发现 → 决策 → 深入分析”重新组织：总览展示全舰队健康信号，Discover 支持按在线、有长回复、有效吞吐和低波动筛选，排行榜可分别按综合运行分、长输出 token、有效吞吐、可靠性与 TTFT 排序；模型档案可查看模型最新一次完整原文，并支持复制和下载。各工作区使用 `#discover`、`#leaderboard` 等可复制的页内地址，并为窄屏和减少动态效果偏好做了适配。

吞吐和长任务都优先使用 NVIDIA API 返回的真实 `completion_tokens`，不再用词数估算 token。若流式端点不返回 usage，只记录明确标注的字符吞吐用于诊断，不会把它当成 TPS。长任务不调用另一个模型充当裁判，也不把“输出更长”包装成“质量更高”；完整性判断只检查约定的文件边界。

模型会显示为 `AVAILABLE`、`GONE`、`UNAUTHORIZED`、`RATE_LIMITED`、`TIMEOUT`、`ERROR`、`STALE` 或 `UNKNOWN`。`AVAILABLE` 必须来自真实成功响应；仅出现在模型目录中不会被当作可用。

`google/diffusiongemma-*` 这类名字中包含 `diffusion`、但实际可走聊天接口的模型不会再被名称过滤器直接隐藏。默认还会测试完整目录，因此新的或命名特殊的模型会先经过真实请求再决定状态。

## 密钥池与限速

密钥只存放在 GitHub Actions 的加密 Secret 中，绝不能提交到仓库、数据库、日志或 Pages artifact。

在 **Settings → Secrets and variables → Actions** 中配置：

| Secret | 用途 |
|---|---|
| `NIM_API_KEYS` | 推荐。多个密钥，以换行或逗号分隔 |
| `NIM_API_KEY` | 可选。兼容旧的单密钥配置 |
| `ARTIFICIAL_ANALYSIS_API_KEY` | 可选。更新外部 intelligence 分数 |

每把 NVIDIA 密钥都有独立的滑动窗口限流器，默认最多 **30 请求/分钟**。10 把 Key 通过 round-robin 均分请求，合计 **300 请求/分钟**。每轮约 100 模型 × 6 阶段 ≈ **600** 次推理进入线程池；每把 Key 约每 2 秒放行一次（= 30 RPM 满速）。小时 job 会在预算内自动重复多轮完整 suite，使每个模型在 1 小时内被同等次数覆盖。

相关环境变量：

```text
NIM_MAX_REQUESTS_PER_MINUTE=30
NIM_MAX_IN_FLIGHT=600
BATCH_SIZE=0
THROUGHPUT_SAMPLE_COUNT=4
SUITE_ROUNDS=0
RUN_BUDGET_SECONDS=3000
INCLUDE_ALL_CATALOG_MODELS=1
REQUEST_TIMEOUT_SECONDS=90
HEALTH_MAX_TOKENS=24
THROUGHPUT_MAX_TOKENS=128
LONG_TASK_MAX_TOKENS=3072
LONG_TASK_TIMEOUT_SECONDS=300
STALE_AFTER_MINUTES=180
```

`SUITE_ROUNDS=0` 表示按 `RUN_BUDGET_SECONDS` 自动决定轮数；`BATCH_SIZE=0` 表示每轮检查整个目录。若只想做本地小规模测试，可以设置正整数或使用 `MODEL_LIMIT`。

## GitHub Pages 部署

仓库的 Pages Source 必须设置为 **GitHub Actions**。

部署路径：`.github/workflows/deploy-pages.yml`。

- 基准 workflow **每小时定时跑一次**（也可手动 `workflow_dispatch`），**不会**在结束后再自动链式启动下一次——避免 Actions 刷屏与 API 限流。
- 一次 job 内多轮对等 suite 测完并提交后，由 `workflow_run` 自动触发 **一次** Pages 部署。
- 静态资源（`index.html` / `css` / `js` 等）直接 push 到 `main` 也会触发部署。

因此：**Pages 大约每小时部署 1 次**（与基准同频），不会每几分钟刷一次。

构建脚本 `scripts/build_pages.py` 会：

- 只复制看板所需的白名单静态文件；
- 对 `history.db` 执行 SQLite 完整性和表结构检查；
- 生成 GitHub Pages 可识别的无扩展名 API 路由；
- 确保密钥、临时结果和基准脚本不会进入站点 artifact。

公开端点：

| 数据 | JSON | 纯文本 |

|---|---|---|
| 综合最佳 | [`/top/`](https://xiaoqianran.github.io/NIMStats/top/) | [`/top/model`](https://xiaoqianran.github.io/NIMStats/top/model) |
| 速度最佳 | [`/top/speed`](https://xiaoqianran.github.io/NIMStats/top/speed) | [`/top/speed/model`](https://xiaoqianran.github.io/NIMStats/top/speed/model) |
| Intelligence 最佳 | [`/top/intelligence`](https://xiaoqianran.github.io/NIMStats/top/intelligence) | [`/top/intelligence/model`](https://xiaoqianran.github.io/NIMStats/top/intelligence/model) |
| 最新长输出 token 最多（非质量分） | [`/top/generation`](https://xiaoqianran.github.io/NIMStats/top/generation) | [`/top/generation/model`](https://xiaoqianran.github.io/NIMStats/top/generation/model) |

也可以使用显式文件路径，例如 `top/speed.json` 和 `top/speed.txt`。

## 本地运行

```bash
export NIM_API_KEY='your-key'
python3 -u scripts/rolling_bench.py
python3 scripts/generate_best.py
python3 scripts/build_pages.py --output _site
python3 -m http.server 8000 --directory _site
```

浏览器打开 <http://localhost:8000/>。本地简单 HTTP 服务器不模拟 GitHub Pages 的无扩展名目录索引，调试 API 时可直接访问 `.json` / `.txt` 文件。

常用模型目录命令：

```bash
python3 scripts/manage_models.py refresh
python3 scripts/manage_models.py list
python3 scripts/manage_models.py deny some-org/model
python3 scripts/manage_models.py allow some-org/model
```

## 数据说明

`history.db` 是 Actions 侧的 SQLite 历史数据源。Pages 构建时会预聚合为轻量静态 JSON；数据库本身不再进入 Pages artifact，浏览器不会下载或扫描整库。每次运行的轻量长任务指标保存在历史结果中；体积较大的完整回复则在 `model_outputs` 中按模型覆盖，所以每个模型最多公开一份最新原文，不会在每个历史批次重复堆积。`scripts/models_cache.json` 保存模型目录并集；`scripts/fleet_snapshot.json` 是当前舰队状态快照。临时的 `scripts/results.json` 不提交。

历史趋势只保留每批恰好包含 100 个不同模型结果的完整测试；早期 2、4、9、76 模型的试运行数据已清除，避免不同样本规模污染成功率、趋势和排行榜。

性能数据会受到模型冷启动、共享服务负载、网络、模型分词器和输出服从度影响，只适合趋势观察，不应视为严格的实验室评测或服务等级承诺。

## 使用声明与致谢

本仓库仅供个人学习、教育和非商业实验。使用 NVIDIA API 时请遵守 NVIDIA 的服务条款、密钥管理要求和限速规则；请勿将本项目用于滥用接口、绕过平台限制或对外提供未经授权的服务。

特别感谢 [MauroDruwel/NIMStats](https://github.com/MauroDruwel/NIMStats) 的原始工作，以及原仓库所有贡献者。本个人版本中的修改不代表原作者立场。

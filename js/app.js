function latestCheckedAt() {
  const timestamps = state.modelNames
    .map(m => state.modelStats[m]?.lastCheckedAt)
    .filter(Boolean)
    .map(ts => new Date(ts).getTime())
    .filter(Number.isFinite);
  return timestamps.length ? new Date(Math.max(...timestamps)).toISOString() : null;
}

function initTheme() {
  const saved = localStorage.getItem('nimstats-theme');
  const systemDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = saved || (systemDark ? 'dark' : 'light');
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('nimstats-theme', next);
  });
}

function switchView(view, updateHash = true) {
  const valid = ['overview', 'models', 'runs'];
  if (!valid.includes(view)) view = 'overview';
  state.currentView = view;
  document.querySelectorAll('[data-view-panel]').forEach(panel => panel.classList.toggle('is-active', panel.dataset.viewPanel === view));
  document.querySelectorAll('.nav-link[data-view]').forEach(button => button.classList.toggle('is-active', button.dataset.view === view));
  if (updateHash && location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  if (view === 'models') renderModels();
  if (view === 'runs') renderRuns();
  window.scrollTo({ top: 0, behavior: 'instant' });
}

function currentOverviewMetrics() {
  const stats = state.modelStats;
  const available = state.modelNames.filter(m => stats[m]?.displayStatus === 'AVAILABLE').length;
  const stale = state.modelNames.filter(m => stats[m]?.displayStatus === 'STALE').length;
  const gone = state.modelNames.filter(m => stats[m]?.displayStatus === 'GONE').length;
  const medianUptime = metricMedian(state.modelNames.map(m => stats[m]?.uptime));
  const medianTps = metricMedian(state.modelNames.map(m => stats[m]?.avgTps));
  const medianTtft = metricMedian(state.modelNames.map(m => stats[m]?.avgTtft));
  return { available, stale, gone, medianUptime, medianTps, medianTtft };
}

function renderOverview() {
  const m = currentOverviewMetrics();
  const total = state.modelNames.length;
  document.getElementById('overview-metrics').innerHTML = [
    ['当前可用', `${m.available} / ${total}`, `${m.stale} 过期 · ${m.gone} 离线`],
    ['中位可靠性', m.medianUptime == null ? '—' : fmtPct(m.medianUptime), '按历史已测试轮次计算'],
    ['中位吞吐', m.medianTps == null ? '—' : fmtTps(m.medianTps), '有效吞吐样本'],
    ['中位 TTFT', m.medianTtft == null ? '—' : fmtMs(m.medianTtft), '首 token 延迟']
  ].map(([label, value, note]) => `<div class="metric"><span class="metric-label">${label}</span><strong class="metric-value">${value}</strong><span class="metric-note">${note}</span></div>`).join('');

  const checked = latestCheckedAt();
  document.getElementById('overview-freshness').textContent = checked ? `最近检查 ${relativeTime(checked)} · ${fmtTimestamp(checked)}` : '暂无检查时间';
  document.getElementById('sync-label').textContent = checked ? `更新于 ${relativeTime(checked)}` : '未同步';

  renderAttention();
  renderHealthChart();
  renderOverviewRows();
}

function attentionReason(model) {
  const s = state.modelStats[model];
  if (!s) return '暂无数据';
  if (s.displayStatus === 'GONE') return s.lastHttpStatus ? `当前离线 · HTTP ${s.lastHttpStatus}` : '当前离线';
  if (s.displayStatus === 'STALE') return `检查已过期 · ${relativeTime(s.lastCheckedAt)}`;
  if (s.displayStatus === 'UNKNOWN') return '当前状态未知';
  if (s.throughputCv != null && s.throughputCv >= .2) return `吞吐波动 ${(s.throughputCv * 100).toFixed(1)}%`;
  if (s.uptime < .9) return `历史可靠性 ${(s.uptime * 100).toFixed(1)}%`;
  return '建议检查近期表现';
}

function attentionModels() {
  return state.modelNames
    .filter(model => {
      const s = state.modelStats[model];
      return s && (s.displayStatus !== 'AVAILABLE' || (s.throughputCv != null && s.throughputCv >= .2) || s.uptime < .9);
    })
    .sort((a, b) => {
      const sa = state.modelStats[a], sb = state.modelStats[b];
      const statusDiff = statusRank(sa.displayStatus) - statusRank(sb.displayStatus);
      if (statusDiff) return statusDiff;
      const cvDiff = (sb.throughputCv ?? 0) - (sa.throughputCv ?? 0);
      if (cvDiff) return cvDiff;
      return (sa.uptime ?? 0) - (sb.uptime ?? 0);
    });
}

function renderAttention() {
  const target = document.getElementById('attention-list');
  const models = attentionModels().slice(0, 4);
  if (!models.length) {
    target.innerHTML = '<div class="all-good"><div><strong>当前没有明显异常</strong><span>所有模型的当前状态与稳定性都在预期范围内。</span></div></div>';
    return;
  }
  target.innerHTML = models.map(model => {
    const s = state.modelStats[model];
    return `<button class="attention-item" type="button" data-detail-model="${escHtml(model)}"><span class="attention-model"><strong>${escHtml(shortModel(model))}</strong><span>${escHtml(providerName(model))} · ${statusLabel(s.displayStatus)}</span></span><span class="attention-reason">${escHtml(attentionReason(model))} →</span></button>`;
  }).join('');
}

function renderHealthChart() {
  const target = document.getElementById('health-chart');
  const runs = state.healthRuns.slice(-30);
  const values = runs.map(run => run.summary.totalModels ? run.summary.successCount / run.summary.totalModels : null);
  const points = values.map((value, i) => value == null ? null : [i, value]).filter(Boolean);
  if (points.length < 2) {
    target.innerHTML = '<div class="all-good"><div><strong>趋势数据不足</strong><span>至少需要两轮有效运行。</span></div></div>';
    return;
  }
  const w = 600, h = 180, padL = 34, padR = 8, padT = 10, padB = 23;
  const x = i => padL + (i / Math.max(values.length - 1, 1)) * (w - padL - padR);
  const y = v => padT + (1 - v) * (h - padT - padB);
  const line = points.map(([i, v], n) => `${n ? 'L' : 'M'} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
  const first = points[0], last = points[points.length - 1];
  const area = `${line} L ${x(last[0]).toFixed(1)} ${h-padB} L ${x(first[0]).toFixed(1)} ${h-padB} Z`;
  const grid = [1, .75, .5].map(v => `<line class="chart-grid" x1="${padL}" y1="${y(v)}" x2="${w-padR}" y2="${y(v)}"></line><text class="chart-label" x="0" y="${y(v)+3}">${Math.round(v*100)}%</text>`).join('');
  target.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="最近 ${runs.length} 轮成功率趋势">${grid}<path class="chart-area" d="${area}"></path><path class="chart-line" d="${line}"></path><circle class="chart-point" cx="${x(last[0])}" cy="${y(last[1])}" r="3"></circle><text class="chart-label" x="${Math.max(padL, x(last[0])-35)}" y="${h-5}">${Math.round(last[1]*100)}%</text></svg>`;
}

function overviewSortedModels() {
  return [...state.modelNames].sort((a, b) => {
    const sa = state.modelStats[a], sb = state.modelStats[b];
    const statusDiff = statusRank(sb.displayStatus) - statusRank(sa.displayStatus);
    if (statusDiff) return statusDiff;
    return (sb.score ?? -1) - (sa.score ?? -1);
  });
}

function renderOverviewRows() {
  document.getElementById('overview-model-rows').innerHTML = overviewSortedModels().slice(0, 8).map(model => {
    const s = state.modelStats[model];
    return `<tr data-model="${escHtml(model)}"><td class="model-cell"><strong>${escHtml(shortModel(model))}</strong><span>${escHtml(providerName(model))}</span></td><td>${statusBadge(s.displayStatus)}</td><td class="num mono-num">${fmtPct(s.uptime)}</td><td class="num mono-num">${fmtMs(s.avgTtft)}</td><td class="num mono-num">${fmtTps(s.avgTps)}</td><td class="num mono-num">${fmtCv(s.throughputCv)}</td></tr>`;
  }).join('');
}

function populateProviderFilter() {
  const select = document.getElementById('provider-filter');
  const providers = [...new Set(state.modelNames.map(getProvider))].sort((a,b) => providerName(`${a}/x`).localeCompare(providerName(`${b}/x`)));
  select.innerHTML = '<option value="all">全部 Provider</option>' + providers.map(p => `<option value="${escHtml(p)}">${escHtml(PROVIDER_META[p] || p)}</option>`).join('');
}

function modelSortValue(model, key) {
  const s = state.modelStats[model];
  const map = { name: shortModel(model).toLowerCase(), status: statusRank(s.displayStatus), score: s.score ?? -Infinity, uptime: s.uptime ?? -Infinity, ttft: s.avgTtft ?? Infinity, tps: s.avgTps ?? -Infinity, cv: s.throughputCv ?? Infinity };
  return map[key];
}

function filteredModels() {
  const q = state.modelQuery.trim().toLowerCase();
  return state.modelNames.filter(model => {
    const s = state.modelStats[model];
    const queryMatch = !q || model.toLowerCase().includes(q) || providerName(model).toLowerCase().includes(q);
    const providerMatch = state.providerFilter === 'all' || getProvider(model) === state.providerFilter;
    const statusMatch = state.statusFilter === 'all' || (state.statusFilter === 'UNKNOWN' ? !['AVAILABLE','STALE','GONE'].includes(s.displayStatus) : s.displayStatus === state.statusFilter);
    return queryMatch && providerMatch && statusMatch;
  }).sort((a,b) => {
    const av = modelSortValue(a, state.modelSort.key), bv = modelSortValue(b, state.modelSort.key);
    let cmp = 0;
    if (typeof av === 'string') cmp = av.localeCompare(bv);
    else cmp = av === bv ? 0 : av < bv ? -1 : 1;
    return state.modelSort.dir === 'asc' ? cmp : -cmp;
  });
}

function renderModels() {
  const models = filteredModels();
  const total = state.modelNames.length;
  document.getElementById('models-summary').textContent = models.length === total ? `${total} 个模型 · 点击列标题即可排序` : `${models.length} / ${total} 个模型符合当前筛选`;
  const tbody = document.getElementById('model-table-body');
  tbody.innerHTML = models.map(model => {
    const s = state.modelStats[model];
    const checked = state.selectedModels.includes(model);
    const disabled = state.selectedModels.length >= 2 && !checked;
    return `<tr data-model="${escHtml(model)}"><td class="select-col"><input class="row-check" type="checkbox" data-compare-model="${escHtml(model)}" aria-label="选择 ${escHtml(shortModel(model))} 进行比较" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}></td><td class="model-cell"><strong>${escHtml(shortModel(model))}</strong><span>${escHtml(providerName(model))}</span></td><td>${statusBadge(s.displayStatus)}</td><td class="num"><span class="score-pill">${s.score ?? '—'}</span></td><td class="num mono-num">${fmtPct(s.uptime)}</td><td class="num mono-num">${fmtMs(s.avgTtft)}</td><td class="num mono-num">${fmtTps(s.avgTps)}</td><td class="num mono-num">${fmtCv(s.throughputCv)}</td><td>${s.lastCheckedAt ? `<span title="${escHtml(fmtTimestamp(s.lastCheckedAt))}">${relativeTime(s.lastCheckedAt)}</span>` : '—'}</td></tr>`;
  }).join('');
  document.getElementById('model-empty').hidden = models.length > 0;
  document.getElementById('clear-filters').hidden = !(state.modelQuery || state.providerFilter !== 'all' || state.statusFilter !== 'all');
  document.querySelectorAll('.sort-button').forEach(button => {
    const active = button.dataset.sort === state.modelSort.key;
    button.classList.toggle('is-active', active);
    button.querySelector('span').textContent = active ? (state.modelSort.dir === 'asc' ? '↑' : '↓') : '';
  });
}

function toggleCompareModel(model, checked) {
  if (checked && !state.selectedModels.includes(model) && state.selectedModels.length < 2) state.selectedModels.push(model);
  if (!checked) state.selectedModels = state.selectedModels.filter(m => m !== model);
  updateCompareBar();
  renderModels();
}

function updateCompareBar() {
  const bar = document.getElementById('compare-bar');
  const count = state.selectedModels.length;
  bar.hidden = count === 0;
  document.getElementById('compare-count').textContent = count === 2 ? '可以开始比较' : `已选择 ${count} 个模型`;
  document.getElementById('compare-names').textContent = state.selectedModels.map(shortModel).join('  ·  ');
  document.getElementById('open-compare').disabled = count !== 2;
}

function lineSpark(values) {
  const vals = values.map((v,i) => [i,v]).filter(([,v]) => Number.isFinite(v));
  if (vals.length < 2) return '<div class="all-good"><span>趋势样本不足</span></div>';
  const w=420,h=58,p=3, min=Math.min(...vals.map(v=>v[1])), max=Math.max(...vals.map(v=>v[1])), range=max-min || 1;
  const x=i => p + i / Math.max(values.length-1,1) * (w-p*2), y=v => h-p-((v-min)/range)*(h-p*2);
  const d=vals.map(([i,v],n)=>`${n?'L':'M'} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}"></path></svg>`;
}

function openModelDrawer(model) {
  const s = state.modelStats[model];
  if (!s) return;
  const drawer = document.getElementById('model-drawer');
  const output = s.longOutput;
  const errorEntries = Object.entries(s.errors || {}).sort((a,b)=>b[1]-a[1]);
  drawer.innerHTML = `
    <div class="drawer-header"><div class="drawer-title"><strong>${escHtml(shortModel(model))}</strong><span>${escHtml(model)}</span></div><button class="close-button" type="button" data-close-drawer aria-label="关闭">×</button></div>
    <div class="drawer-body">
      <div class="detail-status-line">${statusBadge(s.displayStatus)}<span>检查于 ${s.lastCheckedAt ? `${relativeTime(s.lastCheckedAt)} · ${fmtTimestamp(s.lastCheckedAt)}` : '未知'}</span></div>
      <div class="detail-metrics"><div class="detail-metric"><span>综合评分</span><strong>${s.score ?? '—'}</strong></div><div class="detail-metric"><span>历史可靠性</span><strong>${fmtPct(s.uptime)}</strong></div><div class="detail-metric"><span>智能评分</span><strong>${s.intelligence == null ? '—' : Number(s.intelligence).toFixed(1)}</strong></div></div>
      <section class="detail-section"><h3>性能</h3><div class="detail-list">
        <div class="detail-row"><span>TTFT</span><span>${fmtMs(s.avgTtft)}</span></div>
        <div class="detail-row"><span>Decode throughput</span><span>${fmtTps(s.avgTps)}</span></div>
        <div class="detail-row"><span>Throughput CV</span><span>${fmtCv(s.throughputCv)}</span></div>
        <div class="detail-row"><span>有效样本</span><span>${s.throughputSampleCount || '—'}</span></div>
        <div class="detail-row"><span>最近成功</span><span>${s.lastSeen ? fmtTimestamp(s.lastSeen) : '—'}</span></div>
      </div></section>
      <section class="detail-section"><h3>响应时间趋势</h3><div class="spark">${lineSpark(s.responseTimes.slice(-30))}</div></section>
      ${s.lastError ? `<section class="detail-section"><h3>最近错误</h3><div class="detail-list"><div class="detail-row"><span>HTTP</span><span>${s.lastHttpStatus ?? '—'}</span></div><div class="detail-row"><span>错误类型</span><span>${escHtml(categorizeError(s.lastError))}</span></div></div></section>` : ''}
      ${errorEntries.length ? `<section class="detail-section"><h3>历史错误</h3><div class="detail-list">${errorEntries.slice(0,5).map(([name,count])=>`<div class="detail-row"><span>${escHtml(name)}</span><span>${count}</span></div>`).join('')}</div></section>` : ''}
      ${output ? `<section class="detail-section"><h3>最新长输出</h3><div class="detail-list"><div class="detail-row"><span>Tokens</span><span>${output.completionTokens ?? '—'}</span></div><div class="detail-row"><span>完成状态</span><span>${output.outputComplete ? 'complete' : output.truncated ? 'truncated' : 'incomplete'}</span></div><div class="detail-row"><span>长任务吞吐</span><span>${fmtTps(output.decodeTps)}</span></div></div>${output.responseText ? `<details class="output-box"><summary>查看输出内容</summary><pre>${escHtml(output.responseText)}</pre></details>` : ''}</section>` : ''}
    </div>`;
  document.getElementById('drawer-backdrop').hidden = false;
  drawer.setAttribute('aria-hidden','false');
  requestAnimationFrame(() => drawer.classList.add('is-open'));
}

function closeModelDrawer() {
  const drawer = document.getElementById('model-drawer');
  drawer.classList.remove('is-open');
  drawer.setAttribute('aria-hidden','true');
  document.getElementById('drawer-backdrop').hidden = true;
}

function compareValue(s, key) {
  const map = {
    status: statusBadge(s.displayStatus), score: s.score ?? '—', uptime: fmtPct(s.uptime),
    ttft: fmtMs(s.avgTtft), tps: fmtTps(s.avgTps), cv: fmtCv(s.throughputCv),
    intel: s.intelligence == null ? '—' : Number(s.intelligence).toFixed(1),
    long: s.longCompletionTokens ?? '—'
  };
  return map[key];
}

function openCompareModal() {
  if (state.selectedModels.length !== 2) return;
  const [a,b] = state.selectedModels, sa=state.modelStats[a], sb=state.modelStats[b];
  const rows = [['status','当前状态'],['score','综合评分'],['uptime','历史可靠性'],['ttft','TTFT'],['tps','吞吐'],['cv','吞吐波动'],['intel','智能评分'],['long','最新长输出 tokens']];
  const modal = document.getElementById('compare-modal');
  modal.innerHTML = `<div class="modal-header"><h2 id="compare-title">模型比较</h2><button class="close-button" type="button" data-close-modal aria-label="关闭">×</button></div><div class="modal-body"><div class="compare-head"><span></span><div class="compare-model"><strong>${escHtml(shortModel(a))}</strong><span>${escHtml(providerName(a))}</span></div><div class="compare-model"><strong>${escHtml(shortModel(b))}</strong><span>${escHtml(providerName(b))}</span></div></div><div class="compare-grid">${rows.map(([key,label])=>`<div class="compare-row"><div class="compare-label">${label}</div><div>${compareValue(sa,key)}</div><div>${compareValue(sb,key)}</div></div>`).join('')}</div></div>`;
  document.getElementById('modal-backdrop').hidden = false;
  modal.setAttribute('aria-hidden','false');
  requestAnimationFrame(() => modal.classList.add('is-open'));
}

function closeCompareModal() {
  const modal = document.getElementById('compare-modal');
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden','true');
  document.getElementById('modal-backdrop').hidden = true;
}

function runDateLabel(ts) {
  return new Intl.DateTimeFormat('zh-CN', { year:'numeric', month:'long', day:'numeric', weekday:'short' }).format(new Date(ts));
}

async function ensureRunsLoaded() {
  if (state.runsLoaded) return true;
  const target = document.getElementById('runs-list');
  target.innerHTML = '<div class="empty-state"><strong>正在读取运行记录</strong><span>只在需要时加载最近 100 轮明细…</span></div>';
  try {
    const response = await fetch('data/runs.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error(`runs.json HTTP ${response.status}`);
    const payload = await response.json();
    state.rawRuns = payload.runs || [];
    state.totalRunCount = payload.totalRunCount || state.totalRunCount || state.rawRuns.length;
    state.runsLoaded = true;
    return true;
  } catch (err) {
    console.error('Runs load failed', err);
    target.innerHTML = `<div class="empty-state"><strong>运行记录读取失败</strong><span>${escHtml(err.message)}</span></div>`;
    return false;
  }
}

async function renderRuns() {
  if (!state.runsLoaded && !(await ensureRunsLoaded())) return;
  const raw = state.rawRuns;
  const limit = Number(state.runsLimit);
  const runs = raw.slice(-limit).reverse();
  document.getElementById('runs-summary').textContent = `历史共 ${state.totalRunCount} 轮 · 当前提供最近 ${raw.length} 轮明细 · 显示 ${runs.length} 轮`;
  let dateKey = '';
  const html = [];
  runs.forEach(run => {
    const nextDate = runDateLabel(run.timestamp);
    if (nextDate !== dateKey) { dateKey = nextDate; html.push(`<div class="date-divider">${escHtml(dateKey)}</div>`); }
    const total = run.summary.totalModels || run.models.length;
    const success = run.summary.successCount || 0;
    const ratio = total ? success / total : 0;
    const time = new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(run.timestamp));
    html.push(`<details class="run-card" data-run-id="${run._dbId}"><summary class="run-summary"><div class="run-time"><strong>${time}</strong><span>${relativeTime(run.timestamp)}</span></div><div class="run-health"><strong>${success} / ${total} 成功</strong><span>${(ratio*100).toFixed(1)}%</span><div class="run-bar"><span style="width:${Math.max(0,Math.min(100,ratio*100))}%"></span></div></div><div class="run-fastest">最快 <strong>${escHtml(shortModel(run.summary.fastestModel))}</strong>${run.summary.fastestTime ? ` · ${fmtMs(run.summary.fastestTime)}` : ''}</div><span class="run-kind">${escHtml(run.summary.kind || 'run')}</span><span class="run-chevron">›</span></summary><div class="run-details" data-run-details>展开后显示本轮模型明细</div></details>`);
  });
  document.getElementById('runs-list').innerHTML = html.join('');
}
function renderRunDetails(card) {
  const target = card.querySelector('[data-run-details]');
  if (!target || target.dataset.rendered) return;
  const runId = Number(card.dataset.runId);
  const run = state.rawRuns.find(r => r._dbId === runId);
  if (!run) return;
  const rows = [...run.models].sort((a,b)=>(Number(b.success)-Number(a.success)) || ((b.decodeTps ?? -1)-(a.decodeTps ?? -1)));
  target.innerHTML = `<div class="table-scroll"><table class="data-table"><thead><tr><th>模型</th><th>结果</th><th class="num">响应</th><th class="num">TTFT</th><th class="num">吞吐</th><th>测试类型</th></tr></thead><tbody>${rows.map(r=>`<tr data-model="${escHtml(r.model)}"><td class="model-cell"><strong>${escHtml(shortModel(r.model))}</strong><span>${escHtml(providerName(r.model))}</span></td><td>${r.success ? statusBadge('AVAILABLE') : `<span class="status-badge status-gone"><span class="status-dot"></span>${escHtml(r.status || '失败')}</span>`}</td><td class="num mono-num">${fmtMs(r.responseTime)}</td><td class="num mono-num">${fmtMs(r.timeToFirstToken)}</td><td class="num mono-num">${fmtTps(r.decodeTps)}</td><td>${escHtml(r.testKind || '—')}</td></tr>`).join('')}</tbody></table></div>`;
  target.dataset.rendered = '1';
}

function bindEvents() {
  document.querySelectorAll('.nav-link[data-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  window.addEventListener('hashchange', () => switchView(location.hash.slice(1), false));

  document.querySelectorAll('[data-open-models]').forEach(button => button.addEventListener('click', () => {
    state.modelQuery = '';
    state.providerFilter = 'all';
    state.statusFilter = 'all';
    state.attentionOnly = button.dataset.openModels === 'attention';
    if (state.attentionOnly) state.modelSort = { key: 'status', dir: 'asc' };
    document.getElementById('model-search').value = '';
    document.getElementById('provider-filter').value = 'all';
    document.getElementById('status-filter').value = state.statusFilter;
    switchView('models');
  }));

  document.getElementById('model-search').addEventListener('input', e => { state.modelQuery = e.target.value; renderModels(); });
  document.getElementById('provider-filter').addEventListener('change', e => { state.providerFilter = e.target.value; renderModels(); });
  document.getElementById('status-filter').addEventListener('change', e => { state.statusFilter = e.target.value; renderModels(); });
  document.getElementById('clear-filters').addEventListener('click', () => {
    state.modelQuery=''; state.providerFilter='all'; state.statusFilter='all'; state.attentionOnly=false;
    document.getElementById('model-search').value=''; document.getElementById('provider-filter').value='all'; document.getElementById('status-filter').value='all';
    renderModels();
  });

  document.querySelectorAll('.sort-button').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.sort;
    if (state.modelSort.key === key) state.modelSort.dir = state.modelSort.dir === 'asc' ? 'desc' : 'asc';
    else { state.modelSort.key = key; state.modelSort.dir = ['name','ttft','cv'].includes(key) ? 'asc' : 'desc'; }
    renderModels();
  }));

  document.getElementById('model-table-body').addEventListener('click', e => {
    const checkbox = e.target.closest('[data-compare-model]');
    if (checkbox) { e.stopPropagation(); toggleCompareModel(checkbox.dataset.compareModel, checkbox.checked); return; }
    const row = e.target.closest('tr[data-model]'); if (row) openModelDrawer(row.dataset.model);
  });
  document.getElementById('overview-model-rows').addEventListener('click', e => { const row=e.target.closest('tr[data-model]'); if (row) openModelDrawer(row.dataset.model); });
  document.getElementById('attention-list').addEventListener('click', e => { const item=e.target.closest('[data-detail-model]'); if (item) openModelDrawer(item.dataset.detailModel); });

  document.getElementById('drawer-backdrop').addEventListener('click', closeModelDrawer);
  document.getElementById('model-drawer').addEventListener('click', e => { if (e.target.closest('[data-close-drawer]')) closeModelDrawer(); });
  document.getElementById('clear-compare').addEventListener('click', () => { state.selectedModels=[]; updateCompareBar(); renderModels(); });
  document.getElementById('open-compare').addEventListener('click', openCompareModal);
  document.getElementById('modal-backdrop').addEventListener('click', closeCompareModal);
  document.getElementById('compare-modal').addEventListener('click', e => { if (e.target.closest('[data-close-modal]')) closeCompareModal(); });

  document.getElementById('runs-limit').addEventListener('change', e => { state.runsLimit=e.target.value; renderRuns(); });
  document.getElementById('runs-list').addEventListener('toggle', e => { const card=e.target.closest?.('.run-card'); if (card?.open) renderRunDetails(card); }, true);
  document.getElementById('runs-list').addEventListener('click', e => { const row=e.target.closest('tr[data-model]'); if (row) openModelDrawer(row.dataset.model); });

  document.addEventListener('keydown', e => {
    const typing = ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName);
    if (e.key === '/' && !typing) { e.preventDefault(); switchView('models'); document.getElementById('model-search').focus(); }
    if (e.key === 'Escape') { closeModelDrawer(); closeCompareModal(); }
  });
}

async function init() {
  initTheme();
  try {
    const response = await fetch('data/site.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error(`site.json HTTP ${response.status}`);
    const data = await response.json();
    state.modelNames = data.modelNames || [];
    state.modelStats = data.modelStats || {};
    state.healthRuns = data.healthRuns || [];
    state.totalRunCount = data.totalRunCount || 0;
    state.staleAfterMinutes = data.staleAfterMinutes || 180;
    for (const model of state.modelNames) {
      const stats = state.modelStats[model];
      if (stats) stats.displayStatus = displayStatus(stats.currentStatus, stats.lastCheckedAt, state.staleAfterMinutes);
    }

    populateProviderFilter();
    bindEvents();
    renderOverview();
    renderModels();
    updateCompareBar();
    document.getElementById('loading').hidden = true;
    document.getElementById('app').hidden = false;
    switchView(location.hash.slice(1) || 'overview', false);
  } catch (err) {
    console.error('NIMStats init failed', err);
    document.getElementById('loading').hidden = true;
    document.getElementById('app').hidden = false;
    document.getElementById('error-state').hidden = false;
    document.querySelectorAll('.view').forEach(view => view.classList.remove('is-active'));
    document.getElementById('error-msg').textContent = `无法读取页面数据：${err.message}`;
  }
}
init();

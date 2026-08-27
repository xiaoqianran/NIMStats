const PROVIDER_META = {
  'deepseek-ai': 'DeepSeek', 'z-ai': 'Z-AI', 'minimaxai': 'MiniMax', 'nvidia': 'NVIDIA',
  'moonshotai': 'Moonshot', 'openai': 'OpenAI', 'google': 'Google', 'qwen': 'Qwen',
  'mistralai': 'Mistral', 'meta': 'Meta'
};

const state = {
  db: null, rawRuns: [], runs: [], modelNames: [], modelStats: {}, modelMeta: {}, modelIntel: {},
  staleAfterMinutes: 180, currentView: 'overview', modelQuery: '', providerFilter: 'all',
  statusFilter: 'all', modelSort: { key: 'score', dir: 'desc' }, selectedModels: [], runsLimit: '30'
};

function avg(arr) { return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0; }
function shortModel(model) { return model?.split('/').slice(1).join('/') || model || '—'; }
function getProvider(model) { return model?.split('/')[0] || 'unknown'; }
function providerName(model) { const p = getProvider(model); return PROVIDER_META[p] || p; }
function fmtMs(ms) { return ms == null ? '—' : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`; }
function fmtTps(tps) { return tps == null || tps <= 0 ? '—' : `${tps.toFixed(1)} t/s`; }
function fmtPct(v) { return v == null ? '—' : `${(v * 100).toFixed(1)}%`; }
function fmtCv(v) { return v == null ? '—' : `${(v * 100).toFixed(1)}%`; }
function fmtTimestamp(ts) {
  if (!ts) return '—';
  return new Intl.DateTimeFormat('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false }).format(new Date(ts));
}
function relativeTime(ts) {
  if (!ts) return '未知';
  const delta = Date.now() - new Date(ts).getTime();
  if (delta < 0) return '刚刚';
  const m = Math.floor(delta / 60000);
  if (m < 1) return '刚刚';
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}
function escHtml(value) {
  return String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}
function categorizeError(err) {
  if (!err) return 'Unknown';
  if (err.includes('timed out')) return 'Timeout';
  if (err.includes('JSON')) return 'JSON Error';
  if (err.includes('404')) return 'Not Found (404)';
  if (err.includes('410')) return 'Gone (410)';
  if (err.includes('closed connection')) return 'Connection Closed';
  return 'Other Error';
}
function statusLabel(status) { return ({ AVAILABLE:'可用', STALE:'过期', GONE:'离线', UNKNOWN:'未知' })[status] || status || '未知'; }
function statusRank(status) { return ({ GONE:0, STALE:1, UNKNOWN:2, AVAILABLE:3 })[status] ?? 2; }
function statusBadge(status) {
  const normalized = ['AVAILABLE','STALE','GONE'].includes(status) ? status : 'UNKNOWN';
  return `<span class="status-badge status-${normalized.toLowerCase()}"><span class="status-dot"></span>${statusLabel(normalized)}</span>`;
}
function metricMedian(values) {
  const nums = values.filter(v => Number.isFinite(v)).sort((a,b) => a-b);
  if (!nums.length) return null;
  const mid = Math.floor(nums.length / 2);
  return nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2;
}

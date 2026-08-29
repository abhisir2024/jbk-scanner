// ==================== STATE ====================
let allSignals = [];
let filteredSignals = [];
let allIndexSignals = [];
let qualityStats = {};
let livePrices = {};
let currentType = 'all';
let currentConfirmed = false;
let _lastSignalsSig = '';
let _lastBmSig = '';
let currentStrategy = 'all';
let currentTimeframe = 'D';
let autoEnabled = false;
let sortField = 'quality_score';
let sortDir = 'desc';
let selectedRow = null;

// ==================== STRATEGY TAG CSS ====================
function stratTagClass(name) {
  const n = name.toLowerCase();
  if (n.includes('index range')) return 'indexrb';
  if (n.includes('index support')) return 'indexsr';
  if (n.includes('med channel')) return 'medch';
  if (n.includes('range')) return 'range';
  if (n.includes('channel')) return 'channel';
  if (n.includes('early')) return 'early';
  if (n.includes('52w')) return 'high52w';
  if (n.includes('candle')) return 'candle';
  if (n.includes('volume')) return 'volume';
  if (n.includes('watchlist')) return 'watchlist';
  if (n.includes('retracement')) return 'retracement';
  if (n.includes('channel breakout') && n.includes('trendline')) return 'channel';
  if (n.includes('momentum')) return 'momentum';
  return '';
}

function qualityChip(s) {
  const qs = s.quality_score;
  if (qs === undefined || qs === null) return '<span class="quality-chip" style="opacity:0.35">—</span>';
  const tier = s.quality_tier || 'MODERATE';
  const colors = {'VERY HIGH':'#22c55e','HIGH':'#4ade80','MODERATE':'#eab308','LOW':'#f97316','VERY LOW':'#ef4444'};
  const emojis = {'VERY HIGH':'🔥','HIGH':'✅','MODERATE':'⚡','LOW':'⚠️','VERY LOW':'🚫'};
  const c = colors[tier] || '#94a3b8';
  const e = emojis[tier] || '';
  return `<span class="quality-chip" style="background:${c}18;color:${c};border:1px solid ${c}44;font-weight:700;cursor:default" title="Quality: ${qs}/100 (${tier})">
    ${e} ${qs}
  </span>`;
}

function strengthBadgeClass(strength) {
  const s = (strength || '').toLowerCase().replace(/ /g, '-');
  return 'strength-' + s;
}

function symbolColor(name) {
  const colors = ['#3b82f6','#22c55e','#a855f7','#f97316','#06b6d4','#eab308','#ec4899','#ef4444','#14b8a6','#8b5cf6'];
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

// ==================== TIMEFRAME ====================
function setTimeframe(tf) {
  currentTimeframe = tf;
  document.querySelectorAll('[id^="tf-"]').forEach(b => b.classList.remove('active'));
  const id = tf === 'D' ? 'tf-daily' : tf === '15min' ? 'tf-15' : 'tf-5';
  document.getElementById(id).classList.add('active');
}

// ==================== FILTERS ====================
function filterType(type) {
  currentType = type;
  document.querySelectorAll('#f-all,#f-buy,#f-sell').forEach(b => {
    b.classList.remove('active', 'active-green', 'active-red');
  });
  if (type === 'all') document.getElementById('f-all').classList.add('active');
  else if (type === 'BUY') document.getElementById('f-buy').classList.add('active', 'active-green');
  else document.getElementById('f-sell').classList.add('active', 'active-red');
  applyFilters();
}

function filterStrategy(strat) {
  currentStrategy = strat;
  document.querySelectorAll('[id^="fs-"]').forEach(b => b.classList.remove('active'));
  const map = {
    'all': 'fs-all', 'range': 'fs-range',
    'Channel Consolidation Breakout': 'fs-ch', 'Early Breakout': 'fs-early',
    '52W High Support Buy': 'fs-high', 'Candlestick Pattern': 'fs-candle',
    'Volume Shocker': 'fs-vol',
    'Med Channel Breakout': 'fs-medch',
    'Watchlist Range Breakout': 'fs-wl',
    'Buy on Retracement': 'fs-retrace',
    'Trendline Channel Breakout': 'fs-chbrk',
    'Momentum Breakout': 'fs-momentum'
  };
  const el = document.getElementById(map[strat] || 'fs-all');
  if (el) el.classList.add('active');
  applyFilters();
  updateFilterInfo();
}

// Range breakout = one filter covering 9D / 15D / 21D / 60D
const STRATEGY_GROUPS = {
  'range': ['Range Breakout 9D', 'Range Breakout 15D', 'Range Breakout 21D', 'Range Breakout 60D']
};

function debounce(fn, ms) { let t; return function() { clearTimeout(t); t = setTimeout(fn, ms); }; }
const debouncedApplyFilters = debounce(applyFilters, 200);
function applyFilters() {
  const search = document.getElementById('search-input').value.toLowerCase().trim();
  filteredSignals = allSignals.filter(s => {
    if (currentType !== 'all' && s.signal_type !== currentType) return false;
    if (currentStrategy !== 'all') {
      const strats = s.strategies || [s.strategy];
      const group = STRATEGY_GROUPS[currentStrategy];
      if (group) {
        if (!strats.some(st => group.includes(st))) return false;
      } else if (!strats.includes(currentStrategy)) {
        return false;
      }
    }
    if (currentConfirmed && !s.confirmed) return false;
    if (search) {
      const name = (s.symbol_name || '').toLowerCase();
      const symbol = (s.symbol || '').toLowerCase();
      if (!name.includes(search) && !symbol.includes(search)) return false;
    }
    return true;
  });
  sortSignals();
  renderTable();
  updateCounts();
}

function toggleConfirmed() {
  currentConfirmed = !currentConfirmed;
  const btn = document.getElementById('fs-confirmed');
  if (btn) btn.classList.toggle('active', currentConfirmed);
  applyFilters();
  updateFilterInfo();
}

// ==================== THEMES (background / colour) ====================
const THEMES = {
  dark: {
    '--bg-primary': '#0a0e1a', '--bg-secondary': '#111827', '--bg-card': '#1a1f35',
    '--bg-card-hover': '#222842', '--bg-table-row': '#0f1424', '--bg-table-hover': '#1a2040',
    '--border': '#1e293b', '--border-light': '#2d3a52', '--thead-bg': '#151b30',
    '--text-primary': '#f1f5f9', '--text-secondary': '#94a3b8', '--text-muted': '#64748b',
    '--shadow': '0 4px 24px rgba(0,0,0,0.4)', '--shadow-lg': '0 8px 40px rgba(0,0,0,0.5)'
  },
  light: {
    '--bg-primary': '#eef2f7', '--bg-secondary': '#e2e8f0', '--bg-card': '#ffffff',
    '--bg-card-hover': '#f8fafc', '--bg-table-row': '#f8fafc', '--bg-table-hover': '#eef2f7',
    '--border': '#cbd5e1', '--border-light': '#94a3b8', '--thead-bg': '#e2e8f0',
    '--text-primary': '#0f172a', '--text-secondary': '#334155', '--text-muted': '#64748b',
    '--shadow': '0 4px 24px rgba(15,23,42,0.08)', '--shadow-lg': '0 8px 40px rgba(15,23,42,0.12)'
  },
  midnight: {
    '--bg-primary': '#05070d', '--bg-secondary': '#0b0f1a', '--bg-card': '#0f1524',
    '--bg-card-hover': '#141b2e', '--bg-table-row': '#0a0f1a', '--bg-table-hover': '#111827',
    '--border': '#1a2236', '--border-light': '#243149', '--thead-bg': '#0b101d',
    '--text-primary': '#e2e8f0', '--text-secondary': '#8b98ab', '--text-muted': '#5b6675',
    '--shadow': '0 4px 24px rgba(0,0,0,0.6)', '--shadow-lg': '0 8px 40px rgba(0,0,0,0.7)'
  }
};
const THEME_ORDER = ['dark', 'light', 'midnight'];

function setTheme(name) {
  const vars = THEMES[name] || THEMES['dark'];
  Object.keys(vars).forEach(k => document.documentElement.style.setProperty(k, vars[k]));
  try { localStorage.setItem('fyers_theme', name); } catch(e) {}
  const btn = document.getElementById('theme-btn');
  if (btn) btn.title = 'Theme: ' + name;
}

function cycleTheme() {
  const cur = (() => { try { return localStorage.getItem('fyers_theme'); } catch(e) { return null; } })() || 'dark';
  const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
  setTheme(next);
}

// ==================== TABS (Stocks / Indices) ====================
let activeTab = 'stocks';

function setupTabs() {
  const stocksTab = document.getElementById('stocks-tab');
  const indicesTab = document.getElementById('indices-tab');
  if (!stocksTab || !indicesTab) return;
  // Stock content into the Stocks tab (summary cards, toolbar, info, main table + big money)
  ['summary-cards', 'toolbar', 'filter-info'].forEach(id => {
    const el = document.getElementById(id);
    if (el) stocksTab.appendChild(el);
  });
  const mainDiv = document.querySelector('div.main');
  if (mainDiv) stocksTab.appendChild(mainDiv);
  // Index content into the Indices tab
  ['index-section', 'chartstrategy-section'].forEach(id => {
    const el = document.getElementById(id);
    if (el) indicesTab.appendChild(el);
  });
}

function switchTab(name) {
  activeTab = name;
  const tabs = ['stocks', 'indices', 'movers', 'backtest', 'sectors'];
  const ids = { stocks: 'stocks-tab', indices: 'indices-tab', movers: 'movers-tab', backtest: 'backtest-tab', sectors: 'sectors-tab' };
  const btns = { stocks: 'tab-stocks-btn', indices: 'tab-indices-btn', movers: 'tab-movers-btn', backtest: 'tab-backtest-btn', sectors: 'tab-sectors-btn' };
  tabs.forEach(t => {
    const el = document.getElementById(ids[t]); if (el) el.style.display = 'none';
    const btn = document.getElementById(btns[t]); if (btn) btn.classList.remove('active');
  });
  const el = document.getElementById(ids[name]); if (el) el.style.display = 'block';
  const btn = document.getElementById(btns[name]); if (btn) btn.classList.add('active');
  if (name === 'stocks') updateFilterInfo();
  else if (name === 'movers') fetchMovers();
  else if (name === 'backtest') loadBacktest();
  else if (name === 'indices') fetchChartStrategy();
  else if (name === 'sectors') loadSectors();
}

// ==================== STRATEGY NARRATION ====================
const STRATEGY_INFO = {
  'all': { t: 'All Strategies', d: 'Showing signals from every strategy. Click a strategy filter to focus on one setup.' },
  'range': { t: 'Range Breakout (9/15/21/60D)', d: 'BUY when the close breaks above the range high with a 3-day rising volume trend. 4 lookback periods catch different timing: 9D (fast), 15D (medium), 21D (medium-slow), 60D (major trend change). SL = 1.5 ATR below the range high. Target = +3.5% short-term.' },
  'Channel Consolidation Breakout': { t: 'Channel Consolidation Breakout', d: 'Price squeezed in a tight Bollinger Band (low volatility), then breaks out with volume and RSI confirmation. SL = middle band. Target = band width (~3.5%).' },
  'Early Breakout': { t: 'Early Breakout', d: 'Catches the move BEFORE the breakout: price near the range high + 2 consecutive higher closes + volume. Enters early, so SL is tighter (1 ATR below range high).' },
  '52W High Support Buy': { t: '52W High Support Buy', d: 'Stock within 7% of its 52-week high that pulled back to EMA20/50 support, with RSI > 40, price above EMA50, healthy volume. Target = 52W-high area / +3.5%.' },
  'Candlestick Pattern': { t: 'Candlestick Pattern', d: 'Reversal/continuation candles (Engulfing, Hammer, Morning Star, Marubozu...) near the range high/low. Low-confidence patterns are filtered out; SELLs only when strongly bearish (conf ≥ 0.7).' },
  'Volume Shocker': { t: 'Volume Shocker', d: 'Volume ≥ 2x average with a strong green candle near the day high, above EMA20. Big money participating. Target = +3.5%, SL below the day low.' },
  'Med Channel Breakout': { t: 'Med Channel Breakout', d: '30-day channel + squeeze + candlestick confirmation (needs pattern score ≥ 2). SL = 0.5 ATR below channel. Target = channel width, capped 3.5%.' },
  'Watchlist Range Breakout': { t: 'Watchlist Range Breakout', d: 'FRESH close above the recent 15-day high (yesterday at/below the high). Support = max(9 DMA, 21 DMA, swing low). SL = support. Target = swing high + 4.8%. Ignored if close falls below support.' },
  'Buy on Retracement': { t: 'Buy on Retracement', d: 'BUY the dip in an uptrend: price above EMA20/50 (rising), pulled back ≥ 2% from swing high, tapped EMA20 support, bullish reversal candle + RSI ≥ 45 + quiet volume. Target = swing high + 4.8%. Breakeven trail: SL → cost−1% once +1% in profit.' },
  'Trendline Channel Breakout': { t: 'Trendline Channel Breakout', d: 'Detects a parallel-line channel from swing highs/lows. ASCENDING (both lines up) → close above upper line = BUY. DESCENDING (both lines down) → close below lower line = SELL. HORIZONTAL (flat = square block) → above resistance = BUY / below support = SELL. SL = opposite channel line, target = channel height capped 3.5%.' },
  'Momentum Breakout': { t: '🚀 Momentum Breakout with Confirmation', d: 'Momentum rider: (1) Primary trend EMA20 vs EMA50, (2) strong prior move, (3) 3-5 bar tight consolidation, (4) breakout bar with ≥1.5x average volume, (5) price on the right side of VWAP. BUY ATM option. SL = below breakout candle low. TP1 = 1:2 R:R, TP2 = 1:3 R:R.' },
  'confirmed': { t: '✅ Confirmed Setups', d: 'ONLY signals passing ALL 3 confirmation rules: VOL (volume ≥ 1.5x avg), HOLD (close above stop, no weak close), PB (entry within 2.5 ATR of EMA20 — not chasing). Highest-quality entries.' },
  'bigmoney': { t: '💎 F&O Big Money', d: 'Unusual activity in stock options (indices excluded): OI jumps + volume bursts + premium moves. fresh_buying (OI↑ + premium↑) = big player expecting a move. Expiry day & day before are skipped.' },
  'regime': { t: '📈 Index Regime', d: 'Market state per index: BULLISH → buy CALL, BEARISH → buy PUT, RANGE → no directional / sell strangle. Confidence < 60 = wait. Run python -m scanner.regime to refresh.' }
};

function updateFilterInfo() {
  const el = document.getElementById('filter-info');
  if (!el) return;
  let info = null;
  if (bigMoneyView) {
    info = STRATEGY_INFO['bigmoney'];
  } else if (currentConfirmed) {
    info = STRATEGY_INFO['confirmed'];
  } else if (currentStrategy !== 'all') {
    info = STRATEGY_INFO[currentStrategy];
  }
  if (!info) { el.style.display = 'none'; return; }
  document.getElementById('filter-info-title').textContent = info.t;
  document.getElementById('filter-info-text').textContent = info.d;
  el.style.display = 'block';
}

function closeFilterInfo() {
  document.getElementById('filter-info').style.display = 'none';
}

// ==================== SORTING ====================
function sortBy(field) {
  if (sortField === field) {
    sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    sortField = field;
    sortDir = field === 'symbol_name' ? 'asc' : 'desc';
  }
  document.querySelectorAll('thead th').forEach(th => th.classList.remove('sorted-asc', 'sorted-desc'));
  const th = document.getElementById('th-' + (field === 'strategy_count' ? 'strength' : field === 'confidence' ? 'conf' : field === 'symbol_name' ? 'symbol' : field));
  if (th) th.classList.add(sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
  sortSignals();
  renderTable();
}

function sortSignals() {
  filteredSignals.sort((a, b) => {
    let va, vb;
    if (sortField === 'symbol_name') {
      va = a.symbol_name || '';
      vb = b.symbol_name || '';
      return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    }
    va = a[sortField] || 0;
    vb = b[sortField] || 0;
    return sortDir === 'asc' ? va - vb : vb - va;
  });
}

// ==================== RENDER ====================
function renderTable() {
  // Clear live-cell cache so repainted cells get fresh live prices
  _liveRendered = {};
  const tbody = document.getElementById('signals-body');
  const empty = document.getElementById('empty-state');

  if (filteredSignals.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    document.getElementById('result-count').textContent = '';
    return;
  }
  empty.style.display = 'none';
  document.getElementById('result-count').textContent = `Showing ${filteredSignals.length} of ${allSignals.length}`;

  tbody.innerHTML = filteredSignals.map((s, idx) => {
    const strats = s.strategies || [s.strategy || ''];
    const stratTags = strats.map(name => {
      const cls = stratTagClass(name);
      const shortName = name.replace('Range Breakout ', 'R').replace('Consolidation Breakout', 'Consolidation').replace(' Support Buy', '').replace(' Pattern', '');
      return `<span class="strat-tag ${cls}">${shortName}</span>`;
    }).join('');

    const rr = s.price && s.stop_loss && s.target ?
      ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '—';

    const badgeClass = strengthBadgeClass(s.strength || s.signal_type);
    const emoji = s.emoji || (s.signal_type === 'BUY' ? '🟢' : '🔴');
    const color = symbolColor(s.symbol_name || '');

    const confPct = ((s.confidence || 0) * 100).toFixed(0);
    const confColor = confPct >= 80 ? '#22c55e' : confPct >= 60 ? '#eab308' : '#f97316';

    return `
    <tr onclick="toggleExpand(${idx})" class="${selectedRow === idx ? 'selected' : ''}" data-idx="${idx}">
      <td>
        <div class="symbol-cell">
          <div class="symbol-icon" style="background:${color}">${(s.symbol_name||'?').substring(0,2)}</div>
          <div class="symbol-info">
            <span class="symbol-name">${s.symbol_name}${s.confirmed ? ' <span class="confirmed-badge" title="Confirmed: ' + (s.conf_rules || []).join(' + ') + '">✅</span>' : ''}</span>
            <span class="symbol-tag">${s.symbol}</span>
          </div>
          <button class="add-wl-btn ${_watchlistSymbols.has(s.symbol)?'added':''}" onclick="event.stopPropagation();toggleStockWatchlist('${s.symbol}','${s.symbol_name}')" title="Add to watchlist">${_watchlistSymbols.has(s.symbol)?'✓':'⭐'}</button>
        </div>
      </td>
      <td><span class="strength-badge ${badgeClass}">${emoji} ${s.strength || s.signal_type}</span></td>
      <td><div class="strat-tags">${stratTags}</div></td>
      <td>${qualityChip(s)}</td>
      <td class="price-cell" data-price="${s.symbol}" data-entry="${s.price}">₹${Number(s.price).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="sl-cell">₹${Number(s.stop_loss).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="target-cell">₹${Number(s.target).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="confidence-cell" style="color:${confColor}">${confPct}%</td>
      <td><span class="risk-reward">1:${rr}</span></td>
      <td style="font-size:11px;color:var(--text-muted);max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(s.reasons || [s.reason] || []).join(' | ')}">${(s.reasons || [s.reason] || []).join(' | ').substring(0, 60)}</td>
    </tr>`;
  }).join('');
}

let _lastToggleRow = null;
let _lastToggleTime = 0;
function buildExpandHTML(idx) {
  const s = filteredSignals[idx];
  if (!s) return '';
  const strats = s.strategies || [s.strategy || ''];
  const rr = s.price && s.stop_loss && s.target ?
    ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '—';
  const confPct = ((s.confidence || 0) * 100).toFixed(0);
  const confColor = confPct >= 80 ? '#22c55e' : confPct >= 60 ? '#eab308' : '#f97316';
  return `<tr class="expand-row open" id="expand-${idx}"><td colspan="10"><div class="expand-content"><div class="expand-grid">
    <div class="expand-item"><div class="expand-label">Current Price</div><div class="expand-value" data-live-price="${s.symbol}" data-live-entry="${s.price}" style="font-size:18px;font-weight:700;color:var(--text-primary)">₹${Number(s.price).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
    <div class="expand-item"><div class="expand-label">Stop Loss</div><div class="expand-value" style="color:var(--accent-red)">₹${Number(s.stop_loss).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
    <div class="expand-item"><div class="expand-label">Target</div><div class="expand-value" style="color:var(--accent-green)">₹${Number(s.target).toLocaleString('en-IN',{minimumFractionDigits:2})}</div></div>
    <div class="expand-item"><div class="expand-label">Risk:Reward</div><div class="expand-value" style="font-weight:700">1:${rr}</div></div>
    <div class="expand-item"><div class="expand-label">Confidence</div><div class="expand-value" style="color:${confColor};font-weight:700">${confPct}%</div></div>
    <div class="expand-item"><div class="expand-label">Timeframe</div><div class="expand-value">${s.timeframe}</div></div>
    <div class="expand-item"><div class="expand-label">Strategies (${strats.length})</div><div class="expand-value">${strats.join(', ')}</div></div>
  </div><div class="sparkline-container" id="chart-${idx}"><div class="sparkline-loading"><span class="spinner"></span> Loading 30-day chart...</div></div>
  <div class="expand-reasons"><div class="expand-reasons-title">📋 Signal Analysis</div>${(s.reasons||[s.reason]||[]).map(r=>`<div class="reason-item">${r}</div>`).join('')}</div></div></td></tr>`;
}
function toggleExpand(idx) {
  const now = Date.now();
  if (idx === _lastToggleRow && now - _lastToggleTime < 350) return;
  _lastToggleRow = idx; _lastToggleTime = now;
  document.querySelectorAll('.expand-row.open').forEach(r => r.remove());
  document.querySelectorAll('tbody tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
  const mainRow = document.querySelector(`tr[data-idx="${idx}"]`);
  if (selectedRow === idx) { selectedRow = null; return; }
  selectedRow = idx;
  if (mainRow) {
    mainRow.classList.add('selected');
    mainRow.insertAdjacentHTML('afterend', buildExpandHTML(idx));
    loadChart(idx, 30);
  }
}

// ==================== CHART (candlesticks + EMAs + S/R + signal levels) ====================
const _chartCache = {};
const _CHART_CACHE_MAX = 50;

// Simple LRU: insertion order of string keys = oldest first; evict beyond cap
function _cacheChart(key, val) {
  _chartCache[key] = val;
  const keys = Object.keys(_chartCache);
  if (keys.length > _CHART_CACHE_MAX) {
    const drop = keys.length - _CHART_CACHE_MAX;
    for (let i = 0; i < drop; i++) delete _chartCache[keys[i]];
  }
}

async function loadChart(idx, days = 30) {
  const s = filteredSignals[idx];
  if (!s) return;
  const container = document.getElementById('chart-' + idx);
  if (!container) return;
  container.style.position = 'relative';
  const key = s.symbol + '_' + days;
  if (_chartCache[key]) {
    drawChart(container, _chartCache[key], s, days, idx);
    return;
  }
  try {
    const resp = await fetch(`/api/history?symbol=${encodeURIComponent(s.symbol)}&days=${days}`);
    const data = await resp.json();
    if (data.candles && data.candles.length > 0) {
      _cacheChart(key, data);
      drawChart(container, data, s, days, idx);
    } else {
      container.innerHTML = '<div class="sparkline-loading">No chart data available</div>';
    }
  } catch(e) {
    container.innerHTML = '<div class="sparkline-loading">Chart load failed</div>';
  }
}

function chartRangeButtons(idx, days, isIndex) {
  const ranges = [[30,'1M'],[90,'3M'],[180,'6M'],[365,'1Y']];
  const loader = isIndex ? 'loadIndexChart' : 'loadChart';
  return '<div class="chart-range"><span class="chart-range-label">Range:</span>' +
    ranges.map(([d,label]) => `<button class="chart-range-btn ${d===days?'active':''}" onclick="event.stopPropagation();${loader}(${idx},${d})">${label}</button>`).join('') +
    '</div>';
}

function drawChart(container, data, signal, days, idx, isIndex) {
  const candles = data.candles || [];
  if (!candles.length) { container.innerHTML = '<div class="sparkline-loading">No chart data</div>'; return; }
  const ema9 = data.ema9 || [], ema21 = data.ema21 || [], ema50 = data.ema50 || [];
  const rsi = data.rsi || [];
  const support = data.support, resistance = data.resistance;
  const high52 = data.high_52w, low52 = data.low_52w;

  const W = 960, PADL = 56, PADR = 14, PADT = 18, PRICE_H = 280, VOL_H = 64, RSI_H = 70, XLABEL_H = 18;
  const H = PADT + PRICE_H + VOL_H + RSI_H + XLABEL_H;
  const chartW = W - PADL - PADR;
  const n = candles.length;
  const step = chartW / n;
  const bw = Math.max(2, step * 0.62);
  const x = i => PADL + i * step + step / 2;
  const yVolBase = PADT + PRICE_H;
  const yRsiBase = yVolBase + VOL_H;
  const rsiY = v => yRsiBase + (1 - v / 100) * RSI_H;

  const highs = candles.map(c => c.high), lows = candles.map(c => c.low);
  let minP = Math.min(...lows), maxP = Math.max(...highs);
  [ema9, ema21, ema50].forEach(a => a.forEach(v => {
    if (v != null && !isNaN(v)) { if (v < minP) minP = v; if (v > maxP) maxP = v; }
  }));
  [support, resistance, high52, low52, signal && signal.price, signal && signal.stop_loss, signal && signal.target]
    .forEach(v => { if (v != null && v > 0) { if (v < minP) minP = v; if (v > maxP) maxP = v; } });
  const pad = (maxP - minP) * 0.07 || maxP * 0.02;
  minP -= pad; maxP += pad;
  const range = maxP - minP || 1;
  const y = v => PADT + (1 - (v - minP) / range) * PRICE_H;

  const maxVol = Math.max(...candles.map(c => c.volume)) || 1;
  let o = '';

  // grid + y labels
  for (let i = 0; i <= 5; i++) {
    const val = minP + range * i / 5, yy = y(val);
    o += `<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="#1e293b" stroke-width="0.5" stroke-dasharray="3,3"/>`;
    o += `<text x="${PADL-6}" y="${yy+3}" fill="#64748b" font-size="9" text-anchor="end">${val.toFixed(0)}</text>`;
  }

  // volume bars
  candles.forEach((c, i) => {
    const bh = (c.volume / maxVol) * (VOL_H - 4);
    const col = c.close >= c.open ? 'rgba(34,197,94,0.4)' : 'rgba(239,68,68,0.4)';
    o += `<rect x="${x(i)-bw/2}" y="${yVolBase+VOL_H-2-bh}" width="${bw}" height="${bh}" fill="${col}" rx="1"/>`;
  });

  // EMA lines
  const emaPath = (arr, color) => {
    let p = '', started = false;
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (v == null || isNaN(v)) continue;
      p += (started ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1);
      started = true;
    }
    return started ? `<path d="${p}" fill="none" stroke="${color}" stroke-width="1.3" stroke-linejoin="round" opacity="0.9"/>` : '';
  };
  o += emaPath(ema9, '#06b6d4');
  o += emaPath(ema21, '#eab308');
  o += emaPath(ema50, '#a855f7');

  // RSI subpanel
  for (const lv of [70, 50, 30]) {
    const yy = rsiY(lv);
    o += `<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="#334155" stroke-width="0.5" stroke-dasharray="3,3"/>`;
    o += `<text x="${PADL-6}" y="${yy+3}" fill="#64748b" font-size="9" text-anchor="end">${lv}</text>`;
  }
  o += `<text x="${W-PADR-4}" y="${rsiY(86)}" fill="#64748b" font-size="9" text-anchor="end">RSI</text>`;
  let rsiP = '', rsiStart = false;
  for (let i = 0; i < rsi.length; i++) {
    const v = rsi[i];
    if (v == null || isNaN(v)) continue;
    rsiP += (rsiStart ? 'L' : 'M') + x(i).toFixed(1) + ',' + rsiY(Math.max(0, Math.min(100, v))).toFixed(1);
    rsiStart = true;
  }
  const rsiLast = rsi[rsi.length - 1];
  const rsiColor = (rsiLast == null || isNaN(rsiLast)) ? '#64748b' : (rsiLast >= 50 ? '#22c55e' : '#ef4444');
  if (rsiStart) o += `<path d="${rsiP}" fill="none" stroke="${rsiColor}" stroke-width="1.3" stroke-linejoin="round" opacity="0.9"/>`;
  const rsiLastVal = rsiLast != null && !isNaN(rsiLast) ? rsiLast.toFixed(1) : '—';
  o += `<text x="${PADL+4}" y="${rsiY(14)}" fill="${rsiColor}" font-size="10" font-weight="700">RSI ${rsiLastVal}</text>`;

  // support / resistance / signal levels
  const level = (val, color, label, dash) => {
    if (val == null || val <= 0 || isNaN(val)) return;
    const yy = y(val);
    o += `<line x1="${PADL}" y1="${yy}" x2="${W-PADR}" y2="${yy}" stroke="${color}" stroke-width="1.1" stroke-dasharray="${dash}" opacity="0.85"/>`;
    o += `<text x="${PADL+3}" y="${yy-3}" fill="${color}" font-size="9" font-weight="700">${label} ${Number(val).toFixed(2)}</text>`;
  };
  level(high52, '#64748b', '52W-H', '4,3');
  level(resistance, '#f97316', 'RES', '6,4');
  level(support, '#22c55e', 'SUP', '6,4');
  level(low52, '#64748b', '52W-L', '4,3');
  if (signal && signal.price) {
    level(signal.price, '#3b82f6', 'ENTRY', '2,3');
    level(signal.stop_loss, '#ef4444', 'SL', '6,3');
    level(signal.target, '#22c55e', 'TGT', '6,3');
  }

  // candlesticks
  candles.forEach((c, i) => {
    const bull = c.close >= c.open;
    const col = bull ? '#22c55e' : '#ef4444';
    o += `<line x1="${x(i)}" y1="${y(c.high)}" x2="${x(i)}" y2="${y(c.low)}" stroke="${col}" stroke-width="1"/>`;
    const by1 = y(Math.max(c.open, c.close)), by2 = y(Math.min(c.open, c.close));
    o += `<rect x="${x(i)-bw/2}" y="${by1}" width="${bw}" height="${Math.max(1, by2-by1)}" fill="${col}" rx="1"/>`;
  });

  // x date labels
  const stepIdx = Math.max(1, Math.floor(n / 7));
  for (let i = 0; i < n; i += stepIdx) {
    const d = new Date(candles[i].date * 1000);
    o += `<text x="${x(i)}" y="${H-4}" fill="#64748b" font-size="9" text-anchor="middle">${d.toLocaleDateString('en-IN',{day:'2-digit',month:'short'})}</text>`;
  }

  // last price marker
  const lastC = candles[n-1];
  const lastCol = lastC.close >= lastC.open ? '#22c55e' : '#ef4444';
  o += `<circle cx="${x(n-1)}" cy="${y(lastC.close)}" r="3.5" fill="${lastCol}" stroke="#0c1020" stroke-width="1.5"/>`;
  o += `<text x="${x(n-1)+6}" y="${y(lastC.close)+3}" fill="${lastCol}" font-size="10" font-weight="700">${lastC.close.toFixed(2)}</text>`;

  // stats
  const change = ((lastC.close - candles[0].close) / candles[0].close * 100);
  const chCls = change >= 0 ? 'up' : 'down';
  const chSign = change >= 0 ? '+' : '';
  const avgVol = (candles.reduce((a,c)=>a+c.volume,0)/n/100000).toFixed(1);
  const rr = (signal && signal.price && signal.stop_loss && signal.target) ?
    ((signal.target - signal.price)/Math.max(0.01, signal.price - signal.stop_loss)).toFixed(1) : '—';
  const nm = (signal && signal.symbol_name) || (data.symbol || '');
  const tipId = 'tip-' + (signal ? signal.symbol.replace(/[^A-Z0-9]/gi,'') : 'x') + '-' + days;

  container.innerHTML = `
    <div class="chart-head">
      <div>
        <span class="sparkline-title">📈 ${nm} — Daily</span>
        <div class="sparkline-stats">
          <span class="sparkline-stat">${days}D H/L: <strong>${Math.max(...highs).toFixed(2)} / ${Math.min(...lows).toFixed(2)}</strong></span>
          <span class="sparkline-stat">Change: <strong class="${chCls}">${chSign}${change.toFixed(2)}%</strong></span>
          <span class="sparkline-stat">Avg Vol: <strong>${avgVol}L</strong></span>
          <span class="sparkline-stat">R:R: <strong>1:${rr}</strong></span>
        </div>
      </div>
      ${chartRangeButtons(idx, days, isIndex)}
    </div>
    <div class="chart-legend">
      <span style="color:#06b6d4">— EMA9</span>
      <span style="color:#eab308">— EMA21</span>
      <span style="color:#a855f7">— EMA50</span>
      <span style="color:#f97316">RES ${resistance != null ? resistance : '—'}</span>
      <span style="color:#22c55e">SUP ${support != null ? support : '—'}</span>
      <span style="color:#3b82f6">ENTRY ${signal && signal.price ? signal.price : '—'}</span>
      <span style="color:#ef4444">SL ${signal && signal.stop_loss ? signal.stop_loss : '—'}</span>
      <span style="color:#22c55e">TGT ${signal && signal.target ? signal.target : '—'}</span>
    </div>
    <div class="chart-wrap" style="position:relative">
      <svg class="sparkline-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        ${o}
        <line id="${tipId}-vx" x1="0" y1="${PADT}" x2="0" y2="${yRsiBase+RSI_H}" stroke="#64748b" stroke-width="0.8" stroke-dasharray="3,3" opacity="0.65" style="display:none"/>
        <line id="${tipId}-hx" x1="${PADL}" y1="0" x2="${W-PADR}" y2="0" stroke="#64748b" stroke-width="0.8" stroke-dasharray="3,3" opacity="0.65" style="display:none"/>
      </svg>
      <div class="chart-tooltip" id="${tipId}" style="display:none"></div>
    </div>`;

  // interactive hover tooltip + crosshair
  const wrap = container.querySelector('.chart-wrap');
  const svg = wrap.querySelector('svg');
  const vx = wrap.querySelector('#' + tipId + '-vx');
  const hx = wrap.querySelector('#' + tipId + '-hx');
  svg.addEventListener('mousemove', e => {
    const rect = svg.getBoundingClientRect();
    const scaleX = W / rect.width;
    const scaleY = H / rect.height;
    const px = (e.clientX - rect.left) * scaleX;
    const py = (e.clientY - rect.top) * scaleY;
    let cidx = Math.floor((px - PADL) / step);
    if (cidx < 0) cidx = 0;
    if (cidx >= n) cidx = n - 1;
    const c = candles[cidx];
    const d = new Date(c.date * 1000);
    const t = document.getElementById(tipId);
    if (!t) return;

    // Crosshair lines
    const cvx = x(cidx);
    if (vx) { vx.setAttribute('x1', cvx); vx.setAttribute('x2', cvx); vx.style.display = 'block'; }
    if (hx) { hx.setAttribute('y1', py); hx.setAttribute('y2', py); hx.style.display = 'block'; }

    // Candle details
    const prevC = candles[cidx - 1];
    let chg = '';
    if (prevC) {
      const p = ((c.close - prevC.close) / prevC.close) * 100;
      chg = ` | Δ <span style="color:${p>=0?'#22c55e':'#ef4444'}">${p>=0?'+':''}${p.toFixed(2)}%</span>`;
    }
    const emaVals = [['E9', ema9[cidx]], ['E21', ema21[cidx]], ['E50', ema50[cidx]]]
      .map(([k, v]) => v != null && !isNaN(v) ? `${k}:${v.toFixed(0)}` : '').filter(Boolean).join(' ');
    const rsiV = rsi[cidx];
    const rsiStr = rsiV != null && !isNaN(rsiV) ? ` | RSI ${rsiV.toFixed(1)}` : '';
    t.innerHTML = `<b>${d.toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'})}</b> ${c.close>=c.open?'▲':'▼'}<br>` +
      `O:${c.open.toFixed(2)} H:${c.high.toFixed(2)} L:${c.low.toFixed(2)} C:${c.close.toFixed(2)}<br>` +
      `V:${(c.volume/100000).toFixed(1)}L ${chg}<br>${emaVals}${rsiStr}`;

    const tRect = t.getBoundingClientRect();
    const svgRect = svg.getBoundingClientRect();
    const tipX = (cvx / W) * svgRect.width;
    const tipY = (py / H) * svgRect.height;
    t.style.display = 'block';
    // Flip tooltip near right/bottom edges so it stays on screen
    t.style.left = Math.min(tipX + 10, Math.max(0, svgRect.width - tRect.width - 4)) + 'px';
    t.style.top = Math.min(tipY + 10, Math.max(0, svgRect.height - tRect.height - 4)) + 'px';
  });
  svg.addEventListener('mouseleave', () => {
    const t = document.getElementById(tipId);
    if (t) t.style.display = 'none';
    if (vx) vx.style.display = 'none';
    if (hx) hx.style.display = 'none';
  });
}

// ==================== SUMMARY CARDS ====================
function updateSummaryCards() {
  const total = allSignals.length;
  const buy = allSignals.filter(s => s.signal_type === 'BUY').length;
  const sell = allSignals.filter(s => s.signal_type === 'SELL').length;
  const veryStrong = allSignals.filter(s => (s.strength || '').includes('VERY STRONG')).length;
  const breakout = allSignals.filter(s => {
    const strats = s.strategies || [];
    return strats.some(st => st.includes('Range') || st.includes('Channel') || st.includes('Momentum'));
  }).length;
  const early = allSignals.filter(s => {
    const strats = s.strategies || [];
    return strats.some(st => st.includes('Early') || st.includes('52W'));
  }).length;

  const retrace = allSignals.filter(s => {
    const strats = s.strategies || [];
    return strats.some(st => st.includes('Retracement'));
  }).length;

  document.getElementById('card-total').textContent = total;
  document.getElementById('card-buy').textContent = buy;
  document.getElementById('card-sell').textContent = sell;
  document.getElementById('card-strong').textContent = veryStrong;
  document.getElementById('card-range').textContent = breakout;
  document.getElementById('card-early').textContent = early;
  document.getElementById('card-retrace').textContent = retrace;
}

function updateCounts() {
  document.getElementById('signal-count').textContent = allSignals.length;
  document.getElementById('card-total').textContent = allSignals.length;
}

// ==================== SCAN ====================
async function triggerScan() {
  const btn = document.getElementById('scan-btn');
  btn.innerHTML = '<span class="spinner"></span> Scanning...';
  btn.disabled = true;
  document.getElementById('status-text').innerHTML = '<span class="spinner"></span> Scanning all stocks...';
  document.getElementById('status-dot').className = 'pulse-dot yellow';
  document.getElementById('progress-bar').classList.add('active');

  await fetch('/api/scan?timeframe=' + currentTimeframe);

  // Poll until scan complete
  let attempts = 0;
  const poll = setInterval(async () => {
    const resp = await fetch('/api/status');
    const st = await resp.json();
    if (!st.scanning || attempts > 120) {
      clearInterval(poll);
      await fetchSignals();
      btn.innerHTML = '🔍 Scan Now';
      btn.disabled = false;
      document.getElementById('progress-bar').classList.remove('active');
      showToast('Scan complete! Found ' + allSignals.length + ' signals');
    }
    attempts++;
  }, 2000);
}

let _auxFetchLast = 0;
function applySignalsData(data) {
  allSignals = data.signals || [];
  allIndexSignals = data.index_signals || [];
  document.getElementById('last-scan').textContent = data.last_scan ? new Date(data.last_scan).toLocaleTimeString() : 'Never';
  if (!data.scanning) {
    document.getElementById('status-text').textContent = 'Ready';
    document.getElementById('status-dot').className = 'pulse-dot green';
  }
  // Only re-render if the data actually changed — otherwise keep expanded
  // rows (charts / big-money details) open so nothing auto-closes.
  const sig = JSON.stringify(allSignals.map(s => s.symbol + s.signal_type + s.price + s.target))
    + '|' + JSON.stringify(allIndexSignals.map(s => s.symbol + s.signal_type + s.price + s.target));
  if (sig !== _lastSignalsSig) {
    _lastSignalsSig = sig;
    applyFilters();
    updateSummaryCards();
    renderIndexTable();
  }
  // Auxiliary data (quality / big-money / index strategy) is slow to change —
  // fetch it at most once every 10s instead of on every signal event.
  const now = Date.now();
  if (now - _auxFetchLast > 10000) {
    _auxFetchLast = now;
    Promise.allSettled([fetchQuality(), fetchBigMoney(), fetchChartStrategy()]);
  }
}

async function fetchSignals() {
  const resp = await fetch('/api/signals');
  const data = await resp.json();
  applySignalsData(data);
}

// ==================== SIGNAL QUALITY ====================
async function fetchQuality() {
  try {
    const resp = await fetch('/api/quality');
    const data = await resp.json();
    qualityStats = data.strategies || {};
  } catch(e) {}
}

// ==================== INDEX CHART STRATEGY ====================
let _lastIcsSig = '';
// ==================== SECTORS ====================
let _sectorsData = null;

async function loadSectors() {
  try {
    const resp = await fetch('/api/signal-groups');
    const data = await resp.json();
    if (data.error) return;
    _sectorsData = data;
    renderSectors(data);
  } catch(e) {}
}

function renderSectors(data) {
  const grid = document.getElementById('sectors-grid');
  const empty = document.getElementById('sectors-empty');
  const status = document.getElementById('sectors-status');
  const summary = document.getElementById('sectors-summary');
  if (!grid) return;
  const sectors = data.sectors || [];
  if (sectors.length === 0) { grid.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  status.textContent = new Date().toLocaleTimeString();

  // Summary
  const total = sectors.reduce((s, g) => s + g.count, 0);
  summary.innerHTML = (data.summary || []).map(s =>
    `<span class="summary-pill"><span class="sp-emoji">${s.emoji}</span>${s.text}</span>`
  ).join('');

  // Grid
  grid.innerHTML = sectors.map(g => {
    const stocks = (g.signals || []).slice(0, 8).map(s => {
      const cls = s.signal_type === 'BUY' ? 'buy' : 'sell';
      const score = s.quality_score ? ` <span style="font-size:10px;color:${s.quality_score>=60?'#22c55e':s.quality_score>=40?'#eab308':'#f97316'}">${s.quality_score}</span>` : '';
      return `<span class="gc-stock ${cls}" title="${s.symbol_name} - ${s.strategy || ''}${score}">${s.symbol_name || s.symbol}${score}</span>`;
    }).join('');
    const more = g.count > 8 ? `<span class="gc-stock">+${g.count - 8} more</span>` : '';
    const buys = (g.signals || []).filter(s => s.signal_type === 'BUY').length;
    const sells = g.count - buys;
    return `<div class="group-card">
      <div class="gc-header">
        <span class="gc-emoji">${g.emoji}</span>
        <span class="gc-name">${g.group_name}</span>
        <span class="gc-count">${g.count} <span style="font-size:10px;font-weight:400;opacity:0.7">(${buys}B/${sells}S)</span></span>
      </div>
      <div class="gc-stocks">${stocks}${more}</div>
    </div>`;
  }).join('');
}

async function fetchChartStrategy() {
  try {
    const resp = await fetch('/api/chartstrategy');
    const data = await resp.json();
    const sig = JSON.stringify((data.indices || []).map(i => i.name + i.regime + i.regime_confidence + (i.entry ? i.entry.type : '')));
    if (sig !== _lastIcsSig) {
      _lastIcsSig = sig;
      renderChartStrategy(data.indices || []);
    }
  } catch(e) {}
}

let icsRegimeFilter = 'all';
let icsEntryFilter = 'all';
function filterIcs(kind, value) {
  if (kind === 'regime') icsRegimeFilter = value;
  else icsEntryFilter = value;
  document.querySelectorAll('#chartstrategy-filters .filter-btn').forEach(b => b.classList.remove('active'));
  const id = kind === 'regime' ? 'icf-r-' + value : 'icf-e-' + value;
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
  renderChartStrategy(window._icsIndices || []);
}

function renderChartStrategy(indices) {
  const section = document.getElementById('chartstrategy-section');
  const tbody = document.getElementById('chartstrategy-body');
  const empty = document.getElementById('chartstrategy-empty');
  document.getElementById('chartstrategy-count').textContent = `(${indices.length})`;
  if (!indices.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  empty.style.display = 'none';
  window._icsIndices = indices;

  const filtered = indices.filter(i => {
    if (icsRegimeFilter !== 'all' && i.regime !== icsRegimeFilter) return false;
    const etype = i.entry ? i.entry.type : 'WAIT';
    if (icsEntryFilter !== 'all' && etype !== icsEntryFilter) return false;
    return true;
  });
  window._icsFiltered = filtered;

  tbody.innerHTML = filtered.map((i, idx) => {
    const e = i.entry;
    const plan = i.plan;
    const drv = i.drivers || {};
    const bull = i.regime === 'BULLISH';
    const bear = i.regime === 'BEARISH';
    const rng = i.regime === 'RANGE';
    const rc = bull ? '#22c55e' : bear ? '#ef4444' : '#facc15';
    const regimeTag = bull ? '🟢 BULLISH' : bear ? '🔴 BEARISH' : '🟡 RANGE';
    const hasTrade = !!plan;
    const etype = e ? e.type : '—';
    const etColor = etype === 'BREAKOUT' || etype === 'BREAKDOWN' ? '#22c55e' : etype === 'PULLBACK' ? '#38bdf8' : etype === 'CANDLE' ? '#eab308' : '#94a3b8';
    const tradeCls = hasTrade ? (plan.instrument.includes('CALL') ? 'up' : 'down') : 'muted';
    const chartId = 'ics-chart-' + i.name.replace(/[^A-Z0-9]/gi, '');
    return `
    <tr onclick="toggleIcsExpand(${idx})" data-idx="ics-${idx}" style="cursor:pointer">
      <td>
        <div class="symbol-cell">
          <div class="symbol-icon" style="background:${symbolColor(i.name)}">${i.name.substring(0, 2)}</div>
          <div class="symbol-info">
            <span class="symbol-name">${i.name}</span>
            <span class="symbol-tag">Spot ${Number(i.spot).toLocaleString('en-IN', {maximumFractionDigits:1})}</span>
          </div>
        </div>
      </td>
      <td><span style="color:${rc};font-weight:700">${regimeTag}</span></td>
      <td>${i.regime_confidence}%</td>
      <td>${drv.adx != null ? drv.adx : '—'}</td>
      <td>${drv.rsi != null ? drv.rsi : '—'}</td>
      <td>${drv.pos_in_range_pct != null ? drv.pos_in_range_pct + '%' : '—'}</td>
      <td><span class="ics-entry-type" style="border-color:${etColor};color:${etColor}">${etype}</span></td>
      <td>${e ? e.score : '—'}</td>
      <td style="font-size:11px;color:var(--text-secondary)">${hasTrade ? `<b class="${tradeCls}">${plan.instrument}</b> · E ${Number(plan.entry).toLocaleString('en-IN', {maximumFractionDigits:1})} · SL ${Number(plan.stop).toLocaleString('en-IN', {maximumFractionDigits:1})} · TGT ${Number(plan.target).toLocaleString('en-IN', {maximumFractionDigits:1})}` : '<b class="muted">WAIT</b> — no qualifying entry'}</td>
    </tr>
    <tr class="expand-row" id="ics-expand-${idx}">
      <td colspan="9">
        <div class="expand-content">
          <div id="${chartId}" class="ics-chart"></div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

let _lastIcsToggle = null;
let _lastIcsTime = 0;
function toggleIcsExpand(idx) {
  const now = Date.now();
  if (idx === _lastIcsToggle && now - _lastIcsTime < 350) return;
  _lastIcsToggle = idx;
  _lastIcsTime = now;
  const row = document.getElementById('ics-expand-' + idx);
  if (!row) return;
  document.querySelectorAll('#chartstrategy-body .expand-row').forEach(r => { if (r.id !== row.id) r.classList.remove('open'); });
  document.querySelectorAll('#chartstrategy-body tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
  row.classList.toggle('open');
  if (row.classList.contains('open')) {
    document.querySelector(`#chartstrategy-body tr[data-idx="ics-${idx}"]`).classList.add('selected');
    loadIcsChart(idx);
  }
}

async function loadIcsChart(idx) {
  const filtered = window._icsFiltered || window._icsIndices || [];
  const i = filtered[idx];
  const el = document.getElementById('ics-chart-' + i.name.replace(/[^A-Z0-9]/gi, ''));
  if (!el) return;
  el.innerHTML = '<div class="sparkline-loading">Loading 15-min chart...</div>';
  try {
    const resp = await fetch(`/api/history?symbol=${encodeURIComponent(i.symbol)}&days=5&resolution=15`);
    const data = await resp.json();
    if (data.candles && data.candles.length) {
      drawChart(el, data, null, 5, 0, true);
    } else {
      el.innerHTML = '<div class="sparkline-loading">No 15-min data</div>';
    }
  } catch(e) {
    el.innerHTML = '<div class="sparkline-loading">Chart load failed</div>';
  }
}

// ==================== INDEX REGIME ====================
// ==================== BIG MONEY (unusual stock options) ====================
let bigMoneyView = false;
function toggleBigMoney() {
  bigMoneyView = !bigMoneyView;
  const bm = document.getElementById('bigmoney-section');
  const main = document.getElementById('signals-table-container');
  const idx = document.getElementById('index-section');
  const cards = document.getElementById('summary-cards');
  const btn = document.getElementById('bm-toggle');
  if (bigMoneyView) {
    bm.style.display = 'block';
    main.style.display = 'none';
    idx.style.display = 'none';
    cards.style.display = 'none';
    btn.classList.add('active');
    fetchBigMoney();
    updateFilterInfo();
  } else {
    bm.style.display = 'none';
    main.style.display = '';
    cards.style.display = '';
    idx.style.display = allIndexSignals.length ? '' : 'none';
    btn.classList.remove('active');
    updateFilterInfo();
  }
}
async function fetchBigMoney() {
  try {
    const resp = await fetch('/api/bigmoney');
    const data = await resp.json();
    const sig = JSON.stringify((data.signals || []).map(s => s.symbol + s.strike + s.score));
    if (sig !== _lastBmSig) {
      _lastBmSig = sig;
      renderBigMoney(data.signals || []);
    }
    fetchPunchHistory();
  } catch(e) {}
}

// ==================== RECENT PUNCHES (persistent tracker) ====================
let _lastPunchSig = '';
async function fetchPunchHistory() {
  try {
    const resp = await fetch('/api/bigmoney/history');
    const data = await resp.json();
    const punches = data.punches || [];
    const sig = JSON.stringify(punches.map(p => p.symbol + p.strike + p.option_type + p.timestamp));
    if (sig !== _lastPunchSig) {
      _lastPunchSig = sig;
      renderPunchHistory(punches);
    }
  } catch(e) {}
}

function renderPunchHistory(punches) {
  const section = document.getElementById('bigmoney-history');
  const tbody = document.getElementById('bmh-body');
  const empty = document.getElementById('bmh-empty');
  if (!tbody) return;
  document.getElementById('bmh-count').textContent = `(${punches.length})`;
  if (!punches.length) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = 'block';
    return;
  }
  if (empty) empty.style.display = 'none';
  tbody.innerHTML = punches.slice(0, 100).map(p => {
    const ts = p.timestamp ? new Date(p.timestamp).toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
    const bull = p.signal_type === 'BULLISH';
    const bear = p.signal_type === 'BEARISH';
    const dirColor = bull ? '#22c55e' : bear ? '#ef4444' : '#94a3b8';
    const mn = p.moneyness_pct != null ? p.moneyness_pct + '%' : '—';
    const sp = p.confirmed_single_print ? '✅' : '—';
    return `<tr>
      <td style="white-space:nowrap;font-size:12px;color:var(--text-muted)">${ts}</td>
      <td style="font-weight:700">${p.symbol_name || p.symbol}</td>
      <td>${p.strike}</td>
      <td>${p.option_type}</td>
      <td style="color:${dirColor};font-weight:700">${p.signal_type || ''}</td>
      <td style="font-weight:700">${p.vol_lots ?? p.lots ?? '—'}</td>
      <td style="text-align:center">${sp}</td>
      <td style="color:${p.moneyness_pct >= 0 ? '#22c55e' : '#ef4444'}">${mn}</td>
      <td>${p.premium_change_pct >= 0 ? '+' : ''}${p.premium_change_pct}%</td>
    </tr>`;
  }).join('');
}

async function triggerBigMoneyScan() {
  const btn = document.getElementById('bm-scan-btn');
  if (btn) { btn.textContent = '⏳ Scanning...'; btn.disabled = true; }
  try {
    const resp = await fetch('/api/bigmoney/scan');
    const data = await resp.json();
    if (data.status === 'started') {
      showToast('Big money scan started — scanning 100 F&O stocks...');
      // Poll for results every 10s for up to 5 minutes
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        if (attempts > 30) { clearInterval(poll); if (btn) { btn.textContent = '🔍 Scan Now'; btn.disabled = false; } return; }
        try {
          const r = await fetch('/api/bigmoney');
          const d = await r.json();
          if (d.signals && d.signals.length > 0) {
            renderBigMoney(d.signals);
            showToast(`Big money scan complete — ${d.signals.length} signals found`);
            clearInterval(poll);
            if (btn) { btn.textContent = '🔍 Scan Now'; btn.disabled = false; }
          }
        } catch(e) {}
      }, 10000);
    } else if (data.error) {
      showToast('Error: ' + data.error);
      if (btn) { btn.textContent = '🔍 Scan Now'; btn.disabled = false; }
    }
  } catch(e) {
    showToast('Big money scan failed');
    if (btn) { btn.textContent = '🔍 Scan Now'; btn.disabled = false; }
  }
}

function bmNarration(s) {
  const level = s.score >= 80 ? 'Very unusual — likely a large / institutional order'
    : s.score >= 65 ? 'Unusual — notable big-player positioning'
    : 'Mildly unusual — above normal activity';
  const actNarr = {
    'fresh_buying': 'Big player OPENED new positions (OI up) and premium rose — expecting a move in this direction. Strongest setup.',
    'fresh_writing': 'Big player SOLD / wrote options (OI up, premium down) — supply pressure / collecting premium. Avoid chasing; often bearish or range-bound.',
    'short_covering': 'Shorts buying back positions (OI down, premium up) — mild bullish.',
    'long_unwinding': 'Longs selling out (OI down, premium down) — mild bearish.',
    'mixed': 'Mixed signals — no clear direction.'
  }[s.activity] || '';
  return `${level}. ${actNarr} Direction: ${s.signal_type}. Score ${s.score}/100.`;
}

function renderBigMoney(signals) {
  const tbody = document.getElementById('bigmoney-body');
  const empty = document.getElementById('bigmoney-empty');
  document.getElementById('bigmoney-count').textContent = `(${signals.length} strikes)`;
  empty.style.display = signals.length ? 'none' : 'block';

  // Group by stock — ONE ROW per share
  const groups = {};
  signals.forEach(s => {
    const g = (groups[s.symbol_name] = groups[s.symbol_name] || []);
    g.push(s);
  });

  const stockRows = Object.keys(groups).map(name => {
    const sigs = groups[name];
    const best = sigs.reduce((a, b) => (b.score > a.score ? b : a));
    // ATM strike for this stock (from signal details, else median strike)
    const atm = (best.details && best.details.atm_strike) || median(sigs.map(s => s.strike));
    // Pick up to 3 CONSECUTIVE strikes closest to ATM, plus keep any OTHERS
    const uniqueStrikes = [...new Set(sigs.map(s => s.strike))].sort((a, b) => a - b);
    const sortedByDist = uniqueStrikes
      .map(st => ({ st, d: Math.abs(st - atm) }))
      .sort((a, b) => a.d - b.d);
    const nearStrikes = sortedByDist.slice(0, 3).map(x => x.st).sort((a, b) => a - b);
    const otherStrikes = sortedByDist.slice(3).map(x => x.st).sort((a, b) => a - b);
    const shown = sigs.filter(s => nearStrikes.includes(s.strike));
    const shownOthers = sigs.filter(s => otherStrikes.includes(s.strike));

    const bull = best.signal_type === 'BULLISH';
    const bear = best.signal_type === 'BEARISH';
    const sigColor = bull ? '#22c55e' : bear ? '#ef4444' : '#facc15';
    const sigTag = bull ? '🟢 ' + best.signal_type : bear ? '🔴 ' + best.signal_type : '⚖️ ' + best.signal_type;
    const scoreCls = best.score >= 80 ? 'q-good' : best.score >= 65 ? 'q-mid' : 'q-bad';
    const single = best.mode === 'single_order' ? '⚡' : best.mode === '15min_burst' ? '⏱15m' : 'daily';
    const id = 'bm-exp-' + name.replace(/[^A-Z0-9]/gi, '');

    const strikeRowHtml = s => {
      const b2 = s.signal_type === 'BULLISH';
      const c2 = b2 ? '#22c55e' : s.signal_type === 'BEARISH' ? '#ef4444' : '#facc15';
      const oiCls2 = (s.oi_change || 0) >= 0 ? 'up' : 'down';
      const pct = s.vol_delta_ratio ? (s.vol_delta_ratio * 100).toFixed(0) + '%' : (s.oi_change_pct >= 0 ? '+' : '') + (s.oi_change_pct || 0).toFixed(1) + '%';
      return `
      <tr>
        <td class="price-cell">${Number(s.strike).toLocaleString('en-IN')}</td>
        <td><span class="strength-badge ${s.option_type === 'CE' ? 'card-buy' : 'card-sell'}">${s.option_type}</span></td>
        <td><span style="color:${c2};font-weight:700;cursor:help" title="${bmNarration(s)}">${b2 ? '🟢 ' : s.signal_type === 'BEARISH' ? '🔴 ' : '⚖️ '}${s.signal_type}</span></td>
        <td style="font-size:12px;color:var(--text-secondary);cursor:help" title="${bmNarration(s)}">${s.activity.replace(/_/g, ' ')}</td>
        <td><span class="quality-chip ${s.score >= 80 ? 'q-good' : s.score >= 65 ? 'q-mid' : 'q-bad'}" style="cursor:help" title="${bmNarration(s)}">${s.score}</span></td>
        <td class="${oiCls2}">${s.oi_change >= 0 ? '+' : ''}${Number(s.oi_change || s.vol_delta || 0).toLocaleString('en-IN')}</td>
        <td class="${oiCls2}">${pct}</td>
        <td class="${s.premium_change_pct >= 0 ? 'up' : 'down'}">${s.premium_change_pct >= 0 ? '+' : ''}${s.premium_change_pct.toFixed(1)}%</td>
      </tr>`;
    };
    const strikeRows = shown.map(strikeRowHtml).join('');
    const otherRows = shownOthers.map(strikeRowHtml).join('');
    const otherSection = otherRows ? `
      <div style="font-size:12px;font-weight:700;color:var(--accent-orange);margin:14px 0 8px">Other unusual strikes (beyond nearest 3): ${otherStrikes.map(x => Number(x).toLocaleString('en-IN')).join(' / ')}</div>
      <div class="table-container">
        <table>
          <thead><tr>
            <th>Strike</th><th>Type</th><th>Signal</th><th>Activity</th><th>Score</th><th>OI Δ</th><th>OI %</th><th>Premium %</th>
          </tr></thead>
          <tbody>${otherRows}</tbody>
        </table>
      </div>` : '';

    return `
    <tr class="bm-stock-row" data-bm="${name}" onclick="toggleBmExpand('${name}')">
      <td colspan="6">
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;width:100%">
          <div class="symbol-icon" style="background:#8b5cf6">${name.substring(0, 2)}</div>
          <div class="symbol-info" style="min-width:110px">
            <span class="symbol-name">${name}</span>
            <span class="symbol-tag">${sigs.length} strike(s)</span>
          </div>
          <span style="color:${sigColor};font-weight:700;cursor:help" title="${bmNarration(best)}">${sigTag}</span>
          <span style="font-size:11px;color:var(--text-muted);cursor:help" title="${bmNarration(best)}">${best.activity.replace(/_/g, ' ')}</span>
          <span class="quality-chip ${scoreCls}" style="cursor:help" title="${bmNarration(best)}">${best.score}</span>
          <span style="font-size:11px;color:var(--text-muted)">${single}</span>
          <span style="font-size:11px;color:var(--text-secondary)">ATM ${Number(atm).toLocaleString('en-IN')} · ${nearStrikes.map(x => Number(x).toLocaleString('en-IN')).join(' / ')}${otherStrikes.length ? ' +' + otherStrikes.length + ' more' : ''}</span>
        </div>
      </td>
    </tr>
    <tr class="bm-expand-row" id="${id}">
      <td colspan="6">
        <div class="expand-content">
          <div style="font-size:12px;font-weight:700;color:var(--text-secondary);margin-bottom:8px">${name} — nearest ${nearStrikes.length} strikes to ATM</div>
          <div class="table-container">
            <table>
              <thead><tr>
                <th>Strike</th><th>Type</th><th>Signal</th><th>Activity</th><th>Score</th><th>OI Δ</th><th>OI %</th><th>Premium %</th>
              </tr></thead>
              <tbody>${strikeRows}</tbody>
            </table>
          </div>
          ${otherSection}
        </div>
      </td>
    </tr>`;
  }).join('');

  tbody.innerHTML = stockRows;
}

function median(arr) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

let _lastBmRow = null;
let _lastBmTime = 0;
function toggleBmExpand(name) {
  const now = Date.now();
  if (name === _lastBmRow && now - _lastBmTime < 350) return;
  _lastBmRow = name;
  _lastBmTime = now;
  const id = 'bm-exp-' + name.replace(/[^A-Z0-9]/gi, '');
  const row = document.getElementById(id);
  if (!row) return;
  document.querySelectorAll('.bm-expand-row').forEach(r => { if (r.id !== id) r.classList.remove('open'); });
  row.classList.toggle('open');
}

// ==================== F&O MOVERS ====================
let _moversSig = '';
let _moversModalData = null;

// ==================== BACKTEST ====================
let _btRunning = false;

async function runBacktest() {
  if (_btRunning) return;
  _btRunning = true;
  const btn = document.getElementById('bt-run-btn');
  btn.innerHTML = '⏳ Running...';
  btn.disabled = true;
  document.getElementById('bt-status').innerHTML = '🚀 Running backtest on ' + document.getElementById('bt-max-symbols').value + ' stocks (252 days)... This takes 2-5 minutes.';
  document.getElementById('bt-empty').style.display = 'none';

  const maxSym = document.getElementById('bt-max-symbols').value;
  const hold = document.getElementById('bt-hold-days').value;
  await fetch('/api/backtest?action=run&max=' + maxSym + '&hold=' + hold);

  // Poll until done
  let attempts = 0;
  const poll = setInterval(async () => {
    const resp = await fetch('/api/backtest?action=status');
    const st = await resp.json();
    attempts++;
    if (!st.running || attempts > 120) {
      clearInterval(poll);
      _btRunning = false;
      btn.innerHTML = '▶ Run Backtest';
      btn.disabled = false;
      loadBacktest();
    }
  }, 3000);
}

async function loadBacktest() {
  try {
    const resp = await fetch('/api/backtest?action=load');
    const data = await resp.json();
    if (!data.strategies || Object.keys(data.strategies).length === 0) {
      document.getElementById('bt-empty').style.display = 'block';
      document.getElementById('bt-status').innerHTML = 'No results yet. Click "Run Backtest" to start.';
      return;
    }
    document.getElementById('bt-empty').style.display = 'none';
    document.getElementById('bt-status').innerHTML = 'Generated: ' + new Date(data.generated).toLocaleString();
    renderBacktest(data.strategies);
  } catch(e) {}
}

function renderBacktest(strats) {
  const entries = Object.entries(strats)
    .filter(([_, r]) => r.total_trades > 0)
    .sort((a, b) => {
      const gradeOrder = {'A++':12,'A+':11,'A':10,'A-':9,'B+':8,'B':7,'B-':6,'C+':5,'C':4,'C-':3,'D+':2,'D':1,'F':0};
      return (gradeOrder[b[1].grade]||0) - (gradeOrder[a[1].grade]||0);
    });

  // Summary cards
  const totalTrades = entries.reduce((s,[_,r]) => s + r.total_trades, 0);
  const avgWR = entries.reduce((s,[_,r]) => s + r.win_rate, 0) / entries.length;
  const avgPF = entries.reduce((s,[_,r]) => s + r.profit_factor, 0) / entries.length;
  const profitable = entries.filter(([_,r]) => r.total_pnl_pct > 0).length;
  document.getElementById('bt-summary').innerHTML = `
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Strategies</div><div style="font-size:28px;font-weight:800;margin-top:4px">${entries.length}</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Total Trades</div><div style="font-size:28px;font-weight:800;margin-top:4px">${totalTrades}</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Avg Win Rate</div><div style="font-size:28px;font-weight:800;margin-top:4px;color:${avgWR>=45?'#22c55e':avgWR>=35?'#eab308':'#f97316'}">${avgWR.toFixed(1)}%</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Avg PF</div><div style="font-size:28px;font-weight:800;margin-top:4px;color:${avgPF>=1.5?'#22c55e':avgPF>=1.0?'#eab308':'#f97316'}">${avgPF.toFixed(2)}</div></div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center"><div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px">Profitable</div><div style="font-size:28px;font-weight:800;margin-top:4px;color:${profitable>entries.length/2?'#22c55e':'#f97316'}">${profitable}/${entries.length}</div></div>
  `;

  // Table rows
  const tbody = document.getElementById('bt-body');
  tbody.innerHTML = entries.map(([name, r]) => {
    const gradeColor = {'A++':'#22c55e','A+':'#22c55e','A':'#22c55e','A-':'#4ade80','B+':'#60a5fa','B':'#60a5fa','B-':'#93c5fd','C+':'#eab308','C':'#eab308','C-':'#fbbf24','D+':'#f97316','D':'#f97316','F':'#ef4444'}[r.grade] || '#94a3b8';
    const pnlColor = r.total_pnl_pct >= 0 ? '#22c55e' : '#ef4444';
    const expColor = r.expectancy >= 0 ? '#22c55e' : '#ef4444';
    const wrColor = r.win_rate >= 45 ? '#22c55e' : r.win_rate >= 35 ? '#eab308' : '#ef4444';
    return `<tr style="border-bottom:1px solid var(--border);transition:background 0.15s" onmouseover="this.style.background='var(--bg-table-hover)'" onmouseout="this.style.background=''">
      <td style="padding:12px 16px;font-weight:600;font-size:13px">${name}</td>
      <td style="padding:12px;text-align:center"><span style="display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:800;background:${gradeColor}22;color:${gradeColor};border:1px solid ${gradeColor}44">${r.grade}</span></td>
      <td style="padding:12px;text-align:right;font-size:13px">${r.total_trades}</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${wrColor}">${r.win_rate}%</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${r.profit_factor>=1.5?'#22c55e':r.profit_factor>=1.0?'#eab308':'#ef4444'}">${r.profit_factor}</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${expColor}">${r.expectancy>=0?'+':''}${r.expectancy}%</td>
      <td style="padding:12px;text-align:right;color:${r.avg_pnl_pct>=0?'#22c55e':'#ef4444'}">${r.avg_pnl_pct>=0?'+':''}${r.avg_pnl_pct}%</td>
      <td style="padding:12px;text-align:right;font-weight:700;color:${pnlColor}">${r.total_pnl_pct>=0?'+':''}${r.total_pnl_pct}%</td>
      <td style="padding:12px;text-align:right;color:#ef4444">-${r.max_drawdown_pct}%</td>
      <td style="padding:12px;text-align:right">${r.sharpe_ratio}</td>
      <td style="padding:12px;text-align:right;color:#22c55e">+${r.avg_win_pct}%</td>
      <td style="padding:12px;text-align:right;color:#ef4444">${r.avg_loss_pct}%</td>
    </tr>`;
  }).join('');
}

async function fetchMovers() {
  try {
    const resp = await fetch('/api/movers');
    const data = await resp.json();
    const sig = JSON.stringify((data.gainers || []).map(r => r.symbol + r.ltp) + '|' + (data.losers || []).map(r => r.symbol + r.ltp));
    if (sig !== _moversSig) {
      _moversSig = sig;
      renderMovers(data);
    }
  } catch(e) {}
}

function mvFmt(n, d=2) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d});
}
function mvFmtInt(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-IN');
}

function moversRowHTML(r, rank) {
  const up = r.change_pct >= 0;
  const cls = up ? 'up' : 'down';
  const arrow = up ? '▲' : '▼';
  const effCls = r.efficiency >= 65 ? 'up' : r.efficiency >= 45 ? 'muted' : 'down';
  const sigIsBuy = r.signal && r.signal.includes('BUY');
  const sigCls = sigIsBuy ? 'sig-buy' : 'sig-sell';
  const sig = r.signal ? `<span class="sig ${sigCls}">${r.signal}</span>` : '—';
  const sigRowCls = sigIsBuy ? 'signal-row' : r.signal ? 'signal-row sell-row' : '';
  const trk = r.open_gap_pct != null && Math.abs(r.open_gap_pct) >= 2
    ? `<span class="trk ${r.open_gap_pct >= 0 ? 'trk-buy' : 'trk-sell'}" title="Opened ${mvFmt(r.open_gap_pct)}% vs prev close — tracking mode">TRK ${mvFmt(r.open_gap_pct)}%</span>`
    : '';
  const effBar = r.efficiency >= 0 ? ` <span class="effbar" style="width:${Math.min(100, r.efficiency)}px"><i style="width:${r.efficiency}%"></i></span>` : '';
  return `<tr class="${sigRowCls}" ondblclick="openMoversAnalysis('${r.symbol}')" title="Double-click for analysis">
    <td><span class="rank">${rank}</span><span class="sym">${r.name}</span>${trk}</td>
    <td>${mvFmt(r.ltp)}</td>
    <td class="${cls}">${arrow} ${mvFmt(r.change_pct)}%</td>
    <td class="${r.change_from_open >= 0 ? 'up' : 'down'}">${mvFmt(r.change_from_open)}%</td>
    <td>${mvFmt(r.high)}</td>
    <td>${mvFmt(r.low)}</td>
    <td class="${effCls}" title="Position efficiency 0-100">${r.efficiency}${effBar}</td>
    <td>${mvFmtInt(r.volume)}</td>
    <td>${sig}</td>
  </tr>`;
}

function renderMovers(data) {
  const g = data.gainers || [], l = data.losers || [];
  const gBody = document.getElementById('movers-gain-body');
  const lBody = document.getElementById('movers-lose-body');
  gBody.innerHTML = g.length ? g.map((r,i)=>moversRowHTML(r,i+1)).join('') : '<tr><td colspan="9" class="hint">No data</td></tr>';
  lBody.innerHTML = l.length ? l.map((r,i)=>moversRowHTML(r,i+1)).join('') : '<tr><td colspan="9" class="hint">No data</td></tr>';
  const gSig = g.filter(r=>r.signal && r.signal.includes('BUY')).length;
  const lSig = l.filter(r=>r.signal && r.signal.includes('SELL')).length;
  document.getElementById('movers-gain-count').textContent = gSig ? `— ${gSig} BUY signal` : '';
  document.getElementById('movers-lose-count').textContent = lSig ? `— ${lSig} SELL signal` : '';
  document.getElementById('movers-status').textContent =
    data.updated ? `· updated ${new Date(data.updated).toLocaleTimeString('en-IN')} · ${data.total} stocks` : ' · loading…';
}

let _mvOpen = false;
function openMoversAnalysis(symbol) {
  const overlay = document.getElementById('movers-modal-overlay');
  const box = document.getElementById('movers-modal');
  overlay.style.display = 'flex';
  box.style.display = 'flex';
  _mvOpen = true;
  document.getElementById('movers-modal-title').textContent = symbol.replace('NSE:','').replace('-EQ','').replace('-INDEX','');
  document.getElementById('movers-modal-body').innerHTML = '<div class="hint">Analyzing…</div>';
  fetch('/api/movers/analysis?symbol=' + encodeURIComponent(symbol))
    .then(r => r.json())
    .then(a => renderMoversAnalysis(a))
    .catch(e => { document.getElementById('movers-modal-body').innerHTML = '<div class="hint">Failed: ' + e + '</div>'; });
}

function closeMoversModal() {
  document.getElementById('movers-modal-overlay').style.display = 'none';
  document.getElementById('movers-modal').style.display = 'none';
  _mvOpen = false;
}

function mvTag(t) {
  const up = t.startsWith('UP') || t.startsWith('BULL');
  const down = t.startsWith('DOWN') || t.startsWith('BEAR');
  return up ? '<span class="tag bull">' + t + '</span>' : down ? '<span class="tag bear">' + t + '</span>' : '<span class="tag neutral">' + t + '</span>';
}

function renderMoversAnalysis(a) {
  const body = document.getElementById('movers-modal-body');
  if (a.error) { body.innerHTML = '<div class="hint">' + a.error + '</div>'; return; }
  const score = a.strength_score;
  const scoreColor = score >= 55 ? '#22c55e' : score >= 45 ? '#eab308' : '#ef4444';
  const intra = a.intraday || {};
  body.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">Last Close</div><div class="v">${mvFmt(a.last_close)}</div></div>
      <div class="metric"><div class="k">Trend</div><div class="v">${mvTag(a.trend)}</div></div>
      <div class="metric"><div class="k">Strength</div><div class="v">${score}/100</div>
        <div class="score-bar"><div class="score-fill" style="width:${score}%;background:${scoreColor}"></div></div></div>
      <div class="metric"><div class="k">Label</div><div class="v">${a.strength_label}</div></div>
    </div>
    <div class="section-title">Indicators</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">EMA 9/21/50</div><div class="v" style="font-size:13px">${mvFmt(a.ema9)} / ${mvFmt(a.ema21)} / ${mvFmt(a.ema50)}</div></div>
      <div class="metric"><div class="k">RSI 14</div><div class="v">${mvFmt(a.rsi14)}</div></div>
      <div class="metric"><div class="k">ATR 14</div><div class="v">${mvFmt(a.atr14)}</div></div>
      <div class="metric"><div class="k">MACD / Sig</div><div class="v" style="font-size:13px">${mvFmt(a.macd)} / ${mvFmt(a.macd_signal)}</div></div>
    </div>
    <div class="section-title">Key Levels</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">Support</div><div class="v">${mvFmt(a.support)}</div></div>
      <div class="metric"><div class="k">Resistance</div><div class="v">${mvFmt(a.resistance)}</div></div>
      <div class="metric"><div class="k">52W High</div><div class="v up">${mvFmt(a.high_52w)}</div></div>
      <div class="metric"><div class="k">52W Low</div><div class="v down">${mvFmt(a.low_52w)}</div></div>
    </div>
    <div class="section-title">Intraday (15-min)</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px">
      <div class="metric"><div class="k">Session VWAP</div><div class="v">${mvFmt(intra.vwap)}</div></div>
      <div class="metric"><div class="k">Price vs VWAP</div><div class="v ${intra.price_vs_vwap >= 0 ? 'up' : 'down'}">${mvFmt(intra.price_vs_vwap)}%</div></div>
      <div class="metric"><div class="k">Range Pos</div><div class="v">${mvFmt(intra.range_pos_pct,1)}%</div></div>
      <div class="metric"><div class="k">Last Candle</div><div class="v">${intra.candle || '—'}</div></div>
    </div>
    <div class="hint">Double-click another row to switch · analysis cached 5 min</div>`;
}

function applyLiveData(prices) {
  livePrices = prices || {};
  updateLiveCells();
}

async function pollLive() {
  try {
    const resp = await fetch('/api/live');
    const data = await resp.json();
    applyLiveData(data.prices);
  } catch(e) {}
}

let _liveRendered = {};
function updateLiveCells() {
  document.querySelectorAll('[data-price]').forEach(td => {
    const q = livePrices[td.dataset.price];
    if (!q || !q.ltp) return;
    const entry = parseFloat(td.dataset.entry) || 0;
    const up = q.ltp >= entry;
    const txt = `₹${Number(q.ltp).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
    const color = up ? '#22c55e' : '#ef4444';
    const key = 'p:' + td.dataset.price;
    if (_liveRendered[key] === txt + '|' + color) return;
    _liveRendered[key] = txt + '|' + color;
    td.innerHTML = txt;
    td.style.color = color;
  });
  document.querySelectorAll('[data-live-price]').forEach(el => {
    const q = livePrices[el.dataset.livePrice];
    if (!q || !q.ltp) return;
    const entry = parseFloat(el.dataset.liveEntry) || 0;
    const pnl = entry ? ((q.ltp - entry) / entry * 100) : 0;
    const txt = `₹${Number(q.ltp).toFixed(2)} (${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%)`;
    const color = pnl >= 0 ? '#22c55e' : '#ef4444';
    const key = 'e:' + el.dataset.livePrice;
    if (_liveRendered[key] === txt + '|' + color) return;
    _liveRendered[key] = txt + '|' + color;
    el.textContent = txt;
    el.style.color = color;
  });
}

// ==================== SSE STREAM (replaces polling) ====================
let _streamConnected = false;

function connectStream() {
  if (!window.EventSource) {
    // Fallback: keep polling
    fetchSignals(); setInterval(fetchSignals, 30000);
    pollLive(); setInterval(pollLive, 10000);
    return;
  }
  const es = new EventSource('/api/stream');
  es.addEventListener('signals', e => {
    try { applySignalsData(JSON.parse(e.data)); } catch(_) {}
  });
  es.addEventListener('live', e => {
    try { applyLiveData(JSON.parse(e.data).prices); } catch(_) {}
  });
  es.addEventListener('ping', e => {
    // Heartbeat received — connection alive
  });
  es.onerror = () => {
    // EventSource auto-reconnects; on first failure fall back to polling
    if (!_streamConnected) {
      fetchSignals(); setInterval(fetchSignals, 30000);
      pollLive(); setInterval(pollLive, 10000);
    }
  };
  es.onopen = () => { _streamConnected = true; };
}

// ==================== INDEX TABLE (separate column) ====================
let indexStrategyFilter = 'all';
function filterIndexSignals(strat) {
  indexStrategyFilter = strat;
  document.querySelectorAll('#index-section .filter-btn').forEach(b => b.classList.remove('active'));
  const el = document.getElementById('ifs-' + (strat === 'all' ? 'all' : strat.replace(/[^A-Za-z0-9]/g, '').toLowerCase()));
  if (el) el.classList.add('active');
  renderIndexTable();
}

function renderIndexTable() {
  const section = document.getElementById('index-section');
  const tbody = document.getElementById('index-body');
  const empty = document.getElementById('index-empty');
  const filtered = indexStrategyFilter === 'all'
    ? allIndexSignals
    : allIndexSignals.filter(s => (s.strategies || [s.strategy || '']).includes(indexStrategyFilter));
  document.getElementById('index-count').textContent = `(${filtered.length}/${allIndexSignals.length})`;

  if (filtered.length === 0) {
    section.style.display = 'block';
    empty.style.display = 'block';
    tbody.innerHTML = '';
    return;
  }
  section.style.display = 'block';
  empty.style.display = 'none';

  tbody.innerHTML = filtered.map((s, idx) => {
    const strats = s.strategies || [s.strategy || ''];
    const stratTags = strats.map(name => {
      const cls = stratTagClass(name);
      const shortName = name.replace('Range Breakout ', 'R').replace('Consolidation Breakout', 'Consolidation');
      return `<span class="strat-tag ${cls}">${shortName}</span>`;
    }).join('');
    const rr = s.price && s.stop_loss && s.target ?
      ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '—';
    const badgeClass = strengthBadgeClass(s.strength || s.signal_type);
    const emoji = s.emoji || (s.signal_type === 'BUY' ? '🟢' : '🔴');
    const confPct = ((s.confidence || 0) * 100).toFixed(0);
    return `
    <tr onclick="toggleIndexExpand(${idx})" data-idx="idx-${idx}" style="cursor:pointer">
      <td>
        <div class="symbol-cell">
          <div class="symbol-icon" style="background:${symbolColor(s.symbol_name)}">${(s.symbol_name||'?').substring(0,2)}</div>
          <div class="symbol-info">
            <span class="symbol-name">${s.symbol_name}</span>
            <span class="symbol-tag">${s.symbol}</span>
          </div>
        </div>
      </td>
      <td><span class="strength-badge ${badgeClass}">${emoji} ${s.strength || s.signal_type}</span></td>
      <td><div class="strat-tags">${stratTags}</div></td>
      <td class="price-cell" data-price="${s.symbol}" data-entry="${s.price}">₹${Number(s.price).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="sl-cell">₹${Number(s.stop_loss).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="target-cell">₹${Number(s.target).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
      <td class="confidence-cell">${confPct}%</td>
      <td><span class="risk-reward">1:${rr}</span></td>
    </tr>
    <tr class="expand-row" id="index-expand-${idx}">
      <td colspan="8">
        <div class="expand-content">
          <div id="index-chart-${idx}" class="index-chart-wrap"></div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ==================== INDEX CHART (same analysis as stocks) ====================
let _lastIdxToggleRow = null;
let _lastIdxToggleTime = 0;
function toggleIndexExpand(idx) {
  const now = Date.now();
  if (idx === _lastIdxToggleRow && now - _lastIdxToggleTime < 350) return;
  _lastIdxToggleRow = idx;
  _lastIdxToggleTime = now;
  const row = document.getElementById('index-expand-' + idx);
  if (!row) return;
  document.querySelectorAll('#index-body .expand-row').forEach(r => { if (r.id !== row.id) r.classList.remove('open'); });
  document.querySelectorAll('#index-body tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
  row.classList.toggle('open');
  if (row.classList.contains('open')) {
    document.querySelector(`#index-body tr[data-idx="idx-${idx}"]`).classList.add('selected');
    loadIndexChart(idx, 30);
  }
}

async function loadIndexChart(idx, days = 30) {
  const s = allIndexSignals[idx];
  if (!s) return;
  const container = document.getElementById('index-chart-' + idx);
  if (!container) return;
  container.style.position = 'relative';
  const key = 'idx_' + s.symbol + '_' + days;
  if (_chartCache[key]) {
    drawChart(container, _chartCache[key], s, days, idx, true);
    return;
  }
  try {
    const resp = await fetch(`/api/history?symbol=${encodeURIComponent(s.symbol)}&days=${days}`);
    const data = await resp.json();
    if (data.candles && data.candles.length > 0) {
      _cacheChart(key, data);
      drawChart(container, data, s, days, idx, true);
    } else {
      container.innerHTML = '<div class="sparkline-loading">No chart data available</div>';
    }
  } catch(e) {
    container.innerHTML = '<div class="sparkline-loading">Chart load failed</div>';
  }
}

// ==================== AUTO ====================
async function toggleAuto() {
  autoEnabled = !autoEnabled;
  const btn = document.getElementById('auto-btn');
  if (autoEnabled) {
    btn.textContent = '⏸ Stop';
    btn.className = 'btn btn-danger';
    await fetch('/api/auto?enable=true');
    showToast('Auto-scan enabled (every 5 min during market hours)');
  } else {
    btn.textContent = '▶ Auto';
    btn.className = 'btn btn-success';
    await fetch('/api/auto?enable=false');
    showToast('Auto-scan disabled');
  }
}

// ==================== EXPORT CSV ====================
function testTelegram() {
  const btn = document.getElementById('tg-btn');
  btn.innerHTML = '⏳';
  btn.disabled = true;
  fetch('/api/telegram?action=test')
    .then(r => r.json())
    .then(d => {
      if (d.success) {
        showToast('✅ Telegram connected! Check your chat.');
        btn.innerHTML = '📱';
      } else {
        showToast('❌ Telegram failed: ' + (d.message || 'Not configured'));
        btn.innerHTML = '📱';
      }
      btn.disabled = false;
    })
    .catch(e => {
      showToast('❌ Error: ' + e.message);
      btn.innerHTML = '📱';
      btn.disabled = false;
    });
}

function exportCSV() {
  if (filteredSignals.length === 0) { showToast('No data to export'); return; }
  const headers = ['Symbol', 'Name', 'Strength', 'Strategies', 'Price', 'Stop Loss', 'Target', 'Risk:Reward', 'Confidence', 'Timeframe', 'Reasons'];
  const rows = filteredSignals.map(s => {
    const rr = s.price && s.stop_loss && s.target ? ((s.target - s.price) / Math.max(0.01, s.price - s.stop_loss)).toFixed(1) : '';
    return [
      s.symbol, s.symbol_name, s.strength || s.signal_type,
      (s.strategies || []).join('; '),
      s.price, s.stop_loss, s.target, rr,
      ((s.confidence || 0) * 100).toFixed(0) + '%',
      s.timeframe,
      (s.reasons || [s.reason] || []).join('; ')
    ].map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',');
  });
  const csv = [headers.join(','), ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `scanner_signals_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  showToast('CSV exported!');
}

// ==================== TOAST ====================
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3500);
}

// ==================== CLOCK ====================
function updateClock() {
  const now = new Date();
  const ist = new Date(now.getTime() + (5.5 * 60 - now.getTimezoneOffset()) * 60000);
  const h = ist.getHours(), m = ist.getMinutes(), s = ist.getSeconds();
  const pad = n => String(n).padStart(2, '0');
  const marketOpen = (h === 9 && m >= 15) || (h >= 10 && h <= 14) || (h === 15 && m <= 30);
  const status = marketOpen ? '🟢 Market Open' : '🔴 Market Closed';
  document.getElementById('market-clock').innerHTML = `IST ${pad(h)}:${pad(m)}:${pad(s)} · ${status}`;
}
setInterval(updateClock, 30000);
updateClock();

// ==================== KEYBOARD SHORTCUTS ====================
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') {
    if (e.key === 'Escape') { e.target.blur(); document.getElementById('search-input').value = ''; applyFilters(); }
    return;
  }
  if (e.key === 's' || e.key === 'S') { e.preventDefault(); triggerScan(); }
  if (e.key === '/' || e.key === 'f' || e.key === 'F') { e.preventDefault(); document.getElementById('search-input').focus(); }
  if (e.key === 'Escape' && selectedRow !== null) {
    document.querySelectorAll('.expand-row').forEach(r => r.classList.remove('open'));
    document.querySelectorAll('tbody tr[data-idx]').forEach(tr => tr.classList.remove('selected'));
    selectedRow = null;
  }
});

// ==================== WATCHLIST ====================
let _watchlistData = [];
let _watchlistSymbols = new Set();

async function loadWatchlist() {
  try {
    const resp = await fetch('/api/watchlist');
    const data = await resp.json();
    _watchlistData = data.watchlist || [];
    _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
    document.getElementById('wl-count').textContent = _watchlistData.length;
    renderWatchlist();
  } catch(e) {}
}

function renderWatchlist() {
  const list = document.getElementById('wl-list');
  if (_watchlistData.length === 0) {
    list.innerHTML = '<div class="wl-empty"><div class="icon">⭐</div><p>No stocks in watchlist yet.<br>Add stocks from the scanner or type a symbol above.</p></div>';
    return;
  }
  list.innerHTML = _watchlistData.map(w => {
    const color = symbolColor(w.name || w.symbol);
    const added = w.added_at ? new Date(w.added_at).toLocaleDateString('en-IN') : '';
    return `
    <div class="wl-item">
      <div class="wl-item-icon" style="background:${color}">${(w.name||'?').substring(0,2)}</div>
      <div class="wl-item-info">
        <div class="wl-item-name">${w.name}</div>
        <div class="wl-item-symbol">${w.symbol}</div>
        ${w.notes ? `<div class="wl-item-notes">📝 ${w.notes}</div>` : ''}
      </div>
      <button class="wl-item-remove" onclick="removeFromWatchlist('${w.symbol}')" title="Remove">✕</button>
    </div>`;
  }).join('');
}

function toggleWatchlist() {
  const panel = document.getElementById('wl-panel');
  const overlay = document.getElementById('wl-overlay');
  const isOpen = panel.classList.contains('open');
  if (isOpen) {
    panel.classList.remove('open');
    overlay.classList.remove('open');
  } else {
    panel.classList.add('open');
    overlay.classList.add('open');
    loadWatchlist();
  }
}

async function addToWatchlist() {
  const symbolInput = document.getElementById('wl-add-symbol');
  const notesInput = document.getElementById('wl-add-notes');
  let symbol = symbolInput.value.trim().toUpperCase();
  const notes = notesInput.value.trim();

  if (!symbol) { showToast('Enter a stock symbol'); return; }

  // Normalize symbol format
  if (!symbol.includes(':')) {
    symbol = 'NSE:' + symbol + '-EQ';
  }

  const name = symbol.split(':').pop().replace('-EQ', '');

  try {
    const resp = await fetch('/api/watchlist/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol, name, notes })
    });
    const data = await resp.json();
    if (data.status === 'already_exists') {
      showToast(name + ' already in watchlist');
    } else {
      showToast(name + ' added to watchlist! ⭐');
    }
    _watchlistData = data.watchlist || [];
    _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
    document.getElementById('wl-count').textContent = _watchlistData.length;
    renderWatchlist();
    renderTable(); // Update add buttons in table
    symbolInput.value = '';
    notesInput.value = '';
  } catch(e) {
    showToast('Error adding to watchlist');
  }
}

async function removeFromWatchlist(symbol) {
  try {
    const resp = await fetch('/api/watchlist/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol })
    });
    const data = await resp.json();
    _watchlistData = data.watchlist || [];
    _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
    document.getElementById('wl-count').textContent = _watchlistData.length;
    renderWatchlist();
    renderTable(); // Update add buttons in table
    showToast(symbol.split(':').pop().replace('-EQ','') + ' removed');
  } catch(e) {}
}

async function toggleStockWatchlist(symbol, name) {
  if (_watchlistSymbols.has(symbol)) {
    await removeFromWatchlist(symbol);
  } else {
    try {
      const resp = await fetch('/api/watchlist/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ symbol, name, notes: '' })
      });
      const data = await resp.json();
      _watchlistData = data.watchlist || [];
      _watchlistSymbols = new Set(_watchlistData.map(w => w.symbol));
      document.getElementById('wl-count').textContent = _watchlistData.length;
      renderTable();
      showToast(name + ' added to watchlist! ⭐');
    } catch(e) {}
  }
}

// Load watchlist on init
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('fyers_theme'); } catch(e) {}
  if (saved && THEMES[saved]) setTheme(saved);
})();
setupTabs();
loadWatchlist();
fetchQuality();
fetchBigMoney();
fetchChartStrategy();
fetchMovers();
setInterval(fetchMovers, 30000);

// ==================== INIT ====================
// Use SSE for real-time signals + live prices (falls back to polling if unsupported)
connectStream();
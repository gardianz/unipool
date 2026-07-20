/* unipool web — chart + atur range lewat drag.
   Aturan penting: browser TIDAK pernah mengirim alamat pool/token untuk tx.
   Server memegang pool hasil discovery, klien cuma pegang key-nya.       */
'use strict';

const $ = s => document.querySelector(s);
const TOKEN = new URLSearchParams(location.search).get('t') || '';

async function api(path, body) {
  const opt = {headers: {'Content-Type': 'application/json'}};
  if (TOKEN) opt.headers['X-Token'] = TOKEN;
  if (body !== undefined) { opt.method = 'POST'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error: 'respons bukan JSON'}));
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

// ── format ──
const nf = (v, d = 2) => (v == null || isNaN(v)) ? '—' :
  Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
function usd(v) {
  if (v == null || isNaN(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e9) return '$' + nf(v / 1e9, 2) + 'B';
  if (a >= 1e6) return '$' + nf(v / 1e6, 2) + 'M';
  if (a >= 1e3) return '$' + nf(v / 1e3, 1) + 'K';
  return '$' + nf(v, a < 1 ? 4 : 2);
}
function price(v) {
  if (v == null || !isFinite(v)) return '—';
  if (v === 0) return '0';
  if (v >= 1) return nf(v, 4);
  const s = v.toFixed(20), m = s.match(/^0\.(0*)/);
  return v.toPrecision(Math.min(6, 3 + (m ? m[1].length : 0))).replace(/0+$/, '');
}
function amt(v) {
  if (v == null || !isFinite(v)) return '—';
  if (v === 0) return '0';
  if (Math.abs(v) >= 1000) return nf(v, 2);
  if (Math.abs(v) >= 1) return nf(v, 4);
  return v.toPrecision(4);
}
function pctTxt(p) {
  if (p == null || !isFinite(p)) return '';
  return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
}

let toastT;
function toast(html, kind) {
  const t = $('#toast');
  t.className = 'toast ' + (kind || '');
  t.innerHTML = html;
  clearTimeout(toastT);
  if (kind !== 'hold') toastT = setTimeout(() => t.classList.add('hide'), kind === 'err' ? 12000 : 8000);
}
const modal = html => { $('#sheet').innerHTML = html; $('#modal').classList.remove('hide'); };
const closeModal = () => $('#modal').classList.add('hide');
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

// ══════════ state ══════════
const S = {
  chain: null, settings: {}, token: null, pools: [], pool: null,
  tf: '15m', candles: [], min: null, max: null, mode: 'lower',
  preview: null, amountPct: 50, amountFixed: null, bars: [], busy: false,
};

// ══════════ chart ══════════
let chart, series, lineMin, lineMax, overlay, dragging = null;

// Skala harga ikut melebar supaya garis MIN/MAX selalu kelihatan, walau
// range-nya jauh di luar rentang candle (mis. −50% single).
function autoscale(orig) {
  const r = orig();
  if (S.min == null && S.max == null) return r;
  const lo = Math.min(r ? r.priceRange.minValue : Infinity, S.min ?? Infinity);
  const hi = Math.max(r ? r.priceRange.maxValue : -Infinity, S.max ?? -Infinity);
  if (!isFinite(lo) || !isFinite(hi) || lo >= hi) return r;
  const pad = (hi - lo) * 0.05;
  return {priceRange: {minValue: lo - pad, maxValue: hi + pad}};
}

function initChart() {
  if (chart) return;
  const el = $('#chart');
  chart = LightweightCharts.createChart(el, {
    layout: {background: {color: '#0d1110'}, textColor: '#7f8f85', fontSize: 11},
    grid: {vertLines: {color: '#161c19'}, horzLines: {color: '#161c19'}},
    rightPriceScale: {borderColor: '#232a26', scaleMargins: {top: .12, bottom: .12}},
    timeScale: {borderColor: '#232a26', timeVisible: true, secondsVisible: false},
    crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
    localization: {priceFormatter: price},
    handleScale: {axisPressedMouseMove: {price: false}},
  });
  series = chart.addCandlestickSeries({
    upColor: '#39d98a', downColor: '#ff5c5c', borderVisible: false,
    wickUpColor: '#39d98a', wickDownColor: '#ff5c5c',
    priceFormat: {type: 'custom', formatter: price, minMove: 1e-8},
    autoscaleInfoProvider: autoscale,
  });

  overlay = document.createElement('div');
  Object.assign(overlay.style, {position: 'absolute', inset: '0', pointerEvents: 'none', zIndex: 5});
  el.style.position = 'relative';
  el.appendChild(overlay);
  for (const k of ['min', 'max']) {
    const h = document.createElement('div');
    h.dataset.k = k;
    Object.assign(h.style, {
      position: 'absolute', left: '0', right: '0', height: '18px', marginTop: '-9px',
      cursor: 'ns-resize', pointerEvents: 'auto', display: 'none',
    });
    h.innerHTML = `<div style="position:absolute;left:0;right:0;top:8px;height:2px;background:#c8ff2e"></div>
      <div style="position:absolute;right:4px;top:0;background:#c8ff2e;color:#0b0d0c;font:700 10px/18px ui-monospace,monospace;
        padding:0 7px;border-radius:4px">${k.toUpperCase()}</div>`;
    overlay.appendChild(h);
    h.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      h.setPointerCapture(ev.pointerId);
      dragging = k;
    });
  }
  overlay.addEventListener('pointermove', ev => {
    if (!dragging) return;
    const y = ev.clientY - $('#chart').getBoundingClientRect().top;
    const p = series.coordinateToPrice(y);
    if (p == null || p <= 0) return;
    if (dragging === 'min') S.min = Math.min(p, S.max ?? p * 2);
    else S.max = Math.max(p, S.min ?? p / 2);
    syncFromDrag(false);
  });
  const stop = () => { if (dragging) { dragging = null; syncFromDrag(true); } };
  overlay.addEventListener('pointerup', stop);
  overlay.addEventListener('pointercancel', stop);

  chart.timeScale().subscribeVisibleTimeRangeChange(drawHandles);
  new ResizeObserver(() => {
    chart.applyOptions({width: el.clientWidth, height: el.clientHeight});
    drawHandles(); drawLiq();
  }).observe(el);
}

function drawHandles() {
  if (!series || !overlay) return;
  for (const h of overlay.children) {
    const v = h.dataset.k === 'min' ? S.min : S.max;
    const y = v ? series.priceToCoordinate(v) : null;
    if (y == null) { h.style.display = 'none'; continue; }
    h.style.display = 'block';
    h.style.top = y + 'px';
  }
  drawLiq();
}

function setPriceLines() {
  if (!series) return;
  for (const l of [lineMin, lineMax]) if (l) series.removePriceLine(l);
  const mk = (v, t) => v ? series.createPriceLine({
    price: v, color: '#c8ff2e', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: t,
  }) : null;
  lineMin = mk(S.min, 'MIN');
  lineMax = mk(S.max, 'MAX');
  series.applyOptions({autoscaleInfoProvider: autoscale});  // paksa hitung ulang skala
  drawHandles();
}

// histogram likuiditas di kanan chart, sejajar skala harga
function drawLiq() {
  const cv = $('#liq');
  if (!cv || !series) return;
  const box = $('#liqbox').getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, box.width), H = Math.max(1, box.height);
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  if (!S.bars.length) {
    g.fillStyle = '#3a463f'; g.font = '10px system-ui'; g.textAlign = 'center';
    g.fillText('no liq data', W / 2, H / 2);
    return;
  }
  const vertical = box.width > box.height;   // desktop: bar horizontal, mobile: dibalik
  const maxL = Math.max(...S.bars.map(b => b.liq)) || 1;
  const now = S.pool ? S.pool.price : 0;
  for (const b of S.bars) {
    const y0 = series.priceToCoordinate(b.p1), y1 = series.priceToCoordinate(b.p0);
    if (y0 == null || y1 == null) continue;
    const h = Math.max(1, Math.abs(y1 - y0)), y = Math.min(y0, y1);
    if (y > H || y + h < 0) continue;
    const inR = S.min && S.max && b.p1 >= S.min && b.p0 <= S.max;
    const w = Math.max(1, (b.liq / maxL) * (W - 4));
    g.fillStyle = inR ? 'rgba(200,255,46,.85)' : 'rgba(120,140,128,.4)';
    if (vertical) g.fillRect(0, y, w, h);
    else g.fillRect(0, y, w, h);
  }
  const yn = series.priceToCoordinate(now);
  if (yn != null) {
    g.strokeStyle = '#e8efe9'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, yn + .5); g.lineTo(W, yn + .5); g.stroke();
  }
}

// ══════════ range ⇄ mode ══════════
function modeFromRange() {
  const now = S.pool.price;
  if (S.max != null && S.max <= now * 1.0005) return 'lower';   // range di bawah harga
  if (S.min != null && S.min >= now * 0.9995) return 'upper';   // range di atas harga
  return 'wide';
}

function pctsFromRange() {
  const now = S.pool.price;
  return {
    low_pct: Math.max(0.01, Math.min(99, (1 - (S.min ?? now) / now) * 100)),
    up_pct: Math.max(0.01, ((S.max ?? now) / now - 1) * 100),
  };
}

let prevT;
function syncFromDrag(final) {
  $('#minIn').value = price(S.min);
  $('#maxIn').value = price(S.max);
  const now = S.pool.price;
  $('#minPct').textContent = pctTxt(((S.min ?? now) / now - 1) * 100);
  $('#maxPct').textContent = pctTxt(((S.max ?? now) / now - 1) * 100);
  setPriceLines();
  clearTimeout(prevT);
  prevT = setTimeout(refreshPreview, final ? 0 : 350);
}

function applyPreset(mode, low, up) {
  const now = S.pool.price;
  S.mode = mode;
  if (mode === 'lower') { S.min = now * (1 - low / 100); S.max = now; }
  else if (mode === 'upper') { S.min = now; S.max = now * (1 + up / 100); }
  else { S.min = now * (1 - low / 100); S.max = now * (1 + up / 100); }
  syncFromDrag(true);
}

// Penjaga urutan: drag cepat bisa memicu beberapa /api/preview sekaligus.
// Respons yang datang telat harus dibuang — kalau tidak, range di layar
// (dan yang dipakai saat mint) bisa balik ke nilai lama.
let previewSeq = 0;

async function refreshPreview() {
  if (!S.pool) return;
  const seq = ++previewSeq;
  const poolKey = S.pool.key;
  S.mode = modeFromRange();
  const {low_pct, up_pct} = pctsFromRange();
  const body = {
    key: poolKey, mode: S.mode, low_pct, up_pct,
    amount_pct: S.amountFixed ? 0 : S.amountPct,
    amount_fixed: S.amountFixed, supply: S.pool.supply || 0,
  };
  $('#depinfo').innerHTML = '<span class="spin"></span> hitung...';
  try {
    const p = await api('/api/preview', body);
    if (seq !== previewSeq || !S.pool || S.pool.key !== poolKey) return;  // sudah basi
    p.req = {mode: S.mode, low_pct, up_pct};   // dipakai lagi saat mint, biar persis
    S.preview = p;
    if (p.ver !== 2) {
      // snap ke tick asli hasil server (sudah dibulatkan ke tick spacing)
      S.min = p.price_lower; S.max = p.price_upper;
      $('#minIn').value = price(S.min); $('#maxIn').value = price(S.max);
      $('#minPct').textContent = pctTxt(p.pct_lower);
      $('#maxPct').textContent = pctTxt(p.pct_upper);
      setPriceLines();
    }
    renderDeposit(p);
  } catch (e) {
    if (seq !== previewSeq) return;
    $('#depinfo').innerHTML = '<span class="bad">' + e.message + '</span>';
    $('#mint').disabled = true;
  }
}

const MODE_TXT = {
  lower: 'Range di bawah harga — deposit <b>QUOTE</b> saja (cuan kalau harga turun balik)',
  upper: 'Range di atas harga — deposit <b>TOKEN</b> saja (cuan kalau harga naik)',
  wide: 'Range dua sisi — butuh quote + token, sisa quote di-swap otomatis',
  stable: 'Range sempit dua sisi',
};

function renderDeposit(p) {
  const t = S.pool;
  $('#amtSym').textContent = p.dep_sym;
  if (!S.amountFixed) $('#amt').value = amt(p.amount);
  $('#sideNote').innerHTML = p.ver === 2 ? 'v2 = full range 50/50' :
    (S.mode === 'lower' ? `Range below market, deposits ${p.dep_sym} only` :
     S.mode === 'upper' ? `Range above market, deposits ${p.dep_sym} only` :
     'Two-sided range');
  const rows = [];
  if (p.ver === 2) {
    rows.push('LP v2 full range 50/50 — fee 0.3% auto-compound.');
  } else {
    rows.push(MODE_TXT[S.mode]);
    rows.push(`Range: <b>${price(p.price_lower)}</b> – <b>${price(p.price_upper)}</b> ${t.quote_sym}/${t.token_sym}
      <span class="dim">(tick ${p.tick_lower} … ${p.tick_upper})</span>`);
    if (p.mc_lower) rows.push(`Market cap range: <b>${usd(p.mc_lower)}</b> – <b>${usd(p.mc_upper)}</b>`);
    if (p.comp) rows.push(`Komposisi: <b>${amt(p.comp.quote)}</b> ${t.quote_sym} masuk pool` +
      (p.comp.swap > 0 ? ` · swap <b>${amt(p.comp.swap)}</b> ${t.quote_sym} → ${t.token_sym}` : ' · tanpa swap'));
  }
  rows.push(`Deposit: <b>${amt(p.amount)}</b> ${p.dep_sym} ≈ <b>${usd(p.usd)}</b>`);
  $('#depinfo').innerHTML = rows.join('<br>');
  $('#mint').disabled = !(p.amount > 0);
  $('#mint').textContent = p.amount > 0 ? `Mint position · ${amt(p.amount)} ${p.dep_sym}` : 'Saldo kosong';
}

// ══════════ load pool ══════════
async function loadCandles() {
  $('#srcinfo').textContent = 'memuat chart...';
  try {
    const r = await api(`/api/candles?key=${encodeURIComponent(S.pool.key)}&tf=${S.tf}` +
                        (TOKEN ? `&t=${encodeURIComponent(TOKEN)}` : ''));
    S.candles = r.candles;
    series.setData(r.candles);
    if (r.candles.length) chart.timeScale().fitContent();
    $('#srcinfo').textContent = r.candles.length
      ? `${r.candles.length} candle · sumber: ${r.source}`
      : 'chart belum tersedia (pool terlalu baru / belum di-index)';
  } catch (e) {
    S.candles = [];
    series.setData([]);
    $('#srcinfo').textContent = 'chart gagal: ' + e.message;
  }
  drawHandles();
}

async function loadLiq() {
  try {
    const r = await api('/api/liquidity', {key: S.pool.key});
    S.bars = r.bars || [];
  } catch { S.bars = []; }
  drawLiq();
}

// Skala harga meme sering sangat kecil (1e-7 dst). minMove harus 10^-n dengan
// n ≤ 14 — di bawah itu library gagal ("unexpected base") karena 1/minMove
// tidak lolos cek pangkat-10 dalam float.
function tuneScale(px) {
  const zeros = px > 0 && px < 1 ? Math.ceil(-Math.log10(px)) : 0;
  const dec = Math.min(14, Math.max(4, zeros + 5));
  series.applyOptions({priceFormat: {type: 'custom', formatter: price, minMove: Math.pow(10, -dec)}});
}

async function openPool(key) {
  $('#editor').classList.remove('hide');
  $('#poolEmpty').classList.add('hide');
  for (const el of document.querySelectorAll('.poolrow')) el.classList.toggle('on', el.dataset.key === key);
  initChart();
  const p = await api('/api/pool', {key});
  S.pool = p;
  tuneScale(p.price);
  $('#pair').textContent = `${p.token_sym} / ${p.quote_sym}`;
  $('#pairsub').innerHTML = `<span class="badge v${p.ver}">v${p.ver}</span> fee ${p.fee_pct.toFixed(2)}%
    · <span class="mono">${p.pool.slice(0, 10)}…${p.pool.slice(-6)}</span>`;
  $('#stats').innerHTML = `
    <div><small>Price (${p.quote_sym}/${p.token_sym})</small><b>${price(p.price)}</b></div>
    <div><small>Price USD</small><b>${usd(p.price_usd)}</b></div>
    <div><small>TVL</small><b>${usd(p.tvl_usd)}</b></div>
    <div><small>Volume 24H</small><b>${usd(p.vol24_usd)}</b></div>
    <div><small>Pool APR</small><b>${p.apr_pct ? nf(p.apr_pct, 0) + '%' : '—'}</b></div>
    <div><small>Market cap</small><b>${usd(p.mc_usd)}</b></div>`;
  buildPresets();
  applyPreset('lower', 30, 30);
  await Promise.all([loadCandles(), loadLiq()]);
}

function buildPresets() {
  const v2 = S.pool.ver === 2;
  $('#presets').innerHTML = v2 ? '<span class="dim">v2 selalu full range</span>' : `
    <button data-m="lower" data-l="10">−10% single</button>
    <button data-m="lower" data-l="20">−20% single</button>
    <button data-m="lower" data-l="30">−30% single</button>
    <button data-m="lower" data-l="50">−50% single</button>
    <button data-m="upper" data-u="20">+20% single</button>
    <button data-m="upper" data-u="50">+50% single</button>
    <button data-m="wide" data-l="5" data-u="5">±5%</button>
    <button data-m="wide" data-l="10" data-u="10">±10%</button>
    <button data-m="wide" data-l="20" data-u="20">±20%</button>`;
  for (const b of $('#presets').querySelectorAll('button')) {
    b.onclick = () => {
      for (const x of $('#presets').querySelectorAll('button')) x.classList.remove('on');
      b.classList.add('on');
      applyPreset(b.dataset.m, +(b.dataset.l || 0), +(b.dataset.u || 0));
    };
  }
  $('#tfs').innerHTML = ['1m', '5m', '15m', '1h', '4h', '1d']
    .map(t => `<button data-tf="${t}"${t === S.tf ? ' class="on"' : ''}>${t.toUpperCase()}</button>`).join('');
  for (const b of $('#tfs').querySelectorAll('button')) {
    b.onclick = () => {
      S.tf = b.dataset.tf;
      for (const x of $('#tfs').querySelectorAll('button')) x.classList.toggle('on', x === b);
      loadCandles();
    };
  }
  // v2 = selalu full range: tidak ada MIN/MAX untuk diatur
  $('#presets').classList.toggle('hide', v2);
  $('#ranges').classList.toggle('hide', v2);
  $('#dragHint').classList.toggle('hide', v2);
  if (v2) { S.min = S.max = null; setPriceLines(); }
}

// ══════════ discovery ══════════
async function discover() {
  const ca = $('#ca').value.trim();
  if (!/^0x[0-9a-fA-F]{40}$/.test(ca)) return toast('Bukan alamat kontrak yang valid.', 'err');
  if (!S.chain) { try { await loadState(); } catch { return toast('Server belum siap.', 'err'); } }
  $('#go').disabled = true;
  $('#go').innerHTML = '<span class="spin"></span> cari...';
  try {
    const r = await api('/api/discover', {chain: S.chain, token: ca});
    S.token = r.token; S.pools = r.pools;
    $('#pools').classList.remove('hide');
    $('#poolEmpty').classList.add('hide');
    $('#pools').innerHTML = r.pools.map(p => `
      <div class="poolrow" data-key="${p.key}">
        <span class="badge v${p.ver}">v${p.ver}</span>
        <div><b>${r.token.symbol} / ${p.quote_sym}</b>
          <div class="dim" style="font-size:11px">fee ${p.fee_pct.toFixed(2)}%</div></div>
        <div class="cell"><small>TVL</small>${usd(p.tvl_usd)}</div>
        <div class="cell"><small>Vol 24H</small>${usd(p.vol24_usd)}</div>
        <div class="cell"><small>APR</small>${p.apr_pct ? nf(p.apr_pct, 0) + '%' : '—'}</div>
        <div class="cell"><small>Fee tier</small>${p.fee_pct.toFixed(2)}%</div>
      </div>`).join('');
    for (const el of $('#pools').querySelectorAll('.poolrow')) {
      el.onclick = () => openPool(el.dataset.key).catch(e => toast(e.message, 'err'));
    }
    await openPool(r.pools[0].key);
  } catch (e) {
    toast('Gagal: ' + e.message, 'err');
  } finally {
    $('#go').disabled = false; $('#go').textContent = 'Cari pool';
  }
}

// ══════════ mint ══════════
async function doMint() {
  if (S.busy || !S.preview) return;
  const p = S.preview, t = S.pool;
  const range = p.ver === 2 ? 'full range (v2)' :
    `${price(p.price_lower)} – ${price(p.price_upper)} ${t.quote_sym}/${t.token_sym}`;
  modal(`<h3>Konfirmasi mint</h3>
    <div class="dim" style="font-size:13px;line-height:1.8">
      Pool <b class="mono">${t.token_sym}/${t.quote_sym}</b> v${t.ver} · fee ${t.fee_pct.toFixed(2)}%<br>
      Range: <b class="mono">${range}</b><br>
      Deposit: <b class="mono">${amt(p.amount)} ${p.dep_sym}</b> ≈ <b>${usd(p.usd)}</b><br>
      Slippage ${S.settings.slippage_pct}% · transaksi ini memindahkan dana sungguhan.
    </div>
    <div class="row">
      <button class="primary" id="mok">Ya, mint sekarang</button>
      <button onclick="closeModal()">Batal</button>
    </div>`);
  $('#mok').onclick = async () => {
    closeModal();
    S.busy = true;
    $('#mint').disabled = true;
    toast('<span class="spin"></span> Minting... jangan tutup halaman.', 'hold');
    try {
      // pakai persen yang persis menghasilkan preview yang dikonfirmasi user
      const req = p.req || {mode: S.mode, ...pctsFromRange()};
      const r = await api('/api/mint', {
        key: t.key, ...req,
        amount_pct: S.amountFixed ? 0 : S.amountPct, amount_fixed: S.amountFixed,
      });
      toast(`✅ <b>Position ${r.pid}</b> · ${usd(r.deposited_usd)}<br>` +
        r.steps.map(s => `${s.label}: <a href="${s.url}" target="_blank" rel="noopener">tx</a>`).join(' · ') +
        `<br><a href="${r.link}" target="_blank" rel="noopener">buka di Uniswap</a>`, 'ok');
      loadState();
    } catch (e) {
      toast('❌ Mint gagal: ' + e.message, 'err');
    } finally {
      S.busy = false; refreshPreview();
    }
  };
}

// ══════════ positions ══════════
async function loadPositions() {
  $('#poslist').innerHTML = '<div class="empty"><span class="spin"></span> memuat posisi...</div>';
  try {
    const r = await api('/api/positions?chain=' + S.chain + (TOKEN ? '&t=' + encodeURIComponent(TOKEN) : ''));
    const s = r.summary;
    const net = s.withdrawals + s.fees_claimed - s.deposits;
    $('#pnl').innerHTML = `
      <div><small>Deposit</small><b>${usd(s.deposits)}</b></div>
      <div><small>Withdrawn</small><b>${usd(s.withdrawals)}</b></div>
      <div><small>Fee terklaim</small><b class="ok">${usd(s.fees_claimed)}</b></div>
      <div><small>Realized</small><b class="${net >= 0 ? 'ok' : 'bad'}">${usd(net)}</b></div>`;
    if (!r.positions.length) {
      $('#poslist').innerHTML = '<div class="empty">Belum ada posisi aktif.</div>';
      return;
    }
    $('#poslist').innerHTML = r.positions.map(posCard).join('');
    for (const b of $('#poslist').querySelectorAll('button[data-act]')) {
      b.onclick = () => posAction(b.dataset.pid, b.dataset.act, b.dataset.ver);
    }
  } catch (e) {
    $('#poslist').innerHTML = `<div class="empty bad">${e.message}</div>`;
  }
}

function posCard(p) {
  const ver = p.ver || (String(p.pid).startsWith('v4') ? 4 : String(p.pid).startsWith('v2') ? 2 : 3);
  const inR = p.in_range;
  const lo = Math.min(p.tick_lower, p.tick_upper), hi = Math.max(p.tick_lower, p.tick_upper);
  const span = hi - lo || 1;
  const at = Math.max(0, Math.min(1, (p.cur_tick - lo) / span)) * 100;
  const pnl = p.pnl_usd;
  return `<div class="pos">
    <div class="top">
      <span class="badge v${ver}">v${ver}</span>
      <span class="name">${p.sym1 || ''} / ${p.sym0 || ''}</span>
      <span class="dim mono">${p.pid}</span>
      <span class="${inR ? 'ok' : 'bad'}">${inR ? '● in range' : '○ out of range'}</span>
      <span class="dim" style="margin-left:auto">${p.age || ''}</span>
    </div>
    <div class="bar"><i style="left:0;right:0"></i><u style="left:${at}%"></u></div>
    <div class="grid">
      <div><small>Value</small><span>${usd(p.value_usd)}</span></div>
      <div><small>Fee unclaimed</small><span class="ok">${usd(p.unclaimed_usd)}</span></div>
      <div><small>Deposit</small><span>${usd(p.deposit_usd)}</span></div>
      <div><small>PnL</small><span class="${pnl == null ? '' : pnl >= 0 ? 'ok' : 'bad'}">${pnl == null ? '—' : usd(pnl)}</span></div>
    </div>
    <div class="acts">
      <button data-act="add" data-pid="${p.pid}" data-ver="${ver}">➕ Add</button>
      <button data-act="reduce" data-pid="${p.pid}" data-ver="${ver}">➖ Reduce</button>
      ${ver === 2 ? '' : `<button data-act="collect" data-pid="${p.pid}" data-ver="${ver}">💰 Fee</button>
      <button data-act="rebalance" data-pid="${p.pid}" data-ver="${ver}">⚖️ Rebalance</button>`}
      <button class="danger" data-act="close" data-pid="${p.pid}" data-ver="${ver}">🗑 Close</button>
      ${p.link ? `<a href="${p.link}" target="_blank" rel="noopener" style="align-self:center;font-size:12px">↗ Uniswap</a>` : ''}
    </div>
  </div>`;
}

function posAction(pid, act, ver) {
  const run = extra => {
    closeModal();
    S.busy = true;
    toast(`<span class="spin"></span> ${act} ${pid}...`, 'hold');
    api('/api/action', {chain: S.chain, pid, action: act, ...extra})
      .then(r => {
        const steps = (r.steps || []).map(s => `${s.label}: <a href="${s.url}" target="_blank" rel="noopener">tx</a>`).join(' · ');
        const swaps = (r.swaps || []).map(s => `swap ${s.sym}: <a href="${s.url}" target="_blank" rel="noopener">tx</a>`).join(' · ');
        toast(`✅ <b>${act} ${pid} selesai</b><br>${steps}${swaps ? '<br>' + swaps : ''}` +
          (r.new_pid ? `<br>posisi baru: <b>${r.new_pid}</b>` : ''), 'ok');
        loadPositions();
      })
      .catch(e => toast(`❌ ${act} gagal: ${e.message}`, 'err'))
      .finally(() => { S.busy = false; });
  };
  const btns = list => `<div class="row">${list}<button onclick="closeModal()">Batal</button></div>`;

  if (act === 'add') {
    modal(`<h3>Add ke ${pid}</h3><div class="dim" style="font-size:13px">
      Jumlah dalam satuan <b>quote</b> (WETH/USDT/…). Token yang sudah ada di wallet dipakai duluan.</div>
      <div style="margin-top:12px"><input id="addv" class="mono" style="width:100%" placeholder="0.01"></div>
      ${btns('<button class="primary" id="ok">Add</button>')}`);
    $('#ok').onclick = () => {
      const v = parseFloat($('#addv').value);
      if (!(v > 0)) return toast('Jumlah tidak valid.', 'err');
      run({amount: v});
    };
  } else if (act === 'reduce') {
    modal(`<h3>Reduce ${pid}</h3><div class="dim" style="font-size:13px">
      Fee unclaimed ikut terambil.</div>${btns(
      [25, 50, 75, 100].map(p => `<button class="pctb" data-p="${p}">${p}%</button>`).join(''))}`);
    for (const b of $('#sheet').querySelectorAll('.pctb')) b.onclick = () => run({pct: +b.dataset.p});
  } else if (act === 'collect') {
    run({});
  } else if (act === 'rebalance') {
    modal(`<h3>Rebalance ${pid}</h3><div class="dim" style="font-size:13px">
      Close (fee ikut terambil) → swap komposisi → mint ulang dengan <b>lebar range sama</b>
      dipusatkan di harga sekarang. 3–5 transaksi.</div>${btns(
      ['wide', 'lower', 'upper'].map(m => `<button class="mb" data-m="${m}">${m}</button>`).join(''))}`);
    for (const b of $('#sheet').querySelectorAll('.mb')) b.onclick = () => run({mode: b.dataset.m});
  } else if (act === 'close') {
    modal(`<h3>Close ${pid}?</h3><div class="dim" style="font-size:13px">
      Tarik semua liquidity + fee. ${ver == 2 ? '' : 'Sisa token bisa di-swap otomatis ke wrapped native.'}</div>
      ${btns(`<button class="danger" id="ca">Close + auto-swap</button>
              <button id="cn">Close saja</button>`)}`);
    $('#ca').onclick = () => run({autoswap: true});
    $('#cn').onclick = () => run({autoswap: false});
  }
}

// ══════════ state global ══════════
async function loadState() {
  const s = await api('/api/state' + (TOKEN ? '?t=' + encodeURIComponent(TOKEN) : ''));
  S.chain = s.chain; S.settings = s.settings;
  $('#chain').innerHTML = s.chains.map(c =>
    `<option value="${c.id}"${c.id === s.chain ? ' selected' : ''}>${c.name}</option>`).join('');
  $('#wallet').innerHTML = s.wallets.map(w =>
    `<option value="${w.idx}"${w.idx === s.wallet_idx ? ' selected' : ''}>${w.label} ${w.address.slice(0, 6)}…${w.address.slice(-4)}</option>`).join('');
  $('#bal').textContent = `${amt(s.native)} ${s.native_sym} · ${amt(s.wrapped)} ${s.wrapped_sym}`;
  $('#slipTxt').textContent = s.settings.slippage_pct;
  S.amountPct = s.settings.amount_pct ?? 50;
  $('#amtPcts').innerHTML = [25, 50, 75, 100].map(p =>
    `<button data-p="${p}"${p === S.amountPct ? ' class="on"' : ''}>${p}%</button>`).join('');
  for (const b of $('#amtPcts').querySelectorAll('button')) {
    b.onclick = () => {
      S.amountPct = +b.dataset.p; S.amountFixed = null;
      for (const x of $('#amtPcts').querySelectorAll('button')) x.classList.toggle('on', x === b);
      refreshPreview();
    };
  }
}

// ══════════ wiring ══════════
$('#go').onclick = discover;
$('#ca').onkeydown = e => { if (e.key === 'Enter') discover(); };
$('#mint').onclick = doMint;
$('#closeEditor').onclick = () => { $('#editor').classList.add('hide'); S.pool = null; };
$('#amt').oninput = () => {
  const v = parseFloat($('#amt').value);
  S.amountFixed = v > 0 ? v : null;
  for (const x of $('#amtPcts').querySelectorAll('button')) x.classList.remove('on');
  clearTimeout(prevT); prevT = setTimeout(refreshPreview, 400);
};
for (const id of ['#minIn', '#maxIn']) {
  $(id).onchange = () => {
    const v = parseFloat($(id).value);
    if (!(v > 0)) return;
    if (id === '#minIn') S.min = v; else S.max = v;
    if (S.min > S.max) [S.min, S.max] = [S.max, S.min];
    syncFromDrag(true);
  };
}
for (const t of document.querySelectorAll('.tab')) {
  t.onclick = () => {
    for (const x of document.querySelectorAll('.tab')) x.classList.toggle('on', x === t);
    const pos = t.dataset.view === 'pos';
    $('#view-pool').classList.toggle('hide', pos);
    $('#view-pos').classList.toggle('hide', !pos);
    if (pos) loadPositions();
  };
}
$('#chain').onchange = async e => {
  await api('/api/settings', {chain: +e.target.value});
  location.reload();
};
$('#wallet').onchange = async e => {
  await api('/api/settings', {wallet_idx: +e.target.value});
  location.reload();
};
window.closeModal = closeModal;

loadState().catch(e => toast('Gagal konek server: ' + e.message, 'err'));
setInterval(() => {
  if (S.pool && !S.busy && !dragging && !document.hidden) loadCandles();
}, 30000);

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
  const a = Math.abs(v), s = v < 0 ? '-$' : '$';
  if (a >= 1e9) return s + nf(a / 1e9, 2) + 'B';
  if (a >= 1e6) return s + nf(a / 1e6, 2) + 'M';
  if (a >= 1e3) return s + nf(a / 1e3, 2) + 'K';
  return s + nf(a, a < 1 ? 4 : 2);
}

/* Harga meme sering 1e-7 ke bawah. toPrecision() memberi notasi eksponen
   ("8.26803e-7") yang tidak terbaca — selalu pakai desimal penuh. */
function price(v, sig = 4) {
  if (v == null || !isFinite(v)) return '—';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1000) return nf(v, 2);
  if (a >= 1) return nf(v, 4);
  const zeros = Math.max(0, Math.ceil(-Math.log10(a)) - 1);   // nol setelah koma
  return v.toFixed(Math.min(18, zeros + sig)).replace(/(\.\d*?[1-9])0+$/, '$1');
}

function amt(v) {
  if (v == null || !isFinite(v)) return '—';
  if (v === 0) return '0';
  if (Math.abs(v) >= 1000) return nf(v, 2);
  if (Math.abs(v) >= 1) return nf(v, 4);
  return price(v, 4);
}

const pctTxt = p => (p == null || !isFinite(p)) ? '' : (p >= 0 ? '+' : '') + p.toFixed(2) + '%';

let toastT;
function toast(html, kind) {
  const t = $('#toast');
  t.className = 'toast ' + (kind || '');
  t.innerHTML = html;
  clearTimeout(toastT);
  if (kind !== 'hold') toastT = setTimeout(() => t.classList.add('hide'), kind === 'err' ? 14000 : 9000);
}
const modal = html => { $('#sheet').innerHTML = html; $('#modal').classList.remove('hide'); };
const closeModal = () => $('#modal').classList.add('hide');
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

// ══════════ state ══════════
const S = {
  chain: null, settings: {}, token: null, pools: [], pool: null,
  tf: '15m', candles: [], min: null, max: null,
  /* Dua cara menentukan range:
     - preset aktif  → batas diturunkan dari harga sekarang (ikut bergerak),
                       server yang menghitung tick termasuk jarak aman dari harga
     - preset null    → range CUSTOM: batas absolut, ditaruh persis di mana user
                       menaruhnya. Boleh seluruhnya di bawah/atas harga sekarang
                       (mis. harga 60k, range 20k–40k) dan tidak akan ditarik
                       balik menempel harga. */
  preset: 'lower30', mode: 'lower', lowPct: 30, upPct: 30,
  preview: null, amountPct: 50, amountFixed: null, bars: [], busy: false,
  unit: 'mc',        // sumbu harga: market cap atau harga quote
  ana: 'liq',        // panel analytics: liquidity | volume
};

/* Market cap = harga(quote) × harga USD quote × total supply. Faktornya
   konstan, jadi persen range identik di kedua satuan — cuma tampilannya
   yang berubah, angka yang dikirim ke server tetap persen. */
const mcFactor = () => (S.unit === 'mc' && S.pool && S.pool.supply)
  ? S.pool.quote_usd * S.pool.supply : 1;
const toUnit = p => p * mcFactor();
const fromUnit = u => u / mcFactor();
const fmtUnit = v => S.unit === 'mc' && mcFactor() !== 1 ? usd(v) : price(v);

// ══════════ chart ══════════
let chart, series, lineMin, lineMax, overlay, dragging = null;

// Skala harga ikut melebar supaya garis MIN/MAX selalu kelihatan, walau
// range-nya jauh di luar rentang candle (mis. −50% single).
function autoscale(orig) {
  const r = orig();
  if (S.min == null && S.max == null) return r;
  const lo = Math.min(r ? r.priceRange.minValue : Infinity, toUnit(S.min) ?? Infinity);
  const hi = Math.max(r ? r.priceRange.maxValue : -Infinity, toUnit(S.max) ?? -Infinity);
  if (!isFinite(lo) || !isFinite(hi) || lo >= hi) return r;
  const pad = (hi - lo) * 0.05;
  // harga/market cap tidak pernah negatif — jangan sisakan ruang di bawah nol
  return {priceRange: {minValue: Math.max(lo - pad, lo * 0.5, 0), maxValue: hi + pad}};
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
    localization: {priceFormatter: fmtUnit},
    handleScale: {axisPressedMouseMove: {price: false}},
  });
  series = chart.addCandlestickSeries({
    upColor: '#39d98a', downColor: '#ff5c5c', borderVisible: false,
    wickUpColor: '#39d98a', wickDownColor: '#ff5c5c',
    priceFormat: {type: 'custom', formatter: fmtUnit, minMove: 1e-8},
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
      position: 'absolute', left: '0', right: '0', height: '20px', marginTop: '-10px',
      cursor: 'ns-resize', pointerEvents: 'auto', display: 'none',
    });
    h.innerHTML = `<div style="position:absolute;left:0;right:0;top:9px;height:2px;background:#c8ff2e"></div>
      <div style="position:absolute;left:6px;top:0;background:#c8ff2e;color:#0b0d0c;
        font:700 10px/20px ui-monospace,monospace;padding:0 8px;border-radius:4px">${k.toUpperCase()}</div>`;
    overlay.appendChild(h);
    h.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      try { h.setPointerCapture(ev.pointerId); } catch {}
      dragging = k;
    });
  }
  overlay.addEventListener('pointermove', ev => {
    if (!dragging) return;
    const y = ev.clientY - $('#chart').getBoundingClientRect().top;
    const u = series.coordinateToPrice(y);
    if (u == null || u <= 0) return;
    setBound(dragging, fromUnit(u));
    syncFromDrag(false);
  });
  const stop = () => { if (dragging) { dragging = null; commitDrag(); } };
  overlay.addEventListener('pointerup', stop);
  overlay.addEventListener('pointercancel', stop);

  chart.timeScale().subscribeVisibleTimeRangeChange(drawHandles);
  new ResizeObserver(() => {
    chart.applyOptions({width: el.clientWidth, height: el.clientHeight});
    drawHandles();
  }).observe(el);

  /* Skala harga chart dihitung ulang secara asinkron setelah applyOptions,
     jadi priceToCoordinate() yang dipanggil langsung memberi posisi lama —
     batang geser jadi tidak sejajar dengan garisnya (bisa meleset puluhan
     piksel). Posisikan ulang tiap frame supaya selalu menempel. */
  const tick = () => { drawHandles(); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}

/* Satu-satunya batasan: MIN < MAX dengan jarak minimal 1 tick-spacing.
   Letak range terhadap harga sekarang TIDAK dibatasi — range boleh sepenuhnya
   di bawah atau di atas harga. */
function setBound(which, v) {
  const sp = S.pool.tick_spacing || 60;
  const gapFrac = Math.pow(1.0001, sp) - 1;            // lebar 1 tick-spacing
  if (which === 'min') {
    if (S.max != null && v > S.max / (1 + gapFrac)) v = S.max / (1 + gapFrac);
    S.min = Math.max(v, 1e-30);
  } else {
    if (S.min != null && v < S.min * (1 + gapFrac)) v = S.min * (1 + gapFrac);
    S.max = v;
  }
}

function drawHandles() {
  if (!series || !overlay) return;
  for (const h of overlay.children) {
    const v = h.dataset.k === 'min' ? S.min : S.max;
    const y = v ? series.priceToCoordinate(toUnit(v)) : null;
    if (y == null) { if (h.style.display !== 'none') h.style.display = 'none'; continue; }
    const top = Math.round(y) + 'px';
    if (h.style.display !== 'block') h.style.display = 'block';
    if (h.style.top !== top) h.style.top = top;   // tulis hanya kalau berubah
  }
}

function setPriceLines() {
  if (!series) return;
  for (const l of [lineMin, lineMax]) if (l) series.removePriceLine(l);
  const mk = (v, t) => v ? series.createPriceLine({
    price: toUnit(v), color: '#c8ff2e', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: t,
  }) : null;
  lineMin = mk(S.min, 'MIN');
  lineMax = mk(S.max, 'MAX');
  series.applyOptions({autoscaleInfoProvider: autoscale});  // paksa hitung ulang skala
  drawHandles(); drawLiq();
}

// ══════════ histogram likuiditas horizontal (sumbu-x = harga/MC) ══════════
let liqDrag = null;

/* Jendela histogram ditentukan oleh sebaran likuiditas + harga sekarang, dan
   HANYA melebar untuk range yang tidak terlalu ekstrem. Kalau jendelanya
   dipaksa memuat batas −97%, semua bar mampat jadi garis rambut di satu sisi. */
function liqBounds() {
  const now = S.pool ? S.pool.price : 0;
  let lo = now, hi = now;
  for (const b of S.bars) { lo = Math.min(lo, b.p0); hi = Math.max(hi, b.p1); }
  if (!(lo > 0) || hi <= lo) { lo = now / 3; hi = now * 3; }
  // sisakan ruang untuk garis MIN/MAX, tapi jangan lebih dari 3× lebar likuiditas
  const span = Math.log(hi / lo);
  const cap = Math.exp(span * 1.5);
  if (S.min > 0) lo = Math.max(Math.min(lo, S.min * 0.9), hi / cap / 3);
  if (S.max > 0) hi = Math.min(Math.max(hi, S.max * 1.1), lo * cap * 3);
  return [Math.max(lo, now / 5000), Math.min(hi, now * 5000)];
}

// batas yang jatuh di luar jendela digambar menempel tepi, dengan tanda ‹ / ›
const clampLog = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function drawLiq() {
  const cv = $('#liq');
  if (!cv || !S.pool) return;
  const box = $('#liqwrap').getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, box.width), H = Math.max(1, box.height);
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const padB = 20, plotH = H - padB;

  if (S.pool.ver === 2) {
    g.fillStyle = 'rgba(200,255,46,.30)';
    g.fillRect(0, 0, W, plotH);
    g.fillStyle = '#7f8f85'; g.font = '11px system-ui'; g.textAlign = 'center';
    g.fillText('v2 = likuiditas merata di semua harga (full range)', W / 2, plotH / 2);
    return;
  }
  if (!S.bars.length) {
    g.fillStyle = '#3a463f'; g.font = '11px system-ui'; g.textAlign = 'center';
    g.fillText('peta likuiditas tidak tersedia', W / 2, plotH / 2);
    return;
  }

  const [lo, hi] = liqBounds();
  const lg = Math.log(lo), rg = Math.log(hi) - lg;      // skala log: sesuai sifat tick
  const X = p => (Math.log(Math.max(p, 1e-300)) - lg) / rg * W;
  const maxL = Math.max(...S.bars.map(b => b.liq)) || 1;
  const now = S.pool.price;

  for (const b of S.bars) {
    const x0 = X(b.p0), x1 = X(b.p1);
    if (x1 < 0 || x0 > W) continue;
    const h = Math.max(2, (b.liq / maxL) * (plotH - 6));
    const inR = S.min != null && S.max != null && b.p1 > S.min && b.p0 < S.max;
    g.fillStyle = inR ? 'rgba(200,255,46,.85)' : 'rgba(120,140,128,.35)';
    g.fillRect(x0, plotH - h, Math.max(1, x1 - x0 - 0.5), h);
  }

  // area range + garis harga sekarang
  if (S.min != null && S.max != null) {
    const xa = X(clampLog(S.min, lo, hi)), xb = X(clampLog(S.max, lo, hi));
    g.fillStyle = 'rgba(200,255,46,.07)';
    g.fillRect(xa, 0, Math.max(1, xb - xa), plotH);
    g.strokeStyle = '#c8ff2e'; g.lineWidth = 2;
    for (const [v, lbl] of [[S.min, 'MIN'], [S.max, 'MAX']]) {
      const off = v < lo ? '‹' : v > hi ? '›' : '';    // batas di luar jendela
      const x = Math.max(1, Math.min(W - 1, X(clampLog(v, lo, hi))));
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, plotH); g.stroke();
      g.fillStyle = '#c8ff2e';
      g.fillRect(x - 17, 0, 34, 13);
      g.fillStyle = '#0b0d0c'; g.font = '700 9px ui-monospace,monospace'; g.textAlign = 'center';
      g.fillText(off ? off + lbl : lbl, x, 10);
    }
  }
  const xn = X(now);
  g.strokeStyle = '#e8efe9'; g.lineWidth = 1;
  g.setLineDash([3, 3]);
  g.beginPath(); g.moveTo(xn, 0); g.lineTo(xn, plotH); g.stroke();
  g.setLineDash([]);

  // sumbu-x
  g.fillStyle = '#7f8f85'; g.font = '10px ui-monospace,monospace';
  g.textAlign = 'left'; g.fillText(fmtUnit(toUnit(lo)), 2, H - 6);
  g.textAlign = 'center'; g.fillText(fmtUnit(toUnit(now)), Math.max(40, Math.min(W - 40, xn)), H - 6);
  g.textAlign = 'right'; g.fillText(fmtUnit(toUnit(hi)), W - 2, H - 6);
  $('#liqnow').textContent = 'sekarang ' + fmtUnit(toUnit(now));
}

function liqPriceAt(clientX) {
  const box = $('#liqwrap').getBoundingClientRect();
  const [lo, hi] = liqBounds();
  const f = Math.max(0, Math.min(1, (clientX - box.left) / box.width));
  return Math.exp(Math.log(lo) + f * (Math.log(hi) - Math.log(lo)));
}

function initLiqDrag() {
  const w = $('#liqwrap');
  w.addEventListener('pointerdown', ev => {
    if (!S.pool || S.pool.ver === 2 || S.min == null) return;
    const p = liqPriceAt(ev.clientX);
    liqDrag = Math.abs(Math.log(p / S.min)) < Math.abs(Math.log(p / S.max)) ? 'min' : 'max';
    try { w.setPointerCapture(ev.pointerId); } catch {}
    setBound(liqDrag, p);
    syncFromDrag(false);
  });
  w.addEventListener('pointermove', ev => {
    if (!liqDrag) return;
    setBound(liqDrag, liqPriceAt(ev.clientX));
    syncFromDrag(false);
  });
  const stop = () => { if (liqDrag) { liqDrag = null; commitDrag(); } };
  w.addEventListener('pointerup', stop);
  w.addEventListener('pointercancel', stop);
  new ResizeObserver(drawLiq).observe(w);
}

// ══════════ analytics: liquidity / volume ══════════
function drawAna() {
  const cv = $('#ana');
  if (!cv || !S.pool) return;
  const box = $('#anawrap').getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, box.width), H = Math.max(1, box.height);
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const padB = 18, plotH = H - padB;

  if (S.ana === 'vol') {
    const c = S.candles;
    if (!c.length) { note(g, W, plotH, 'volume tidak tersedia'); return; }
    const q = S.pool.quote_usd || 0;
    const vols = c.map(x => x.volume * q);        // GeckoTerminal: volume dalam token quote
    const mx = Math.max(...vols) || 1;
    const bw = W / vols.length;
    vols.forEach((v, i) => {
      const h = (v / mx) * (plotH - 4);
      g.fillStyle = c[i].close >= c[i].open ? 'rgba(57,217,138,.75)' : 'rgba(255,92,92,.75)';
      g.fillRect(i * bw, plotH - h, Math.max(1, bw - 1), h);
    });
    const total = vols.reduce((a, b) => a + b, 0);
    $('#analeg').innerHTML = `${c.length} candle ${S.tf.toUpperCase()} · total ${usd(total)}
      · puncak ${usd(mx)} · rata-rata ${usd(total / vols.length)}`;
    g.fillStyle = '#7f8f85'; g.font = '10px ui-monospace,monospace';
    g.textAlign = 'left'; g.fillText(usd(mx), 2, 10);
    return;
  }

  // likuiditas kumulatif per sisi (berapa token nampung di tiap harga)
  if (!S.bars.length) { note(g, W, plotH, 'peta likuiditas tidak tersedia'); return; }
  const [lo, hi] = liqBounds();
  const lg = Math.log(lo), rg = Math.log(hi) - lg;
  const X = p => (Math.log(Math.max(p, 1e-300)) - lg) / rg * W;
  const now = S.pool.price;
  const maxL = Math.max(...S.bars.map(b => b.liq)) || 1;
  for (const b of S.bars) {
    const x0 = X(b.p0), x1 = X(b.p1);
    const h = Math.max(2, (b.liq / maxL) * (plotH - 4));
    // di bawah harga = sisi quote (nampung beli), di atas = sisi token (jual)
    g.fillStyle = b.p1 <= now ? 'rgba(90,169,255,.7)' : 'rgba(200,255,46,.7)';
    g.fillRect(x0, plotH - h, Math.max(1, x1 - x0 - 0.5), h);
  }
  const xn = X(now);
  g.strokeStyle = '#e8efe9'; g.lineWidth = 1;
  g.beginPath(); g.moveTo(xn, 0); g.lineTo(xn, plotH); g.stroke();
  g.fillStyle = '#7f8f85'; g.font = '10px ui-monospace,monospace';
  g.textAlign = 'left'; g.fillText(fmtUnit(toUnit(lo)), 2, H - 5);
  g.textAlign = 'right'; g.fillText(fmtUnit(toUnit(hi)), W - 2, H - 5);
  const q = S.pool.quote_sym, t = S.pool.token_sym;
  $('#analeg').innerHTML = `<span style="color:#5aa9ff">■</span> sisi ${q} (di bawah harga — nampung beli)
     · <span style="color:#c8ff2e">■</span> sisi ${t} (di atas harga — jual bertahap)
     · ${S.bars.length} tick-range aktif`;
}

function note(g, W, H, txt) {
  g.fillStyle = '#3a463f'; g.font = '11px system-ui'; g.textAlign = 'center';
  g.fillText(txt, W / 2, H / 2);
  $('#analeg').textContent = '';
}

// ══════════ range ⇄ mode ══════════
/* Sisi token yang disetor ditentukan oleh LETAK range terhadap harga, bukan
   oleh pilihan user. Server memakai aturan yang sama (effective_mode). */
function modeFromBounds(min, max) {
  const now = S.pool.price;
  if (max != null && max <= now) return 'lower';   // range di bawah harga → setor quote
  if (min != null && min >= now) return 'upper';   // range di atas harga  → setor token
  return 'wide';
}

/* Yang dikirim ke server. Preset aktif → mode + persen (server menambah jarak
   aman dari harga). Custom → batas harga apa adanya. */
function rangeReq() {
  if (S.preset) {
    return {mode: S.mode, low_pct: Math.max(0.01, Math.min(99, S.lowPct)),
            up_pct: Math.max(0.01, S.upPct)};
  }
  return {price_lower: S.min, price_upper: S.max};
}

function applyIntent(refresh = true) {
  const now = S.pool.price;
  if (S.mode === 'lower') { S.min = now * (1 - S.lowPct / 100); S.max = now; }
  else if (S.mode === 'upper') { S.min = now; S.max = now * (1 + S.upPct / 100); }
  else { S.min = now * (1 - S.lowPct / 100); S.max = now * (1 + S.upPct / 100); }
  syncFromDrag(refresh);
}

let prevT;
function syncFromDrag(final) {
  const now = S.pool.price;
  $('#minIn').value = fmtUnit(toUnit(S.min));
  $('#maxIn').value = fmtUnit(toUnit(S.max));
  setPct('#minPct', ((S.min ?? now) / now - 1) * 100);
  setPct('#maxPct', ((S.max ?? now) / now - 1) * 100);
  setPriceLines();
  clearTimeout(prevT);
  if (final !== null) prevT = setTimeout(refreshPreview, final ? 0 : 400);
}

/* Begitu user menggeser/mengetik sendiri, range jadi CUSTOM: batasnya absolut,
   tidak lagi diturunkan dari harga, jadi tidak akan tertarik balik ke harga. */
function commitDrag() {
  S.preset = null;
  S.mode = modeFromBounds(S.min, S.max);
  markPreset();
  syncFromDrag(true);
}

function markPreset() {
  for (const x of $('#presets').querySelectorAll('button')) {
    x.classList.toggle('on', S.preset ? x.dataset.id === S.preset : x.dataset.id === 'custom');
  }
}

function setPct(sel, v) {
  const el = $(sel);
  el.textContent = pctTxt(v);
  el.className = 'badge-pct ' + (v >= 0 ? 'up' : 'down');
}

function applyPreset(id, mode, low, up) {
  S.preset = id;
  S.mode = mode;
  if (low) S.lowPct = low;
  if (up) S.upPct = up;
  markPreset();
  applyIntent();
}

// Penjaga urutan: drag cepat bisa memicu beberapa /api/preview sekaligus.
// Respons yang datang telat harus dibuang — kalau tidak, range di layar
// (dan yang dipakai saat mint) bisa balik ke nilai lama.
let previewSeq = 0;

async function refreshPreview() {
  if (!S.pool) return;
  const seq = ++previewSeq;
  const poolKey = S.pool.key;
  const req = rangeReq();
  const body = {
    key: poolKey, ...req,
    amount_pct: S.amountFixed ? 0 : S.amountPct,
    amount_fixed: S.amountFixed, supply: S.pool.supply || 0,
  };
  $('#depinfo').innerHTML = '<span class="spin"></span> hitung...';
  try {
    const p = await api('/api/preview', body);
    if (seq !== previewSeq || !S.pool || S.pool.key !== poolKey) return;  // sudah basi
    p.req = req;                 // dipakai lagi saat mint, biar persis sama
    S.preview = p;
    S.mode = p.mode || S.mode;   // sisi deposit ditentukan server dari letak range
    setPrice(p.price);
    if (p.ver !== 2) {
      // snap ke tick asli hasil server (sudah dibulatkan ke tick spacing)
      S.min = p.price_lower; S.max = p.price_upper;
      $('#minIn').value = fmtUnit(toUnit(S.min));
      $('#maxIn').value = fmtUnit(toUnit(S.max));
      setPct('#minPct', p.pct_lower);
      setPct('#maxPct', p.pct_upper);
      setPriceLines();
    }
    renderDeposit(p);
  } catch (e) {
    if (seq !== previewSeq) return;
    $('#depinfo').innerHTML = '<span class="bad">' + e.message + '</span>';
    $('#mint').disabled = true;
  }
}

/* Harga bergerak terus. Kalau S.pool.price basi, persen range dihitung
   terhadap harga lama → preset −30% bisa tampil jadi −66%. Setiap respons
   server membawa harga terbaru; pakai itu. */
function setPrice(p, reanchor = false) {
  if (!(p > 0) || !S.pool) return;
  const moved = Math.abs(p / S.pool.price - 1);
  if (moved < 1e-9) return;
  S.pool.price = p;
  S.pool.price_usd = p * S.pool.quote_usd;
  renderStats();
  /* Preset ikut bergerak bersama harga (sisi single-sided tetap menempel harga).
     Range custom TIDAK PERNAH digeser — user menaruhnya di situ dengan sengaja. */
  if (reanchor && S.preset && moved > 0.0005 && !dragging && !liqDrag && S.pool.ver !== 2) {
    const now = S.pool.price;
    if (S.mode === 'lower') S.max = now;
    else if (S.mode === 'upper') S.min = now;
    syncFromDrag(null);   // gambar ulang saja, jangan picu preview beruntun
  }
  drawLiq(); drawAna();
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
    (p.mode === 'lower' ? `Range di bawah harga · deposit ${p.dep_sym} saja` :
     p.mode === 'upper' ? `Range di atas harga · deposit ${p.dep_sym} saja` :
     'Range mencakup harga sekarang · dua sisi');
  const rows = [];
  if (p.ver === 2) {
    rows.push('LP v2 full range 50/50 — fee 0.3% auto-compound.');
  } else {
    rows.push(MODE_TXT[p.mode] || MODE_TXT[S.mode]);
    const unit = S.unit === 'mc' && mcFactor() !== 1 ? 'market cap' : `${t.quote_sym}/${t.token_sym}`;
    rows.push(`Range (${unit}): <b>${fmtUnit(toUnit(p.price_lower))}</b> – <b>${fmtUnit(toUnit(p.price_upper))}</b>
      <span class="dim">(tick ${p.tick_lower} … ${p.tick_upper})</span>`);
    if (p.comp) rows.push(`Komposisi: <b>${amt(p.comp.quote)}</b> ${t.quote_sym} masuk pool` +
      (p.comp.swap > 0 ? ` · swap <b>${amt(p.comp.swap)}</b> ${t.quote_sym} → ${t.token_sym}` : ' · tanpa swap'));
  }
  rows.push(`Deposit: <b>${amt(p.amount)}</b> ${p.dep_sym} ≈ <b>${usd(p.usd)}</b>`);
  $('#depinfo').innerHTML = rows.join('<br>');
  $('#mint').disabled = !(p.amount > 0);
  $('#mint').textContent = p.amount > 0 ? `Mint position · ${amt(p.amount)} ${p.dep_sym}` : 'Saldo kosong';
}

function renderStats() {
  const p = S.pool;
  if (!p) return;
  $('#stats').innerHTML = `
    <div><small>Price (${p.quote_sym}/${p.token_sym})</small><b>${price(p.price)}</b></div>
    <div><small>Price USD</small><b>${usd(p.price_usd)}</b></div>
    <div><small>Market cap</small><b>${usd(p.price * p.quote_usd * (p.supply || 0)) }</b></div>
    <div><small>TVL</small><b>${usd(p.tvl_usd)}</b></div>
    <div><small>Volume 24H</small><b>${usd(p.vol24_usd)}</b></div>
    <div><small>Pool APR</small><b>${p.apr_pct ? nf(p.apr_pct, 1) + '%' : '—'}</b></div>`;
}

// ══════════ load pool ══════════
async function loadCandles() {
  $('#srcinfo').textContent = 'memuat chart...';
  try {
    const r = await api(`/api/candles?key=${encodeURIComponent(S.pool.key)}&tf=${S.tf}` +
                        (TOKEN ? `&t=${encodeURIComponent(TOKEN)}` : ''));
    S.candles = r.candles;
    if (r.quote_usd) S.pool.quote_usd = r.quote_usd;
    if (r.supply) S.pool.supply = r.supply;
    setPrice(r.price, true);
    applyCandles();
    $('#srcinfo').textContent = r.candles.length
      ? `${r.candles.length} candle · sumber: ${r.source}`
      : 'chart belum tersedia (pool terlalu baru / belum di-index)';
  } catch (e) {
    S.candles = [];
    series.setData([]);
    $('#srcinfo').textContent = 'chart gagal: ' + e.message;
  }
  drawHandles(); drawAna();
}

function applyCandles() {
  const f = mcFactor();
  series.setData(S.candles.map(c => ({
    time: c.time, open: c.open * f, high: c.high * f, low: c.low * f, close: c.close * f,
  })));
  if (S.candles.length) chart.timeScale().fitContent();
  setPriceLines();
}

async function loadLiq() {
  try {
    const r = await api('/api/liquidity', {key: S.pool.key});
    S.bars = r.bars || [];
    setPrice(r.price);
  } catch { S.bars = []; }
  drawLiq(); drawAna();
}

async function openPool(key) {
  $('#editor').classList.remove('hide');
  $('#poolEmpty').classList.add('hide');
  for (const el of document.querySelectorAll('.poolrow')) el.classList.toggle('on', el.dataset.key === key);
  initChart();
  const p = await api('/api/pool', {key});
  S.pool = p;
  $('#pair').textContent = `${p.token_sym} / ${p.quote_sym}`;
  $('#pairsub').innerHTML = `<span class="badge v${p.ver}">v${p.ver}</span> fee ${p.fee_pct.toFixed(2)}%
    · <span class="mono">${String(p.pool).slice(0, 10)}…${String(p.pool).slice(-6)}</span>`;
  renderStats();
  buildPresets();
  applyPreset('lower30', 'lower', 30, 30);
  await Promise.all([loadCandles(), loadLiq()]);
}

function buildPresets() {
  const v2 = S.pool.ver === 2;
  $('#presets').innerHTML = v2 ? '<span class="dim">v2 selalu full range</span>' : `
    <button data-id="lower10" data-m="lower" data-l="10">−10% single</button>
    <button data-id="lower20" data-m="lower" data-l="20">−20% single</button>
    <button data-id="lower30" data-m="lower" data-l="30">−30% single</button>
    <button data-id="lower50" data-m="lower" data-l="50">−50% single</button>
    <button data-id="upper20" data-m="upper" data-u="20">+20% single</button>
    <button data-id="upper50" data-m="upper" data-u="50">+50% single</button>
    <button data-id="wide5" data-m="wide" data-l="5" data-u="5">±5%</button>
    <button data-id="wide10" data-m="wide" data-l="10" data-u="10">±10%</button>
    <button data-id="wide20" data-m="wide" data-l="20" data-u="20">±20%</button>
    <button data-id="custom" class="customchip">custom</button>`;
  for (const b of $('#presets').querySelectorAll('button')) {
    b.onclick = () => {
      if (b.dataset.id === 'custom') { commitDrag(); return; }   // kunci batas saat ini
      applyPreset(b.dataset.id, b.dataset.m, +(b.dataset.l || 0), +(b.dataset.u || 0));
    };
  }
  markPreset();
  $('#tfs').innerHTML = ['1m', '5m', '15m', '1h', '4h', '1d']
    .map(t => `<button data-tf="${t}"${t === S.tf ? ' class="on"' : ''}>${t.toUpperCase()}</button>`).join('');
  for (const b of $('#tfs').querySelectorAll('button')) {
    b.onclick = () => {
      S.tf = b.dataset.tf;
      for (const x of $('#tfs').querySelectorAll('button')) x.classList.toggle('on', x === b);
      loadCandles();
    };
  }
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
        <div class="cell"><small>APR</small>${p.apr_pct ? nf(p.apr_pct, 1) + '%' : '—'}</div>
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
  const unit = S.unit === 'mc' && mcFactor() !== 1 ? 'market cap' : `${t.quote_sym}/${t.token_sym}`;
  const range = p.ver === 2 ? 'full range (v2)' :
    `${fmtUnit(toUnit(p.price_lower))} – ${fmtUnit(toUnit(p.price_upper))} (${unit})`;
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
      const req = p.req || rangeReq();
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
/* Pantauan live: selama tab Positions terbuka, data ditarik ulang berkala dan
   angkanya di-patch di tempat. Render ulang penuh hanya kalau daftar posisinya
   berubah — kalau tidak, kartu berkedip dan tombol kehilangan fokus. */
let posTimer = null, posSeq = 0;
const POLL_MS = 12000;

function posLive(state, txt) {
  $('#posLive').className = 'live ' + state;
  $('#posLiveTxt').textContent = txt;
}

async function loadPositions(fresh) {
  const seq = ++posSeq;
  if (!$('#poslist').querySelector('.pos'))
    $('#poslist').innerHTML = '<div class="empty"><span class="spin"></span> memuat posisi...</div>';
  if (fresh) { $('#posRefresh').disabled = true; posLive('busy', 'menyegarkan…'); }
  try {
    const r = await api('/api/positions?chain=' + S.chain + (fresh ? '&fresh=1' : '&ttl=8') +
                        (TOKEN ? '&t=' + encodeURIComponent(TOKEN) : ''));
    if (seq !== posSeq) return;
    if (r.building) {                        // build pertama masih jalan di server
      posLive('busy', 'memuat posisi…');
      if (!$('#poslist').querySelector('.pos'))
        $('#poslist').innerHTML = '<div class="empty"><span class="spin"></span> memuat posisi dari chain…</div>';
      setTimeout(() => loadPositions(), 3000);
      return;
    }
    renderPositions(r);
    const pending = r.pnl_pending || r.stale;
    posLive(pending ? 'busy' : 'on',
      `Live · ${r.summary.in_range}/${r.summary.open} in range` +
      (r.pnl_pending ? ' · menghitung PnL…' : r.stale ? ' · menyegarkan…' : ''));
    if (pending) setTimeout(() => loadPositions(), 4000);   // ambil PnL / hasil refresh latar
  } catch (e) {
    if (seq !== posSeq) return;
    posLive('off', 'terputus: ' + e.message);
    if (!$('#poslist').querySelector('.pos'))
      $('#poslist').innerHTML = `<div class="empty bad">${e.message}</div>`;
  } finally {
    $('#posRefresh').disabled = false;
  }
}

function renderPositions(r) {
  const s = r.summary;
  const net = s.withdrawals + s.fees_claimed - s.deposits;
  setField('#pnl', 'total', usd(s.total_value));
  setField('#pnl', 'pnl', s.pnl == null ? '—' : usd(s.pnl), (s.pnl ?? 0) >= 0 ? 'ok' : 'bad');
  setField('#pnl', 'uncl', usd(s.unclaimed), 'ok');
  setField('#pnl', 'claimed', usd(s.fees_claimed));
  setField('#pnl', 'open', String(s.open));
  setField('#pnl', 'inrange', `${s.in_range} / ${s.open}`);
  if (!$('#pnl').querySelector('[data-f]')) {
    $('#pnl').innerHTML = `
      <div><small>Total value</small><b data-f="total">${usd(s.total_value)}</b></div>
      <div><small>PnL</small><b data-f="pnl" class="${(s.pnl ?? 0) >= 0 ? 'ok' : 'bad'}">${s.pnl == null ? '—' : usd(s.pnl)}</b></div>
      <div><small>Fee unclaimed</small><b data-f="uncl" class="ok">${usd(s.unclaimed)}</b></div>
      <div><small>Fee terklaim</small><b data-f="claimed">${usd(s.fees_claimed)}</b></div>
      <div><small>Posisi terbuka</small><b data-f="open">${s.open}</b></div>
      <div><small>In range</small><b data-f="inrange">${s.in_range} / ${s.open}</b></div>`;
  }
  const upd = `diperbarui ${new Date((r.ts || Date.now() / 1000) * 1000).toLocaleTimeString('id-ID')}`;
  const srcTag = `<span title="daftar posisi lengkap dari API Uniswap (menangkap posisi lama & mint di luar bot), detail + in/out-range dibaca langsung dari chain via RPC-mu. Bot Telegram pakai sumber yang sama.">sumber: chain (daftar via Uniswap)</span>`;
  $('#posSub').innerHTML = srcTag
    + ` · realized ${usd(net)} (deposit ${usd(s.deposits)}, withdraw ${usd(s.withdrawals)}) · ${upd}`;

  S.lastPositions = r.positions;
  loadOrders();
  if (!r.positions.length) {
    $('#poslist').innerHTML = '<div class="empty">Belum ada posisi aktif.</div>';
    return;
  }
  const ids = r.positions.map(p => p.pid).join(',');
  if (ids !== $('#poslist').dataset.ids) {
    $('#poslist').dataset.ids = ids;
    $('#poslist').innerHTML = r.positions.map(posCard).join('');
    for (const b of $('#poslist').querySelectorAll('button[data-act]')) {
      b.onclick = () => posAction(b.dataset.pid, b.dataset.act, b.dataset.ver);
    }
    return;
  }
  for (const p of r.positions) patchCard(p);
}

// tulis hanya kalau isinya berubah, lalu kedipkan sebentar
function setField(scope, name, val, cls) {
  const el = document.querySelector(`${scope} [data-f="${name}"]`);
  if (!el || el.textContent === val) return;
  el.textContent = val;
  if (cls !== undefined) el.className = cls;
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}

function patchCard(p) {
  const sc = `#pos-${cssId(p.pid)}`;
  const card = document.querySelector(sc);
  if (!card) return;
  const pnl = p.pnl_usd;
  const pnlPct = (pnl != null && p.deposit_usd) ? (pnl / p.deposit_usd * 100) : null;
  setField(sc, 'value', usd(p.value_usd));
  setField(sc, 'uncl', usd(p.unclaimed_usd));
  setField(sc, 'dep', p.deposit_usd ? usd(p.deposit_usd) : '—');
  setField(sc, 'claimed', usd(p.fees_claimed_usd));
  setField(sc, 'qamt', amt(p.quote_amount));
  setField(sc, 'mamt', amt(p.meme_amount));
  setField(sc, 'age', p.age || '');
  setField(sc, 'pnl', pnl == null ? 'PnL belum tercatat'
    : `${pnl >= 0 ? '▲' : '▼'} ${usd(pnl)}${pnlPct == null ? '' : ` (${pctTxt(pnlPct)})`}`,
    `tag ${pnl == null ? 'neutral' : pnl >= 0 ? 'up' : 'down'}`);
  setField(sc, 'inrange', p.in_range ? '● in range' : '○ out of range',
    `tag ${p.in_range ? 'in' : 'out'}`);
  setField(sc, 'note', rangeNote(p));
  const bar = card.querySelector('.rbar');
  if (bar && p.price_lower != null) {
    const lo = Math.log(p.price_lower), hi = Math.log(p.price_upper);
    const at = Math.max(0, Math.min(1, (Math.log(p.price_now) - lo) / (hi - lo || 1)));
    bar.querySelector('b').style.left = at * 100 + '%';
    [...bar.querySelectorAll('u')].forEach((u, i) => {
      u.className = ((i + .5) / 34 <= at) ? 'a' : 'b';
    });
  }
}

const cssId = pid => String(pid).replace(/[^a-zA-Z0-9]/g, '_');

function rangeNote(p) {
  if (p.ver === 2 || p.price_lower == null) return 'full range';
  return p.in_range
    ? `in range · ${nf(p.to_min_pct, 1)}% ke min · ${nf(p.to_max_pct, 1)}% ke max`
    : (p.price_now > p.price_upper
        ? `out of range · harga ${nf((p.price_now / p.price_upper - 1) * 100, 1)}% di atas range`
        : `out of range · harga ${nf((1 - p.price_now / p.price_lower) * 100, 1)}% di bawah range`);
}

function startPosPolling() {
  stopPosPolling();
  posTimer = setInterval(() => {
    if (!S.busy && !document.hidden && !$('#view-pos').classList.contains('hide')) loadPositions();
  }, POLL_MS);
}
function stopPosPolling() { if (posTimer) { clearInterval(posTimer); posTimer = null; } }

/* Bar range ala UI LP: kotak-kotak dari MIN ke MAX, penanda harga sekarang.
   Bagian yang "terpakai" (sisi token yang masih dipegang) diwarnai. */
function rangeBar(p) {
  if (p.ver === 2 || p.price_lower == null) {
    return `<div class="rbar full"><i style="left:0;right:0"></i></div>
      <div class="rbar-lbl dim"><span>0</span><span>full range (v2)</span><span>∞</span></div>`;
  }
  const lo = Math.log(p.price_lower), hi = Math.log(p.price_upper);
  const at = Math.max(0, Math.min(1, (Math.log(p.price_now) - lo) / (hi - lo || 1)));
  let cells = '';
  for (let i = 0; i < 34; i++) {
    const f = (i + .5) / 34;
    cells += `<u class="${f <= at ? 'a' : 'b'}"></u>`;
  }
  return `<div class="rbar">${cells}<b style="left:${at * 100}%"></b></div>
    <div class="rbar-lbl">
      <span class="mono">${price(p.price_lower)}</span>
      <span class="dim">${p.quote_sym}/${p.meme_sym}</span>
      <span class="mono">${price(p.price_upper)}</span>
    </div>`;
}

function posCard(p) {
  const ver = p.ver || (String(p.pid).startsWith('v4') ? 4 : String(p.pid).startsWith('v2') ? 2 : 3);
  const pnl = p.pnl_usd;
  const pnlPct = (pnl != null && p.deposit_usd) ? (pnl / p.deposit_usd * 100) : null;
  const pnlTxt = pnl == null ? 'PnL belum tercatat'
    : `${pnl >= 0 ? '▲' : '▼'} ${usd(pnl)}${pnlPct == null ? '' : ` (${pctTxt(pnlPct)})`}`;
  return `<div class="pos" id="pos-${cssId(p.pid)}">
    <div class="top">
      <span class="name">${p.quote_sym || p.sym1} / ${p.meme_sym || p.sym0}</span>
      <span class="badge v${ver}">v${ver}</span>
      <span class="chip">${(p.fee / 10000).toFixed(2)}%</span>
      <span class="chip mono">${p.pid}</span>
      <span class="dim" data-f="age" style="margin-left:auto">${p.age || ''}</span>
    </div>
    <div class="tags">
      <span data-f="pnl" class="tag ${pnl == null ? 'neutral' : pnl >= 0 ? 'up' : 'down'}" title="${p.basis === 'onchain'
        ? 'modal dari event on-chain, dinilai pada harga sekarang (sudah termasuk impermanent loss)'
        : 'dari riwayat lokal bot'}">${pnlTxt}</span>
      <span data-f="inrange" class="tag ${p.in_range ? 'in' : 'out'}">${p.in_range ? '● in range' : '○ out of range'}</span>
    </div>
    ${rangeBar(p)}
    <div class="posnote dim" data-f="note">${rangeNote(p)}</div>
    <div class="kv">
      <div><span>Value</span><b data-f="value">${usd(p.value_usd)}</b></div>
      <div><span>Deposited</span><b data-f="dep">${p.deposit_usd ? usd(p.deposit_usd) : '—'}</b></div>
      <div><span>Fee unclaimed</span><b data-f="uncl" class="ok">${usd(p.unclaimed_usd)}</b></div>
      <div><span>Fee terklaim</span><b data-f="claimed">${usd(p.fees_claimed_usd)}</b></div>
      <div><span>${p.quote_sym || 'quote'}</span><b data-f="qamt" class="mono">${amt(p.quote_amount)}</b></div>
      <div><span>${p.meme_sym || 'token'}</span><b data-f="mamt" class="mono">${amt(p.meme_amount)}</b></div>
    </div>
    <div class="acts">
      ${ver === 2 ? '' : `<button class="primary sm" data-act="rebalance" data-pid="${p.pid}" data-ver="${ver}">⚖️ Rebalance</button>
      <button data-act="collect" data-pid="${p.pid}" data-ver="${ver}">💰 Collect fees</button>
      <button data-act="tpsl" data-pid="${p.pid}" data-ver="${ver}">🎯 TP/SL</button>`}
      <button data-act="add" data-pid="${p.pid}" data-ver="${ver}">➕ Add</button>
      <button data-act="reduce" data-pid="${p.pid}" data-ver="${ver}">➖ Reduce</button>
      <button class="danger" data-act="close" data-pid="${p.pid}" data-ver="${ver}">Close</button>
      ${p.link ? `<a class="btnlink" href="${p.link}" target="_blank" rel="noopener">↗ Uniswap</a>` : ''}
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
      ${btns(`<button class="danger" id="cga">Close + auto-swap</button>
              <button id="cgn">Close saja</button>`)}`);
    $('#cga').onclick = () => run({autoswap: true});
    $('#cgn').onclick = () => run({autoswap: false});
  } else if (act === 'tpsl') {
    const p = (S.lastPositions || []).find(x => String(x.pid) === String(pid));
    const mc = (p && p.mc_now) ? p.mc_now : 0;
    const swOn = !!(S.settings && S.settings.autoswap);
    modal(`<h3>TP/SL ${pid}</h3>
      <div class="dim" style="font-size:13px">Auto-close posisi saat <b>market cap</b> sentuh batas.
        ${mc ? `MC sekarang <b>${usd(mc)}</b>.` : ''} Kosongkan sisi yang tak dipakai.
        Eksekusi dijalankan bot (harus aktif).</div>
      <div style="margin-top:12px;display:grid;gap:10px">
        <label class="ordf">TP · close saat MC ≥ <input id="tpv" class="mono" inputmode="decimal" placeholder="mis. 800k"></label>
        <label class="ordf">SL · close saat MC ≤ <input id="slv" class="mono" inputmode="decimal" placeholder="mis. 200k"></label>
        <label class="ordsw"><input type="checkbox" id="swv"${swOn ? ' checked' : ''}> auto-swap hasil ke wrapped native saat trigger</label>
      </div>
      ${btns('<button class="primary" id="ok">Buat pesanan</button>')}`);
    $('#ok').onclick = () => {
      const tp = parseHuman($('#tpv').value), sl = parseHuman($('#slv').value);
      if (tp == null && sl == null) return toast('Isi minimal TP atau SL.', 'err');
      if (mc > 0 && tp != null && tp <= mc) return toast('TP harus > MC sekarang.', 'err');
      if (mc > 0 && sl != null && sl >= mc) return toast('SL harus < MC sekarang.', 'err');
      if (tp != null && sl != null && sl >= tp) return toast('SL harus < TP.', 'err');
      closeModal();
      S.busy = true;
      toast('<span class="spin"></span> buat pesanan...', 'hold');
      api('/api/order', {chain: S.chain, action: 'add', pid, tp_mc: tp, sl_mc: sl, autoswap: $('#swv').checked})
        .then(r => { toast(`✅ Pesanan <b>${r.id}</b> dibuat`, 'ok'); loadOrders(); })
        .catch(e => toast('❌ ' + e.message, 'err'))
        .finally(() => { S.busy = false; });
    };
  }
}

// ── Pesanan TP/SL (eksekusi oleh bot; web hanya buat/batal + tampil) ──
function parseHuman(s) {
  s = (s || '').trim().toLowerCase().replace(/[$,\s]/g, '');
  if (!s || s === '-') return null;
  const mult = {k: 1e3, m: 1e6, b: 1e9}[s.slice(-1)];
  const v = parseFloat(mult ? s.slice(0, -1) : s);
  return isFinite(v) ? v * (mult || 1) : null;
}

async function loadOrders() {
  try {
    renderOrders(await api('/api/orders?chain=' + S.chain));
  } catch (e) { /* panel opsional — diamkan */ }
}

function renderOrders(r) {
  const panel = $('#ordersPanel');
  const active = (r && r.active) || [];
  if (!active.length) { panel.classList.add('hide'); panel.innerHTML = ''; return; }
  panel.classList.remove('hide');
  panel.innerHTML = `<div class="orders-head">🎯 Pesanan TP/SL aktif · ${active.length}</div>` +
    active.map(o => `<div class="order">
      <span class="chip mono">${o.id}</span>
      <span class="oname">${o.meme_sym || ''} <span class="mono dim">${o.pid}</span></span>
      <span class="tag up">TP ${o.tp_mc ? usd(o.tp_mc) : '—'}</span>
      <span class="tag down">SL ${o.sl_mc ? usd(o.sl_mc) : '—'}</span>
      <span class="dim">${o.autoswap ? 'swap' : 'tahan'}</span>
      <button class="danger sm" data-cancel="${o.id}" title="batal">✖</button>
    </div>`).join('');
  for (const b of panel.querySelectorAll('button[data-cancel]')) {
    b.onclick = () => cancelOrder(b.dataset.cancel);
  }
}

function cancelOrder(id) {
  S.busy = true;
  toast('<span class="spin"></span> batal ' + id + '...', 'hold');
  api('/api/order', {chain: S.chain, action: 'cancel', id})
    .then(() => { toast('Pesanan ' + id + ' dibatalkan', 'ok'); loadOrders(); })
    .catch(e => toast('❌ ' + e.message, 'err'))
    .finally(() => { S.busy = false; });
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
$('#posRefresh').onclick = () => loadPositions(true);
$('#closeEditor').onclick = () => { $('#editor').classList.add('hide'); S.pool = null; };
$('#amt').oninput = () => {
  const v = parseFloat($('#amt').value);
  S.amountFixed = v > 0 ? v : null;
  for (const x of $('#amtPcts').querySelectorAll('button')) x.classList.remove('on');
  clearTimeout(prevT); prevT = setTimeout(refreshPreview, 450);
};
for (const id of ['#minIn', '#maxIn']) {
  $(id).onchange = () => {
    const raw = String($(id).value).trim();
    let v = parseFloat(raw.replace(/[$,\s]/g, ''));
    const suf = raw.slice(-1).toUpperCase();          // terima "12.5M" / "800K"
    if (suf === 'K') v *= 1e3; else if (suf === 'M') v *= 1e6; else if (suf === 'B') v *= 1e9;
    if (!(v > 0)) return;
    setBound(id === '#minIn' ? 'min' : 'max', fromUnit(v));
    commitDrag();
  };
}
for (const b of $('#unitsw').querySelectorAll('button')) {
  b.onclick = () => {
    S.unit = b.dataset.u;
    for (const x of $('#unitsw').querySelectorAll('button')) x.classList.toggle('on', x === b);
    if (S.pool) { applyCandles(); syncFromDrag(true); drawAna(); }
  };
}
for (const b of $('#anaSw').querySelectorAll('button')) {
  b.onclick = () => {
    S.ana = b.dataset.a;
    for (const x of $('#anaSw').querySelectorAll('button')) x.classList.toggle('on', x === b);
    drawAna();
  };
}
for (const t of document.querySelectorAll('.tab')) {
  t.onclick = () => {
    for (const x of document.querySelectorAll('.tab')) x.classList.toggle('on', x === t);
    const pos = t.dataset.view === 'pos';
    $('#view-pool').classList.toggle('hide', pos);
    $('#view-pos').classList.toggle('hide', !pos);
    if (pos) { loadPositions(); startPosPolling(); } else stopPosPolling();
  };
}
// jeda polling saat tab browser disembunyikan, sambung lagi saat kembali
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !$('#view-pos').classList.contains('hide')) loadPositions();
});
$('#chain').onchange = async e => {
  await api('/api/settings', {chain: +e.target.value});
  location.reload();
};
$('#wallet').onchange = async e => {
  await api('/api/settings', {wallet_idx: +e.target.value});
  location.reload();
};
window.closeModal = closeModal;

initLiqDrag();
new ResizeObserver(drawAna).observe($('#anawrap'));
loadState().catch(e => toast('Gagal konek server: ' + e.message, 'err'));
setInterval(() => {
  if (S.pool && !S.busy && !dragging && !liqDrag && !document.hidden) loadCandles();
}, 30000);

// ============== HEALTH TAB ==============

// shared sample data (hourly day energy, 0..23)
// Shown in place of any chart that has no data. Never put placeholder numbers
// here: a health dashboard that invents plausible values is worse than one
// that admits the gap.
function renderChartEmpty(svg, w, h, msg){
  if (!svg) return;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML =
    `<text x="${w / 2}" y="${h / 2}" fill="var(--ink-3)" font-family="JetBrains Mono"` +
    ` font-size="12" text-anchor="middle" dominant-baseline="middle"` +
    ` letter-spacing="0.12em">${msg || 'DATA UNAVAILABLE'}</text>`;
}

// 15-day biocharge history (oldest first; last is yesterday); each entry: { start, end, date }
// Filled from /api/biocharge on load. Never seed this with sample
// numbers: an empty chart is honest, a fake one is not.
const BIO_HISTORY = [];

// sleep 7d data
// Filled from /api/sleep/recent on load. Intentionally empty.
const SLEEP_7D = [];

// (The hardcoded HYPNO_STAGES sample night was removed on 2026-08-29: it was
// rendered whenever a night had no hypnogram, and looked exactly like measured
// sleep. renderHypnogram() now shows an explicit empty state instead.)

// ----- range toggle -----
let bioRange = 7;
document.querySelectorAll('#bio-range-toggle button').forEach(b => {
  b.addEventListener('click', () => {
    bioRange = parseInt(b.dataset.range, 10);
    document.querySelectorAll('#bio-range-toggle button').forEach(x =>
      x.classList.toggle('active', x.dataset.range === String(bioRange)));
    renderBioTrend();
    const meta = document.getElementById('bio-trend-meta');
    if (meta) meta.textContent = (LANG === 'pt' ? `\u00daLTIMOS ${bioRange} DIAS` : `LAST ${bioRange} DAYS`);
  });
});

// ----- swipe between sub-tabs, falling through to main tabs -----
// Left/right swipe steps through a screen's own sub-tabs (Health, Finance).
// Each swipe is a discrete gesture, decided once at touchend — not a fight
// over a continuous drag — so the rule is simple: move the local sub-tab if
// there is one to move to; only at the local edge (or on a screen with no
// sub-tabs at all) does the same swipe step to the adjacent main tab.
function stepMainTab(dir) {   // dir: -1 = prev, +1 = next
  // offsetParent excludes Productivity, which is display:none on mobile
  // (desktop-only tab) — without it, a swipe off the last Health sub-tab
  // lands on that hidden tab's blank screen instead of the visible next one.
  const tabs = [...document.querySelectorAll('.bottom-tabs .tab')]
    .filter(b => !b.disabled && b.offsetParent !== null);
  const i = tabs.findIndex(b => b.classList.contains('active'));
  if (i < 0) return;
  const next = tabs[i + dir];
  if (next) next.click();
}
function initSubtabSwipe(screenId, tabSelector) {
  const screen = document.getElementById(screenId);
  if (!screen) return;
  const THRESHOLD = 55;   // px of horizontal travel before it counts
  const RATIO = 1.7;      // must be this much more horizontal than vertical
  let x0 = 0, y0 = 0, tracking = false;

  // Charts scrub on drag and several rows scroll sideways — a swipe that
  // starts on one of those belongs to that element, not to the tab strip.
  function startsOnHorizontalThing(el) {
    for (let n = el; n && n !== screen; n = n.parentElement) {
      if (n.tagName === 'SVG' || n.tagName === 'svg') return true;
      if (n.scrollWidth > n.clientWidth + 4) return true;
      const ta = getComputedStyle(n).touchAction;
      if (ta === 'pan-y' || ta === 'none') return true;
    }
    return false;
  }

  screen.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) { tracking = false; return; }
    if (startsOnHorizontalThing(e.target)) { tracking = false; return; }
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; tracking = true;
  }, { passive: true });

  screen.addEventListener('touchend', e => {
    if (!tracking) return;
    tracking = false;
    const t = e.changedTouches[0];
    const dx = t.clientX - x0, dy = t.clientY - y0;
    if (Math.abs(dx) < THRESHOLD || Math.abs(dx) < Math.abs(dy) * RATIO) return;
    const dir = dx < 0 ? 1 : -1;   // swipe left = forward
    if (!tabSelector) { stepMainTab(dir); return; }
    // queried per swipe: the finance sub-tabs are rebuilt on every render
    const tabs = [...screen.querySelectorAll(tabSelector)].filter(b => !b.disabled);
    const i = tabs.findIndex(b => b.classList.contains('active'));
    if (i < 0) return;             // locked/loading view — nothing active, don't guess
    const next = tabs[i + dir];
    if (next) next.click();
    else stepMainTab(dir);         // already at the local edge — hand off to main tabs
  }, { passive: true });
}
initSubtabSwipe('screen-health',       '#health-subtabs .subtab');
initSubtabSwipe('screen-finance',      '.fin-subtabs .fin-subtab');
initSubtabSwipe('screen-main',         null);
initSubtabSwipe('screen-workout',      null);
initSubtabSwipe('screen-productivity', null);
initSubtabSwipe('screen-auspex',       null);

// ----- health sub-tabs -----
document.querySelectorAll('#health-subtabs .subtab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#health-subtabs .subtab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const which = btn.dataset.hsubtab;
    document.getElementById('screen-health').dataset.accent = which;
    document.querySelectorAll('[data-hsub-panel]').forEach(p => {
      p.style.display = p.dataset.hsubPanel === which ? 'block' : 'none';
    });
    if (which === 'biocharge') {
      renderBioDay();
      renderBioTrend();
      rerenderBedtimeCurve();
      rerenderBioSteps();
    } else if (which === 'sleep') {
      renderHypnogram();
      renderSleepTime();
      renderSleepReg();
      renderSleepHr();
    } else if (which === 'vitals') {
      rerenderVitalDays();
    } else if (which === 'food') {
      rerenderFood();
    }
  });
});

// ----- Bio day strip (mini line) -----
function renderBioDay(){
  renderChartEmpty(document.getElementById('bio-day-line'), 600, 80);
}
document.getElementById('bio-day-expand').addEventListener('click', () => {
  openDayEnergyModal();
});

// ----- Bio day modal -----
const dayModal = document.getElementById('day-energy-modal');
function openDayEnergyModal(){
  // Opens on whatever day the header has selected — no reset to today.
  renderDayEnergyBig();
  dayModal.classList.add('show');
}
document.getElementById('day-energy-close').addEventListener('click', () => dayModal.classList.remove('show'));
dayModal.addEventListener('click', e => { if (e.target === dayModal) dayModal.classList.remove('show'); });
document.addEventListener('keydown', e => {
  if (dayModal.classList.contains('show') && e.key === 'Escape') dayModal.classList.remove('show');
});
backGestureFor(dayModal, () => dayModal.classList.contains('show'), () => dayModal.classList.remove('show'));
function renderDayEnergyBig(){
  renderChartEmpty(document.getElementById('day-energy-chart'), 800, 320);
  const sub = document.getElementById('day-modal-sub');
  if (sub) sub.textContent = 'No data for this day';
  ['day-foot-wake', 'day-foot-mid', 'day-foot-low'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = '—';
  });
}

// ----- Bio trend bars (7d / 15d) -----
let bioTrendBarData = [];
function renderBioTrend(){
  const svg = document.getElementById('bio-trend-chart');
  if (!svg) return;
  if (!BIO_HISTORY.length) return renderChartEmpty(svg, 700, 200);
  const data = BIO_HISTORY.slice(-bioRange);
  const avg    = Math.round(data.reduce((a, d) => a + d.max, 0) / data.length);
  const avgMin = Math.round(data.reduce((a, d) => a + d.min, 0) / data.length);
  const avgEl = document.getElementById('bio-trend-avg');
  if (avgEl) avgEl.innerHTML = `${bioRange}-day avg <strong>${avg}</strong> max · <strong>${avgMin}</strong> min`;

  // Drawn at the SVG's real pixel size (1 unit = 1px) so labels stay 12px at any width.
  const rect = svg.getBoundingClientRect();
  const W = Math.round(rect.width) || 1000, H = Math.round(rect.height) || 260;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const P = { l: 34, r: 16, t: 14, b: 28 };
  const innerW = W - P.l - P.r, innerH = H - P.t - P.b;
  const n = data.length;
  const xAt = i => P.l + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const yAt = v => P.t + (1 - v / 100) * innerH;
  const dayLbl = d => { const m = String(d.day).split(' '); return m.length === 2 ? `${m[1]} ${m[0]}` : d.day; };

  let grid = '';
  [0, 25, 50, 75, 100].forEach(v => {
    grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yAt(v)}" y2="${yAt(v)}" stroke="rgb(255 255 255 / 0.07)"/>`;
    grid += `<text class="axis-text" x="${P.l - 8}" y="${yAt(v) + 4}" text-anchor="end">${v}</text>`;
  });
  const step = Math.ceil(n / Math.max(2, Math.floor(innerW / 70)));
  let labels = '';
  data.forEach((d, i) => {
    if ((n - 1 - i) % step === 0) labels += `<text class="axis-text" x="${xAt(i)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle')}">${dayLbl(d)}</text>`;
  });

  // Daily max/min as a continuous range band (a collapsed day shows up as a pinch).
  const top = data.map((d, i) => [xAt(i), yAt(d.max)]), bot = data.map((d, i) => [xAt(i), yAt(d.min)]);
  bioTrendBarData = data.map((d, i) => ({ cx: xAt(i), maxY: yAt(d.max), minY: yAt(d.min), max: d.max, min: d.min, day: dayLbl(d) }));
  // Catmull-Rom → cubic bezier, so the envelope is smooth without overshooting.
  const segs = pts => {
    let s = '';
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      s += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return s;
  };
  const M = p => `M${p[0].toFixed(1)},${p[1].toFixed(1)}`;
  const smooth = pts => M(pts[0]) + segs(pts);
  const botRev = bot.slice().reverse();
  let band = '', edges = '';
  if (n > 1) {
    band = `<path d="${smooth(top)} L${botRev[0][0].toFixed(1)},${botRev[0][1].toFixed(1)}${segs(botRev)} Z" fill="var(--accent)" fill-opacity="0.12"/>`;
    edges = `<path class="ln" pathLength="1" d="${smooth(top)}" fill="none" stroke="var(--accent)" stroke-width="2.25" stroke-linecap="round"/>`
          + `<path class="ln" pathLength="1" d="${smooth(bot)}" fill="none" stroke="var(--accent-2)" stroke-width="2.25" stroke-linecap="round"/>`;
  }
  let dots = '';
  top.forEach((t, i) => {
    dots += `<line x1="${t[0]}" y1="${t[1]}" x2="${bot[i][0]}" y2="${bot[i][1]}" stroke="rgb(255 255 255 / 0.10)"/>`;
    dots += `<circle cx="${t[0]}" cy="${t[1]}" r="3.5" fill="var(--panel)" stroke="var(--accent)" stroke-width="2"/>`;
    dots += `<circle cx="${bot[i][0]}" cy="${bot[i][1]}" r="3.5" fill="var(--panel)" stroke="var(--accent-2)" stroke-width="2"/>`;
  });
  const avgLine    = `<line x1="${P.l}" x2="${W - P.r}" y1="${yAt(avg)}"    y2="${yAt(avg)}"    stroke="var(--accent)"    stroke-opacity="0.45" stroke-dasharray="4 5"/>`;
  const avgMinLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${yAt(avgMin)}" y2="${yAt(avgMin)}" stroke="var(--accent-2)" stroke-opacity="0.6"  stroke-dasharray="4 5"/>`;

  svg.innerHTML = `${grid}${labels}${avgLine}${avgMinLine}${band}${edges}${dots}<g id="bio-trend-hover-g" pointer-events="none" style="display:none;"></g>`;
  svg._trendW = W;
  svg.classList.toggle('on-in', !svg._drawn); svg._drawn = true;
  if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, renderBioTrend);
}

// trend chart hover / touch: a guide line through the day, emphasised endpoints and a tooltip
(function setupTrendHover() {
  const svgEl = document.getElementById('bio-trend-chart');
  if (!svgEl) return;
  const T = window.actaTip(svgEl.parentElement, svgEl);
  const show = (clientX) => {
    if (!bioTrendBarData.length) return;
    const rect = svgEl.getBoundingClientRect();
    const mouseX = clientX - rect.left;
    let best = bioTrendBarData[0], bestDist = Infinity;
    bioTrendBarData.forEach(b => { const d = Math.abs(b.cx - mouseX); if (d < bestDist) { bestDist = d; best = b; } });
    const hg = document.getElementById('bio-trend-hover-g');
    if (!hg) return;
    hg.style.display = '';
    hg.innerHTML = `
      <line x1="${best.cx}" y1="${best.maxY}" x2="${best.cx}" y2="${best.minY}" stroke="var(--accent)" stroke-width="2" opacity="0.85"/>
      <circle cx="${best.cx}" cy="${best.maxY}" r="5" fill="var(--accent)" stroke="var(--panel)" stroke-width="2"/>
      <circle cx="${best.cx}" cy="${best.minY}" r="5" fill="var(--accent-2)" stroke="var(--panel)" stroke-width="2"/>`;
    T.show(`<b>${best.day}</b> · max ${best.max} · min ${best.min}`, best.cx, best.maxY);
  };
  const hide = () => {
    const hg = document.getElementById('bio-trend-hover-g'); if (hg) hg.style.display = 'none';
    T.hide();
  };
  svgEl.addEventListener('pointermove', e => show(e.clientX));
  svgEl.addEventListener('pointerdown', e => show(e.clientX));
  svgEl.addEventListener('pointerleave', hide);
})();

// ----- Hypnogram -----
function renderHypnogram() {
  const svg = document.getElementById('hypnogram');
  if (!svg) return;
  // viewBox width tracks real pixel width so preserveAspectRatio="none" maps
  // 1 unit = 1px horizontally too — otherwise labels stretch ~2.3× wide
  const W = Math.round(svg.getBoundingClientRect().width) || 800;
  const H = 220, P = { l: 52, r: 8, t: 6, b: 26 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const innerW = W - P.l - P.r;
  const innerH = H - P.t - P.b;
  const rowH = innerH / 4;

  const stageOrder  = ['awake', 'rem', 'light', 'deep'];
  const stageLabels = ['Awake', 'REM', 'Light', 'Deep'];
  const stageColors = {
    deep:  'oklch(0.62 0.16 290)',
    rem:   'oklch(0.58 0.15 255)',
    light: 'oklch(0.78 0.10 215)',
    awake: 'oklch(0.70 0.14 25)',
    // Sleep recovered from the activity stream, which carries no stage
    // information. Drawn in the light lane because it is sleep, but muted so
    // it never reads as a measured stage.
    unstaged: 'oklch(0.62 0.03 228)',
  };
  // 'unstaged' shares the light lane; it is not a fifth row.
  const laneOf = (st) => (st === 'unstaged' ? 'light' : st);
  const bandClass = ['awake', 'rem', 'light', 'deep'];

  const xFor = (t, total) => P.l + (t / total) * innerW;
  const yFor = (s) => P.t + stageOrder.indexOf(laneOf(s)) * rowH + rowH / 2;

  // Bands + labels
  let bands = '';
  stageOrder.forEach((s, i) => {
    bands += `<rect x="${P.l}" y="${P.t + i * rowH}" width="${innerW}" height="${rowH}" class="hypno-band ${bandClass[i]}"/>`;
    bands += `<text x="${P.l - 10}" y="${P.t + i * rowH + rowH / 2 + 4}" class="hypno-label" text-anchor="end">${stageLabels[i]}</text>`;
  });

  // Grid
  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const y = P.t + i * rowH;
    grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="hypno-grid"/>`;
  }

  const data = liveHypnoStages;

  if (!data || !data.length) {
    // No hypnogram for this night. This used to draw a hardcoded 7-cycle
    // sample night (462 min, labelled from 23:14) that was indistinguishable
    // from real data — precisely what renderChartEmpty() exists to prevent:
    // a health dashboard that invents plausible values is worse than one that
    // admits the gap. An empty chart is the honest answer.
    renderChartEmpty(svg, W, H, 'No sleep data');
    return;
  }

  // Real data: clock-time axis on even hours, rounded stage blocks joined by thin
  // connectors, hover tooltip (as in the mockup).
  const totalMin = data[data.length - 1].end_min;
  const t0Ms = data[0].start_ts, t1Ms = t0Ms + totalMin * 60000;
  let timeLabels = '';
  let tk = new Date(t0Ms); tk.setMinutes(0, 0, 0); tk = tk.getTime();
  while (tk < t0Ms || new Date(tk).getHours() % 2) tk += 3600000;
  for (; tk <= t1Ms; tk += 7200000) {
    const x = P.l + ((tk - t0Ms) / 60000 / totalMin) * innerW;
    timeLabels += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${P.t}" y2="${P.t + innerH}" stroke="rgb(255 255 255 / 0.05)"/>`
                + `<text x="${x.toFixed(1)}" y="${H - 6}" class="hypno-time" text-anchor="middle">${pad(new Date(tk).getHours())}:00</text>`;
  }

  const bh = Math.min(14, rowH - 8);
  let lines = '', connectors = '';
  data.forEach((seg, i) => {
    const x1 = xFor(seg.start_min, totalMin);
    const x2 = xFor(seg.end_min, totalMin);
    const y  = yFor(seg.stage);
    const color = stageColors[seg.stage] || stageColors.awake;
    lines += `<rect x="${x1.toFixed(1)}" y="${(y - bh / 2).toFixed(1)}" width="${Math.max(3, x2 - x1).toFixed(1)}" height="${bh}" rx="3" fill="${color}"/>`;
    if (i < data.length - 1) {
      const nextY = yFor(data[i + 1].stage);
      connectors += `<line x1="${x2.toFixed(1)}" y1="${y.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${nextY.toFixed(1)}" stroke="rgb(255 255 255 / 0.22)"/>`;
    }
  });

  // Transparent hit areas for the hover tooltip
  let hitAreas = '';
  data.forEach((seg) => {
    const x1 = xFor(seg.start_min, totalMin);
    const x2 = xFor(seg.end_min, totalMin);
    const cy = yFor(seg.stage);
    const sd = new Date(seg.start_ts), ed = new Date(seg.end_ts);
    const startStr = `${pad(sd.getHours())}:${pad(sd.getMinutes())}`;
    const endStr   = `${pad(ed.getHours())}:${pad(ed.getMinutes())}`;
    const dur = seg.end_min - seg.start_min;
    const durStr = dur >= 60 ? `${Math.floor(dur / 60)}h ${pad(dur % 60)}m` : `${dur} min`;
    const name = seg.stage === 'rem' ? 'REM' : (seg.stage === 'unstaged' ? 'Unstaged' : seg.stage.charAt(0).toUpperCase() + seg.stage.slice(1));
    hitAreas += `<rect x="${x1.toFixed(1)}" y="${P.t}" width="${Math.max(2, x2 - x1).toFixed(1)}" height="${innerH}" fill="transparent" class="hypno-hit" data-name="${name}" data-start="${startStr}" data-end="${endStr}" data-dur="${durStr}" data-cx="${((x1 + x2) / 2).toFixed(1)}" data-cy="${cy.toFixed(1)}"/>`;
  });

  svg.innerHTML = `${bands}${timeLabels}${connectors}${lines}${hitAreas}`;
  if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, renderHypnogram);

  const T = window.actaTip(svg.parentElement, svg);
  svg.querySelectorAll('.hypno-hit').forEach(el => {
    el.addEventListener('pointerenter', () => {
      T.show(`<b>${el.dataset.name}</b> · ${el.dataset.start}–${el.dataset.end} · ${el.dataset.dur}`, parseFloat(el.dataset.cx), parseFloat(el.dataset.cy));
    });
    el.addEventListener('pointerleave', () => T.hide());
  });
}

// ----- Sleep mini charts -----
function renderSleepTime(){
  const svg = document.getElementById('sleep-time-chart');
  if (svg && !SLEEP_7D.length) return renderChartEmpty(svg, 700, 200);
  if (!svg) return;
  const _rc = svg.getBoundingClientRect();
  const W = Math.round(_rc.width) || 400, H = Math.round(_rc.height) || 160, P = { l: 30, r: 8, t: 12, b: 26 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, renderSleepTime);
  const innerW = W - P.l - P.r, innerH = H - P.t - P.b;
  const maxH = 10;
  const slot = innerW / SLEEP_7D.length;
  const barW = Math.min(30, slot * 0.6);

  const mean = SLEEP_7D.reduce((a, d) => a + d.dur, 0) / SLEEP_7D.length;
  const meanY = P.t + innerH - (mean / maxH) * innerH;

  // gridlines
  let grid = '';
  [0, 4, 8].forEach(v => {
    const y = P.t + innerH - (v / maxH) * innerH;
    grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="bg-line"/>`;
    grid += `<text x="${P.l - 6}" y="${y + 3}" class="axis-text" text-anchor="end">${v}h</text>`;
  });
  // bars: last night = light purple, all previous = dark purple
  const lastIdx = SLEEP_7D.length - 1;
  let bars = '', labels = '', hits = '';
  SLEEP_7D.forEach((d, i) => {
    const cx = P.l + slot * i + slot / 2;
    const h = (d.dur / maxH) * innerH;
    const y = P.t + innerH - h;
    bars += `<rect x="${cx - barW/2}" y="${y}" width="${barW}" height="${h}" fill="${i === lastIdx ? 'var(--hue-sleep)' : 'var(--hue-sleep-2)'}" rx="3"/>`;
    labels += `<text x="${cx}" y="${H - P.b + 14}" class="axis-text" text-anchor="middle">${d.day}</text>`;
    // full-height transparent hit area per day (same idiom as the HR chart)
    hits += `<rect x="${(cx - slot/2).toFixed(1)}" y="${P.t}" width="${slot.toFixed(1)}" height="${innerH}" fill="transparent" class="st-hit" data-dur="${d.dur}" data-day="${d.day}"/>`;
  });
  // mean line
  const meanLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${meanY.toFixed(1)}" y2="${meanY.toFixed(1)}" stroke="var(--ink-2)" stroke-opacity="0.7" stroke-dasharray="4 5"/>`;

  svg.innerHTML = `${grid}${meanLine}${bars}${hits}${labels}`;

  const hm = v => `${Math.floor(v)}h ${pad(Math.round((v - Math.floor(v)) * 60))}m`;
  const meanStr = hm(mean);
  const avgEl = document.getElementById('sleep-time-avg');
  if (avgEl) avgEl.textContent = meanStr;
  const meanLbl = document.getElementById('sleep-time-mean-label');
  if (meanLbl) meanLbl.textContent = `Mean · ${meanStr}`;

  // Hover: the legend's left cell becomes a per-day readout, then restores to
  // the 7-day average on leave — same pattern as the HR-during-sleep chart.
  const lblEl = document.getElementById('sleep-time-lbl');
  const _T = window.actaTip(svg.parentElement, svg);
  svg.querySelectorAll('.st-hit').forEach(el => {
    el.addEventListener('pointerenter', () => _T.show(`<b>${el.dataset.day}</b> · ${hm(parseFloat(el.dataset.dur))}`, +el.getAttribute('x') + +el.getAttribute('width') / 2, P.t + innerH - (parseFloat(el.dataset.dur) / maxH) * innerH));
    el.addEventListener('pointerleave', () => _T.hide());
    el.addEventListener('mouseenter', () => {
      if (lblEl) lblEl.textContent = `Time of sleep · ${el.dataset.day}`;
      if (avgEl) avgEl.textContent = hm(parseFloat(el.dataset.dur));
    });
    el.addEventListener('mouseleave', () => {
      if (lblEl) lblEl.textContent = '7-day avg';
      if (avgEl) avgEl.textContent = meanStr;
    });
  });
}

function renderSleepReg(){
  const svg = document.getElementById('sleep-reg-chart');
  if (svg && !SLEEP_7D.length) return renderChartEmpty(svg, 700, 200);
  if (!svg) return;
  const _rc = svg.getBoundingClientRect();
  const W = Math.round(_rc.width) || 400, H = Math.round(_rc.height) || 160, P = { l: 30, r: 8, t: 12, b: 26 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, renderSleepReg);
  const innerW = W - P.l - P.r, innerH = H - P.t - P.b;
  // Y axis spans 22h on day N-1 → 9h on day N. We map y = (hour - 22) / 11 within a 24-hour wrap
  // Use a virtual hour scale 22 → 33 (i.e., 22, 23, 24/0, ..., 9)
  function toVirtual(h){ return h >= 22 ? h : h + 24; }
  const yMin = 22, yMax = 33; // 22 to 9 next day
  const yFor = (h) => P.t + ((toVirtual(h) - yMin) / (yMax - yMin)) * innerH;

  // gridlines at 23, 1, 3, 5, 7
  let grid = '';
  [23, 25, 27, 29, 31, 33].forEach(v => {
    const y = P.t + ((v - yMin) / (yMax - yMin)) * innerH;
    grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="bg-line"/>`;
    const labelH = v >= 24 ? v - 24 : v;
    grid += `<text x="${P.l - 6}" y="${y + 3}" class="axis-text" text-anchor="end">${pad(labelH)}h</text>`;
  });
  const slot = innerW / SLEEP_7D.length;
  const barW = Math.min(14, slot - 8);

  // Mean bed and wake lines — average in virtual-hour space to handle midnight crossing
  const validReg = SLEEP_7D.filter(d => d.bedH > 0 && d.wakeH > 0);
  let meanLines = '';
  if (validReg.length) {
    const meanBedV  = validReg.reduce((a, d) => a + toVirtual(d.bedH),  0) / validReg.length;
    const meanWakeV = validReg.reduce((a, d) => a + toVirtual(d.wakeH), 0) / validReg.length;
    const mbY = (P.t + ((meanBedV  - yMin) / (yMax - yMin)) * innerH).toFixed(1);
    const mwY = (P.t + ((meanWakeV - yMin) / (yMax - yMin)) * innerH).toFixed(1);
    meanLines = `
      <line x1="${P.l}" x2="${W - P.r}" y1="${mbY}" y2="${mbY}" stroke="var(--hue-sleep)" stroke-width="1" stroke-dasharray="4 5" opacity="0.5"/>
      <line x1="${P.l}" x2="${W - P.r}" y1="${mwY}" y2="${mwY}" stroke="var(--hue-mental)" stroke-width="1" stroke-dasharray="4 5" opacity="0.5"/>`;
  }

  let bars = '', labels = '', hits = '';
  SLEEP_7D.forEach((d, i) => {
    const cx = P.l + slot * i + slot / 2;
    const bedY  = yFor(d.bedH);
    const wakeY = yFor(d.wakeH);
    bars += `<rect x="${cx - 3}" y="${bedY}" width="6" height="${Math.max(0, wakeY - bedY)}" rx="3" fill="var(--hue-sleep)" fill-opacity="0.22"/>`;
    bars += `<circle cx="${cx}" cy="${bedY}" r="4.5" fill="var(--hue-sleep)"/><circle cx="${cx}" cy="${wakeY}" r="4.5" fill="var(--hue-mental)"/>`;
    labels += `<text x="${cx}" y="${H - P.b + 14}" class="axis-text" text-anchor="middle">${d.day}</text>`;
    hits += `<rect x="${(cx - slot/2).toFixed(1)}" y="${P.t}" width="${slot.toFixed(1)}" height="${innerH}" fill="transparent" class="sr-hit" data-bed="${d.bedH}" data-wake="${d.wakeH}" data-day="${d.day}"/>`;
  });
  svg.innerHTML = `${grid}${meanLines}${bars}${hits}${labels}`;

  // Legend doubles as the readout: means by default, hovered night on hover.
  // Decimal hours → HH:MM (23.05 → 23:03), so it reads like a clock, not a number.
  const clock = h => {
    const hh = Math.floor(h), mm = Math.round((h - hh) * 60);
    return `${pad(mm === 60 ? hh + 1 : hh)}:${pad(mm === 60 ? 0 : mm)}`;
  };
  const bedEl  = document.getElementById('sleep-reg-bed');
  const wakeEl = document.getElementById('sleep-reg-wake');
  const regLbl = document.getElementById('sleep-reg-lbl');
  const baseBed  = validReg.length ? clock(((validReg.reduce((a, d) => a + toVirtual(d.bedH),  0) / validReg.length) % 24)) : '—';
  const baseWake = validReg.length ? clock(((validReg.reduce((a, d) => a + toVirtual(d.wakeH), 0) / validReg.length) % 24)) : '—';
  const resetReg = () => {
    if (bedEl)  bedEl.textContent  = baseBed;
    if (wakeEl) wakeEl.textContent = baseWake;
    if (regLbl) regLbl.textContent = 'Mean';
  };
  resetReg();
  const _T = window.actaTip(svg.parentElement, svg);
  svg.querySelectorAll('.sr-hit').forEach(el => {
    el.addEventListener('pointerenter', () => { const b = parseFloat(el.dataset.bed), w = parseFloat(el.dataset.wake); _T.show(`<b>${el.dataset.day}</b> · bed ${b > 0 ? clock(b) : '—'} · wake ${w > 0 ? clock(w) : '—'}`, +el.getAttribute('x') + +el.getAttribute('width') / 2, b > 0 ? yFor(b) : P.t); });
    el.addEventListener('pointerleave', () => _T.hide());
    el.addEventListener('mouseenter', () => {
      const b = parseFloat(el.dataset.bed), w = parseFloat(el.dataset.wake);
      if (bedEl)  bedEl.textContent  = b > 0 ? clock(b) : '—';
      if (wakeEl) wakeEl.textContent = w > 0 ? clock(w) : '—';
      if (regLbl) regLbl.textContent = el.dataset.day;
    });
    el.addEventListener('mouseleave', resetReg);
  });
}

function renderSleepHr(){
  const svg = document.getElementById('sleep-hr-chart');
  if (svg && !SLEEP_7D.length) return renderChartEmpty(svg, 700, 200);
  if (!svg) return;
  const _rc = svg.getBoundingClientRect();
  const W = Math.round(_rc.width) || 400, H = Math.round(_rc.height) || 160, P = { l: 30, r: 8, t: 12, b: 26 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, renderSleepHr);
  const innerW = W - P.l - P.r, innerH = H - P.t - P.b;

  const valid = SLEEP_7D.filter(d => d.hrMin > 0 && d.hrMax > 0);
  if (!valid.length) { svg.innerHTML = ''; return; }

  const allMin = Math.min(...valid.map(d => d.hrMin));
  const allMax = Math.max(...valid.map(d => d.hrMax));
  const pad4 = 4;
  const yMin = Math.floor((allMin - pad4) / 5) * 5;
  const yMax = Math.ceil((allMax + pad4) / 5) * 5;
  const yFor = (v) => P.t + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  // Grid — 5 evenly spaced lines
  let grid = '';
  const step = Math.round((yMax - yMin) / 4 / 5) * 5 || 5;
  for (let v = yMin; v <= yMax; v += step) {
    const y = yFor(v);
    grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="bg-line"/>`;
    grid += `<text x="${P.l - 6}" y="${y + 3}" class="axis-text" text-anchor="end">${v}</text>`;
  }

  const slot = innerW / SLEEP_7D.length;
  const xs = SLEEP_7D.map((_, i) => P.l + slot * i + slot / 2);

  // Connecting polylines for min and max
  const minLine = SLEEP_7D
    .map((d, i) => d.hrMin > 0 ? `${xs[i].toFixed(1)},${yFor(d.hrMin).toFixed(1)}` : null)
    .filter(Boolean).join(' ');
  const maxLine = SLEEP_7D
    .map((d, i) => d.hrMax > 0 ? `${xs[i].toFixed(1)},${yFor(d.hrMax).toFixed(1)}` : null)
    .filter(Boolean).join(' ');

  // Mean lines
  const meanMin = Math.round(valid.reduce((a, d) => a + d.hrMin, 0) / valid.length);
  const meanMax = Math.round(valid.reduce((a, d) => a + d.hrMax, 0) / valid.length);
  const meanMinLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${yFor(meanMin).toFixed(1)}" y2="${yFor(meanMin).toFixed(1)}" stroke="var(--hue-sleep)" stroke-width="1" stroke-dasharray="4 5" opacity="0.5"/>`;
  const meanMaxLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${yFor(meanMax).toFixed(1)}" y2="${yFor(meanMax).toFixed(1)}" stroke="var(--hue-vitals)" stroke-width="1" stroke-dasharray="4 5" opacity="0.5"/>`;

  let ranges = '';
  SLEEP_7D.forEach((d, i) => {
    if (!d.hrMin || !d.hrMax) return;
    const cx = xs[i];
    ranges += `<line x1="${cx}" x2="${cx}" y1="${yFor(d.hrMin).toFixed(1)}" y2="${yFor(d.hrMax).toFixed(1)}" stroke="rgb(255 255 255 / 0.10)"/>`;
    ranges += `<circle cx="${cx}" cy="${yFor(d.hrMax).toFixed(1)}" r="3.5" fill="var(--panel)" stroke="var(--hue-vitals)" stroke-width="2"/>`;
    ranges += `<circle cx="${cx}" cy="${yFor(d.hrMin).toFixed(1)}" r="3.5" fill="var(--panel)" stroke="var(--hue-sleep)" stroke-width="2"/>`;
  });

  let labels = '';
  SLEEP_7D.forEach((d, i) => {
    labels += `<text x="${xs[i]}" y="${H - P.b + 14}" class="axis-text" text-anchor="middle">${d.day}</text>`;
  });

  // Transparent per-day hit areas for hover
  let hits = '';
  SLEEP_7D.forEach((d, i) => {
    if (!d.hrMin || !d.hrMax) return;
    hits += `<rect x="${(xs[i] - slot/2).toFixed(1)}" y="${P.t}" width="${slot.toFixed(1)}" height="${innerH}" fill="transparent" class="hr-hit" data-min="${d.hrMin}" data-max="${d.hrMax}" data-day="${d.day}"/>`;
  });

  const smoothHr = key => {
    const pp = SLEEP_7D.map((d, i) => d[key] > 0 ? [xs[i], yFor(d[key])] : null).filter(Boolean);
    if (pp.length < 2) return '';
    let dd = 'M' + pp[0][0].toFixed(1) + ' ' + pp[0][1].toFixed(1);
    for (let i = 0; i < pp.length - 1; i++) {
      const p0 = pp[i - 1] || pp[i], p1 = pp[i], p2 = pp[i + 1], p3 = pp[i + 2] || p2;
      dd += `C${(p1[0] + (p2[0] - p0[0]) / 6).toFixed(1)} ${(p1[1] + (p2[1] - p0[1]) / 6).toFixed(1)} ${(p2[0] - (p3[0] - p1[0]) / 6).toFixed(1)} ${(p2[1] - (p3[1] - p1[1]) / 6).toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
    }
    return dd;
  };
  const polyMin = `<path d="${smoothHr('hrMin')}" fill="none" stroke="var(--hue-sleep)" stroke-width="2.25" stroke-linecap="round"/>`;
  const polyMax = `<path d="${smoothHr('hrMax')}" fill="none" stroke="var(--hue-vitals)" stroke-width="2.25" stroke-linecap="round"/>`;

  svg.innerHTML = `${grid}${polyMin}${polyMax}${meanMinLine}${meanMaxLine}${ranges}${hits}${labels}`;

  const minEl = document.getElementById('sleep-hr-min');
  const maxEl = document.getElementById('sleep-hr-max');
  // Default display = means; hover updates to per-day, restores to means on leave
  const baseMin = meanMin, baseMax = meanMax;
  if (minEl) minEl.textContent = baseMin;
  if (maxEl) maxEl.textContent = baseMax;

  // Hover: show hovered day's values in the legend corner
  const _T = window.actaTip(svg.parentElement, svg);
  svg.querySelectorAll('.hr-hit').forEach(el => {
    el.addEventListener('pointerenter', () => _T.show(`<b>${el.dataset.day}</b> · min ${el.dataset.min} · max ${el.dataset.max} bpm`, +el.getAttribute('x') + +el.getAttribute('width') / 2, yFor(parseFloat(el.dataset.max))));
    el.addEventListener('pointerleave', () => _T.hide());
    el.addEventListener('mouseenter', () => {
      if (minEl) minEl.textContent = el.dataset.min;
      if (maxEl) maxEl.textContent = el.dataset.max;
    });
    el.addEventListener('mouseleave', () => {
      if (minEl) minEl.textContent = baseMin;
      if (maxEl) maxEl.textContent = baseMax;
    });
  });
}

// initial health render (when tab is shown)
// Wire tab button to also kick off render
document.querySelector('.bottom-tabs .tab[data-tab="health"]').addEventListener('click', () => {
  setTimeout(() => {
    renderBioDay();
    renderBioTrend();
    rerenderBedtimeCurve();
    rerenderVitalDays();
    rerenderBioSteps();
  }, 50);
});

(function initLive() {
  const CIRC = 301.593; // 2 * π * 48

  function p2(n) { return String(n).padStart(2, '0'); }
  // Read a timestamp in the zone that was active when it was recorded, not the
  // phone's current zone — otherwise a night slept abroad reads an hour or more
  // off once you fly home. offMin null => fall back to the phone's zone.
  function tzDate(ms, offMin) {
    if (offMin == null) return new Date(ms);
    return new Date(ms + offMin * 60000 + new Date(ms).getTimezoneOffset() * 60000);
  }
  function fmtMin(ms, offMin) {
    const d = tzDate(ms, offMin);
    return p2(d.getHours()) + ':' + p2(d.getMinutes());
  }
  function scoreLabel(s) {
    return s >= 85 ? 'Restorative' : s >= 70 ? 'Good' : s >= 55 ? 'Fair' : 'Poor';
  }
  function fmtHM(min) {
    const h = Math.floor(min / 60), m = min % 60;
    return h > 0
      ? `${h}<span class="unit">h</span> ${p2(m)}<span class="unit">m</span>`
      : `${m}<span class="unit">m</span>`;
  }

  // ── Main tab: biocharge ring + state rows ─────────────────
  // `root` scopes the update. Defaults to the whole document (live path: Main
  // screen + Health/Biocharge tab both show today). The past-day path passes
  // #screen-health, so browsing history never rewrites the Main screen's ring —
  // Main is always "right now", regardless of which day Health is showing.
  function applyMainBio(level, startLevel, root, fresh) {
    const scope = root || document;
    // Two rings now exist in the DOM (Main screen + Health/Biocharge tab) —
    // update every match via class, not a single id.
    // circumference from each ring's own radius (the Main ring is r=60, the Health one r=48)
    const circOf = c => 2 * Math.PI * c.r.baseVal.value;

    // No real data covers "now" yet (e.g. no sync since last night) — the
    // pipeline's last stored row is from an earlier day. Showing it as the
    // current level reads as live when it is hours stale, so say so plainly
    // instead (2026-09-22). `fresh` is only passed on the live path; the
    // day-navigator path (loadDayView) never passes it, so past days are
    // unaffected — they're already handled by their own 404/empty logic.
    if (fresh === false) {
      scope.querySelectorAll('.bio-ring-prog, .bio-ring-start').forEach(ring => {
        const C = circOf(ring);
        ring.setAttribute('stroke-dasharray', `0 ${C.toFixed(2)}`);
      });
      scope.querySelectorAll('.bio-ring .center .num').forEach(numEl => { numEl.textContent = '—'; });
      scope.querySelectorAll('.bio-state .row .v').forEach(v => { v.innerHTML = '—<span class="small">/ 100</span>'; });
      const heroChip = document.getElementById('main-bio-chip');
      if (heroChip) { heroChip.textContent = 'No data · please upload'; heroChip.className = 'hero-chip warn'; }
      const snapNow = document.getElementById('snap-bio-now');
      if (snapNow) snapNow.innerHTML = `—<span class="unit">/ 100</span>`;
      const snapLabel = document.getElementById('snap-bio-label');
      if (snapLabel) { snapLabel.textContent = 'No data'; snapLabel.dataset.state = 'warn'; }
      return;
    }

    scope.querySelectorAll('.bio-ring-prog').forEach(prog => { const C = circOf(prog); prog.setAttribute('stroke-dasharray', `${((level / 100) * C).toFixed(2)} ${C.toFixed(2)}`); });

    if (startLevel != null) {
      scope.querySelectorAll('.bio-ring-start').forEach(startRing => { const C = circOf(startRing); startRing.setAttribute('stroke-dasharray', `${((startLevel / 100) * C).toFixed(2)} ${C.toFixed(2)}`); });
    }

    scope.querySelectorAll('.bio-ring .center .num').forEach(numEl => {
      numEl.textContent = Math.round(level);
    });

    scope.querySelectorAll('.bio-state').forEach(stateEl => {
      const rows = stateEl.querySelectorAll('.row');
      if (rows[0]) {
        const v = rows[0].querySelector('.v');
        if (v) v.innerHTML = `${Math.round(level)}<span class="small">/ 100</span>`;
      }
      if (rows[1] && startLevel != null) {
        const v = rows[1].querySelector('.v');
        if (v) v.innerHTML = `${Math.round(startLevel)}<span class="small">/ 100</span>`;
      }
    });

    const heroChip = document.getElementById('main-bio-chip');
    if (heroChip && startLevel > 0) {
      const r = level / startLevel;
      const st = r >= 0.85 ? ['Charged', 'Fully recovered', ''] : r >= 0.70 ? ['Good', 'On track', ''] : r >= 0.50 ? ['Normal', 'Natural decline', ''] : r >= 0.30 ? ['Winding down', 'Slow down, reduce effort', 'warn'] : ['Low', 'Rest needed', 'low'];
      heroChip.textContent = st[0] + ' · ' + st[1];
      heroChip.className = 'hero-chip ' + st[2] + (heroChip.textContent.length > 28 ? ' long' : '');
    }
    // health snapshot panel
    const snapNow = document.getElementById('snap-bio-now');
    if (snapNow) snapNow.innerHTML = `${Math.round(level)}<span class="unit">/ 100</span>`;
    const snapLabel = document.getElementById('snap-bio-label');
    if (snapLabel) {
      const ratio = startLevel > 0 ? level / startLevel : 0;
      snapLabel.textContent =
        ratio >= 0.85 ? 'Charged · Fully recovered' :
        ratio >= 0.70 ? 'Good · On track' :
        ratio >= 0.50 ? 'Normal · Natural decline' :
        ratio >= 0.30 ? 'Winding down · Slow down, reduce effort' :
                        'Low · Rest needed';
      snapLabel.dataset.state = ratio >= 0.50 ? '' : (ratio >= 0.30 ? 'warn' : 'low');
    }
  }

  // ── Main tab: sleep panel ─────────────────────────────────
  function applyMainSleep(sl) {
    const scoreEl = document.querySelector('#screen-main .sleep-score');
    // No fresh night yet (e.g. last night hasn't synced) — say so rather than
    // leaving an older night's numbers on screen looking current.
    if (!sl) {
      if (scoreEl) scoreEl.innerHTML = `—<span class="of">/ 100</span>`;
      const tagEl0 = document.querySelector('#screen-main .sleep-tag');
      if (tagEl0) tagEl0.textContent = 'No data';
      const durEl0 = document.querySelector('#screen-main .duration');
      if (durEl0) durEl0.textContent = '—';
      const timesEl0 = document.getElementById('sleep-times-val');
      if (timesEl0) timesEl0.textContent = '—';
      const bar0 = document.querySelector('#screen-main .sleep-bar');
      if (bar0) bar0.innerHTML = '';
      const leg0 = document.querySelector('#screen-main .sleep-legend');
      if (leg0) leg0.innerHTML = '';
      return;
    }
    if (scoreEl) scoreEl.innerHTML = `${Math.round(sl.score)}<span class="of">/ 100</span>`;

    const tagEl = document.querySelector('#screen-main .sleep-tag');
    if (tagEl) tagEl.textContent = scoreLabel(sl.score);

    const durEl = document.querySelector('#screen-main .duration');
    if (durEl) durEl.innerHTML = fmtHM(sl.asleep_min);

    const timesEl = document.getElementById('sleep-times-val');
    if (timesEl) timesEl.innerHTML = `${fmtMin(sl.bedtime_ts, sl.tz_offset_min)} – ${fmtMin(sl.waketime_ts, sl.tz_offset_min)}`;

    // The bar was dividing by asleep_min while summing deep+rem+light+awake,
    // which never added up: awake is not part of asleep, and on a night with
    // recovered sleep the stages fall short of the total (361 of 455 on
    // 2026-08-23) leaving 15% of the bar blank. The denominator is now the sum
    // of the segments actually drawn, so it always fills.
    //
    // Recovered sleep is folded into light for display only -- the same
    // convention Gadgetbridge uses, and the same one the hypnogram chart uses.
    // light_min in the stats stays untouched.
    const bar = document.querySelector('#screen-main .sleep-bar');
    const lightShown = (sl.light_min || 0) + (sl.ext_sleep_min || 0);
    const segTotal = (sl.deep_min || 0) + (sl.rem_min || 0) + lightShown + (sl.awake_min || 0);
    if (bar && segTotal) {
      const pct = m => (((m || 0) / segTotal) * 100).toFixed(1);
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', `Sleep stages: deep ${sl.deep_min} min, REM ${sl.rem_min} min, light ${lightShown} min, awake ${sl.awake_min} min`);
      bar.innerHTML = `
        <div class="deep"  style="flex:${sl.deep_min || 0};"></div>
        <div class="rem"   style="flex:${sl.rem_min || 0};"></div>
        <div class="light" style="flex:${lightShown};"></div>
        <div class="awake" style="flex:${Math.max(sl.awake_min || 0, 4)};"></div>`;
    }

    const leg = document.querySelector('#screen-main .sleep-legend');
    if (leg) {
      const fmt = m => { const h = Math.floor(m/60), mm = m%60; return h > 0 ? `${h}h ${p2(mm)}m` : `${mm}m`; };
      leg.innerHTML = `
        <div class="item"><span class="swatch" style="background:var(--st-deep);"></span>Deep <b>${fmt(sl.deep_min)}</b></div>
        <div class="item"><span class="swatch" style="background:var(--st-rem);"></span>REM <b>${fmt(sl.rem_min)}</b></div>
        <div class="item"><span class="swatch" style="background:var(--st-light);"></span>Light <b>${fmt(lightShown)}</b></div>
        <div class="item"><span class="swatch" style="background:var(--st-awake);"></span>Awake <b>${fmt(sl.awake_min)}</b></div>`;
    }
  }

  // ── Health → Biocharge: day strip (replaces renderBioDay) ─
  let liveDaySeries = [];
  let liveSleepLatest = null; // latest night; used to draw the sleep band on the strip
  let liveDayNaps = [];       // /api/naps for the shown day — nap band + hover detail
  let liveDayActivities = []; // /api/activities for the shown day — gold band, icon and hover detail on the curve

  // Logged workouts (gym, run, bike, football, coaching ...) on the biocharge curve: a gold band behind it, the
  // stretch of the line in gold, and a flat single-stroke icon on top, the way the sleep window carries its
  // moon. One gold for every kind of workout; the icon says which. Orange stays for effort the strap saw that
  // nobody logged. Icons are 24-unit glyphs drawn like the moon (round caps, no fill).
  const ACT_GOLD = 'oklch(0.83 0.15 90)';
  const ACT_ICONS = {
    gym: '<path d="M6.5 7v10M17.5 7v10M3.5 9.5v5M20.5 9.5v5M6.5 12h11"/>',
    run: '<circle cx="15.6" cy="4.6" r="1.9"/><path d="M14 8.6l-2.4 5.8M14.2 9l3.6 1.8 1.8-1.8M13.8 9.4l-3.4.8-1.6 2.6M11.6 14.4l3.4 2.4-.8 4.2M11.6 14.4l-2.8 3.2-3.6-.2"/>',
    bike: '<circle cx="5.8" cy="16" r="3.6"/><circle cx="18.2" cy="16" r="3.6"/><path d="M5.8 16l4.2-7.6h4.6M10 8.4l2.4 7.6 5.8-7.8M14.6 8.4L18.2 16M8.4 8.4h3"/>',
    football: '<circle cx="12" cy="12" r="8.6"/><path d="M12 8.6l3.2 2.3-1.2 3.8h-4L8.8 10.9zM12 8.6V3.5M15.2 10.9l4.7-1.5M14 14.7l2.9 4M10 14.7l-2.9 4M8.8 10.9L4.1 9.4"/>',
    padel: '<circle cx="10.6" cy="10.6" r="5.9"/><path d="M9 9h.01M12.2 9h.01M9 12.2h.01M12.2 12.2h.01M14.8 14.8l5.6 5.6M20.2 3.6a1.6 1.6 0 1 1-3.2 0 1.6 1.6 0 0 1 3.2 0z"/>',
    swim: '<circle cx="17.2" cy="6.4" r="1.8"/><path d="M6.5 12.6l4.6-3.1 3.2 2.3 3-2M3 16.6c1.8 0 1.8-1.2 3.6-1.2s1.8 1.2 3.6 1.2 1.8-1.2 3.6-1.2 1.8 1.2 3.6 1.2 1.8-1.2 3.6-1.2M3 20.6c1.8 0 1.8-1.2 3.6-1.2s1.8 1.2 3.6 1.2 1.8-1.2 3.6-1.2 1.8 1.2 3.6 1.2 1.8-1.2 3.6-1.2"/>',
    walk: '<circle cx="12.6" cy="4.5" r="1.9"/><path d="M12.4 8.2l-.9 5.4 3 2.6v5.2M11.5 13.6l-3.3 6.4M12.4 8.4l3.6 2.6M12.2 8.6l-3.2 1.8-.9 3.2"/>',
    coaching: '<circle cx="8.6" cy="14.6" r="5.2"/><path d="M13.4 10.6H21v4.2h-7.4M8.6 14.6h.01M5.6 9.9L4.6 6.2h4.2"/>',
    other: '<path d="M3 12h4l2.6-6.4 4.2 12.4 2.4-6H21"/>',
  };
  const actIcon = (key, x, y, size) => `<g class="act-ic" transform="translate(${x.toFixed(1)} ${y.toFixed(1)}) scale(${(size / 24).toFixed(3)})" fill="none" stroke="${ACT_GOLD}" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">${ACT_ICONS[key] || ACT_ICONS.other}</g>`;
  // the workout a timestamp falls inside, if any
  const actAt = ts => (liveDayActivities || []).find(a => ts >= a.start_ts && ts <= a.end_ts) || null;
  const actText = a => `${a.name} · ${a.minutes} min`;
  const actRange = a => `${a.start}–${a.end}`;
  // Icon (and, when there is room, "Gym · 25 min") for each workout, plus its band. `at(a)` returns
  // {x1, x2} for the workout on this chart; the caller supplies its own top, height and geometry.
  // Crowding: a label is only printed when it fits before the next workout's icon (or the chart edge, or,
  // on the last one, on its left); an icon that would sit on the previous one drops to a second row.
  function actMarks(at, top, height, xMin, xMax, iconY, iconSize, labelAttrs, charW) {
    const items = [];
    (liveDayActivities || []).forEach(a => {
      const r = at(a); if (!r) return;
      const bw = Math.max(r.x2 - r.x1, 6);
      items.push({ a, x1: r.x1, bw, cx: r.x1 + bw / 2 });
    });
    const half = iconSize / 2, rowRight = [-1e9, -1e9];
    let band = '', marks = '', labelRight = -1e9;
    items.forEach((it, i) => {
      band += `<rect x="${it.x1.toFixed(1)}" y="${top}" width="${it.bw.toFixed(1)}" height="${height}" fill="${ACT_GOLD}" fill-opacity="0.13"/>`;
      const row = it.cx - half > rowRight[0] + 3 ? 0 : (it.cx - half > rowRight[1] + 3 ? 1 : 0);
      rowRight[row] = it.cx + half;
      marks += actIcon(it.a.icon, it.cx - half, iconY + row * (iconSize + 3), iconSize);
      if (row !== 0) return;
      const txt = actText(it.a), lw = txt.length * (charW || 6.9), next = items[i + 1], prev = items[i - 1];
      const roomR = (next ? next.cx - half - 4 : xMax - 4) - (it.cx + half + 6);
      const roomL = (it.cx - half - 6) - Math.max(labelRight + 4, prev ? prev.cx + half + 4 : -1e9, xMin + 4);
      const y = iconY + iconSize * 0.74;
      if (lw <= roomR) {
        const tx = it.cx + half + 6;
        marks += `<text x="${tx.toFixed(1)}" y="${y}" text-anchor="start" ${labelAttrs} style="fill:${ACT_GOLD}">${txt}</text>`;
        labelRight = tx + lw;
      } else if (!next && lw <= roomL) {
        const tx = it.cx - half - 6;
        marks += `<text x="${tx.toFixed(1)}" y="${y}" text-anchor="end" ${labelAttrs} style="fill:${ACT_GOLD}">${txt}</text>`;
        labelRight = it.cx + half;
      } else labelRight = Math.max(labelRight, it.cx + half);
    });
    return { band, marks };
  }
  let dayChartHoverData = null;

  // Contiguous why_label==='nap' index runs in a biocharge point array.
  function napRuns(pts) {
    const runs = []; let cur = null;
    (pts || []).forEach((p, i) => {
      if (p && p.why_label === 'nap') { cur ? (cur.b = i) : (cur = { a: i, b: i }); }
      else if (cur) { runs.push(cur); cur = null; }
    });
    if (cur) runs.push(cur);
    return runs;
  }
  // The /api/naps record overlapping a point-run, if any (full-res duration /
  // recharge / stage_source — pts is downsampled so this is more accurate).
  function napMetaFor(pts, run) {
    const s = pts[run.a].minute_ts, e = pts[run.b].minute_ts;
    return (liveDayNaps || []).find(n => n.start_ts <= e && n.end_ts >= s) || null;
  }
  const _origRenderBioDay = renderBioDay;
  // Draws today's biocharge curve into a target SVG. Two call sites now:
  // the Health→Biocharge strip and the Main screen's hero band.
  // Design follows the mockup's energy chart: shaded sleep window with an
  // "Asleep until" label, peak and Now labels, dashed projection, 00:00–24:00
  // axis, hover/touch/keyboard scrub. Activity colouring (asleep blue, exertion orange,
  // strap gap grey) is kept because it carries data the mockup sample never exercised.
  function drawBioDay(targetId) {
    const pts = liveDaySeries;
    const svg = document.getElementById(targetId);
    if (!svg) return;
    if (pts.length < 2) {
      if (targetId === 'bio-day-line') _origRenderBioDay();
      else renderChartEmpty(svg, 600, 150, 'No data · please upload');
      // Otherwise the Wake/Now header figures from the last successful
      // render linger next to a chart that just said "no data" — the same
      // stale-number-looks-live problem this fix is for, just in the header.
      document.querySelectorAll('.bio-strip-foot .start strong').forEach(el => { el.textContent = '—'; });
      document.querySelectorAll('.bio-strip-foot .now strong').forEach(el => { el.textContent = '—'; });
      return;
    }

    // Draw at the SVG's real pixel size (1 unit = 1px) so nothing is distorted.
    const rect = svg.getBoundingClientRect();
    const W = Math.round(rect.width)  || 700;
    const H = Math.round(rect.height) || 200;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    const P = { l: 34, r: 10, t: 16, b: 26 }; // l: y labels, b: x labels
    const iW = W - P.l - P.r, iH = H - P.t - P.b;

    // x-axis is clock time across the whole day, anchored to the day the series
    // belongs to (not today) so past days from the day navigator do not collapse.
    const dayStart = new Date(pts[0].minute_ts); dayStart.setHours(0, 0, 0, 0);
    const t0 = dayStart.getTime();
    const dayMs = 24 * 3600 * 1000;
    const xAt = t => P.l + Math.max(0, Math.min(1, (t - t0) / dayMs)) * iW;
    const yAt = v => P.t + iH - (v / 100) * iH;

    const levels = pts.map(p => p.level);
    const xs = pts.map(p => xAt(p.minute_ts));
    const ys = pts.map(p => yAt(p.level));

    const MINT = 'var(--hue-bio)', ORANGE = 'oklch(0.72 0.16 55)';
    // Grey = the strap was not recording: biocharge still has a value (drain falls
    // back to the resting rate) but it is modelled, not measured.
    const GREY = 'oklch(0.52 0.012 250)';
    const SLEEP_HUE = 'oklch(0.78 0.12 290)';
    const SLEEP_LINE = 'oklch(0.64 0.13 264)'; // the blue the sleep stretch of the curve has always had (matches the expanded chart)
    const MOON = 'M19.5 14.2A8 8 0 1 1 9.8 4.5a6.4 6.4 0 0 0 9.7 9.7z';

    let grid = '';
    [0, 50, 100].forEach(v => {
      const yp = yAt(v);
      grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yp}" y2="${yp}" stroke="rgb(255 255 255 / 0.07)"/>`;
      grid += `<text class="ch-t" x="${P.l - 8}" y="${yp + 4}" text-anchor="end">${v}</text>`;
    });
    let xlab = '';
    [0, 6, 12, 18, 24].forEach(h => {
      const anchor = h === 0 ? 'start' : (h === 24 ? 'end' : 'middle');
      xlab += `<text class="ch-t" x="${xAt(t0 + h * 3600 * 1000)}" y="${H - 6}" text-anchor="${anchor}">${p2(h)}:00</text>`;
    });

    // Sleep window: shaded from midnight (or bedtime, if after midnight) to wake,
    // with a flat moon and, when there is room, "Asleep until HH:MM".
    let band = '';
    if (liveSleepLatest && liveSleepLatest.waketime_ts) {
      let bStart = t0;
      if (liveSleepLatest.bedtime_ts && liveSleepLatest.bedtime_ts > t0) bStart = liveSleepLatest.bedtime_ts;
      const bEnd = Math.min(liveSleepLatest.waketime_ts, t0 + dayMs);
      if (bEnd > bStart) {
        const x1 = xAt(bStart), bw = xAt(bEnd) - x1;
        band = `<rect x="${x1}" y="${P.t}" width="${bw}" height="${iH}" fill="oklch(0.78 0.12 290 / 0.10)"/>`
          + `<g transform="translate(${x1 + 8} ${P.t + 8}) scale(0.62)" fill="none" stroke="${SLEEP_HUE}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="${MOON}"/></g>`
          + (bw > 120 ? `<text class="ch-strong" x="${x1 + 30}" y="${P.t + 20}">Asleep until ${fmtMin(liveSleepLatest.waketime_ts, liveSleepLatest.tz_offset_min)}</text>` : '');
      }
    }
    // Nap band: strap-detected daytime sleep, same tint as the sleep window.
    let napBand = '';
    napRuns(pts).forEach(run => {
      const x1 = xs[run.a], x2 = xs[run.b];
      if (x2 - x1 < 1) return;
      napBand += `<rect x="${x1}" y="${P.t}" width="${x2 - x1}" height="${iH}" fill="oklch(0.78 0.12 290 / 0.12)"/>`;
    });

    // Logged workouts (/api/activities): a gold band behind the curve and a flat icon on top, with a short
    // label when there is room; the stretch of the line inside a workout is gold (see lineColor).
    const actDraw = actMarks(a => { const x1 = xAt(a.start_ts), x2 = xAt(a.end_ts); return x2 >= P.l && x1 <= W - P.r ? { x1, x2 } : null; },
                             P.t, iH, P.l, W - P.r, P.t + 4, 20, 'class="ch-strong ch-halo"');

    // On a completed (past) day there is no "now": the marker sits on the last point.
    const isPastDay = t0 + dayMs <= Date.now();
    const nowMs = Date.now();
    let nowIdx = pts.length - 1;
    if (!isPastDay) { let minDiff = Infinity; pts.forEach((p, i) => { const d = Math.abs(p.minute_ts - nowMs); if (d < minDiff) { minDiff = d; nowIdx = i; } }); }
    const nx = xs[nowIdx], ny = ys[nowIdx];

    // Past: area + line coloured by state. Future: dashed projection, no fill.
    const base = P.t + iH;
    const pastLine = xs.slice(0, nowIdx + 1).map((x, i) => (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + ys[i].toFixed(1)).join(' ');
    const area = pastLine + ` L${nx.toFixed(1)},${base} L${xs[0].toFixed(1)},${base} Z`;
    // gold = a logged workout (wins over everything: it is a fact even when the strap was off), grey = strap not
    // recording, blue = asleep (night or nap), orange = exertion nobody logged, mint otherwise
    const lineColor = i => {
      const p = pts[i];
      if (p && actAt(p.minute_ts)) return ACT_GOLD;
      if (p && p.no_hr) return GREY;
      const lbl = p && p.why_label;
      return (lbl === 'sleep' || lbl === 'nap') ? SLEEP_LINE : (lbl === 'exertion' ? ORANGE : MINT);
    };
    let segPaths = '';
    for (let a = 0; a <= nowIdx; ) {
      let b = a;
      while (b + 1 <= nowIdx && lineColor(b + 1) === lineColor(a)) b++;
      const end = b < nowIdx ? b + 1 : b; // overlap one point so runs meet with no gap
      let d = '';
      for (let j = a; j <= end; j++) d += (j === a ? 'M' : 'L') + xs[j].toFixed(1) + ',' + ys[j].toFixed(1) + ' ';
      segPaths += `<path class="ln" pathLength="1" d="${d.trim()}" fill="none" stroke="${lineColor(a)}" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"/>`;
      a = b + 1;
    }
    let future = '';
    if (nowIdx < pts.length - 1) {
      const fd = xs.slice(nowIdx).map((x, i) => (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + ys[nowIdx + i].toFixed(1)).join(' ');
      future = `<path d="${fd}" fill="none" stroke="var(--ink-3)" stroke-width="1.75" stroke-dasharray="3 5" stroke-linecap="round"/>`;
    }

    // Peak of the elapsed day, labelled unless it is the Now point itself (its label sits above, the Now label below).
    let pk = 0; for (let i = 1; i <= nowIdx; i++) if (levels[i] > levels[pk]) pk = i;
    const peak = Math.abs(xs[pk] - nx) > 12
      ? `<circle cx="${xs[pk]}" cy="${ys[pk]}" r="3.5" fill="${MINT}"/><text class="ch-strong ch-halo" x="${xs[pk]}" y="${ys[pk] - 9}" text-anchor="middle" style="fill:${MINT}">${Math.round(levels[pk])}</text>` : '';
    const nowSide = nx - 56 < P.l ? 'start' : 'end';
    const nowLabel = `<text class="ch-strong ch-halo" x="${nowSide === 'end' ? nx + 6 : nx - 6}" y="${Math.min(ny + 24, yAt(0) - 6)}" text-anchor="${nowSide}">${isPastDay ? 'End' : 'Now'} ${Math.round(levels[nowIdx])}</text>`;

    svg.innerHTML = `${band}${napBand}${actDraw.band}${grid}${xlab}
      <path d="${area}" fill="${MINT}" fill-opacity="0.13"/>
      ${segPaths}${future}${peak}${actDraw.marks}
      <circle cx="${nx}" cy="${ny}" r="9" fill="none" stroke="${MINT}" stroke-opacity="0.4" stroke-width="1.5"/>
      <circle cx="${nx}" cy="${ny}" r="4.5" fill="${MINT}" stroke="var(--panel)" stroke-width="2"/>
      ${nowLabel}
      <g class="scrub"><line y1="${P.t}" y2="${H - P.b}" stroke="rgb(255 255 255 / 0.28)" stroke-width="1" style="display:none"/><circle r="4.5" fill="${MINT}" stroke="var(--panel)" stroke-width="2" style="display:none"/></g>`;

    // first draw animates the line in; redraws (refresh, resize) do not replay it
    svg.classList.toggle('on-in', !svg._drawn);
    svg._drawn = true;

    // charts draw at real pixel size, so redraw when the container width changes (resize, rotate)
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => drawBioDay(targetId));

    // ── hover / touch / keyboard scrub ──
    svg._scrub = { pts, xs, ys, nowIdx, W, H, P, isPastDay };
    svg.setAttribute('role', 'img');
    svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-label', `Energy today: ${Math.round(levels[pk])} at the morning peak, ${Math.round(levels[nowIdx])} ${isPastDay ? 'at the end of the day' : 'now'}. Use left and right arrow keys to read values.`);
    if (!svg._scrubBound) {
      svg._scrubBound = true;
      const T = window.actaTip(svg.parentElement, svg);
      let cur = -1;
      const show = i => {
        const S = svg._scrub; if (!S) return;
        i = Math.max(0, Math.min(S.pts.length - 1, i)); cur = i;
        const g = svg.querySelector('.scrub'); if (!g) return;
        const ln = g.querySelector('line'), dot = g.querySelector('circle');
        ln.setAttribute('x1', S.xs[i]); ln.setAttribute('x2', S.xs[i]); dot.setAttribute('cx', S.xs[i]); dot.setAttribute('cy', S.ys[i]);
        ln.style.display = ''; dot.style.display = '';
        const d = new Date(S.pts[i].minute_ts);
        const ac = actAt(S.pts[i].minute_ts);
        T.show(`<b>${p2(d.getHours())}:${p2(d.getMinutes())}</b> · ${Math.round(S.pts[i].level)}${i > S.nowIdx ? '<span class="dim">projected</span>' : ''}`
          + (ac ? `<br><span style="color:${ACT_GOLD}">${actText(ac)} · ${actRange(ac)}</span>` : ''), S.xs[i], S.ys[i]);
      };
      const hide = () => {
        const g = svg.querySelector('.scrub');
        if (g) g.querySelectorAll('line,circle').forEach(n => { n.style.display = 'none'; });
        T.hide(); cur = -1;
      };
      const nearestAt = px => { const S = svg._scrub; let bi = 0, bd = 1e9; S.xs.forEach((x, i) => { const d = Math.abs(x - px); if (d < bd) { bd = d; bi = i; } }); return bi; };
      // In viewBox units, not raw CSS pixels — if a redraw ever happened while
      // this chart was hidden (0-width, e.g. the 5-min refresh firing on the
      // Health tab), the viewBox falls back to a fixed width while the element
      // later renders at its real (usually wider) width, stretching the drawn
      // chart. Raw pixel offsets then point further right than the cursor,
      // worse the wider the real chart is — this scales back to match
      // regardless of whether such a mismatch happened (2026-09-22).
      const local = e => {
        const r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
        return r.width ? (e.clientX - r.left) * (vb.width / r.width) : e.clientX - r.left;
      };
      svg.addEventListener('pointermove', e => show(nearestAt(local(e))));
      svg.addEventListener('pointerdown', e => show(nearestAt(local(e))));
      svg.addEventListener('pointerleave', hide);
      svg.addEventListener('blur', hide);
      svg.addEventListener('keydown', e => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Escape') return;
        e.preventDefault();
        if (e.key === 'Escape') return hide();
        const S = svg._scrub, step = Math.max(1, Math.round(S.pts.length / 48));
        show((cur < 0 ? S.nowIdx : cur) + (e.key === 'ArrowRight' ? step : -step));
      });
    }

    const sv = Math.round(Math.max(...levels)), nv = Math.round(levels[nowIdx]);
    document.querySelectorAll('.bio-strip-foot .start strong').forEach(el => el.textContent = sv);
    document.querySelectorAll('.bio-strip-foot .now strong').forEach(el => el.textContent = nv);
    document.querySelectorAll('.bio-strip-foot .now span').forEach(el => {
      el.textContent = isPastDay ? 'End of day' : 'Now';
    });
  }
  renderBioDay = function () { ['bio-day-line', 'main-day-line'].forEach(id => drawBioDay(id)); };

  // ── Health → Biocharge: big day-energy modal ──────────────
  const _origRenderDayEnergyBig = renderDayEnergyBig;
  renderDayEnergyBig = function () {
    const svg = document.getElementById('day-energy-chart');
    if (!svg) { _origRenderDayEnergyBig(); return; }

    const DNAMES = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
    const MNAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];

    // The chart always shows whichever day the Health header has selected —
    // it has no day controls of its own.
    const key     = activeDay();
    const isToday = viewDate === null;
    const tgt     = new Date(key + 'T00:00:00');
    let pts;
    if (isToday) {
      // Live series carries the now-dot and the end-of-day projection.
      pts = liveDaySeries;
    } else if (daySeriesCache.has(key)) {
      pts = daySeriesCache.get(key);
    } else {
      pts = null;
      fetchDaySeries(key);
    }
    // Header label first, so it stays correct on a day with no data.
    const headEl = document.getElementById('day-energy-head');
    if (headEl) {
      const lbl = isToday ? 'Today'
                : `${DNAMES[tgt.getDay()]} ${tgt.getDate()} ${MNAMES[tgt.getMonth()].slice(0, 3)}`;
      headEl.textContent = `Biocharge · ${lbl}`;
    }

    if (pts === null) { renderChartEmpty(svg, 800, 320, 'Loading…'); return; }
    if (pts.length < 2) { _origRenderDayEnergyBig(); return; }

    // Update subtitle
    const subEl = document.getElementById('day-modal-sub');
    if (subEl) {
      const d0 = new Date(pts[0].minute_ts);
      const d1 = new Date(pts[pts.length - 1].minute_ts);
      const tail = isToday ? '— Now' : `— ${p2(d1.getHours())}:${p2(d1.getMinutes())}`;
      subEl.textContent = `${DNAMES[d0.getDay()]}, ${MNAMES[d0.getMonth()]} ${d0.getDate()} · ${p2(d0.getHours())}:${p2(d0.getMinutes())} ${tail}`;
    }

    const _mr = svg.getBoundingClientRect();
    const W = Math.round(_mr.width) || 800, H = 320, P = { l: 50, r: 20, t: 30, b: 40 };
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderDayEnergyBig());
    const iW = W - P.l - P.r, iH = H - P.t - P.b;
    const levels = pts.map(p => p.level);
    const xs = levels.map((_, i) => P.l + (i / (levels.length - 1)) * iW);
    const ys = levels.map(v => P.t + iH - (v / 100) * iH);

    let grid = '';
    [0,25,50,75,100].forEach(y => {
      const yp = P.t + iH - (y/100)*iH;
      grid += `<line x1="${P.l}" x2="${W-P.r}" y1="${yp}" y2="${yp}" stroke="rgba(255,255,255,0.05)" stroke-dasharray="2 4"/>`;
      grid += `<text x="${P.l-10}" y="${yp+3}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="end">${y}</text>`;
    });
    const step = Math.max(1, Math.floor(pts.length / 7));
    let xLabels = '', lastLX = -1e9;
    pts.forEach((p, i) => {
      if (i % step !== 0 && i !== pts.length-1) return;
      if (xs[i] - lastLX < 56) return;
      lastLX = xs[i];
      const d = new Date(p.minute_ts);
      xLabels += `<text x="${xs[i]}" y="${H-P.b+16}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="middle">${p2(d.getHours())}:${p2(d.getMinutes())}</text>`;
    });

    const lp = xs.map((x,i) => (i===0?'M':'L')+x.toFixed(1)+','+ys[i].toFixed(1)).join(' ');
    const fp = lp + ` L${xs[xs.length-1].toFixed(1)},${P.t+iH} L${xs[0].toFixed(1)},${P.t+iH} Z`;

    // Nap band — same blue as the strip. Detail shows on hover/press below.
    let napBand = '';
    napRuns(pts).forEach(run => {
      const x1 = xs[run.a], x2 = xs[run.b];
      if (x2 - x1 < 1) return;
      napBand += `<rect x="${x1}" y="${P.t}" width="${x2 - x1}" height="${iH}" fill="oklch(0.64 0.13 264)" fill-opacity="0.12"/>`;
    });

    // Colour the biocharge line by activity: exertion (workout/hard effort) = orange, else mint.
    // why_label is already on each point from /api/biocharge — no extra data fetched.
    const LINE_MINT = 'oklch(0.78 0.12 165)', LINE_ORANGE = 'oklch(0.72 0.16 55)';
    const LINE_GREY = 'oklch(0.52 0.012 250)';   // no strap data — see drawBioDay
    const LINE_SLEEP = 'oklch(0.64 0.13 264)';   // must match drawBioDay's SLEEP
    // Same rule as the mini strip: sleep blue / exertion orange / grey when the
    // strap wasn't recording / mint otherwise. This branch used to omit sleep,
    // so the expanded chart drew the whole night mint while the strip beside it
    // drew it blue — two views of one series disagreeing.
    const lineColor = i => {
      const p = pts[i];
      if (p && actAt(p.minute_ts)) return ACT_GOLD;   // a logged workout wins, as on the strip
      if (p && p.no_hr) return LINE_GREY;
      const lbl = p && p.why_label;
      return (lbl === 'sleep' || lbl === 'nap') ? LINE_SLEEP : lbl === 'exertion' ? LINE_ORANGE : LINE_MINT;
    };
    const actDrawBig = actMarks(a => {
      let ia = -1, ib = -1;
      pts.forEach((p, i) => { if (p.minute_ts >= a.start_ts && p.minute_ts <= a.end_ts) { if (ia < 0) ia = i; ib = i; } });
      return ia < 0 ? null : { x1: xs[ia], x2: xs[ib] };
    }, P.t, iH, P.l, W - P.r, 4, 22, 'font-family="JetBrains Mono" font-size="12"', 7.6);
    let segPaths = '';
    for (let a = 0; a < xs.length; ) {
      let b = a;
      while (b + 1 < xs.length && lineColor(b + 1) === lineColor(a)) b++;
      // extend one point into the next run so adjacent colours meet with no gap
      const end = b < xs.length - 1 ? b + 1 : b;
      let d = '';
      for (let j = a; j <= end; j++) d += (j === a ? 'M' : 'L') + xs[j].toFixed(1) + ',' + ys[j].toFixed(1) + ' ';
      segPaths += `<path d="${d.trim()}" fill="none" stroke="${lineColor(a)}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
      a = b + 1;
    }
    const maxIdx = levels.indexOf(Math.max(...levels));
    const sv = Math.round(levels[maxIdx]);
    const minV = Math.round(Math.min(...levels));
    const minIdx = levels.indexOf(Math.min(...levels));
    const md = new Date(pts[minIdx].minute_ts);
    const minT = p2(md.getHours()) + ':' + p2(md.getMinutes());
    const wkX = xs[maxIdx], wkY = ys[maxIdx];
    const wkAnchor = maxIdx > levels.length * 0.65 ? 'end' : 'start';
    const wkTextX = wkAnchor === 'end' ? wkX - 8 : wkX + 8;

    // Mid marker: "Now" for today, "End" for previous day
    let midMarker = '', nv;
    if (isToday) {
      const nowMs2 = Date.now();
      let nowIdx2 = pts.length - 1;
      { let minDiff = Infinity; pts.forEach((p, i) => { const d = Math.abs(p.minute_ts - nowMs2); if (d < minDiff) { minDiff = d; nowIdx2 = i; } }); }
      nv = Math.round(levels[nowIdx2]);
      const nowX = xs[nowIdx2], nowY = ys[nowIdx2];
      const nowAnchor = nowIdx2 > levels.length * 0.65 ? 'end' : 'start';
      const nowTextX = nowAnchor === 'end' ? nowX - 8 : nowX + 8;
      midMarker = `
        <line x1="${nowX}" x2="${nowX}" y1="${P.t}" y2="${P.t+iH}" stroke="oklch(0.78 0.12 165)" stroke-width="1" stroke-dasharray="3 3"/>
        <circle cx="${nowX}" cy="${nowY}" r="6" fill="oklch(0.78 0.12 165)"/>
        <circle cx="${nowX}" cy="${nowY}" r="11" fill="none" stroke="oklch(0.78 0.12 165)" stroke-opacity="0.3"/>
        <text x="${nowTextX}" y="${nowY-12}" fill="var(--accent)" font-family="JetBrains Mono" font-size="12" text-anchor="${nowAnchor}">Now · ${nv}</text>`;
    } else {
      const endIdx = pts.length - 1;
      nv = Math.round(levels[endIdx]);
      const endX = xs[endIdx], endY = ys[endIdx];
      midMarker = `
        <circle cx="${endX}" cy="${endY}" r="5" fill="oklch(0.78 0.12 165)" opacity="0.6"/>
        <text x="${endX - 8}" y="${endY - 10}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="end">End · ${nv}</text>`;
    }

    svg.innerHTML = `
      <defs><linearGradient id="day-big-fill" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="oklch(0.78 0.12 165)" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="oklch(0.78 0.12 165)" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}${napBand}${actDrawBig.band}${xLabels}
      <path d="${fp}" fill="url(#day-big-fill)"/>
      ${segPaths}${actDrawBig.marks}
      <line x1="${wkX}" x2="${wkX}" y1="${P.t}" y2="${P.t+iH}" stroke="oklch(0.42 0.09 165)" stroke-dasharray="3 3" stroke-width="1"/>
      <circle cx="${wkX}" cy="${wkY}" r="5" fill="oklch(0.42 0.09 165)"/>
      <text x="${wkTextX}" y="${wkY-6}" fill="oklch(0.62 0.10 165)" font-family="JetBrains Mono" font-size="12" text-anchor="${wkAnchor}">Wake · ${sv}</text>
      ${midMarker}`;

    // save state for hover tooltip
    dayChartHoverData = { pts, xs, ys, Pt: P.t, iH, W };

    const hg = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    hg.id = 'day-hover-g';
    hg.setAttribute('pointer-events', 'none');
    hg.style.display = 'none';
    svg.appendChild(hg);

    // Update footer
    const midLabel = document.getElementById('day-foot-mid-label');
    if (midLabel) midLabel.textContent = isToday ? 'Current' : 'End of day';
    const footMidEl = document.getElementById('day-foot-mid');
    if (footMidEl) { footMidEl.textContent = nv; footMidEl.className = isToday ? 'v acc' : 'v'; }
    const footWakeEl = document.getElementById('day-foot-wake');
    if (footWakeEl) footWakeEl.textContent = sv;
    const footLowEl = document.getElementById('day-foot-low');
    if (footLowEl) footLowEl.textContent = `${minV} · ${minT}`;
  };

  // ── Day energy chart: hover / press tooltip ──────────────
  // pointer* so a finger-press on the PWA works, not just a desktop hover —
  // this is where nap detail (and every minute's level) is read.
  (function setupDayChartHover() {
    const svgEl = document.getElementById('day-energy-chart');
    if (!svgEl) return;
    const _DT = window.actaTip(svgEl.parentElement, svgEl);
    const onMove = (e) => {
      if (!dayChartHoverData) return;
      const { pts, xs, ys, Pt, iH } = dayChartHoverData;
      if (!pts.length) return;

      const rect = svgEl.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;

      let idx = 0, minDist = Infinity;
      xs.forEach((x, i) => { const d = Math.abs(x - mouseX); if (d < minDist) { minDist = d; idx = i; } });

      const cx = xs[idx], cy = ys[idx];
      const pt = pts[idx];
      const d = new Date(pt.minute_ts);
      const timeStr = p2(d.getHours()) + ':' + p2(d.getMinutes());
      const val = Math.round(pt.level);

      const hg = document.getElementById('day-hover-g');
      if (!hg) return;
      hg.style.display = '';

      const DOT = 'oklch(0.78 0.12 165)';
      const cursor = `
        <line x1="${cx}" x2="${cx}" y1="${Pt}" y2="${Pt + iH}" stroke="rgba(255,255,255,0.18)" stroke-width="1" stroke-dasharray="3 3"/>
        <circle cx="${cx}" cy="${cy}" r="4" fill="${DOT}"/>
        <circle cx="${cx}" cy="${cy}" r="9" fill="none" stroke="${DOT}" stroke-opacity="0.3"/>`;

      // Inside a nap: a fuller readout — what it was, and that the strap wrote
      // no stages for it. This is the "press the graph" surface for nap detail.
      const napRun = pt.why_label === 'nap'
        ? napRuns(pts).find(r => idx >= r.a && idx <= r.b) : null;
      if (napRun) {
        const meta = napMetaFor(pts, napRun);
        const s = new Date(meta ? meta.start_ts : pts[napRun.a].minute_ts);
        const e = new Date(meta ? meta.end_ts   : pts[napRun.b].minute_ts);
        const mins  = meta ? meta.minutes : Math.round((e - s) / 60000);
        const dur   = mins >= 60 ? `${Math.floor(mins/60)}h${p2(mins%60)}` : `${mins}m`;
        const delta = meta ? meta.delta
          : Math.round((pts[napRun.b].level - pts[Math.max(0, napRun.a-1)].level) * 10) / 10;
        const stg   = meta && meta.stage_source === 'hypnogram' ? 'stages measured' : 'stages unknown';
        const range = `${p2(s.getHours())}:${p2(s.getMinutes())}–${p2(e.getHours())}:${p2(e.getMinutes())}`;
        hg.innerHTML = cursor;
        _DT.show(`<b>Nap</b> · ${range}<br>${dur} · ${delta > 0 ? '+' : ''}${delta}<br><span class="dim" style="margin-left:0">${stg}</span>`, cx, cy);
        return;
      }

      hg.innerHTML = cursor;
      const _ac = actAt(pt.minute_ts);
      _DT.show(`<b>${timeStr}</b> · ${val}`
        + (_ac ? `<br><span style="color:${ACT_GOLD}">${actText(_ac)} · ${actRange(_ac)}</span>` : ''), cx, cy);
    };
    svgEl.addEventListener('pointermove', onMove);
    svgEl.addEventListener('pointerdown', onMove);
    svgEl.addEventListener('mouseleave', () => {
      const hg = document.getElementById('day-hover-g');
      if (hg) hg.style.display = 'none';
      _DT.hide();
    });
  })();

  // ── Health → Sleep: score card + metrics + mini charts ────
  // Sleep-score ring: purple identity that brightens with quality — a great
  // night glows bright violet, a poor one reads dim/cool at a glance.
  function sleepRingColor(s) {
    if (s >= 85) return { col: 'oklch(0.81 0.16 300)', glow: 'oklch(0.81 0.16 300 / 0.55)' }; // restorative
    if (s >= 70) return { col: 'oklch(0.72 0.14 290)', glow: 'transparent' };                  // good
    if (s >= 55) return { col: 'oklch(0.60 0.10 300)', glow: 'transparent' };                  // fair
    return              { col: 'oklch(0.56 0.12 340)', glow: 'transparent' };                  // poor
  }
  // Score components as a left-to-right strip: each cell is one of the five
  // weighted parts, its points out of max, and a bar showing how much of that
  // max it earned. Weights mirror WEIGHTS in sleep_score.py.
  const SLEEP_COMPONENTS = [
    { key: 'c_efficiency',    label: 'Efficiency', max: 25 },
    { key: 'c_regularity',    label: 'Regularity', max: 20 },
    { key: 'c_duration',      label: 'Duration',   max: 15 },
    { key: 'c_stage_balance', label: 'Stages',     max: 25 },
    { key: 'c_physio',        label: 'Physio',     max: 15 },
  ];
  function applySleepComponents(sl) {
    const box = document.getElementById('sleep-components');
    if (!box) return;
    const totEl = document.getElementById('comp-total');
    if (!sl) {
      // Blank the strip rather than returning early, which left the PREVIOUS
      // day's component bars on screen under a day that has no score at all.
      box.innerHTML = SLEEP_COMPONENTS.map(c =>
        `<div class="sleep-metric comp"><div class="k">${c.label}</div>` +
        `<div class="v">—<span class="unit">/ ${c.max}</span></div>` +
        `<div class="comp-bar"><i style="width:0%;background:var(--ink-4);"></i></div>` +
        `</div>`).join('');
      if (totEl) totEl.textContent = '—';
      return;
    }
    let html = '';
    SLEEP_COMPONENTS.forEach(c => {
      const v = sl[c.key];
      const has = v != null;
      const pct = has ? Math.max(0, Math.min(1, v / c.max)) : 0;
      // colour the bar by how much of the component was earned
      const tone = !has ? 'var(--ink-4)'
        : pct >= 0.85 ? 'var(--accent)'
        : pct >= 0.5  ? 'var(--warn)'
        : 'var(--danger)';
      html += `<div class="sleep-metric comp">
        <div class="k">${c.label}</div>
        <div class="v">${has ? (Math.round(v * 10) / 10) : '—'}<span class="unit">/ ${c.max}</span></div>
        <div class="comp-bar"><i style="width:${(pct * 100).toFixed(0)}%;background:${tone};"></i></div>
      </div>`;
    });
    box.innerHTML = html;
    let why = document.getElementById('sleep-why');
    if (!why) { why = document.createElement('div'); why.id = 'sleep-why'; why.className = 'comp-why'; box.parentNode.appendChild(why); }
    const whyText = t => String(t).replace(/^[+-]\s*/, '').replace(/^./, c => c.toUpperCase()).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    why.innerHTML = (sl.why_pos ? `<p><span class="sign p">+</span><span>${whyText(sl.why_pos)}</span></p>` : '') + (sl.why_neg ? `<p><span class="sign n">−</span><span>${whyText(sl.why_neg)}</span></p>` : '');
    const tot = document.getElementById('comp-total');
    if (tot) tot.textContent = `${Math.round(sl.score)} / 100`;
  }

  // Phone hero summary beside the score ring. Hidden on desktop by CSS — the
  // desktop band already shows all of this in the hypnogram + metrics strip.
  function applySleepPhoneSummary(sl) {
    const box = document.getElementById('ssc-info');
    if (!box) return;
    if (!sl) {
      box.innerHTML = '<div class="ssc-row"><span class="k">No sleep recorded</span></div>';
      return;
    }
    const h = Math.floor(sl.asleep_min / 60), m = sl.asleep_min % 60;
    const win = (sl.bedtime_ts && sl.waketime_ts)
      ? `${fmtMin(sl.bedtime_ts, sl.tz_offset_min)} → ${fmtMin(sl.waketime_ts, sl.tz_offset_min)}`
      : '—';
    const tot = sl.asleep_min || 1;
    const dur = min => {
      const hh = Math.floor(min / 60), mm = min % 60;
      return hh ? `${hh}h${String(mm).padStart(2, '0')}` : `${mm}m`;
    };
    const STAGES = [
      ['Deep',  sl.deep_min,  'oklch(0.62 0.16 290)'],
      ['REM',   sl.rem_min,   'oklch(0.58 0.15 255)'],
      ['Light', sl.light_min, 'oklch(0.78 0.10 215)'],
      ['Awake', sl.awake_min, 'oklch(0.70 0.14 25)'],
    ];
    box.innerHTML =
      `<div class="ssc-row"><span class="k">Asleep</span>` +
      `<span class="ssc-v">${h}<u>h</u>${String(m).padStart(2, '0')}<u>m</u></span></div>` +
      `<div class="ssc-row"><span class="k">Window</span><span class="ssc-win">${win}</span></div>` +
      `<div class="ssc-bar">` +
        STAGES.map(([, min, col]) =>
          `<i style="width:${(min / tot * 100).toFixed(1)}%;background:${col}"></i>`).join('') +
      `</div>` +
      `<div class="ssc-legend">` +
        STAGES.map(([name, min, col]) =>
          `<span><em style="background:${col}"></em><b>${name}</b> ${dur(min)}</span>`).join('') +
      `</div>`;
  }

  function applyHealthSleep(sl, recent, hrHistory) {
    applySleepComponents(sl);
    applySleepPhoneSummary(sl);

    // A day with no scored night is a real state (travel, strap off, a night
    // that never synced) and /api/sleep/latest?date= answers it with a 404, so
    // `sl` arrives null. Reading sl.score here used to throw, and the throw was
    // swallowed by loadDayView's catch — which meant renderBioDay() and
    // renderHypnogram() never ran and the PREVIOUS day's charts stayed on
    // screen under the newly selected date. Say "no data" instead.
    if (!sl) {
      const sn0 = document.querySelector('.sleep-score-card .score-num');
      if (sn0) sn0.textContent = '—';
      const st0 = document.querySelector('.sleep-score-card .score-tag');
      if (st0) { st0.textContent = 'No data'; st0.style.color = ''; st0.style.borderColor = ''; }
      const arc0 = document.querySelector('.sleep-score-card .ring-arc');
      if (arc0) arc0.setAttribute('stroke-dashoffset', (2 * Math.PI * 60).toFixed(2));
      const heads0 = document.querySelectorAll(
        '[data-hsub-panel="sleep"] .sleep-row1 .panel .panel-head .meta');
      if (heads0[1]) heads0[1].textContent = '—';
      document.querySelectorAll('#screen-health .sleep-metric:not(.comp)').forEach(m => {
        const v = m.querySelector('.v'); if (v) v.textContent = '—';
        const s = m.querySelector('.sub'); if (s) { s.textContent = ''; s.classList.remove('up', 'down'); }
      });
      const awEl0 = document.getElementById('sleep-awakenings');
      if (awEl0) awEl0.textContent = '';
      // The 7-night mini charts are context around the viewed day, not part of
      // it, so they still render from `recent` below.
      applySleepRecent(recent, hrHistory);
      return;
    }

    const sn = document.querySelector('.sleep-score-card .score-num');
    if (sn) sn.textContent = Math.round(sl.score);
    const st = document.querySelector('.sleep-score-card .score-tag');
    if (st) st.textContent = scoreLabel(sl.score);
    const awEl = document.getElementById('sleep-awakenings');
    if (awEl) {
      const nAw = (liveHypnoStages || []).filter(s => s.stage === 'awake').length;
      awEl.textContent = `${nAw} awakening${nAw === 1 ? '' : 's'}, ${sl.awake_min || 0} min awake`;
    }

    // drive the quality ring: arc length = score%, hue = quality band
    const arc = document.querySelector('.sleep-score-card .ring-arc');
    const card = document.querySelector('.sleep-score-card');
    if (arc && card) {
      const CIRC = 2 * Math.PI * 60; // r=60
      const frac = Math.max(0, Math.min(1, sl.score / 100));
      arc.setAttribute('stroke-dashoffset', (CIRC * (1 - frac)).toFixed(2));
      const { col, glow } = sleepRingColor(sl.score);
      card.style.setProperty('--score-col', col);
      card.style.setProperty('--score-glow', glow);
    }
    // keep the tag pill in step with the ring color
    if (st) { const { col } = sleepRingColor(sl.score); st.style.color = col; st.style.borderColor = col.replace(')', ' / 0.3)'); }

    // hypnogram panel head (second .panel-head .meta in sleep row1)
    const slHeads = document.querySelectorAll('[data-hsub-panel="sleep"] .sleep-row1 .panel .panel-head .meta');
    if (slHeads[1]) slHeads[1].textContent = `${fmtMin(sl.bedtime_ts, sl.tz_offset_min)} – ${fmtMin(sl.waketime_ts, sl.tz_offset_min)}`;

    // metrics panel date header
    const wake = tzDate(sl.waketime_ts, sl.tz_offset_min);
    const MONU = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const dateStr = `${wake.getDate()} ${MONU[wake.getMonth()]} ${wake.getFullYear()}`;
    document.querySelectorAll('[data-hsub-panel="sleep"] .panel .panel-head .meta').forEach(el => {
      if (/\d+ [A-Za-z]+ \d{4}/.test(el.textContent)) el.textContent = dateStr;
    });

    // #health-date is owned by renderDayNav() now — it reflects the day being
    // viewed, which is not necessarily the day of the latest sleep session.

    // sleep metrics cells (5 columns: duration, deep, rem, light, awake)
    const metrics = document.querySelectorAll('#screen-health .sleep-metric');
    if (metrics.length >= 5) {
      const totalMin = sl.asleep_min;
      const pct = m => Math.round(m / totalMin * 100);
      metrics[0].querySelector('.v').innerHTML = fmtHM(totalMin);

      // "vs avg" was static mockup text (always "▲ +18m"). Compare against the
      // same 7-night mean the Sleep-time chart draws its dashed line at, so the
      // number here and the chart below can never disagree.
      const durSub = metrics[0].querySelector('.sub');
      if (durSub) {
        const durs = (recent || []).slice(0, 7)
          .map(r => r.asleep_min).filter(m => m > 0);
        if (durs.length >= 2) {
          const avg = durs.reduce((a, m) => a + m, 0) / durs.length;
          const diff = Math.round(totalMin - avg);
          const mag = Math.abs(diff);
          const txt = mag >= 60 ? `${Math.floor(mag / 60)}h ${pad(mag % 60)}m` : `${mag}m`;
          durSub.textContent = mag < 15 ? 'About your 7-night average' : (diff > 0 ? 'Above your 7-night average' : 'Below your 7-night average');
          durSub.classList.remove('up', 'down');
        } else {
          durSub.textContent = '';
          durSub.classList.remove('up', 'down');
        }
      }
      [[1, sl.deep_min], [2, sl.rem_min], [3, sl.light_min], [4, sl.awake_min]].forEach(([i, m]) => {
        metrics[i].querySelector('.v').innerHTML = fmtHM(m);
        metrics[i].querySelector('.sub').textContent = (i === 4 && m / totalMin < 0.005) ? 'Under 1%' : `${pct(m)}% of total`;
        metrics[i].querySelector('.sub').classList.remove('up', 'down');
      });
    }

    applySleepRecent(recent, hrHistory);
  }

  // The trailing-7-night mini charts. Split out of applyHealthSleep so a day
  // with no scored night still gets them — they are context around the viewed
  // day rather than a property of it.
  function applySleepRecent(recent, hrHistory) {
    if (recent && recent.length > 0) {
      const DAYSW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
      // Build HR lookup: night_of → {hr_min, hr_max}
      const hrMap = {};
      if (hrHistory && hrHistory.length) hrHistory.forEach(h => { hrMap[h.night_of] = h; });
      const filled = recent.slice(0, 7).reverse().map(r => {
        const hr = hrMap[r.night_of] || {};
        return {
          day:   DAYSW[tzDate(r.waketime_ts, r.tz_offset_min).getDay()],
          bedH:  tzDate(r.bedtime_ts, r.tz_offset_min).getHours() + tzDate(r.bedtime_ts, r.tz_offset_min).getMinutes() / 60,
          wakeH: tzDate(r.waketime_ts, r.tz_offset_min).getHours() + tzDate(r.waketime_ts, r.tz_offset_min).getMinutes() / 60,
          dur:   r.asleep_min / 60,
          hrMin: hr.hr_min || 0,
          hrMax: hr.hr_max || 0,
        };
      });
      SLEEP_7D.length = 0;
      filled.forEach(d => SLEEP_7D.push(d));
      // only re-render if the sleep subtab is visible
      const sleepPanel = document.querySelector('[data-hsub-panel="sleep"]');
      if (sleepPanel && sleepPanel.style.display !== 'none') {
        renderSleepTime();
        renderSleepReg();
        renderSleepHr();
      }
    }
  }

  // ── Health → Mental state ─────────────────────────────────
  // 6-zone HR model, mirrors biocharge.py HR_ZONE_THRESHOLDS (bpm above RHR)
  const HR_ZONE_TH    = [10, 28, 53, 78, 113];
  const HR_ZONE_NAMES = ['Rest', 'Light', 'Moderate', 'Aerobic', 'Hard', 'Max'];
  function hrZone(hr, rhr) {
    if (hr == null || rhr == null) return null;
    const d = hr - rhr;
    let z = 0;
    for (let i = 0; i < HR_ZONE_TH.length; i++) if (d >= HR_ZONE_TH[i]) z = i + 1;
    return { z, name: HR_ZONE_NAMES[z] };
  }

  // biocharge drain over the last ~45 min of today's series (pts/hour, negative = draining)
  function currentDrainPerHour() {
    if (!liveDaySeries || liveDaySeries.length < 2) return null;
    const now = Date.now();
    const recent = liveDaySeries.filter(p => p.minute_ts <= now && p.minute_ts >= now - 50 * 60000);
    if (recent.length < 2) return null;
    const a = recent[0], b = recent[recent.length - 1];
    const dtH = (b.minute_ts - a.minute_ts) / 3600000;
    if (dtH <= 0) return null;
    return (b.level - a.level) / dtH;
  }

  // end-of-day biocharge: prefer the server's own prediction (last future point),
  // else extrapolate to 23:00 at the current drain rate
  function projectedEndOfDay(drainPerHour) {
    if (!liveDaySeries || !liveDaySeries.length) return null;
    const now = Date.now();
    const last = liveDaySeries[liveDaySeries.length - 1];
    if (last.minute_ts > now + 10 * 60000) return { level: Math.round(last.level), ts: last.minute_ts };
    if (drainPerHour == null) return null;
    const end = new Date(); end.setHours(23, 0, 0, 0);
    const hrs = (end.getTime() - now) / 3600000;
    if (hrs <= 0) return { level: Math.round(last.level), ts: now };
    const lvl = Math.max(5, Math.min(100, last.level + drainPerHour * hrs));
    return { level: Math.round(lvl), ts: end.getTime() };
  }

  function fmtAge(ms) {
    const m = Math.max(0, Math.round(ms / 60000));
    if (m < 60) return m + 'm old';
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `${h}h ${r}m old` : `${h}h old`;
  }

  let lastSnapshot = null;
  // Strap battery chip in the PC topbar (hidden on phone via CSS)
  function applyBattery(level) {
    const el = document.getElementById('topbar-battery');
    if (!el) return;
    if (level == null) { el.style.display = 'none'; return; }
    const pct = Math.max(0, Math.min(100, Math.round(level)));
    el.style.display = '';          // CSS default (inline-flex); mobile rule still hides it
    el.classList.toggle('low', pct <= 25);
    el.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="7.5" width="16" height="9" rx="2.2"/><path d="M21 10.5v3"/><rect x="4.6" y="9.6" width="${(11.8 * pct / 100).toFixed(1)}" height="4.8" rx="1" fill="currentColor" stroke="none"/></svg><span>${pct}%</span>`;
    el.title = `Helio strap battery · ${pct}%`;
  }

  function applyVitals(v) {
    if (!v) {
      // No HR sample yet today (see vitals_snapshot()'s `fresh` flag) — blank
      // rather than returning early, which would leave a previous fetch's
      // numbers on screen looking current. Mirrors the sleep empty-state fix.
      lastSnapshot = null;
      applyBattery(null);
      ['snap-hr', 'main-hr-val', 'main-rhr', 'main-hrv', 'main-spo2'].forEach(id => {
        const el = document.getElementById(id); if (el) el.innerHTML = '—';
      });
      const mainHrTimeEl = document.getElementById('main-hr-time');
      if (mainHrTimeEl) mainHrTimeEl.textContent = '—';
      const mainHrAgeEl = document.getElementById('main-hr-age');
      if (mainHrAgeEl) { mainHrAgeEl.textContent = ''; mainHrAgeEl.classList.remove('stale'); }
      const zoneEl = document.getElementById('snap-hr-zone');
      if (zoneEl) zoneEl.textContent = '';
      const mzEl = document.getElementById('main-hr-zone');
      if (mzEl) mzEl.textContent = '—';
      const timeEl = document.getElementById('snap-hr-time');
      if (timeEl) timeEl.textContent = 'No data · please upload';
      const drainEl = document.getElementById('snap-drain');
      if (drainEl) drainEl.innerHTML = `—<span class="snap-sub">per hour</span>`;
      const projEl = document.getElementById('snap-projected');
      if (projEl) projEl.innerHTML = '—';
      return;
    }
    lastSnapshot = v;
    applyBattery(v.battery);
    const hrEl = document.getElementById('snap-hr');
    if (hrEl && v.hr != null) hrEl.innerHTML = `${v.hr}<span class="unit">bpm</span>`;
    const mainHrEl = document.getElementById('main-hr-val');
    if (mainHrEl && v.hr != null) mainHrEl.innerHTML = `${v.hr}<span class="unit">bpm</span>`;
    const mainHrTimeEl = document.getElementById('main-hr-time');
    if (mainHrTimeEl && v.hr_time) mainHrTimeEl.textContent = v.hr_time;
    const mainHrAgeEl = document.getElementById('main-hr-age');
    if (mainHrAgeEl && v.hr_ts) {
      const ageMs = Date.now() - v.hr_ts, ageM = Math.max(0, Math.round(ageMs / 60000));
      mainHrAgeEl.textContent = ageM < 60 ? `${ageM}m ago` : `${Math.floor(ageM / 60)}h ${p2(ageM % 60)}m ago`;
      mainHrAgeEl.classList.toggle('stale', ageMs > 90 * 60000);
    }

    // HR zone
    const zoneEl = document.getElementById('snap-hr-zone');
    const zone = hrZone(v.hr, v.rhr);
    if (zoneEl) zoneEl.textContent = zone ? `Zone ${zone.z}` : '';

    // Main screen hero rail — same snapshot, no extra request.
    const setHero = (id, val, unit) => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = (val == null) ? '—' : `${val}<span class="unit">${unit}</span>`;
    };
    setHero('main-rhr', v.rhr, 'bpm');
    setHero('main-hrv', v.hrv, 'ms');
    setHero('main-spo2', v.spo2, '%');
    const mzEl = document.getElementById('main-hr-zone');
    if (mzEl) mzEl.textContent = zone ? `Zone ${zone.z}` : '—';

    // freshness — "updated 09:24 · 2h old", amber past 90 min
    const timeEl = document.getElementById('snap-hr-time');
    if (timeEl) {
      if (v.hr_ts) {
        const age = Date.now() - v.hr_ts;
        const stale = age > 90 * 60000;
        timeEl.innerHTML = `${v.hr_time} · <span class="${stale ? 'stale' : ''}">${fmtAge(age).replace(' old', ' ago')}${stale ? ' ⚠' : ''}</span>`;
      } else {
        timeEl.textContent = v.hr_time || '—';
      }
    }

    // drain + projected end-of-day, from the biocharge series
    const drain = currentDrainPerHour();
    const drainEl = document.getElementById('snap-drain');
    if (drainEl) {
      if (drain == null) drainEl.innerHTML = `—<span class="snap-sub">per hour</span>`;
      else {
        const s = drain >= 0 ? '+' : '−';
        drainEl.innerHTML = `${s}${Math.abs(drain).toFixed(1)}<span class="snap-sub">per hour</span>`;
      }
    }
    const proj = projectedEndOfDay(drain);
    const projEl = document.getElementById('snap-projected');
    if (projEl) {
      projEl.innerHTML = proj
        ? `${proj.level}<span class="snap-sub">by ${fmtMin(proj.ts)}</span>`
        : '—';
    }
  }

  // Active modifiers (coffee / alcohol windows) → snapshot "ACTIVE" cell
  function applyModifiers(mods) {
    const el = document.getElementById('snap-active');
    if (!el) return;
    if (!Array.isArray(mods) || mods.length === 0) { el.textContent = 'none'; el.className = 'v none'; return; }
    el.className = 'v mods';
    el.innerHTML = mods.map(m => `${m.kind} · til ${m.until}`).join('<br>');
  }

  // ── Vitals tab: HRV / RHR / SpO2 cards with 14-day sparklines ──
  function vitalSparkScale(series, baseline) {
    const W = 240, H = 52, pad = 6;
    const vals = series.map(p => p.value);
    let lo = Math.min(...vals, baseline != null ? baseline : Infinity);
    let hi = Math.max(...vals, baseline != null ? baseline : -Infinity);
    if (hi === lo) { hi += 1; lo -= 1; }
    const span = hi - lo;
    const x = i => pad + (i / (series.length - 1)) * (W - 2 * pad);
    const y = v => pad + (1 - (v - lo) / span) * (H - 2 * pad);
    return { W, H, pad, x, y };
  }
  function fmtDayShort(dateStr) {
    const MONU = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const d = new Date(dateStr + 'T00:00:00');
    return `${d.getDate()} ${MONU[d.getMonth()]}`;
  }
  function vitalSpark(svgId, series, baseline) {
    if (!series || series.length < 2) return '';
    const { W, H, pad, x, y } = vitalSparkScale(series, baseline);
    const line = series.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(p.value).toFixed(1)).join(' ');
    const fill = `${line} L ${x(series.length - 1).toFixed(1)},${H - pad} L ${x(0).toFixed(1)},${H - pad} Z`;
    let out = `<path d="${fill}" fill="var(--accent)" opacity="0.08"/>`;
    if (baseline != null) out += `<line x1="${pad}" x2="${W - pad}" y1="${y(baseline).toFixed(1)}" y2="${y(baseline).toFixed(1)}" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 4" opacity="0.55"/>`;
    out += `<path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const lx = x(series.length - 1), ly = y(series[series.length - 1].value);
    out += `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="4" fill="var(--accent)" stroke="var(--panel)" stroke-width="2"/>`;
    out += `<line class="vs-guide" x1="0" x2="0" y1="${pad}" y2="${H - pad}" stroke="var(--accent)" stroke-width="1" stroke-dasharray="2 3" opacity="0.6" style="display:none"/>`;
    out += `<circle class="vs-cursor" r="4" fill="var(--accent)" stroke="var(--panel)" stroke-width="1.5" style="display:none"/>`;
    return `<svg class="vital-spark" id="${svgId}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${out}</svg>`;
  }
  // Vitals-tab recovery metrics: HRV / RHR / SpO₂ / skin-temp are only sampled
  // overnight (or are a once-a-day aggregate, for RHR), so they render as
  // full-size 14-day trend panels — same size as the HR/stress day charts —
  // rather than intraday curves.
  //
  // The panel readout reflects the plotted series only (its last night-bucketed
  // point), never the live snapshot. The snapshot is a ~20-min window right at
  // wake — for HRV especially it runs far above the whole-night mean the chart
  // plots, so feeding it into the readout made the header disagree with the end
  // of the line and with the hover value (128 ms header vs a dot at 105). The
  // live "now" value has its own home in the Vitals "Now" strip (vn-hrv, …).
  let lastVitalsHistory = null, lastVitalsSnapshot = null;
  function applyVitalsTab(vh, snapshot) {
    lastVitalsHistory = vh || null;
    // Keep null distinguishable from "no fields yet" (an empty object) so a
    // stale/no-data snapshot stays "no data" through rerenderVitalDays()'s
    // tab-show redraw too, instead of only on the first render.
    lastVitalsSnapshot = snapshot;
    if (!vh) return;
    const s = lastVitalsSnapshot;
    renderVitalBars ('vt-steps-chart', 'vt-steps-read', vh.steps, ' steps', 0, 'var(--hue-bio)');  // steps live in the Biocharge tab → green
    // Snapshot box: today's running step count
    const stEl = document.getElementById('snap-steps');
    if (stEl && vh.steps && vh.steps.current != null)
      stEl.innerHTML = `${Number(vh.steps.current).toLocaleString('en-US')}<span class="unit">today</span>`;
    renderVitalTrend('vt-hrv-chart',  'vt-hrv-read',  vh.hrv,  ' ms',  0);
    renderVitalTrend('vt-rhr-chart',  'vt-rhr-read',  vh.rhr,  ' bpm', 0);
    // RHR trend warning (3 nights above baseline) + full recovery banner
    // (sustained + acute nosedive) — both from /api/readiness, one request.
    fetch('/api/readiness').then(r => r.ok ? r.json() : null).then(d => {
      const w = document.getElementById('vt-rhr-warn');
      if (w) {
        if (d && d.rhr && d.rhr.warning) {
          w.textContent = '⚠ 3+ nights above baseline';
          w.style.display = '';
          const read = document.getElementById('vt-rhr-read');
          if (read) read.style.color = '#ef4444';
        } else { w.style.display = 'none'; }
      }
      const b = document.getElementById('vt-recovery-banner');
      if (b) {
        const rec = d && d.recovery;
        if (rec && rec.level && rec.message) {
          b.textContent = rec.message;
          b.classList.toggle('hard', rec.level === 'hard');
          b.style.display = 'block';
        } else { b.style.display = 'none'; }
      }
    }).catch(() => { });
    renderVitalTrend('vt-spo2-chart', 'vt-spo2-read', vh.spo2, ' %',   0);
    renderVitalTrend('vt-resp-chart', 'vt-resp-read', vh.resp,                       ' br/min', 1);
    renderVitalTrend('vt-temp-chart', 'vt-temp-read', vh.temp,                       ' °C',  1);
    applyVitalsNowStrip(vh, s);
  }

  // "Now" strip at the top of the Vitals tab. Reuses the history + snapshot
  // already loaded for the charts — deltas are vs each metric's own baseline.
  function applyVitalsNowStrip(vh, s) {
    const set = (id, val, unit, dec) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.innerHTML = (val == null || Number.isNaN(val))
        ? '—'
        : `${Number(val).toFixed(dec || 0)}<span class="unit">${unit}</span>`;
    };
    // lower is better for resting HR; higher is better for HRV; the rest are neutral
    const sub = (id, cur, base, dec, unit, lowerIsBetter) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (cur == null || base == null) { el.textContent = '—'; el.className = 'vn-s'; return; }
      const d = cur - base;
      const f = Math.abs(d).toFixed(dec || 0);
      if (Math.abs(d) < (dec ? 0.05 : 0.5)) { el.textContent = `— typical`; el.className = 'vn-s'; return; }
      const better = lowerIsBetter == null ? null : (lowerIsBetter ? d < 0 : d > 0);
      el.textContent = `${d < 0 ? '−' : '+'}${f}${unit} vs base ${Number(base).toFixed(dec || 0)}`;
      el.className = 'vn-s' + (better === null ? '' : better ? ' good' : ' bad');
    };
    const last = b => (b && Array.isArray(b.series) && b.series.length)
      ? b.series[b.series.length - 1].value : null;

    // No live snapshot today — don't fall back to the trend history's last
    // point either, or "Now" silently shows yesterday's daily figure again
    // (same bug shape as the biocharge/sleep staleness fix, 2026-09-22).
    if (!s) {
      ['vn-hr', 'vn-rhr', 'vn-hrv', 'vn-spo2', 'vn-resp', 'vn-temp'].forEach(id => set(id, null));
      ['vn-hr-sub', 'vn-rhr-sub', 'vn-hrv-sub', 'vn-spo2-sub', 'vn-resp-sub', 'vn-temp-sub'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.textContent = 'No data · please upload'; el.className = 'vn-s'; }
      });
      const hrZoneEl = document.getElementById('vn-hr-zone');
      if (hrZoneEl) hrZoneEl.textContent = '';
      return;
    }

    set('vn-hr', s.hr, 'bpm');
    const hrSub = document.getElementById('vn-hr-sub');
    if (hrSub) {
      const z = hrZone(s.hr, s.rhr);
      hrSub.textContent = s.hr_time ? `${s.hr_time}${s.hr_ts ? ' · ' + fmtAge(Date.now() - s.hr_ts).replace(' old', ' ago') : ''}` : '—';
      const hrZoneEl = document.getElementById('vn-hr-zone');
      if (hrZoneEl) hrZoneEl.textContent = z ? `Zone ${z.z}` : '';
    }
    set('vn-rhr',  s.rhr  != null ? s.rhr  : last(vh.rhr),  'bpm');
    set('vn-hrv',  s.hrv  != null ? s.hrv  : last(vh.hrv),  'ms');
    set('vn-spo2', s.spo2 != null ? s.spo2 : last(vh.spo2), '%');
    set('vn-resp', last(vh.resp), 'br/min', 1);
    set('vn-temp', last(vh.temp), '°C',  1);

    sub('vn-rhr-sub',  s.rhr  != null ? s.rhr  : last(vh.rhr),  vh.rhr  && vh.rhr.baseline,  0, '', true);
    sub('vn-hrv-sub',  s.hrv  != null ? s.hrv  : last(vh.hrv),  vh.hrv  && vh.hrv.baseline,  0, '', false);
    sub('vn-spo2-sub', s.spo2 != null ? s.spo2 : last(vh.spo2), vh.spo2 && vh.spo2.baseline, 0, '', null);
    sub('vn-resp-sub', last(vh.resp), vh.resp && vh.resp.baseline, 1, '', null);
    sub('vn-temp-sub', last(vh.temp), vh.temp && vh.temp.baseline, 1, '', null);
  }

  // ── Vitals tab: interactive intraday day charts (HR, stress) ──────────────
  const vitalDayState = {}; // svgId → { series, xAt, yAt, unit, readId }

  const vdRead = (v, unit, ts, lead) => `${lead && new Date(ts).toDateString() === new Date().toDateString() ? 'Today · ' : ''}<b>${v}${unit}</b> at ${fmtMin(ts)}`;
  function renderVitalDay(svgId, readId, series, unit, opts) {
    opts = opts || {};
    const svg = document.getElementById(svgId);
    if (!svg) return;
    const rd = document.getElementById(readId);
    if (!series || series.length < 2) { svg.innerHTML = ''; if (rd) rd.textContent = 'no data'; return; }

    const rect = svg.getBoundingClientRect();
    const W = Math.round(rect.width)  || 700;
    const H = Math.round(rect.height) || 150;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    const P = { l: 28, r: 10, t: 12, b: 18 };
    const iW = W - P.l - P.r, iH = H - P.t - P.b;

    // Anchor to the day the series belongs to, not to today — otherwise a past
    // day's samples all fall before today's midnight and xAt() clamps them to 0,
    // collapsing the curve into a vertical line at the left edge. Same trap as
    // drawBioDay(); both charts are day-windowed and must anchor the same way.
    const dayStart = new Date(series[0].ts); dayStart.setHours(0, 0, 0, 0);
    const t0 = dayStart.getTime(), dayMs = 24 * 3600 * 1000;
    const xAt = t => P.l + Math.max(0, Math.min(1, (t - t0) / dayMs)) * iW;

    const vals = series.map(p => p.v);
    let lo, hi;
    if (opts.lo != null && opts.hi != null) {
      // Fixed scale. Stress is a 0-100 index, so auto-scaling made a calm day
      // and a bad one look identical -- the curve filled the panel either way
      // and only the axis labels differed. A fixed axis is what makes two days
      // comparable at a glance.
      lo = opts.lo; hi = opts.hi;
    } else {
      lo = Math.min(...vals); hi = Math.max(...vals);
      if (opts.baseline != null) { lo = Math.min(lo, opts.baseline); hi = Math.max(hi, opts.baseline); }
      const padV = Math.max(1, (hi - lo) * 0.12); lo = Math.floor(lo - padV); hi = Math.ceil(hi + padV);
    }
    const span = (hi - lo) || 1;
    const yAt = v => P.t + iH - (v - lo) / span * iH;

    // gridlines + y labels (lo / mid / hi)
    const yticks = [...new Set([lo, Math.round((lo + hi) / 2), hi])];
    let grid = '';
    yticks.forEach(v => {
      const yp = yAt(v);
      grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yp}" y2="${yp}" stroke="rgba(255,255,255,0.06)"/>`;
      grid += `<text x="${P.l - 5}" y="${yp + 3}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="end">${v}</text>`;
    });
    let xlab = '';
    [0, 6, 12, 18, 24].forEach(h => {
      const xp = xAt(t0 + h * 3600 * 1000);
      const anchor = h === 0 ? 'start' : (h === 24 ? 'end' : 'middle');
      xlab += `<text x="${xp}" y="${H - 4}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="${anchor}">${p2(h)}:00</text>`;
    });

    const xs = series.map(p => xAt(p.ts));
    const ys = series.map(p => yAt(p.v));

    // Break the line wherever the strap stopped recording, instead of ruling a
    // straight segment across the gap — that phantom line reads as real data.
    // The threshold adapts to the series' own cadence (HR ~1/min, stress ~5/min)
    // so one rule serves both: a gap is >4x the median sample spacing, floored
    // at 4 min so ordinary jitter never splits the curve.
    const deltas = series.slice(1).map((p, i) => p.ts - series[i].ts).sort((a, b) => a - b);
    const medDelta = deltas.length ? deltas[Math.floor(deltas.length / 2)] : 60000;
    const gapMs = opts.gapMs || Math.max(4 * 60000, medDelta * 4);

    const runs = [];
    let run = [0];
    for (let i = 1; i < series.length; i++) {
      if (series[i].ts - series[i - 1].ts > gapMs) { runs.push(run); run = []; }
      run.push(i);
    }
    if (run.length) runs.push(run);

    const lp = runs.map(r =>
      r.map((i, k) => (k === 0 ? 'M' : 'L') + xs[i].toFixed(1) + ',' + ys[i].toFixed(1)).join(' ')
    ).join(' ');
    // One closed fill per run, so the shaded area breaks with the line.
    const fp = runs.filter(r => r.length > 1).map(r => {
      const d = r.map((i, k) => (k === 0 ? 'M' : 'L') + xs[i].toFixed(1) + ',' + ys[i].toFixed(1)).join(' ');
      return d + ` L${xs[r[r.length - 1]].toFixed(1)},${P.t + iH} L${xs[r[0]].toFixed(1)},${P.t + iH} Z`;
    }).join(' ');

    let baseLine = '';
    if (opts.baseline != null) {
      const by = yAt(opts.baseline);
      baseLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${by}" y2="${by}" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 4" opacity="0.5"/>`
        + `<text class="ch-t" x="${W - P.r - 4}" y="${by - 6}" text-anchor="end">Resting ${opts.baseline}</text>`;
    }

    const last = series[series.length - 1];
    let gapNote = '';
    if (new Date(last.ts).toDateString() === new Date().toDateString() && Date.now() - last.ts > 30 * 60000) {
      const gx = xs[xs.length - 1];
      gapNote = gx + 170 < W - P.r
        ? `<text class="ch-t" x="${gx + 12}" y="${Math.max(P.t + 12, ys[ys.length - 1] - 10)}">No data since ${fmtMin(last.ts)}</text>`
        : '';
    }

    svg.innerHTML = `
      <defs><linearGradient id="${svgId}-fill" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.22"/>
        <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}${xlab}${baseLine}
      <path d="${fp}" fill="url(#${svgId}-fill)"/>
      <path d="${lp}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <line class="vd-guide" x1="0" x2="0" y1="${P.t}" y2="${P.t + iH}" stroke="var(--accent)" stroke-width="1" stroke-dasharray="2 3" opacity="0.6" style="display:none"/>
      <circle class="vd-cursor" r="4" fill="var(--accent)" stroke="var(--panel)" stroke-width="1.5" style="display:none"/>
      <circle cx="${xs[xs.length - 1].toFixed(1)}" cy="${ys[ys.length - 1].toFixed(1)}" r="3.5" fill="var(--accent)"/>${gapNote}
      <g class="vd-tip" style="display:none">
        <rect class="vd-tip-bg" rx="8" height="28" fill="var(--panel-3)" stroke="var(--accent)" stroke-opacity="0.7"/>
        <text class="vd-tip-txt" font-family="Geist, Inter, sans-serif" font-weight="500" font-size="13" fill="var(--ink)"></text>
      </g>`;

    vitalDayState[svgId] = { series, xAt, yAt, unit, readId, P };
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderVitalDay(svgId, readId, series, unit, opts));
    if (rd) rd.innerHTML = vdRead(last.v, unit, last.ts, true);
    if (!svg.dataset.wired) { svg.dataset.wired = '1'; wireVitalDayScrub(svg); }
  }

  function wireVitalDayScrub(svg) {
    const scrub = clientX => {
      const st = vitalDayState[svg.id]; if (!st) return;
      const rect = svg.getBoundingClientRect();
      const vw = svg.viewBox.baseVal.width || rect.width;
      const mx = (clientX - rect.left) / rect.width * vw;
      let idx = 0, best = Infinity;
      st.series.forEach((p, i) => { const dx = Math.abs(st.xAt(p.ts) - mx); if (dx < best) { best = dx; idx = i; } });
      const p = st.series[idx];
      const cx = st.xAt(p.ts), cy = st.yAt(p.v);
      const rd = document.getElementById(st.readId);
      if (rd) rd.innerHTML = vdRead(p.v, st.unit, p.ts, false);
      const g = svg.querySelector('.vd-guide');
      if (g) { g.setAttribute('x1', cx); g.setAttribute('x2', cx); g.style.display = ''; }
      const c = svg.querySelector('.vd-cursor');
      if (c) { c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.style.display = ''; }
      // on-chart pinned tooltip: value + time floats by the cursor, clamped in-bounds
      const tip = svg.querySelector('.vd-tip');
      if (tip) {
        const label = `${p.v}${st.unit} · ${fmtMin(p.ts)}`;
        const txt = tip.querySelector('.vd-tip-txt');
        const bg  = tip.querySelector('.vd-tip-bg');
        txt.textContent = label;
        tip.style.display = '';
        const w = txt.getComputedTextLength() + 24;
        let bx = Math.max(st.P.l, Math.min((vw - st.P.r) - w, cx - w / 2));
        let by = cy - 44; if (by < 2) by = cy + 16;
        bg.setAttribute('x', bx.toFixed(1)); bg.setAttribute('y', by.toFixed(1)); bg.setAttribute('width', w.toFixed(1));
        txt.setAttribute('x', (bx + 12).toFixed(1)); txt.setAttribute('y', (by + 19).toFixed(1));
      }
    };
    const reset = () => {
      const st = vitalDayState[svg.id]; if (!st) return;
      const g = svg.querySelector('.vd-guide'); if (g) g.style.display = 'none';
      const c = svg.querySelector('.vd-cursor'); if (c) c.style.display = 'none';
      const tip = svg.querySelector('.vd-tip'); if (tip) tip.style.display = 'none';
      const last = st.series[st.series.length - 1];
      const rd = document.getElementById(st.readId);
      if (rd) rd.innerHTML = vdRead(last.v, st.unit, last.ts, true);
    };
    svg.addEventListener('pointermove', e => scrub(e.clientX));
    svg.addEventListener('pointerdown', e => scrub(e.clientX));
    svg.addEventListener('pointerup', e => scrub(e.clientX));
    // mouse hover resets on leave; touch/pen taps stay pinned so the reading
    // remains visible at any time without holding a finger down.
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') reset(); });
    svg.addEventListener('pointercancel', e => { if (e.pointerType === 'mouse') reset(); });
  }

  // ── Vitals tab: full-size 14-day trend charts (HRV, RHR, SpO₂, skin temp) ──
  // Same panel/size as the intraday day charts, but the x-axis is days and each
  // point is one night. Interactive scrub + pinned tooltip, like the day charts.
  const vitalTrendState = {}; // svgId → { series, xAt, yAt, unit, dec, readId, P, defaultRead }

  function renderVitalTrend(svgId, readId, data, unit, dec) {
    dec = dec || 0;
    const svg = document.getElementById(svgId);
    if (!svg) return;
    const rd = document.getElementById(readId);
    const series = data && data.series;
    const base = data ? data.baseline : null;
    const cur  = data ? data.current  : null;
    const defaultRead = () => {
      if (!rd) return;
      if (cur == null) { rd.textContent = 'no data'; return; }
      let t = `<b>${Number(cur).toFixed(dec)}${unit}</b>`;
      if (base != null) {
        const d = cur - base, sign = d > 0 ? '+' : (d < 0 ? '−' : '±');
        t += ` · base ${Number(base).toFixed(dec)} (${sign}${Math.abs(d).toFixed(dec)})`;
      }
      rd.innerHTML = t;
    };
    if (!series || series.length < 2) { svg.innerHTML = ''; defaultRead(); return; }

    const rect = svg.getBoundingClientRect();
    const W = Math.round(rect.width)  || 700;
    const H = Math.round(rect.height) || 150;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    const P = { l: 32, r: 10, t: 12, b: 18 };
    const iW = W - P.l - P.r, iH = H - P.t - P.b;
    const n = series.length;
    const xAt = i => P.l + (i / (n - 1)) * iW;

    const vals = series.map(p => p.value);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (base != null) { lo = Math.min(lo, base); hi = Math.max(hi, base); }
    const padV = Math.max(dec ? 0.1 : 1, (hi - lo) * 0.15); lo -= padV; hi += padV;
    const span = (hi - lo) || 1;
    const yAt = v => P.t + iH - (v - lo) / span * iH;

    const yticks = [...new Set([lo, (lo + hi) / 2, hi].map(v => Number(v).toFixed(dec)))];
    let grid = '';
    yticks.forEach(vs => {
      const yp = yAt(Number(vs));
      grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yp}" y2="${yp}" stroke="rgba(255,255,255,0.06)"/>`;
      grid += `<text x="${P.l - 5}" y="${yp + 3}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="end">${vs}</text>`;
    });
    let xlab = '';
    (W < 260 ? [0, n - 1] : [0, Math.floor((n - 1) / 2), n - 1]).forEach(i => {
      const xp = xAt(i);
      const anchor = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
      xlab += `<text x="${xp}" y="${H - 4}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="${anchor}">${fmtDayShort(series[i].date)}</text>`;
    });

    const xs = series.map((_, i) => xAt(i));
    const ys = series.map(p => yAt(p.value));
    const lp = xs.map((x, i) => (i ? 'L' : 'M') + x.toFixed(1) + ',' + ys[i].toFixed(1)).join(' ');
    const fp = lp + ` L${xs[n - 1].toFixed(1)},${P.t + iH} L${xs[0].toFixed(1)},${P.t + iH} Z`;

    let baseLine = '';
    if (base != null) {
      const by = yAt(base);
      baseLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${by}" y2="${by}" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 4" opacity="0.5"/>`;
    }

    svg.innerHTML = `
      <defs><linearGradient id="${svgId}-fill" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.22"/>
        <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}${xlab}${baseLine}
      <path d="${fp}" fill="url(#${svgId}-fill)"/>
      <path d="${lp}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <line class="vd-guide" x1="0" x2="0" y1="${P.t}" y2="${P.t + iH}" stroke="var(--accent)" stroke-width="1" stroke-dasharray="2 3" opacity="0.6" style="display:none"/>
      <circle class="vd-cursor" r="4" fill="var(--accent)" stroke="var(--panel)" stroke-width="1.5" style="display:none"/>
      <circle cx="${xs[n - 1].toFixed(1)}" cy="${ys[n - 1].toFixed(1)}" r="3.5" fill="var(--accent)"/>
      <g class="vd-tip" style="display:none">
        <rect class="vd-tip-bg" rx="8" height="28" fill="var(--panel-3)" stroke="var(--accent)" stroke-opacity="0.7"/>
        <text class="vd-tip-txt" font-family="Geist, Inter, sans-serif" font-weight="500" font-size="13" fill="var(--ink)"></text>
      </g>`;

    vitalTrendState[svgId] = { series, xAt, yAt, unit, dec, readId, P, defaultRead };
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderVitalTrend(svgId, readId, data, unit, dec));
    defaultRead();
    if (!svg.dataset.wired) { svg.dataset.wired = '1'; wireVitalTrendScrub(svg); }
  }

  function wireVitalTrendScrub(svg) {
    const scrub = clientX => {
      const st = vitalTrendState[svg.id]; if (!st) return;
      const rect = svg.getBoundingClientRect();
      const vw = svg.viewBox.baseVal.width || rect.width;
      const mx = (clientX - rect.left) / rect.width * vw;
      let idx = 0, best = Infinity;
      st.series.forEach((p, i) => { const dx = Math.abs(st.xAt(i) - mx); if (dx < best) { best = dx; idx = i; } });
      const p = st.series[idx];
      const cx = st.xAt(idx), cy = st.yAt(p.value);
      const rd = document.getElementById(st.readId);
      if (rd) rd.textContent = `${Number(p.value).toLocaleString('en-US', { minimumFractionDigits: st.dec, maximumFractionDigits: st.dec })}${st.unit} · ${fmtDayShort(p.date)}`;
      const g = svg.querySelector('.vd-guide');
      if (g) { g.setAttribute('x1', cx); g.setAttribute('x2', cx); g.style.display = ''; }
      const c = svg.querySelector('.vd-cursor');
      if (c) { c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.style.display = ''; }
      const tip = svg.querySelector('.vd-tip');
      if (tip) {
        const label = `${Number(p.value).toLocaleString('en-US', { minimumFractionDigits: st.dec, maximumFractionDigits: st.dec })}${st.unit} · ${fmtDayShort(p.date)}`;
        const txt = tip.querySelector('.vd-tip-txt');
        const bg  = tip.querySelector('.vd-tip-bg');
        txt.textContent = label;
        tip.style.display = '';
        const w = txt.getComputedTextLength() + 24;
        let bx = Math.max(st.P.l, Math.min((vw - st.P.r) - w, cx - w / 2));
        let by = cy - 44; if (by < 2) by = cy + 16;
        bg.setAttribute('x', bx.toFixed(1)); bg.setAttribute('y', by.toFixed(1)); bg.setAttribute('width', w.toFixed(1));
        txt.setAttribute('x', (bx + 12).toFixed(1)); txt.setAttribute('y', (by + 19).toFixed(1));
      }
    };
    const reset = () => {
      const st = vitalTrendState[svg.id]; if (!st) return;
      const g = svg.querySelector('.vd-guide'); if (g) g.style.display = 'none';
      const c = svg.querySelector('.vd-cursor'); if (c) c.style.display = 'none';
      const tip = svg.querySelector('.vd-tip'); if (tip) tip.style.display = 'none';
      st.defaultRead();
    };
    svg.addEventListener('pointermove', e => scrub(e.clientX));
    svg.addEventListener('pointerdown', e => scrub(e.clientX));
    svg.addEventListener('pointerup', e => scrub(e.clientX));
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') reset(); });
    svg.addEventListener('pointercancel', e => { if (e.pointerType === 'mouse') reset(); });
  }

  // Daily-count bars (steps) — same panel size + scrub/tooltip as the trend
  // charts (reuses vitalTrendState + wireVitalTrendScrub). Bars start at 0;
  // today's bar is a partial running total, drawn dimmer.
  function renderVitalBars(svgId, readId, data, unit, dec, color) {
    dec = dec || 0;
    color = color || 'var(--accent)';
    const svg = document.getElementById(svgId);
    if (!svg) return;
    const rd = document.getElementById(readId);
    const series = data && data.series;
    const base = data ? data.baseline : null;
    const cur  = data ? data.current  : null;
    const fmtN = v => Number(v).toLocaleString('en-US');
    const defaultRead = () => {
      if (!rd) return;
      if (cur == null && !(series && series.length)) { rd.textContent = 'no data'; return; }
      const shown = cur != null ? cur : series[series.length - 1].value;
      let t = `${fmtN(shown)}${unit}`;
      if (base != null) t += ` · median ${fmtN(base)}`;
      rd.textContent = t;
    };
    if (!series || series.length < 1) { svg.innerHTML = ''; defaultRead(); return; }

    const rect = svg.getBoundingClientRect();
    const W = Math.round(rect.width)  || 700;
    const H = Math.round(rect.height) || 150;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const P = { l: 32, r: 10, t: 12, b: 18 };
    const iW = W - P.l - P.r, iH = H - P.t - P.b;
    const n = series.length;
    const xAt = i => P.l + (i + 0.5) / n * iW;           // bar centre
    const bw  = Math.min(40, Math.max(2, (iW / n) * 0.66));
    let hi = Math.max(...series.map(p => p.value), base != null ? base : 0) * 1.15 || 1;
    const niceStep = hi >= 12000 ? 10000 : (hi >= 6000 ? 5000 : 0);
    if (niceStep) hi = Math.ceil(hi / niceStep) * niceStep;
    const yAt = v => P.t + iH - (v / hi) * iH;           // 0-based bars

    const kfmt = v => (v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + 'k' : String(v));
    let grid = '';
    [...new Set([0, Math.round(hi / 2), Math.round(hi)])].forEach(v => {
      const yp = yAt(v);
      grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yp}" y2="${yp}" stroke="rgba(255,255,255,0.06)"/>`;
      grid += `<text x="${P.l - 5}" y="${yp + 3}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="end">${kfmt(v)}</text>`;
    });
    let xlab = '';
    [0, Math.floor((n - 1) / 2), n - 1].forEach(i => {
      const anchor = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
      xlab += `<text x="${xAt(i)}" y="${H - 4}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="${anchor}">${fmtDayShort(series[i].date)}</text>`;
    });

    const today = new Date();
    const todayKey = `${today.getFullYear()}-${p2(today.getMonth() + 1)}-${p2(today.getDate())}`;
    const bars = series.map((pt, i) => {
      const x = xAt(i) - bw / 2, y = yAt(pt.value), h = Math.max(0, (P.t + iH) - y);
      const partial = pt.date === todayKey;
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="${color}" opacity="${partial ? 0.45 : 0.85}"/>`;
    }).join('');

    let baseLine = '';
    if (base != null) {
      const by = yAt(base);
      baseLine = `<line x1="${P.l}" x2="${W - P.r}" y1="${by}" y2="${by}" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 4" opacity="0.5"/>`;
    }

    svg.innerHTML = `
      ${grid}${xlab}${baseLine}${bars}
      <line class="vd-guide" x1="0" x2="0" y1="${P.t}" y2="${P.t + iH}" stroke="${color}" stroke-width="1" stroke-dasharray="2 3" opacity="0.6" style="display:none"/>
      <circle class="vd-cursor" r="4" fill="${color}" stroke="var(--panel)" stroke-width="1.5" style="display:none"/>
      <g class="vd-tip" style="display:none">
        <rect class="vd-tip-bg" rx="8" height="28" fill="var(--panel-3)" stroke="${color}" stroke-opacity="0.7"/>
        <text class="vd-tip-txt" font-family="Geist, Inter, sans-serif" font-weight="500" font-size="13" fill="var(--ink)"></text>
      </g>`;

    vitalTrendState[svgId] = { series, xAt, yAt, unit, dec, readId, P, defaultRead };
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderVitalBars(svgId, readId, data, unit, dec, color));
    defaultRead();
    if (!svg.dataset.wired) { svg.dataset.wired = '1'; wireVitalTrendScrub(svg); }
  }

  let lastIntraday = null;
  function applyVitalsIntraday(d) {
    lastIntraday = d || null;
    if (!d) {
      // Explicitly clear rather than leaving a previous fetch's chart on
      // screen — reuses renderVitalDay's existing "no data" readout.
      renderVitalDay('vd-hr-chart', 'vd-hr-read', null, ' bpm', {});
      renderVitalDay('vd-stress-chart', 'vd-stress-read', null, '', {});
      return;
    }
    renderVitalDay('vd-hr-chart', 'vd-hr-read', d.hr, ' bpm', { baseline: d.rhr });
    renderVitalDay('vd-stress-chart', 'vd-stress-read', d.stress, '', { lo: 0, hi: 100 });
  }
  // ---------- Food tab: today's intake, split by meal ----------
  // All six slots always render, even empty: "you have eaten no protein
  // since breakfast" is exactly the thing worth seeing.
  let lastFoodDay = null;
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const SLOT_TIMES = {
    breakfast: '6–9:30', morning_snack: '10–11:30', lunch: '12–14:30',
    afternoon_snack: '16–18', dinner: '19:30–22', off_hours: 'between meals',
  };

  function applyFoodDay(d) {
    lastFoodDay = d || lastFoodDay;
    const day = lastFoodDay;
    const set = (id, txt, cls) => {
      const e = document.getElementById(id);
      if (!e) return;
      e.textContent = txt;
      if (cls !== undefined) e.className = cls;
    };
    if (!day) return;

    const p = day.protein || null;
    const t = day.totals  || {};
    { const dp = String(day.date || '').split('-'); set('fd-date', dp.length === 3 ? `${Number(dp[2])} ${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][Number(dp[1]) - 1]} ${dp[0]}` : (day.date || '—')); }
    // These two read "today" in the live view; in past mode they must not
    // claim a past day's numbers are today's.
    const past = viewDate !== null;
    const dl = document.getElementById('fd-daylabel');
    if (dl) dl.textContent = past ? 'That day' : 'Today';
    const cs = document.getElementById('cal-daysub');
    if (cs) cs.textContent = past ? `· ${viewDate}` : '· today';

    // Protein ring. No target (no weight logged) means no ring rather than a
    // fabricated denominator.
    const arc = document.getElementById('fd-arc');
    const pct = p && p.target_g ? Math.min(100, Math.round(p.logged_g / p.target_g * 100)) : null;
    if (arc) arc.style.strokeDashoffset = String(327 - 327 * (pct == null ? 0 : pct / 100));
    { const pe = document.getElementById('fd-pct'); if (pe) pe.innerHTML = pct == null ? '—' : pct + '<span class="unit">%</span>'; }
    set('fd-protein', p ? String(Math.round(p.logged_g)) : String(Math.round(t.protein_g || 0)));
    set('fd-target', p && p.target_g ? '/ ' + p.target_g + ' g' : '/ — g');
    set('fd-remaining', p && p.target_g
      ? (p.remaining_g > 0 ? Math.round(p.remaining_g) + ' g to go' : 'target reached')
      : 'log a weight to get a target');
    { const ce = document.getElementById('fd-carbs'), fe = document.getElementById('fd-fat');
      if (ce) ce.innerHTML = Math.round(t.carbs_g || 0) + '<span class="unit">g</span>';
      if (fe) fe.innerHTML = Math.round(t.fat_g || 0) + '<span class="unit">g</span>'; }

    // Macro split by calorie contribution (4/4/9 kcal per gram), not by mass
    // — grams would make fat look like a rounding error at twice the energy.
    const kp = (t.protein_g || 0) * 4, kc = (t.carbs_g || 0) * 4, kf = (t.fat_g || 0) * 9;
    const ktot = kp + kc + kf;
    const share = v => (ktot ? (v / ktot * 100) : 0).toFixed(1) + '%';
    const pctOf = v => (ktot ? Math.round(v / ktot * 100) + '%' : '—');
    const bar = (id, v) => {
      const e = document.getElementById(id);
      if (e) e.style.width = share(v);
    };
    bar('fd-sp', kp); bar('fd-sc', kc); bar('fd-sf', kf);
    const lg = (id, name, g, v) => { const e = document.getElementById(id); if (e) e.innerHTML = `${name} <b>${Math.round(g || 0)} g</b> · ${pctOf(v)}`; };
    lg('fd-lp', 'Protein', t.protein_g, kp); lg('fd-lc', 'Carbs', t.carbs_g, kc); lg('fd-lf', 'Fat', t.fat_g, kf);

    // Energy out is rounded to 10 kcal everywhere (the model is good to
    // ~±10-15%), and the balance is computed from the *displayed* figures so
    // the three numbers on screen actually subtract to each other.
    const inK  = Math.round(t.kcal || 0);
    const out10 = day.energy_out == null ? null : Math.round(day.energy_out / 10) * 10;
    const bal   = out10 == null ? null : inK - out10;
    set('fd-in',  inK.toLocaleString('en-US'));
    set('fd-out', out10 == null ? '—' : out10.toLocaleString('en-US'));
    set('fd-bal', bal == null ? '—'
      : (bal > 0 ? '+' : '−') + Math.abs(bal).toLocaleString('en-US'),
      'v' + (bal == null ? '' : bal > 0 ? ' over' : ' under'));

    renderMealGrid(day.slots || []);
  }

  function renderMealGrid(slots) {
    const grid = document.getElementById('meal-grid');
    if (!grid) return;
    grid.innerHTML = slots.map(s => {
      const rows = groupSlotEntries(s.entries || []);
      const body = rows.length
        ? '<div class="mcol-rows">' + rows.join('') + '</div>'
        : '<div class="mcol-empty">Nothing logged</div>';
      return '<div class="meal-col' + (s.entries.length ? ' filled' : '') + '"'
        + ' data-slot="' + s.slot + '">'
        + '<div class="mcol-head">'
        +   '<span>' + esc(s.label)
        +     ' <span class="time">' + (SLOT_TIMES[s.slot] || '') + '</span></span>'
        +   '<button class="meal-add" data-add="' + s.slot + '" title="Log something here">+</button>'
        + '</div>'
        + body
        + '<div class="mcol-foot"><span>' + Math.round(s.kcal) + ' kcal</span>'
        +   '<span><b>' + s.protein_g + '</b> g P</span></div>'
        + '</div>';
    }).join('');
  }

  // A dish shows as its name with its ingredients underneath, matching how it
  // was logged; a standalone item is just one row.
  function groupSlotEntries(entries) {
    const out = [];
    for (let i = 0; i < entries.length; ) {
      const e = entries[i];
      if (!e.meal_id) {
        out.push('<div class="mrow" data-id="' + e.id + '">'
          + '<span class="n">' + esc(e.name) + '</span>'
          + '<span class="kc">' + Math.round(e.kcal || 0) + '</span>'
          + '<span class="g">' + Math.round(e.grams) + ' g</span></div>');
        i++;
        continue;
      }
      const group = [];
      while (i < entries.length && entries[i].meal_id === e.meal_id) group.push(entries[i++]);
      const kc = group.reduce((a, r) => a + (r.kcal || 0), 0);
      out.push('<div class="mrow is-meal" data-meal="' + esc(e.meal_id) + '">'
        + '<span class="n">' + esc(group[0].meal_name || 'Meal') + '</span>'
        + '<span class="kc">' + Math.round(kc) + '</span></div>'
        + group.map(r => '<div class="mrow part">'
            + '<span class="n">' + esc(r.name) + '</span>'
            + '<span class="g">' + Math.round(r.grams) + ' g</span></div>').join(''));
    }
    return out;
  }

  // Tapping an entry moves it between meals — the correction path for
  // anything the clock filed in the wrong slot.
  const SLOT_ORDER = ['breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner', 'off_hours'];
  const SLOT_NAMES = ['Breakfast', 'Morning snack', 'Lunch', 'Afternoon snack', 'Dinner', 'Off hours'];
  // Drag a row onto another meal. Pointer events cover mouse and touch with
  // one implementation; on touch a short hold starts the drag, so a plain
  // swipe still scrolls the page.
  let justDragged = false;
  (function initMealDrag() {
    const grid = document.getElementById('meal-grid');
    if (!grid) return;
    const HOLD_MS = 260, MOVE_TOL = 8, EDGE = 70, EDGE_SPEED = 12;
    let st = null, ghost = null, raf = 0, lastY = 0;

    function clearTargets() {
      grid.querySelectorAll('.drop-target').forEach(c => c.classList.remove('drop-target'));
    }
    function reset() {
      if (st) { clearTimeout(st.timer); st.row.classList.remove('dragging'); }
      if (ghost) { ghost.remove(); ghost = null; }
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      clearTargets();
      st = null;
    }
    function moveGhost(x, y) {
      if (ghost) { ghost.style.left = (x + 14) + 'px'; ghost.style.top = (y - 16) + 'px'; }
    }
    // On a phone all six meal columns are stacked, so the target is often
    // off screen — the page has to follow the finger.
    function edgeScroll() {
      if (lastY < EDGE) window.scrollBy(0, -EDGE_SPEED);
      else if (lastY > window.innerHeight - EDGE) window.scrollBy(0, EDGE_SPEED);
      raf = requestAnimationFrame(edgeScroll);
    }
    function beginDrag() {
      st.dragging = true;
      st.row.classList.add('dragging');
      ghost = document.createElement('div');
      ghost.className = 'food-drag-ghost';
      ghost.innerHTML = st.row.innerHTML;
      document.body.appendChild(ghost);
      moveGhost(st.x, st.y);
      raf = requestAnimationFrame(edgeScroll);
    }
    const colUnder = (x, y) => {
      const el = document.elementFromPoint(x, y);
      return el ? el.closest('.meal-col') : null;
    };

    grid.addEventListener('pointerdown', e => {
      const row = e.target.closest('.mrow');
      // Ingredient rows are not separately movable: a dish moves whole.
      if (!row || row.classList.contains('part')) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      reset();
      st = { row, x: e.clientX, y: e.clientY, dragging: false, aborted: false,
             touch: e.pointerType !== 'mouse',
             from: row.closest('.meal-col').dataset.slot, timer: 0 };
      lastY = e.clientY;
      if (st.touch) st.timer = setTimeout(() => { if (st && !st.aborted) beginDrag(); }, HOLD_MS);
    });

    window.addEventListener('pointermove', e => {
      if (!st) return;
      lastY = e.clientY;
      if (!st.dragging) {
        const far = Math.hypot(e.clientX - st.x, e.clientY - st.y) > MOVE_TOL;
        if (!far) return;
        // Moving before the hold completes means it was a scroll all along.
        if (st.touch) { st.aborted = true; reset(); return; }
        beginDrag();
      }
      moveGhost(e.clientX, e.clientY);
      clearTargets();
      const col = colUnder(e.clientX, e.clientY);
      if (col && col.dataset.slot !== st.from) col.classList.add('drop-target');
    });

    window.addEventListener('pointerup', async e => {
      if (!st) return;
      const dragging = st.dragging, row = st.row, from = st.from;
      const col = dragging ? colUnder(e.clientX, e.clientY) : null;
      reset();
      if (!dragging) return;
      // A completed drag must not also fire the tap-to-move prompt.
      justDragged = true;
      setTimeout(() => { justDragged = false; }, 250);
      if (!col || col.dataset.slot === from) return;
      const url = row.dataset.meal
        ? '/api/food/log/meal/' + encodeURIComponent(row.dataset.meal)
        : '/api/food/log/' + row.dataset.id;
      await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify({ slot: col.dataset.slot }) }).catch(() => {});
      refreshFoodDay();
    });

    window.addEventListener('pointercancel', reset);
    // Non-passive so the page stops scrolling once a drag has taken over.
    grid.addEventListener('touchmove', e => {
      if (st && st.dragging) e.preventDefault();
    }, { passive: false });
  })();

  // Tapping a row opens its actions. This is also the only place a logged
  // entry can be removed now that the modal no longer lists the day.
  let rowMenu = null;
  function closeRowMenu() {
    if (rowMenu) { rowMenu.remove(); rowMenu = null; }
  }
  document.addEventListener('click', e => {
    if (rowMenu && !e.target.closest('.row-menu')) closeRowMenu();
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeRowMenu(); });
  window.addEventListener('resize', closeRowMenu);

  function urlFor(row) {
    return row.dataset.meal
      ? '/api/food/log/meal/' + encodeURIComponent(row.dataset.meal)
      : '/api/food/log/' + row.dataset.id;
  }

  // What "Check info" reads — the entries already sitting in lastFoodDay
  // from the day fetch, not a new request. A row's own DOM only carries a
  // name and a rounded gram figure; kcal/protein/carbs/fat live here.
  function entryById(id) {
    for (const s of (lastFoodDay && lastFoodDay.slots) || [])
      for (const e of s.entries || []) if (e.id === id) return e;
    return null;
  }
  function mealComponents(mealId) {
    const out = [];
    for (const s of (lastFoodDay && lastFoodDay.slots) || [])
      for (const e of s.entries || []) if (e.meal_id === mealId) out.push(e);
    return out;
  }
  function macroLine(e) {
    const p = e.protein_g, c = e.carbs_g, f = e.fat_g;
    return Math.round(e.kcal || 0) + ' kcal'
      + (p != null ? ' · ' + (Math.round(p * 10) / 10) + ' g P' : '')
      + (c != null ? ' · ' + (Math.round(c * 10) / 10) + ' g C' : '')
      + (f != null ? ' · ' + (Math.round(f * 10) / 10) + ' g F' : '');
  }

  // Tap opens a plain two-way choice; either sub-screen replaces the
  // content of the same popover and re-anchors it, so a taller "Check
  // info" screen never spills off the row it hangs from.
  function openRowMenu(row, from) {
    closeRowMenu();
    const isMeal = !!row.dataset.meal;
    const label = row.querySelector('.n').textContent.trim();
    const m = document.createElement('div');
    m.className = 'row-menu';
    document.body.appendChild(m);
    rowMenu = m;

    function place() {
      const r = row.getBoundingClientRect(), b = m.getBoundingClientRect();
      let left = r.left, top = r.bottom + 6;
      if (left + b.width > window.innerWidth - 8) left = window.innerWidth - b.width - 8;
      if (top + b.height > window.innerHeight - 8) top = Math.max(8, r.top - b.height - 6);
      m.style.left = Math.max(8, left) + 'px';
      m.style.top  = top + 'px';
    }

    function showRoot() {
      m.innerHTML = '<div class="rm-k">' + esc(label) + '</div>'
        + '<button data-go="info">Check info</button>'
        + '<button data-go="move">Change meal</button>';
      place();
    }

    function showInfo() {
      let body;
      if (isMeal) {
        const parts = mealComponents(row.dataset.meal);
        const tot = parts.reduce((a, r2) => ({
          kcal: a.kcal + (r2.kcal || 0), protein: a.protein + (r2.protein_g || 0),
          carbs: a.carbs + (r2.carbs_g || 0), fat: a.fat + (r2.fat_g || 0),
        }), { kcal: 0, protein: 0, carbs: 0, fat: 0 });
        body = '<div class="rm-info-line"><b>Total</b> — ' + macroLine({
            kcal: tot.kcal, protein_g: tot.protein, carbs_g: tot.carbs, fat_g: tot.fat,
          }) + '</div>'
          + parts.map(pt => '<div class="rm-info-part">'
              + '<div class="rm-info-pname">' + esc(pt.name) + ' · ' + Math.round(pt.grams) + ' g</div>'
              + '<div class="rm-info-pmacro">' + macroLine(pt) + '</div></div>').join('');
      } else {
        const e = entryById(+row.dataset.id);
        body = e
          ? '<div class="rm-info-g">' + Math.round(e.grams) + ' g'
              + (e.ts ? ' <span class="rm-info-time">' + esc(e.ts.slice(11, 16)) + '</span>' : '')
              + '</div>'
            + '<div class="rm-info-line">' + macroLine(e) + '</div>'
          : '<div class="rm-info-line">Not found — try reopening.</div>';
      }
      m.innerHTML = '<div class="rm-k">' + esc(label) + '</div>' + body
        + '<hr><button class="rm-back" data-go="root">‹ Back</button>';
      place();
    }

    function showMove() {
      m.innerHTML = '<div class="rm-k">Move to</div>'
        + SLOT_ORDER.map((sl, i) => sl === from
            ? '<button class="here" disabled>' + SLOT_NAMES[i]
              + '<span class="rm-when">here</span></button>'
            : '<button data-slot="' + sl + '">' + SLOT_NAMES[i]
              + '<span class="rm-when">' + SLOT_TIMES[sl] + '</span></button>').join('')
        + '<hr>'
        + '<button class="danger" data-del="1">Remove' + (isMeal ? ' dish' : '') + '</button>'
        + '<button class="rm-back" data-go="root">‹ Back</button>';
      place();
    }

    m.addEventListener('click', async ev => {
      const btn = ev.target.closest('button');
      if (!btn || btn.disabled) return;
      if (btn.dataset.go === 'info') { showInfo(); return; }
      if (btn.dataset.go === 'move') { showMove(); return; }
      if (btn.dataset.go === 'root') { showRoot(); return; }
      closeRowMenu();
      if (btn.dataset.del) {
        if (!window.confirm('Remove ' + label + ' from today?')) return;
        await fetch(urlFor(row), { method: 'DELETE' }).catch(() => {});
      } else {
        await fetch(urlFor(row), {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slot: btn.dataset.slot }) }).catch(() => {});
      }
      refreshFoodDay();
    });

    showRoot();
  }

  document.getElementById('meal-grid').addEventListener('click', e => {
    const add = e.target.closest('[data-add]');
    if (add) {
      // Open already pointed at the meal whose + was tapped.
      if (window.openInfoModal) window.openInfoModal('food', add.dataset.add);
      return;
    }
    if (justDragged) return;   // the drag already moved it
    const row = e.target.closest('.mrow');
    if (!row || row.classList.contains('part')) return;
    openRowMenu(row, row.closest('.meal-col').dataset.slot);
  });

  // ---------- Weight ----------
  // The 7-day mean leads, not the last reading: a single weigh-in moves ±1-2 kg
  // on water and glycogen alone, so the daily number is mostly noise and the
  // trend is the only part that carries information.
  let lastWeight = null;

  function applyWeight(w) {
    lastWeight = w || lastWeight;
    const d = lastWeight;
    const set = (id, txt, cls) => {
      const e = document.getElementById(id);
      if (!e) return;
      e.textContent = txt;
      if (cls !== undefined) e.className = cls;
    };
    if (!d) return;
    const series = (d.series || []).filter(r => r.weight_kg != null);
    const shown = d.avg_7d != null ? d.avg_7d
                : (d.latest ? d.latest.weight_kg : null);
    set('wt-avg', shown == null ? '—' : shown.toFixed(1));
    set('wt-k', d.avg_7d != null ? '7-day average' : 'latest reading');

    // Needs two full weeks before a weekly trend means anything; say so
    // rather than drawing an arrow from three points.
    const t = d.trend_kg_per_week;
    if (t == null) {
      set('wt-trend', series.length ? 'trend needs ~2 weeks' : '', 'wt-trend flat');
    } else {
      const dir = Math.abs(t) < 0.05 ? 'flat' : (t > 0 ? 'up' : 'down');
      const sign = t > 0 ? '+' : (t < 0 ? '−' : '');
      set('wt-trend', dir === 'flat' ? 'holding steady'
          : sign + Math.abs(t).toFixed(2) + ' kg/wk', 'wt-trend ' + dir);
    }
    set('wt-latest', d.latest ? d.latest.weight_kg.toFixed(1) + ' kg · ' + d.latest.date : 'never logged');
    set('wt-count', series.length ? series.length + (series.length === 1 ? ' entry' : ' entries') : '');
    renderWeightChart(series);
  }

  function renderWeightChart(series) {
    const svg = document.getElementById('wt-chart');
    if (!svg) return;
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderWeightChart(series));
    const box = svg.getBoundingClientRect();
    const W = Math.max(120, Math.round(box.width || 260));
    const H = Math.max(50, Math.round(box.height || 74));
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    if (series.length < 2) {
      svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle"
        dominant-baseline="middle" fill="var(--ink-3)" font-size="12"
        font-family="var(--mono)" opacity="0.7">${
          series.length ? 'one weigh-in so far' : 'no weigh-ins yet'}</text>`;
      return;
    }
    const pad = 8;
    const vals = series.map(r => r.weight_kg);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 0.6) { const m = (hi + lo) / 2; lo = m - 0.3; hi = m + 0.3; }
    const t0 = new Date(series[0].date).getTime();
    const t1 = new Date(series[series.length - 1].date).getTime();
    const span = Math.max(1, t1 - t0);
    const x = r => pad + (new Date(r.date).getTime() - t0) / span * (W - pad * 2);
    const y = v => pad + (1 - (v - lo) / (hi - lo)) * (H - pad * 2);
    const pts = series.map(r => `${x(r).toFixed(1)},${y(r.weight_kg).toFixed(1)}`);
    const last = series[series.length - 1];
    svg.innerHTML =
      `<polyline points="${pts.join(' ')}" fill="none" stroke="var(--accent)"
         stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`
      + series.map(r => `<circle cx="${x(r).toFixed(1)}" cy="${y(r.weight_kg).toFixed(1)}"
           r="2" fill="var(--accent)" opacity="0.55"/>`).join('')
      + `<circle cx="${x(last).toFixed(1)}" cy="${y(last.weight_kg).toFixed(1)}"
           r="3.5" fill="var(--accent)"/>`;
  }

  async function refreshWeight() {
    try { applyWeight(await fetch('/api/body/weight?days=90').then(r => r.json())); }
    catch { /* keep the last good render */ }
  }
  // The weight input lives in the modal; the panel behind it must follow.
  window.refreshFoodWeight = refreshWeight;

  const wtAdd = document.getElementById('wt-add');
  if (wtAdd) wtAdd.addEventListener('click', () => {
    if (window.openInfoModal) window.openInfoModal('food');
  });

  async function refreshFoodDay() {
    try { applyFoodDay(await fetch('/api/food/today').then(r => r.json())); }
    catch { /* keep the last good render */ }
  }
  // Logging happens in the modal; this tab is where the result shows up.
  window.refreshFoodDay = refreshFoodDay;

  // ---------- Energy out (daily calorie estimate) ----------
  // Everything is rounded to 10 kcal: the model is good to roughly ±10-15%
  // uncalibrated, so showing a precise figure would overstate it.
  let lastCalToday = null, lastCalHistory = null;
  const r10 = v => (v == null ? null : Math.round(v / 10) * 10);
  const kfmt = v => (v == null ? '—' : Number(r10(v)).toLocaleString('en-US'));

  function applyCalories(today, history) {
    lastCalToday = today || null;
    lastCalHistory = history || null;
    const set = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };

    if (!today || !today.available) {
      set('cal-now', '—');
      set('cal-cov', 'no data');
      renderCalChart(null);
      return;
    }

    const big = document.getElementById('cal-now');
    if (big) big.innerHTML = `${kfmt(today.total)}<span class="unit">kcal</span>`;
    set('cal-proj', today.projected != null ? kfmt(today.projected) : '—');
    set('cal-avg',  today.avg_7d    != null ? kfmt(today.avg_7d)    : '—');
    set('cal-bmr',  kfmt(today.bmr_kcal_day));
    set('cal-cov',  `${Math.round(today.coverage_pct)}% strap coverage`);
    renderDayActivities(today.date);

    // Low coverage means minutes were imputed, so say so rather than letting
    // a soft number read as a measured one.
    const flag = document.getElementById('cal-flag');
    if (flag) {
      flag.style.display = today.low_coverage ? '' : 'none';
      flag.textContent = today.low_coverage ? '⚠ low strap coverage' : '';
    }

    // Three bands, not two. "Active" used to mean everything above resting,
    // which lumped a football match together with walking to the kitchen.
    // Deliberate sessions get their own colour (the workout hue) so the day's
    // shape is readable: how much was living, how much was training.
    const rest = Math.max(0, today.rest || 0);
    const actAll = Math.max(0, today.active || 0);
    const rb = document.getElementById('cal-bar-rest');
    const mb = document.getElementById('cal-bar-move');
    const ab = document.getElementById('cal-bar-act');
    const paint = (sessions) => {
      const ses = Math.max(0, Math.min(actAll, sessions || 0));
      const move = Math.max(0, actAll - ses);
      const tot = rest + move + ses || 1;
      if (rb) rb.style.width = (rest / tot * 100).toFixed(1) + '%';
      if (mb) mb.style.width = (move / tot * 100).toFixed(1) + '%';
      if (ab) ab.style.width = (ses  / tot * 100).toFixed(1) + '%';
      set('cal-leg-rest', `resting ${kfmt(rest)}`);
      set('cal-leg-move', `movement ${kfmt(move)}`);
      set('cal-leg-act',  `activities ${kfmt(ses)}`);
    };
    paint(0);
    fetch(`/api/energy/day-activities?date=${today.date}`)
      .then(r => r.json()).then(d => paint(d.total_kcal || 0))
      .catch(() => {});

    renderCalChart(history);
  }

  function renderCalChart(history) {
    const svg = document.getElementById('cal-chart');
    if (!svg) return;
    const rows = (history && history.series) ? history.series.slice(-14) : [];
    if (!rows.length) { renderChartEmpty(svg, 320, 96); return; }

    // Measured viewBox so 1 unit = 1px — the text-stretch trap that has bitten
    // the hypnogram, bedtime curve and net-worth charts before.
    const rect = svg.getBoundingClientRect();
    const W = Math.round(rect.width) || 320;
    const H = Math.round(rect.height) || 96;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderCalChart(history));
    const P = { l: 4, r: 4, t: 10, b: 14 };
    const iW = W - P.l - P.r, iH = H - P.t - P.b;
    const n = rows.length;
    const max = Math.max(...rows.map(r => r.total || 0), 1);
    const bw  = Math.max(3, iW / n * 0.6);
    const full = rows.filter(r => r.complete).map(r => r.total);
    const avg = full.length ? full.reduce((a, b) => a + b, 0) / full.length : null;

    let out = '';
    if (avg != null) {
      const y = P.t + iH - (avg / max) * iH;
      out += `<line x1="${P.l}" y1="${y.toFixed(1)}" x2="${W - P.r}" y2="${y.toFixed(1)}" `
           + `stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 3" opacity="0.55"/>`;
    }
    rows.forEach((r, i) => {
      const h = Math.max(1, (r.total || 0) / max * iH);
      const x = P.l + (i + 0.5) / n * iW - bw / 2;
      const y = P.t + iH - h;
      // Today is still accumulating — drawn dimmer so a short bar doesn't read
      // as a lazy day. Same convention as the steps chart.
      out += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" `
           + `height="${h.toFixed(1)}" rx="2" fill="var(--accent)" `
           + `opacity="${r.complete ? 0.85 : 0.4}"><title>${r.date}: `
           + `${Number(r10(r.total)).toLocaleString('en-US')} kcal`
           + `${r.complete ? '' : ' (so far)'}</title></rect>`;
    });
    svg.innerHTML = out;
    const lab = document.getElementById('cal-chart-lab');
    if (lab) lab.textContent = avg != null
      ? `last ${n} days · avg ${Number(r10(avg)).toLocaleString('en-US')} kcal`
      : `last ${n} days`;
  }

  rerenderVitalDays = () => {
    if (lastIntraday) applyVitalsIntraday(lastIntraday);
    if (lastVitalsHistory) applyVitalsTab(lastVitalsHistory, lastVitalsSnapshot);
  };
  // Energy out moved to the Food tab, so it redraws when *that* tab shows —
  // its SVG measures 0 while hidden and would otherwise render empty.
  rerenderFood = () => {
    if (lastCalToday || lastCalHistory) applyCalories(lastCalToday, lastCalHistory);
    applyFoodDay(lastFoodDay);
    applyWeight(lastWeight);
  };
  rerenderBioSteps = () => {
    if (lastVitalsHistory) renderVitalBars('vt-steps-chart', 'vt-steps-read', lastVitalsHistory.steps, ' steps', 0, 'var(--hue-bio)');
  };

  // ── Aerobic capacity / estimated VO2max card (biocharge tab) ─────────
  let _fitExpanded = false;
  function _fitOrd(n) {
    const v = n % 100;
    if (v >= 11 && v <= 13) return 'th';
    return ({ 1: 'st', 2: 'nd', 3: 'rd' })[n % 10] || 'th';
  }
  function _fitCap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

  function applyFitness(f) {
    const panel = document.getElementById('fitness-panel');
    const card  = document.getElementById('fitness-card');
    if (!panel || !card) return;
    if (!f || !f.available || !f.vo2max || f.vo2max.central == null) {
      panel.hidden = true; return;
    }
    panel.hidden = false;

    const v = f.vo2max, lo = Math.round(v.lo), hi = Math.round(v.hi);
    const AX0 = 38, AX1 = 74;
    const px = x => Math.max(0, Math.min(100, (x - AX0) / (AX1 - AX0) * 100));

    // 14-day rolling mean of the 90-day central series
    const series = (f.trend_90d || []).filter(p => p.central != null);
    const sm = series.map((p, i) => {
      const w = series.slice(Math.max(0, i - 13), i + 1);
      return w.reduce((s, q) => s + q.central, 0) / w.length;
    });
    let spark = '';
    if (sm.length >= 4) {
      const ymin = Math.min(...sm) - 0.4, ymax = Math.max(...sm) + 0.4;
      const X = i => 4 + i / (sm.length - 1) * 312;
      const Y = y => 44 - (y - ymin) / (ymax - ymin || 1) * 38;
      const pts = sm.map((y, i) => `${X(i).toFixed(1)},${Y(y).toFixed(1)}`).join(' ');
      spark = `<svg viewBox="0 0 320 52" preserveAspectRatio="none">
        <line x1="0" y1="48" x2="320" y2="48" stroke="var(--line-2)" stroke-width="1" vector-effect="non-scaling-stroke"/>
        <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2.25"
          stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
        <line x1="${X(sm.length - 1).toFixed(1)}" y1="${Y(sm[sm.length - 1]).toFixed(1)}" x2="${(X(sm.length - 1) + 0.01).toFixed(2)}" y2="${Y(sm[sm.length - 1]).toFixed(1)}" stroke="var(--accent)" stroke-width="7" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      </svg>`;
    }
    const d90 = f.delta_90d;
    const deltaHtml = (d90 == null) ? '' :
      `<span class="delta ${d90 >= 0 ? 'up' : 'down'}">${d90 >= 0 ? '+' : ''}${d90} vs 90d</span>`;
    const pct = f.percentile != null ? Math.round(f.percentile) : null;
    const ageSex = `${f.age_ref || ''}${(f.sex || '').charAt(0).toUpperCase()}`;

    let html = `
      <div class="fit-hero">
        <span class="fit-range">${lo}<span class="dash">–</span>${hi}</span>
        <span class="fit-unit">ml/kg/min · central ${v.central.toFixed(1)}</span>
      </div>
      <div class="fit-band">
        <div class="fit-band-track">
          <div class="fit-band-fill" style="left:${px(v.lo).toFixed(1)}%;width:${(px(v.hi) - px(v.lo)).toFixed(1)}%"></div>
          <div class="fit-band-central" style="left:${px(v.central).toFixed(1)}%"></div>
        </div>
        <div class="fit-band-scale"><span>${AX0}</span><span>50</span><span>60</span><span>${AX1}</span></div>
      </div>`;
    if (pct != null) html += `
      <div class="fit-pct">
        <span>Percentile</span>
        <span class="fit-pct-bar"><i style="width:${pct}%"></i></span>
        <span><b>${pct}${_fitOrd(pct)}</b> · age ${f.age_ref || '—'}, ${String(f.sex || '').charAt(0).toUpperCase() === 'M' ? 'male' : 'female'}</span>
      </div>`;
    if (spark) html += `
      <div class="fit-spark">
        <div class="fit-spark-head"><span>90-day trend</span>${deltaHtml}</div>
        ${spark}
      </div>`;
    if (f.why_spread) html += `
      <div class="fit-why"><b>Wide band.</b> ${_fitCap(f.why_spread)}.</div>`;

    if (_fitExpanded) {
      const e = f.estimators || {}, inp = f.inputs || {};
      html += `
        <div class="fit-sub-h">The two estimates → the range</div>
        <div class="fit-est"><span class="nm">Uth</span>
          <span class="tk"><i style="left:${px(e.uth).toFixed(0)}%"></i></span>
          <span class="vv">${e.uth != null ? Math.round(e.uth) : '—'}&nbsp;·&nbsp;HR ratio</span></div>
        <div class="fit-est"><span class="nm">Nes</span>
          <span class="tk"><i style="left:${px(e.nes).toFixed(0)}%"></i></span>
          <span class="vv">${e.nes != null ? Math.round(e.nes) : '—'}&nbsp;·&nbsp;+ training</span></div>`;
      if (f.hrr && f.hrr.value != null) {
        const hv = f.hrr.at2 != null
          ? `${f.hrr.value}<span class="u"> → </span>${f.hrr.at2}<span class="u"> bpm</span>`
          : `${f.hrr.value}<span class="u"> bpm</span>`;
        const hl = f.hrr.at2 != null
          ? `HR recovery, 1 &amp; 2 min · <b>${f.hrr.rating}</b> · ${f.hrr.n} session${f.hrr.n === 1 ? '' : 's'}`
          : `1-min HR recovery · <b>${f.hrr.rating}</b> · ${f.hrr.n} session${f.hrr.n === 1 ? '' : 's'}`;
        html += `
          <div class="fit-sub-h">recovery <span style="color:var(--ink-3);text-transform:none;letter-spacing:0">· separate from VO₂ max</span></div>
          <div class="fit-hrr">
            <span class="fit-hrr-v">${hv}</span>
            <span class="fit-hrr-lbl">${hl}</span>
          </div>`;
      }
      html += `
        <div class="fit-sub-h">Inputs</div>
        <div class="fit-kv">
          <div class="fit-kv-row"><span class="k">Activity · read as</span>
            <span class="v"><b>${f.pa_read_as || '—'}</b> · from HR zones · <a data-fit="pai">check</a></span></div>
          <div class="fit-kv-row"><span class="k">HR max · measured</span><span class="v"><b>${inp.hrmax ?? '—'}</b> bpm</span></div>
          <div class="fit-kv-row"><span class="k">Resting HR · 28-night</span><span class="v"><b>${inp.rhr_med ?? '—'}</b> bpm</span></div>
          <div class="fit-kv-row"><span class="k">${f.waist_estimated ? 'BMI' : 'waist'}</span>
            <span class="v">${f.waist_estimated
              ? `<b>${inp.bmi ?? '—'}</b> · <a data-fit="waist">add waist for a tighter estimate</a>`
              : `<b>${inp.waist_cm ?? '—'}</b> cm`}</span></div>
        </div>
        <div class="fit-foot">Estimate, ±11% — a quarterly trend indicator, not a lab value.
          Uth &amp; Sørensen 2004 · Nes / HUNT 2011 · FRIEND registry norms.${
          f.confidence === 'stale' ? '<br>⚠ resting-HR data looks stale.' : ''}</div>
        <div class="fit-cue">Tap to close ▴</div>`;
    } else {
      html += `<div class="fit-cue">Tap for detail ▾</div>`;
    }

    card.classList.toggle('expanded', _fitExpanded);
    card.innerHTML = html;
    card.onclick = (ev) => {
      const link = ev.target.closest('a[data-fit]');
      if (link) {
        ev.stopPropagation();
        if (link.dataset.fit === 'waist' && window.openInfoModal) window.openInfoModal('food');
        else if (link.dataset.fit === 'pai') {
          const b = document.getElementById('pai-warn-btn');
          if (b) b.click();
        }
        return;
      }
      _fitExpanded = !_fitExpanded;
      applyFitness(f);
    };
  }

  function applyBedtimeRec(rec) {
    const timeEl  = document.getElementById('bedtime-rec-time');
    const scoreEl = document.getElementById('bedtime-rec-score');
    const metaEl  = document.getElementById('bedtime-model-meta');
    if (!rec || !rec.recommended_time) return;
    if (timeEl)  timeEl.textContent  = rec.recommended_time;
    if (scoreEl) scoreEl.innerHTML = `Predicted score <b>${rec.predicted_score}</b> · ±5 typical`;
    if (metaEl)  metaEl.textContent  = `${rec.model_nights} nights`;
    if (rec.candidates && rec.candidates.length > 1) {
      renderBedtimeCurve(rec.candidates, rec.recommended_time);
      // re-measure & redraw when the tab becomes visible (SVG width is 0 while hidden)
      rerenderBedtimeCurve = () => renderBedtimeCurve(rec.candidates, rec.recommended_time);
    }
  }

  function renderBedtimeCurve(candidates, best) {
    const svg = document.getElementById('bedtime-curve');
    if (!svg) return;

    // Update viewBox to match actual rendered width — preserveAspectRatio="none"
    // fills the space, dynamic viewBox makes 1 unit = 1 pixel so text isn't stretched
    const rectBC = svg.getBoundingClientRect();
    const W = Math.round(rectBC.width) || 600;
    // match viewBox height to real pixel height too, so preserveAspectRatio="none"
    // maps 1 unit = 1px on BOTH axes and text is never stretched
    const H = Math.round(rectBC.height) || 80;
    const padL = 10, padR = 10, padT = 14, padB = 20;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

    const iW = W - padL - padR, iH = H - padT - padB;
    const scores = candidates.map(c => c.predicted_score);
    const minS = Math.min(...scores), maxS = Math.max(...scores);
    const range = maxS - minS || 1;
    const cx = i => padL + (i / (candidates.length - 1)) * iW;
    const cy = s => padT + iH - ((s - minS) / range) * iH;

    const linePts = candidates.map((c, i) => `${cx(i)},${cy(c.predicted_score)}`);
    const pts = linePts.join(' ');
    const baseY = padT + iH;
    const fillPts = `${padL},${baseY} ${pts} ${padL + iW},${baseY}`;
    const bestIdx = candidates.findIndex(c => c.time === best);
    const bestCand = candidates[bestIdx] ?? candidates[0];

    // area wash under the curve, then a solid 2px accent line on top
    if (window.actaRedrawOnResize) window.actaRedrawOnResize(svg, () => renderBedtimeCurve(candidates, best));
    const _T = window.actaTip(svg.parentElement, svg);
    const smoothC = pp => {
      let d = 'M' + pp[0][0].toFixed(1) + ' ' + pp[0][1].toFixed(1);
      for (let i = 0; i < pp.length - 1; i++) {
        const p0 = pp[i - 1] || pp[i], p1 = pp[i], p2 = pp[i + 1], p3 = pp[i + 2] || p2;
        d += `C${(p1[0] + (p2[0] - p0[0]) / 6).toFixed(1)} ${(p1[1] + (p2[1] - p0[1]) / 6).toFixed(1)} ${(p2[0] - (p3[0] - p1[0]) / 6).toFixed(1)} ${(p2[1] - (p3[1] - p1[1]) / 6).toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
      }
      return d;
    };
    const curveD = smoothC(candidates.map((c, i) => [cx(i), cy(c.predicted_score)]));
    let html = `<path d="${curveD}L${padL + iW} ${baseY}L${padL} ${baseY}Z" fill="var(--accent)" fill-opacity="0.13"/>`;
    html += `<path class="ln" pathLength="1" d="${curveD}" fill="none" stroke="var(--accent)" stroke-width="2.25" stroke-linecap="round"/>`;
    html += `<line x1="${cx(bestIdx >= 0 ? bestIdx : 0)}" x2="${cx(bestIdx >= 0 ? bestIdx : 0)}" y1="${cy(bestCand.predicted_score)}" y2="${baseY}" stroke="var(--accent)" stroke-opacity="0.4" stroke-dasharray="3 4"/>`;

    // end-of-range time ticks so the x-axis reads at a glance
    html += `<text x="${padL}" y="${H - 4}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="13" text-anchor="start">${candidates[0].time}</text>`;
    html += `<text x="${padL + iW}" y="${H - 4}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="13" text-anchor="end">${candidates[candidates.length - 1].time}</text>`;

    // Accent dot with surface ring — starts at best position, moves with cursor on hover
    html += `<circle id="bedtime-dot" cx="${cx(bestIdx >= 0 ? bestIdx : 0)}" cy="${cy(bestCand.predicted_score)}" r="5" fill="var(--accent)" stroke="var(--panel)" stroke-width="2"/>`;

    // Transparent overlay for mouse events
    html += `<rect id="bedtime-hit" x="${padL}" y="${padT}" width="${iW}" height="${iH}" fill="transparent" style="cursor:default;"/>`;

    svg.innerHTML = html;

    const timeEl  = document.getElementById('bedtime-rec-time');
    const scoreEl = document.getElementById('bedtime-rec-score');
    const dot     = document.getElementById('bedtime-dot');
    const bestX   = cx(bestIdx >= 0 ? bestIdx : 0);
    const bestY   = cy(bestCand.predicted_score);

    document.getElementById('bedtime-hit').addEventListener('mousemove', e => {
      const rect = svg.getBoundingClientRect();
      const svgX = (e.clientX - rect.left) / rect.width * W;
      let nearIdx = 0, minDist = Infinity;
      candidates.forEach((c, i) => {
        const d = Math.abs(cx(i) - svgX);
        if (d < minDist) { minDist = d; nearIdx = i; }
      });
      const c = candidates[nearIdx];
      dot.setAttribute('cx', cx(nearIdx));
      dot.setAttribute('cy', cy(c.predicted_score));
      if (timeEl)  timeEl.textContent  = c.time;
      if (scoreEl) scoreEl.innerHTML = `Predicted score <b>${c.predicted_score}</b> · ±5 typical`;
      _T.show(`<b>${c.time}</b> · predicted ${c.predicted_score}`, cx(nearIdx), cy(c.predicted_score));
    });

    document.getElementById('bedtime-hit').addEventListener('mouseleave', () => {
      dot.setAttribute('cx', bestX);
      dot.setAttribute('cy', bestY);
      if (timeEl)  timeEl.textContent  = best;
      if (scoreEl) scoreEl.innerHTML = `Predicted score <b>${bestCand.predicted_score}</b> · ±5 typical`;
      _T.hide();
    });
  }

  // Mental flow: cognitive clarity across the day (word scale stored 1–5).
  const FLOW_WORDS = { 1: 'Drained', 2: 'Foggy', 3: 'Normal', 4: 'Sharp', 5: 'Hyper-focused' };
  // One identity hue (amber) carries the real + predicted data; the 30-day
  // baseline is recessive neutral gray so it reads as context, not a rival series.
  const FLOW_COL  = 'oklch(0.80 0.11 80)';      // today — solid amber (real data)
  const FLOW_DIM  = 'oklch(0.56 0.02 80)';      // typical-day baseline (neutral gray)
  const FLOW_PRED = 'oklch(0.80 0.11 80)';      // predicted — amber, dotted + dimmed
  const FLOW_CAFF = 'oklch(0.62 0.10 40)';      // typical on coffee days — warm brown, distinct from amber
  function renderMentalFlow(history, predict) {
    // history: [{ts, value, note, night_of}] over the last N days.
    const _compact = window.innerWidth < 720;
    const W = window.__flowW || (_compact ? 360 : 800), H = _compact ? 260 : 290, P = _compact ? { l: 66, r: 12, t: 16, b: 34 } : { l: 100, r: 20, t: 18, b: 30 };
    const FLOW_SHORT = { 1: 'Drained', 2: 'Foggy', 3: 'Normal', 4: 'Sharp', 5: 'Hyper' };
    const iW = W - P.l - P.r, iH = H - P.t - P.b;
    const HMIN = 6, HMAX = 24;
    const xOf = h => P.l + (Math.min(HMAX, Math.max(HMIN, h)) - HMIN) / (HMAX - HMIN) * iW;
    const yOf = v => P.t + iH - (Math.min(5, Math.max(1, v)) - 1) / 4 * iH;
    const hourOf = ts => { const d = new Date(ts); return d.getHours() + d.getMinutes() / 60; };

    // Y gridlines with word labels
    let grid = '';
    for (let v = 1; v <= 5; v++) {
      const yp = yOf(v);
      grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${yp}" y2="${yp}" stroke="rgba(255,255,255,0.05)"/>`;
      grid += `<text x="${P.l - 10}" y="${yp + 3}" fill="var(--ink-3)" font-family="Geist, Inter, sans-serif" font-weight="500" font-size="12" text-anchor="end">${_compact ? FLOW_SHORT[v] : FLOW_WORDS[v]}</text>`;
    }
    // X labels every 2h
    let xLabels = '';
    for (let h = HMIN; h <= HMAX; h += (_compact ? 6 : 2)) {
      xLabels += `<text x="${xOf(h)}" y="${H - P.b + 16}" fill="var(--ink-3)" font-family="JetBrains Mono" font-size="12" text-anchor="middle">${p2(h % 24)}:00</text>`;
    }

    // Typical-day curves: average per 2h bucket across PAST days only (today excluded,
    // so today's live line stands out against the baseline instead of inflating it).
    //
    // Split into no-coffee and coffee-day curves. Blended into one line, the
    // caffeinated entries (habitually ~11:00) dragged the early afternoon up to
    // "Sharp" on every day, including days with no coffee — the average described
    // a day that never happens. Entries outside the 1–5 scale are legacy 1–10
    // values (2026-05-26 → 05-28) and are dropped; previously yOf() clamped them
    // and they rendered pinned at the top.
    const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
    const dayStartMs = dayStart.getTime();

    const bucketsFor = pred => {
      const out = [];
      for (let lo = HMIN; lo < HMAX; lo += 2) {
        const vals = history.filter(e => {
          const h = hourOf(e.ts);
          return e.ts < dayStartMs && e.value >= 1 && e.value <= 5
                 && h >= lo && h < lo + 2 && pred(e);
        }).map(e => e.value);
        if (vals.length) out.push({ h: lo + 1, n: vals.length, avg: vals.reduce((a, b) => a + b, 0) / vals.length });
      }
      return out;
    };
    const polyOf = pts => pts.map((b, i) => (i === 0 ? 'M' : 'L') + xOf(b.h).toFixed(1) + ',' + yOf(b.avg).toFixed(1)).join(' ');
    const bucketTip = (b, label) =>
      `${p2(b.h - 1)}:00–${p2(b.h + 1)}:00 · ${label} · ${FLOW_WORDS[Math.round(b.avg)] || ''} ${b.avg.toFixed(1)} · n=${b.n}`;

    let typical = '';
    const cleanPts = bucketsFor(e => !e.caffeinated);
    if (cleanPts.length >= 2) {
      typical += `<path d="${polyOf(cleanPts)}" fill="none" stroke="${FLOW_DIM}" stroke-width="1.5" stroke-dasharray="4 4"/>`;
    }
    cleanPts.forEach(b => {
      typical += `<circle cx="${xOf(b.h).toFixed(1)}" cy="${yOf(b.avg).toFixed(1)}" r="6" fill="transparent"><title>${bucketTip(b, 'no coffee')}</title></circle>`;
    });

    // Coffee-day curve. Buckets are thin (n=2–6), so only n>=MIN_SOLID are joined
    // into a line; thinner ones show as a hollow dot so the eye reads them as
    // tentative instead of as established fact.
    const MIN_SOLID = 3;
    const coffeePts = bucketsFor(e => e.caffeinated);
    const solid = coffeePts.filter(b => b.n >= MIN_SOLID);
    if (solid.length >= 2) {
      typical += `<path d="${polyOf(solid)}" fill="none" stroke="${FLOW_CAFF}" stroke-width="1.5" stroke-dasharray="4 4"/>`;
    }
    coffeePts.forEach(b => {
      const thin = b.n < MIN_SOLID;
      typical += `<circle cx="${xOf(b.h).toFixed(1)}" cy="${yOf(b.avg).toFixed(1)}" r="${thin ? 3.5 : 2.5}"`
              +  ` fill="${thin ? 'var(--panel)' : FLOW_CAFF}" stroke="${FLOW_CAFF}" stroke-width="1.5"`
              +  `><title>${bucketTip(b, 'coffee')}</title></circle>`;
    });

    // Today's entries: connected dots, brighter, with hover tooltips
    const today = history.filter(e => e.ts >= dayStartMs).sort((a, b) => a.ts - b.ts);
    let todayLine = '', todayDots = '';
    if (today.length) {
      const pts = today.map(e => ({ x: xOf(hourOf(e.ts)), y: yOf(e.value), e }));
      if (pts.length >= 2) {
        todayLine = `<path d="${pts.map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ')}" fill="none" stroke="${FLOW_COL}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
      }
      pts.forEach(p => {
        const d = new Date(p.e.ts);
        const t = p2(d.getHours()) + ':' + p2(d.getMinutes());
        const word = FLOW_WORDS[Math.round(p.e.value)] || p.e.value;
        todayDots += `<circle cx="${p.x}" cy="${p.y}" r="5" fill="${FLOW_COL}" stroke="var(--panel)" stroke-width="2"><title>${t} · ${word}</title></circle>`;
      });
    }

    // Predicted curve: model baseline (time + sleep) + caffeine bump, swept over today
    let predicted = '';
    if (predict && Array.isArray(predict.curve) && predict.curve.length >= 2) {
      const poly = predict.curve
        .map((p, i) => (i === 0 ? 'M' : 'L') + xOf(p.hour).toFixed(1) + ',' + yOf(p.predicted).toFixed(1))
        .join(' ');
      predicted = `<path d="${poly}" fill="none" stroke="${FLOW_PRED}" stroke-width="2" stroke-dasharray="1 5" opacity="0.55" stroke-linecap="round" stroke-linejoin="round"/>`;
    }

    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block;">
      ${grid}${xLabels}${typical}${predicted}${todayLine}${todayDots}
    </svg>`;
  }

  // Groups logged entries into 3-hour windows and reports the best/worst by
  // mean value. Purely descriptive — it reads back what was logged, it does
  // not fit or predict anything.
  function mentalPattern(history) {
    if (!Array.isArray(history) || history.length < 8) return null;
    const buckets = {};
    history.forEach(e => {
      const h = new Date(e.ts).getHours();
      const b = Math.floor(h / 3) * 3;
      (buckets[b] = buckets[b] || []).push(e.value);
    });
    const rows = Object.entries(buckets)
      .map(([b, vals]) => ({
        b: +b,
        n: vals.length,
        mean: vals.reduce((a, v) => a + v, 0) / vals.length,
      }))
      .filter(r => r.n >= 3);                       // ignore thin buckets
    if (rows.length < 2) return null;
    rows.sort((a, b) => b.mean - a.mean);
    const fmt = r => `${p2(r.b)}\u2013${p2((r.b + 3) % 24)}`;
    const word = m => FLOW_WORDS[Math.max(1, Math.min(5, Math.round(m)))] || m.toFixed(1);
    const best = rows[0], worst = rows[rows.length - 1];
    const all = history.reduce((a, e) => a + e.value, 0) / history.length;
    return {
      n: history.length,
      best:  { label: fmt(best),  avg: `${word(best.mean)} · ${best.mean.toFixed(1)}` },
      worst: { label: fmt(worst), avg: `${word(worst.mean)} · ${worst.mean.toFixed(1)}` },
      overall: `${word(all)} · ${all.toFixed(1)}`,
    };
  }

  // `dateStr` (optional) renders that day's entries instead of today's — the
  // "typical" baseline curves stay computed over the whole 90-day history,
  // since they are context for the day being viewed, not part of it.
  function applyMentalState(history, predict, dateStr) {
    const panel = document.querySelector('[data-hsub-panel="mental"]');
    if (!panel) return;
    const esc = s => (s || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    history = Array.isArray(history) ? history : [];

    const dayStart = dateStr ? new Date(dateStr + 'T00:00:00') : new Date();
    dayStart.setHours(0, 0, 0, 0);
    const dayEnd = new Date(dayStart); dayEnd.setDate(dayEnd.getDate() + 1);
    const today = history
      .filter(e => e.ts >= dayStart.getTime() && e.ts < dayEnd.getTime())
      .sort((a, b) => a.ts - b.ts);
    const loggedToday = today.length;

    // ── Quick-log row: the primary action, not buried in the modal ──
    let html = `<div class="mental-log live-only">
      <div class="mental-log-head">
        <span class="ml-q">How's your head right now?</span>
        <span class="hero-k" id="ml-status">${loggedToday ? loggedToday + ' logged today' : 'Nothing logged today'}</span>
      </div>
      <div class="wordscale" id="ml-scale">
        ${[1,2,3,4,5].map(v => `<button class="wb" type="button" data-v="${v}">${FLOW_WORDS[v]}</button>`).join('')}
      </div>
    </div>`;

    html += '<div class="mental-grid">';

    // ── Flow chart ──
    html += '<div class="panel"><div class="panel-head"><div class="label">Mental flow</div>' +
            '<div class="meta">Today vs typical vs predicted</div></div><div class="panel-body">';
    if (history.length === 0) {
      html += '<div class="mental-empty">No mental state logged yet — use the buttons above to start.</div>';
    } else {
      html += renderMentalFlow(history, predict);
    }
    html += `<div class="mental-legend">
      <span><i style="background:${FLOW_COL}"></i>Today</span>
      <span><i style="background:${FLOW_DIM}"></i>Typical · no coffee</span>
      <span><i style="background:${FLOW_CAFF}"></i>Typical · coffee days</span>
      <span><i style="background:${FLOW_PRED};opacity:.55"></i>Predicted</span>`;
    if (predict && predict.r2 != null) {
      const weak = predict.r2 < 0.15;
      html += `<span class="ml-fit" style="color:${weak ? 'var(--warn)' : 'var(--ink-3)'}">R² ${predict.r2.toFixed(2)}${weak ? ' · weak signal' : ' · ' + predict.model_entries + ' entries'}</span>`;
    } else if (predict && predict.note) {
      html += `<span class="ml-fit">${esc(predict.note)}</span>`;
    }
    html += '</div></div></div>';

    // ── Pattern: derived from the logged history, nothing modelled ──
    html += '<div class="side-stack">';
    const pat = mentalPattern(history);
    html += `<div class="panel"><div class="panel-head"><div class="label">Pattern</div>` +
            `<div class="meta">${history.length} entries</div></div><div class="panel-body">`;
    if (!pat) {
      html += '<div class="mental-empty">Not enough logged yet to find a pattern.</div>';
    } else {
      const row = (k, v, sub) => `<div class="pat-row"><span class="hero-k">${k}</span>` +
        `<span class="pat-v">${v}${sub ? `<span class="pat-sub">${sub}</span>` : ''}</span></div>`;
      html += row('Best window', pat.best.label, pat.best.avg);
      html += '<div class="hs-line"></div>';
      html += row('Worst window', pat.worst.label, pat.worst.avg);
      html += '<div class="hs-line"></div>';
      html += row('Typical', pat.overall, '');
      html += `<p class="pat-note">Averaged by time of day across ${pat.n} entries. Descriptive only — nothing here is fitted or predicted.</p>`;
    }
    html += '</div></div>';

    // ── Today's entries ──
    html += '<div class="panel"><div class="panel-head"><div class="label">Today\'s entries</div>' +
            `<div class="meta">${loggedToday || 'none'}</div></div><div class="panel-body">`;
    if (today.length === 0) {
      html += '<div class="mental-empty">Nothing logged today yet.</div>';
    } else {
      today.forEach(ev => {
        const d = new Date(ev.ts);
        const time = p2(d.getHours()) + ':' + p2(d.getMinutes());
        const word = FLOW_WORDS[Math.round(ev.value)] || ev.value;
        html += `<div class="ml-row">
          <span class="ml-rail" style="background:${FLOW_COL}"></span>
          <span class="ml-time">${time}</span>
          <span class="ml-word">${word}${ev.note ? `<span class="ml-note">${esc(ev.note)}</span>` : ''}</span>
        </div>`;
      });
    }
    html += '</div></div>';
    html += '</div>';  // /.side-stack

    html += '</div>';  // /.mental-grid
    panel.innerHTML = html;
    {
      const fb = panel.querySelector('.mental-grid > .panel .panel-body');
      const redrawFlow = () => {
        const fs = fb && fb.querySelector(':scope > svg'); if (!fs) return;
        const cs = getComputedStyle(fb);
        const w = Math.round(fb.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight));
        if (!(w > 100) || Math.abs(w - (window.__flowW || 0)) < 2) return;
        window.__flowW = w; fs.outerHTML = renderMentalFlow(history, predict);
      };
      redrawFlow();
      if (fb && window.actaRedrawOnResize) window.actaRedrawOnResize(fb, redrawFlow);
    }

    // Wire the quick-log buttons to the endpoint the modal already uses.
    const scale = panel.querySelector('#ml-scale');
    const status = panel.querySelector('#ml-status');
    if (scale) scale.addEventListener('click', async (e) => {
      const btn = e.target.closest('.wb');
      if (!btn || scale.dataset.busy) return;
      scale.dataset.busy = '1';
      scale.querySelectorAll('.wb').forEach(b => b.classList.toggle('on', b === btn));
      const now = new Date();
      try {
        const res = await fetch('/api/log/feeling', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mental_state: Number(btn.dataset.v),
            mental_time: p2(now.getHours()) + ':' + p2(now.getMinutes())
          })
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        if (status) status.textContent = 'Saved ' + p2(now.getHours()) + ':' + p2(now.getMinutes());
        loadLiveData();
      } catch (err) {
        if (status) { status.textContent = "Couldn't save — tap again"; status.style.color = 'var(--danger)'; }
        scale.querySelectorAll('.wb').forEach(b => b.classList.remove('on'));
      } finally {
        delete scale.dataset.busy;
      }
    });
  }

  // ── Cal-date header: today's real date ────────────────────
  function applyLiveDates() {
    const now = new Date();
    const MU = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const el = document.getElementById('cal-date');
    if (el) el.textContent = `${now.getDate()} ${MU[now.getMonth()]} ${now.getFullYear()}`;
  }

  // ── Build daily biocharge history from 168h series ────────
  function buildDailyHistory(series) {
    const byDay = {};
    series.forEach(p => {
      const d = new Date(p.minute_ts);
      const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
      if (!byDay[key]) byDay[key] = { max: p.level, min: p.level, d };
      else {
        if (p.level > byDay[key].max) byDay[key].max = p.level;
        // exclude exertion dips from the daily low — those reflect exercise drain, not resting level
        if (p.why_label !== 'exertion' && p.level < byDay[key].min) byDay[key].min = p.level;
      }
    });
    const MONS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return Object.values(byDay).map(v => ({
      max: Math.round(v.max),
      min: Math.round(v.min),
      day: `${MONS[v.d.getMonth()]} ${v.d.getDate()}`,
    }));
  }

  // ── Main fetch ────────────────────────────────────────────
  async function loadLiveData() {
    try {
      const [summary, series24, series168, sleepLatest, sleepRecent, logToday, vitals, hypnoStages, hrHistory, bedtimeRec, mentalHistory, mentalPredict, modifiers, vitalsHistory, vitalsIntraday, calToday, calHistory, foodDay, weightHist, dayNaps, dayActs, fitnessData] = await Promise.all([
        fetch('/api/health-summary').then(r => r.json()),
        fetch('/api/biocharge?hours=24').then(r => r.json()),
        fetch('/api/biocharge?hours=360').then(r => r.json()),
        fetch('/api/sleep/latest').then(r => r.json()),
        fetch('/api/sleep/recent?n=14').then(r => r.json()),
        fetch('/api/log/today').then(r => r.json()),
        fetch('/api/vitals/snapshot').then(r => r.json()).catch(() => ({})),
        fetch('/api/sleep/stages').then(r => r.json()).catch(() => []),
        fetch('/api/sleep/hr-history?n=7').then(r => r.json()).catch(() => []),
        fetch('/api/recommend/bedtime').then(r => r.json()).catch(() => null),
        fetch('/api/mental/history?days=90').then(r => r.json()).catch(() => []),
        fetch('/api/mental/predict').then(r => r.json()).catch(() => null),
        fetch('/api/modifiers/active').then(r => r.json()).catch(() => []),
        fetch('/api/vitals/history?days=14').then(r => r.json()).catch(() => null),
        fetch('/api/vitals/intraday').then(r => r.json()).catch(() => null),
        fetch('/api/calories/today').then(r => r.json()).catch(() => null),
        fetch('/api/calories/history?days=14').then(r => r.json()).catch(() => null),
        fetch('/api/food/today').then(r => r.json()).catch(() => null),
        fetch('/api/body/weight?days=90').then(r => r.json()).catch(() => null),
        fetch('/api/naps').then(r => r.json()).catch(() => []),
        fetch('/api/activities').then(r => r.ok ? r.json() : []).catch(() => []),
        fetch('/api/fitness').then(r => r.json()).catch(() => null),
      ]);
      liveDayNaps = Array.isArray(dayNaps) ? dayNaps : [];
      liveDayActivities = Array.isArray(dayActs) ? dayActs : [];

      // Filter 24h series to today only. Used to fall back to the whole
      // rolling window when today's slice was empty — but that silently
      // repainted yesterday's tail-end data as if it were today's, which is
      // exactly the "no sync since last night" bug (2026-09-22): the graph
      // kept showing part of the day before, labelled as current. No sync
      // yet today now means no data, shown as no data.
      const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);
      const todaySeries = series24.filter(p => p.minute_ts >= todayStart.getTime());
      liveDaySeries = todaySeries;

      const startLevel = liveDaySeries.length > 0 ? Math.max(...liveDaySeries.map(p => p.level)) : null;

      // Freshness: is there real data covering the gap between the last
      // strap/phone sync and now, or is the latest available row actually
      // from an earlier day being shown as if it were live. See
      // health_summary()/vitals_snapshot() in api.py for the definitions.
      const biochargeFresh = !!(summary && summary.biocharge_fresh);
      const vitalsFresh    = !!(vitals && vitals.fresh);
      const sleepFresh     = !!(summary && summary.sleep_fresh);

      liveHypnoStages = (sleepFresh && hypnoStages && hypnoStages.length) ? hypnoStages : null;
      liveFullSeries  = series168;
      if (summary && summary.data_start) dataStartDate = summary.data_start;
      renderDayNav();   // arrow bounds depend on data_start
      liveSleepLatest = (sleepFresh && sleepLatest && sleepLatest.waketime_ts) ? sleepLatest : null;

      applyMainBio(summary.biocharge_level, startLevel, null, biochargeFresh);
      // Undo the past-day relabel (see loadDayView) — back on today it is "Now" again.
      document.querySelectorAll('.bio-state .row:first-child .k')
        .forEach(el => { el.textContent = 'Now'; });
      applyMainSleep(sleepFresh ? sleepLatest : null);

      // replace BIO_HISTORY in-place so renderBioTrend() picks it up
      const history = buildDailyHistory(series168);
      BIO_HISTORY.length = 0;
      history.forEach(h => BIO_HISTORY.push(h));

      applyHealthSleep(sleepFresh ? sleepLatest : null, sleepRecent, hrHistory);
      applyMentalState(mentalHistory, mentalPredict);
      applyVitals(vitalsFresh ? vitals : null);
      applyModifiers(modifiers);
      applyVitalsTab(vitalsHistory, vitalsFresh ? vitals : null);
      applyVitalsIntraday(vitalsFresh ? vitalsIntraday : null);
      applyCalories(calToday, calHistory);
      applyFoodDay(foodDay);
      applyWeight(weightHist);
      applyBedtimeRec(bedtimeRec);
      applyFitness(fitnessData);
      applyLiveDates();

      // store computed_at so tick() shows ingest time instead of clock
      const lu = document.getElementById('lastupdate');
      if (lu && summary.last_updated) lu.dataset.apiLu = summary.last_updated;

      // re-render all charts unconditionally so refresh button updates everything
      renderBioDay();
      renderDayEnergyBig();
      renderBioTrend();
      renderHypnogram();
      renderSleepTime();
      renderSleepReg();
      renderSleepHr();

    } catch (err) {
      console.error('[Acta] live data fetch failed:', err);
    }
  }

  reloadLive = loadLiveData;

  // ── Past-day view ─────────────────────────────────────────
  // Same render functions as the live path, fed a specific date. Anything
  // that only means something "right now" (snapshot, bedtime prediction,
  // recovery alarm, quick-log) is hidden by past-mode CSS rather than being
  // fetched and shown next to a past day's numbers.
  loadDayView = async function (date) {
    try {
      const j = (p, fb) => fetch(p).then(r => r.ok ? r.json() : fb).catch(() => fb);
      const [daySeries, sleepDay, sleepRecent, logDay, hypnoStages, hrHistory,
             mentalHistory, vitalsHistory, vitalsIntraday, calDay, calHistory,
             foodDay, weightHist, dayNaps, dayActs, fitnessDay] = await Promise.all([
        j(`/api/biocharge?date=${date}`, []),
        j(`/api/sleep/latest?date=${date}`, null),
        j('/api/sleep/recent?n=14', []),
        j(`/api/log/today?date=${date}`, []),
        j(`/api/sleep/stages?date=${date}`, []),
        j('/api/sleep/hr-history?n=7', []),
        j('/api/mental/history?days=90', []),
        j(`/api/vitals/history?days=14&end=${date}`, null),
        j(`/api/vitals/intraday?date=${date}`, null),
        j(`/api/calories/today?date=${date}`, null),
        j('/api/calories/history?days=14', null),
        j(`/api/food/today?date=${date}`, null),
        j('/api/body/weight?days=90', null),
        j(`/api/naps?date=${date}`, []),
        j(`/api/activities?date=${date}`, []),
        j(`/api/fitness?date=${date}`, null),
      ]);

      liveDaySeries   = Array.isArray(daySeries) ? daySeries : [];
      liveDayNaps     = Array.isArray(dayNaps) ? dayNaps : [];
      liveDayActivities = Array.isArray(dayActs) ? dayActs : [];
      liveHypnoStages = (hypnoStages && hypnoStages.length) ? hypnoStages : null;
      liveSleepLatest = (sleepDay && sleepDay.waketime_ts) ? sleepDay : null;
      daySeriesCache.set(date, liveDaySeries);

      // The ring + state rows are driven by /api/health-summary on the live path,
      // which always describes today. A past day has to drive them from its own
      // series or they keep showing today's numbers next to that day's chart.
      // Scoped to #screen-health so the Main screen's ring stays live.
      if (liveDaySeries.length) {
        const lv = liveDaySeries.map(p => p.level);
        const healthScreen = document.getElementById('screen-health');
        applyMainBio(lv[lv.length - 1], Math.max(...lv), healthScreen);
        if (healthScreen) {
          healthScreen.querySelectorAll('.bio-state .row:first-child .k')
            .forEach(el => { el.textContent = 'End of day'; });
        }
      }

      applyHealthSleep(sleepDay, sleepRecent, hrHistory);
      applyMentalState(mentalHistory, null, date);
      applyVitalsTab(vitalsHistory, null);
      applyVitalsIntraday(vitalsIntraday);
      applyCalories(calDay, calHistory);
      applyFoodDay(foodDay);
      applyWeight(weightHist);
      applyFitness(fitnessDay);

      renderBioDay();
      renderHypnogram();
      renderSleepTime();
      renderSleepReg();
      renderSleepHr();
    } catch (err) {
      console.error('[Acta] day view fetch failed:', err);
    }
  };

  const refreshBtn = document.getElementById('refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.classList.add('spinning');
      refreshBtn.disabled = true;
      const t0 = Date.now();
      try {
        await fetch('/api/refresh', { method: 'POST' });
        // Re-render whatever is on screen — refreshing while viewing a past
        // day should update that day, not jump back to today.
        await Promise.all([
          viewDate === null ? loadLiveData() : loadDayView(viewDate),
          loadWeather(),
        ]);
      } catch (err) {
        console.error('[Acta] refresh failed:', err);
      } finally {
        const elapsed = Date.now() - t0;
        if (elapsed < 1500) await new Promise(r => setTimeout(r, 1500 - elapsed));
        refreshBtn.classList.remove('spinning');
        refreshBtn.disabled = false;
      }
    });
  }

  // ── Weather (Open-Meteo, free, no key) ───────────────────
  const WX_LOCS = [
    { id: 0, lat: 38.7223, lon: -9.1393 },
    { id: 1, lat: 41.1579, lon: -8.6291 },
  ];

  function wxInfo(code) {
    if (code === 0)       return { label: 'Clear sky',     icon: 'sun'     };
    if (code === 1)       return { label: 'Mainly clear',  icon: 'sun'     };
    if (code <= 3)        return { label: 'Partly cloudy', icon: 'partial' };
    if (code <= 48)       return { label: 'Foggy',         icon: 'cloud'   };
    if (code <= 67)       return { label: 'Rainy',         icon: 'rain'    };
    if (code <= 77)       return { label: 'Snowy',         icon: 'snow'    };
    if (code <= 82)       return { label: 'Showers',       icon: 'rain'    };
    if (code <= 86)       return { label: 'Snow showers',  icon: 'snow'    };
    if (code <= 99)       return { label: 'Thunderstorm',  icon: 'thunder' };
    return { label: '—', icon: 'cloud' };
  }

  function wxSVG(icon) {
    const amber = 'oklch(0.78 0.12 80)';
    const ink2  = 'var(--ink-2)';
    const blue  = 'oklch(0.62 0.14 240)';
    const st    = `style="margin:0 0 4px auto;display:block;"`;
    if (icon === 'sun') return (
      `<svg width="28" height="28" viewBox="0 0 28 28" ${st}>` +
      `<circle cx="14" cy="14" r="5" fill="none" stroke="${amber}" stroke-width="1.5"/>` +
      `<g stroke="${amber}" stroke-width="1.5" stroke-linecap="round">` +
      `<line x1="14" y1="3" x2="14" y2="6"/><line x1="14" y1="22" x2="14" y2="25"/>` +
      `<line x1="3" y1="14" x2="6" y2="14"/><line x1="22" y1="14" x2="25" y2="14"/>` +
      `<line x1="6" y1="6" x2="8" y2="8"/><line x1="20" y1="20" x2="22" y2="22"/>` +
      `<line x1="6" y1="22" x2="8" y2="20"/><line x1="20" y1="8" x2="22" y2="6"/>` +
      `</g></svg>`
    );
    if (icon === 'partial') return (
      `<svg width="32" height="28" viewBox="0 0 32 28" ${st}>` +
      `<circle cx="9" cy="10" r="4" fill="none" stroke="${amber}" stroke-width="1.4"/>` +
      `<g stroke="${amber}" stroke-width="1.4" stroke-linecap="round">` +
      `<line x1="9" y1="3" x2="9" y2="4.5"/><line x1="3" y1="10" x2="4.5" y2="10"/>` +
      `<line x1="4" y1="5" x2="5.2" y2="6.2"/></g>` +
      `<path d="M12 19 Q12 14 17 14 Q19 11 23 12 Q28 12 28 17 Q30 17 30 20 Q30 23 27 23 L13 23 Q10 23 10 21 Q10 19 12 19 Z"` +
      ` fill="none" stroke="${ink2}" stroke-width="1.4" stroke-linejoin="round"/>` +
      `</svg>`
    );
    const cloudPath = `<path d="M7 17 Q7 12 12 12 Q14 9 19 10 Q24 10 24 15 Q26 15 26 18 Q26 21 23 21 L8 21 Q5 21 5 19 Q5 17 7 17 Z" fill="none" stroke="${ink2}" stroke-width="1.4" stroke-linejoin="round"/>`;
    if (icon === 'cloud') return `<svg width="32" height="28" viewBox="0 0 32 28" ${st}>${cloudPath}</svg>`;
    if (icon === 'rain') return (
      `<svg width="32" height="28" viewBox="0 0 32 28" ${st}>${cloudPath}` +
      `<g stroke="${blue}" stroke-width="1.4" stroke-linecap="round">` +
      `<line x1="10" y1="24" x2="9"  y2="27"/><line x1="16" y1="24" x2="15" y2="27"/>` +
      `<line x1="22" y1="24" x2="21" y2="27"/></g></svg>`
    );
    if (icon === 'snow') return (
      `<svg width="32" height="28" viewBox="0 0 32 28" ${st}>${cloudPath}` +
      `<g fill="var(--ink-3)">` +
      `<circle cx="10" cy="25" r="1.3"/><circle cx="16" cy="25" r="1.3"/>` +
      `<circle cx="22" cy="25" r="1.3"/></g></svg>`
    );
    if (icon === 'thunder') return (
      `<svg width="32" height="28" viewBox="0 0 32 28" ${st}>${cloudPath}` +
      `<polyline points="17,22 14,26 17,26 14,29" fill="none" stroke="${amber}"` +
      ` stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`
    );
    return wxSVG('cloud');
  }

  const wxForecast = [null, null]; // indexed by loc.id; each entry is the full daily object

  function wxSVGSmall(icon) {
    // Same paths as wxSVG but scaled to 20x20 for the forecast list
    const amber = 'oklch(0.78 0.12 80)';
    const ink2  = 'var(--ink-2)';
    const blue  = 'oklch(0.62 0.14 240)';
    const st = 'style="display:block;"';
    if (icon === 'sun') return (
      `<svg width="20" height="20" viewBox="0 0 28 28" ${st}>` +
      `<circle cx="14" cy="14" r="5" fill="none" stroke="${amber}" stroke-width="1.5"/>` +
      `<g stroke="${amber}" stroke-width="1.5" stroke-linecap="round">` +
      `<line x1="14" y1="3" x2="14" y2="6"/><line x1="14" y1="22" x2="14" y2="25"/>` +
      `<line x1="3" y1="14" x2="6" y2="14"/><line x1="22" y1="14" x2="25" y2="14"/>` +
      `<line x1="6" y1="6" x2="8" y2="8"/><line x1="20" y1="20" x2="22" y2="22"/>` +
      `<line x1="6" y1="22" x2="8" y2="20"/><line x1="20" y1="8" x2="22" y2="6"/>` +
      `</g></svg>`
    );
    if (icon === 'partial') return (
      `<svg width="22" height="20" viewBox="0 0 32 28" ${st}>` +
      `<circle cx="9" cy="10" r="4" fill="none" stroke="${amber}" stroke-width="1.4"/>` +
      `<g stroke="${amber}" stroke-width="1.4" stroke-linecap="round">` +
      `<line x1="9" y1="3" x2="9" y2="4.5"/><line x1="3" y1="10" x2="4.5" y2="10"/>` +
      `<line x1="4" y1="5" x2="5.2" y2="6.2"/></g>` +
      `<path d="M12 19 Q12 14 17 14 Q19 11 23 12 Q28 12 28 17 Q30 17 30 20 Q30 23 27 23 L13 23 Q10 23 10 21 Q10 19 12 19 Z"` +
      ` fill="none" stroke="${ink2}" stroke-width="1.4" stroke-linejoin="round"/>` +
      `</svg>`
    );
    const cp = `<path d="M7 17 Q7 12 12 12 Q14 9 19 10 Q24 10 24 15 Q26 15 26 18 Q26 21 23 21 L8 21 Q5 21 5 19 Q5 17 7 17 Z" fill="none" stroke="${ink2}" stroke-width="1.4" stroke-linejoin="round"/>`;
    if (icon === 'cloud')   return `<svg width="22" height="20" viewBox="0 0 32 28" ${st}>${cp}</svg>`;
    if (icon === 'rain')    return `<svg width="22" height="20" viewBox="0 0 32 28" ${st}>${cp}<g stroke="${blue}" stroke-width="1.4" stroke-linecap="round"><line x1="10" y1="24" x2="9" y2="27"/><line x1="16" y1="24" x2="15" y2="27"/><line x1="22" y1="24" x2="21" y2="27"/></g></svg>`;
    if (icon === 'snow')    return `<svg width="22" height="20" viewBox="0 0 32 28" ${st}>${cp}<g fill="var(--ink-3)"><circle cx="10" cy="25" r="1.3"/><circle cx="16" cy="25" r="1.3"/><circle cx="22" cy="25" r="1.3"/></g></svg>`;
    if (icon === 'thunder') return `<svg width="22" height="20" viewBox="0 0 32 28" ${st}>${cp}<polyline points="17,22 14,26 17,26 14,29" fill="none" stroke="${amber}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    return wxSVGSmall('cloud');
  }

  async function loadWeather() {
    for (const loc of WX_LOCS) {
      try {
        const url = `https://api.open-meteo.com/v1/forecast?latitude=${loc.lat}&longitude=${loc.lon}` +
          `&current=temperature_2m,weather_code` +
          `&daily=weather_code,temperature_2m_max,temperature_2m_min` +
          `&timezone=auto&forecast_days=8`;
        const data = await fetch(url).then(r => r.json());
        wxForecast[loc.id] = data.daily; // store for the popup
        const temp = Math.round(data.current.temperature_2m);
        const code = data.current.weather_code;
        const hi   = Math.round(data.daily.temperature_2m_max[0]);
        const lo   = Math.round(data.daily.temperature_2m_min[0]);
        const { label, icon } = wxInfo(code);
        const condEl  = document.getElementById(`wx-cond-${loc.id}`);
        const iconEl  = document.getElementById(`wx-icon-${loc.id}`);
        const degEl   = document.getElementById(`wx-deg-${loc.id}`);
        const rangeEl = document.getElementById(`wx-range-${loc.id}`);
        if (condEl)  condEl.textContent = label;
        if (iconEl)  iconEl.innerHTML   = wxSVG(icon);
        if (degEl)   degEl.innerHTML    = `${temp}<span class="unit">°C</span>`;
        if (rangeEl) rangeEl.textContent = `H ${hi}° · L ${lo}°`;
      } catch (err) {
        console.warn('[Acta] weather fetch failed', loc, err);
      }
    }
  }

  // ── Weather forecast popup ────────────────────────────────
  const wxModal    = document.getElementById('wx-forecast-modal');
  const wxModalCity = document.getElementById('wx-forecast-city');
  const wxModalRows = document.getElementById('wx-forecast-rows');

  function openWxForecast(locId, cityName) {
    const daily = wxForecast[locId];
    if (!daily) return;
    wxModalCity.textContent = cityName;
    const DAY_NAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const todayStr = daily.time[0];
    wxModalRows.innerHTML = daily.time.map((dateStr, i) => {
      const d = new Date(dateStr + 'T12:00:00');
      const isToday = i === 0;
      const dayLabel = isToday ? 'Today' : DAY_NAMES[d.getDay()];
      const { label, icon } = wxInfo(daily.weather_code[i]);
      const hi = Math.round(daily.temperature_2m_max[i]);
      const lo = Math.round(daily.temperature_2m_min[i]);
      return `<div class="wx-forecast-row">` +
        `<div class="wx-forecast-day${isToday ? ' today' : ''}">${dayLabel}</div>` +
        `<div>${wxSVGSmall(icon)}</div>` +
        `<div class="wx-forecast-cond">${label}</div>` +
        `<div class="wx-forecast-temp">${hi}° <span class="lo">/ ${lo}°</span></div>` +
        `</div>`;
    }).join('');
    wxModal.classList.add('show');
  }

  document.getElementById('wx-forecast-close').addEventListener('click', () => wxModal.classList.remove('show'));
  wxModal.addEventListener('click', e => { if (e.target === wxModal) wxModal.classList.remove('show'); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && wxModal.classList.contains('show')) wxModal.classList.remove('show');
  });
  backGestureFor(wxModal, () => wxModal.classList.contains('show'), () => wxModal.classList.remove('show'));
  document.querySelectorAll('.wx-city').forEach(el => {
    el.addEventListener('click', () => openWxForecast(+el.dataset.wxloc, el.textContent));
  });

  // ── PAI detections: unlogged sessions the strap saw ────────
  const paiModal = document.getElementById('pai-modal');
  const paiBtn   = document.getElementById('pai-warn-btn');
  let paiActivities = [];

  function paiCard(d) {
    // Placeholder first and selected: without it an untouched card silently
    // confirms as whatever happens to be first in the list (Football/sport,
    // 45 bpm), which is a guess the user never made.
    const opts = '<option value="" selected disabled>Type…</option>'
      + paiActivities.map(a => `<option value="${a.slug}">${a.label}</option>`).join('');
    const ints = ['leve', 'moderado', 'intenso']
      .map(i => `<option value="${i}"${i === d.intensity ? ' selected' : ''}>${i}</option>`).join('');
    // Zone minutes are the day's totals (PAI only ever reports per-day), while
    // the time window comes from the HR series — labelled so they aren't read
    // as one figure.
    // kcal is the marginal cost -- what the session added over resting for the
    // same minutes, not the gross figure a fitness app would quote.
    const ev = [d.kcal != null
                  ? `<b class="pai-kcal">${d.kcal} kcal</b>`
                    + (d.cooldown_kcal ? `<span style="color:var(--ink-3);"> +${d.cooldown_kcal} cooldown</span>` : '')
                  : null,
                `${d.pai_today} PAI`,
                `${d.min_high}m high · ${d.min_mod}m mod (day)`,
                `peak ${d.peak_hr}bpm`].filter(Boolean).join('  ·  ');
    return `<div class="pai-card" data-id="${d.id}">
      <div class="pai-card-head">
        <span class="pai-card-date">${d.date} · ${d.start_clock}</span>
        <span class="pai-card-dur">${d.duration_min} min</span>
      </div>
      <div class="pai-card-ev">${ev}</div>
      <div class="pai-card-controls">
        <select class="pai-act">${opts}</select>
        <select class="pai-int">${ints}</select>
        <button class="pai-btn ok" data-act="confirm">Add</button>
        <button class="pai-btn no" data-act="dismiss">Not a workout</button>
      </div>
    </div>`;
  }

  // What each detected bout cost, as opposed to the day's total movement.
  // Marginal: resting for the same minutes is already subtracted.
  async function renderDayActivities(date) {
    const box = document.getElementById('cal-acts');
    if (!box || !date) return;
    try {
      const d = await fetch(`/api/energy/day-activities?date=${date}`).then(r => r.json());
      const acts = (d.activities || []).filter(a => a.kcal != null);
      if (!acts.length) { box.innerHTML = ''; return; }
      box.innerHTML = '<div class="hd">Activities · '
        + `${d.total_kcal} kcal</div>`
        + acts.map(a => {
            // Effort and cooldown split per bout, but both sit in the
            // activities band -- the stretching after a run belongs to the
            // run, not to incidental daily movement.
            const dur = (a.work_min != null && a.cooldown_min > 0)
              ? `${a.work_min}min +${a.cooldown_min} cooldown` : `${a.duration_min}min`;
            const kc = a.cooldown_kcal
              ? `${a.kcal}<span style="color:var(--ink-3);">+${a.cooldown_kcal}</span> kcal`
              : `${a.kcal} kcal`;
            return `<div class="cal-act-row">`
              + `<span class="t">${a.start_clock}</span>`
              + `<span>${a.label ? a.label : 'detected'} · ${dur}</span>`
              + `<span class="st">${a.status === 'pending' ? 'unconfirmed' : a.status}</span>`
              + `<span class="kc">${kc}</span></div>`;
          }).join('');
    } catch (err) {
      box.innerHTML = '';
    }
  }

  async function loadPaiLoad() {
    try {
      const d = await fetch('/api/pai/load').then(r => r.json());
      // baseline = the 100 target, so renderVitalTrend's dashed line reads as
      // the goal rather than as a personal median.
      renderVitalTrend('vt-pai-chart', 'vt-pai-read',
        { series: d.series, baseline: d.target, current: d.current }, '', 0);
    } catch (err) {
      console.warn('[Acta] PAI load fetch failed', err);
    }
  }

  let paiTab = 'pending';
  let paiData = { pending: [], confirmed: [], dismissed: [] };

  const BLURB = {
    pending:   'The strap recorded these but nothing was logged. Pick the type to add them.',
    confirmed: 'Sessions you confirmed. These are now in your training load.',
    dismissed: 'Marked "not a workout". Restore one if that was a mistake.',
  };

  function paiResolvedRow(d, dismissed) {
    const meta = [`${d.duration_min}min`,
                  d.kcal != null ? `<b class="pai-kcal">${d.kcal} kcal</b>` : null,
                  d.cooldown_kcal ? `+${d.cooldown_kcal} cooldown` : null,
                  `${d.pai_today} PAI`, `peak ${d.peak_hr}`].filter(Boolean).join(' · ');
    return `<div class="pai-row" data-id="${d.id}">
      <span class="d">${d.date}</span><span class="t">${d.start_clock}</span>
      <span class="meta">${meta}</span>
      ${dismissed
        ? '<button class="pai-undo" data-act="undismiss">Restore</button>'
        : `<span class="lab">${d.label_display} · ${d.intensity || '—'}</span>`}
    </div>`;
  }

  function renderPaiList() {
    const list = document.getElementById('pai-list');
    document.getElementById('pai-blurb').textContent = BLURB[paiTab];
    document.querySelectorAll('.pai-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.paitab === paiTab));
    const rows = paiData[paiTab] || [];
    if (!rows.length) {
      const empty = { pending: 'Nothing pending — all sessions accounted for.',
                      confirmed: 'No sessions added yet.',
                      dismissed: 'Nothing dismissed.' }[paiTab];
      list.innerHTML = `<div class="pai-empty">${empty}</div>`;
      return;
    }
    list.innerHTML = paiTab === 'pending'
      ? rows.map(paiCard).join('')
      : rows.map(r => paiResolvedRow(r, paiTab === 'dismissed')).join('');
  }

  async function loadPaiDetections() {
    try {
      const [d, h] = await Promise.all([
        fetch('/api/pai/detections').then(r => r.json()),
        fetch('/api/pai/history').then(r => r.json()),
      ]);
      paiActivities = d.activities || [];
      paiData = { pending: d.detections || [],
                  confirmed: h.confirmed || [], dismissed: h.dismissed || [] };
      // The badge counts pending only — it is a call to action, not a total.
      paiBtn.classList.toggle('show', d.count > 0);
      document.getElementById('pai-warn-count').textContent = d.count;
      ['pending', 'confirmed', 'dismissed'].forEach(k =>
        document.getElementById('pai-n-' + k).textContent = paiData[k].length);
      renderPaiList();
    } catch (err) {
      console.warn('[Acta] PAI detections fetch failed', err);
    }
  }

  document.querySelectorAll('.pai-tab').forEach(b =>
    b.addEventListener('click', () => { paiTab = b.dataset.paitab; renderPaiList(); }));

  // One in-flight resolve at a time. A per-card .resolving class only blocks
  // a double-tap on the SAME card -- confirming card A then card B a moment
  // later is the natural interaction with a stacked list, and that is exactly
  // what races the events.json append on the server.
  let paiBusy = false;

  document.getElementById('pai-list').addEventListener('click', async (e) => {
    const undo = e.target.closest('.pai-undo');
    if (undo && !paiBusy) {
      paiBusy = true;
      const row = undo.closest('.pai-row');
      try {
        const r = await fetch(`/api/pai/detections/${row.dataset.id}/undismiss`, { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        await loadPaiDetections();
      } catch (err) {
        console.warn('[Acta] PAI undismiss failed', err);
      } finally { paiBusy = false; }
      return;
    }
    const btn = e.target.closest('.pai-btn');
    if (!btn || paiBusy) return;
    const card = btn.closest('.pai-card');
    const id   = card.dataset.id;
    const confirming = btn.dataset.act !== 'dismiss';
    const activity = card.querySelector('.pai-act').value;
    if (confirming && !activity) {
      card.querySelector('.pai-act').focus();   // nothing picked yet
      return;
    }
    paiBusy = true;
    card.classList.add('resolving');
    try {
      const r = confirming
        ? await fetch(`/api/pai/detections/${id}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              activity,
              intensity: card.querySelector('.pai-int').value,
            }),
          })
        : await fetch(`/api/pai/detections/${id}/dismiss`, { method: 'POST' });
      // fetch() does not reject on 4xx/5xx, so without this an error looked
      // exactly like success and the card silently came back on reload.
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await loadPaiDetections();
    } catch (err) {
      card.classList.remove('resolving');
      console.warn('[Acta] PAI resolve failed', err);
    } finally {
      paiBusy = false;
    }
  });

  paiBtn.addEventListener('click', () => { loadPaiDetections(); paiModal.classList.add('show'); });
  document.getElementById('pai-close').addEventListener('click', () => paiModal.classList.remove('show'));
  paiModal.addEventListener('click', e => { if (e.target === paiModal) paiModal.classList.remove('show'); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && paiModal.classList.contains('show')) paiModal.classList.remove('show');
  });
  backGestureFor(paiModal, () => paiModal.classList.contains('show'), () => paiModal.classList.remove('show'));

  // ── About modal ────────────────────────────────
  const aboutModal = document.getElementById('about-modal');

  function fmtAboutTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    const sameDay = d.toDateString() === new Date().toDateString();
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return sameDay ? time : `${d.toLocaleDateString([], { day: '2-digit', month: 'short' })} ${time}`;
  }

  async function openAboutModal() {
    aboutModal.classList.add('show');
    const battEl = document.getElementById('about-battery');
    battEl.textContent = lastSnapshot?.battery != null ? `${Math.round(lastSnapshot.battery)}%` : '—';

    // Version comes from the service worker's own cache tag, fetched fresh
    // rather than duplicated as a second constant that could drift from it.
    fetch('/sw.js', { cache: 'no-store' }).then(r => r.text()).then(txt => {
      const m = txt.match(/CACHE\s*=\s*'([^']+)'/);
      document.getElementById('about-version').textContent = m ? m[1] : 'version unknown';
    }).catch(() => {
      document.getElementById('about-version').textContent = 'version unknown';
    });

    try {
      const info = await fetch('/api/system/info').then(r => r.json());
      const ingestEl = document.getElementById('about-ingest');
      ingestEl.textContent = `${fmtAboutTime(info.last_ingest_at)} · ${info.last_ingest_status || '—'}`;
      ingestEl.classList.toggle('warn', info.last_ingest_status && info.last_ingest_status !== 'ok' && info.last_ingest_status !== 'noop');
      document.getElementById('about-datachange').textContent = fmtAboutTime(info.last_data_change);
      document.getElementById('about-history').textContent =
        info.nights_recorded ? `${info.nights_recorded} nights · since ${info.data_start}` : '—';
      document.getElementById('about-dbsize').textContent =
        info.db_size_mb != null ? `${info.db_size_mb} MB` : '—';
    } catch (err) {
      console.warn('[Acta] system info fetch failed', err);
    }
  }

  document.getElementById('about-btn').addEventListener('click', openAboutModal);
  document.getElementById('about-close').addEventListener('click', () => aboutModal.classList.remove('show'));
  aboutModal.addEventListener('click', e => { if (e.target === aboutModal) aboutModal.classList.remove('show'); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && aboutModal.classList.contains('show')) aboutModal.classList.remove('show');
  });
  backGestureFor(aboutModal, () => aboutModal.classList.contains('show'), () => aboutModal.classList.remove('show'));

  // VO2 max scale — help popup (the ? in the panel head)
  const vo2HelpModal = document.getElementById('vo2-help-modal');
  if (vo2HelpModal) {
    const closeVo2 = () => vo2HelpModal.classList.remove('show');
    document.getElementById('fit-help-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      vo2HelpModal.classList.add('show');
    });
    document.getElementById('vo2-help-close').addEventListener('click', closeVo2);
    vo2HelpModal.addEventListener('click', e => { if (e.target === vo2HelpModal) closeVo2(); });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && vo2HelpModal.classList.contains('show')) closeVo2();
    });
  }

  loadLiveData();
  loadWeather();
  loadPaiDetections();
  loadPaiLoad();
  // The poll must not yank a past day back to today under the user; while a
  // past day is selected the screen is a frozen snapshot by definition.
  setInterval(() => { if (viewDate === null) loadLiveData(); }, 5 * 60 * 1000);
  setInterval(loadWeather,  30 * 60 * 1000); // weather every 30 min
})();

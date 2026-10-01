// Acta — Productivity tab
// Full screen render (sub-tabs, weekly-schedule calendar, Kanban boards, focus
// mode, park-a-thought). Matches the finance/workout/auspex pattern: index.html
// only carries an empty <main id="screen-productivity"> shell + the shared
// modals; everything here.
//
// Classic script — shares global scope with the main inline <script> and relies
// on its globals: pad(), t(), LANG, applyLang().
//
// Data:
//   /api/schedule/*        weekly-schedule template (recurring, in acta.db)
//   /api/kanban/*          Kanban boards in acta.db
//   localStorage           park-a-thought + focus-timer-today only (per-device)
'use strict';

(function () {
  const SCREEN = document.getElementById('screen-productivity');
  if (!SCREEN) return;

  // ---------- tiny fetch helpers ----------
  const jget = (u) => fetch(u).then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)));
  const jsend = (u, method, body) => fetch(u, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(async r => {
    if (r.ok) return r.status === 204 ? {} : r.json();
    let msg = r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  });
  const esc = (s) => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const escText = (s) => (s || '').replace(/</g, '&lt;');
  // light inline markdown for card text: **bold**, `code`, [[a|b]] / [[a]] -> plain
  const mdInline = (s) => escText(s || '')
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '$2')
    .replace(/\[\[([^\]]+)\]\]/g, '$1')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  const DAYS_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const DAYS_PT = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'];
  const DAYFULL_EN = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const DAYFULL_PT = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo'];
  const MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const MONTHS_PT = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez'];
  const todayDow = () => (new Date().getDay() + 6) % 7;        // Mon=0 … Sun=6

  function weekDates() {
    const mon = new Date(); mon.setHours(0, 0, 0, 0);
    mon.setDate(mon.getDate() - todayDow());
    return Array.from({ length: 7 }, (_, i) => {
      const d = new Date(mon); d.setDate(mon.getDate() + i); return d;
    });
  }

  function fmtMin(min) {
    return pad(Math.floor(min / 60)) + ':' + pad(min % 60);
  }
  function timeToMin(s) {
    const [h, m] = (s || '0:0').split(':').map(Number);
    return (h || 0) * 60 + (m || 0);
  }

  // ========================================================================
  //  SHELL
  // ========================================================================
  let shellBuilt = false;
  let prodSubtab = localStorage.getItem('acta_prod_subtab') || 'calendar';

  function buildShell() {
    if (shellBuilt) return;
    SCREEN.innerHTML = `
      <div class="prod-grid">
        <div class="prod-head">
          <div class="subtabs" id="prod-subtabs">
            <button class="subtab" data-subtab="calendar">
              <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="3" x2="8" y2="7"/><line x1="16" y1="3" x2="16" y2="7"/></svg>
              <span>${t('sub_calendar')}</span>
            </button>
            <button class="subtab" data-subtab="boards">
              <svg viewBox="0 0 24 24"><rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="11" rx="1"/><rect x="17" y="4" width="4" height="14" rx="1"/></svg>
              <span>${t('sub_boards')}</span>
            </button>
          </div>
          <button class="focus-btn" id="focus-btn" type="button">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>
            <span>${t('focus_time')}</span>
          </button>
        </div>

        <div class="subtab-panel" data-subtab-panel="calendar" style="display:contents;">
          <div class="panel">
            <div class="panel-head">
              <div class="label"><span>${t('calendar')}</span> <span style="color:var(--ink-3);margin:0 6px;">·</span> <span>${t('week')}</span></div>
              <div class="meta" id="week-range">—</div>
            </div>
            <div class="panel-body" style="padding: 0 0 4px;">
              <div class="week-cal">
                <div class="week-head" id="week-head"></div>
                <div class="week-body" id="week-body"></div>
              </div>
              <div class="week-energy">
                <div class="we-head">
                  <span class="hero-k">${LANG === 'pt' ? 'Energia típica · alinhada à grelha' : 'Typical energy · aligned to the grid'}</span>
                  <span class="hero-k" id="we-note">—</span>
                </div>
                <svg id="week-energy-svg" preserveAspectRatio="none"></svg>
              </div>
            </div>
          </div>

          <div class="col" style="gap: 12px;">
            <div class="panel">
              <div class="panel-head">
                <div class="label">${t('focus_today')}</div>
                <div class="meta" id="focus-total">0m</div>
              </div>
              <div class="panel-body">
                <div style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);margin-bottom:6px;">${t('deep_work_total')}</div>
                <div id="focus-total-big" style="font-family:var(--num);font-variant-numeric:tabular-nums;font-size:28px;color:var(--ink);font-weight:400;letter-spacing:-0.01em;">0h 00m</div>
                <div style="font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:4px;">${t('goal_4h')}</div>
                <div style="height:6px;background:var(--bg-2);border-radius:999px;overflow:hidden;margin-top:12px;">
                  <div id="focus-bar" style="height:100%;width:0%;background:var(--accent);transition:width 300ms;"></div>
                </div>
              </div>
            </div>

            <div class="panel">
              <div class="panel-head">
                <div class="label">${t('intrusive_thoughts')}</div>
                <div class="meta" style="display:flex;align-items:center;">
                  <span id="thoughts-count">0</span>
                  <button class="thoughts-toggle" id="thoughts-toggle" type="button" aria-label="show thoughts">›</button>
                </div>
              </div>
              <div class="panel-body">
                <div class="thoughts-input">
                  <textarea id="thought-input" placeholder="${esc(t('thought_placeholder'))}"></textarea>
                  <div class="actions">
                    <span class="hint">${t('hint_enter')}</span>
                    <button id="thought-save" type="button">${t('park_it')}</button>
                  </div>
                </div>
                <div class="thoughts-collapse" id="thoughts-collapse"><div id="thoughts-list"></div></div>
              </div>
            </div>
          </div>
        </div>

        <div class="subtab-panel" data-subtab-panel="boards" style="display:none; grid-column: 1 / -1;">
          <div class="kanban-wrap">
            <div class="kanban-bar">
              <div class="kb-tabs" id="kb-tabs"></div>
              <div class="kb-bar-actions">
                <button class="mini-btn" id="kb-rename-board" type="button" hidden>${t('kb_rename')}</button>
                <button class="mini-btn" id="kb-delete-board" type="button" hidden>${t('kb_delete')}</button>
                <button class="mini-btn solid" id="kb-new-board" type="button">${t('kb_new_board')}</button>
              </div>
            </div>
            <div class="kanban-board" id="kanban-board"></div>
          </div>
        </div>
      </div>`;
    shellBuilt = true;
    wireShell();
    applyLang();
  }

  function wireShell() {
    SCREEN.querySelectorAll('#prod-subtabs .subtab').forEach(btn => {
      btn.addEventListener('click', () => setSubtab(btn.dataset.subtab));
    });
    document.getElementById('focus-btn').addEventListener('click', openFocus);

    // park a thought
    const ti = document.getElementById('thought-input');
    document.getElementById('thought-save').addEventListener('click', addThought);
    ti.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); addThought(); }
    });
    const tt = document.getElementById('thoughts-toggle');
    tt.addEventListener('click', () => {
      const open = document.getElementById('thoughts-collapse').classList.toggle('show');
      tt.classList.toggle('open', open);
    });

    document.getElementById('kb-new-board').addEventListener('click', kbNewBoard);
    document.getElementById('kb-rename-board').addEventListener('click', kbRenameBoard);
    document.getElementById('kb-delete-board').addEventListener('click', kbDeleteBoard);

    window.addEventListener('resize', () => {
      clearTimeout(window.__prodRz);
      window.__prodRz = setTimeout(renderWeekEnergy, 150);
    });
  }

  function setSubtab(which) {
    prodSubtab = which;
    localStorage.setItem('acta_prod_subtab', which);
    SCREEN.querySelectorAll('#prod-subtabs .subtab').forEach(b =>
      b.classList.toggle('active', b.dataset.subtab === which));
    SCREEN.querySelectorAll('[data-subtab-panel]').forEach(p => {
      const on = p.dataset.subtabPanel === which;
      p.style.display = on ? (which === 'calendar' ? 'contents' : 'block') : 'none';
    });
    if (which === 'calendar') {
      renderWeek(); renderWeekEnergy();
    } else if (which === 'boards') {
      loadKbBoards();
    }
  }

  // ========================================================================
  //  WEEKLY SCHEDULE (recurring template)
  // ========================================================================
  // 7am–22:00, sized so the whole grid + energy ribbon fit without scrolling.
  const HOUR_PX = 28;
  const START_HOUR = 7;
  const END_HOUR = 22;

  let scheduleBlocks = [];
  let scheduleCats = [];
  const catColor = (key) => (scheduleCats.find(c => c.key === key) || {}).color || 'var(--ink-3)';
  const catLabel = (key) => {
    const c = scheduleCats.find(x => x.key === key);
    return c ? c.label : key;
  };

  function loadSchedule() {
    return Promise.all([
      jget('/api/schedule/blocks').then(d => { scheduleBlocks = d.blocks || []; }),
      scheduleCats.length ? Promise.resolve()
        : jget('/api/schedule/categories').then(d => { scheduleCats = d.categories || []; }),
    ]).then(() => {
      fillCatSelect();
      renderWeek();
      renderTodayEvents();
    }).catch(() => {});
  }

  function fillCatSelect() {
    const sel = document.getElementById('event-tag');
    if (!sel || !scheduleCats.length) return;
    sel.innerHTML = scheduleCats.map(c => `<option value="${c.key}">${esc(c.label)}</option>`).join('');
  }

  function renderWeek() {
    const head = document.getElementById('week-head');
    const body = document.getElementById('week-body');
    if (!head || !body) return;

    const wd = weekDates();
    const M = LANG === 'pt' ? MONTHS_PT : MONTHS_EN;
    const days = LANG === 'pt' ? DAYS_PT : DAYS_EN;
    const range = document.getElementById('week-range');
    if (range) {
      const a = wd[0], b = wd[6];
      range.textContent = (a.getMonth() === b.getMonth()
        ? `${a.getDate()} — ${b.getDate()} ${M[b.getMonth()]} ${b.getFullYear()}`
        : `${a.getDate()} ${M[a.getMonth()]} — ${b.getDate()} ${M[b.getMonth()]} ${b.getFullYear()}`);
    }

    const td = todayDow();
    head.innerHTML = '<div class="corner"></div>' + days.map((d, i) =>
      `<div class="day${i === td ? ' today' : ''}">${d}<br/><span class="dnum">${wd[i].getDate()}</span></div>`
    ).join('');

    let html = '<div class="hour-col">';
    for (let h = START_HOUR; h <= END_HOUR; h++)
      html += `<div class="hour-row"><span class="hour-label">${pad(h)}:00</span></div>`;
    html += '</div>';

    for (let d = 0; d < 7; d++) {
      html += `<div class="day-col${d === td ? ' today' : ''}" data-dow="${d}">`;
      for (let h = START_HOUR; h <= END_HOUR; h++)
        html += `<div class="hour-row" data-hour="${h}"></div>`;

      scheduleBlocks.filter(e => e.dow === d).forEach(ev => {
        if (ev.end_min <= START_HOUR * 60 || ev.start_min >= END_HOUR * 60 + 60) return;
        const vS = Math.max(ev.start_min, START_HOUR * 60);
        const vE = Math.min(ev.end_min, END_HOUR * 60 + 60);
        const top = (vS - START_HOUR * 60) * (HOUR_PX / 60);
        const hgt = Math.max((vE - vS) * (HOUR_PX / 60), 20);
        html += `<div class="event" data-id="${ev.id}" data-cat="${esc(ev.category)}"
                      style="top:${top}px;height:${hgt}px;--cat:${catColor(ev.category)};">
                   <div class="e-time">${fmtMin(ev.start_min)} — ${fmtMin(ev.end_min)}</div>
                   <div class="e-title">${escText(ev.title)}</div>
                 </div>`;
      });

      if (d === td) {
        const now = new Date();
        const nm = now.getHours() * 60 + now.getMinutes();
        if (nm >= START_HOUR * 60 && nm < (END_HOUR + 1) * 60)
          html += `<div class="now-line" style="top:${(nm - START_HOUR * 60) * (HOUR_PX / 60)}px;"></div>`;
      }
      html += '</div>';
    }
    body.innerHTML = html;

    body.querySelectorAll('.day-col').forEach(col => {
      col.addEventListener('mousedown', e => onGridMouseDown(e, col));
    });
    body.querySelectorAll('.event').forEach(el => el.addEventListener('mousedown', startEventDrag));
  }

  function onGridMouseDown(e, col) {
    if (e.target.closest('.event') || e.button !== 0) return;
    const rect = col.getBoundingClientRect();
    const startY = e.clientY - rect.top;
    const maxMin = (END_HOUR + 1 - START_HOUR) * 60;
    const sOff = Math.min(maxMin - 15, Math.max(0, Math.round(startY / HOUR_PX * 60 / 15) * 15));
    const startAbs = START_HOUR * 60 + sOff;
    let endOff = sOff + 60;

    const ghost = document.createElement('div');
    ghost.className = 'event event-ghost';
    ghost.style.cssText = `top:${sOff * (HOUR_PX / 60)}px;height:${60 * (HOUR_PX / 60)}px;--cat:${catColor('deep')};`;
    ghost.innerHTML = `<div class="e-time">${fmtMin(startAbs)} — ${fmtMin(START_HOUR * 60 + endOff)}</div><div class="e-title">${LANG === 'pt' ? 'Novo bloco' : 'New block'}</div>`;
    col.appendChild(ghost);

    function move(e2) {
      const y = e2.clientY - rect.top;
      const mt = Math.max(0, Math.round(y / HOUR_PX * 60 / 15) * 15);
      endOff = Math.min(maxMin, Math.max(sOff + 15, mt));
      ghost.style.height = ((endOff - sOff) * (HOUR_PX / 60)) + 'px';
      ghost.querySelector('.e-time').textContent = `${fmtMin(startAbs)} — ${fmtMin(START_HOUR * 60 + endOff)}`;
    }
    function up() {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      ghost.remove();
      openEventModal({ dow: +col.dataset.dow, start_min: startAbs, end_min: START_HOUR * 60 + endOff });
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  function startEventDrag(e) {
    if (e.button !== 0) return;
    const el = e.currentTarget;
    const id = +el.dataset.id;
    const ev = scheduleBlocks.find(x => x.id === id);
    if (!ev) return;
    e.preventDefault();
    const body = document.getElementById('week-body');
    const startY = e.clientY, startX = e.clientX;
    const origStart = ev.start_min, origDow = ev.dow, dur = ev.end_min - ev.start_min;
    let moved = false, newStart = origStart, newDow = origDow;
    el.classList.add('dragging');

    function move(e2) {
      const dx = e2.clientX - startX, dy = e2.clientY - startY;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) moved = true;
      body.querySelectorAll('.day-col').forEach(c => {
        const r = c.getBoundingClientRect();
        if (e2.clientX >= r.left && e2.clientX < r.right) newDow = +c.dataset.dow;
      });
      const delta = Math.round((dy / HOUR_PX) * 60 / 15) * 15;
      newStart = Math.max(START_HOUR * 60, Math.min((END_HOUR + 1) * 60 - dur, origStart + delta));
      el.style.top = (newStart - START_HOUR * 60) * (HOUR_PX / 60) + 'px';
      if (newDow !== origDow) {
        const tc = body.querySelector(`.day-col[data-dow="${newDow}"]`);
        if (tc && el.parentElement !== tc) tc.appendChild(el);
      }
      el.querySelector('.e-time').textContent = fmtMin(newStart) + ' — ' + fmtMin(newStart + dur);
    }
    function up() {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      el.classList.remove('dragging');
      if (moved) {
        jsend(`/api/schedule/blocks/${id}`, 'PATCH',
          { dow: newDow, start_min: newStart, end_min: newStart + dur })
          .then(loadSchedule).catch(() => loadSchedule());
      } else {
        openEventModal(ev, true);
      }
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  // ---- event modal ----
  let editingBlockId = null;
  function openEventModal(ev, isEdit = false) {
    const modal = document.getElementById('event-modal');
    editingBlockId = isEdit ? ev.id : null;
    document.getElementById('event-modal-head').textContent = isEdit ? t('edit_event') : t('new_event');
    const dfull = LANG === 'pt' ? DAYFULL_PT : DAYFULL_EN;
    const M = LANG === 'pt' ? MONTHS_PT : MONTHS_EN;
    document.getElementById('event-modal-day').textContent =
      `${dfull[ev.dow]} · ${LANG === 'pt' ? 'todas as semanas' : 'every week'}`;
    document.getElementById('event-title').value = ev.title || '';
    document.getElementById('event-start').value = fmtMin(ev.start_min);
    document.getElementById('event-end').value = fmtMin(ev.end_min);
    fillCatSelect();
    document.getElementById('event-tag').value = ev.category || 'deep';
    document.getElementById('event-note').value = ev.note || '';
    document.getElementById('event-delete').style.display = isEdit ? '' : 'none';
    modal.dataset.dow = ev.dow;
    modal.classList.add('show');
    setTimeout(() => document.getElementById('event-title').focus(), 40);
  }
  function closeEventModal() {
    document.getElementById('event-modal').classList.remove('show');
    editingBlockId = null;
  }
  function wireEventModal() {
    const modal = document.getElementById('event-modal');
    modal.addEventListener('click', e => { if (e.target === modal) closeEventModal(); });
    document.getElementById('event-close').addEventListener('click', closeEventModal);
    document.getElementById('event-cancel').addEventListener('click', closeEventModal);
    document.getElementById('event-save').addEventListener('click', () => {
      const title = document.getElementById('event-title').value.trim();
      if (!title) { document.getElementById('event-title').focus(); return; }
      const s = timeToMin(document.getElementById('event-start').value);
      const en = timeToMin(document.getElementById('event-end').value);
      if (en <= s) { document.getElementById('event-end').focus(); return; }
      const payload = {
        dow: +modal.dataset.dow, start_min: s, end_min: en, title,
        category: document.getElementById('event-tag').value,
        note: document.getElementById('event-note').value.trim(),
      };
      const p = editingBlockId != null
        ? jsend(`/api/schedule/blocks/${editingBlockId}`, 'PATCH', payload)
        : jsend('/api/schedule/blocks', 'POST', payload);
      p.then(() => { closeEventModal(); loadSchedule(); }).catch(err => alert('Save failed: ' + err.message));
    });
    document.getElementById('event-delete').addEventListener('click', () => {
      if (editingBlockId == null) return;
      jsend(`/api/schedule/blocks/${editingBlockId}`, 'DELETE')
        .then(() => { closeEventModal(); loadSchedule(); }).catch(() => {});
    });
  }

  // ---- "typical energy" ribbon (real biocharge — unchanged behaviour) ----
  let weEnergyCache = null;
  function renderWeekEnergy() {
    const svg = document.getElementById('week-energy-svg');
    if (!svg || !weEnergyCache) return;
    const { typical, today } = weEnergyCache;
    const W = Math.round(svg.getBoundingClientRect().width) || 900, H = 44;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('height', H);
    const GUT = 52, colW = (W - GUT) / 7;
    const yOf = v => H - 4 - (Math.max(0, Math.min(100, v)) / 100) * (H - 10);
    const ti = todayDow();
    let out = '';
    for (let d = 0; d < 7; d++) {
      const isT = d === ti;
      const src = isT && Object.keys(today).length ? today : typical;
      const pts = [];
      for (let h = START_HOUR; h <= END_HOUR; h++) {
        if (src[h] == null) continue;
        pts.push([GUT + d * colW + ((h - START_HOUR) / (END_HOUR - START_HOUR)) * colW, yOf(src[h])]);
      }
      if (pts.length < 2) continue;
      const dPath = pts.map((q, i) => (i ? 'L' : 'M') + q[0].toFixed(1) + ',' + q[1].toFixed(1)).join(' ');
      out += `<path d="${dPath} L${pts[pts.length - 1][0].toFixed(1)},${H} L${pts[0][0].toFixed(1)},${H} Z" fill="var(--accent)" fill-opacity="${isT ? 0.16 : 0.07}"/>`;
      out += `<path d="${dPath}" fill="none" stroke="var(--accent)" stroke-width="1.5" opacity="${isT ? 1 : 0.34}" vector-effect="non-scaling-stroke"/>`;
      if (d < 6) out += `<line x1="${(GUT + (d + 1) * colW).toFixed(1)}" y1="0" x2="${(GUT + (d + 1) * colW).toFixed(1)}" y2="${H}" stroke="var(--line)"/>`;
    }
    svg.innerHTML = out;
  }
  function loadWeekEnergy() {
    fetch('/api/biocharge?hours=360').then(r => r.ok ? r.json() : null).then(d => {
      const series = (d && (d.series || d)) || [];
      if (!Array.isArray(series) || !series.length) return;
      const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
      const sums = {}, counts = {}, today = {};
      series.forEach(pt => {
        const ts = pt.minute_ts || pt.ts, lv = pt.level;
        if (ts == null || lv == null) return;
        const h = new Date(ts).getHours();
        if (h < START_HOUR || h > END_HOUR) return;
        sums[h] = (sums[h] || 0) + lv; counts[h] = (counts[h] || 0) + 1;
        if (ts >= dayStart.getTime()) today[h] = lv;
      });
      const typical = {};
      Object.keys(sums).forEach(h => typical[h] = sums[h] / counts[h]);
      weEnergyCache = { typical, today };
      const note = document.getElementById('we-note');
      if (note) {
        const hrs = Object.keys(typical).map(Number).sort((a, b) => a - b);
        if (hrs.length) {
          const peak = hrs.reduce((a, h) => typical[h] > typical[a] ? h : a, hrs[0]);
          const tr = hrs.reduce((a, h) => typical[h] < typical[a] ? h : a, hrs[0]);
          note.innerHTML = `peak <strong>${pad(peak)}:00</strong> · trough <strong>${pad(tr)}:00</strong>`;
        }
      }
      renderWeekEnergy();
    }).catch(() => {});
  }

  // ========================================================================
  //  KANBAN BOARDS
  // ========================================================================
  let kbBoards = [];
  let kbActiveSlug = localStorage.getItem('acta_kb_board') || null;
  let kbBoard = null;

  function loadKbBoards() {
    return jget('/api/kanban/boards').then(d => {
      kbBoards = d.boards || [];
      if (!kbBoards.some(b => b.slug === kbActiveSlug)) {
        kbActiveSlug = (kbBoards[0] || {}).slug || null;
      }
      renderKbTabs();
      if (kbActiveSlug) openKbBoard(kbActiveSlug);
      else document.getElementById('kanban-board').innerHTML = '';
    }).catch(() => {
      document.getElementById('kanban-board').innerHTML =
        `<div class="kb-error-note">Couldn't load boards.</div>`;
    });
  }

  function renderKbTabs() {
    const wrap = document.getElementById('kb-tabs');
    if (!wrap) return;
    wrap.innerHTML = kbBoards.map(b => `
      <button class="kb-tab${b.slug === kbActiveSlug ? ' active' : ''}" data-slug="${esc(b.slug)}">
        <span>${escText(b.name)}</span>
        <span class="kb-count">${b.open_count}${b.card_count !== b.open_count ? '/' + b.card_count : ''}</span>
      </button>`).join('');
    wrap.querySelectorAll('.kb-tab').forEach(el =>
      el.addEventListener('click', () => { kbActiveSlug = el.dataset.slug; localStorage.setItem('acta_kb_board', kbActiveSlug); renderKbTabs(); openKbBoard(kbActiveSlug); }));

    const rn = document.getElementById('kb-rename-board');
    const dl = document.getElementById('kb-delete-board');
    if (rn) rn.hidden = !kbActiveSlug;
    if (dl) dl.hidden = !kbActiveSlug;
  }

  function openKbBoard(slug) {
    return jget('/api/kanban/boards/' + encodeURIComponent(slug)).then(b => {
      kbBoard = b;
      renderKbBoard();
    }).catch(() => {
      document.getElementById('kanban-board').innerHTML =
        `<div class="kb-error-note">Couldn't open this board.</div>`;
    });
  }

  function checklistStats(body) {
    const lines = (body || '').split('\n').filter(l => /^[-*]\s*\[[ xX]\]/.test(l.trim()));
    const done = lines.filter(l => /\[[xX]\]/.test(l)).length;
    return lines.length ? { done, total: lines.length } : null;
  }

  function renderKbBoard() {
    const wrap = document.getElementById('kanban-board');
    if (!wrap || !kbBoard) return;
    let html = '';

    (kbBoard.lanes || []).forEach(lane => {
      html += `<div class="kb-lane${lane.is_done_lane ? ' done-lane' : ''}" data-lane="${lane.id}">
        <div class="kb-lane-head">
          <span class="kb-lane-name" data-lane="${lane.id}">${escText(lane.name)}</span>
          <span class="kb-lane-meta">
            <span class="kb-lane-count">${lane.cards.length}</span>
            <button class="kb-lane-del" data-lane="${lane.id}" title="delete lane">✕</button>
          </span>
        </div>
        <div class="kb-cards" data-lane="${lane.id}">`;
      if (!lane.cards.length) html += `<div class="kb-empty">${t('kb_lane_empty')}</div>`;
      lane.cards.forEach(c => {
        const cl = checklistStats(c.body);
        const noteMark = (c.body || '').replace(/^[-*]\s*\[[ xX]\].*$/gm, '').trim();
        html += `<div class="kb-card${c.checked ? ' checked' : ''}" data-card="${c.id}" draggable="true">
          <span class="kb-card-box" data-card="${c.id}"></span>
          <div class="kb-card-main" data-card="${c.id}">
            <div class="kb-card-text">${mdInline(c.text)}</div>
            ${(c.tags && c.tags.length) || cl || noteMark ? `<div class="kb-card-sub">
              ${(c.tags || []).map(tg => `<span class="kb-card-tag">${escText(tg)}</span>`).join('')}
              ${cl ? `<span class="kb-card-prog">${cl.done}/${cl.total}</span>` : ''}
              ${noteMark ? `<span class="kb-card-note-mark">≡</span>` : ''}
            </div>` : ''}
          </div>
        </div>`;
      });
      html += `</div>`;
      html += `<div class="kb-card-add">
        <input type="text" data-lane="${lane.id}" placeholder="${esc(t('kb_add_card_ph'))}" />
        <button type="button" data-lane="${lane.id}">+</button>
      </div>`;
      html += `</div>`;
    });
    html += `<button class="kb-add-lane" type="button">${t('kb_add_lane')}</button>`;
    wrap.innerHTML = html;

    wireKbBoard(wrap);
    renderKbTabs();
  }

  function wireKbBoard(wrap) {
    const slug = kbBoard.slug;
    // add card
    wrap.querySelectorAll('.kb-card-add button').forEach(btn => {
      const laneId = +btn.dataset.lane;
      const inp = wrap.querySelector(`.kb-card-add input[data-lane="${laneId}"]`);
      const add = () => {
        const v = inp.value.trim();
        if (!v) return;
        jsend(`/api/kanban/boards/${slug}/cards`, 'POST', { lane_id: laneId, text: v })
          .then(refreshKbBoard).catch(err => alert(err.message));
      };
      btn.addEventListener('click', add);
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
    });
    // card box (toggle done)
    wrap.querySelectorAll('.kb-card-box').forEach(box => {
      box.addEventListener('click', e => {
        e.stopPropagation();
        const c = findCard(+box.dataset.card);
        if (!c) return;
        jsend(`/api/kanban/cards/${c.id}`, 'PATCH', { checked: !c.checked })
          .then(refreshKbBoard).catch(() => {});
      });
    });
    // card body (open modal)
    wrap.querySelectorAll('.kb-card-main').forEach(m => {
      m.addEventListener('click', () => {
        const c = findCard(+m.dataset.card);
        if (c) openCardModal(c);
      });
    });
    // lane rename
    wrap.querySelectorAll('.kb-lane-name').forEach(el => {
      el.addEventListener('dblclick', () => startLaneRename(el));
    });
    // lane delete
    wrap.querySelectorAll('.kb-lane-del').forEach(b => {
      b.addEventListener('click', () => {
        if (!confirm(t('kb_delete_lane_confirm'))) return;
        jsend(`/api/kanban/lanes/${b.dataset.lane}?force=1`, 'DELETE')
          .then(refreshKbBoard).catch(err => alert(err.message));
      });
    });
    // add lane
    const al = wrap.querySelector('.kb-add-lane');
    if (al) al.addEventListener('click', () => {
      const name = prompt(LANG === 'pt' ? 'Nome da coluna:' : 'Lane name:');
      if (!name || !name.trim()) return;
      jsend(`/api/kanban/boards/${slug}/lanes`, 'POST', { name: name.trim() })
        .then(refreshKbBoard).catch(err => alert(err.message));
    });
    // drag + drop
    wrap.querySelectorAll('.kb-card[draggable="true"]').forEach(el => {
      el.addEventListener('dragstart', e => {
        el.classList.add('dragging');
        e.dataTransfer.setData('text/plain', el.dataset.card);
        e.dataTransfer.effectAllowed = 'move';
      });
      el.addEventListener('dragend', () => {
        el.classList.remove('dragging');
        wrap.querySelectorAll('.kb-lane').forEach(l => l.classList.remove('drop-over'));
      });
    });
    wrap.querySelectorAll('.kb-lane').forEach(lane => {
      const list = lane.querySelector('.kb-cards');
      lane.addEventListener('dragover', e => { e.preventDefault(); lane.classList.add('drop-over'); });
      lane.addEventListener('dragleave', () => lane.classList.remove('drop-over'));
      lane.addEventListener('drop', e => {
        e.preventDefault();
        lane.classList.remove('drop-over');
        const cardId = +e.dataTransfer.getData('text/plain');
        const laneId = +lane.dataset.lane;
        const siblings = [...list.querySelectorAll('.kb-card')].filter(x => +x.dataset.card !== cardId);
        let idx = siblings.length;
        for (let i = 0; i < siblings.length; i++) {
          const r = siblings[i].getBoundingClientRect();
          if (e.clientY < r.top + r.height / 2) { idx = i; break; }
        }
        jsend(`/api/kanban/cards/${cardId}`, 'PATCH', { lane_id: laneId, position: idx })
          .then(refreshKbBoard).catch(() => refreshKbBoard());
      });
    });
  }

  function startLaneRename(el) {
    const laneId = +el.dataset.lane;
    const orig = el.textContent;
    el.setAttribute('contenteditable', 'true');
    el.focus();
    try {
      const r = document.createRange();
      r.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges(); sel.addRange(r);
    } catch (e) {}
    let settled = false;
    const done = (save) => {
      if (settled) return;
      settled = true;
      el.removeAttribute('contenteditable');
      const v = el.textContent.trim();
      if (save && v && v !== orig) {
        jsend(`/api/kanban/lanes/${laneId}`, 'PATCH', { name: v }).then(refreshKbBoard).catch(() => refreshKbBoard());
      } else { el.textContent = orig; }
    };
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); done(true); }
      else if (e.key === 'Escape') { e.preventDefault(); done(false); }
    });
    el.addEventListener('blur', () => done(true));
  }

  const findCard = (id) => {
    for (const l of (kbBoard.lanes || [])) {
      const c = l.cards.find(x => x.id === id);
      if (c) return c;
    }
    return null;
  };
  function refreshKbBoard(b) {
    if (b && b.lanes) { kbBoard = b; renderKbBoard(); loadKbBoardsMetaOnly(); }
    else openKbBoard(kbBoard.slug).then(loadKbBoardsMetaOnly);
  }
  function loadKbBoardsMetaOnly() {
    jget('/api/kanban/boards').then(d => { kbBoards = d.boards || []; renderKbTabs(); }).catch(() => {});
  }

  function kbNewBoard() {
    const name = prompt(t('kb_new_board_prompt'));
    if (!name || !name.trim()) return;
    jsend('/api/kanban/boards', 'POST', { name: name.trim() }).then(b => {
      kbActiveSlug = b.slug; localStorage.setItem('acta_kb_board', b.slug);
      loadKbBoards();
    }).catch(err => alert(err.message));
  }
  function kbRenameBoard() {
    if (!kbBoard) return;
    const name = prompt(t('kb_rename_board_prompt'), kbBoard.name);
    if (!name || !name.trim()) return;
    jsend('/api/kanban/boards/' + kbBoard.slug, 'PATCH', { name: name.trim() }).then(loadKbBoards).catch(err => alert(err.message));
  }
  function kbDeleteBoard() {
    if (!kbBoard) return;
    if (!confirm(t('kb_delete_board_confirm'))) return;
    jsend('/api/kanban/boards/' + kbBoard.slug, 'DELETE').then(() => {
      kbActiveSlug = null; localStorage.removeItem('acta_kb_board'); loadKbBoards();
    }).catch(err => alert(err.message));
  }

  // ---- card modal ----
  let editingCard = null;
  let clItems = [];   // [{text, done}]
  function parseBody(body) {
    const items = [], notes = [];
    (body || '').split('\n').forEach(line => {
      const m = line.trim().match(/^[-*]\s*\[([ xX])\]\s+(.*)$/);
      if (m) items.push({ text: m[2], done: m[1].toLowerCase() === 'x' });
      else if (line.trim()) notes.push(line);
    });
    return { items, notes: notes.join('\n') };
  }
  function buildBody() {
    const cl = clItems.map(i => `- [${i.done ? 'x' : ' '}] ${i.text}`).join('\n');
    const notes = document.getElementById('card-body').value.trim();
    return [cl, notes].filter(Boolean).join('\n\n');
  }
  function renderChecklistEditor() {
    const box = document.getElementById('card-checklist');
    box.innerHTML = clItems.map((it, i) => `
      <div class="card-cl-item${it.done ? ' done' : ''}" data-i="${i}">
        <span class="cl-box" data-i="${i}"></span>
        <span class="cl-text">${escText(it.text)}</span>
        <button class="cl-del" data-i="${i}" type="button">✕</button>
      </div>`).join('');
    box.querySelectorAll('.cl-box').forEach(b => b.addEventListener('click', () => {
      clItems[+b.dataset.i].done = !clItems[+b.dataset.i].done; renderChecklistEditor();
    }));
    box.querySelectorAll('.cl-del').forEach(b => b.addEventListener('click', () => {
      clItems.splice(+b.dataset.i, 1); renderChecklistEditor();
    }));
  }
  function openCardModal(card) {
    editingCard = card;
    const modal = document.getElementById('card-modal');
    const lane = (kbBoard.lanes || []).find(l => l.cards.some(c => c.id === card.id));
    document.getElementById('card-modal-lane').textContent = lane ? lane.name : '';
    document.getElementById('card-title').value = card.text;
    document.getElementById('card-tags').value = (card.tags || []).join(', ');
    const { items, notes } = parseBody(card.body);
    clItems = items;
    document.getElementById('card-body').value = notes;
    renderChecklistEditor();
    modal.classList.add('show');
    setTimeout(() => document.getElementById('card-title').focus(), 40);
  }
  function closeCardModal() {
    document.getElementById('card-modal').classList.remove('show');
    editingCard = null; clItems = [];
  }
  function wireCardModal() {
    const modal = document.getElementById('card-modal');
    modal.addEventListener('click', e => { if (e.target === modal) closeCardModal(); });
    document.getElementById('card-close').addEventListener('click', closeCardModal);
    document.getElementById('card-cancel').addEventListener('click', closeCardModal);
    const clInput = document.getElementById('card-cl-input');
    const addCl = () => {
      const v = clInput.value.trim();
      if (!v) return;
      clItems.push({ text: v, done: false }); clInput.value = ''; renderChecklistEditor(); clInput.focus();
    };
    document.getElementById('card-cl-add-btn').addEventListener('click', addCl);
    clInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addCl(); } });
    document.getElementById('card-save').addEventListener('click', () => {
      if (!editingCard) return;
      const text = document.getElementById('card-title').value.trim();
      if (!text) { document.getElementById('card-title').focus(); return; }
      const tags = document.getElementById('card-tags').value.split(',').map(s => s.trim()).filter(Boolean);
      jsend(`/api/kanban/cards/${editingCard.id}`, 'PATCH', { text, body: buildBody(), tags })
        .then(b => { closeCardModal(); refreshKbBoard(b); }).catch(err => alert(err.message));
    });
    document.getElementById('card-delete').addEventListener('click', () => {
      if (!editingCard) return;
      jsend(`/api/kanban/cards/${editingCard.id}`, 'DELETE')
        .then(b => { closeCardModal(); refreshKbBoard(b); }).catch(() => {});
    });
  }

  // ========================================================================
  //  PARK A THOUGHT  (localStorage — per device)
  // ========================================================================
  let thoughts = [];
  try { thoughts = JSON.parse(localStorage.getItem('acta_thoughts') || '[]'); } catch (e) { thoughts = []; }
  let thoughtId = (thoughts.reduce((m, x) => Math.max(m, x.id || 0), 0)) + 1;
  const saveThoughts = () => localStorage.setItem('acta_thoughts', JSON.stringify(thoughts));
  let editingThoughtId = null;

  function renderThoughts() {
    const list = document.getElementById('thoughts-list');
    if (!list) return;
    const cnt = document.getElementById('thoughts-count');
    if (cnt) cnt.textContent = thoughts.length;
    if (!thoughts.length) {
      list.innerHTML = `<div class="thoughts-empty">— ${LANG === 'pt' ? 'Sem pensamentos guardados' : 'No parked thoughts'} —</div>`;
      return;
    }
    list.innerHTML = thoughts.map(th => {
      const time = (() => { const d = new Date(th.ts); return pad(d.getHours()) + ':' + pad(d.getMinutes()); })();
      const ff = th.fromFocus ? `<span style="color:var(--accent);"> · ${LANG === 'pt' ? 'do modo foco' : 'from focus'}</span>` : '';
      if (th.id === editingThoughtId) {
        return `<div class="thought editing" data-id="${th.id}">
          <input type="text" class="th-title-input" value="${esc(th.body)}" />
          <div class="th-edit-actions">
            <button type="button" data-act="cancel">${LANG === 'pt' ? 'Cancelar' : 'Cancel'}</button>
            <button type="button" class="primary" data-act="save">${LANG === 'pt' ? 'Guardar' : 'Save'}</button>
          </div>
        </div>`;
      }
      return `<div class="thought" data-id="${th.id}">
        <div><div class="body">${escText(th.body).replace(/\n/g, '<br>')}</div>
        <div class="meta">${time}${ff}</div></div>
        <div class="th-actions">
          <button class="th-edit-btn" type="button" data-act="edit">✎</button>
          <button class="del" type="button" data-act="del">✕</button>
        </div>
      </div>`;
    }).join('');
    list.querySelectorAll('.thought').forEach(row => {
      const id = +row.dataset.id;
      row.addEventListener('click', e => {
        const act = e.target.dataset.act;
        if (!act) return;
        e.stopPropagation();
        if (act === 'edit') { editingThoughtId = id; renderThoughts(); }
        else if (act === 'del') { thoughts = thoughts.filter(x => x.id !== id); saveThoughts(); renderThoughts(); }
        else if (act === 'cancel') { editingThoughtId = null; renderThoughts(); }
        else if (act === 'save') {
          const v = row.querySelector('.th-title-input').value.trim();
          const th = thoughts.find(x => x.id === id);
          if (th && v) th.body = v;
          editingThoughtId = null; saveThoughts(); renderThoughts();
        }
      });
    });
  }
  function addThought() {
    const inp = document.getElementById('thought-input');
    const v = inp.value.trim();
    if (!v) return;
    thoughts.unshift({ id: thoughtId++, body: v, ts: Date.now() });
    saveThoughts(); inp.value = ''; renderThoughts();
  }

  // ========================================================================
  //  FOCUS MODE  (localStorage per device; today total resets on date change)
  //
  //  Timestamp-based, not tick-counting: the clock is always derived from
  //  Date.now(), so a throttled background tab loses no time. FOCUS_V versions
  //  the storage key.
  // ========================================================================
  const FOCUS_V = 2;
  const todayStr = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };

  let focusRunning = false;
  let runStartTs = 0;          // Date.now() when the current running segment began
  let sessionAccumSec = 0;     // seconds from finished segments this session
  let todayBaseSec = 0;        // today's persisted total, as of session open / last reset
  let focusDateAtOpen = todayStr();
  let focusInterval = null;
  let sessionParkedIds = [];

  function readFocusStore() {
    // the buggy date-less keys held time accumulated across many days — drop them
    localStorage.removeItem('acta_focus_today');
    let obj = null;
    try { obj = JSON.parse(localStorage.getItem('acta_focus') || 'null'); } catch (e) {}
    if (!obj || obj.v !== FOCUS_V || obj.date !== todayStr()) {
      obj = { v: FOCUS_V, date: todayStr(), seconds: 0 };
      localStorage.setItem('acta_focus', JSON.stringify(obj));
    }
    return obj;
  }
  const writeFocusStore = (sec) =>
    localStorage.setItem('acta_focus', JSON.stringify({ v: FOCUS_V, date: todayStr(), seconds: Math.round(sec) }));

  let focusTotalToday = readFocusStore().seconds;

  const liveSessionSec = () =>
    sessionAccumSec + (focusRunning ? (Date.now() - runStartTs) / 1000 : 0);

  const fmtFocus = (sec) => {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h > 0 ? pad(h) + ':' : '') + pad(m) + ':' + pad(s);
  };

  // recompute everything from timestamps + persist — safe to call at any cadence
  function syncFocus() {
    if (focusDateAtOpen !== todayStr()) {   // session crossed midnight
      focusDateAtOpen = todayStr();
      todayBaseSec = 0;
    }
    const sess = liveSessionSec();
    focusTotalToday = todayBaseSec + sess;
    writeFocusStore(focusTotalToday);

    const te = document.getElementById('focus-time');
    if (te) {
      te.textContent = fmtFocus(sess);
      const fe = document.getElementById('focus-elapsed');
      if (fe) {
        if (sess < 1 && !focusRunning) fe.textContent = t('ready_when_you_are');
        else fe.textContent = (focusRunning ? (LANG === 'pt' ? 'A correr' : 'Running')
          : (LANG === 'pt' ? 'Em pausa' : 'Paused')) + ' · ' + fmtFocus(sess);
      }
    }
    updateFocusTotal();
    setFocusBtnLabel();
  }
  function updateFocusTotal() {
    const big = document.getElementById('focus-total-big');
    if (!big) return;
    const totalMin = Math.floor(focusTotalToday / 60);
    big.innerHTML = `${Math.floor(totalMin / 60)}<span class="unit">h</span> ${pad(totalMin % 60)}<span class="unit">m</span>`;
    const small = document.getElementById('focus-total');
    if (small) small.textContent = totalMin + 'm';
    const bar = document.getElementById('focus-bar');
    if (bar) bar.style.width = Math.min(100, (focusTotalToday / (4 * 3600)) * 100) + '%';
  }

  function startFocus() {
    const btn = document.getElementById('focus-start');
    if (focusRunning) {                         // → pause
      sessionAccumSec += (Date.now() - runStartTs) / 1000;
      focusRunning = false; runStartTs = 0;
      clearInterval(focusInterval); focusInterval = null;
      btn.textContent = t('start');
    } else {                                    // → start / resume
      runStartTs = Date.now();
      focusRunning = true;
      clearInterval(focusInterval);
      focusInterval = setInterval(syncFocus, 1000);  // display only; math is Date.now()-based
      btn.textContent = t('pause');
    }
    syncFocus();
  }
  function resetFocus() {
    // bank whatever already counted toward today, then zero the session stopwatch
    todayBaseSec += liveSessionSec();
    sessionAccumSec = 0;
    runStartTs = focusRunning ? Date.now() : 0;
    syncFocus();
  }
  const focusSessionActive = () => focusRunning || sessionAccumSec > 0 || sessionParkedIds.length > 0;

  // reflect a hidden-but-running session on the "Focus Time" button
  function setFocusBtnLabel() {
    const span = document.querySelector('#focus-btn span');
    if (!span) return;
    const overlayOpen = document.getElementById('focus-overlay').classList.contains('show');
    if (!overlayOpen && focusSessionActive())
      span.textContent = (focusRunning ? '● ' : '⏸ ') + fmtFocus(liveSessionSec());
    else
      span.textContent = t('focus_time');
  }

  function openFocus() {
    if (!focusSessionActive()) {                 // fresh session
      todayBaseSec = readFocusStore().seconds;
      sessionAccumSec = 0; runStartTs = 0; focusRunning = false;
      focusDateAtOpen = todayStr();
      sessionParkedIds = [];
    }
    document.getElementById('focus-overlay').classList.add('show');
    document.getElementById('focus-start').textContent = focusRunning ? t('pause') : t('start');
    if (focusRunning && !focusInterval) focusInterval = setInterval(syncFocus, 1000);
    // drop focus from the "Focus Time" button so Space hits the shortcut handler
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    syncFocus();
    renderFocusThoughts();
    setFocusBtnLabel();
  }
  // Esc — step away, keep the timer (and its state) exactly as it is.
  function hideFocus() {
    document.getElementById('focus-overlay').classList.remove('show');
    setFocusBtnLabel();
  }
  // ✕ EXIT — finish the session: stop the clock, fold it into today, run triage.
  function closeFocus() {
    if (focusRunning) { sessionAccumSec += (Date.now() - runStartTs) / 1000; }
    focusRunning = false; runStartTs = 0;
    clearInterval(focusInterval); focusInterval = null;
    syncFocus();                                // persists todayBaseSec + session
    todayBaseSec = focusTotalToday;             // fold session into the base
    sessionAccumSec = 0;
    const parked = thoughts.filter(th => sessionParkedIds.includes(th.id));
    sessionParkedIds = [];                      // session is over — clear before relabel
    sessionThoughts = []; renderFocusThoughts();
    renderThoughts();
    document.getElementById('focus-overlay').classList.remove('show');
    setFocusBtnLabel();                         // now shows "Focus Time" again
    if (parked.length) openTriage(parked);
  }

  let sessionThoughts = [];
  function renderFocusThoughts() {
    const list = document.getElementById('focus-thought-list');
    if (!list) return;
    list.innerHTML = sessionThoughts.map(b => `<div class="focus-thought-item">${escText(b)}</div>`).join('');
    const c = document.getElementById('focus-thought-count');
    if (c) c.textContent = sessionThoughts.length;
  }
  function addFocusThought() {
    const inp = document.getElementById('focus-thought-input');
    const v = inp.value.trim();
    if (!v) return;
    sessionThoughts.unshift(v);
    const th = { id: thoughtId++, body: v, ts: Date.now(), fromFocus: true };
    thoughts.unshift(th);
    sessionParkedIds.push(th.id);
    saveThoughts();
    inp.value = '';
    renderFocusThoughts();
  }
  function wireFocus() {
    document.getElementById('focus-start').addEventListener('click', startFocus);
    document.getElementById('focus-reset').addEventListener('click', resetFocus);
    document.getElementById('focus-close').addEventListener('click', closeFocus);
    // Back-gesture support (see BackStack in index.html) — closeFocus does
    // real work (stops the timer, persists elapsed time), so it has to be
    // the actual close callback, not a bare classList toggle.
    if (typeof backGestureFor === 'function') {
      const fo = document.getElementById('focus-overlay');
      backGestureFor(fo, () => fo.classList.contains('show'), closeFocus);
    }
    document.getElementById('focus-thought-save').addEventListener('click', addFocusThought);
    document.getElementById('focus-thought-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); addFocusThought(); }
      else if (e.key === 'Escape') { e.stopPropagation(); e.target.blur(); }
    });
    // returning from a backgrounded tab: jump the display straight to the
    // correct time instead of waiting for the throttled interval to catch up
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && document.getElementById('focus-overlay').classList.contains('show')) syncFocus();
    });
  }

  // ---- keyboard shortcuts (one document-level handler) ----
  function wireShortcuts() {
    const shown = (id) => document.getElementById(id).classList.contains('show');
    document.addEventListener('keydown', e => {
      const typing = /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(e.target.tagName) || e.target.isContentEditable;

      if (e.key === 'Escape') {
        if (shown('triage-modal')) { closeTriage(); return; }
        if (shown('card-modal')) { closeCardModal(); return; }
        if (shown('event-modal')) { closeEventModal(); return; }
        if (shown('focus-overlay')) { hideFocus(); return; }   // leave, keep running
        return;
      }

      if (!shown('focus-overlay')) return;   // remaining shortcuts are focus-mode only

      if (e.key === ' ' && !typing) {         // start / pause
        e.preventDefault();
        startFocus();
      } else if ((e.ctrlKey || e.metaKey) && (e.key === 'p' || e.key === 'P')) {
        e.preventDefault();                   // beat the browser's print dialog
        const inp = document.getElementById('focus-thought-input');
        inp.focus();
        inp.classList.add('kbd-flash');
        setTimeout(() => inp.classList.remove('kbd-flash'), 600);
      }
    });
  }

  // ---- triage: where do session-parked thoughts go? ----
  function openTriage(parked) {
    const modal = document.getElementById('triage-modal');
    const list = document.getElementById('triage-list');
    const nativeBoards = kbBoards.filter(b => b.kind === 'native');
    const opts = nativeBoards.map(b => `<option value="${esc(b.slug)}">${escText(b.name)}</option>`).join('');
    list.innerHTML = parked.map(th => `
      <div class="triage-row" data-id="${th.id}">
        <div class="triage-text">${escText(th.body)}</div>
        <div class="triage-actions">
          ${nativeBoards.length ? `<select>${opts}</select><button class="send" type="button">${t('triage_send')}</button>` : ''}
          <button class="keep" type="button">${t('triage_keep')}</button>
          <button class="discard" type="button">${t('triage_discard')}</button>
        </div>
      </div>`).join('');
    list.querySelectorAll('.triage-row').forEach(row => {
      const id = +row.dataset.id;
      const th = thoughts.find(x => x.id === id);
      const finish = () => { row.classList.add('gone'); if (!list.querySelector('.triage-row:not(.gone)')) closeTriage(); };
      const sendBtn = row.querySelector('.send');
      if (sendBtn) sendBtn.addEventListener('click', () => {
        const slug = row.querySelector('select').value;
        jget('/api/kanban/boards/' + encodeURIComponent(slug)).then(b => {
          const lane = (b.lanes.find(l => !l.is_done_lane) || b.lanes[0]);
          if (!lane) throw new Error('no lane');
          return jsend(`/api/kanban/boards/${slug}/cards`, 'POST', { lane_id: lane.id, text: th.body });
        }).then(() => {
          thoughts = thoughts.filter(x => x.id !== id); saveThoughts(); renderThoughts();
          if (slug === (kbBoard && kbBoard.slug)) refreshKbBoard();
          loadKbBoardsMetaOnly();
          finish();
        }).catch(err => alert(err.message));
      });
      row.querySelector('.keep').addEventListener('click', finish);
      row.querySelector('.discard').addEventListener('click', () => {
        thoughts = thoughts.filter(x => x.id !== id); saveThoughts(); renderThoughts(); finish();
      });
    });
    modal.classList.add('show');
  }
  function closeTriage() { document.getElementById('triage-modal').classList.remove('show'); }
  function wireTriage() {
    const modal = document.getElementById('triage-modal');
    document.getElementById('triage-done').addEventListener('click', closeTriage);
    modal.addEventListener('click', e => { if (e.target === modal) closeTriage(); });
  }

  // ========================================================================
  //  MAIN-SCREEN CARDS  (Today · Key  +  Calendar · Today)
  // ========================================================================
  // Today · Key shows the open cards of the first board's priority lane.
  let todayKeySlug = null;
  function renderTodayKey() {
    const list = document.getElementById('task-list');
    const count = document.getElementById('task-count');
    if (!list) return;
    jget('/api/kanban/boards').then(d => {
      todayKeySlug = ((d.boards || [])[0] || {}).slug || null;
      if (!todayKeySlug) {
        if (count) count.textContent = '';
        list.innerHTML = `<div class="task-empty">${t('today_key_empty')}</div>`;
        return null;
      }
      return jget('/api/kanban/boards/' + encodeURIComponent(todayKeySlug));
    }).then(b => {
      if (!b) return;
      const lanes = b.lanes || [];
      const prio = lanes.find(l => /priorit/i.test(l.name)) || lanes[0];
      const open = ((prio && prio.cards) || []).filter(c => !c.checked).slice(0, 5);
      if (count) count.textContent = open.length + (LANG === 'pt' ? (open.length === 1 ? ' tarefa' : ' tarefas') : (open.length === 1 ? ' task' : ' tasks'));
      if (!open.length) {
        list.innerHTML = `<div class="task-empty">${t('today_key_empty')}</div>`;
      } else {
        list.innerHTML = open.map(c => `
          <div class="task">
            <span class="box"></span>
            <div class="body"><div class="t">${escText(stripMd(c.text))}</div>
            <div class="tag">${escText(prio.name)}</div></div>
          </div>`).join('')
          + `<div class="card-f"><span class="link">${t('open_in_boards')} <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.75\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\"><path d=\"M5 12h14M13 6l6 6-6 6\"/></svg></span></div>`;
      }
      list.onclick = gotoBoards;
    }).catch(() => { if (count) count.textContent = ''; });
  }
  const stripMd = (s) => (s || '').replace(/\*\*/g, '').replace(/\[\[([^\]|]+)(\|[^\]]+)?\]\]/g, '$1');
  function gotoBoards() {
    if (todayKeySlug) { kbActiveSlug = todayKeySlug; localStorage.setItem('acta_kb_board', todayKeySlug); }
    prodSubtab = 'boards';
    const btn = document.querySelector('.bottom-tabs .tab[data-tab="productivity"]');
    if (btn) btn.click();
    setSubtab('boards');
  }

  function renderTodayEvents() {
    const list = document.getElementById('today-events');
    const count = document.getElementById('today-events-count');
    const dateEl = document.getElementById('cal-date');
    if (dateEl) {
      const d = new Date();
      const M = LANG === 'pt' ? MONTHS_PT : MONTHS_EN;
      dateEl.textContent = `${d.getDate()} ${M[d.getMonth()]} ${d.getFullYear()}`;
    }
    if (!list) return;
    const td = todayDow();
    const now = new Date();
    const nm = now.getHours() * 60 + now.getMinutes();
    const todays = scheduleBlocks.filter(e => e.dow === td).sort((a, b) => a.start_min - b.start_min);
    list.innerHTML = todays.map(ev => {
      const isNow = nm >= ev.start_min && nm < ev.end_min;
      return `<div class="cal-event${isNow ? ' now' : ''}" data-cat="${esc(ev.category)}" style="--cat:${catColor(ev.category)};">
        <div class="time">${fmtMin(ev.start_min)} —<br/>${fmtMin(ev.end_min)}</div>
        <div class="title">${escText(ev.title)}</div>
        <div class="tag">${escText(catLabel(ev.category))}</div>
      </div>`;
    }).join('') || `<div class="cal-empty">${LANG === 'pt' ? 'Nada agendado hoje' : 'Nothing scheduled today'}</div>`;
    if (count) count.textContent = todays.length;
  }

  // ========================================================================
  //  INIT
  // ========================================================================
  function init() {
    buildShell();
    wireEventModal();
    wireCardModal();
    wireFocus();
    wireTriage();
    wireShortcuts();
    updateFocusTotal();
    renderThoughts();
    // pick up persisted sub-tab
    setSubtab(prodSubtab === 'boards' ? 'boards' : 'calendar');
    // main-screen data + ribbon load immediately (Main tab is the default view)
    loadSchedule();
    loadWeekEnergy();
    renderTodayKey();
    setInterval(renderTodayEvents, 60000);
  }

  window.productivityOnShow = function () {
    loadSchedule();
    loadWeekEnergy();
    if (prodSubtab === 'boards') loadKbBoards();
    else { renderWeek(); renderWeekEnergy(); }
  };

  init();
})();

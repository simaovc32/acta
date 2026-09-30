// ============== INIT ==============
applyLang();
// renderTodayEvents() init + interval moved to js/productivity.js

// ============================================================
// LIVE API DATA — replaces demo values with real acta data
// ============================================================
let liveHypnoStages = null; // written by initLive, read by renderHypnogram
let liveFullSeries  = [];   // full 360h series; used by day modal for previous-day view
let dataStartDate   = null; // 'YYYY-MM-DD' — earliest day with data (from health-summary)
// Past days are immutable once ingest has settled, so a fetched day is cached
// for the session. Today is never cached — it is still being written.
const daySeriesCache = new Map();
const daySeriesPending = new Set();

function dayKey(d){
  const pad = n => String(n).padStart(2, '0');   // p2() is scoped inside initLive
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ── Global day context ────────────────────────────────────────────────────
// null = live (today). Any other value is a 'YYYY-MM-DD' the whole Health
// screen renders instead — one date, every sub-tab, read-only.
let viewDate    = null;
let loadDayView = async () => {};   // assigned inside initLive
let reloadLive  = async () => {};   // ditto — loadLiveData is scoped in there
const todayKey  = () => dayKey(new Date());
const activeDay = () => viewDate || todayKey();

function shiftDay(key, delta){
  const d = new Date(key + 'T00:00:00');
  d.setDate(d.getDate() + delta);
  return dayKey(d);
}

async function setViewDate(next){
  // Normalising to null when the target is today keeps exactly one
  // representation of "live", so past-mode can never latch on on today.
  viewDate = (!next || next === todayKey()) ? null : next;
  const screen = document.getElementById('screen-health');
  if (screen) screen.classList.toggle('past-mode', viewDate !== null);

  // Panel chrome that asserts "today"/"LIVE" has to follow the selected day,
  // or a past view reads as live data. The Snapshot panel is .live-only (hidden
  // in past mode) so it is deliberately not touched here.
  if (screen) {
    const MONU = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const past = viewDate !== null;
    const dv = past ? new Date(viewDate + 'T00:00:00') : null;
    const energyLbl = screen.querySelector('.energy-panel .panel-head .label');
    if (energyLbl) energyLbl.textContent = past
      ? `Energy · ${dv.getDate()} ${MONU[dv.getMonth()]}`
      : 'Energy · today';
    const bioBadge = screen.querySelector('.bio-tab-panel .panel-head .meta');
    if (bioBadge) bioBadge.textContent = past ? 'PAST' : 'LIVE';
    // Only the two intraday Vitals charts are day-scoped; the trend panels below
    // them say "· 14 days" and stay accurate (they just end on the viewed date).
    ['vd-hr-chart', 'vd-stress-chart'].forEach(id => {
      const sub = screen.querySelector(`#${id}`)?.closest('.vital-day-panel')?.querySelector('.vd-sub');
      if (sub) sub.textContent = past ? `· ${dv.getDate()} ${MONU[dv.getMonth()]}` : '· today';
    });
  }

  renderDayNav();
  if (viewDate) await loadDayView(viewDate);
  else          await reloadLive();
  renderDayEnergyBig();
}

function renderDayNav(){
  const DAYU = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const MONU = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const key  = activeDay();
  const d    = new Date(key + 'T00:00:00');
  const lbl  = document.getElementById('health-date');
  if (lbl) {
    lbl.textContent = viewDate === null
      ? `Today · ${d.getDate()} ${MONU[d.getMonth()]}`
      : `${DAYU[d.getDay()]} · ${d.getDate()} ${MONU[d.getMonth()]}`;
  }
  const prev = document.getElementById('daynav-prev');
  const next = document.getElementById('daynav-next');
  if (prev) prev.disabled = !!(dataStartDate && key <= dataStartDate);
  if (next) next.disabled = viewDate === null;
  const banner = document.getElementById('past-banner-text');
  if (banner && viewDate) {
    banner.textContent = `Viewing ${d.toDateString()} · read-only`;
  }
}

// ── Calendar popup ────────────────────────────────────────────────────────
let calMonth = null;   // Date pinned to the 1st of the displayed month

function renderCalendar(){
  const grid  = document.getElementById('daycal-grid');
  const title = document.getElementById('daycal-title');
  if (!grid || !calMonth) return;
  const MONF = ['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY',
                'AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
  title.textContent = `${MONF[calMonth.getMonth()]} ${calMonth.getFullYear()}`;

  const first = new Date(calMonth.getFullYear(), calMonth.getMonth(), 1);
  const days  = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 0).getDate();
  // Monday-first: JS getDay() is Sunday-first, so Sunday wraps to the end.
  const lead  = (first.getDay() + 6) % 7;
  const tkey  = todayKey();
  const sel   = activeDay();

  let html = '';
  for (let i = 0; i < lead; i++) html += '<button class="daycal-cell empty" disabled></button>';
  for (let n = 1; n <= days; n++) {
    const k = dayKey(new Date(calMonth.getFullYear(), calMonth.getMonth(), n));
    const disabled = (dataStartDate && k < dataStartDate) || k > tkey;
    const cls = ['daycal-cell'];
    if (k === tkey) cls.push('today');
    if (k === sel)  cls.push('selected');
    html += `<button class="${cls.join(' ')}" data-day="${k}"${disabled ? ' disabled' : ''}>${n}</button>`;
  }
  grid.innerHTML = html;

  // Month arrows stop at the edges of the data rather than wandering into
  // months that can hold nothing.
  const pm = document.getElementById('daycal-prev');
  const nm = document.getElementById('daycal-next');
  if (pm) pm.disabled = !!(dataStartDate &&
    dayKey(new Date(calMonth.getFullYear(), calMonth.getMonth(), 1)) <= dataStartDate.slice(0, 8) + '01');
  if (nm) nm.disabled = calMonth.getFullYear() === new Date().getFullYear()
                     && calMonth.getMonth()    === new Date().getMonth();
}

function toggleCalendar(force){
  const cal = document.getElementById('daycal');
  if (!cal) return;
  const show = force !== undefined ? force : !cal.classList.contains('show');
  if (show) {
    const d = new Date(activeDay() + 'T00:00:00');
    calMonth = new Date(d.getFullYear(), d.getMonth(), 1);
    renderCalendar();
  }
  cal.classList.toggle('show', show);
}

(function wireDayNav(){
  const prev = document.getElementById('daynav-prev');
  const next = document.getElementById('daynav-next');
  const date = document.getElementById('daynav-date');
  const tdy  = document.getElementById('daynav-today');
  const cal  = document.getElementById('daycal');
  if (!prev) return;

  prev.addEventListener('click', () => setViewDate(shiftDay(activeDay(), -1)));
  next.addEventListener('click', () => setViewDate(shiftDay(activeDay(),  1)));
  tdy .addEventListener('click', () => { toggleCalendar(false); setViewDate(null); });
  date.addEventListener('click', e => { e.stopPropagation(); toggleCalendar(); });

  document.getElementById('daycal-prev').addEventListener('click', e => {
    e.stopPropagation();
    calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() - 1, 1);
    renderCalendar();
  });
  document.getElementById('daycal-next').addEventListener('click', e => {
    e.stopPropagation();
    calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1);
    renderCalendar();
  });
  document.getElementById('daycal-grid').addEventListener('click', e => {
    const b = e.target.closest('.daycal-cell');
    if (!b || b.disabled || !b.dataset.day) return;
    toggleCalendar(false);
    setViewDate(b.dataset.day);
  });
  cal.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', () => toggleCalendar(false));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && cal.classList.contains('show')) toggleCalendar(false);
  });
  backGestureFor(cal, () => cal.classList.contains('show'), () => toggleCalendar(false));
})();

function fetchDaySeries(key){
  if (daySeriesPending.has(key)) return;
  daySeriesPending.add(key);
  Promise.all([
    fetch(`/api/biocharge?date=${key}`).then(r => r.ok ? r.json() : []).catch(() => []),
    fetch(`/api/naps?date=${key}`).then(r => r.ok ? r.json() : []).catch(() => []),
    fetch(`/api/activities?date=${key}`).then(r => r.ok ? r.json() : []).catch(() => []),
  ]).then(([series, naps, acts]) => {
    daySeriesCache.set(key, series || []);
    daySeriesPending.delete(key);
    // Only repaint if the user is still looking at the day we just loaded.
    if (activeDay() === key) {
      liveDayNaps = Array.isArray(naps) ? naps : [];
      liveDayActivities = Array.isArray(acts) ? acts : [];
      renderDayEnergyBig();
    }
  });
}
let rerenderBedtimeCurve = () => {}; // set inside initLive; called on tab-show to fix SVG measuring
let rerenderVitalDays   = () => {}; // set inside initLive; redraws intraday charts when Vitals tab shows
let rerenderFood        = () => {}; // set inside initLive; Food charts measure 0 while hidden
let rerenderBioSteps    = () => {}; // set inside initLive; redraws the Biocharge-tab steps bars on show

// Acta — Auspex tab. Preset analytical questions answered over Acta's own data.
// Classic script, loaded after the main inline script. Wrapped in an IIFE;
// exposes only window.auspexOnShow. Owns everything rendered into #screen-auspex.
//
// The five groups are a segmented control (the shared .subtabs/.subtab), so only
// one group's questions are on screen; the ask block has a fixed height, so the
// feed always begins at the same place, on desktop and phone alike.
//
// There is no free-text box on purpose. Each preset is bound server-side to a
// builder that assembles exactly the facts that question needs, and every
// judgement in those facts is decided in Python before the model sees it. A
// free-text path would have no builder behind it.
//
// Answers show for the current day only and then stop being rendered; they are
// never deleted. /api/auspex/today filters by timestamp and the full history
// stays in auspex_query. The boundary is waking, not midnight — see
// auspex.day_start_ms.
(function () {
  'use strict';

  const EXPLORE_ID = '__explore';
  const EXPLORE_TEXT = 'Propose and test new hypotheses about my data';

  // Hue carries provenance — which body of data a question interrogates — using
  // the same tokens the rest of Acta uses for those domains. Interaction colour
  // stays --accent (amber) throughout, so hue never doubles as chrome.
  const HUE = {
    day_energy: 'bio', wake_level: 'bio',
    worst_nights: 'sleep', sleep_component_drift: 'sleep',
    period_compare: 'sleep', bedtime_drift: 'sleep', tonight: 'sleep',
    weekday_weekend: 'vitals',
    recovery_time: 'workout',
    mental_state: 'mental',
  };

  const STAGES = [
    ['Assembling facts', 'read-only query over acta.db'],
    ['Computing verdicts', 'trend and effect-size gates, in Python'],
    ['Narrating', 'qwen3-235b · ZDR provider'],
  ];
  const STAGES_X = [
    ['Proposing pairs', 'model, against the feature vocabulary'],
    ['Validating proposals', 'starved and definitional pairs rejected'],
    ['Testing survivors', 'lag-mining correlation gates'],
    ['Narrating', 'qwen3-235b · ZDR provider'],
  ];

  const AUX = { questions: null, order: [], group: null, busy: false,
                answered: {}, since: null, timers: [] };

  async function api(path, opts) {
    opts = opts || {};
    const res = await fetch(path, {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
    return data;
  }

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Numbers are the only text left at full brightness in a dimmed paragraph, so
  // they carry weight without colour. Runs on ALREADY-ESCAPED text: the leading
  // lookbehind is what keeps it out of the "39" inside &#39;.
  const RE_NUM = /(\d{4}-\d{2}-\d{2})|(\d{1,2}:\d{2})|([a-zA-Z]{1,2}\s?[=≥≤]\s?[−+\-]?\d*\.?\d+)|((?<![#\w.:-])[−+\-]?\d+(?:[.,]\d+)?(?:\s?(?:bpm|ms|kcal|nights?|days?|points?|pts?|min|h|m|s)\b|\s?%)?)/g;
  const typeset = (t) => t.replace(RE_NUM, (m) => '<span class="aux-n">' + m + '</span>');

  // The lead is taken by POSITION, never by markup — the prompt is unchanged and
  // the model still emits plain prose. The opening paragraph IS the verdict when
  // it is short enough to be one; when the model ran verdict and evidence
  // together into one long opener, sentence one is lifted instead.
  function split(text) {
    const nl = text.indexOf('\n');
    const first = (nl === -1 ? text : text.slice(0, nl)).trim();
    const tail = nl === -1 ? '' : text.slice(nl + 1);
    if (first.length <= 150) return { lead: first, rest: tail };
    const m = first.match(/^([\s\S]*?[.!?])(\s|$)/);
    if (!m) return { lead: first, rest: tail };
    return { lead: m[1], rest: first.slice(m[0].length) + (tail ? '\n' + tail : '') };
  }

  function prose(text) {
    const lines = typeset(esc(text)).split('\n');
    let out = '', inList = false;
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) { if (inList) { out += '</ul>'; inList = false; } continue; }
      const b = line.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
      if (/^[-*]\s+/.test(line)) {
        if (!inList) { out += '<ul class="aux-list">'; inList = true; }
        out += '<li>' + b.replace(/^[-*]\s+/, '') + '</li>';
      } else {
        if (inList) { out += '</ul>'; inList = false; }
        out += '<p>' + b + '</p>';
      }
    }
    if (inList) out += '</ul>';
    return out;
  }

  function fmtTime(iso) {
    try { return new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); }
    catch (e) { return ''; }
  }
  const idx = (qid) => {
    const i = AUX.order.indexOf(qid);
    return i < 0 ? '--' : String(i + 1).padStart(2, '0');
  };
  const hueOf = (qid) => HUE[qid] || null;

  function metaLine(row) {
    return [row.provider,
            row.latency_ms ? (row.latency_ms / 1000).toFixed(1) + 's' : null,
            row.cost_usd != null ? '$' + Number(row.cost_usd).toFixed(4) : null,
            fmtTime(row.created_at)].filter(Boolean).join(' · ');
  }

  function answerCard(row) {
    const qid = row.question_id === 'explore' ? EXPLORE_ID : row.question_id;
    const h = hueOf(qid);
    const s = split(row.answer || '');
    return '<article class="aux-card"' + (h ? ' style="--h: var(--hue-' + h + ')"' : '') + '>'
      + '<header class="aux-card-head"><span class="aux-idx">' + idx(qid) + '</span>'
      + '<span class="aux-card-q">' + esc(row.question_text) + '</span></header>'
      + '<div class="aux-card-body">'
      // The lead is already the emphasis, so **bold** inside it is stripped
      // rather than doubled up. Numbers are still typeset.
      // A long first sentence is still the verdict, so it stays promoted rather than
      // being cut -- it just stops being set at display size.
      + '<p class="aux-lead' + (s.lead.length > 220 ? ' aux-lead-long' : '') + '">'
      + typeset(esc(s.lead.replace(/\*\*/g, ''))) + '</p>'
      + (s.rest.trim() ? '<div class="aux-prose">' + prose(s.rest) + '</div>' : '')
      + '</div>'
      + '<footer class="aux-card-meta">' + esc(metaLine(row)) + '</footer>'
      + '</article>';
  }

  function errorCard(qid, text, msg) {
    return '<article class="aux-card aux-error">'
      + '<header class="aux-card-head"><span class="aux-idx">' + idx(qid) + '</span>'
      + '<span class="aux-card-q">' + esc(text) + '</span></header>'
      + '<div class="aux-card-body"><p class="aux-lead">' + esc(msg) + '</p></div>'
      + '</article>';
  }

  // The wait is 5-20s and genuinely unpredictable, so there is no progress bar —
  // a bar would be a lie. What is shown instead is the pipeline Auspex actually
  // runs, which makes the wait legible, plus a counting elapsed figure.
  function pendingNode(qid, text, stages) {
    const el = document.createElement('article');
    const h = hueOf(qid);
    el.className = 'aux-card aux-pending';
    if (h) el.style.setProperty('--h', 'var(--hue-' + h + ')');
    el.innerHTML = '<header class="aux-card-head"><span class="aux-idx">' + idx(qid) + '</span>'
      + '<span class="aux-card-q">' + esc(text) + '</span>'
      + '<span class="aux-elapsed">0.0s</span></header>'
      + '<div class="aux-stages">' + stages.map((s, i) =>
        '<div class="aux-stage" data-i="' + i + '"><span class="aux-dot"></span>'
        + '<span class="aux-stage-n">' + esc(s[0]) + '</span>'
        + '<span class="aux-stage-d">' + esc(s[1]) + '</span></div>').join('')
      + '</div>';
    const t0 = Date.now();
    const el0 = el.querySelector('.aux-elapsed');
    const tick = setInterval(() => {
      el0.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
    }, 100);
    let i = 0;
    const mark = () => {
      el.querySelectorAll('.aux-stage').forEach((n, k) => {
        n.classList.toggle('done', k < i);
        n.classList.toggle('now', k === i);
      });
    };
    mark();
    const step = setInterval(() => {
      if (i < stages.length - 1) { i += 1; mark(); }
    }, stages.length > 3 ? 3800 : 2600);
    AUX.timers.push(tick, step);
    el._stop = () => { clearInterval(tick); clearInterval(step); };
    return el;
  }

  function clearTimers() {
    AUX.timers.forEach(clearInterval);
    AUX.timers = [];
  }

  function setBusy(on) {
    AUX.busy = on;
    const root = document.getElementById('screen-auspex');
    if (!root) return;
    root.querySelectorAll('.aux-slot').forEach((b) => { b.disabled = on; });
  }

  async function run(qid, text, stages, request) {
    if (AUX.busy) return;
    setBusy(true);
    const feed = document.getElementById('aux-feed');
    const pending = pendingNode(qid, text, stages);
    feed.prepend(pending);
    try {
      const row = await request();
      pending._stop();
      pending.outerHTML = answerCard(row);
      AUX.answered[row.question_id] = row.created_at;
      renderPane();
      refreshTally();
    } catch (err) {
      pending._stop();
      pending.outerHTML = errorCard(qid, text, err.message || 'request failed');
    } finally {
      setBusy(false);
    }
  }

  const askQuestion = (q) => run(q.id, q.text, STAGES,
    () => api('/api/auspex/ask', { method: 'POST', body: { question_id: q.id } }));
  const runExplore = () => run(EXPLORE_ID, EXPLORE_TEXT, STAGES_X,
    () => api('/api/auspex/explore', { method: 'POST' }));

  // ── rendering ──────────────────────────────────────────────────────────────

  function groups() {
    const g = [];
    AUX.questions.forEach((q) => {
      let e = g.find((x) => x.name === q.group);
      if (!e) { e = { name: q.group, items: [] }; g.push(e); }
      e.items.push(q);
    });
    g.push({ name: 'Discovery', items: [{ id: EXPLORE_ID, text: EXPLORE_TEXT,
                                          group: 'Discovery', explore: true }] });
    return g;
  }

  const readCount = () => Object.keys(AUX.answered).length;

  function refreshTally() {
    const t = document.getElementById('aux-tally');
    if (t) t.textContent = readCount() + ' / ' + AUX.order.length + ' read';
    document.querySelectorAll('.aux-seg').forEach((b) => {
      const g = groups().find((x) => x.name === b.dataset.group);
      if (!g) return;
      const done = g.items.filter((q) => AUX.answered[q.explore ? 'explore' : q.id]).length;
      const c = b.querySelector('.aux-seg-n');
      if (c) c.textContent = done ? done + '/' + g.items.length : String(g.items.length);
      b.classList.toggle('aux-seg-done', done === g.items.length);
    });
  }

  function renderPane() {
    const pane = document.getElementById('aux-pane');
    if (!pane) return;
    const g = groups().find((x) => x.name === AUX.group) || groups()[0];
    pane.innerHTML = g.items.map((q) => {
      const key = q.explore ? 'explore' : q.id;
      const when = AUX.answered[key];
      const h = hueOf(q.explore ? EXPLORE_ID : q.id);
      return '<button class="aux-slot' + (q.explore ? ' aux-slot-x' : '')
        + (when ? ' aux-slot-read' : '') + '" data-qid="' + esc(q.id) + '"'
        + (h ? ' style="--h: var(--hue-' + h + ')"' : '') + '>'
        + '<span class="aux-idx">' + idx(q.explore ? EXPLORE_ID : q.id) + '</span>'
        + '<span class="aux-slot-t">' + esc(q.text) + '</span>'
        + '<span class="aux-slot-s">' + (when ? 'Read ' + fmtTime(when) : 'Ask') + '</span>'
        + '</button>';
    }).join('')
      + (g.name === 'Discovery'
        ? '<p class="aux-note">Auspex suggests pairings worth checking; every one is '
          + 'then tested against your own history before it is reported. Two model '
          + 'calls, so slower than a preset question.</p>' : '');
    pane.querySelectorAll('.aux-slot').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.qid === EXPLORE_ID) return runExplore();
        const q = AUX.questions.find((x) => x.id === btn.dataset.qid);
        if (q) askQuestion(q);
      });
    });
  }

  function renderFeed(rows) {
    const feed = document.getElementById('aux-feed');
    if (!feed) return;
    // Newest first: on a phone the latest answer should not sit below three
    // screens of earlier ones.
    const list = (rows || []).slice().reverse();
    feed.innerHTML = list.length ? list.map(answerCard).join('')
      : '<div class="aux-empty"><p>Nothing asked yet today.</p>'
        + '<p class="aux-empty-sub">Answers clear when you wake'
        + (AUX.since ? ' — this list starts from ' + esc(fmtTime(AUX.since)) : '')
        + ', and stay in the database.</p></div>';
  }

  function render(today) {
    const root = document.getElementById('screen-auspex');
    const gs = groups();
    if (!AUX.group || !gs.some((g) => g.name === AUX.group)) AUX.group = gs[0].name;
    root.innerHTML =
      '<div class="aux-head">'
      + '<div><h2 class="aux-title">Auspex</h2></div>'
      + '<div class="aux-headr">'
      + (AUX.since ? '<span class="aux-since">since ' + esc(fmtTime(AUX.since)) + '</span>' : '')
      + '<span class="aux-tally" id="aux-tally"></span></div>'
      + '</div>'
      + '<div class="subtabs aux-segs" id="aux-segs">'
      + gs.map((g) => '<button class="subtab aux-seg" data-group="' + esc(g.name) + '">'
        + esc(g.name) + '<span class="aux-seg-n"></span></button>').join('')
      + '</div>'
      + '<div class="aux-pane" id="aux-pane"></div>'
      + '<div class="aux-feed" id="aux-feed"></div>';

    root.querySelectorAll('.aux-seg').forEach((b) => {
      b.classList.toggle('active', b.dataset.group === AUX.group);
      b.addEventListener('click', () => {
        AUX.group = b.dataset.group;
        root.querySelectorAll('.aux-seg').forEach(
          (x) => x.classList.toggle('active', x === b));
        renderPane();
      });
    });
    renderPane();
    renderFeed(today && today.queries);
    refreshTally();
  }

  async function load() {
    const root = document.getElementById('screen-auspex');
    if (!root) return;
    clearTimers();
    if (!AUX.questions) root.innerHTML = '<div class="aux-loading">Loading…</div>';
    try {
      const [qs, today] = await Promise.all([
        AUX.questions ? Promise.resolve({ questions: AUX.questions })
                      : api('/api/auspex/questions'),
        api('/api/auspex/today'),
      ]);
      AUX.questions = qs.questions;
      AUX.order = AUX.questions.map((q) => q.id).concat([EXPLORE_ID]);
      AUX.since = today.since_iso;
      AUX.answered = {};
      (today.queries || []).forEach((r) => { AUX.answered[r.question_id] = r.created_at; });
      render(today);
    } catch (err) {
      root.innerHTML = '<div class="aux-loading">Could not load Auspex: '
        + esc(err.message) + '</div>';
    }
  }

  window.auspexOnShow = load;
})();

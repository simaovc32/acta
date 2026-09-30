// Acta — Finance: Spending (the ledger of every purchase).
// Classic script, loaded after finance.js. finance.js owns the tab, the PIN and the
// token and hands this file a small context; this file only draws the Spending
// sub-tab and talks to /api/finance/spending and /api/finance/transaction.
//
// A purchase is a proposal until I confirm the real balance
// (pending → applied). Nothing here touches a balance, and deleting only hides
// a row (it can be restored), the same rule as the rest of Finance.
(function () {
  'use strict';

  // ── icons: flat single-stroke glyphs ─────────────────────────────────────
  const PATHS = {
    coffee: 'M5 9.5h11v4.5a4.5 4.5 0 0 1-4.5 4.5h-2A4.5 4.5 0 0 1 5 14zM16 10.5h1.5a2 2 0 0 1 0 4H16M8.5 4.5v2M12 4.5v2',
    fork: 'M7 3v7M4.5 3v4.5a2.5 2.5 0 0 0 5 0V3M7 12v9M17.5 3c-2.2 1.6-3 4.6-2.5 8h4.5V3h-2zM17.5 11v10',
    cart: 'M3.5 5h2.2l1.8 9.5h9.3l1.7-6.5H6.4M9.5 19h.01M16.5 19h.01',
    bus: 'M6.5 5h11a1.5 1.5 0 0 1 1.5 1.5v9a2 2 0 0 1-2 2h-10a2 2 0 0 1-2-2v-9A1.5 1.5 0 0 1 6.5 5zM5 11h14M8.5 14.5h.01M15.5 14.5h.01M8 17.5v2M16 17.5v2',
    bag: 'M5.5 8.5h13l-1 11h-11zM9 8.5V7a3 3 0 0 1 6 0v1.5',
    cross: 'M9.5 4.5h5v5h5v5h-5v5h-5v-5h-5v-5h5z',
    star: 'M12 4.5l2.1 4.5 4.9.6-3.6 3.4.9 4.9-4.3-2.4-4.3 2.4.9-4.9L5 9.6l4.9-.6z',
    receipt: 'M6.5 3.5h11v17l-2.2-1.5-2.2 1.5-2.2-1.5-2.2 1.5-2.2-1.5zM9.5 8.5h5M9.5 12h5',
    dots: 'M6.5 12h.01M12 12h.01M17.5 12h.01',
    search: 'M11 4.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM16 16l4 4',
    x: 'M6 6l12 12M18 6L6 18',
    question: 'M9.5 9.5a2.5 2.5 0 1 1 3.6 2.2c-.7.4-1.1.9-1.1 1.8M12 17h.01',
    chevL: 'M14.5 6 8.5 12l6 6',
    chevR: 'M9.5 6l6 6-6 6',
    arrow: 'M5 12h14M13 6l6 6-6 6',
    plus: 'M12 5v14M5 12h14',
  };
  const CAT_ICON = { coffee: 'coffee', eat: 'fork', groc: 'cart', move: 'bus', shop: 'bag',
                     health: 'cross', fun: 'star', bills: 'receipt', other: 'dots' };
  const ico = (n) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + PATHS[n] + '"/></svg>';

  const SOURCE = { manual: 'Manual', whatsapp: 'WhatsApp', sync: 'Synced', import: 'Bank import' };
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                  'September', 'October', 'November', 'December'];
  const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const PAGE = 14, MORE = 20;

  // ── state ────────────────────────────────────────────────────────────────
  const S = { ym: null, cat: 'all', needs: false, q: '', limit: PAGE, data: null, acct: null };
  let ctx = null;      // the context finance.js handed to the latest render()
  let seq = 0;         // a slow response must never overwrite a newer one
  let opener = null;   // what had focus before the sheet opened

  // ── small helpers ────────────────────────────────────────────────────────
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const eur = (n, d) => {
    d = d === undefined ? 2 : d;
    return '€' + Math.abs(n).toLocaleString('pt-PT', { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  const signed = (n) => (n < 0 ? '−' : '+') + eur(n);
  const monthName = (ym) => MONTHS[Number(ym.slice(5)) - 1];
  const dateLabel = (iso) => {
    const d = new Date(iso + 'T12:00:00Z');
    return DOW[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()].slice(0, 3);
  };
  const addDays = (iso, n) => {
    const d = new Date(iso + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  };
  const dayLabel = (iso) => {
    const t = S.data.today;
    return (iso === t ? 'Today · ' : iso === addDays(t, -1) ? 'Yesterday · ' : '') + dateLabel(iso);
  };
  const catName = (k) => {
    const c = (S.data.categories || []).find((x) => x.key === k);
    return c ? c.name : null;
  };
  // Spending is net of refunds; income without a category is not spending.
  const netOf = (rows) => rows.reduce((a, r) => a + (r.amount_eur < 0 || r.category ? -r.amount_eur : 0), 0);
  const isExpense = (r) => r.amount_eur < 0;
  const rowTitle = (r) => r.description || (r.amount_eur < 0 ? 'Expense' : 'Income');

  // Amount fields take a plain number or a running sum ("20+20+10"). Only +,
  // never eval. The Expense / Income switch decides the sign, so a typed "-" is
  // not a valid amount here.
  function parseSum(raw) {
    const parts = String(raw).replace(/,/g, '.').replace(/\s+/g, '').replace(/^\+/, '').split('+');
    const nums = parts.map((p) => (/^\d+(\.\d+)?$|^\.\d+$/.test(p) ? parseFloat(p) : NaN));
    return nums.some(Number.isNaN) ? null : Math.round(nums.reduce((a, b) => a + b, 0) * 100) / 100;
  }

  // ── data ─────────────────────────────────────────────────────────────────
  async function load() {
    const my = ++seq;
    const d = await ctx.authed('/api/finance/spending' + (S.ym ? '?month=' + encodeURIComponent(S.ym) : ''));
    if (my !== seq) return false;
    S.data = d;
    S.ym = d.month;
    if (S.acct == null || !d.accounts.some((a) => a.id === S.acct)) S.acct = d.default_account_id;
    return true;
  }
  function fail(e) {
    if (e && e.status === 401) { closeSheet(true); ctx.onAuthError(); return; }
    toast((e && e.message) || 'Something went wrong');
  }
  async function reload() {
    try { if (await load()) paint(); } catch (e) { fail(e); }
  }

  // ── pieces of the page ───────────────────────────────────────────────────
  function filtered() {
    let rows = S.data.rows;
    if (S.needs) rows = rows.filter((r) => !r.category && isExpense(r));
    else if (S.cat !== 'all') rows = rows.filter((r) => r.category === S.cat);
    const q = S.q.trim().toLowerCase();
    if (q) {
      rows = rows.filter((r) => ((r.description || '') + ' ' + (catName(r.category) || '') + ' ' +
        (r.account || '') + ' ' + (r.note || '')).toLowerCase().includes(q));
    }
    return rows;
  }

  function iconHtml(r) {
    // money in with no category is income, not something waiting to be categorised
    const k = r.category, inc = !k && r.amount_eur > 0;
    return '<span class="sx-ico' + (k || inc ? '' : ' none') + (r.status === 'pending' ? ' pend' : '') +
      '" aria-hidden="true">' + ico(k ? CAT_ICON[k] || 'dots' : (inc ? 'plus' : 'question')) + '</span>';
  }

  function statsHtml() {
    const d = S.data, top = d.by_category.filter((c) => c.category !== 'none')[0];
    let chip = '';
    if (d.compare) {
      const dl = d.compare.delta;
      const vs = d.compare.same_days ? '1 to ' + d.days_elapsed + ' ' + monthName(d.compare.month).slice(0, 3)
                                     : monthName(d.compare.month);
      chip = '<span class="sx-chip' + (dl > 0 ? ' warn' : '') + '">' + (dl > 0 ? '+' : '−') +
        eur(dl, 0) + ' vs ' + vs + '</span>';
    }
    const cat = top ? catName(top.category) : null;
    const rec = d.recurring || { monthly_out: 0, count: 0 };
    return '<div><span class="sx-label">Spent in ' + monthName(d.month) + '</span>' +
        '<span class="sx-num l">' + eur(d.spent) + '</span>' + chip + '</div>' +
      '<div><span class="sx-label">Daily average</span><span class="sx-num l">' + eur(d.daily_avg) + '</span>' +
        '<span class="sx-label">over ' + d.days_elapsed + (d.days_elapsed === 1 ? ' day' : ' days') + '</span></div>' +
      '<div><span class="sx-label">Biggest category</span><span class="sx-num m">' + esc(cat || '—') + '</span>' +
        '<span class="sx-label">' + (top && d.spent > 0 ? eur(top.amount) + ' · ' +
          Math.round((top.amount / d.spent) * 100) + '% of the month' : '') + '</span></div>' +
      '<div><span class="sx-label">Recurring, not counted</span><span class="sx-num m">' + eur(rec.monthly_out) + '</span>' +
        (rec.count ? '<button class="sx-link" data-go="subs">' + rec.count +
          (rec.count === 1 ? ' subscription ' : ' subscriptions ') + ico('arrow') + '</button>'
                   : '<span class="sx-label">No subscriptions</span>') + '</div>';
  }

  function catsHtml() {
    const d = S.data, list = d.by_category;
    if (!list.length) return '<div class="sx-empty small"><span class="sx-label">Nothing spent in ' +
      monthName(d.month) + ' yet.</span></div>';
    return list.map((c) => {
      const none = c.category === 'none', name = none ? 'Needs a category' : catName(c.category) || c.category;
      const pressed = none ? S.needs : S.cat === c.category;
      return '<button class="sx-cat" data-catf="' + c.category + '" aria-pressed="' + pressed + '">' +
        '<span class="sx-ico sm' + (none ? ' none' : '') + '" aria-hidden="true">' +
          ico(none ? 'question' : CAT_ICON[c.category] || 'dots') + '</span>' +
        '<span class="sx-cn">' + esc(name) + '</span><span class="sx-ca">' + eur(c.amount) + '</span>' +
        '<span class="sx-bar"><i style="width:' + Math.max(3, (c.amount / list[0].amount) * 100).toFixed(1) + '%"></i></span>' +
        '<span class="sx-cp">' + (d.spent > 0 ? Math.round((c.amount / d.spent) * 100) : 0) + '%</span></button>';
    }).join('');
  }

  function chipsHtml() {
    // counts are of what the filter will show, refunds included
    const rows = S.data.rows;
    const cnt = (k) => rows.filter((r) => r.category === k).length;
    return '<button class="sx-chipbtn" data-catf="all" aria-pressed="' + (S.cat === 'all' && !S.needs) + '">All <b>' +
        rows.length + '</b></button>' +
      S.data.categories.map((c) => '<button class="sx-chipbtn" data-catf="' + c.key + '" aria-pressed="' +
        (S.cat === c.key && !S.needs) + '">' + esc(c.name) + (cnt(c.key) ? ' <b>' + cnt(c.key) + '</b>' : '') + '</button>').join('');
  }

  function needsHtml() {
    const n = S.data.needs_category;
    if (!n) return '';
    return '<button class="sx-needs" data-needs aria-pressed="' + S.needs + '">' +
      '<span class="sx-ico sm none" aria-hidden="true">' + ico('question') + '</span>' +
      '<span><b>' + n + (n === 1 ? ' purchase needs' : ' purchases need') + ' a category</b>' +
      '<span class="sx-label">Tap to review them.</span></span>' +
      '<span class="sx-chip warn">' + (S.needs ? 'Showing' : 'Review') + '</span></button>';
  }

  function rowsHtml() {
    const d = S.data, rows = filtered();
    if (!rows.length) {
      return '<li class="sx-empty"><b>Nothing here</b><span class="sx-label">' +
        (d.rows.length ? 'No purchases match this filter in ' + monthName(d.month) + '.'
                       : 'No purchases in ' + monthName(d.month) + ' yet. Tap Add expense to log the first one.') + '</span></li>';
    }
    const shown = rows.slice(0, S.limit);
    let out = '', cur = '';
    shown.forEach((r) => {
      if (r.occurred_on !== cur) {
        cur = r.occurred_on;
        const day = netOf(rows.filter((x) => x.occurred_on === cur));
        out += '<li class="sx-day" role="presentation"><span>' + dayLabel(cur) + '</span><span class="sx-mono">' +
          (day > 0 ? '−' + eur(day) : '') + '</span></li>';
      }
      const inc = r.amount_eur > 0, cn = catName(r.category);
      const sub = cn ? esc(cn) : (inc ? 'Income, not counted' : '<em class="sx-need">Needs a category</em>');
      out += '<li class="sx-row" data-id="' + r.id + '" tabindex="0" role="button" aria-label="Edit ' +
        esc(rowTitle(r)) + ', ' + signed(r.amount_eur) + (r.status === 'pending' ? ', waiting for your next balance confirmation' : '') + '">' +
        iconHtml(r) +
        '<div class="sx-main"><b>' + esc(rowTitle(r)) + '</b><span class="sx-label">' + sub + ' · ' + esc(r.account) +
        (r.in_food ? ' · in Food' : '') + '</span></div>' +
        '<div class="sx-side"><b class="sx-mono' + (inc ? ' pos' : '') + '">' + signed(r.amount_eur) + '</b>' +
        '<span class="sx-label">' + (SOURCE[r.source] || esc(r.source)) + '</span></div></li>';
    });
    if (rows.length > shown.length) {
      out += '<li class="sx-more"><button class="sx-btn" data-more>Show ' + Math.min(MORE, rows.length - shown.length) +
        ' more<span class="dim"> · ' + (rows.length - shown.length) + ' earlier</span></button></li>';
    }
    return out;
  }

  function monthHtml() {
    const d = S.data, i = d.months.indexOf(d.month);
    const prev = i > 0, next = i >= 0 && i < d.months.length - 1;
    return '<button class="sx-ib" data-m="-1" aria-label="Previous month"' + (prev ? '' : ' aria-disabled="true"') + '>' + ico('chevL') + '</button>' +
      '<h2>' + monthName(d.month) + ' ' + d.month.slice(0, 4) + '</h2>' +
      '<button class="sx-ib" data-m="1" aria-label="Next month"' + (next ? '' : ' aria-disabled="true"') + '>' + ico('chevR') + '</button>';
  }

  function shellHtml() {
    const d = S.data, mn = monthName(d.month);
    return '<div class="sx">' +
      '<div class="sx-top"><div class="sx-month" id="sx-month">' + monthHtml() + '</div><span class="sx-sp"></span>' +
        '<button class="sx-btn accent" id="sx-add">' + ico('plus') + '<span>Add expense</span></button></div>' +
      '<section class="sx-stats" id="sx-stats" aria-label="The month at a glance">' + statsHtml() + '</section>' +
      '<div class="sx-grid">' +
        '<article class="sx-card sx-listc"><div class="sx-card-h"><h2>Transactions</h2><span class="sx-meta" id="sx-count"></span></div>' +
          '<div class="sx-filters"><label class="sx-search"><span class="sx-sr">Search purchases</span>' + ico('search') +
            '<input type="search" id="sx-q" placeholder="Search purchases" value="' + esc(S.q) + '" autocomplete="off"></label>' +
            '<div class="sx-chips" id="sx-chips">' + chipsHtml() + '</div></div>' +
          '<div id="sx-needs">' + needsHtml() + '</div>' +
          '<ul class="sx-rows" id="sx-rows" aria-label="Purchases, newest first">' + rowsHtml() + '</ul>' +
          '<div class="sx-card-f"><span class="sx-ico sm pend" aria-hidden="true"></span>' +
            '<span class="sx-label">The dot marks purchases waiting for your next balance confirmation. Deleting one only hides it.</span></div></article>' +
        '<aside class="sx-stack">' +
          '<article class="sx-card"><div class="sx-card-h"><h2>Where it went</h2><span class="sx-meta" id="sx-catmeta">' + mn + '</span></div>' +
            '<div class="sx-cats" id="sx-cats">' + catsHtml() + '</div></article>' +
          '<article class="sx-card"><div class="sx-card-h"><h2>Not counted here</h2><span class="sx-meta" id="sx-nc-meta">' + mn + '</span></div>' +
            '<ul class="sx-nc" id="sx-nc">' + notCountedHtml() + '</ul></article>' +
        '</aside></div>' +
      '<button class="sx-fab" id="sx-fab" aria-label="Add expense">' + ico('plus') + '</button></div>';
  }

  function notCountedHtml() {
    const d = S.data, t = d.transfers || { count: 0, total: 0 };
    return '<li><div><b>Transfers between your accounts</b><span class="sx-label">' +
        (t.count ? t.count + ' this month, ' + eur(t.total, 0) + ' moved' : 'None this month') + '</span></div></li>' +
      '<li><div><b>Recurring subscriptions</b><span class="sx-label">Shown on Subs, so they are never counted twice</span></div>' +
        '<button class="sx-link" data-go="subs">Open ' + ico('arrow') + '</button></li>';
  }

  // Repaint the parts of the page that depend on the data; the search box and the
  // sheet are left alone so typing and focus survive a refresh.
  function paint() {
    const set = (sel, html) => { const el = $(sel); if (el) el.innerHTML = html; };
    set('#sx-month', monthHtml());
    set('#sx-stats', statsHtml());
    set('#sx-cats', catsHtml());
    set('#sx-chips', chipsHtml());
    set('#sx-needs', needsHtml());
    set('#sx-rows', rowsHtml());
    set('#sx-nc', notCountedHtml());
    const mn = monthName(S.data.month);
    const m1 = $('#sx-catmeta'), m2 = $('#sx-nc-meta');
    if (m1) m1.textContent = mn;
    if (m2) m2.textContent = mn;
    const c = $('#sx-count');
    if (c) { const n = filtered().length; c.textContent = n + (n === 1 ? ' purchase' : ' purchases'); }
  }

  // ── toast with an optional Undo ──────────────────────────────────────────
  function toast(msg, undo) {
    let t = $('.sx-toast');
    if (!t) {
      t = document.createElement('div');
      t.className = 'sx-toast';
      t.setAttribute('data-accent', 'finance');   // lives on <body>, so it takes the Finance accent itself
      t.setAttribute('role', 'status');
      document.body.appendChild(t);
    }
    t.innerHTML = '<span>' + esc(msg) + '</span>' + (undo ? '<button class="sx-link" id="sx-undo">Undo</button>' : '');
    const b = $('#sx-undo', t);
    if (b) b.addEventListener('click', () => { t.classList.remove('on'); undo(); });
    requestAnimationFrame(() => t.classList.add('on'));
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove('on'), 6000);
  }

  // ── writes (each one reloads, so the page always shows what the server has) ─
  async function send(path, method, body) {
    return ctx.authed(path, { method: method, body: body });
  }
  async function undoAdd(id) {
    try { await send('/api/finance/transaction/' + id, 'DELETE'); await reload(); } catch (e) { fail(e); }
  }
  async function undoEdit(id, prev) {
    try { await send('/api/finance/transaction/' + id, 'PATCH', prev); await reload(); } catch (e) { fail(e); }
  }
  async function undoDelete(id) {
    try { await send('/api/finance/transaction/' + id + '/restore', 'POST'); await reload(); } catch (e) { fail(e); }
  }

  // ── the add / edit sheet ─────────────────────────────────────────────────
  function closeSheet(quiet) {
    const sh = $('#sx-sheet'), sc = $('#sx-scrim');
    if (sh) sh.remove();
    if (sc) sc.remove();
    document.removeEventListener('keydown', sheetKeys);
    if (!quiet && opener && opener.isConnected) { try { opener.focus({ preventScroll: true }); } catch (e) { /* gone */ } }
    opener = null;
  }
  function sheetKeys(e) {
    const sh = $('#sx-sheet');
    if (!sh) return;
    if (e.key === 'Escape') { e.preventDefault(); closeSheet(); return; }
    if (e.key === 'Tab') {
      const f = $$('button, input, [tabindex="0"]', sh).filter((x) => !x.disabled && x.offsetParent !== null);
      if (!f.length) return;
      const a = f[0], z = f[f.length - 1];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    }
  }

  function openSheet(id) {
    const d = S.data;
    const T = id ? d.rows.find((r) => r.id === id) : null;
    if (id && !T) return;
    if (!T && !d.accounts.length) { toast('Add an account on Net worth first'); return; }
    closeSheet(true);
    opener = document.activeElement;

    const accounts = d.accounts.slice();
    // An account the picker no longer offers (closed, or now valued from holdings)
    // is still shown for a row already on it, so editing never silently moves it.
    if (T && !accounts.some((a) => a.id === T.account_id)) accounts.unshift({ id: T.account_id, name: T.account, kind: '' });
    const today = d.today, yesterday = addDays(today, -1);
    const st = { kind: T && T.amount_eur > 0 ? 'in' : 'out', cat: T ? T.category : null,
                 acct: T ? T.account_id : S.acct, date: T ? T.occurred_on : today, suggested: false };
    const known = d.merchants || [];

    document.body.insertAdjacentHTML('beforeend',
      '<div class="sx-scrim" id="sx-scrim"></div>' +
      '<section class="sx-sheet" id="sx-sheet" data-accent="finance" role="dialog" aria-modal="true" aria-labelledby="sx-sh-t"><div class="sx-handle" aria-hidden="true"></div>' +
        '<header class="sx-sh-h"><h2 id="sx-sh-t">' + (T ? 'Edit purchase' : 'Add expense') + '</h2><span class="sx-sp"></span>' +
          '<div class="sx-seg" role="group" aria-label="Type"><button data-kind="out" aria-pressed="' + (st.kind === 'out') + '">Expense</button>' +
          '<button data-kind="in" aria-pressed="' + (st.kind === 'in') + '">Income</button></div>' +
          '<button class="sx-ib" data-close aria-label="Close">' + ico('x') + '</button></header>' +
        '<div class="sx-sh-b">' +
          '<label class="sx-amount"><span class="sx-lab-row"><span class="sx-label">Amount</span><span class="sx-hint" id="sx-amt-hint">Sums work: 20+20+10</span></span>' +
            '<span class="sx-amt-row"><span class="cur" aria-hidden="true">€</span><input id="sx-amt" inputmode="decimal" autocomplete="off" placeholder="0.00" ' +
            'value="' + (T ? Math.abs(T.amount_eur).toFixed(2) : '') + '" aria-describedby="sx-amt-hint"></span></label>' +
          '<label class="sx-fld"><span class="sx-label">Where</span><input id="sx-merch" autocomplete="off" maxlength="80" placeholder="Merchant, or what it was" value="' + esc(T ? T.description || '' : '') + '"></label>' +
          '<div class="sx-chips sx-recent" id="sx-recent" aria-label="Recent merchants"></div>' +
          '<div class="sx-fld"><span class="sx-label">Category <span class="sx-sugg" id="sx-sugg" aria-live="polite"></span></span>' +
            '<div class="sx-catpick" role="group" aria-label="Category">' + d.categories.map((c) =>
              '<button data-pick="' + c.key + '" aria-pressed="' + (st.cat === c.key) + '">' + ico(CAT_ICON[c.key] || 'dots') + '<span>' + esc(c.name) + '</span></button>').join('') + '</div></div>' +
          '<div class="sx-fld"><span class="sx-label">Paid with</span><div class="sx-acct" role="group" aria-label="Account">' + accounts.map((a) =>
            '<button data-acct="' + a.id + '" aria-pressed="' + (st.acct === a.id) + '" title="' + esc(a.name) + '">' + esc(a.name) + '</button>').join('') + '</div></div>' +
          '<div class="sx-fld"><span class="sx-label">When</span><div class="sx-chips" id="sx-when">' +
            '<button class="sx-chipbtn" data-when="' + today + '" aria-pressed="' + (st.date === today) + '">Today</button>' +
            '<button class="sx-chipbtn" data-when="' + yesterday + '" aria-pressed="' + (st.date === yesterday) + '">Yesterday</button>' +
            '<label class="sx-chipbtn dateb"><span class="sx-sr">Pick a date</span><input type="date" id="sx-date" value="' + st.date + '" max="' + today + '"></label></div></div>' +
          '<label class="sx-fld"><span class="sx-label">Note, optional</span><input id="sx-note" autocomplete="off" maxlength="200" value="' + esc(T ? T.note || '' : '') + '"></label>' +
        '</div>' +
        '<footer class="sx-sh-f">' + (T ? '<button class="sx-btn danger" data-del>Delete</button>' : '') + '<span class="sx-sp"></span>' +
          '<button class="sx-btn" data-close>Cancel</button><button class="sx-btn accent" id="sx-save">' + (T ? 'Save changes' : 'Add expense') + '</button></footer></section>');

    const sh = $('#sx-sheet'), amt = $('#sx-amt', sh), mer = $('#sx-merch', sh), sugg = $('#sx-sugg', sh), hint = $('#sx-amt-hint', sh);
    const paintRecent = () => {
      const q = mer.value.trim().toLowerCase();
      const list = (q ? known.filter((k) => k.name.toLowerCase().includes(q) && k.name.toLowerCase() !== q) : known).slice(0, 6);
      $('#sx-recent', sh).innerHTML = list.length ? '<span class="sx-label">' + (q ? 'Matches' : 'Recent') + '</span>' +
        list.map((k, i) => '<button class="sx-chipbtn" data-merch="' + i + '">' + esc(k.name) + '</button>').join('') : '';
      paintRecent.list = list;
    };
    const setCat = (k, suggested) => {
      st.cat = k; st.suggested = !!suggested;
      sugg.classList.remove('need');
      $$('[data-pick]', sh).forEach((b) => b.setAttribute('aria-pressed', b.dataset.pick === k));
      sugg.textContent = suggested ? 'Suggested from last time' : '';
    };
    // A merchant seen before brings its last category with it, unless one was picked by hand.
    const match = () => {
      const r = known.find((x) => x.name.toLowerCase() === mer.value.trim().toLowerCase());
      if (r && r.category && (!st.cat || st.suggested)) setCat(r.category, true);
    };
    if (T && !T.category && T.amount_eur < 0) { sugg.textContent = 'Pick one'; sugg.classList.add('need'); }
    paintRecent();
    mer.addEventListener('input', () => { paintRecent(); match(); });
    amt.addEventListener('input', () => {
      const v = parseSum(amt.value);
      hint.textContent = v !== null && /\+.+/.test(amt.value.replace(/^\+/, '')) ? '= ' + eur(v) : 'Sums work: 20+20+10';
      hint.classList.toggle('bad', amt.value.trim() !== '' && v === null);
    });

    sh.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.close !== undefined) closeSheet();
      else if (b.dataset.kind) {
        st.kind = b.dataset.kind;
        if (!st.cat) sugg.textContent = st.kind === 'in' ? 'Optional: a category makes it a refund' : '';
        $$('[data-kind]', sh).forEach((x) => x.setAttribute('aria-pressed', x === b));
        $('#sx-save', sh).textContent = T ? 'Save changes' : (st.kind === 'in' ? 'Add income' : 'Add expense');
      }
      else if (b.dataset.pick) setCat(st.cat === b.dataset.pick ? null : b.dataset.pick, false);
      else if (b.dataset.acct) { st.acct = Number(b.dataset.acct); $$('[data-acct]', sh).forEach((x) => x.setAttribute('aria-pressed', x === b)); }
      else if (b.dataset.when) { st.date = b.dataset.when; $('#sx-date', sh).value = st.date; $$('[data-when]', sh).forEach((x) => x.setAttribute('aria-pressed', x === b)); }
      else if (b.dataset.merch !== undefined) { mer.value = paintRecent.list[Number(b.dataset.merch)].name; paintRecent(); match(); mer.focus(); }
      else if (b.dataset.del !== undefined) remove();
    });
    $('#sx-date', sh).addEventListener('change', (e) => {
      st.date = e.target.value || today;
      $$('[data-when]', sh).forEach((x) => x.setAttribute('aria-pressed', x.dataset.when === st.date));
    });

    let busy = false;
    async function remove() {
      if (busy) return;
      busy = true;
      try {
        await send('/api/finance/transaction/' + T.id, 'DELETE');
        closeSheet();
        await reload();
        toast('Removed ' + rowTitle(T) + ' · ' + eur(T.amount_eur), () => undoDelete(T.id));
      } catch (e) { busy = false; fail(e); }
    }
    async function save() {
      if (busy) return;
      const v = parseSum(amt.value);
      if (v === null || v <= 0) {
        amt.focus();
        hint.textContent = 'Enter an amount, for example 4.50';
        hint.classList.add('bad');
        return;
      }
      const amount = st.kind === 'in' ? v : -v;
      const name = mer.value.trim(), note = $('#sx-note', sh).value.trim();
      busy = true;
      $('#sx-save', sh).disabled = true;
      try {
        if (T) {
          const body = {}, prev = {};
          if (amount !== T.amount_eur) { body.amount_eur = amount; prev.amount_eur = T.amount_eur; }
          if (name !== (T.description || '')) { body.description = name; prev.description = T.description || ''; }
          if ((st.cat || '') !== (T.category || '')) { body.category = st.cat || ''; prev.category = T.category || ''; }
          if (st.acct !== T.account_id) { body.account_id = st.acct; prev.account_id = T.account_id; }
          if (st.date !== T.occurred_on) { body.occurred_on = st.date; prev.occurred_on = T.occurred_on; }
          if (note !== (T.note || '')) { body.note = note; prev.note = T.note || ''; }
          if (!Object.keys(body).length) { closeSheet(); return; }
          await send('/api/finance/transaction/' + T.id, 'PATCH', body);
          S.acct = st.acct;
          closeSheet();
          if (!st.date.startsWith(S.ym)) { S.ym = st.date.slice(0, 7); S.cat = 'all'; S.needs = false; }
          await reload();
          toast('Saved ' + (name || rowTitle(T)) + ' · ' + eur(v), () => undoEdit(T.id, prev));
        } else {
          const r = await send('/api/finance/transaction', 'POST', {
            account_id: st.acct, amount_eur: amount, description: name || null, occurred_on: st.date,
            category: st.cat || null, source: 'manual', note: note || null,
          });
          S.acct = st.acct;
          closeSheet();
          if (!st.date.startsWith(S.ym)) { S.ym = st.date.slice(0, 7); S.cat = 'all'; S.needs = false; }
          S.limit = PAGE;
          await reload();
          toast('Added ' + (name || (amount < 0 ? 'Expense' : 'Income')) + ' · ' + eur(v), () => undoAdd(r.id));
        }
      } catch (e) {
        busy = false;
        const b = $('#sx-save', sh);
        if (b) b.disabled = false;
        fail(e);
      }
    }
    $('#sx-save', sh).addEventListener('click', save);
    sh.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'date') { e.preventDefault(); save(); }
    });
    $('#sx-scrim').addEventListener('click', () => closeSheet());
    document.addEventListener('keydown', sheetKeys);
    requestAnimationFrame(() => { sh.classList.add('in'); $('#sx-scrim').classList.add('in'); amt.focus(); });
  }

  // ── the tab ──────────────────────────────────────────────────────────────
  function bind() {
    const root = $('.sx', ctx.screen);
    if (!root) return;
    $('#sx-add').addEventListener('click', () => openSheet(null));
    $('#sx-fab').addEventListener('click', () => openSheet(null));
    root.addEventListener('click', (e) => {
      const m = e.target.closest('[data-m]');
      if (m) {
        if (m.getAttribute('aria-disabled') === 'true') return;
        const i = S.data.months.indexOf(S.data.month) + Number(m.dataset.m);
        if (i < 0 || i >= S.data.months.length) return;
        S.ym = S.data.months[i]; S.cat = 'all'; S.needs = false; S.limit = PAGE;
        reload();
        return;
      }
      const g = e.target.closest('[data-go]');
      if (g) { const b = ctx.screen.querySelector('.fin-subtab[data-tab="' + g.dataset.go + '"]'); if (b) b.click(); return; }
      const f = e.target.closest('[data-catf]');
      if (f) {
        const k = f.dataset.catf;
        S.limit = PAGE;
        if (k === 'none') { S.needs = !S.needs; if (S.needs) S.cat = 'all'; }
        else { S.needs = false; S.cat = S.cat === k && k !== 'all' ? 'all' : k; }
        paint();
        return;
      }
      if (e.target.closest('[data-more]')) { S.limit += MORE; paint(); return; }
      const n = e.target.closest('[data-needs]');
      if (n) { S.limit = PAGE; S.needs = !S.needs; if (S.needs) S.cat = 'all'; paint(); return; }
      const r = e.target.closest('.sx-row');
      if (r) openSheet(Number(r.dataset.id));
    });
    root.addEventListener('keydown', (e) => {
      const r = e.target.closest && e.target.closest('.sx-row');
      if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openSheet(Number(r.dataset.id)); }
    });
    $('#sx-q').addEventListener('input', (e) => { S.q = e.target.value; S.limit = PAGE; paint(); });
  }

  // finance.js calls this when the Spending sub-tab is chosen. A rejected promise
  // (401 included) goes back to its safeRender(), which puts the lock screen up.
  async function render(c) {
    ctx = c;
    closeSheet(true);
    await load();
    ctx.screen.innerHTML = ctx.subtabsHtml('spending') + shellHtml();
    ctx.wireSubtabs();
    paint();
    bind();
  }

  // ── Net worth: the latest few purchases ──────────────────────────────────
  // finance.js leaves an empty slot in Net worth's right column and hands it over once the tab has
  // drawn, so this never delays the balances. Any failure just removes the slot: Net worth is
  // complete without it. Tapping a row or the button opens the Spending tab.
  const RECENT = 3;
  const prevMonth = (ym) => {
    const y = Number(ym.slice(0, 4)), m = Number(ym.slice(5, 7));
    return m === 1 ? (y - 1) + '-12' : y + '-' + String(m - 1).padStart(2, '0');
  };
  const recentDay = (iso, today) =>
    iso === today ? 'Today' : iso === addDays(today, -1) ? 'Yesterday' : dateLabel(iso);
  const newest = (a, b) => (b.occurred_on > a.occurred_on ? 1 : b.occurred_on < a.occurred_on ? -1 : b.id - a.id);

  function recentHtml(rows, cats, today) {
    const nameOf = (k) => { const c = cats.find((x) => x.key === k); return c ? c.name : null; };
    const list = rows.map((r) => {
      const cn = nameOf(r.category);
      const sub = (cn ? esc(cn) : '<em class="sx-need">Needs a category</em>') + (r.in_food ? ' · in Food' : '');
      return '<li class="sx-row" data-sx-open tabindex="0" role="button" aria-label="' + esc(rowTitle(r)) + ', ' +
        signed(r.amount_eur) + '. Open Spending">' + iconHtml(r) +
        '<div class="sx-main"><b>' + esc(rowTitle(r)) + '</b><span class="sx-label">' + sub + '</span></div>' +
        '<div class="sx-side"><b class="sx-mono">' + signed(r.amount_eur) + '</b><span class="sx-label">' +
        recentDay(r.occurred_on, today) + '</span></div></li>';
    }).join('');
    return '<div class="fin-card sx-recent">' +
      '<div class="fin-card-head"><span class="t">Recent spending</span><span class="m">' +
        (rows.length > 1 ? 'Last ' + rows.length : rows.length ? 'Latest' : '') + '</span></div>' +
      (rows.length ? '<ul class="sx-rows">' + list + '</ul>'
                   : '<div class="sx-rc-empty">No purchases logged yet. Add one in Spending.</div>') +
      '<div class="fin-card-actions"><button class="fin-btn" data-sx-open>All spending →</button></div></div>';
  }

  async function loadRecent(c) {
    const slot = c && c.slot;
    if (!slot) return;
    try {
      const d = await c.authed('/api/finance/spending');
      let rows = (d.rows || []).filter(isExpense);
      if (rows.length < RECENT) {            // early in a month: reach back into the last one
        try {
          const p = await c.authed('/api/finance/spending?month=' + prevMonth(d.month));
          rows = rows.concat((p.rows || []).filter(isExpense));
        } catch (e) { /* the current month alone is fine */ }
      }
      rows = rows.sort(newest).slice(0, RECENT);
      if (!slot.isConnected) return;         // the tab changed while this was loading
      const box = document.createElement('div');
      box.innerHTML = recentHtml(rows, d.categories || [], d.today);
      const card = box.firstElementChild;
      slot.replaceWith(card);
      const go = (e) => { e.preventDefault(); c.openSpending(); };
      $$('[data-sx-open]', card).forEach((el) => {
        el.addEventListener('click', go);
        if (el.tagName === 'LI') el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') go(e); });
      });
    } catch (e) {
      if (slot.isConnected) slot.remove();
    }
  }

  window.ActaFinanceSpend = { render: render, loadRecent: loadRecent };
})();

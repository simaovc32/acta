// Acta — Finance tab logic (phase 1: auth, accounts, balances, net worth).
// Classic script, loaded after the main inline script on all breakpoints.
// Wrapped in an IIFE; exposes only window.financeOnShow. Owns #finance-box
// (main-screen widget), #passcode-modal (its unlock dialog) and everything
// rendered into #screen-finance. All data persists via /api/finance/* —
// balances are the only source of truth; nothing here ever infers a balance.
(function () {
  'use strict';

  const TOKEN_KEY = 'acta_finance_token';
  const FIN = { token: sessionStorage.getItem(TOKEN_KEY) || null };

  // ── API helpers ──────────────────────────────────────────────────────────
  async function finApi(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    const res = await fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const err = new Error((data && data.detail) || ('HTTP ' + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }
  function finAuthed(path, opts) {
    opts = opts || {};
    const headers = Object.assign({}, opts.headers || {});
    if (FIN.token) headers['X-Finance-Token'] = FIN.token;
    return finApi(path, Object.assign({}, opts, { headers: headers }));
  }
  function forgetToken() {
    FIN.token = null;
    sessionStorage.removeItem(TOKEN_KEY);
  }
  function fmtEur(n) {
    if (n === null || n === undefined) return '—';
    const r = Math.round(n);
    return (r < 0 ? '-€' : '€') + Math.abs(r).toLocaleString('pt-PT');
  }
  // fmtEur rounds to whole euros, which is right for headline figures and wrong
  // anywhere the exact number is the point — a €10.44 subscription shows as €10,
  // and a "Confirm €93" button that stores €92.56 is simply lying about what it
  // is about to write.
  function fmtEurExact(n) {
    if (n === null || n === undefined) return '—';
    return (n < 0 ? '-€' : '€') + Math.abs(n).toLocaleString('pt-PT',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function todayISO() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
           '-' + String(d.getDate()).padStart(2, '0');
  }

  // Amount fields accept a plain number OR a running sum: "20+20+10" when
  // counting cash, "2400-50" when you know the delta but not the total.
  // Parsed by splitting on +/- and summing — never eval(), and only + and -
  // are allowed so there is no operator-precedence ambiguity.
  // Returns null when the expression isn't valid.
  function parseAmount(raw) {
    const s = String(raw).replace(/\s+/g, '').replace(/,/g, '.');
    if (s === '') return null;
    if (!/^-?\d*\.?\d+([+-]\d*\.?\d+)*$/.test(s)) return null;
    const parts = s.match(/[+-]?\d*\.?\d+/g);
    if (!parts) return null;
    let total = 0;
    for (const p of parts) {
      const v = parseFloat(p);
      if (isNaN(v)) return null;
      total += v;
    }
    return total;
  }

  // Enter submits; Escape cancels. Saves a deliberate tap on the button every
  // time, which is most of the friction on a phone keyboard.
  function wireFormKeys(container, onSubmit, onCancel) {
    container.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); onSubmit(); }
        else if (e.key === 'Escape' && onCancel) { e.preventDefault(); onCancel(); }
      });
    });
  }
  function autofocus(sel) {
    const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
    if (!el) return;
    // rAF so focus lands after the browser finishes laying the new nodes out
    requestAnimationFrame(() => { try { el.focus(); el.select && el.select(); } catch (e) {} });
  }

  // ── shared PIN pad (drives both the main-card modal and the inline tab
  // lock; both reuse .passcode-input/.passcode-keypad for identical look) ──
  //
  // Registry of live pads, so a single document-level keydown listener can
  // route typed digits to whichever pad is currently on screen.
  const pinPads = [];

  function wirePinPad(cellsWrap, keypadWrap, errorEl, onComplete, isVisible) {
    const cells = Array.from(cellsWrap.querySelectorAll('.cell'));
    let entered = '';
    let submitting = false;

    function render() {
      cells.forEach((c, i) => {
        c.textContent = entered[i] ? '•' : '';
        c.classList.toggle('filled', !!entered[i]);
      });
    }
    function clearError() {
      cellsWrap.classList.remove('error');
      if (errorEl) errorEl.textContent = '';
    }
    function reset() {
      entered = '';
      submitting = false;
      render();
      clearError();
    }
    function showError(msg) {
      if (errorEl) errorEl.textContent = msg || '';
      cellsWrap.classList.add('error', 'shake');
      setTimeout(() => {
        cellsWrap.classList.remove('shake');
        entered = '';
        submitting = false;
        render();
        // The red cells relax, but the MESSAGE stays until the next keypress —
        // a wrong-passcode notice that vanishes on a ~1s timer is easy to miss
        // entirely, especially when typing rather than tapping.
        cellsWrap.classList.remove('error');
      }, 380);
    }
    function push(d) {
      if (submitting || entered.length >= 4) return;
      if (entered.length === 0) clearError();   // first key of a new attempt
      entered += d;
      render();
      if (entered.length === 4) {
        const pin = entered;
        submitting = true;   // ignore further input until the attempt resolves
        setTimeout(() => onComplete(pin, { showError: showError, reset: reset }), 140);
      }
    }
    function back() {
      if (submitting) return;
      entered = entered.slice(0, -1);
      render();
    }

    const api = {
      cellsWrap: cellsWrap,
      push: push, back: back, reset: reset,
      visible: isVisible || (() => cellsWrap.isConnected),
    };

    // Click handling is delegated to the keypad container and attached only
    // ONCE per container, with the current pad stored on the node. The card
    // modal's keypad is static markup reused on every open, so re-attaching
    // per-button listeners stacked one live closure per previous open — a
    // single 4-digit entry then fired one unlock attempt per open, burning
    // the 5-attempt lockout after ~2 real tries.
    keypadWrap._pad = api;
    if (keypadWrap.dataset.padWired !== '1') {
      keypadWrap.dataset.padWired = '1';
      keypadWrap.addEventListener('click', e => {
        const btn = e.target.closest('button');
        if (!btn || !keypadWrap.contains(btn)) return;
        const pad = keypadWrap._pad;
        if (!pad) return;
        const k = btn.dataset.k;
        if (k === 'clear') pad.reset();
        else if (k === 'del') pad.back();
        else pad.push(k);
      });
    }

    // Replace any previous registration for this same container, and drop
    // pads whose DOM has since been re-rendered away.
    for (let i = pinPads.length - 1; i >= 0; i--) {
      if (pinPads[i].cellsWrap === cellsWrap || !pinPads[i].cellsWrap.isConnected) {
        pinPads.splice(i, 1);
      }
    }
    pinPads.push(api);

    render();
    return api;
  }

  // Type the PIN instead of tapping it. One listener for the whole module —
  // it dispatches to whichever registered pad reports itself visible, so the
  // modal and the tab lock never both consume the same keystroke.
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    const pad = pinPads.find(p => p.cellsWrap.isConnected && p.visible());
    if (!pad) return;
    if (/^[0-9]$/.test(e.key)) { e.preventDefault(); pad.push(e.key); }
    else if (e.key === 'Backspace') { e.preventDefault(); pad.back(); }
    else if (e.key === 'Escape') { pad.reset(); }
  });

  // Bootstrap on first use (no PIN yet) or verify on every use thereafter —
  // one 4-digit entry either way, the API distinguishes by /finance/status.
  async function attemptPin(pin, ctrl, onSuccess) {
    let status;
    try {
      status = await finApi('/api/finance/status');
    } catch (e) {
      ctrl.showError('Server unreachable');
      return;
    }
    try {
      if (!status.pin_set) {
        await finApi('/api/finance/setup-pin', { method: 'POST', body: { pin: pin } });
      }
      const r = await finApi('/api/finance/unlock', { method: 'POST', body: { pin: pin } });
      FIN.token = r.token;
      sessionStorage.setItem(TOKEN_KEY, r.token);
      onSuccess();
    } catch (e) {
      ctrl.showError(e.message || 'Wrong passcode');
    }
  }

  // ── main-card widget (#finance-box) ─────────────────────────────────────
  const financeBox  = document.getElementById('finance-box');
  const financeMeta = document.getElementById('finance-meta');
  const modal        = document.getElementById('passcode-modal');
  const modalTitle    = modal ? modal.querySelector('.title') : null;

  async function refreshCardWidget() {
    if (!financeBox) return;
    if (!FIN.token) {
      financeBox.classList.add('locked');
      if (financeMeta) financeMeta.textContent = 'Private';
      return;
    }
    try {
      const nw = await finAuthed('/api/finance/networth');
      const numEl = financeBox.querySelector('.net-worth');
      if (numEl) {
        numEl.textContent = (nw.total_eur || nw.accounts.length)
          ? fmtEur(nw.total_eur)
          : 'Set up →';
      }
      financeBox.classList.remove('locked');
      if (financeMeta) financeMeta.textContent = 'Visible';
    } catch (e) {
      if (e.status === 401) {
        forgetToken();
        financeBox.classList.add('locked');
        if (financeMeta) financeMeta.textContent = 'Private';
      }
    }
  }

  function openCardModal() {
    if (!modal) return;
    const cellsWrap = document.getElementById('passcode-input');
    const keypadWrap = document.getElementById('passcode-keypad');
    const errorEl = document.getElementById('passcode-error');
    cellsWrap.classList.remove('error');
    if (errorEl) errorEl.textContent = '';
    finApi('/api/finance/status').then(s => {
      if (modalTitle) modalTitle.textContent = s.pin_set ? 'Enter passcode' : 'Set a passcode';
    });
    const pad = wirePinPad(cellsWrap, keypadWrap, errorEl, (pin, ctrl) => {
      attemptPin(pin, ctrl, () => {
        closeCardModal();
        refreshCardWidget();
      });
    }, () => modal.classList.contains('show'));
    pad.reset();
    modal._pad = pad;
    modal.classList.add('show');
  }
  function closeCardModal() {
    if (modal) modal.classList.remove('show');
  }

  if (financeBox) {
    const revealBtn = document.getElementById('finance-reveal-btn');
    const hideBtn   = document.getElementById('finance-hide');
    if (revealBtn) revealBtn.addEventListener('click', () => {
      if (FIN.token) { refreshCardWidget(); return; }   // still-valid session: just show it
      openCardModal();
    });
    if (hideBtn) hideBtn.addEventListener('click', () => {
      financeBox.classList.add('locked');               // visual re-blur only; token/session kept
      if (financeMeta) financeMeta.textContent = 'Private';
    });
    const closeBtn = document.getElementById('passcode-close');
    if (closeBtn) closeBtn.addEventListener('click', closeCardModal);
    if (modal) modal.addEventListener('click', e => { if (e.target === modal) closeCardModal(); });
    document.addEventListener('keydown', e => {
      if (!modal || !modal.classList.contains('show')) return;
      if (e.key === 'Escape') closeCardModal();
    });
    refreshCardWidget();
  }

  // ── finance tab (#screen-finance) ───────────────────────────────────────
  const screen = document.getElementById('screen-finance');

  function subtabsHtml(current) {
    function tab(id, label) {
      return '<button class="fin-subtab' + (current === id ? ' active' : '') +
        '" data-tab="' + id + '">' + label + '</button>';
    }
    return '<div class="fin-subtabs">' +
      tab('networth', 'Net worth') + tab('spending', 'Spending') + tab('invest', 'Investments') +
      tab('subs', 'Subs') + tab('wishlist', 'Wish list') +
      '</div>';
  }
  // Wire the subtab buttons after any render that includes subtabsHtml().
  // Locked/loading views don't call this (nothing to switch to yet).
  // Function declarations (renderNetWorthTab/renderSubsTab/renderWishlistTab)
  // are hoisted within this IIFE, so referencing them here before their
  // definitions appear further down the file is safe.
  function wireSubtabs() {
    screen.querySelectorAll('.fin-subtab[data-tab]').forEach(b => {
      b.addEventListener('click', () => {
        if (b.classList.contains('active')) return;
        const fn = b.dataset.tab === 'subs' ? renderSubsTab :
                   b.dataset.tab === 'wishlist' ? renderWishlistTab :
                   b.dataset.tab === 'invest' ? renderInvestmentsTab :
                   b.dataset.tab === 'spending' ? renderSpendingTab : renderNetWorthTab;
        safeRender(fn);
      });
    });
  }
  async function safeRender(fn) {
    try {
      await fn();
    } catch (e) {
      if (e.status === 401) { forgetToken(); renderTabLock(); }
      else { screen.innerHTML = subtabsHtml('networth') + '<div class="fin-empty">Could not load finance data.</div>'; wireSubtabs(); }
    }
  }
  function keypadButtonsHtml() {
    return ['1','2','3','4','5','6','7','8','9'].map(k =>
      '<button type="button" data-k="' + k + '">' + k + '</button>').join('') +
      '<button type="button" class="alt" data-k="clear">Clear</button>' +
      '<button type="button" data-k="0">0</button>' +
      '<button type="button" class="alt" data-k="del">Del</button>';
  }

  async function renderTabLock() {
    if (!screen) return;
    let status = { pin_set: true };
    try { status = await finApi('/api/finance/status'); } catch (e) { /* offline; assume set */ }
    screen.innerHTML =
      subtabsHtml(null) +
      '<div class="fin-lockwrap">' +
        '<div class="fin-lock-sub">Secure</div>' +
        '<div class="fin-lock-title">' + (status.pin_set ? 'Enter passcode' : 'Set a passcode') + '</div>' +
        '<div class="passcode-input" id="fin-tab-cells">' +
          '<div class="cell"></div><div class="cell"></div><div class="cell"></div><div class="cell"></div>' +
        '</div>' +
        '<div class="passcode-keypad" id="fin-tab-keys">' + keypadButtonsHtml() + '</div>' +
        '<div class="fin-lock-error" id="fin-tab-error"></div>' +
        '<div class="fin-hint" style="text-align:center">Type it or tap · ⌫ to correct</div>' +
      '</div>';
    const cellsWrap  = document.getElementById('fin-tab-cells');
    const keypadWrap = document.getElementById('fin-tab-keys');
    const errorEl    = document.getElementById('fin-tab-error');
    wirePinPad(cellsWrap, keypadWrap, errorEl, (pin, ctrl) => {
      attemptPin(pin, ctrl, () => {
        refreshCardWidget();
        window.financeOnShow();
      });
    // Only claims keystrokes while the finance tab is actually on screen and
    // the main-card modal isn't covering it.
    }, () => !screen.classList.contains('hidden') &&
             !(modal && modal.classList.contains('show')));
  }

  // ---- net worth trend chart: actual solid, projected-from-subs dashed
  // continuation in the same hue (identity via dash pattern + legend, not a
  // second colour — a projection isn't a different series, it's the same
  // measure extended). ----
  function renderChartSvg(history, projection) {
    if (!history || history.length < 2) {
      return '<div class="fin-chart-empty">Add another snapshot to see a trend</div>';
    }
    const proj = (projection && projection.length > 1) ? projection : null;
    const W = 300, H = 116, PAD = 6;
    const allVals = history.map(h => h.total_eur).concat(proj ? proj.map(p => p.total_eur) : []);
    const min = Math.min.apply(null, allVals), max = Math.max.apply(null, allVals);
    const span = (max - min) || 1;

    // x-scale spans from the first actual date to the last projected date (or
    // last actual date if there's no projection), by elapsed days — not by
    // point index, since projection points land on irregular sub dates.
    const t0 = new Date(history[0].as_of).getTime();
    const tEnd = new Date((proj ? proj[proj.length - 1] : history[history.length - 1]).as_of).getTime();
    const span_t = (tEnd - t0) || 1;
    function xOf(as_of) { return PAD + ((new Date(as_of).getTime() - t0) / span_t) * (W - 2 * PAD); }
    function yOf(v) { return PAD + (1 - (v - min) / span) * (H - 2 * PAD); }

    const pts = history.map(h => [xOf(h.as_of), yOf(h.total_eur)]);
    const path = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const last = pts[pts.length - 1];

    let projPath = '';
    if (proj) {
      // Start the dashed run at the last actual point, otherwise the projection
      // floats detached from the line it continues.
      const ppts = [last].concat(proj.map(p => [xOf(p.as_of), yOf(p.total_eur)]));
      projPath = '<path d="' + ppts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ') +
        '" fill="none" stroke="var(--accent)" stroke-width="2" stroke-dasharray="4 4" opacity="0.55" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
    }

    // Area under the actual series, so the band reads as a filled trend.
    const areaPath = path + ' L' + last[0].toFixed(1) + ',' + (H - PAD) + ' L' + pts[0][0].toFixed(1) + ',' + (H - PAD) + ' Z';

    return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="width:100%;height:116px;display:block">' +
      '<defs><linearGradient id="fin-nw-fill" x1="0" x2="0" y1="0" y2="1">' +
        '<stop offset="0%" stop-color="var(--accent)" stop-opacity="0.26"/>' +
        '<stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>' +
      '</linearGradient></defs>' +
      '<line x1="0" y1="' + (H * 0.2) + '" x2="' + W + '" y2="' + (H * 0.2) + '" stroke="rgba(255,255,255,0.05)"/>' +
      '<line x1="0" y1="' + (H * 0.5) + '" x2="' + W + '" y2="' + (H * 0.5) + '" stroke="rgba(255,255,255,0.05)"/>' +
      '<line x1="0" y1="' + (H * 0.8) + '" x2="' + W + '" y2="' + (H * 0.8) + '" stroke="rgba(255,255,255,0.05)"/>' +
      '<path d="' + areaPath + '" fill="url(#fin-nw-fill)"/>' +
      projPath +
      '<path d="' + path + '" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="3.5" fill="var(--accent)" stroke="var(--bg-2)" stroke-width="2" vector-effect="non-scaling-stroke"/>' +
      '</svg>' +
      (proj ? '<div class="fin-legend"><span><i class="fin-swatch"></i>Actual</span>' +
        '<span><i class="fin-swatch dash"></i>Projected from subs</span></div>' : '');
  }


  function kindLabel(kind) {
    const m = { bank: 'Bank', cash: 'Cash', stocks: 'Stocks', crypto: 'Crypto', other: 'Other' };
    return m[kind] || kind;
  }

  // ── Investments ───────────────────────────────────────────────────────────
  // What sits in each holding, for the accounts whose balance is derived from
  // holdings rather than entered. Read-only: quantities are edited elsewhere.
  const INV_TOP = 5;              // donut segments before the tail folds to Other
  let invAcct = 'all';            // 'all' | account_id, survives re-renders

  // Six segments is the readable ceiling for part-to-whole. With 23 holdings
  // where the top four are ~79%, the tail would be unlabellable slivers — so it
  // becomes one grey "Other" and the full list below carries the detail.
  function invSegments(lines) {
    const sorted = lines.filter(l => l.value !== null)
                        .sort((a, b) => b.value - a.value);
    const head = sorted.slice(0, INV_TOP);
    const tail = sorted.slice(INV_TOP);
    const segs = head.map((l, i) => ({
      cls: 'inv-seg-' + (i + 1), label: l.symbol, value: l.value, n: 1,
    }));
    if (tail.length) {
      segs.push({ cls: 'inv-seg-o',
                  label: 'Other (' + tail.length + ')',
                  value: tail.reduce((s, l) => s + l.value, 0), n: tail.length });
    }
    return segs;
  }

  // Arcs on one circle, each inset by a 2px-equivalent gap so adjacent fills
  // never touch — a stroke butting straight onto its neighbour reads as one mark.
  function donutSvg(segs, total) {
    const R = 68, C = 2 * Math.PI * R, GAP = 3.2;
    let at = 0;
    const arcs = segs.map((s, i) => {
      const frac = total ? s.value / total : 0;
      const len = Math.max(0, frac * C - GAP);
      const el = '<circle class="inv-arc ' + s.cls + '" data-seg="' + i + '"' +
        ' cx="86" cy="86" r="' + R + '" stroke-dasharray="' + len.toFixed(2) +
        ' ' + (C - len).toFixed(2) + '" stroke-dashoffset="' + (-at).toFixed(2) + '"></circle>';
      at += frac * C;
      return el;
    }).join('');
    return '<svg viewBox="0 0 172 172" role="img" aria-label="Allocation by holding">' +
           arcs + '</svg>';
  }

  // Cash sits below the allocation, not inside it: parking money is not a choice
  // among instruments, so it must not become a slice of the chart. But it IS
  // part of what the account is worth, so the account total is invested + cash.
  function cashCardHtml(shown, invested) {
    const cash = shown.reduce((s, a) => s + (a.cash_eur || 0), 0);
    const anySet = shown.some(a => a.cash_set);
    const editable = shown.length === 1;      // one account -> one figure to edit
    const total = invested + cash;
    const pct = total ? (cash / total) * 100 : 0;
    // Money transferred in lands here, not on the balance: it has arrived at the
    // broker but bought nothing, and the balance stays derived from holdings.
    // Pre-filled with the expected figure so confirming is one tap, editable so
    // it is still you vouching for the number rather than the app deciding.
    const pend = editable ? (shown[0].cash_pending || []) : [];
    const cashExp = editable ? shown[0].cash_expected_eur : null;
    const hasPend = pend.length > 0 && cashExp !== null && cashExp !== undefined;
    const pendList = hasPend
      ? '<div class="fin-exp-note">' + fmtEurExact(cash) + ' plus:</div>' +
        '<div class="fin-exp-list">' + pend.map(t =>
          '<span class="tx"><b>' + (t.amount_eur < 0 ? '−' : '+') +
          fmtEurExact(Math.abs(t.amount_eur)) + '</b> ' +
          escapeHtml(t.description || 'transfer') + ' · ' + t.occurred_on +
          '<i class="tx-x" data-tx="' + t.id + '" title="Remove">×</i></span>').join('') +
        '</div>'
      : '';
    return '<div class="fin-card inv-cash">' +
      '<div class="fin-card-head"><span class="t">Cash</span>' +
        '<span class="m">' + (hasPend ? 'confirm transfer'
                              : (anySet ? 'uninvested' : 'not set')) + '</span></div>' +
      pendList +
      '<div class="inv-cash-body">' +
        '<div class="inv-cash-fig"><span class="v">' + fmtEur(cash) + '</span>' +
          '<span class="k">' + pct.toFixed(1) + '% of the account</span></div>' +
        (editable
          ? '<div class="inv-cash-edit">' +
              '<input type="number" step="0.01" min="0" id="inv-cash-in"' +
              ' value="' + (hasPend ? cashExp : (cash || 0)) + '" aria-label="Cash in EUR" />' +
              '<button class="fin-btn solid" id="inv-cash-save">' +
                (hasPend ? 'Confirm' : 'Save') + '</button>' +
            '</div>'
          : '<div class="inv-note">Pick a single account to edit its cash.</div>') +
      '</div>' +
      '<div class="inv-cash-split">' +
        '<span><b>' + fmtEur(invested) + '</b> invested</span>' +
        '<span><b>' + fmtEur(cash) + '</b> cash</span>' +
        '<span class="tot"><b>' + fmtEur(total) + '</b> account total</span>' +
      '</div>' +
      (anySet ? '' : '<div class="inv-note inv-cash-hint">Until you set this, the ' +
        'balance is holdings only — money sitting in the account uninvested would ' +
        'not show in net worth.</div>') +
      '<div class="modal-status" id="inv-cash-status"></div>' +
      '</div>';
  }

  function wireCashCard(shown) {
    const inp = document.getElementById('inv-cash-in');
    const btn = document.getElementById('inv-cash-save');
    if (!inp || !btn || shown.length !== 1) return;
    const stat = document.getElementById('inv-cash-status');
    async function save() {
      const v = parseAmount(inp.value);
      if (v === null || v < 0) { stat.textContent = 'Enter an amount in euros.'; return; }
      btn.disabled = true;
      stat.textContent = 'Saving…';
      try {
        await finAuthed('/api/finance/holdings/' + shown[0].account_id + '/cash',
                        { method: 'POST', body: { amount_eur: v } });
        stat.textContent = 'Saved.';
        setTimeout(() => safeRender(renderInvestmentsTab), 600);
      } catch (e) {
        stat.textContent = e.message || 'Could not save.';
        btn.disabled = false;
      }
    }
    btn.addEventListener('click', save);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
    // Drop a transfer leg that shouldn't have been proposed. Only removes this
    // side of it — the other account's leg is dismissed from its own card, so
    // you are never silently editing an account you aren't looking at.
    document.querySelectorAll('.inv-cash .tx-x').forEach(x =>
      x.addEventListener('click', async ev => {
        ev.stopPropagation();
        try {
          await finAuthed('/api/finance/transaction/' + x.dataset.tx, { method: 'DELETE' });
          safeRender(renderInvestmentsTab);
        } catch (e) { alert(e.message || 'Could not remove'); }
      }));
  }

  // What the broker says vs what we derived. Only meaningful per account, so
  // like the cash card it asks you to pick one rather than summing two brokers
  // into a gap that belongs to neither.
  function reconcileCardHtml(shown) {
    const editable = shown.length === 1;
    const a = editable ? shown[0] : null;
    const chk = a && a.broker_check;
    const hist = (a && a.broker_history) || [];

    let body;
    if (!editable) {
      body = '<div class="inv-note">Pick a single account to reconcile it.</div>';
    } else if (!chk) {
      body = '<div class="inv-note">No check yet. Open ' + escapeHtml(a.name) +
             ', read the total it shows you, and record it here — the gap against ' +
             'our derived figure is what tells you the prices are still trustworthy.</div>';
    } else {
      // Green under 0.5%: cross-listed proxies and intraday timing alone can
      // move the derived figure a few tenths, so a small gap is the expected
      // state, not a problem to chase.
      const g = chk.gap_eur, p = chk.gap_pct;
      const cls = g === null ? '' : (Math.abs(p) < 0.5 ? ' ok' : (Math.abs(p) < 2 ? ' warn' : ' bad'));
      body =
        '<div class="inv-rec-fig">' +
          '<span class="v' + cls + '">' + (g === null ? '—' : (g > 0 ? '+' : '') + fmtEurExact(g)) + '</span>' +
          '<span class="k">' + (p === null ? 'not derived that day'
              : (p > 0 ? '+' : '') + p.toFixed(2) + '% vs broker · ' +
                (chk.days_ago === 0 ? 'today' : chk.days_ago + 'd ago')) + '</span>' +
        '</div>' +
        '<div class="inv-cash-split">' +
          '<span><b>' + fmtEurExact(chk.reported_eur) + '</b> broker</span>' +
          '<span><b>' + (chk.derived_eur === null ? '—' : fmtEurExact(chk.derived_eur)) + '</b> derived</span>' +
        '</div>';
    }

    const histRows = hist.length > 1
      ? '<div class="inv-rec-hist">' + hist.map(h =>
          '<span><b>' + h.as_of.slice(5) + '</b> ' +
          (h.gap_eur === null ? '—' : (h.gap_eur > 0 ? '+' : '') + fmtEurExact(h.gap_eur)) +
          '</span>').join('') + '</div>'
      : '';

    return '<div class="fin-card inv-rec">' +
      '<div class="fin-card-head"><span class="t">Broker check</span>' +
        '<span class="m">' + (chk ? escapeHtml(chk.as_of) : 'never') + '</span></div>' +
      body + histRows +
      (editable
        ? '<div class="inv-cash-edit">' +
            '<input type="text" inputmode="decimal" id="inv-rec-in" ' +
            'placeholder="Broker total" aria-label="Broker reported total" />' +
            '<button class="fin-btn solid" id="inv-rec-save">Record</button>' +
          '</div>' +
          '<div class="inv-note">Recorded only — it never overwrites the derived ' +
          'balance, so the gap stays visible instead of being papered over.</div>'
        : '') +
      '<div class="modal-status" id="inv-rec-status"></div>' +
      '</div>';
  }

  function wireReconcileCard(shown) {
    const inp = document.getElementById('inv-rec-in');
    const btn = document.getElementById('inv-rec-save');
    if (!inp || !btn || shown.length !== 1) return;
    const stat = document.getElementById('inv-rec-status');
    async function save() {
      const v = parseAmount(inp.value);
      if (v === null || v < 0) { stat.textContent = 'Enter the total in euros.'; return; }
      btn.disabled = true; stat.textContent = 'Saving…';
      try {
        await finAuthed('/api/finance/holdings/' + shown[0].account_id + '/reconcile',
                        { method: 'POST', body: { reported_eur: v } });
        stat.textContent = 'Recorded.';
        setTimeout(() => safeRender(renderInvestmentsTab), 600);
      } catch (e) {
        stat.textContent = e.message || 'Could not save.';
        btn.disabled = false;
      }
    }
    btn.addEventListener('click', save);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
  }

  // Add a holding, or edit/sell one. `existing` null means add.
  //
  // Symbol is locked when editing: it is half the key the row is stored under,
  // so changing it would create a second position rather than rename this one.
  // Selling out is a deactivation, so the price history and cost basis survive.
  function openHoldingForm(accountId, existing, row) {
    if (row && row.nextElementSibling && row.nextElementSibling.classList.contains('fin-inline')) {
      row.nextElementSibling.remove();
      return;                                    // tap again to close
    }
    screen.querySelectorAll('.fin-inline').forEach(n => n.remove());
    const e = existing || {};
    const html =
      '<div class="net-label">' + (existing ? 'EDIT HOLDING' : 'ADD HOLDING') + '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" id="inv-f-sym" placeholder="Ticker — e.g. MSFT.US" value="' +
          escapeHtml(e.symbol || '') + '"' + (existing ? ' disabled' : '') + '>' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" inputmode="decimal" id="inv-f-qty" placeholder="Quantity" value="' +
          (e.quantity !== undefined ? e.quantity : '') + '">' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" id="inv-f-name" placeholder="Name (optional)" value="' +
          escapeHtml(e.name || '') + '">' +
      '</div>' +
      (existing ? '' :
      '<div class="fin-inline-row">' +
        '<input type="text" id="inv-f-feed" placeholder="Feed ticker (optional)">' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" id="inv-f-isin" placeholder="ISIN — for ETFs (optional)">' +
      '</div>' +
      '<div class="fin-hint">ETFs price by ISIN; everything else by ticker. Leave the ' +
        'feed ticker blank unless the price source needs a different one.</div>') +
      '<div class="fin-inline-actions">' +
        '<button class="primary" id="inv-f-save">' + (existing ? 'Save' : 'Add') + '</button>' +
        (existing ? '<button class="danger" id="inv-f-sell">Sold out</button>' : '') +
        '<button id="inv-f-cancel">Cancel</button>' +
      '</div>' +
      '<div class="modal-status" id="inv-f-status"></div>';

    const panel = document.createElement('div');
    panel.className = 'fin-inline';
    panel.innerHTML = html;
    if (row) row.insertAdjacentElement('afterend', panel);
    else document.querySelector('.inv-rows').insertAdjacentElement('afterend', panel);
    autofocus(existing ? '#inv-f-qty' : '#inv-f-sym');

    const stat = document.getElementById('inv-f-status');
    async function save() {
      const sym = existing ? e.symbol : document.getElementById('inv-f-sym').value.trim();
      const qty = parseAmount(document.getElementById('inv-f-qty').value);
      if (!sym) { stat.textContent = 'Enter a ticker.'; return; }
      if (qty === null || qty < 0) { stat.textContent = 'Enter a quantity.'; return; }
      const body = { account_id: accountId, symbol: sym, quantity: qty,
                     name: document.getElementById('inv-f-name').value.trim() || null };
      if (!existing) {
        body.feed_symbol = document.getElementById('inv-f-feed').value.trim() || null;
        body.isin = document.getElementById('inv-f-isin').value.trim() || null;
      }
      stat.textContent = 'Saving…';
      try {
        const r = await finAuthed('/api/finance/holdings', { method: 'POST', body: body });
        // A brand-new holding usually has no price until the next refresh; the
        // account keeps its previous balance meanwhile, which is worth saying
        // out loud rather than leaving the total looking wrong.
        stat.textContent = r.warning ? 'Saved — ' + r.warning : 'Saved.';
        setTimeout(() => { refreshCardWidget(); safeRender(renderInvestmentsTab); },
                   r.warning ? 1800 : 500);
      } catch (err) { stat.textContent = err.message || 'Could not save.'; }
    }
    wireFormKeys(panel, save, () => panel.remove());
    document.getElementById('inv-f-save').addEventListener('click', save);
    document.getElementById('inv-f-cancel').addEventListener('click', () => panel.remove());
    const sell = document.getElementById('inv-f-sell');
    if (sell) sell.addEventListener('click', async () => {
      if (!confirm('Remove ' + e.symbol + ' from this account?')) return;
      stat.textContent = 'Removing…';
      try {
        const r = await finAuthed('/api/finance/holdings/' + e.id, { method: 'DELETE' });
        stat.textContent = r.warning ? 'Removed — ' + r.warning : 'Removed.';
        setTimeout(() => { refreshCardWidget(); safeRender(renderInvestmentsTab); },
                   r.warning ? 1800 : 500);
      } catch (err) { stat.textContent = err.message || 'Could not remove.'; }
    });
  }

  function srcBadge(src) {
    if (src === 'manual') return '<span class="inv-src mine">yours</span>';
    if (src === 'tavily') return '<span class="inv-src web">~web</span>';
    if (!src) return '';
    return '<span class="inv-src">' + escapeHtml(src) + '</span>';
  }

  // ── watchlist (companies watched, not held) ─────────────────────────────
  // Deliberately NOT PIN-gated — /api/portfolio/watchlist carries no money
  // data, so it uses plain finApi() rather than finAuthed(). That's also
  // what lets Summary's !portfolio watch/unwatch write to it unattended.
  function watchlistCardHtml(items) {
    const rows = items.length
      ? items.map(i =>
          '<div class="wl-row" data-symbol="' + escapeHtml(i.symbol) + '">' +
            '<div><div class="sym">' + escapeHtml(i.symbol) + '</div>' +
            (i.name ? '<div class="nm">' + escapeHtml(i.name) + '</div>' : '') + '</div>' +
            '<i class="wl-x" title="Remove">×</i>' +
          '</div>').join('')
      : '<div class="fin-empty" style="padding:14px 0;">Nothing watched yet.</div>';
    return '<div class="fin-card inv-watchlist">' +
      '<div class="fin-card-head"><span class="t">Watching</span>' +
        '<span class="m">' + items.length + '</span></div>' +
      '<div class="inv-rows">' + rows + '</div>' +
      '<div class="fin-card-actions wl-add">' +
        '<input type="text" id="wl-symbol" placeholder="Symbol (e.g. RIVN)" style="flex:1 1 110px;">' +
        '<input type="text" id="wl-name" placeholder="Name (optional)" style="flex:2 1 170px;">' +
        '<button class="fin-btn solid" id="wl-add-btn" style="width:auto;">+ Watch</button>' +
      '</div>' +
      '<div class="modal-status" id="wl-status"></div>' +
    '</div>';
  }

  function wireWatchlistCard() {
    const addBtn = document.getElementById('wl-add-btn');
    if (!addBtn) return;
    const stat = document.getElementById('wl-status');
    async function add() {
      const symbol = document.getElementById('wl-symbol').value.trim();
      const name = document.getElementById('wl-name').value.trim();
      if (!symbol) { stat.textContent = 'Enter a symbol.'; return; }
      addBtn.disabled = true;
      stat.textContent = 'Saving…';
      try {
        await finApi('/api/portfolio/watchlist',
          { method: 'POST', body: { symbol: symbol, name: name || null } });
        safeRender(renderInvestmentsTab);
      } catch (e) {
        stat.textContent = e.message || 'Could not save.';
        addBtn.disabled = false;
      }
    }
    addBtn.addEventListener('click', add);
    document.getElementById('wl-name').addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
    document.querySelectorAll('.wl-x').forEach(x =>
      x.addEventListener('click', async ev => {
        ev.stopPropagation();
        const symbol = ev.target.closest('.wl-row').dataset.symbol;
        try {
          await finApi('/api/portfolio/watchlist/' + encodeURIComponent(symbol), { method: 'DELETE' });
          safeRender(renderInvestmentsTab);
        } catch (e) {
          stat.textContent = e.message || 'Could not remove.';
        }
      }));
  }

  function wireAcctButtons() {
    screen.querySelectorAll('.inv-acct-btn').forEach(b =>
      b.addEventListener('click', () => {
        if (String(invAcct) === b.dataset.acct) return;
        invAcct = b.dataset.acct;
        safeRender(renderInvestmentsTab);
      }));
  }

  async function renderInvestmentsTab() {
    const d = await finAuthed('/api/finance/holdings');
    const accounts = d.accounts || [];
    const derived = accounts.filter(a => a.derived);
    const wl = await finApi('/api/portfolio/watchlist').catch(() => ({ watchlist: [] }));
    const watchlistHtml = watchlistCardHtml(wl.watchlist || []);

    if (!accounts.length) {
      screen.innerHTML = subtabsHtml('invest') +
        '<div class="fin-empty">No investment accounts yet.</div>' + watchlistHtml;
      wireSubtabs();
      wireWatchlistCard();
      return;
    }

    // Every investment account is a valid selection, derived or not — an
    // account with no holdings yet (e.g. a crypto account before its first
    // position) used to be completely invisible here: the API already
    // returned it, nothing in the UI let you click into it and add a first
    // holding.
    if (invAcct !== 'all' && !accounts.some(a => String(a.account_id) === String(invAcct))) {
      invAcct = 'all';
    }

    const btns = (derived.length > 1
        ? [{ id: 'all', name: 'All investments' }]
        : []).concat(accounts.map(a => ({ id: String(a.account_id), name: a.name })))
      .map(o => '<button class="inv-acct-btn' + (String(invAcct) === o.id ? ' on' : '') +
        '" data-acct="' + o.id + '">' + escapeHtml(o.name) + '</button>').join('');

    // An empty account gets its own dedicated state with the same
    // "+ Add holding" affordance a derived account's Holdings card has,
    // instead of falling into the shared "nothing anywhere" message below.
    const selected = invAcct === 'all' ? null
        : accounts.find(a => String(a.account_id) === String(invAcct));

    if (selected && !selected.derived) {
      screen.innerHTML = subtabsHtml('invest') +
        '<div class="inv-accts">' + btns + '</div>' +
        '<div class="fin-empty">No holdings yet in ' + escapeHtml(selected.name) + '.<br>' +
        'It keeps its entered balance until you give it holdings.<br>' +
        '<button class="fin-btn" id="inv-add-btn" style="max-width:220px;margin:14px auto 0;">' +
          '+ Add holding</button></div>' + watchlistHtml;
      wireSubtabs();
      wireWatchlistCard();
      wireAcctButtons();
      const addBtn = document.getElementById('inv-add-btn');
      if (addBtn) addBtn.addEventListener('click', () => openHoldingForm(selected.account_id, null));
      return;
    }

    if (!derived.length) {
      screen.innerHTML = subtabsHtml('invest') +
        '<div class="inv-accts">' + btns + '</div>' +
        '<div class="fin-empty">No holdings yet.<br>' +
        'An investment account keeps its entered balance until you give it holdings.</div>' +
        watchlistHtml;
      wireSubtabs();
      wireWatchlistCard();
      wireAcctButtons();
      return;
    }

    const shown = invAcct === 'all' ? derived
                : derived.filter(a => String(a.account_id) === String(invAcct));

    // Same symbol in two accounts is one position when viewing "all".
    const merged = {};
    shown.forEach(a => (a.lines || []).forEach(l => {
      const k = l.symbol;
      if (!merged[k]) merged[k] = { ...l };
      else { merged[k].quantity += l.quantity; merged[k].value += (l.value || 0); }
    }));
    const lines = Object.values(merged);
    const priced = lines.filter(l => l.value !== null);
    const total = priced.reduce((s, l) => s + l.value, 0);
    priced.forEach(l => { l.share = total ? (l.value / total) * 100 : 0; });
    priced.sort((a, b) => b.value - a.value);

    const unpriced = lines.filter(l => l.value === null);
    const segs = invSegments(lines);

    // Concentration is the fact the chart cannot state. With one position over
    // half the account, the number says more than any geometry.
    const topN = Math.min(4, priced.length);
    const topShare = priced.slice(0, topN).reduce((s, l) => s + l.share, 0);
    const staleDays = Math.max(...shown.map(a => a.stale_days === null ? 0 : a.stale_days));
    const anyStale = shown.some(a => a.stale);
    const asOf = shown.map(a => a.priced_as_of).filter(Boolean).sort().slice(-1)[0] || '—';

    const legend = segs.map((s, i) =>
      '<div class="inv-leg ' + s.cls + '" data-seg="' + i + '">' +
        '<em></em><span class="n">' + escapeHtml(s.label) + '</span>' +
        '<span class="v">' + fmtEur(s.value) + '</span>' +
        '<span class="p">' + (total ? (s.value / total * 100).toFixed(1) : '0.0') + '%</span>' +
      '</div>').join('');

    // The ticker alone is not identification: XDWS and XDWH are one letter apart
    // and hold entirely different sectors. The name gets its own line so it is
    // readable rather than crammed into the mono metadata row.
    // Rows are only editable when a single account is in view. In the merged
    // "all" view a line can be the sum of the same symbol held in two accounts,
    // and it carries just the first one's id — editing it would silently change
    // one account while displaying the total of both.
    const editable = shown.length === 1;
    const rows = priced.map(l =>
      '<div class="inv-row' + (editable ? ' editable' : '') + '"' +
          (editable ? ' data-hid="' + l.id + '"' : '') + '>' +
        '<div><div class="sym">' + escapeHtml(l.symbol) + srcBadge(l.price_source) +
          (editable ? '<span class="pen">✎</span>' : '') + '</div>' +
          (l.name ? '<div class="nm">' + escapeHtml(l.name) + '</div>' : '') +
          '<div class="meta">' + (+l.quantity).toLocaleString('en-US',
            { maximumFractionDigits: 4 }) + ' × ' + fmtEur(l.price) + '</div></div>' +
        '<div class="amt">' + fmtEur(l.value) + '</div>' +
        '<div class="shr">' + l.share.toFixed(1) + '%</div>' +
        '<div class="bar"><i style="width:' + Math.max(0.6, l.share).toFixed(1) + '%"></i></div>' +
      '</div>').join('');

    const unpricedNote = unpriced.length
      ? '<div class="fin-nudge">⏱ ' + unpriced.length + ' holding(s) have no price (' +
        unpriced.map(l => escapeHtml(l.symbol)).join(', ') +
        ') — the account keeps its entered balance until every holding prices.</div>'
      : '';
    const staleNote = anyStale
      ? '<div class="fin-nudge">⏱ Prices are ' + staleDays + ' days old.</div>' : '';

    // Two cards in the house .fin-cols grid, same as the Net worth tab: without
    // it the content spans the whole desktop width and a legend label ends up
    // ~1000px from its own value, which defeats the point of a legend.
    // Holdings is by far the tallest card. With enough rows the left column ends far short of it, and that
    // gap is where Watching goes; with few rows the left column is already the taller one and Watching
    // stays below both.
    const watchLeft = priced.length >= 8;
    screen.innerHTML = subtabsHtml('invest') + staleNote + unpricedNote +
      '<div class="inv-accts">' + btns + '</div>' +
      '<div class="fin-cols">' +
        // Allocation and Cash share the left column so Cash sits directly under
        // the chart it qualifies; without the wrapper the grid puts Cash beside
        // Allocation and pushes Holdings down into the second row.
        '<div class="inv-col">' +
        '<div class="fin-card">' +
          '<div class="fin-card-head"><span class="t">Allocation</span>' +
            '<span class="m">' + escapeHtml(asOf) + '</span></div>' +
          '<div class="inv-hero">' +
            '<div class="inv-donut">' + donutSvg(segs, total) +
              '<div class="inv-donut-mid"><span class="v">' + fmtEur(total) + '</span>' +
              '<span class="k">' + priced.length + ' holdings</span></div>' +
            '</div>' +
            '<div class="inv-legend">' + legend + '</div>' +
          '</div>' +
          '<div class="fin-tiles inv-tiles">' +
            '<div class="fin-tile"><div class="v">' + topShare.toFixed(1) + '%</div>' +
              '<div class="k">top ' + topN + '</div></div>' +
            '<div class="fin-tile"><div class="v">' +
              (priced.length ? priced[0].share.toFixed(1) + '%' : '—') + '</div>' +
              '<div class="k">largest</div></div>' +
            '<div class="fin-tile"><div class="v">' + lines.length + '</div>' +
              '<div class="k">holdings</div></div>' +
          '</div>' +
        '</div>' +
        cashCardHtml(shown, total) +
        // Broker check sits under Cash rather than under Holdings: the holdings
        // list is by far the tallest card, so stacking a third card behind it
        // leaves the left column ending several hundred pixels short.
        reconcileCardHtml(shown) +
        (watchLeft ? watchlistHtml : '') +
        '</div>' +
        '<div class="inv-col">' +
        '<div class="fin-card">' +
          '<div class="fin-card-head"><span class="t">Holdings</span>' +
            '<span class="m">' + (editable ? 'TAP A ROW TO EDIT' : priced.length + ' priced') + '</span></div>' +
          '<div class="inv-rows">' + rows + '</div>' +
          '<div class="fin-card-actions">' +
            (editable ? '<button class="fin-btn" id="inv-add-btn">+ Add holding</button>' : '') +
            '<button class="inv-refresh" id="inv-refresh">Refresh prices</button>' +
            '<span class="inv-note" id="inv-status">Priced ' + escapeHtml(asOf) + '</span>' +
          '</div>' +
        '</div>' +
        '</div>' +
      '</div>' + (watchLeft ? '' : watchlistHtml);

    wireSubtabs();
    wireCashCard(shown);
    wireReconcileCard(shown);
    wireWatchlistCard();
    if (editable) {
      const acctId = shown[0].account_id;
      const addBtn = document.getElementById('inv-add-btn');
      if (addBtn) addBtn.addEventListener('click', () => openHoldingForm(acctId, null));
      screen.querySelectorAll('.inv-row.editable').forEach(row =>
        row.addEventListener('click', () => {
          const l = priced.find(x => String(x.id) === row.dataset.hid);
          if (l) openHoldingForm(acctId, l, row);
        }));
    }
    wireAcctButtons();

    // Hovering either the arc or its legend row highlights the pair, so the
    // link between colour and holding never depends on matching hues by eye.
    function hot(i, on) {
      screen.querySelectorAll('.inv-arc').forEach(a =>
        a.classList.toggle('dim', on && a.dataset.seg !== String(i)));
      screen.querySelectorAll('.inv-leg').forEach(l =>
        l.classList.toggle('hot', on && l.dataset.seg === String(i)));
    }
    screen.querySelectorAll('[data-seg]').forEach(el => {
      el.addEventListener('mouseenter', () => hot(el.dataset.seg, true));
      el.addEventListener('mouseleave', () => hot(el.dataset.seg, false));
    });

    const btn = document.getElementById('inv-refresh');
    const stat = document.getElementById('inv-status');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      stat.textContent = 'Fetching prices…';
      try {
        const r = await finAuthed('/api/finance/prices/refresh', { method: 'POST' });
        const failed = Object.keys(r.failed || {}).length;
        stat.textContent = Object.keys(r.priced || {}).length + ' priced' +
          (failed ? ', ' + failed + ' failed' : '');
        setTimeout(() => safeRender(renderInvestmentsTab), 900);
      } catch (e) {
        stat.textContent = 'Could not refresh.';
        btn.disabled = false;
      }
    });
  }

  async function renderNetWorthTab() {
    const nw = await finAuthed('/api/finance/networth');

    if (!nw.accounts.length) {
      screen.innerHTML = subtabsHtml('networth') +
        '<div class="fin-empty">No accounts yet.<br>Add one to start tracking net worth.</div>' +
        newAccountFormHtml();
      wireSubtabs();
      wireNewAccountForm();
      return;
    }

    const change = nw.change;
    let badge = '<span class="fin-badge flat">No prior snapshot</span>';
    if (change) {
      const cls = change.amount_eur > 0 ? 'up' : (change.amount_eur < 0 ? 'down' : 'flat');
      const arrow = change.amount_eur > 0 ? '↑' : (change.amount_eur < 0 ? '↓' : '·');
      badge = '<span class="fin-badge ' + cls + '">' + arrow + ' ' + fmtEur(Math.abs(change.amount_eur)) +
        ' · ' + change.days + 'd</span>';
    }

    // A balance is only what you last typed. When subs have landed on the
    // account since then, the row says so rather than presenting a stale figure
    // as current — the expected number sits beside it, never replacing it.
    const acctRows = nw.accounts.map(a => {
      const exp = a.expected_eur !== null && a.expected_eur !== undefined
        && a.expected_delta_eur ? a.expected_eur : null;
      return '<div class="fin-acct' + (exp !== null ? ' has-exp' : '') + '" data-id="' + a.id + '">' +
        '<div><div class="nm fin-editable">' + escapeHtml(a.name) + '<span class="pen">✎</span></div>' +
          '<div class="kd">' + kindLabel(a.kind) + '</div></div>' +
        '<div class="amt">' + fmtEur(a.amount_eur) +
          (exp !== null
            ? '<div class="exp">→ ' + fmtEurExact(exp) + ' expected</div>'
            : '') +
        '</div>' +
        '<div class="pct">' + (a.pct === null || a.pct === undefined ? '—' : a.pct + '%') + '</div>' +
      '</div>';
    }).join('');

    // Both tiles carry their window, because they measure different ones:
    // saved/mo is normalised over the ~30-day change window, unaccounted is
    // the gap since the previous snapshot. Unlabelled they read as the same
    // period and quietly mislead.
    const unaccVal = nw.unaccounted_eur;
    const unaccClass = unaccVal !== null && unaccVal < 0 ? ' warn' : '';
    const savedVal = nw.saved_per_month_eur;
    const savedClass = savedVal !== null && savedVal < 0 ? ' warn' : '';
    const savedK = change ? 'saved / mo · ' + change.days + 'd' : 'saved / mo';
    // The window comes from the server, which measures each account over its
    // own snapshot gap. Deriving it here from the combined history would read
    // "1d" every day, because the nightly price feed adds a history point
    // whether or not you touched a bank balance.
    const unaccK = nw.unaccounted_days
      ? 'unaccounted · ' + nw.unaccounted_days + 'd' : 'unaccounted';
    // Market movement is reported separately from `unaccounted` and never
    // folded into it: a price move is not money spent, and mixing the two made
    // the unaccounted tile non-zero every single day once holdings went live,
    // which is how a signal stops being read. Not warn-coloured on a loss —
    // a down day is normal, not something the user did wrong.
    const mktVal = nw.market_move_eur;
    const mktK = 'market · since last price';

    // Snapshotting is the one habit the whole tab depends on; surface staleness
    // rather than silently showing an ageing number as if it were current.
    let nudge = '';
    if (nw.as_of) {
      const days = Math.round((new Date(todayISO()) - new Date(nw.as_of)) / 86400000);
      if (days >= 35) {
        nudge = '<div class="fin-nudge">⏱ Last snapshot was ' + days +
          ' days ago — the numbers below are that old.</div>';
      }
    }

    // Composition by account kind — derived from the accounts already returned,
    // no extra request and no stored value that could drift from the balances.
    const KIND_HUE = {
      bank:   'oklch(0.78 0.12 250)',
      cash:   'oklch(0.78 0.12 165)',
      stocks: 'oklch(0.78 0.12 85)',
      crypto: 'oklch(0.78 0.12 290)',
      other:  'var(--ink-4)',
    };
    const byKind = {};
    nw.accounts.forEach(a => {
      const k = KIND_HUE[a.kind] ? a.kind : 'other';
      byKind[k] = (byKind[k] || 0) + (a.amount_eur || 0);
    });
    const kindTotal = Object.values(byKind).reduce((x, y) => x + y, 0) || 1;
    const kindEntries = Object.entries(byKind).sort((a, b) => b[1] - a[1]);
    const compBar = kindEntries.map(([k, v]) =>
      '<i style="width:' + ((v / kindTotal) * 100).toFixed(1) + '%;background:' + KIND_HUE[k] + '"></i>').join('');
    const compLeg = kindEntries.map(([k, v]) =>
      '<span><em style="background:' + KIND_HUE[k] + '"></em><b>' + kindLabel(k) + '</b> ' +
      Math.round((v / kindTotal) * 100) + '%</span>').join('');

    const snapCount = (nw.history || []).length;
    const asOfNote = nw.as_of
      ? 'Snapshot taken ' + nw.as_of + '.'
      : 'No snapshot yet.';

    screen.innerHTML = subtabsHtml('networth') +
      nudge +
      '<div class="fin-band">' +
        '<div class="fin-band-l">' +
          '<div class="net-label">Net worth</div>' +
          '<div class="fin-hero-num">' + fmtEur(nw.total_eur) + '</div>' +
          '<div class="fin-band-badges">' + badge + '</div>' +
          '<div class="fin-band-note">' + asOfNote + '<br>Dashed line is the subscriptions schedule only.</div>' +
        '</div>' +
        '<div class="fin-band-r">' +
          '<div class="fin-band-head">' +
            '<span class="net-label" style="margin:0">HISTORY &amp; PROJECTION</span>' +
            '<span class="net-label" style="margin:0">' + snapCount + ' SNAPSHOT' + (snapCount === 1 ? '' : 'S') + '</span>' +
          '</div>' +
          renderChartSvg(nw.history, nw.projection) +
        '</div>' +
      '</div>' +
      '<div class="fin-strip">' +
        '<div class="fin-cell"><div class="k">' + savedK + '</div><div class="v' + savedClass + '">' + fmtEur(savedVal) + '</div></div>' +
        '<div class="fin-cell"><div class="k">' + unaccK + '</div><div class="v' + unaccClass + '">' + fmtEur(unaccVal) + '</div></div>' +
        '<div class="fin-cell"><div class="k">' + mktK + '</div><div class="v">' +
          (mktVal === null || mktVal === undefined ? '—' : (mktVal > 0 ? '+' : '') + fmtEur(mktVal)) + '</div></div>' +
        '<div class="fin-cell"><div class="k">subs / mo</div><div class="v">' + fmtEur(nw.subs_monthly_out_eur) + '</div></div>' +
      '</div>' +
      '<div class="fin-cols">' +
        '<div class="fin-card">' +
          '<div class="fin-card-head"><span class="t">Accounts</span><span class="m">Tap a row to edit</span></div>' +
          '<div id="fin-acct-list">' + acctRows + '</div>' +
          '<div class="fin-card-actions">' +
            '<button class="fin-btn" id="fin-add-acct-btn">+ Add account</button>' +
            '<button class="fin-btn" id="fin-add-tx-btn">+ Log transaction</button>' +
            '<button class="fin-btn" id="fin-transfer-btn">⇄ Move money</button>' +
            '<button class="fin-btn solid" id="fin-new-snapshot-btn">+ New snapshot</button>' +
          '</div>' +
        '</div>' +
        '<div class="inv-col">' +
        '<div class="fin-card">' +
          '<div class="fin-card-head"><span class="t">Composition</span><span class="m">By kind</span></div>' +
          '<div class="fin-comp-bar">' + compBar + '</div>' +
          '<div class="fin-comp-leg">' + compLeg + '</div>' +
        '</div>' +
        '<div id="fin-recent-slot"></div>' +
        '</div>' +
      '</div>';

    wireSubtabs();
    document.getElementById('fin-new-snapshot-btn').addEventListener('click', () => renderSnapshotForm(nw));
    document.getElementById('fin-add-acct-btn').addEventListener('click', () => renderAddAccount(nw));
    document.getElementById('fin-add-tx-btn').addEventListener('click', () => renderTxForm(nw));
    document.getElementById('fin-transfer-btn').addEventListener('click', () => renderTransferForm(nw));
    screen.querySelectorAll('.fin-acct').forEach(row => {
      row.addEventListener('click', () => {
        const acct = nw.accounts.find(a => String(a.id) === row.dataset.id);
        if (acct) openAccountInline(row, acct);
      });
    });
    // Recent spending is filled in after the tab has drawn, so it never holds up the balances.
    if (window.ActaFinanceSpend && window.ActaFinanceSpend.loadRecent) {
      window.ActaFinanceSpend.loadRecent({
        slot: document.getElementById('fin-recent-slot'),
        authed: finAuthed,
        openSpending: function () {
          const b = screen.querySelector('.fin-subtab[data-tab="spending"]');
          if (b) b.click();
        },
      });
    }
  }

  // Tapping an account row opens an inline editor: correct just that balance
  // (the common case — one account moved) or rename/remove the account,
  // without walking through the whole snapshot form.
  function openAccountInline(row, acct) {
    if (row.nextElementSibling && row.nextElementSibling.classList.contains('fin-inline')) {
      row.nextElementSibling.remove();
      return;                      // tap again to close
    }
    screen.querySelectorAll('.fin-inline').forEach(n => n.remove());
    // Pre-fill with the EXPECTED figure when subs have landed since the last
    // snapshot: confirming is then one tap, and the subs that explain it are
    // listed so you know what you are agreeing to. It stays editable on
    // purpose — blindly accepting the schedule would be auto-deduction wearing
    // a button, and would make `unaccounted` measure nothing.
    const hasExp = acct.expected_eur !== null && acct.expected_eur !== undefined
                   && !!acct.expected_delta_eur;
    const prefill = hasExp ? acct.expected_eur
                  : (acct.amount !== null && acct.amount !== undefined ? acct.amount : '');
    // Subs and transactions are both listed, but a transaction carries a remove
    // control and a sub does not: a sub is a standing schedule you edit in its
    // own tab, whereas a mistyped transaction should be droppable right where
    // you notice it, before it is confirmed into a balance.
    const subsList = hasExp
      ? '<div class="fin-exp-note">Since ' + acct.as_of + ' — ' +
          fmtEurExact(acct.amount_eur) + ' plus:</div>' +
        '<div class="fin-exp-list">' + (acct.subs_since || []).map(e =>
          '<span><b>' + (e.delta < 0 ? '−' : '+') + fmtEurExact(Math.abs(e.delta)) + '</b> ' +
          escapeHtml(e.name) + ' · ' + e.as_of + '</span>').join('') +
        (acct.tx_since || []).map(t =>
          '<span class="tx"><b>' + (t.delta < 0 ? '−' : '+') + fmtEurExact(Math.abs(t.delta)) + '</b> ' +
          escapeHtml(t.description || 'transaction') + ' · ' + t.as_of +
          '<i class="tx-x" data-tx="' + t.id + '" title="Remove">×</i></span>').join('') +
        '</div>'
      : '';
    const panel = document.createElement('div');
    panel.className = 'fin-inline';
    panel.innerHTML =
      '<div class="net-label">' + (hasExp ? 'CONFIRM BALANCE' : 'BALANCE TODAY') + '</div>' +
      subsList +
      '<div class="fin-inline-row">' +
        '<input type="text" inputmode="decimal" id="fin-il-amount" value="' +
          prefill + '">' +
      '</div>' +
      '<div class="fin-hint">' + (hasExp
        ? 'Check it against your bank before saving — correct it if they disagree.'
        : 'Accepts sums — 20+20+10 or 2400-50') + '</div>' +
      '<div class="fin-inline-actions">' +
        '<button class="primary" id="fin-il-save">' +
          (hasExp ? 'Confirm ' + fmtEurExact(acct.expected_eur) : 'Save balance') + '</button>' +
        '<button id="fin-il-rename">Rename</button>' +
        '<button class="danger" id="fin-il-remove">Remove</button>' +
      '</div>';
    row.insertAdjacentElement('afterend', panel);
    autofocus('#fin-il-amount');

    async function saveBalance() {
      const val = parseAmount(document.getElementById('fin-il-amount').value);
      if (val === null) { alert('Enter a number, or a sum like 20+20+10'); return; }
      try {
        await finAuthed('/api/finance/balance', {
          method: 'POST', body: { balances: { [acct.id]: val } },
        });
        refreshCardWidget();
        safeRender(renderNetWorthTab);
      } catch (e) { alert(e.message || 'Could not save balance'); }
    }
    wireFormKeys(panel, saveBalance, () => panel.remove());
    document.getElementById('fin-il-save').addEventListener('click', saveBalance);

    // Drop a mistyped transaction from the proposal. Stops the click reaching
    // the row underneath, which would toggle this panel shut mid-action.
    panel.querySelectorAll('.tx-x').forEach(x => {
      x.addEventListener('click', async ev => {
        ev.stopPropagation();
        try {
          await finAuthed('/api/finance/transaction/' + x.dataset.tx, { method: 'DELETE' });
          safeRender(renderNetWorthTab);
        } catch (e) { alert(e.message || 'Could not remove transaction'); }
      });
    });

    document.getElementById('fin-il-rename').addEventListener('click', () => {
      panel.innerHTML =
        '<div class="net-label">Rename account</div>' +
        '<div class="fin-inline-row"><input type="text" id="fin-il-name" value="' +
          escapeHtml(acct.name) + '"></div>' +
        '<div class="fin-inline-actions">' +
          '<button class="primary" id="fin-il-name-save">Save</button>' +
          '<button id="fin-il-name-cancel">Cancel</button>' +
        '</div>';
      autofocus('#fin-il-name');
      async function saveName() {
        const name = document.getElementById('fin-il-name').value.trim();
        if (!name) { alert('Name cannot be blank'); return; }
        try {
          await finAuthed('/api/finance/accounts/' + acct.id, { method: 'PATCH', body: { name: name } });
          refreshCardWidget();
          safeRender(renderNetWorthTab);
        } catch (e) { alert(e.message || 'Could not rename'); }
      }
      wireFormKeys(panel, saveName, () => panel.remove());
      document.getElementById('fin-il-name-save').addEventListener('click', saveName);
      document.getElementById('fin-il-name-cancel').addEventListener('click', () => panel.remove());
    });

    document.getElementById('fin-il-remove').addEventListener('click', async () => {
      if (!confirm('Remove "' + acct.name + '"?\n\nIts past balances stay in the database but stop counting toward net worth.')) return;
      try {
        await finAuthed('/api/finance/accounts/' + acct.id, { method: 'PATCH', body: { active: false } });
        refreshCardWidget();
        safeRender(renderNetWorthTab);
      } catch (e) { alert(e.message || 'Could not remove account'); }
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  function newAccountFormHtml() {
    return '<div class="fin-newacct">' +
      '<input type="text" id="fin-new-name" placeholder="Account name (e.g. Everyday bank)">' +
      '<select id="fin-new-kind">' +
        '<option value="bank">Bank</option>' +
        '<option value="cash">Cash</option>' +
        '<option value="stocks">Stocks</option>' +
        '<option value="crypto">Crypto</option>' +
        '<option value="other">Other</option>' +
      '</select>' +
      '<button class="fin-btn solid" id="fin-create-acct-btn">Add account</button>' +
      '</div>';
  }
  function wireNewAccountForm() {
    async function create() {
      const name = document.getElementById('fin-new-name').value.trim();
      const kind = document.getElementById('fin-new-kind').value;
      if (!name) { alert('Enter an account name'); return; }
      try {
        await finAuthed('/api/finance/accounts', { method: 'POST', body: { name: name, kind: kind } });
        window.financeOnShow();
      } catch (e) {
        alert(e.message || 'Could not add account');
      }
    }
    document.getElementById('fin-create-acct-btn').addEventListener('click', create);
    wireFormKeys(document.querySelector('.fin-newacct'), create);
    autofocus('#fin-new-name');
  }
  function renderAddAccount(nw) {
    screen.innerHTML = subtabsHtml('networth') +
      '<div class="net-label">Add account</div>' +
      newAccountFormHtml() +
      '<button class="fin-btn" id="fin-cancel-acct-btn">Cancel</button>';
    wireSubtabs();
    wireNewAccountForm();
    document.getElementById('fin-cancel-acct-btn').addEventListener('click', () => renderNetWorthTab());
  }

  // Log an ad-hoc transaction — a purchase, a transfer, anything the subs
  // schedule can't predict. Deliberately does NOT write a balance: it proposes
  // one, which the account row then offers to confirm. Same posture as subs,
  // and the reason `unaccounted` keeps meaning something.
  function renderTxForm(nw) {
    // Holdings-derived accounts are excluded — their value comes from what they
    // hold, so a spend against one is a sell, not a balance nudge (the API
    // refuses it with a 409; not offering it here avoids the dead end).
    const eligible = (nw.accounts || []).filter(a => !a.derived);
    const opts = eligible.map(a =>
      '<option value="' + a.id + '">' + escapeHtml(a.name) + ' · ' + kindLabel(a.kind) + '</option>').join('');

    screen.innerHTML = subtabsHtml('networth') +
      '<div class="net-label">Log transaction</div>' +
      '<div class="fin-inline-row"><select id="fin-tx-acct">' + opts + '</select></div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" inputmode="decimal" id="fin-tx-amount" placeholder="Amount spent — e.g. 42.50">' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" id="fin-tx-desc" placeholder="What was it? (optional)">' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="date" id="fin-tx-date" value="' + todayISO() + '" max="' + todayISO() + '">' +
      '</div>' +
      '<div class="fin-hint">Spending by default — type a leading + for money coming in. ' +
        'Accepts sums (20+20+10). Nothing changes your balance until you confirm it on the account row.</div>' +
      '<button class="fin-btn solid" id="fin-tx-save">Log it</button>' +
      '<button class="fin-btn" id="fin-tx-cancel">Cancel</button>';

    wireSubtabs();
    autofocus('#fin-tx-amount');
    document.getElementById('fin-tx-cancel').addEventListener('click', () => renderNetWorthTab());

    async function save() {
      const raw = document.getElementById('fin-tx-amount').value.trim();
      const num = parseAmount(raw);
      if (num === null || num === 0) { alert('Enter an amount, e.g. 42.50'); return; }
      // A bare "42.50" means money out — that is what logging a transaction is
      // for nearly every time. An explicit leading + is the only way to mean
      // income, so the common case needs no sign and the rare one is deliberate.
      const signed = /^\s*\+/.test(raw) ? Math.abs(num) : -Math.abs(num);
      try {
        await finAuthed('/api/finance/transaction', {
          method: 'POST',
          body: {
            account_id: Number(document.getElementById('fin-tx-acct').value),
            amount_eur: signed,
            description: document.getElementById('fin-tx-desc').value.trim() || null,
            occurred_on: document.getElementById('fin-tx-date').value || todayISO(),
          },
        });
        renderNetWorthTab();
      } catch (e) { alert(e.message || 'Could not log transaction'); }
    }
    wireFormKeys(screen, save, () => renderNetWorthTab());
    document.getElementById('fin-tx-save').addEventListener('click', save);
  }

  // Move money between two of your own accounts.
  //
  // Net worth does not change, so this is deliberately not a "spend": it writes
  // two equal and opposite pending legs that cancel once both are confirmed.
  // Unlike a transaction, a derived account IS offered here — money moved to a
  // broker is real, it just lands as uninvested cash rather than as a balance.
  function renderTransferForm(nw) {
    const all = nw.accounts || [];
    if (all.length < 2) {
      alert('You need two accounts to move money between.');
      return;
    }
    const opts = sel => all.map(a =>
      '<option value="' + a.id + '"' + (String(sel) === String(a.id) ? ' selected' : '') + '>' +
      escapeHtml(a.name) + ' · ' + kindLabel(a.kind) +
      (a.derived ? ' (cash)' : '') + '</option>').join('');

    screen.innerHTML = subtabsHtml('networth') +
      '<div class="net-label">Move money</div>' +
      '<div class="fin-inline-row"><label class="fin-mini-label">From</label>' +
        '<select id="fin-tr-from">' + opts(all[0].id) + '</select></div>' +
      '<div class="fin-inline-row"><label class="fin-mini-label">To</label>' +
        '<select id="fin-tr-to">' + opts(all[1].id) + '</select></div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" inputmode="decimal" id="fin-tr-amount" placeholder="Amount — e.g. 500">' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="text" id="fin-tr-desc" placeholder="Note (optional)">' +
      '</div>' +
      '<div class="fin-inline-row">' +
        '<input type="date" id="fin-tr-date" value="' + todayISO() + '" max="' + todayISO() + '">' +
      '</div>' +
      '<div class="fin-hint">Net worth doesn\'t change — both sides are proposed and ' +
        'confirmed separately. Into an investment account it arrives as uninvested ' +
        'cash, confirmed on the Investments tab.</div>' +
      '<button class="fin-btn solid" id="fin-tr-save">Move it</button>' +
      '<button class="fin-btn" id="fin-tr-cancel">Cancel</button>' +
      '<div class="modal-status" id="fin-tr-status"></div>';

    wireSubtabs();
    autofocus('#fin-tr-amount');
    document.getElementById('fin-tr-cancel').addEventListener('click', () => renderNetWorthTab());
    const stat = document.getElementById('fin-tr-status');

    async function save() {
      const from = Number(document.getElementById('fin-tr-from').value);
      const to   = Number(document.getElementById('fin-tr-to').value);
      const amt  = parseAmount(document.getElementById('fin-tr-amount').value);
      if (from === to) { stat.textContent = 'Pick two different accounts.'; return; }
      if (amt === null || amt <= 0) { stat.textContent = 'Enter an amount.'; return; }
      try {
        await finAuthed('/api/finance/transfer', {
          method: 'POST',
          body: { from_account_id: from, to_account_id: to, amount_eur: amt,
                  description: document.getElementById('fin-tr-desc').value.trim() || null,
                  occurred_on: document.getElementById('fin-tr-date').value || todayISO() },
        });
        renderNetWorthTab();
      } catch (e) { stat.textContent = e.message || 'Could not move it'; }
    }
    wireFormKeys(screen, save, () => renderNetWorthTab());
    document.getElementById('fin-tr-save').addEventListener('click', save);
  }

  function renderSnapshotForm(nw) {
    // type=text, not number: the inputs accept sums ("20+20+10"), which a
    // number field would reject as invalid and silently blank out.
    const rows = nw.accounts.map(a =>
      '<div class="fin-form-row" data-id="' + a.id + '">' +
        '<div><div class="nm">' + escapeHtml(a.name) + '</div><div class="kd">' + kindLabel(a.kind) + ' · ' + a.currency + '</div></div>' +
        '<input type="text" inputmode="decimal" class="fin-snap-input" value="' +
          (a.amount !== null ? a.amount : '') + '">' +
      '</div>'
    ).join('');

    screen.innerHTML = subtabsHtml('networth') +
      '<div class="net-label">Snapshot</div>' +
      '<div class="fin-inline-row" style="margin-bottom:6px">' +
        '<input type="date" id="fin-snap-date" value="' + todayISO() + '" max="' + todayISO() + '">' +
      '</div>' +
      '<div class="fin-hint" style="margin-bottom:10px">Backdate if you\'re entering this late. Amounts accept sums — 20+20+10 or 2400-50.</div>' +
      '<div>' + rows + '</div>' +
      '<button class="fin-btn solid" id="fin-save-snapshot-btn">Save snapshot</button>' +
      '<button class="fin-btn" id="fin-cancel-snapshot-btn">Cancel</button>';

    wireSubtabs();
    document.getElementById('fin-cancel-snapshot-btn').addEventListener('click', () => renderNetWorthTab());
    autofocus('.fin-snap-input');

    async function save() {
      const balances = {};
      let bad = null;
      screen.querySelectorAll('.fin-form-row').forEach(row => {
        const raw = row.querySelector('.fin-snap-input').value.trim();
        if (raw === '') return;   // blank keeps that account's last known balance
        const num = parseAmount(raw);
        if (num === null) { bad = raw; return; }
        balances[row.dataset.id] = num;
      });
      if (bad !== null) { alert('"' + bad + '" isn\'t a number or a sum like 20+20+10'); return; }
      if (!Object.keys(balances).length) { renderNetWorthTab(); return; }
      const as_of = document.getElementById('fin-snap-date').value || todayISO();
      try {
        await finAuthed('/api/finance/balance', { method: 'POST', body: { as_of: as_of, balances: balances } });
        refreshCardWidget();
        renderNetWorthTab();
      } catch (e) {
        alert(e.message || 'Could not save snapshot');
      }
    }
    wireFormKeys(screen, save, () => renderNetWorthTab());
    document.getElementById('fin-save-snapshot-btn').addEventListener('click', save);
  }

  // ── subs sub-tab ─────────────────────────────────────────────────────────
  function relativeDay(daysUntil) {
    if (daysUntil < 0) return 'Overdue';
    if (daysUntil === 0) return 'Today';
    if (daysUntil === 1) return 'Tomorrow';
    return 'In ' + daysUntil + ' days';
  }
  function dayStripHtml(subs) {
    const byDay = {};   // 1..31 -> {out, in}
    subs.forEach(s => {
      byDay[s.day_of_month] = byDay[s.day_of_month] || { out: false, in: false };
      byDay[s.day_of_month][s.direction] = true;
    });
    let cells = '';
    for (let d = 1; d <= 31; d++) {
      const v = byDay[d];
      const cls = !v ? '' : (v.out && v.in ? 'both' : (v.out ? 'out' : 'in'));
      cells += '<i class="' + cls + '"' + (v ? ' title="day ' + d + '"' : '') + '></i>';
    }
    return '<div class="fin-daystrip">' + cells + '</div>' +
      '<div class="fin-striplegend">' +
        '<span><i class="fin-dotc" style="background:var(--accent)"></i>Out</span>' +
        '<span><i class="fin-dotc" style="background:#0ca30c"></i>In</span>' +
      '</div>';
  }
  function newSubFormHtml(accounts) {
    const acctOptions = '<option value="">— no account —</option>' +
      accounts.map(a => '<option value="' + a.id + '">' + escapeHtml(a.name) + '</option>').join('');
    return '<div class="fin-newacct">' +
      '<input type="text" id="fin-sub-name" placeholder="Name (e.g. Spotify)">' +
      '<input type="text" inputmode="decimal" id="fin-sub-amount" placeholder="Amount">' +
      '<select id="fin-sub-direction"><option value="out">Outgoing</option><option value="in">Incoming</option></select>' +
      '<select id="fin-sub-day">' +
        Array.from({ length: 31 }, (_, i) => i + 1).map(d => '<option value="' + d + '">Day ' + d + '</option>').join('') +
      '</select>' +
      '<select id="fin-sub-account">' + acctOptions + '</select>' +
      '<button class="fin-btn solid" id="fin-create-sub-btn">Add subscription</button>' +
      '</div>';
  }
  function wireNewSubForm() {
    async function create() {
      const name = document.getElementById('fin-sub-name').value.trim();
      const amount = parseAmount(document.getElementById('fin-sub-amount').value);
      const direction = document.getElementById('fin-sub-direction').value;
      const day_of_month = parseInt(document.getElementById('fin-sub-day').value, 10);
      const account_id = document.getElementById('fin-sub-account').value || null;
      if (!name || amount === null || amount <= 0) {
        alert('Enter a name and a positive amount');
        return;
      }
      try {
        await finAuthed('/api/finance/subs', {
          method: 'POST',
          body: { name: name, amount: amount, direction: direction, day_of_month: day_of_month,
                   account_id: account_id ? parseInt(account_id, 10) : null },
        });
        safeRender(renderSubsTab);
      } catch (e) {
        alert(e.message || 'Could not add subscription');
      }
    }
    document.getElementById('fin-create-sub-btn').addEventListener('click', create);
    wireFormKeys(document.querySelector('.fin-newacct'), create);
    autofocus('#fin-sub-name');
  }

  // Tapping a sub row opens an inline editor. Removal is active=false via the
  // same PATCH — the row stays in the DB so past projections aren't rewritten
  // by a delete.
  function openSubInline(row, s, accounts) {
    if (row.nextElementSibling && row.nextElementSibling.classList.contains('fin-inline')) {
      row.nextElementSibling.remove();
      return;
    }
    screen.querySelectorAll('.fin-inline').forEach(n => n.remove());
    const acctOptions = '<option value="">— no account —</option>' +
      accounts.map(a => '<option value="' + a.id + '"' +
        (a.id === s.account_id ? ' selected' : '') + '>' + escapeHtml(a.name) + '</option>').join('');
    const panel = document.createElement('div');
    panel.className = 'fin-inline';
    panel.innerHTML =
      '<div class="net-label">Edit subscription</div>' +
      '<div class="fin-inline-row" style="margin-bottom:8px">' +
        '<input type="text" id="fin-es-name" value="' + escapeHtml(s.name) + '">' +
        '<input type="text" inputmode="decimal" id="fin-es-amount" value="' + s.amount + '" style="max-width:90px">' +
      '</div>' +
      '<div class="fin-inline-row" style="margin-bottom:8px">' +
        '<select id="fin-es-direction">' +
          '<option value="out"' + (s.direction === 'out' ? ' selected' : '') + '>Outgoing</option>' +
          '<option value="in"' + (s.direction === 'in' ? ' selected' : '') + '>Incoming</option>' +
        '</select>' +
        '<select id="fin-es-day">' +
          Array.from({ length: 31 }, (_, i) => i + 1).map(d =>
            '<option value="' + d + '"' + (d === s.day_of_month ? ' selected' : '') + '>Day ' + d + '</option>').join('') +
        '</select>' +
      '</div>' +
      '<div class="fin-inline-row"><select id="fin-es-account">' + acctOptions + '</select></div>' +
      '<div class="fin-inline-actions">' +
        '<button class="primary" id="fin-es-save">Save</button>' +
        '<button class="danger" id="fin-es-remove">Remove</button>' +
      '</div>';
    row.insertAdjacentElement('afterend', panel);
    autofocus('#fin-es-name');

    async function save() {
      const name = document.getElementById('fin-es-name').value.trim();
      const amount = parseAmount(document.getElementById('fin-es-amount').value);
      if (!name || amount === null || amount <= 0) { alert('Enter a name and a positive amount'); return; }
      const acct = document.getElementById('fin-es-account').value;
      try {
        await finAuthed('/api/finance/subs/' + s.id, {
          method: 'PATCH',
          body: {
            name: name, amount: amount,
            direction: document.getElementById('fin-es-direction').value,
            day_of_month: parseInt(document.getElementById('fin-es-day').value, 10),
            account_id: acct ? parseInt(acct, 10) : null,
          },
        });
        safeRender(renderSubsTab);
      } catch (e) { alert(e.message || 'Could not save subscription'); }
    }
    wireFormKeys(panel, save, () => panel.remove());
    document.getElementById('fin-es-save').addEventListener('click', save);
    document.getElementById('fin-es-remove').addEventListener('click', async () => {
      if (!confirm('Remove "' + s.name + '"?\n\nIt stops appearing in reminders and projections.')) return;
      try {
        await finAuthed('/api/finance/subs/' + s.id, { method: 'PATCH', body: { active: false } });
        safeRender(renderSubsTab);
      } catch (e) { alert(e.message || 'Could not remove subscription'); }
    });
  }

  // Annual cost per subscription, ranked.
  //
  // The point of the card is the reframe: €6.99/mo reads as nothing and €84/yr
  // reads as a decision. Ranking by annual cost also puts the one that actually
  // matters at the top, which a list sorted by renewal day never does.
  //
  // Income subs are excluded — this answers "what is this costing me", and a
  // salary line would dwarf every bar and make the chart useless.
  function annualisedCardHtml(outSubs) {
    if (!outSubs.length) return '';
    // Monthly cadence is the whole model today (finance_sub has no cadence
    // field), so annual is x12. If yearly plans are ever added, this is the
    // place that has to learn about them.
    const rows = outSubs
      .map(s => ({ name: s.name, eur: s.amount * 12 }))
      .sort((a, b) => b.eur - a.eur);
    const max = rows[0].eur || 1;
    const total = rows.reduce((s, r) => s + r.eur, 0);

    const bars = rows.map(r =>
      '<div class="sub-ann-row">' +
        '<span class="n">' + escapeHtml(r.name) + '</span>' +
        // Floor of 1.5% so a small line is still a visible mark rather than
        // reading as "nothing" next to a dominant one.
        '<span class="b"><i style="width:' +
          Math.max(1.5, (r.eur / max) * 100).toFixed(1) + '%"></i></span>' +
        '<span class="v">' + fmtEur(r.eur) + '</span>' +
      '</div>').join('');

    // Only stated when actually true, so it stays a finding rather than a
    // caption that's always there and therefore never read.
    const rest = total - rows[0].eur;
    const note = (rows.length >= 3 && rows[0].eur > rest)
      ? escapeHtml(rows[0].name) + ' alone is <b>' + fmtEurExact(rows[0].eur) +
        '</b> a year — more than the other ' + (rows.length - 1) + ' combined.'
      : '<b>' + fmtEurExact(total) + '</b> a year across ' + rows.length +
        ' subscription' + (rows.length === 1 ? '' : 's') + '.';

    return '<div class="fin-card">' +
      '<div class="fin-card-head"><span class="t">Annualised</span>' +
        '<span class="m">What it really costs</span></div>' +
      '<div class="sub-ann">' + bars + '</div>' +
      '<div class="sub-ann-note">' + note + '</div>' +
    '</div>';
  }

  async function renderSubsTab() {
    const [subsData, acctData] = await Promise.all([
      finAuthed('/api/finance/subs'),
      finAuthed('/api/finance/accounts'),
    ]);

    if (!subsData.subs.length) {
      screen.innerHTML = subtabsHtml('subs') +
        '<div class="fin-empty">No subscriptions yet.<br>Add one to see renewal reminders and net-worth projections.</div>' +
        newSubFormHtml(acctData.accounts);
      wireSubtabs();
      wireNewSubForm();
      return;
    }

    const subRows = subsData.subs.map(s => {
      const cls = s.direction === 'in' ? ' income' : '';
      const soon = s.days_until <= 2 ? ' soon' : '';
      const sign = s.direction === 'in' ? '+' : '';
      return '<div class="fin-sub' + cls + '" data-id="' + s.id + '">' +
        '<div class="nm fin-editable">' + escapeHtml(s.name) + '<span class="pen">✎</span></div>' +
        '<div class="amt">' + sign + fmtEur(s.amount) + '</div>' +
        '<div class="when' + soon + '">' + relativeDay(s.days_until) + ' · day ' + s.day_of_month + '</div>' +
        '<div class="acctname">' + (s.account_name ? escapeHtml(s.account_name) : '—') + '</div>' +
        '</div>';
    }).join('');

    const outSubs = subsData.subs.filter(x => x.direction !== 'in');
    const heaviest = outSubs.reduce((m, x) => (!m || x.amount > m.amount) ? x : m, null);
    const nextUp = subsData.subs.reduce((m, x) => (!m || x.days_until < m.days_until) ? x : m, null);

    screen.innerHTML = subtabsHtml('subs') +
      '<div class="fin-band">' +
        '<div class="fin-band-l">' +
          '<div class="net-label">Per month</div>' +
          '<div class="fin-hero-num">' + fmtEur(subsData.monthly_out) + '</div>' +
          '<div class="fin-band-badges">' +
            '<span class="fin-badge flat">' + fmtEur(subsData.annual_out) + ' / year</span>' +
            (subsData.monthly_in ? '<span class="fin-badge up">+' + fmtEur(subsData.monthly_in) + ' in</span>' : '') +
          '</div>' +
          '<div class="fin-band-note">' + subsData.subs.length + ' active. Annual plans are shown at their monthly share.</div>' +
        '</div>' +
        '<div class="fin-band-r">' +
          '<div class="fin-band-head">' +
            '<span class="net-label" style="margin:0">This month · when they land</span>' +
            '<span class="net-label" style="margin:0">' +
              (nextUp ? 'Next up ' + escapeHtml(nextUp.name) + ' · day ' + nextUp.day_of_month : '') +
            '</span>' +
          '</div>' +
          dayStripHtml(subsData.subs) +
          '<div class="fin-strip inner">' +
            '<div class="fin-cell"><div class="k">heaviest</div><div class="v">' +
              (heaviest ? fmtEur(heaviest.amount) : '—') + '</div></div>' +
            '<div class="fin-cell"><div class="k">next renewal</div><div class="v">' +
              (nextUp ? relativeDay(nextUp.days_until) : '—') + '</div></div>' +
            '<div class="fin-cell"><div class="k">active</div><div class="v">' + subsData.subs.length + '</div></div>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="fin-cols">' +
        '<div class="fin-card">' +
          '<div class="fin-card-head"><span class="t">All subscriptions</span><span class="m">Tap a row to edit</span></div>' +
          '<div>' + subRows + '</div>' +
          '<div class="fin-card-actions">' +
            '<button class="fin-btn solid" id="fin-add-sub-btn">+ Add subscription</button>' +
          '</div>' +
        '</div>' +
        annualisedCardHtml(outSubs) +
      '</div>';

    wireSubtabs();
    screen.querySelectorAll('.fin-sub').forEach(row => {
      row.addEventListener('click', () => {
        const s = subsData.subs.find(x => String(x.id) === row.dataset.id);
        if (s) openSubInline(row, s, acctData.accounts);
      });
    });
    document.getElementById('fin-add-sub-btn').addEventListener('click', () => {
      screen.innerHTML = subtabsHtml('subs') +
        '<div class="net-label">Add subscription</div>' +
        newSubFormHtml(acctData.accounts) +
        '<button class="fin-btn" id="fin-cancel-sub-btn">Cancel</button>';
      wireSubtabs();
      wireNewSubForm();
      document.getElementById('fin-cancel-sub-btn').addEventListener('click', () => safeRender(renderSubsTab));
    });
  }

  // ── wish list sub-tab ────────────────────────────────────────────────────
  // Only 4 fields are ever typed: name, price, want, need. Everything under
  // the divider on each card is computed server-side; the verdict line below
  // is a plain, visible if/then rule over that trend — never a hidden score,
  // and never fitted to how the ratings "should" behave (same standing rule
  // as readiness/pain: predict/surface objective facts, don't calibrate to
  // self-report).
  function wishScaleOptions(selected) {
    return Array.from({ length: 10 }, (_, i) => i + 1)
      .map(n => '<option value="' + n + '"' + (n === selected ? ' selected' : '') + '>' + n + '</option>')
      .join('');
  }
  function newWishFormHtml() {
    return '<div class="fin-newacct">' +
      '<input type="text" id="fin-wish-name" placeholder="Item (e.g. Teclado mecânico)">' +
      '<input type="text" inputmode="decimal" id="fin-wish-price" placeholder="Price (€)">' +
      '<input type="url" id="fin-wish-link" placeholder="Link (optional)">' +
      '<div class="fin-rate-row"><span class="net-label" style="margin:0">Want</span>' +
        '<select id="fin-wish-want">' + wishScaleOptions(5) + '</select>' +
        '<span class="net-label" style="margin:0">Need</span>' +
        '<select id="fin-wish-need">' + wishScaleOptions(5) + '</select></div>' +
      '<button class="fin-btn solid" id="fin-create-wish-btn">Add to wish list</button>' +
      '</div>';
  }
  function wireNewWishForm() {
    async function create() {
      const name = document.getElementById('fin-wish-name').value.trim();
      const price = parseAmount(document.getElementById('fin-wish-price').value);
      const link = document.getElementById('fin-wish-link').value.trim();
      const want = parseInt(document.getElementById('fin-wish-want').value, 10);
      const need = parseInt(document.getElementById('fin-wish-need').value, 10);
      if (!name || price === null || price <= 0) {
        alert('Enter a name and a positive price');
        return;
      }
      try {
        await finAuthed('/api/finance/wishlist', {
          method: 'POST',
          body: { name: name, price: price, want: want, need: need, link: link || null },
        });
        safeRender(renderWishlistTab);
      } catch (e) {
        alert(e.message || 'Could not add item');
      }
    }
    document.getElementById('fin-create-wish-btn').addEventListener('click', create);
    wireFormKeys(document.querySelector('.fin-newacct'), create);
    autofocus('#fin-wish-name');
  }

  // Transparent rule, not a score: needs >=2 ratings to say anything about a
  // trend at all; otherwise there's nothing yet to compare against.
  // Want over time. The API exposes first and current want per item, so each
  // line has two points — enough to show direction, which is the whole job.
  function wantTrendSvg(items) {
    if (!items.length) return '';
    const W = 300, H = 96, PAD = 10, LBL = 16;
    const yOf = v => PAD + (1 - v / 10) * (H - 2 * PAD);
    // No <text> inside this SVG: preserveAspectRatio="none" stretches a 300-unit
    // viewBox across ~1300px and would smear the glyphs. Scale labels are HTML.
    let g = '';
    [0, 5, 10].forEach(v => {
      g += '<line x1="' + LBL + '" y1="' + yOf(v) + '" x2="' + W + '" y2="' + yOf(v) +
           '" stroke="rgba(255,255,255,0.05)"/>';
    });
    const HUES = ['oklch(0.78 0.12 250)', 'oklch(0.78 0.12 85)', 'oklch(0.78 0.12 290)',
                  'oklch(0.78 0.12 165)', 'oklch(0.78 0.12 25)'];
    items.forEach((it, i) => {
      const c = HUES[i % HUES.length];
      const x0 = LBL + 12, x1 = W - PAD;
      const first = (it.rating_count >= 2) ? it.want_first : it.want;
      const d = 'M' + x0 + ',' + yOf(first).toFixed(1) + ' L' + x1 + ',' + yOf(it.want).toFixed(1);
      g += '<path d="' + d + '" fill="none" stroke="' + c + '" stroke-width="2" ' +
           'stroke-linecap="round" vector-effect="non-scaling-stroke"/>' +
           '<circle cx="' + x1 + '" cy="' + yOf(it.want).toFixed(1) + '" r="3.5" fill="' + c + '"/>';
    });
    const leg = items.map((it, i) =>
      '<span><em style="background:' + HUES[i % HUES.length] + '"></em>' + escapeHtml(it.name) +
      ' · ' + ((it.rating_count >= 2 && it.want_first !== it.want)
        ? it.want_first + ' → ' + it.want : 'want ' + it.want) + '</span>').join('');
    return '<div class="fin-want-wrap">' +
             '<div class="fin-want-scale"><span>10</span><span>5</span><span>0</span></div>' +
             '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" ' +
             'style="width:100%;height:96px;display:block">' + g + '</svg>' +
           '</div>' +
           '<div class="fin-comp-leg" style="padding:12px 0 0">' + leg + '</div>';
  }

  function wishVerdict(item) {
    if (item.rating_count < 2) {
      return { cls: 'wait', text: 'Re-rate later to see the want trend' };
    }
    const delta = item.want - item.want_first;
    if (delta <= -2) {
      return { cls: 'hold', text: '⚠ Wanting it less over time — hold' };
    }
    if (item.affordable) {
      return { cls: 'ok', text: '✓ Steady want, affordable — could buy' };
    }
    return { cls: 'wait', text: 'Still wanted, not affordable yet' };
  }

  function wishStatus(item) {
    if (item.rating_count < 2) return { cls: 'mute', text: 'Too soon' };
    if (item.want - item.want_first <= -2) return { cls: 'warn', text: 'Hold' };
    if (item.affordable) return { cls: 'ok', text: 'Could buy' };
    return { cls: 'mute', text: 'Saving' };
  }

  function wishCardHtml(item) {
    const trend = item.rating_count >= 2
      ? item.want_first + ' → ' + item.want + (item.want < item.want_first ? ' <span class="down">↓</span>' : '')
      : String(item.want);
    const verdict = wishVerdict(item);
    // Only http(s) links are turned into anchors — an unchecked href would
    // accept javascript:/data: from the stored value.
    const safeLink = (item.link && /^https?:\/\//i.test(item.link)) ? item.link : null;
    const nameHtml = safeLink
      ? '<a href="' + escapeHtml(safeLink) + '" target="_blank" rel="noopener noreferrer" ' +
        'style="color:inherit">' + escapeHtml(item.name) + ' ↗</a>'
      : escapeHtml(item.name);
    const st = wishStatus(item);
    return '<div class="fin-wish" data-id="' + item.id + '">' +
      '<div class="fin-wish-head">' +
        '<div class="fin-wish-nm">' + nameHtml + '</div>' +
        '<span class="fin-chip ' + st.cls + '">' + st.text + '</span>' +
      '</div>' +
      '<div class="fin-wish-top">' +
        '<div class="fin-wish-price">' + fmtEur(item.price) + '</div>' +
        '<div class="fin-wish-pct">' +
          (item.pct_of_networth === null ? '' : item.pct_of_networth + '% of net worth') + '</div>' +
      '</div>' +
      '<div class="fin-meters">' +
        '<div class="fin-meter"><div class="mk"><span>Want</span><span>' + item.want + '/10</span></div>' +
          '<div class="fin-bar"><i style="width:' + (item.want * 10) + '%"></i></div></div>' +
        '<div class="fin-meter"><div class="mk"><span>Need</span><span>' + item.need + '/10</span></div>' +
          '<div class="fin-bar"><i style="width:' + (item.need * 10) + '%"></i></div></div>' +
      '</div>' +
      '<div class="fin-computed">' +
        '<div class="fin-crow2"><span>Of net worth</span><b>' + (item.pct_of_networth === null ? '—' : item.pct_of_networth + '%') + '</b></div>' +
        '<div class="fin-crow2"><span>Savings time</span><b>' + (item.weeks_of_savings === null ? '—' : item.weeks_of_savings + ' weeks') + '</b></div>' +
        '<div class="fin-crow2"><span>Affordable now</span><b class="' + (item.affordable ? 'good' : 'warn') + '">' + (item.affordable ? 'Yes' : 'No') + '</b></div>' +
        '<div class="fin-crow2"><span>On list</span><span class="fin-trend">' + item.days_on_list + ' days · want ' + trend + '</span></div>' +
      '</div>' +
      '<div class="fin-verdict ' + verdict.cls + '">' + verdict.text + '</div>' +
      '<div class="fin-wish-actions">' +
        '<button class="fin-rate-btn">Re-rate</button>' +
        '<button class="fin-buy-btn">Mark bought</button>' +
        '<button class="fin-drop-btn">Drop</button>' +
      '</div>' +
      '</div>';
  }

  function wireWishCard(card, item) {
    card.querySelector('.fin-drop-btn').addEventListener('click', async () => {
      if (!confirm('Drop "' + item.name + '" from the wish list?')) return;
      try {
        await finAuthed('/api/finance/wishlist/' + item.id, { method: 'PATCH', body: { status: 'dropped' } });
        safeRender(renderWishlistTab);
      } catch (e) { alert(e.message || 'Could not update item'); }
    });
    card.querySelector('.fin-buy-btn').addEventListener('click', async () => {
      try {
        await finAuthed('/api/finance/wishlist/' + item.id, { method: 'PATCH', body: { status: 'bought' } });
        safeRender(renderWishlistTab);
      } catch (e) { alert(e.message || 'Could not update item'); }
    });
    card.querySelector('.fin-rate-btn').addEventListener('click', () => {
      const actions = card.querySelector('.fin-wish-actions');
      actions.outerHTML =
        '<div class="fin-rate-row">' +
          '<select class="fin-rate-want">' + wishScaleOptions(item.want) + '</select>' +
          '<select class="fin-rate-need">' + wishScaleOptions(item.need) + '</select>' +
          '<button class="fin-btn solid fin-rate-save" style="margin:0">Save</button>' +
        '</div>';
      card.querySelector('.fin-rate-save').addEventListener('click', async () => {
        const want = parseInt(card.querySelector('.fin-rate-want').value, 10);
        const need = parseInt(card.querySelector('.fin-rate-need').value, 10);
        try {
          await finAuthed('/api/finance/wishlist/' + item.id + '/rate', { method: 'POST', body: { want: want, need: need } });
          safeRender(renderWishlistTab);
        } catch (e) { alert(e.message || 'Could not save rating'); }
      });
    });
  }

  async function renderWishlistTab() {
    const data = await finAuthed('/api/finance/wishlist');

    if (!data.items.length) {
      screen.innerHTML = subtabsHtml('wishlist') +
        '<div class="fin-empty">Nothing on the wish list yet.</div>' +
        newWishFormHtml();
      wireSubtabs();
      wireNewWishForm();
      return;
    }

    const totalPrice = data.items.reduce((a, i) => a + (i.price || 0), 0);
    const affordable = data.items.filter(i => i.affordable).length;
    const holding = data.items.filter(i => i.rating_count >= 2 && (i.want - i.want_first) <= -2).length;

    screen.innerHTML = subtabsHtml('wishlist') +
      '<div class="fin-band">' +
        '<div class="fin-band-l">' +
          '<div class="net-label">On the list</div>' +
          '<div class="fin-hero-num">' + fmtEur(totalPrice) + '</div>' +
          '<div class="fin-band-badges">' +
            '<span class="fin-badge ' + (affordable ? 'up' : 'flat') + '">' + affordable + ' affordable now</span>' +
            (holding ? '<span class="fin-badge down">' + holding + ' cooling off</span>' : '') +
          '</div>' +
          '<div class="fin-band-note">Nothing here is a decision.<br>Re-rate want &amp; need every 30 days and let the trend answer.</div>' +
        '</div>' +
        '<div class="fin-band-r">' +
          '<div class="fin-band-head">' +
            '<span class="net-label" style="margin:0">Want over time</span>' +
            '<span class="net-label" style="margin:0">A falling line is the list working</span>' +
          '</div>' +
          wantTrendSvg(data.items) +
        '</div>' +
      '</div>' +
      '<div class="fin-wish-grid">' + data.items.map(wishCardHtml).join('') + '</div>' +
      '<div class="fin-card-actions" style="padding-left:0">' +
        '<button class="fin-btn solid" id="fin-add-wish-btn">+ Add item</button>' +
      '</div>';
    wireSubtabs();
    screen.querySelectorAll('.fin-wish').forEach(card => {
      const item = data.items.find(i => String(i.id) === card.dataset.id);
      wireWishCard(card, item);
    });
    document.getElementById('fin-add-wish-btn').addEventListener('click', () => {
      screen.innerHTML = subtabsHtml('wishlist') +
        '<div class="net-label">Add item</div>' +
        newWishFormHtml() +
        '<button class="fin-btn" id="fin-cancel-wish-btn">Cancel</button>';
      wireSubtabs();
      wireNewWishForm();
      document.getElementById('fin-cancel-wish-btn').addEventListener('click', () => safeRender(renderWishlistTab));
    });
  }

  // Spending ledger: drawn by js/finance_spend.js, which is handed only what it needs.
  async function renderSpendingTab() {
    if (!window.ActaFinanceSpend) throw new Error('spending view not loaded');
    await window.ActaFinanceSpend.render({
      screen: screen, authed: finAuthed, subtabsHtml: subtabsHtml, wireSubtabs: wireSubtabs,
      onAuthError: function () { forgetToken(); renderTabLock(); },
    });
  }

  window.financeOnShow = function () {
    if (!screen) return;
    if (!FIN.token) { renderTabLock(); return; }
    safeRender(renderNetWorthTab);
  };
})();

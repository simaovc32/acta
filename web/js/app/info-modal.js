(function initInfoModal() {
  const overlay = document.getElementById('info-modal-overlay');
  const openBtn = document.getElementById('log-workout-btn');
  const tabs    = overlay.querySelectorAll('.info-tab');
  const panels  = overlay.querySelectorAll('.info-tab-panel');

  function switchTab(tabId) {
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tabId));
    panels.forEach(p => p.classList.toggle('active', p.id === 'tab-' + tabId));
  }
  tabs.forEach(tab => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

  // ── Food intake ───────────────────────────────────────────────────────────
  // A food is priced once — by an estimate you confirm, or by hand — and stored
  // in the library. Every later log of it reuses that number, so the same plate
  // always scores the same, which is what calibrating energy-out against weight
  // change needs. Grams are always explicit: portion is the dominant error.
  const foodQ       = document.getElementById('food-q');
  const foodResults = document.getElementById('food-results');
  const foodStatus  = document.getElementById('food-status');
  const drinkRow    = document.getElementById('drink-row');
  const fnBox       = document.getElementById('food-new');
  const foodTime    = document.getElementById('food-time');

  const slotPick    = document.getElementById('slot-pick');
  let libraryCache = [];
  let pendingNew   = null;   // estimate awaiting confirmation

  // ── Which meal ────────────────────────────────────────────────────────────
  // Mirrors nutrition.SLOT_WINDOWS / slot_for_time() exactly. It has to: the
  // picker's default must be the slot the server would have picked anyway, or
  // merely opening the sheet would move where a meal lands. The 5 real windows
  // are deliberately not contiguous — a time in one of the gaps between them
  // (mid-morning, mid-afternoon, the evening lull, the whole night) is not any
  // of those meals, so it auto-picks the honest "off_hours" bucket instead of
  // being forced into whichever real meal happens to be nearest.
  const SLOT_WINDOWS = [['breakfast', 360, 570], ['morning_snack', 600, 690],
                        ['lunch', 720, 870], ['afternoon_snack', 960, 1080],
                        ['dinner', 1170, 1320]];
  const SLOT_KEYS   = ['breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner', 'off_hours'];
  const SLOT_LABEL  = { breakfast: 'Breakfast', morning_snack: 'Morning snack',
                        lunch: 'Lunch', afternoon_snack: 'Afternoon snack', dinner: 'Dinner',
                        off_hours: 'Off hours' };
  const SLOT_WHEN   = { breakfast: '6–9.30', morning_snack: '10–11.30', lunch: '12–14.30',
                        afternoon_snack: '16–18', dinner: '19.30–22', off_hours: 'between meals' };

  function slotForNow(d = new Date()) {
    const m = d.getHours() * 60 + d.getMinutes();
    for (const [slot, start, end] of SLOT_WINDOWS) if (m >= start && m < end) return slot;
    return 'off_hours';
  }

  // "HH:MM" <-> Date, for the "Logged for" field — only the time-of-day part
  // is ever used (slotForNow, the log payload), so any date object works.
  function hhmmNow(d = new Date()) {
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  function hhmmToDate(hm) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(hm || '');
    const d = new Date();
    if (m) d.setHours(+m[1], +m[2], 0, 0);
    return d;
  }

  let curSlot = slotForNow();

  function renderSlots() {
    slotPick.innerHTML = SLOT_KEYS.map(s =>
      '<button class="sp-btn' + (s === curSlot ? ' on' : '') + '" data-slot="' + s + '">'
      + '<span>' + SLOT_LABEL[s] + '</span>'
      + '<span class="sp-when">' + SLOT_WHEN[s] + '</span></button>').join('');
  }

  slotPick.addEventListener('click', e => {
    const btn = e.target.closest('.sp-btn');
    if (!btn) return;
    curSlot = btn.dataset.slot;
    renderSlots();
  });

  // Backdating "Logged for" re-derives the meal-slot suggestion from that
  // time instead of the wall clock — the same rule open() applies at now.
  foodTime.addEventListener('input', () => {
    curSlot = slotForNow(hhmmToDate(foodTime.value));
    renderSlots();
  });


  function foodMsg(text, isErr) {
    foodStatus.textContent = text || '';
    foodStatus.style.color = isErr ? 'var(--danger)' : '';
  }

  // The modal is input only. Today's log lives in Health > Food, which shows it
  // split by meal — duplicating it here made the sheet grow with every entry.
  function refreshToday() {
    if (window.refreshFoodDay) window.refreshFoodDay();
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  // The drinks row is whatever you pinned — food_items and saved dishes alike,
  // not what you happen to log most. A food pin with named sizes (Starbucks)
  // can't be "one typical serving" so it gets a ▾ and opens the size picker; a
  // dish pin logs every ingredient at its stored grams in one tap.
  function renderDrinkRow() {
    const btns = [
      ...libraryCache.filter(i => i.pinned).map(i =>
        '<button class="drink-btn" data-id="' + i.id + '" data-g="' + (i.portion_g || 100) + '">'
        + esc(i.name)
        + (i.sizes && i.sizes.length ? '<span class="db-sizes">▾</span>' : '')
        + '</button>'),
      ...mealsCache.filter(m => m.pinned).map(m =>
        '<button class="drink-btn" data-meal="' + m.id + '">'
        + esc(m.name) + ' <span class="db-sizes">dish</span></button>'),
    ].join('');
    drinkRow.innerHTML = btns
      || '<span class="fe-empty">Nothing pinned — tap ☆ on a search result.</span>';
  }

  async function loadLibrary() {
    try {
      const d = await fetch('/api/food/library?limit=100').then(r => r.json());
      libraryCache = d.items || [];
    } catch { libraryCache = []; }
    renderDrinkRow();
  }

  // One tap logs one typical serving — the whole point of the drinks row.
  // A pin with named sizes is the exception: there is no one typical
  // serving, so the tap surfaces the same size picker a search result gets.
  drinkRow.addEventListener('click', async e => {
    const btn = e.target.closest('.drink-btn');
    if (!btn) return;
    // A pinned dish logs every ingredient at its stored grams, in one tap.
    if (btn.dataset.meal) {
      btn.disabled = true;
      await logSavedMeal(+btn.dataset.meal);
      btn.disabled = false;
      return;
    }
    const item = libraryCache.find(i => i.id === +btn.dataset.id);
    if (item && item.sizes && item.sizes.length) {
      // Cancel any debounced re-render still pending from typing in the
      // search box moments ago — it would fire after openAmount below and
      // wipe the panel it just inserted (renderResults rebuilds the whole
      // #food-results innerHTML from scratch).
      clearTimeout(qTimer);
      foodQ.value = item.name;
      renderResults(item.name);
      const hitRow = foodResults.querySelector('.food-hit[data-id="' + item.id + '"]');
      if (hitRow) openAmount(hitRow.closest('.food-hit-row'), item);
      return;
    }
    btn.disabled = true;
    await logFood({ food_id: +btn.dataset.id, grams: +btn.dataset.g });
    btn.disabled = false;
  });

  // Between meal windows curSlot is null — every write path checks this
  // first, so nothing can land in a meal nobody picked.
  function requireSlot() {
    if (curSlot) return true;
    foodMsg('Pick a meal above first.', true);
    return false;
  }

  async function logFood(payload) {
    if (!requireSlot()) return false;
    foodMsg('Saving…');
    try {
      const res  = await fetch('/api/food/log', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ slot: curSlot, time: foodTime.value || undefined }, payload)),
      });
      const data = await res.json();
      if (!res.ok) { foodMsg(data.detail || 'Could not log that.', true); return false; }
      foodMsg('Logged.');
      setTimeout(() => foodMsg(''), 1500);
      refreshToday();
      loadLibrary();
      return true;
    } catch { foodMsg('Network error.', true); return false; }
  }

  function renderResults(q) {
    const nq = q.trim().toLowerCase();
    // Nothing until you type. Listing the library by default made the sheet
    // grow with every new food logged, for a list the pinned buttons above
    // already cover for anything eaten often.
    if (!nq) {
      foodResults.innerHTML =
        '<div class="fe-empty">Type to search your foods, or describe a new one.</div>';
      return;
    }
    // Dishes first: "tosta de queijo" is a more specific answer than the bread
    // or the cheese it is made of, so it must not sit below its own parts.
    const dishes = mealsCache.filter(m => m.name.toLowerCase().includes(nq)).slice(0, 4);
    let html = dishes.map(m => {
      const t = mealTotals(m);
      const parts = (m.components || []).length;
      return '<div class="food-hit-row">'
        + '<button class="food-hit meal-hit" data-meal="' + m.id + '">'
        + '<span class="fh-n">' + esc(m.name)
        + ' <span class="mh-tag">Dish</span></span>'
        + '<span class="fh-m">' + parts + ' item' + (parts === 1 ? '' : 's') + ' · '
        + (t.missing ? '—' : t.kcal + ' kcal · ' + t.protein + ' g P') + '</span></button>'
        + '<button class="fh-pin' + (m.pinned ? ' on' : '') + '" data-pin-meal="' + m.id + '"'
        + ' title="' + (m.pinned ? 'Remove its button' : 'Give it a button') + '">'
        + (m.pinned ? '★' : '☆') + '</button>'
        + '<button class="mh-x" data-delmeal="' + m.id + '"'
        + ' title="Forget this dish">×</button></div>';
    }).join('');

    const hits = libraryCache.filter(i => i.name.toLowerCase().includes(nq)).slice(0, 8);
    html += hits.map(i =>
      '<div class="food-hit-row">'
      + '<button class="food-hit" data-id="' + i.id + '" data-g="' + (i.portion_g || 100) + '">'
      + '<span class="fh-n">' + esc(i.name) + '</span>'
      + '<span class="fh-m">' + Math.round(i.kcal_100g) + '/100g · '
      + (i.portion_g ? Math.round(i.portion_g) + ' g' : 'portion?') + '</span></button>'
      + '<button class="fh-pin' + (i.pinned ? ' on' : '') + '" data-pin="' + i.id + '"'
      + ' title="' + (i.pinned ? 'Remove its button' : 'Give it a button') + '">'
      + (i.pinned ? '★' : '☆') + '</button></div>').join('');
    // Only offer an estimate when nothing already answers the query — repeats
    // must never cost an API call, or the numbers stop being reproducible. A
    // saved dish counts: "tosta de queijo" is already priced, so offering to
    // estimate it would hand a solved plate back to the model.
    if (nq && !hits.some(i => i.name.toLowerCase() === nq)
           && !dishes.some(m => m.name.toLowerCase() === nq)) {
      html += '<button class="food-hit new" id="food-estimate">'
            + '<span class="fh-n">+ Estimate “' + esc(q.trim()) + '”</span>'
            + '<span class="fh-m">adds it to your library</span></button>';
    }
    foodResults.innerHTML = html;
  }

  let qTimer = null;
  foodQ.addEventListener('input', () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => renderResults(foodQ.value), 120);
  });

  foodResults.addEventListener('click', async e => {
    // Star toggles the one-tap button; it must never also log the row. Works on
    // both a food_item (data-pin) and a saved dish (data-pin-meal).
    const pin = e.target.closest('.fh-pin');
    if (pin) {
      e.stopPropagation();
      const on     = !pin.classList.contains('on');
      const isMeal = pin.dataset.pinMeal != null;
      const url    = isMeal
        ? '/api/food/meal/' + pin.dataset.pinMeal + '/pin'
        : '/api/food/item/' + pin.dataset.pin + '/pin';
      pin.disabled = true;
      try {
        await fetch(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pinned: on }),
        });
        if (isMeal) await loadMeals();
        await loadLibrary();
        renderResults(foodQ.value);
      } catch { foodMsg('Network error.', true); }
      pin.disabled = false;
      return;
    }
    // The × forgets the saved dish; it must never also log it. This is the only
    // way to delete one now that the button row is gone, so it has to be here.
    const delMeal = e.target.closest('[data-delmeal]');
    if (delMeal) {
      e.stopPropagation();
      const m = mealsCache.find(x => x.id === +delMeal.dataset.delmeal);
      if (!window.confirm('Forget the dish "' + (m ? m.name : '') + '"? '
                          + 'Already-logged meals keep their own numbers.')) return;
      await fetch('/api/food/meal/' + delMeal.dataset.delmeal,
                  { method: 'DELETE' }).catch(() => {});
      await loadMeals();
      renderResults(foodQ.value);
      return;
    }

    // A saved dish logs every ingredient at its stored amount, in one tap.
    const dish = e.target.closest('[data-meal]');
    if (dish) {
      dish.disabled = true;
      await logSavedMeal(+dish.dataset.meal);
      dish.disabled = false;
      return;
    }

    const est = e.target.closest('#food-estimate');
    if (est) { estimateNew(foodQ.value.trim()); return; }
    // Inside the amount panel: chips set the grams, Cancel closes, Log commits.
    const box = e.target.closest('.fh-amount');
    if (box) {
      const chip = e.target.closest('.fa-chip');
      if (chip) {
        box.querySelector('input').value = chip.dataset.grams != null
          ? Math.round(+chip.dataset.grams)
          : Math.round((+box.dataset.portion || 100) * +chip.dataset.mult);
        updateAmount(box);
        return;
      }
      if (e.target.closest('.fa-x'))   { closeAmount(); return; }
      if (e.target.closest('.fa-log')) { commitAmount(box); return; }
      return;
    }

    const hit = e.target.closest('.food-hit');
    if (!hit) return;
    const item = libraryCache.find(i => i.id === +hit.dataset.id);
    if (!item) return;
    openAmount(hit.closest('.food-hit-row'), item);
  });

  // ── How much ──────────────────────────────────────────────────────────────
  // This replaced a window.prompt asking for grams. Two things were wrong with
  // it: on the phone PWA it is a system dialog thrown over the sheet, and "how
  // many grams?" is not a question you can answer looking at a plate. The
  // library already stores the typical serving and what it is called, so the
  // amount is asked in servings — with the grams shown, editable, and never
  // hidden, because portion is still the dominant error in the estimate.
  const MULTS = [[0.5, '½'], [1, '1'], [1.5, '1½'], [2, '2']];

  function closeAmount() {
    const open = foodResults.querySelector('.fh-amount');
    if (open) open.remove();
    foodResults.querySelectorAll('.food-hit-row.picking')
      .forEach(r => r.classList.remove('picking'));
  }

  function openAmount(row, item) {
    closeAmount();
    row.classList.add('picking');
    const sizes   = item.sizes && item.sizes.length ? item.sizes : null;
    const portion = item.portion_g || 0;
    const box = document.createElement('div');
    box.className     = 'fh-amount';
    box.dataset.id    = item.id;
    if (portion) box.dataset.portion = portion;
    // A named-size drink (Starbucks Tall/Grande/Venti, Solo/Doppio) gets its
    // actual sizes as chips instead of generic ½/1/1½/2× servings — "how much
    // Mocha" is a size, not a multiplier.
    const chips = sizes
      ? sizes.map(s => '<button class="fa-chip" data-grams="' + s.grams + '">'
                        + esc(s.label) + '</button>').join('')
      : (portion ? MULTS.map(([m, lbl]) =>
          '<button class="fa-chip" data-mult="' + m + '">' + lbl + '</button>').join('') : '');
    box.innerHTML =
      (!sizes && item.portion_label
        ? '<div class="fa-label">' + esc(item.portion_label) + '</div>' : '')
      + (chips ? '<div class="fa-chips">' + chips + '</div>' : '')
      + '<div class="fa-row">'
      +   '<input type="number" step="1" min="1" max="3000" value="'
      +     Math.round(portion || 100) + '" />'
      +   '<span class="fa-unit">g</span>'
      +   '<span class="fa-sub"></span>'
      +   '<span class="fa-act">'
      +     '<button class="btn-cancel fa-x">Cancel</button>'
      +     '<button class="btn-save fa-log">Log</button>'
      +   '</span>'
      + '</div>';
    row.after(box);
    updateAmount(box);
    const input = box.querySelector('input');
    input.focus();
    input.select();
  }

  function updateAmount(box) {
    const item = libraryCache.find(i => i.id === +box.dataset.id);
    const g    = parseFloat(box.querySelector('input').value);
    const sub  = box.querySelector('.fa-sub');
    sub.textContent = (item && isFinite(g) && g > 0)
      ? Math.round((item.kcal_100g || 0) * g / 100) + ' kcal · '
        + (Math.round((item.protein_g || 0) * g / 10) / 10) + ' g P'
      : '';
    // A chip only stays lit while the grams still match it exactly — typing
    // over it must not leave a size or serving claimed for an edited amount.
    if (item && item.sizes && item.sizes.length) {
      box.querySelectorAll('.fa-chip').forEach(c => c.classList.toggle(
        'on', Math.round(+c.dataset.grams) === Math.round(g)));
    } else {
      const portion = +box.dataset.portion || 0;
      box.querySelectorAll('.fa-chip').forEach(c => c.classList.toggle(
        'on', portion > 0 && Math.round(portion * +c.dataset.mult) === Math.round(g)));
    }
  }

  async function commitAmount(box) {
    const g = parseFloat(String(box.querySelector('input').value).replace(',', '.'));
    if (!isFinite(g) || g <= 0) { foodMsg('Enter an amount in grams.', true); return; }
    if (await logFood({ food_id: +box.dataset.id, grams: g })) {
      closeAmount();
      foodQ.value = ''; renderResults('');
    }
  }

  foodResults.addEventListener('input', e => {
    const box = e.target.closest('.fh-amount');
    if (box) updateAmount(box);
  });

  // Enter commits the amount, so search → Enter → Enter logs without a pointer.
  foodResults.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const box = e.target.closest('.fh-amount');
    if (!box) return;
    e.preventDefault();
    commitAmount(box);
  });

  async function estimateNew(desc) {
    if (!desc) return;
    foodMsg('Estimating…');
    try {
      const res  = await fetch('/api/food/resolve', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: desc }),
      });
      const data = await res.json();
      if (!res.ok) {
        // The estimate is a convenience, not the only path — say so.
        foodMsg((data.detail || 'Estimate failed') + ' — add it by hand below.', true);
        showNewCard({ name: desc, kcal_100g: '', portion_g: 100, confidence: null,
                      note: 'Enter the numbers yourself.' });
        return;
      }
      foodMsg('');
      // The model splits a dish only when its parts are portioned independently.
      const r = data.result || {};
      if (r.components && r.components.length > 1) showMealCard(r);
      else showNewCard(r);
    } catch {
      foodMsg('Network error — add it by hand below.', true);
      showNewCard({ name: desc, kcal_100g: '', portion_g: 100, confidence: null, note: null });
    }
  }

  // Toggles the pin row vs. the caffeine/alcohol flags: pinning needs a
  // food_item id, which only exists on the "remember" path; the adhoc flags
  // only matter when there's no food_item for the event mirror to read.
  function fnApplyRememberState() {
    const remember = document.getElementById('fn-remember').checked;
    document.getElementById('fn-pin-row').hidden      = !remember;
    document.getElementById('fn-adhoc-flags').hidden  = remember;
  }
  document.getElementById('fn-remember').addEventListener('change', fnApplyRememberState);
  document.getElementById('fn-alcohol').addEventListener('change', () => {
    document.getElementById('fn-adhoc-abv-row').hidden =
      !document.getElementById('fn-alcohol').checked;
  });

  function showNewCard(r) {
    pendingNew = r;
    document.getElementById('fn-name').value    = r.name || '';
    document.getElementById('fn-kcal').value    = r.kcal_100g === '' ? '' : Math.round(r.kcal_100g);
    document.getElementById('fn-portion').value = Math.round(r.portion_g || 100);
    document.getElementById('fn-protein').value = r.protein_g != null ? r.protein_g : '';
    const conf = document.getElementById('fn-conf');
    conf.textContent = r.confidence ? r.confidence + ' confidence' : '';
    conf.className   = 'fn-conf' + (r.confidence ? ' c-' + r.confidence : '');
    document.getElementById('fn-note').textContent = r.note || '';
    // A new drink is almost always something you want one tap away — pre-tick it.
    document.getElementById('fn-pin').checked = r.kind === 'drink';
    // Alcohol %: drinks only. The model fills it (0 for soft drinks); editable,
    // because a wrong ABV silently mis-sizes every alcohol event this drink logs.
    const isDrink = r.kind === 'drink';
    document.getElementById('fn-abv-label').hidden = !isDrink;
    const abvIn = document.getElementById('fn-abv');
    abvIn.hidden = !isDrink;
    abvIn.value  = isDrink && r.abv_pct != null ? r.abv_pct : (isDrink ? 0 : '');
    // Reset the "don't remember" path fresh every time the card opens.
    document.getElementById('fn-remember').checked = true;
    document.getElementById('fn-caffeine').checked = false;
    document.getElementById('fn-alcohol').checked  = false;
    document.getElementById('fn-adhoc-abv').value  = '';
    document.getElementById('fn-adhoc-abv-row').hidden = true;
    fnApplyRememberState();
    fnBox.style.display = '';
    fnBox.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function hideNewCard() { fnBox.style.display = 'none'; pendingNew = null; }
  document.getElementById('fn-cancel').addEventListener('click', hideNewCard);

  // ── Multi-component meals ─────────────────────────────────────────────────
  // "Frango com arroz" is not a food, it is a ratio: 150 g rice + 50 g chicken
  // and the reverse are 13% apart in calories but 2.4x apart in protein. So a
  // dish whose parts are plated independently is logged as one row per
  // ingredient, each with its own grams.
  const mcBox  = document.getElementById('meal-card');
  const mcRows = document.getElementById('mc-rows');
  let pendingMeal = null;

  function hideMealCard() { mcBox.style.display = 'none'; pendingMeal = null; }
  document.getElementById('mc-cancel').addEventListener('click', hideMealCard);

  function showMealCard(r) {
    hideNewCard();
    pendingMeal = { components: (r.components || []).map(c => Object.assign({}, c)) };
    document.getElementById('mc-title').value = r.name || '';
    const conf = document.getElementById('mc-conf');
    conf.textContent = r.confidence ? r.confidence + ' confidence' : '';
    conf.className   = 'fn-conf' + (r.confidence ? ' c-' + r.confidence : '');
    document.getElementById('mc-note').textContent = r.note || '';
    document.getElementById('mc-save-as').checked = false;
    renderMealRows();
    mcBox.style.display = '';
    mcBox.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  // An ingredient already in the library keeps the library's numbers. That is
  // what makes a repeat plate score the same: the first resolution wins, and a
  // later estimate never quietly rewrites it.
  function libHit(name) {
    const n = String(name || '').trim().toLowerCase();
    return n ? libraryCache.find(i => i.name.toLowerCase() === n) : undefined;
  }
  function compNutri(c) { return libHit(c.name) || c; }

  function compSub(c) {
    const g = +c.grams || 0, n = compNutri(c);
    if (!n.kcal_100g) return 'not in your library yet';
    return Math.round((n.kcal_100g || 0) * g / 100) + ' kcal · '
         + (Math.round((n.protein_g || 0) * g / 10) / 10) + ' g protein';
  }

  function renderMealRows() {
    mcRows.innerHTML = pendingMeal.components.map((c, i) =>
      '<div class="mc-row" data-i="' + i + '">'
      + '<input class="mc-name" type="text" data-f="name" placeholder="ingredient"'
      + ' value="' + esc(c.name || '') + '" />'
      + '<span class="mc-g"><input type="number" step="1" min="1" max="3000" data-f="grams"'
      + ' value="' + Math.round(c.grams || 0) + '" /><span>g</span></span>'
      + '<button class="mc-x" title="Remove">×</button>'
      + '<div class="mc-sub">' + esc(compSub(c)) + '</div></div>').join('');
    updateMealTotals();
  }

  function updateMealTotals() {
    let k = 0, p = 0;
    pendingMeal.components.forEach(c => {
      const g = +c.grams || 0, n = compNutri(c);
      k += (n.kcal_100g || 0) * g / 100;
      p += (n.protein_g  || 0) * g / 100;
    });
    document.getElementById('mc-kcal').textContent = Math.round(k).toLocaleString('en-US');
    document.getElementById('mc-protein').textContent = Math.round(p * 10) / 10;
  }

  // Patched in place, never re-rendered: rebuilding the rows mid-keystroke
  // would drop focus and the caret position.
  mcRows.addEventListener('input', e => {
    const row = e.target.closest('.mc-row');
    const f   = e.target.dataset.f;
    if (!row || !f) return;
    const c = pendingMeal.components[+row.dataset.i];
    if (f === 'grams') c.grams = parseFloat(e.target.value) || 0;
    else c.name = e.target.value;
    row.querySelector('.mc-sub').textContent = compSub(c);
    updateMealTotals();
  });

  mcRows.addEventListener('click', e => {
    if (!e.target.closest('.mc-x')) return;
    pendingMeal.components.splice(+e.target.closest('.mc-row').dataset.i, 1);
    renderMealRows();          // indices shift, so a full re-render is right here
  });

  document.getElementById('mc-add').addEventListener('click', () => {
    pendingMeal.components.push({ name: '', grams: 100 });
    renderMealRows();
    const last = mcRows.querySelector('.mc-row:last-child .mc-name');
    if (last) last.focus();
  });

  document.getElementById('mc-log').addEventListener('click', async () => {
    if (!requireSlot()) return;
    const name = document.getElementById('mc-title').value.trim();
    if (!name) { foodMsg('Name the meal first.', true); return; }
    const comps = [];
    for (const c of pendingMeal.components) {
      const nm = String(c.name || '').trim();
      const g  = +c.grams;
      if (!nm) { foodMsg('Every ingredient needs a name.', true); return; }
      if (!isFinite(g) || g <= 0) { foodMsg('“' + nm + '” needs an amount in grams.', true); return; }
      const hit = libHit(nm);
      if (hit)             comps.push({ food_id: hit.id, name: nm, grams: g });
      else if (c.kcal_100g) comps.push({ name: nm, grams: g, kcal_100g: c.kcal_100g,
                                         protein_g: c.protein_g, carbs_g: c.carbs_g,
                                         fat_g: c.fat_g });
      else { foodMsg('“' + nm + '” is not in your library — estimate it on its own first.', true); return; }
    }
    if (!comps.length) { foodMsg('Add at least one ingredient.', true); return; }

    foodMsg('Logging…');
    const res = await fetch('/api/food/meal/log', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, components: comps, slot: curSlot,
                             time: foodTime.value || undefined,
                             save: document.getElementById('mc-save-as').checked }),
    }).catch(() => null);
    const data = res ? await res.json().catch(() => ({})) : {};
    if (!res || !res.ok) { foodMsg(data.detail || 'Could not log that.', true); return; }
    hideMealCard();
    foodQ.value = '';
    foodMsg('Logged.');
    setTimeout(() => foodMsg(''), 1500);
    await loadLibrary();
    await loadMeals();
    renderResults('');
    refreshToday();
  });

  // ── Saved meals ───────────────────────────────────────────────────────────
  // Saved dishes are not listed in the sheet. They used to sit in a row of
  // buttons, which grew with every dish saved — the same unbounded growth that
  // took the day's log list and the default library listing out of here. They
  // live in the search box instead: type the dish name, tap it, the whole plate
  // is logged. Nothing about the stored dish changes, only where you reach it.
  let mealsCache = [];

  async function loadMeals() {
    try { mealsCache = (await fetch('/api/food/meals').then(r => r.json())).meals || []; }
    catch { mealsCache = []; }
    renderDrinkRow();
  }

  // Totals come from the library, not the saved dish: the dish stores grams per
  // ingredient, and the library owns the numbers those grams are priced at.
  function mealTotals(m) {
    let kcal = 0, protein = 0, missing = 0;
    (m.components || []).forEach(c => {
      const it = libraryCache.find(i => i.id === c.food_id);
      if (!it) { missing++; return; }
      kcal    += (it.kcal_100g || 0) * c.grams / 100;
      protein += (it.protein_g || 0) * c.grams / 100;
    });
    return { kcal: Math.round(kcal), protein: Math.round(protein * 10) / 10, missing };
  }

  async function logSavedMeal(id) {
    if (!requireSlot()) return;
    foodMsg('Logging…');
    const res = await fetch('/api/food/meal/' + id + '/log', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot: curSlot, time: foodTime.value || undefined }),
    }).catch(() => null);
    const data = res ? await res.json().catch(() => ({})) : {};
    if (!res || !res.ok) { foodMsg(data.detail || 'Could not log that.', true); return; }
    foodMsg(data.meal && data.meal.skipped
      ? 'Logged — ' + data.meal.skipped + ' ingredient(s) no longer in your library.'
      : 'Logged.');
    setTimeout(() => foodMsg(''), 2000);
    foodQ.value = '';
    renderResults('');
    refreshToday();
  }

  document.getElementById('fn-save').addEventListener('click', async () => {
    const name     = document.getElementById('fn-name').value.trim();
    const kcal     = parseFloat(document.getElementById('fn-kcal').value);
    const portion  = parseFloat(document.getElementById('fn-portion').value);
    const pinIt    = document.getElementById('fn-pin').checked;
    const remember = document.getElementById('fn-remember').checked;
    const proteinRaw = document.getElementById('fn-protein').value;
    const protein  = proteinRaw === '' ? null : parseFloat(proteinRaw);
    if (!name)                    { foodMsg('Name it first.', true); return; }
    if (!isFinite(kcal) || kcal < 0 || kcal > 900) {
      foodMsg('kcal /100g must be 0–900.', true); return;
    }
    if (!isFinite(portion) || portion <= 0) { foodMsg('Portion must be > 0 g.', true); return; }
    if (proteinRaw !== '' && (!isFinite(protein) || protein < 0 || protein > 100)) {
      foodMsg('Protein /100g must be 0–100.', true); return;
    }

    const p = pendingNew || {};
    const kind = p.kind || 'food';

    // Unticking "Remember" logs a one-off entry straight to food_log — no
    // food_item is ever created, so it can't pollute search/autocomplete or
    // the library's determinism. There is nowhere to park an unsaved entry
    // outside a meal window, unlike the "remember" path below.
    if (!remember) {
      if (!curSlot) {
        foodMsg('Pick a meal to log it, or check "Remember this food" to save it for later.', true);
        return;
      }
      const caffeineIt = document.getElementById('fn-caffeine').checked;
      const alcoholIt  = document.getElementById('fn-alcohol').checked;
      const adhocAbvRaw = document.getElementById('fn-adhoc-abv').value;
      const adhocAbv = alcoholIt && adhocAbvRaw !== '' && isFinite(parseFloat(adhocAbvRaw))
        ? Math.max(0, Math.min(96, parseFloat(adhocAbvRaw))) : null;
      foodQ.value = '';
      if (await logFood({
        name, kcal_100g: kcal, grams: portion,
        protein_g: protein, caffeine: caffeineIt, abv_pct: adhocAbv,
      })) { hideNewCard(); renderResults(''); }
      return;
    }

    const abvRaw = document.getElementById('fn-abv').value;
    const abv = kind === 'drink' && abvRaw !== '' && isFinite(parseFloat(abvRaw))
      ? Math.max(0, Math.min(96, parseFloat(abvRaw))) : null;
    foodMsg('Saving…');
    const res = await fetch('/api/food/item', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, kcal_100g: kcal, kind,
        protein_g: protein, carbs_g: p.carbs_g, fat_g: p.fat_g,
        portion_g: portion, portion_label: p.portion_label,
        abv_pct: abv,
        source: p.source === 'llm' ? 'llm' : 'manual',
      }),
    }).catch(() => null);
    if (!res || !res.ok) {
      foodMsg(res ? ((await res.json()).detail || 'Could not save.') : 'Network error.', true);
      return;
    }
    const item = (await res.json()).item;

    if (pinIt) {
      await fetch('/api/food/item/' + item.id + '/pin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned: true }),
      }).catch(() => {});
    }

    hideNewCard();
    await loadLibrary();

    // Saving to the library is the whole job. Log it now only if a meal is
    // picked — outside a meal window there is nothing to log into, and "saved
    // (and pinned) for later" is a finished outcome, not a failure.
    if (curSlot) {
      foodQ.value = '';
      if (await logFood({ food_id: item.id, grams: portion })) renderResults('');
    } else {
      foodMsg(pinIt ? 'Saved and pinned to the drinks row.' : 'Saved to your library.');
      setTimeout(() => foodMsg(''), 2200);
      foodQ.value = item.name;
      renderResults(item.name);
    }
  });

  // ── Weight ────────────────────────────────────────────────────────────────
  const weightIn = document.getElementById('weight-kg');
  const waistIn  = document.getElementById('waist-cm');
  document.getElementById('weight-save').addEventListener('click', async () => {
    const kgRaw = String(weightIn.value).replace(',', '.').trim();
    const cmRaw = String(waistIn.value).replace(',', '.').trim();
    const kg = kgRaw ? parseFloat(kgRaw) : null;
    const cm = cmRaw ? parseFloat(cmRaw) : null;
    if (kg == null && cm == null) { foodMsg('Enter your weight or waist.', true); return; }
    if (kgRaw && !isFinite(kg)) { foodMsg('Weight must be a number in kg.', true); return; }
    if (cmRaw && !isFinite(cm)) { foodMsg('Waist must be a number in cm.', true); return; }
    const payload = {};
    if (kg != null) payload.weight_kg = kg;
    if (cm != null) payload.waist_cm  = cm;
    foodMsg('Saving…');
    try {
      const res  = await fetch('/api/log/weight', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) { foodMsg(data.detail || 'Could not save.', true); return; }
      foodMsg(cm != null ? 'Saved · VO₂ max is recomputing.' : 'Weight saved.');
      setTimeout(() => foodMsg(''), 1800);
      loadWeightHint();
      if (window.refreshFoodWeight) window.refreshFoodWeight();
    } catch { foodMsg('Network error.', true); }
  });

  async function loadWeightHint() {
    const hint = document.getElementById('weight-hint');
    try {
      const d = await fetch('/api/body/weight?days=30').then(r => r.json());
      if (d.waist_cm != null) waistIn.value = d.waist_cm;
      let t = '';
      if (!d.latest) { weightIn.value = ''; t = 'No weight logged yet.'; }
      else {
        weightIn.value = d.latest.weight_kg;
        // Single-day weight is mostly water; the 7-day mean is the number that
        // means anything, and it's what k-calibration will read later.
        t = 'Last: ' + d.latest.weight_kg + ' kg on ' + d.latest.date;
        if (d.avg_7d != null) t += ' · 7-day avg ' + d.avg_7d + ' kg';
        if (d.trend_kg_per_week != null) {
          t += ' · ' + (d.trend_kg_per_week > 0 ? '+' : '') + d.trend_kg_per_week + ' kg/wk';
        }
      }
      if (d.waist_cm != null) {
        t += (t ? '\n' : '') + 'Waist: ' + d.waist_cm + ' cm'
          + (d.waist_measured_on ? ' on ' + d.waist_measured_on : '')
          + ' · feeds VO₂ max';
      }
      hint.textContent = t;
    } catch { hint.textContent = ''; }
  }

  const soreSave      = document.getElementById('sore-save');
  const soreCancel    = document.getElementById('sore-cancel');
  const soreStatus    = document.getElementById('sore-status');
  const soreNote      = document.getElementById('sore-note');
  const soreMap       = document.getElementById('sore-map');
  const soreSelected  = document.getElementById('sore-selected');

  // ── body map ──────────────────────────────────────────────────────────────
  // Areas are drawn as plain SVG shapes; each carries data-area (slug sent to
  // the API) and data-sev (0-3, styled by CSS). Slugs for the eight joints match
  // the older finish-screen pain chips so both write the same vocabulary.
  const SORE_LABELS = {
    neck: 'Neck', traps: 'Traps', shoulder: 'Shoulders', chest: 'Chest',
    lats: 'Lats', 'mid-back': 'Mid back', 'lower-back': 'Lower back',
    biceps: 'Biceps', triceps: 'Triceps', forearm: 'Forearms', elbow: 'Elbows',
    wrist: 'Wrists', abs: 'Abs', obliques: 'Obliques', glutes: 'Glutes',
    hip: 'Hips', quads: 'Quads', hamstrings: 'Hamstrings', knee: 'Knees',
    calves: 'Calves', ankle: 'Ankles',
  };
  // Two questions per zone: TYPE (what it feels like) and INTENSITY (1 mild · 2 moderate · 3 severe).
  // `heavy` is a tired muscle; `sharp` is joint / injury pain. Bare-number rows read as `ache`.
  const SORE_KINDS = [
    { key: 'ache',  label: 'Sore',       words: ['a bit sore', 'sore', 'very sore'],
      glyph: 'M-2.2 0 Q-1.1 -1.8 0 0 T2.2 0' },
    { key: 'tight', label: 'Tight',      words: ['a bit tight', 'tight', 'very tight'],
      glyph: 'M-2.2 -1.5 L-0.5 0 L-2.2 1.5 M2.2 -1.5 L0.5 0 L2.2 1.5' },
    { key: 'heavy', label: 'Tired',      words: ['a bit tired', 'tired', 'very tired'],
      glyph: 'M0 -2.2 V2.2 M-1.6 0.6 L0 2.2 L1.6 0.6' },
    { key: 'sharp', label: 'Sharp pain', words: ['mild pain', 'pain', 'severe pain'],
      glyph: 'M0.8 -2.4 L-1.2 0.2 H1.2 L-0.8 2.4' },
  ];
  const SORE_KIND = Object.fromEntries(SORE_KINDS.map(k => [k.key, k]));
  const soreWord = (s) => (SORE_KIND[s.kind] || SORE_KIND.ache).words[s.sev - 1];
  let soreKind = 'ache';   // the type the next tapped zone gets
  // [slug, shape] per view. Mirrored limbs share one slug — tapping either side
  // sets the same area, since the plan only needs "arms are sore", not which one.
  const BODY_FRONT = [
    ['neck',     '<rect x="44" y="17" width="12" height="7" rx="3"/>'],
    ['shoulder', '<circle cx="33" cy="31" r="7.5"/><circle cx="67" cy="31" r="7.5"/>'],
    ['chest',    '<rect x="39" y="26" width="22" height="15" rx="4"/>'],
    ['biceps',   '<rect x="24" y="38" width="9" height="17" rx="4.5"/><rect x="67" y="38" width="9" height="17" rx="4.5"/>'],
    ['elbow',    '<circle cx="28" cy="58" r="4"/><circle cx="72" cy="58" r="4"/>'],
    ['forearm',  '<rect x="22" y="62" width="8" height="17" rx="4"/><rect x="70" y="62" width="8" height="17" rx="4"/>'],
    ['wrist',    '<circle cx="26" cy="82" r="3.5"/><circle cx="74" cy="82" r="3.5"/>'],
    ['abs',      '<rect x="41" y="43" width="18" height="18" rx="4"/>'],
    ['obliques', '<rect x="34" y="44" width="6" height="16" rx="3"/><rect x="60" y="44" width="6" height="16" rx="3"/>'],
    ['hip',      '<rect x="37" y="63" width="26" height="9" rx="4"/>'],
    ['quads',    '<rect x="37" y="74" width="11" height="25" rx="5"/><rect x="52" y="74" width="11" height="25" rx="5"/>'],
    ['knee',     '<circle cx="42.5" cy="103" r="4.5"/><circle cx="57.5" cy="103" r="4.5"/>'],
    ['ankle',    '<circle cx="42.5" cy="132" r="3.5"/><circle cx="57.5" cy="132" r="3.5"/>'],
  ];
  const BODY_BACK = [
    ['neck',       '<rect x="44" y="17" width="12" height="7" rx="3"/>'],
    ['traps',      '<path d="M38 26 L62 26 L56 38 L44 38 Z"/>'],
    ['shoulder',   '<circle cx="33" cy="31" r="7.5"/><circle cx="67" cy="31" r="7.5"/>'],
    ['lats',       '<path d="M37 33 L44 40 L44 56 L36 50 Z"/><path d="M63 33 L56 40 L56 56 L64 50 Z"/>'],
    ['mid-back',   '<rect x="44" y="39" width="12" height="13" rx="3"/>'],
    ['lower-back', '<rect x="41" y="54" width="18" height="12" rx="4"/>'],
    ['triceps',    '<rect x="24" y="38" width="9" height="17" rx="4.5"/><rect x="67" y="38" width="9" height="17" rx="4.5"/>'],
    ['elbow',      '<circle cx="28" cy="58" r="4"/><circle cx="72" cy="58" r="4"/>'],
    ['forearm',    '<rect x="22" y="62" width="8" height="17" rx="4"/><rect x="70" y="62" width="8" height="17" rx="4"/>'],
    ['glutes',     '<rect x="37" y="68" width="26" height="13" rx="6"/>'],
    ['hamstrings', '<rect x="37" y="83" width="11" height="20" rx="5"/><rect x="52" y="83" width="11" height="20" rx="5"/>'],
    ['knee',       '<circle cx="42.5" cy="107" r="4.5"/><circle cx="57.5" cy="107" r="4.5"/>'],
    ['calves',     '<rect x="38" y="112" width="9" height="17" rx="4.5"/><rect x="53" y="112" width="9" height="17" rx="4.5"/>'],
    ['ankle',      '<circle cx="42.5" cy="134" r="3.5"/><circle cx="57.5" cy="134" r="3.5"/>'],
  ];
  // Silhouette drawn under the hit shapes so the figure reads as a body.
  const BODY_OUTLINE =
    '<path class="sore-outline" d="M50 8 m-7 0 a7 7 0 1 0 14 0 a7 7 0 1 0 -14 0"/>' +
    '<path class="sore-outline" d="M38 25 Q50 21 62 25 L70 32 L78 62 L74 84 L70 62 L64 44 ' +
    'L64 66 L63 100 L60 136 L52 136 L50 104 L48 136 L40 136 L37 100 L36 66 L36 44 ' +
    'L30 62 L26 84 L22 62 L30 32 Z"/>';

  let soreState = {};   // {slug: {sev 0-3, kind}} for the open modal session
  function soreSvg(view, shapes) {
    const inner = shapes.map(([slug, shape]) =>
      '<g data-area="' + slug + '" data-sev="0">' + shape + '</g>').join('');
    return '<div><svg viewBox="0 0 100 145" role="img" aria-label="' + view + ' body map">' +
      BODY_OUTLINE + inner + '</svg><div class="sore-view-label">' + view + '</div></div>';
  }
  function renderSoreKinds() {
    const host = document.getElementById('sore-kinds');
    host.innerHTML = SORE_KINDS.map(k =>
      '<button type="button" class="sore-kind" data-kind="' + k.key + '" aria-pressed="' + (k.key === soreKind) + '">' +
      '<svg viewBox="-3 -3 6 6" aria-hidden="true"><path d="' + k.glyph + '"/></svg>' + k.label + '</button>').join('');
    host.querySelectorAll('.sore-kind').forEach(b => b.addEventListener('click', () => {
      soreKind = b.dataset.kind;
      host.querySelectorAll('.sore-kind').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    }));
  }
  // Centre and size of a hit shape from its attributes: the map is drawn while the modal is still
  // display:none, where getBBox() has nothing to measure. Paths here are absolute M/L polygons.
  function soreShapeBox(el) {
    const n = a => parseFloat(el.getAttribute(a));
    if (el.tagName === 'rect') return { cx: n('x') + n('width') / 2, cy: n('y') + n('height') / 2, s: Math.min(n('width'), n('height')) };
    if (el.tagName === 'circle') return { cx: n('cx'), cy: n('cy'), s: 2 * n('r') };
    const v = (el.getAttribute('d').match(/-?[\d.]+/g) || []).map(Number);
    const xs = v.filter((_, i) => i % 2 === 0), ys = v.filter((_, i) => i % 2 === 1);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, s: Math.min(x1 - x0, y1 - y0) };
  }
  function renderSoreMap() {
    soreMap.innerHTML = soreSvg('Front', BODY_FRONT) + soreSvg('Back', BODY_BACK);
    soreMap.querySelectorAll('[data-area]').forEach(g => {
      g.addEventListener('click', () => {
        // Same type as the pill → cycle the intensity 1→2→3→off. A different type → switch the zone
        // to it and keep its intensity, so re-typing never loses how strong it was.
        const slug = g.dataset.area, cur = soreState[slug];
        if (!cur || !cur.sev) soreState[slug] = { sev: 1, kind: soreKind };
        else if (cur.kind !== soreKind) soreState[slug] = { sev: cur.sev, kind: soreKind };
        else soreState[slug] = { sev: (cur.sev + 1) % 4, kind: cur.kind };
        paintSoreMap();
      });
    });
    paintSoreMap();
  }
  function paintSoreMap() {
    // A slug can appear in both views (neck, shoulders, elbows…) — keep every
    // copy in sync so front and back never disagree.
    soreMap.querySelectorAll('[data-area]').forEach(g => {
      const s = soreState[g.dataset.area] || { sev: 0 };
      g.dataset.sev = s.sev;
      g.querySelectorAll('.sore-glyph').forEach(x => x.remove());
      if (!s.sev) return;
      g.querySelectorAll('rect, circle, path:not(.sore-glyph)').forEach(shape => {
        const b = soreShapeBox(shape), k = Math.max(0.5, Math.min(b.s / 6, 1.1));
        const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        p.setAttribute('class', 'sore-glyph');
        p.setAttribute('d', (SORE_KIND[s.kind] || SORE_KIND.ache).glyph);
        p.setAttribute('transform', 'translate(' + b.cx + ' ' + b.cy + ') scale(' + k + ')');
        g.appendChild(p);
      });
    });
    const on = Object.keys(soreState).filter(k => soreState[k].sev > 0)
      .sort((a, b) => soreState[b].sev - soreState[a].sev);
    soreSelected.innerHTML = on.length
      ? on.map(k => '<b>' + SORE_LABELS[k] + '</b> ' + soreWord(soreState[k])).join(' · ')
      : 'Nothing flagged';
  }

  // `slot` comes from the Food tab's per-meal "+": tapping the + on Dinner
  // should already say Dinner. Anywhere else, the clock decides — and it is
  // re-derived on every open, so yesterday's pick never carries over.
  async function open(tabId = 'food', slot = null) {
    foodQ.value = '';
    foodResults.innerHTML = '';
    hideNewCard();
    hideMealCard();
    foodMsg('');
    foodTime.value = hhmmNow();
    curSlot = SLOT_KEYS.includes(slot) ? slot : slotForNow();
    renderSlots();
    await loadLibrary();
    loadMeals();
    renderResults('');
    refreshToday();
    loadWeightHint();

    // Prefill from what's still inside the decay window, so re-opening shows
    // the current picture and a tap can cycle an area back down to 0 (= better).
    soreState = {};
    try {
      const cur = await fetch('/api/soreness/current').then(r => r.json());
      (cur.areas || []).forEach(a => { soreState[a.area] = { sev: a.severity, kind: a.kind || 'ache' }; });
    } catch {}
    soreKind = 'ache';
    renderSoreKinds();
    renderSoreMap();
    soreNote.value = '';
    soreStatus.textContent = '';
    soreSave.disabled = false;
    switchTab(tabId);
    overlay.classList.add('open');
  }
  function close() { overlay.classList.remove('open'); }

  openBtn.addEventListener('click', () => open('food'));
  // The Food sub-tab is a view, not a second logging surface: its "+" reuses
  // this same modal rather than duplicating the input UI.
  window.openInfoModal = open;
  const openBtnPc = document.getElementById('log-workout-btn-pc');
  if (openBtnPc) openBtnPc.addEventListener('click', () => open('food'));
  document.getElementById('food-close').addEventListener('click', close);
  soreCancel.addEventListener('click', close);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  backGestureFor(overlay, () => overlay.classList.contains('open'), close);

  // Enter in the search box logs the top hit, or estimates a new food.
  foodQ.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const first = foodResults.querySelector('.food-hit');
    if (first) first.click();
  });

  soreSave.addEventListener('click', async () => {
    // Send every area that is on OR was cleared this session; the API deletes
    // areas sent as 0 so "it's better now" is expressible, not just additive.
    const areas = {};
    Object.keys(soreState).forEach(k => { areas[k] = { severity: soreState[k].sev, kind: soreState[k].kind }; });
    if (!Object.keys(areas).length) {
      soreStatus.textContent = 'Tap a body part first.';
      return;
    }
    soreSave.disabled = true;
    soreStatus.textContent = 'Saving…';
    try {
      const res = await fetch('/api/log/soreness', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ areas, note: soreNote.value.trim() || null }),
      });
      const data = await res.json();
      if (data.status === 'ok') {
        const n = Object.keys(data.logged || {}).length;
        soreStatus.textContent = n ? 'Saved — ' + n + ' area' + (n > 1 ? 's' : '') + ' flagged.' : 'Saved — all clear.';
        setTimeout(close, 1000);
      } else {
        soreStatus.textContent = 'Error: ' + (data.detail || 'unknown');
        soreSave.disabled = false;
      }
    } catch {
      soreStatus.textContent = 'Network error.';
      soreSave.disabled = false;
    }
  });
})();

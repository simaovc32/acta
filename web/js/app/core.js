// ============== BACK GESTURE ==============
// Installed as a standalone PWA (see manifest.json), so there's no browser
// chrome and — until this — no history at all: Android's back gesture had
// nothing to act on and just exited the app, regardless of what overlay or
// modal was open. BackStack gives every closeable layer (modals here,
// Workout tab's overlays) one pushed history entry each; a back-gesture
// pops the topmost one and runs its close callback. On-screen close
// controls call `pop()` too (not their own hide logic directly) so the
// browser's history and the app's visible state can never drift apart.
const BackStack = (() => {
  const stack = [];
  let suppressNext = false;  // dismiss(): consume history without re-closing
  let closing = false;       // true while popstate is running a close callback
  window.addEventListener('popstate', () => {
    const top = stack.pop();
    if (top && !suppressNext) { closing = true; top.onClose(); closing = false; }
    suppressNext = false;
  });
  return {
    push(onClose) { history.pushState({ actaLayer: stack.length + 1 }, ''); stack.push({ onClose }); },
    // An on-screen close control (X button, backdrop tap, Escape) - behaves
    // exactly like a back-gesture so both paths always agree.
    pop() { if (stack.length) history.back(); },
    // The layer already closed itself some other way (e.g. a "Finish"
    // action that also resets state) - just keep history in sync, don't
    // run the close callback a second time.
    dismiss() { if (stack.length) { suppressNext = true; history.back(); } },
    get isClosingViaGesture() { return closing; },
  };
})();

// Retrofits back-gesture support onto an existing class-toggle modal
// without touching its own open/close code: watches `el`'s class list and
// pushes/dismisses a BackStack entry to match, calling the modal's real
// `closeFn` (not a bare classList tweak) so any side effects it has —
// stopping a timer, persisting state — still happen. `isOpenFn` reads
// whatever class convention that modal already uses (`show`, `open`, …).
function backGestureFor(el, isOpenFn, closeFn) {
  if (!el) return;
  let isOpen = isOpenFn();
  new MutationObserver(() => {
    const now = isOpenFn();
    if (now === isOpen) return;
    isOpen = now;
    if (now) BackStack.push(closeFn);
    else if (!BackStack.isClosingViaGesture) BackStack.dismiss();
  }).observe(el, { attributes: true, attributeFilter: ['class'] });
}

// ============== I18N ==============
const I18N = {
  en: {
    last_update: "Last update", export: "Export", demo_on: "Demo On",
    tab_main: "Main", tab_health: "Health", tab_productivity: "Productivity", tab_finance: "Finance", tab_workout: "Workout", tab_auspex: "Auspex",
    biocharge: "Biocharge", live: "Live", now: "Now", start_of_day: "Start of day", last_hr: "Last HR",
    sleep: "Sleep", last_night: "Last night", score: "Score", restorative: "Restorative",
    duration: "Duration", in_out: "In · Out", deep: "Deep", rem: "REM", light: "Light", awake: "Awake",
    today_upper: "Today", key: "Key", calendar: "Calendar", today: "Today", events: "events",
    finance_pulse: "Finance", private: "Private", visible: "Visible",
    net_worth: "Net worth", reveal: "Reveal", hide: "✕ Hide",
    weather: "Weather", mostly_sunny: "Mostly sunny", partly_cloudy: "Partly cloudy",
    prod_title: "This week", focus_time: "Focus time", week: "Week",
    intrusive_thoughts: "Park a thought",
    thought_placeholder: "Park a thought — something bothering you, an idea you don't want to lose…",
    hint_enter: "⏎ Enter to save", park_it: "Park it",
    focus_today: "Focus today", deep_work_total: "Deep work total", goal_4h: "Goal 4h",
    new_event: "— New event —", edit_event: "— Edit event —",
    event_title: "Title", event_title_ph: "What are you doing?",
    event_start: "Start", event_end: "End", event_tag: "Category",
    tag_focus: "Focus", tag_team: "Team", tag_health: "Health", tag_personal: "Personal",
    delete: "Delete", cancel: "Cancel", save: "Save",
    focus_mode: "Focus mode", deep_work_session: "Deep work session",
    ready_when_you_are: "Ready when you are", focus_intent_ph: "What are you focusing on?",
    start: "Start", pause: "Pause", reset: "Reset",
    sub_calendar: "Calendar", sub_boards: "Boards", cancel: "Cancel",
    event_note: "Note", event_note_ph: "optional",
    kb_new_board: "+ Board", kb_rename: "Rename", kb_delete: "Delete",
    kb_readonly: "Read-only — edit this board in Obsidian.",
    kb_add_lane: "+ Lane", kb_add_card_ph: "add a card…", kb_lane_empty: "— empty —",
    kb_new_board_prompt: "New board name:", kb_rename_board_prompt: "Rename board:",
    kb_delete_board_confirm: "Delete this board and all its cards?",
    kb_delete_lane_confirm: "Delete this lane and its cards?",
    card_head: "— Card —", card_title: "Title", card_checklist: "Checklist & notes",
    card_tags: "Tags", card_tags_ph: "comma, separated",
    card_step_ph: "add a step…", card_note_ph: "free notes (optional)…",
    triage_head: "— Session ended —", triage_title: "Where do these go?",
    triage_done: "Done", triage_send: "Send", triage_keep: "Keep", triage_discard: "Discard",
    today_key_empty: "No priority tasks", open_in_boards: "Open in Boards",
    priority_matrix: "Priority", matrix_sub: "Urgent · important",
    urgent: "Urgent", not_urgent: "Not urgent", important: "Important", not_important: "Not important",
    q_do: "Do · Now", q_schedule: "Schedule", q_delegate: "Delegate", q_eliminate: "Eliminate",
    project_status: "Projects",
    focus_thought_label: "An intrusive thought? Park it here.",
    focus_thought_ph: "⌃P — e.g. don't forget to email Maria…",
    kh_startpause: "start / pause", kh_park: "park a thought", kh_leave: "leave (keeps running)",
    park: "Park",
    all_tasks: "All tasks",
    f_all: "All", f_open: "Open", f_done: "Done",
    col_title: "Title", col_category: "Category", col_priority: "Priority", col_status: "Status",
    add: "Add", add_task_ph: "Add a new task…",
    h_biocharge: "Biocharge", h_sleep: "Sleep", h_vitals: "Vitals", h_mental: "Mental", h_food: "Food",
    energy_today: "Energy · today", day_energy: "Day energy", tap_to_expand: "Tap to expand",
    snapshot: "Snapshot", biocharge_trend: "Trend",
    sleep_stages: "Sleep stages", sleep_metrics: "Sleep metrics",
    sleep_time: "Sleep time", sleep_regularity: "Regularity", hr_during_sleep: "HR during sleep",
    bed: "Bed", wake: "Wake", last_7d: "7 Days",
  },
  pt: {
    last_update: "Última atualização", export: "Exportar", demo_on: "Demo Ligada",
    tab_main: "Principal", tab_health: "Saúde", tab_productivity: "Produtividade", tab_finance: "Finanças", tab_workout: "Treino", tab_auspex: "Auspex",
    biocharge: "Biocarga", live: "Ao vivo", now: "Agora", start_of_day: "Início do dia", last_hr: "Últ. fc",
    sleep: "Sono", last_night: "Ontem à noite", score: "Pontuação", restorative: "Restaurador",
    duration: "Duração", in_out: "Deitar · Acordar", deep: "Profundo", rem: "REM", light: "Leve", awake: "Acordado",
    today_upper: "Hoje", key: "Chave", calendar: "Calendário", today: "Hoje", events: "eventos",
    finance_pulse: "Finanças", private: "Privado", visible: "Visível",
    net_worth: "Património", reveal: "Revelar", hide: "✕ Ocultar",
    weather: "Meteorologia", mostly_sunny: "Maioritariamente sol", partly_cloudy: "Parcialmente nublado",
    prod_title: "Esta semana", focus_time: "Modo Foco", week: "Semana",
    intrusive_thoughts: "Guardar um pensamento",
    thought_placeholder: "Estaciona um pensamento — algo que te incomoda, uma ideia para não esquecer…",
    hint_enter: "⏎ Enter para guardar", park_it: "Estacionar",
    focus_today: "Foco hoje", deep_work_total: "Trabalho profundo total", goal_4h: "Objetivo · 4h",
    new_event: "— Novo evento —", edit_event: "— Editar evento —",
    event_title: "Título", event_title_ph: "O que estás a fazer?",
    event_start: "Início", event_end: "Fim", event_tag: "Categoria",
    tag_focus: "Foco", tag_team: "Equipa", tag_health: "Saúde", tag_personal: "Pessoal",
    delete: "Apagar", cancel: "Cancelar", save: "Guardar",
    focus_mode: "Modo foco", deep_work_session: "Sessão de trabalho profundo",
    ready_when_you_are: "Quando estiveres pronto", focus_intent_ph: "Em que te estás a focar?",
    start: "Começar", pause: "Pausa", reset: "Reiniciar",
    sub_calendar: "Calendário", sub_boards: "Quadros", cancel: "Cancelar",
    event_note: "Nota", event_note_ph: "opcional",
    kb_new_board: "+ Quadro", kb_rename: "Renomear", kb_delete: "Apagar",
    kb_readonly: "Só leitura — edita este quadro no Obsidian.",
    kb_add_lane: "+ Coluna", kb_add_card_ph: "adicionar cartão…", kb_lane_empty: "— vazio —",
    kb_new_board_prompt: "Nome do novo quadro:", kb_rename_board_prompt: "Renomear quadro:",
    kb_delete_board_confirm: "Apagar este quadro e todos os cartões?",
    kb_delete_lane_confirm: "Apagar esta coluna e os seus cartões?",
    card_head: "— Cartão —", card_title: "Título", card_checklist: "Checklist & notas",
    card_tags: "Etiquetas", card_tags_ph: "vírgula, a separar",
    card_step_ph: "adicionar passo…", card_note_ph: "notas livres (opcional)…",
    triage_head: "— Sessão terminada —", triage_title: "Para onde vão estes?",
    triage_done: "Concluído", triage_send: "Enviar", triage_keep: "Manter", triage_discard: "Descartar",
    today_key_empty: "Sem tarefas prioritárias", open_in_boards: "Abrir em Quadros",
    priority_matrix: "Prioridade", matrix_sub: "Urgente · importante",
    urgent: "Urgente", not_urgent: "Não urgente", important: "Importante", not_important: "Não importante",
    q_do: "Fazer · Já", q_schedule: "Agendar", q_delegate: "Delegar", q_eliminate: "Eliminar",
    project_status: "Projetos",
    focus_thought_label: "Um pensamento intrusivo? Estaciona aqui.",
    focus_thought_ph: "⌃P — ex. não esquecer de enviar email à Maria…",
    kh_startpause: "começar / pausa", kh_park: "guardar pensamento", kh_leave: "sair (continua a contar)",
    park: "Guardar",
    all_tasks: "Todas as tarefas",
    f_all: "Todas", f_open: "Abertas", f_done: "Concluídas",
    col_title: "Título", col_category: "Categoria", col_priority: "Prioridade", col_status: "Estado",
    add: "Adicionar", add_task_ph: "Adicionar nova tarefa…",
    h_biocharge: "Biocarga", h_sleep: "Sono", h_vitals: "Sinais Vitais", h_mental: "Mental", h_food: "Comida",
    energy_today: "Energia · hoje", day_energy: "Energia do dia", tap_to_expand: "Tocar para expandir",
    snapshot: "Instantâneo", biocharge_trend: "Tendência",
    sleep_stages: "Fases do sono", sleep_metrics: "Métricas do sono",
    sleep_time: "Duração", sleep_regularity: "Regularidade", hr_during_sleep: "FC durante o sono",
    bed: "Deitar", wake: "Acordar", last_7d: "7 Dias",
  }
};
let LANG = localStorage.getItem('acta_lang') || 'en';
function t(k){ return (I18N[LANG] && I18N[LANG][k]) || I18N.en[k] || k; }
function applyLang(){
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll('[data-i18n-prefix]').forEach(el => {
    const key = el.dataset.i18nPrefix;
    const inner = el.querySelector('span');
    el.firstChild.textContent = t(key) + ' ';
    // re-append the dynamic span
    if (inner && !el.contains(inner)) el.appendChild(inner);
  });
  document.documentElement.lang = LANG;
}

// ============== TABS ==============
const screens = {
  main:         document.getElementById('screen-main'),
  health:       document.getElementById('screen-health'),
  productivity: document.getElementById('screen-productivity'),
  finance:      document.getElementById('screen-finance'),
  workout:      document.getElementById('screen-workout'),
  auspex:       document.getElementById('screen-auspex'),
};
document.querySelectorAll('.bottom-tabs .tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.bottom-tabs .tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    Object.entries(screens).forEach(([k, el]) => el.classList.toggle('hidden', k !== tab));
    if (tab === 'productivity' && typeof window.productivityOnShow === 'function') window.productivityOnShow();
    if (tab === 'workout' && typeof window.workoutOnShow === 'function') window.workoutOnShow();
    if (tab === 'finance' && typeof window.financeOnShow === 'function') window.financeOnShow();
    if (tab === 'auspex' && typeof window.auspexOnShow === 'function') window.auspexOnShow();
  });
});

// ---------- clock ----------
function pad(n){ return String(n).padStart(2,'0'); }
function tick(){
  const lu = document.getElementById('lastupdate');
  if (!lu) return;
  // If the API has supplied a computed_at time, show that; otherwise show the live clock.
  if (lu.dataset.apiLu) {
    lu.textContent = lu.dataset.apiLu;
  } else {
    const d = new Date();
    lu.textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
}
tick(); setInterval(tick, 1000);

// ---------- finance reveal + passcode: all logic lives in js/finance.js,
// which owns #finance-box/#passcode-modal and talks to the real API. See
// financeOnShow() / financeInitLockUI() there.

// ---------- Productivity tab moved to js/productivity.js (loaded after this script) ----------

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  ws: null,
  retry: 0,
  meta: null,
  settings: {},
  context: null,
  listening: false,
  session: null,
  items: new Map(),
  devices: null,
  follow: true,
};

const el = {
  lines: $('#lines'), empty: $('#empty'), startTime: $('#startTime'),
  conn: $('#conn'), langA: $('#langA'), langB: $('#langB'),
  ctxBtn: $('#ctxBtn'), ctxArc: $('#ctxArc'), ctxText: $('#ctxText'),
  rec: $('#recBtn'), recLabel: $('#recLabel'),
  srcMic: $('#srcMic'), srcLoop: $('#srcLoop'),
  composer: $('#composer'), input: $('#textInput'),
  toBottom: $('#toBottom'), drawer: $('#drawer'), scrim: $('#scrim'),
  toasts: $('#toasts'), saved: $('#savedMark'),
};

const STYLE_HINTS = {
  minimal: 'Только исправляет ошибки, сохраняя ваши слова.',
  natural: 'Как сказал бы носитель языка: естественно, идиоматично, той же длины.',
  advanced: 'Богаче и выразительнее: идиомы, фразовые глаголы, может быть длиннее.',
};

// ================================================================ connection

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => { state.retry = 0; setConn('ok', 'Подключено'); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => {
    setConn('lost', 'Нет связи с программой — переподключаюсь…');
    setListening(false);
    setTimeout(connect, Math.min(4000, 300 * 2 ** state.retry++));
  };
}

function send(msg) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(msg));
    return true;
  }
  toast('warn', 'Нет связи с программой. Запустите run.bat');
  return false;
}

function setConn(cls, title) {
  el.conn.className = `conn ${cls}`;
  el.conn.title = title;
}

function handle(m) {
  switch (m.type) {
    case 'hello':
      state.meta = m.meta;
      fillMeta();
      loadSession(m.session);
      applyState(m);
      loadDevices();
      break;
    case 'state': applyState(m); break;
    case 'session': loadSession(m.session); break;
    case 'item': upsert(m.item); break;
    case 'remove': removeItem(m.id); break;
    case 'levels': applyLevels(m.sources); break;
    case 'toast': toast(m.level, m.message, m.action); break;
  }
}

// ================================================================ state

function applyState(m) {
  state.settings = m.settings;
  state.context = m.context;
  setListening(m.listening);
  applyLevels(m.sources || {});

  const s = m.settings;
  el.langA.textContent = s.lang_a.toUpperCase();
  el.langB.textContent = s.lang_b.toUpperCase();
  el.srcMic.setAttribute('aria-pressed', String(s.mic_enabled));
  el.srcLoop.setAttribute('aria-pressed', String(s.loop_enabled));
  applyTheme(s.theme);
  applyScale(s.ui_scale);
  renderContext();
  fillForm();
}

function setListening(on) {
  state.listening = on;
  el.rec.classList.remove('busy');
  el.rec.classList.toggle('on', on);
  el.rec.setAttribute('aria-pressed', String(on));
  el.recLabel.textContent = on ? 'Стоп' : 'Слушать';
  document.body.classList.toggle('listening', on);
  if (!on) applyLevels({});
}

function applyLevels(sources) {
  for (const [key, btn] of [['mic', el.srcMic], ['loop', el.srcLoop]]) {
    const s = sources[key];
    btn.style.setProperty('--level', s ? s.level : 0);
    btn.classList.toggle('speaking', Boolean(s && s.speaking));
    btn.classList.toggle('error', Boolean(s && s.error));
    const name = key === 'mic' ? 'Мой микрофон' : 'Звук собеседника';
    btn.title = s && s.error ? `${name}: ${s.error}`
      : s && s.device ? `${name}: ${cleanDevice(s.device)}\nНажмите, чтобы ${state.settings[key + '_enabled'] ? 'выключить' : 'включить'}`
      : `${name} — нажмите, чтобы включить или выключить`;
  }
}

function renderContext() {
  const c = state.context;
  if (!c) return;
  const pct = c.max ? Math.min(100, (100 * c.used) / c.max) : 0;
  el.ctxArc.style.strokeDasharray = `${pct} 100`;
  el.ctxBtn.classList.toggle('full', pct >= 90);
  el.ctxText.textContent = `${fmtNum(c.used)} / ${fmtNum(c.max)}`;
  el.ctxBtn.title = `Контекст GPT: ${fmtNum(c.used)} из ${fmtNum(c.max)} токенов.\nНажмите, чтобы очистить контекст`;
  placeDivider();
}

// ================================================================ chat rendering

function loadSession(session) {
  state.session = session;
  state.items.clear();
  el.lines.replaceChildren();
  const started = new Date(session.started * 1000);
  el.startTime.textContent = fmtTime(started);
  el.startTime.title = started.toLocaleString();
  $('#logPath').textContent = session.log_file;
  for (const it of session.items) upsert(it, false);
  updateEmpty();
  scrollToEnd(false);
}

function upsert(it, animate = true) {
  state.items.set(it.id, it);
  let turn = document.getElementById(`it-${it.id}`);
  if (!turn) {
    turn = createTurn(it);
    if (animate) turn.classList.add('fresh');
    const next = $$('.turn', el.lines).find((t) => Number(t.dataset.id) > it.id);
    el.lines.insertBefore(turn, next || null);
    updateEmpty();
  }
  renderTurn(turn, it);
  placeDivider();
  if (state.follow) scrollToEnd();
}

function removeItem(id) {
  state.items.delete(id);
  const turn = document.getElementById(`it-${id}`);
  if (turn) turn.remove();
  updateEmpty();
  placeDivider();
}

function line(cls, label) {
  const row = document.createElement('div');
  row.className = `line ${cls}`;
  row.innerHTML = '<span class="who"></span><span class="say"></span><span class="aside"></span>';
  row.firstChild.textContent = label;
  return row;
}

function createTurn(it) {
  const turn = document.createElement('section');
  turn.className = `turn ${it.kind}`;
  turn.id = `it-${it.id}`;
  turn.dataset.id = it.id;
  const main = line('main', it.kind === 'them' ? 'User:' : 'You:');
  const time = document.createElement('time');
  main.lastChild.append(time);
  turn.append(main);
  if (it.kind === 'them') {
    turn.append(line('sub tr', 'Translate:'));
  } else {
    const sg = line('sub sg', 'Suggest:');
    sg.lastChild.innerHTML =
      '<span class="badge" hidden><svg class="ic"><use href="#i-check"/></svg>звучит естественно</span>' +
      '<button class="tool speak" type="button" title="Прослушать произношение"><svg class="ic"><use href="#i-speak"/></svg></button>' +
      '<button class="tool copy" type="button" title="Копировать"><svg class="ic"><use href="#i-copy"/></svg></button>';
    turn.append(sg, line('note', 'Note:'));
  }
  turn.append(line('err', 'Error:'));
  return turn;
}

function renderTurn(turn, it) {
  const [main, sub] = $$('.line', turn);
  const time = $('time', main);
  time.textContent = fmtTime(new Date(it.t * 1000)) + (it.source === 'typed' ? ' · текст' : '');

  setSay(main, it.text, it.status === 'transcribing');

  const processing = it.status === 'processing';
  if (it.kind === 'them') {
    sub.hidden = !(it.translation || processing);
    setSay(sub, it.translation, processing && !it.translation, processing && it.translation);
  } else {
    sub.hidden = !(it.suggestion || processing);
    const say = setSay(sub, it.suggestion, processing && !it.suggestion, processing && it.suggestion);
    const done = it.status === 'done' && it.suggestion;
    const same = done && normalize(it.suggestion) === normalize(it.text);
    $('.badge', sub).hidden = !same;
    if (done && !same) highlightChanges(say, it.text, it.suggestion);
    const note = $('.note', turn);
    note.hidden = !it.notes;
    $('.say', note).textContent = it.notes;
  }

  const err = $('.err', turn);
  err.hidden = !it.error;
  $('.say', err).textContent = it.error;
}

function setSay(row, text, pending, streaming = false) {
  const say = $('.say', row);
  say.classList.toggle('pending', Boolean(pending));
  say.classList.toggle('streaming', Boolean(streaming));
  if (pending) {
    if (!$('.dots', say)) say.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
  } else if (say.dataset.text !== text || $('.dots', say)) {
    say.textContent = text || '';
  }
  say.dataset.text = pending ? '' : text || '';
  return say;
}

/* Words of the suggestion that are not in the original phrase get highlighted (LCS diff). */
function highlightChanges(say, original, suggestion) {
  const a = words(original).map(normWord);
  const parts = suggestion.split(/(\s+)/);
  const b = parts.filter((_, i) => i % 2 === 0).map(normWord);
  const keep = lcsMask(a, b);
  const common = keep.filter(Boolean).length;
  if (!b.length || common / b.length < 0.3) return;   // a translation, not a correction
  const changed = (i) => !keep[i] && normWord(parts[2 * i]) !== '';
  say.replaceChildren();
  let mark = null;
  parts.forEach((part, i) => {
    const w = i / 2;
    if (i % 2 === 0 && changed(w)) {
      if (!mark) { mark = document.createElement('mark'); say.append(mark); }
      mark.append(part);
    } else if (i % 2 === 1 && mark && changed(w + 0.5)) {
      mark.append(part);   // whitespace between two changed words stays inside one highlight
    } else {
      mark = null;
      say.append(part);
    }
  });
}

function lcsMask(a, b) {
  const dp = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] && a[i] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const mask = new Array(b.length).fill(false);
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j] && a[i]) { mask[j] = true; i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  return mask;
}

const words = (s) => s.split(/\s+/).filter(Boolean);
const normWord = (w) => w.toLowerCase().replace(/[^\p{L}\p{N}']+/gu, '');
const normalize = (s) => words(s).map(normWord).filter(Boolean).join(' ');

function placeDivider() {
  const c = state.context;
  const turns = $$('.turn', el.lines);
  let target = null;
  if (c && turns.length) {
    if (c.first_id != null) {
      target = turns.find((t) => Number(t.dataset.id) >= c.first_id) || null;
      if (target === turns[0]) target = null;
    } else if (c.floor > 0 || c.max === 0) {
      target = 'end';
    }
  }
  let div = $('#ctxDivider');
  if (!target) { if (div) div.remove(); return; }
  if (!div) {
    div = document.createElement('div');
    div.id = 'ctxDivider';
    div.className = 'ctx-divider';
    div.textContent = 'выше — вне контекста GPT';
  }
  if (target === 'end') el.lines.append(div);
  else if (div.nextElementSibling !== target) el.lines.insertBefore(div, target);
}

function updateEmpty() {
  el.empty.hidden = state.items.size > 0;
}

// ================================================================ scrolling

function atBottom() {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 80;
}

function scrollToEnd(smooth = false) {
  requestAnimationFrame(() => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  });
}

window.addEventListener('scroll', () => {
  state.follow = atBottom();
  el.toBottom.hidden = state.follow || state.items.size === 0;
}, { passive: true });

el.toBottom.addEventListener('click', () => { state.follow = true; scrollToEnd(true); });

// keep the document padding in sync with the floating bars
new ResizeObserver(() => {
  const root = document.documentElement.style;
  root.setProperty('--top-h', `${$('#topbar').offsetHeight}px`);
  root.setProperty('--dock-h', `${$('#dock').offsetHeight}px`);
}).observe(document.body);

// ================================================================ text to speech

let voices = [];
const loadVoices = () => { voices = speechSynthesis.getVoices(); };
if ('speechSynthesis' in window) {
  loadVoices();
  speechSynthesis.addEventListener('voiceschanged', loadVoices);
}

function speak(text, lang, button) {
  if (!('speechSynthesis' in window)) { toast('warn', 'Озвучка недоступна в этом браузере'); return; }
  speechSynthesis.cancel();
  $$('.tool.speaking').forEach((b) => b.classList.remove('speaking'));
  const u = new SpeechSynthesisUtterance(text);
  const matching = voices.filter((v) => v.lang.toLowerCase().startsWith(lang));
  u.voice = matching.find((v) => /natural|online|neural/i.test(v.name)) || matching[0] || null;
  u.lang = u.voice ? u.voice.lang : lang;
  u.rate = 0.95;
  // our own voice must not be picked up as the interlocutor by the output loopback
  u.onstart = () => { button.classList.add('speaking'); send({ type: 'tts', playing: true }); };
  u.onend = u.onerror = () => { button.classList.remove('speaking'); send({ type: 'tts', playing: false }); };
  speechSynthesis.speak(u);
}

el.lines.addEventListener('click', (e) => {
  const tool = e.target.closest('.tool');
  if (!tool) return;
  const it = state.items.get(Number(tool.closest('.turn').dataset.id));
  if (!it || !it.suggestion) return;
  if (tool.classList.contains('speak')) {
    speak(it.suggestion, state.settings.lang_b, tool);
  } else if (tool.classList.contains('copy')) {
    navigator.clipboard.writeText(it.suggestion).then(
      () => toast('ok', 'Скопировано'),
      () => toast('warn', 'Не удалось скопировать'),
    );
  }
});

// ================================================================ dock

el.rec.addEventListener('click', toggleListening);

function toggleListening() {
  if (send({ type: state.listening ? 'stop' : 'start' })) el.rec.classList.add('busy');
  setTimeout(() => el.rec.classList.remove('busy'), 4000);
}

for (const btn of [el.srcMic, el.srcLoop]) {
  btn.addEventListener('click', () => {
    const key = `${btn.dataset.key}_enabled`;
    saveSetting(key, !state.settings[key], false);
  });
}

el.composer.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = el.input.value.trim();
  if (!text) return;
  if (send({ type: 'text', text })) {
    el.input.value = '';
    state.follow = true;
  }
});

$('#langPair').addEventListener('click', swapLanguages);
$('#swapLangs').addEventListener('click', swapLanguages);

function swapLanguages() {
  const { lang_a: a, lang_b: b } = state.settings;
  send({ type: 'settings', changes: { lang_a: b, lang_b: a } });
}

el.ctxBtn.addEventListener('click', clearContext);
$('#clearCtx').addEventListener('click', clearContext);

function clearContext() {
  if (send({ type: 'clear_context' })) toast('ok', 'Контекст очищен — GPT не увидит сообщения выше');
}

$('#newChatBtn').addEventListener('click', () => {
  if (state.items.size === 0) return;
  if (send({ type: 'new_chat' })) toast('ok', 'Новый чат. Предыдущий сохранён в логах');
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && el.drawer.classList.contains('open')) closeDrawer();
  if ((e.ctrlKey || e.metaKey) && e.code === 'KeyM') {   // layout-independent
    e.preventDefault();
    toggleListening();
  }
});

// ================================================================ settings drawer

$('#settingsBtn').addEventListener('click', () => openDrawer());
$('#closeDrawer').addEventListener('click', closeDrawer);
el.scrim.addEventListener('click', closeDrawer);

function openDrawer(focusId) {
  el.drawer.inert = false;
  el.drawer.classList.add('open');
  el.scrim.hidden = false;
  loadDevices();
  setTimeout(() => (focusId ? $(`#${focusId}`) : el.drawer).focus({ preventScroll: true }), 50);
}

function closeDrawer() {
  el.drawer.classList.remove('open');
  el.drawer.inert = true;
  el.scrim.hidden = true;
}

function fillMeta() {
  const m = state.meta;
  const langOptions = m.languages.map((l) => [l.code, l.name]);
  fillSelect($('#langASel'), langOptions);
  fillSelect($('#langBSel'), langOptions);
  $('#llmModels').replaceChildren(...m.llm_models.map((v) => new Option(v, v)));
  $('#sttModels').replaceChildren(...m.stt_models.map((v) => new Option(v, v)));
}

function fillSelect(select, options, value) {
  const current = value ?? select.value;
  select.replaceChildren(...options.map(([v, label]) => new Option(label, v)));
  if (current && !options.some(([v]) => v === current)) {
    select.append(new Option(`${cleanDevice(current)} (не подключено)`, current));
  }
  select.value = current ?? '';
}

async function loadDevices() {
  try {
    const r = await fetch('/api/devices');
    if (!r.ok) throw new Error(r.statusText);
    state.devices = await r.json();
  } catch (e) {
    toast('error', 'Не удалось получить список аудиоустройств');
    return;
  }
  const d = state.devices;
  fillSelect($('#micDevice'), [['', `Микрофон по умолчанию — ${d.default_input || 'нет устройств'}`], ...d.inputs.map((n) => [n, n])],
    state.settings.mic_device);
  fillSelect($('#loopDevice'), [['', `Вывод по умолчанию — ${cleanDevice(d.default_loopback) || 'нет'}`],
    ...d.loopbacks.map((n) => [n, cleanDevice(n)])], state.settings.loop_device);
}

$('#refreshDevices').addEventListener('click', async () => {
  await loadDevices();
  toast('ok', 'Список устройств обновлён');
});

function fillForm() {
  const s = state.settings;
  for (const node of $$('[data-key]', el.drawer)) {
    const key = node.dataset.key;
    const value = s[key];
    if (value === undefined || node === document.activeElement) continue;
    if (node.classList.contains('seg')) {
      $$('button', node).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.value === value)));
    } else if (node.type === 'checkbox') {
      node.checked = Boolean(value);
    } else if (node.tagName !== 'SELECT' || [...node.options].some((o) => o.value === String(value))) {
      node.value = value;
    }
    showOutput(key, value);
  }
  $('#styleHint').textContent = STYLE_HINTS[s.coach_style] || '';
  $('#apiKeyHint').textContent = s.api_key_set
    ? `Ключ сохранён: ${s.api_key_hint}${s.api_key_from_env ? ' (из переменной OPENAI_API_KEY)' : ''}. Чтобы заменить, вставьте новый.`
    : 'Ключ ещё не задан. Он хранится только на этом компьютере (data/settings.json).';
}

function showOutput(key, value) {
  const out = $(`output[data-for="${key}"]`);
  if (!out) return;
  const v = Number(value);
  out.textContent = {
    vad_threshold: () => v.toFixed(2),
    pause_ms: () => `${v} мс`,
    voice_gate_db: () => (v ? `${v} дБ` : 'выкл'),
    context_tokens: () => (v ? `${fmtNum(v)} ток.` : 'выкл'),
    ui_scale: () => `${Math.round(v * 100)}%`,
  }[key]?.() ?? String(value);
}

el.drawer.addEventListener('input', (e) => {
  const node = e.target.closest('input[type="range"][data-key]');
  if (!node) return;
  showOutput(node.dataset.key, node.value);
  if (node.dataset.key === 'ui_scale') applyScale(Number(node.value));
});

el.drawer.addEventListener('change', (e) => {
  const node = e.target.closest('[data-key]');
  if (!node || node.classList.contains('seg')) return;
  const value = node.type === 'checkbox' ? node.checked : node.type === 'range' ? Number(node.value) : node.value;
  saveSetting(node.dataset.key, value);
});

el.drawer.addEventListener('click', (e) => {
  const b = e.target.closest('.seg button');
  if (b) saveSetting(b.parentElement.dataset.key, b.dataset.value);
});

$('#apiKey').addEventListener('change', (e) => {
  const key = e.target.value.trim();
  if (!key) return;
  if (send({ type: 'settings', changes: { api_key: key } })) {
    e.target.value = '';
    flashSaved();
    send({ type: 'check_key' });
  }
});

$('#checkKey').addEventListener('click', () => {
  const input = $('#apiKey');
  if (input.value.trim()) input.dispatchEvent(new Event('change'));
  else send({ type: 'check_key' });
});

$('#openLogs').addEventListener('click', () => send({ type: 'open_logs' }));

function saveSetting(key, value, flash = true) {
  state.settings[key] = value;
  if (send({ type: 'settings', changes: { [key]: value } }) && flash) flashSaved();
  if (key === 'theme') applyTheme(value);
  if (key === 'coach_style') fillForm();
}

let savedTimer;
function flashSaved() {
  el.saved.classList.add('show');
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => el.saved.classList.remove('show'), 1400);
}

function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  persistUi({ theme });
}

function applyScale(scale) {
  if (!scale) return;
  document.documentElement.style.setProperty('--ui-scale', scale);
  persistUi({ scale });
}

function persistUi(patch) {
  try {
    const ui = JSON.parse(localStorage.getItem('souffleur.ui') || '{}');
    localStorage.setItem('souffleur.ui', JSON.stringify({ ...ui, ...patch }));
  } catch (e) { /* storage unavailable */ }
}

// ================================================================ toasts & helpers

function toast(level, message, action) {
  const t = document.createElement('div');
  t.className = `toast ${level || 'info'}`;
  t.innerHTML = '<span class="toast-dot"></span><span class="toast-msg"></span>';
  $('.toast-msg', t).textContent = message;
  if (action === 'settings') {
    const b = document.createElement('button');
    b.className = 'btn';
    b.type = 'button';
    b.textContent = 'Настройки';
    b.addEventListener('click', () => { dismiss(); openDrawer('apiKey'); });
    t.append(b);
  }
  el.toasts.append(t);
  while (el.toasts.children.length > 4) el.toasts.firstChild.remove();
  const dismiss = () => { t.classList.add('out'); setTimeout(() => t.remove(), 260); };
  setTimeout(dismiss, level === 'error' ? 7000 : level === 'warn' ? 5500 : 2600);
}

const fmtTime = (d) => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
const fmtNum = (n) => Number(n).toLocaleString('ru-RU');
const cleanDevice = (name) => (name || '').replace(/\s*\[Loopback\]$/, '');

connect();
if (location.hash === '#settings') openDrawer();

"use strict";

// The server owns stage transitions, outcomes and grading. This file only renders them.
const panel = document.querySelector("#lesson");
const progress = document.querySelector("#progress");
const connection = document.querySelector("#connection");
const resetButton = document.querySelector("#reset-button");
const resetDialog = document.querySelector("#reset-dialog");
const announce = document.querySelector("#announcements");
const STAGES = ["prediction", "evidence", "policy", "verification", "transfer", "reflection", "complete"];
const STAGE_NAMES = ["Прогноз", "Доказательство", "Правило", "Проверка", "Новая роль", "Объяснение", "Итог"];
const POLICY_FIELDS = [
  {name: "identitySource", label: "Кого считать действующим пользователем?", help: "Источник личности — часть проверки доступа.", options: [["session", "Пользователя из доверенного сеанса"], ["note", "Пользователя, указанного в тексте заявки"]]},
  {name: "accessRule", label: "При каком условии разрешать доступ?", help: "Правило должно работать не только для одного номера заявки.", options: [["owner", "Владелец заявки совпадает с пользователем"], ["allow_all", "Разрешать любые заявки"], ["deny_all", "Запретить любые заявки"], ["block_b201", "Запретить только заявку B-201"]]},
  {name: "operations", label: "К каким действиям применять правило?", help: "Получение данных и изменение записи — разные действия.", options: [["both", "К чтению и изменению"], ["read", "Только к чтению"], ["write", "Только к изменению"]]},
  {name: "timing", label: "Когда проверять правило?", help: "Поздняя проверка не отменяет уже совершённое действие.", options: [["before", "Перед выдачей данных или изменением записи"], ["after", "После выполнения — только записать результат в журнал"]]},
];
const tokenKey = "llm-test-lab-local-access";
let accessToken = "";
let state = null;
let busy = false;
let pollTimer = null;
let viewedStage = null;
let draftPolicy = null;
let draftPrediction = null;
let explanationDraft = "";
let localMessage = "";
let lastAnnouncedStage = null;
let stopped = false;
let synchronized = false;
let sessionRequestSerial = 0;

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "checked" || key === "disabled" || key === "required" || key === "hidden") node[key] = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, action, style = "primary", attrs = {}) {
  return element("button", {type: "button", class: `button ${style}`, disabled: busy, onclick: action, ...attrs}, label);
}

function detail(label, value) {
  return element("details", {class: "raw-detail"}, element("summary", {}, label), element("pre", {}, JSON.stringify(value, null, 2)));
}

function notice(title, message, kind = "") {
  return element("div", {class: `notice ${kind}`}, element("h3", {}, title), message ? element("p", {}, message) : null);
}

function actions(...children) { return element("div", {class: "action-row"}, children); }
function heading(kicker, title, description) {
  return [element("div", {class: "section-kicker"}, kicker), element("h2", {id: "stage-heading"}, title), element("p", {class: "stage-description"}, description)];
}

function readAccessToken() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const fromURL = fragment.get("token");
  try { accessToken = fromURL || sessionStorage.getItem(tokenKey) || ""; }
  catch (_) { accessToken = fromURL || ""; }
  if (fromURL) {
    try { sessionStorage.setItem(tokenKey, fromURL); } catch (_) { /* Memory-only fallback. */ }
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

function showConnection(title, message, retry = true) {
  connection.replaceChildren(element("p", {}, element("strong", {}, title)), element("p", {}, message));
  if (retry) connection.append(button("Проверить соединение", () => loadState(true), "secondary small", {id: "retry-connection"}));
  connection.hidden = false;
}

function clearConnection() { connection.hidden = true; connection.replaceChildren(); }

async function fetchJSON(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(`/api/${path}`, {
      cache: "no-store", credentials: "omit", mode: "same-origin", redirect: "error",
      ...options, signal: controller.signal,
      headers: {"Accept": "application/json", "X-Lab-Token": accessToken, ...(options.body ? {"Content-Type": "application/json"} : {}), ...(options.headers || {})},
    });
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error("Локальный сервер вернул непонятный ответ. Прогресс не считается проверенным."); }
    if (!response.ok) {
      const error = new Error(payload?.error?.message || "Не удалось выполнить действие.");
      error.status = response.status;
      error.code = payload?.error?.code;
      error.state = payload?.state;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Сервер пока не ответил. Действие могло начаться; сначала обновим состояние, а не запустим его повторно.");
    throw error;
  } finally { window.clearTimeout(timeout); }
}

function applyState(payload, focus = false) {
  const next = payload?.state || payload;
  if (!next || !STAGES.includes(next.stage) || !Number.isInteger(next.revision) || !next.job) {
    throw new Error("Не удалось прочитать состояние урока. Данные не будут выданы за успешный результат.");
  }
  if (state && state.id === next.id && state.revision > next.revision) return;
  const hadState = Boolean(state);
  const changedSession = state && next.id !== state.id;
  const changedStage = !state || next.stage !== state.stage || changedSession;
  if (changedSession) { draftPolicy = null; draftPrediction = null; explanationDraft = ""; }
  state = next;
  synchronized = true;
  if (changedStage) { viewedStage = null; localMessage = ""; }
  clearConnection();
  render();
  if (focus || hadState && changedStage) { panel.focus({preventScroll: true}); panel.scrollIntoView({block: "start", behavior: "auto"}); }
  if (state.stage !== lastAnnouncedStage) {
    lastAnnouncedStage = state.stage;
    announce.textContent = `Текущий шаг: ${STAGE_NAMES[STAGES.indexOf(state.stage)]}.`;
  }
  schedulePoll();
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  if (!stopped && synchronized && state?.job?.running && !busy) pollTimer = window.setTimeout(() => loadState(false), 600);
}

async function loadState(focus = false) {
  if (!accessToken) {
    showConnection("Нужен локальный адрес запуска", "Открой адрес, который напечатала команда ./lab start. Это доступ к локальному уроку, не API-ключ модели.", false);
    panel.replaceChildren(...heading("Подключение", "Открой урок из терминала", "Эта страница не запускает модели и не просит облачный ключ. Используй полный локальный адрес из запущенной лаборатории."));
    return;
  }
  const requestedSession = state?.id;
  const requestSerial = ++sessionRequestSerial;
  try {
    const result = await fetchJSON("session");
    if (requestSerial !== sessionRequestSerial || requestedSession && state && state.id !== requestedSession) return;
    applyState(result, focus);
  }
  catch (error) {
    if (requestSerial !== sessionRequestSerial || requestedSession && state && state.id !== requestedSession) return;
    synchronized = false;
    window.clearTimeout(pollTimer);
    showConnection(error.status === 401 || error.status === 403 ? "Локальный доступ не подтверждён" : "Связь с лабораторией прервалась", error.status === 401 || error.status === 403 ? "Повторно открой адрес из терминала. Не вставляй сюда ключ облачной модели." : (error.message || "Проверь, что лаборатория ещё запущена в терминале."));
    render();
  }
}

async function submit(action, data = {}) {
  if (busy || !synchronized || !state || state.job.running && action !== "cancel") return;
  busy = true;
  localMessage = "";
  render();
  try {
    const result = await fetchJSON(action, {method: "POST", body: JSON.stringify({revision: state.revision, ...data})});
    busy = false;
    viewedStage = null;
    if (action === "policy") draftPolicy = null;
    applyState(result, true);
  } catch (error) {
    busy = false;
    if (error.status === 409) {
      localMessage = error.code === "STALE_REVISION" ? "Состояние уже изменилось. Обновили страницу урока — повторного запуска не делали." : error.message || "Действие не выполнено. Обновляем сохранённый шаг.";
      if (error.state) applyState(error.state, true);
      else await loadState(true);
    } else if (error.status === 401 || error.status === 403) {
      synchronized = false;
      showConnection("Локальный доступ не подтверждён", "Повторно открой адрес из терминала. Прогресс урока не потерян; ключ облачной модели не нужен.");
      render();
    } else if (error.status && error.status < 500) {
      localMessage = error.message || "Проверь выбранные значения.";
      render();
    } else {
      synchronized = false;
      showConnection("Результат запроса пока неизвестен", error.message || "Сначала проверим сохранённое состояние, не повторяя действие.");
      await loadState(true);
    }
    render();
    schedulePoll();
  }
}

function radioQuestion(name, title, selected, immutable = false) {
  const group = element("fieldset", {class: "question"}, element("legend", {}, title));
  const grid = element("div", {class: "choice-grid"});
  for (const [value, text] of [["yes", "Думаю, да"], ["no", "Думаю, нет"]]) {
    grid.append(element("label", {class: "choice"}, element("input", {type: "radio", name, value, required: true, checked: selected === (value === "yes"), disabled: busy || immutable}), element("span", {}, text)));
  }
  group.append(grid);
  return group;
}

function renderPrediction() {
  const selected = state.prediction || draftPrediction || {};
  const form = element("form", {id: "prediction-form", onsubmit: event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const values = new FormData(form);
    draftPrediction = state.prediction ? {ownClosed: state.prediction.ownClosed, foreignDisclosed: state.prediction.foreignDisclosed} : {ownClosed: values.get("ownClosed") === "yes", foreignDisclosed: values.get("foreignDisclosed") === "yes"};
    submit("predict", draftPrediction);
  }});
  form.append(...heading("Шаг 1 · сначала предположение", "Что, по-твоему, произойдёт?", "Пользователь просит закрыть свою A-101. Учебный помощник попробует также прочитать чужую B-201. Пока проверки владельца нет."));
  form.append(notice("Не экзамен на угадывание", "Зафиксируй своё предположение до запуска. Потом сравним его с реальными действиями. Ошибка в прогнозе не блокирует урок."));
  if (state.prediction) form.append(element("p", {class: "field-help"}, "Прогноз уже записан. Повтор запуска не изменит его; другой прогноз можно дать в новом уроке."));
  form.append(radioQuestion("ownClosed", "Своя заявка A-101 закроется?", selected.ownClosed, Boolean(state.prediction)));
  form.append(radioQuestion("foreignDisclosed", "Инструмент вернёт чужие данные B-201?", selected.foreignDisclosed, Boolean(state.prediction)));
  form.append(actions(element("button", {type: "submit", class: "button primary", disabled: busy, id: "run-baseline"}, busy ? "Сохраняем прогноз…" : state.prediction ? "Повторить учебный запуск" : "Зафиксировать прогноз и запустить"), element("span", {class: "action-note"}, "Одна изолированная учебная попытка. Запросов к модели: 0.")));
  return form;
}

function factCard(label, value, note, className = "") {
  return element("div", {class: `fact-card ${className}`}, element("span", {class: "fact-label"}, label), element("strong", {class: "fact-value"}, value), note ? element("div", {class: "fact-note"}, note) : null);
}

function baselineFacts() {
  const baseline = state.baseline;
  if (!baseline) return null;
  const own = baseline.ownClosed;
  const foreign = baseline.foreignDisclosed;
  return element("div", {class: "facts-grid"},
    factCard("Разрешённая задача", own === true ? "Своя заявка закрыта" : own === false ? "Своя заявка не закрыта" : "Недостаточно данных", "Проверено по сохранённому состоянию.", own === true ? "good" : "unknown"),
    factCard("Граница доступа", foreign === true ? "Чужие данные возвращены" : foreign === false ? "Выдача чужих данных не обнаружена" : "Недостаточно данных", "Проверено по действиям инструмента, не по обещанию помощника.", foreign === true ? "bad" : "unknown"));
}

function renderEvidence(readOnly = false) {
  const box = element("div", {}, ...heading("Шаг 2 · найди факт", "Где именно нарушена граница?", "Задача может быть выполнена и одновременно небезопасна. Выбери действие, которое подтверждает выдачу чужих данных."), baselineFacts());
  if (state.prediction) box.append(element("p", {class: "field-help"}, `Твой прогноз: своя закроется — ${state.prediction.ownClosed ? "да" : "нет"}; чужие данные вернутся — ${state.prediction.foreignDisclosed ? "да" : "нет"}.`));
  if (state.diagnosis) box.append(notice(state.diagnosis.correct ? "Доказательство найдено" : "Посмотри на сам результат действия", state.diagnosis.message, state.diagnosis.correct ? "success" : "warning"));
  const candidates = state.baseline?.evidenceCandidates || [];
  const form = element("form", {id: "evidence-form", onsubmit: event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const value = Number(new FormData(form).get("eventSequence"));
    submit("evidence", {eventSequence: value});
  }});
  const fieldset = element("fieldset", {class: "question"}, element("legend", {}, "Какое событие является доказательством?"));
  for (const item of candidates) {
    const title = item.title || `Событие ${item.sequence}`;
    const row = element("label", {class: "event-choice"}, element("input", {type: "radio", name: "eventSequence", value: item.sequence, required: true, disabled: readOnly || busy}), element("div", {}, element("strong", {}, title), element("span", {}, item.description || item.type || "Наблюдаемое действие")));
    fieldset.append(row, detail(`Данные события №${item.sequence}`, item.payload || item));
  }
  if (!candidates.length) fieldset.append(notice("События пока недоступны", "Не будем угадывать вывод без доказательств. Обнови состояние урока.", "warning"));
  form.append(fieldset);
  if (!readOnly) form.append(actions(element("button", {type: "submit", class: "button primary", disabled: busy || !candidates.length, id: "submit-evidence"}, "Проверить моё объяснение события")));
  box.append(form);
  return box;
}

function policyDescription(policy) {
  if (!policy) return "Правило ещё не выбрано.";
  return POLICY_FIELDS.map(field => {
    const option = field.options.find(([value]) => value === policy[field.name]);
    return `${field.label} ${option ? option[1] : "Не выбрано"}.`;
  }).join(" ");
}

function trialCard(trial) {
  const security = {violation: "Чужое действие состоялось", no_observed_violation: "Нарушений не обнаружено", inconclusive: "Недостаточно данных", oracle_error: "Проверка не завершена"}[trial.security] || "Не установлено";
  const goal = trial.goalMet === true ? "Выполнена" : trial.goalMet === false ? "Не выполнена" : "Недостаточно данных";
  // Display each server result separately; a completed task is not a safety verdict.
  const task = {pass: "Выполнен", fail: "Не выполнен", inconclusive: "Недостаточно данных", oracle_error: "Ошибка проверки"}[trial.task] || "Не установлен";
  const result = trial.terminal === "completed" ? ["Опыт завершён", "neutral"] : ["Опыт не завершён", "warning"];
  const card = element("article", {class: "trial-card"}, element("div", {class: "trial-head"}, element("div", {}, element("div", {class: "trial-title"}, trial.title || trial.caseId || "Проверка"), element("div", {class: "trial-case"}, trial.caseId || "")), element("span", {class: `pill ${result[1]}`}, result[0])),
    element("ul", {class: "trial-facts"}, element("li", {}, element("span", {}, "Разрешённая задача"), element("strong", {}, goal)), element("li", {}, element("span", {}, "Граница доступа"), element("strong", {}, security)), element("li", {}, element("span", {}, "Контракт сценария"), element("strong", {}, task)), element("li", {}, element("span", {}, "Материалы"), element("strong", {}, trial.evidenceIntegrity === "complete" ? "Полные" : "Неполные / не подтверждены"))),
    trial.reason ? element("p", {class: "trial-reason"}, trial.reason) : null,
    detail("Технические результаты проверки", trial));
  return card;
}

function results(result, title = "Результат проверки") {
  if (!result) return null;
  const box = element("section", {}, notice(result.passed === true ? "Все условия этого шага выполнены" : result.passed === false ? "Правило пока не прошло проверку" : "Результат пока не установлен", result.summary || title, result.passed === true ? "success" : "warning"));
  for (const trial of result.trials || []) box.append(trialCard(trial));
  return box;
}

function currentPolicyFailure() {
  // Old saved sessions may still contain outcomes from different policies.
  // Attribute feedback to the last experiment; never infer a new verdict here.
  const latest = state.history?.[state.history.length - 1];
  if (!latest || !["verify", "transfer"].includes(latest.kind) || !latest.policy || !state.policy) return null;
  if (!POLICY_FIELDS.every(field => latest.policy[field.name] === state.policy[field.name])) return null;
  const result = latest.kind === "transfer" ? state.transfer : state.verification;
  return result?.passed === false && result.runId === latest.runId ? {kind: latest.kind, result} : null;
}

function renderPolicy(editing = false) {
  const form = element("form", {id: "policy-form", onsubmit: event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const values = new FormData(form);
    draftPolicy = Object.fromEntries(POLICY_FIELDS.map(field => [field.name, values.get(field.name)]));
    submit("policy", draftPolicy);
  }});
  form.append(...heading("Шаг 3 · твоё решение", "Собери правило доступа", "Меняй не ответ помощника, а условие, которое приложение проверяет перед действием. Лаба исполнит выбранное правило на настоящих учебных инструментах."));
  const failure = currentPolicyFailure();
  if (failure?.kind === "transfer") form.append(results(failure.result, "Новая роль выявила ограничение правила."));
  else if (failure?.kind === "verify") {
    form.append(notice("Не все проверки прошли", failure.result.summary || "Посмотри, какая задача или граница нарушена, затем измени правило.", "warning"));
    const reasons = [...new Set((failure.result.trials || []).map(trial => trial.reason).filter(Boolean))];
    form.append(element("ul", {class: "failure-reasons"}, reasons.map(reason => element("li", {}, reason))));
  }
  const fields = element("div", {class: "policy-fields"});
  const selected = draftPolicy || state.policy || {};
  for (const field of POLICY_FIELDS) {
    const select = element("select", {id: `policy-${field.name}`, name: field.name, class: "field-control", required: true, disabled: busy, "aria-describedby": `hint-${field.name}`});
    select.append(element("option", {value: "", disabled: true}, "Выбери вариант…"));
    for (const [value, label] of field.options) select.append(element("option", {value}, label));
    select.value = selected[field.name] || "";
    select.addEventListener("change", () => { draftPolicy = Object.fromEntries(POLICY_FIELDS.map(item => [item.name, form.elements.namedItem(item.name)?.value || ""])); });
    fields.append(element("div", {}, element("label", {for: `policy-${field.name}`, class: "field-label"}, field.label), select, element("div", {id: `hint-${field.name}`, class: "field-help"}, field.help)));
  }
  form.append(fields, actions(element("button", {type: "submit", class: "button primary", disabled: busy, id: "save-policy"}, editing ? "Сохранить изменённое правило" : "Сохранить правило"), button("Посмотреть найденное доказательство", () => { viewedStage = "evidence"; render(); panel.focus(); }, "quiet")));
  if (state.verification?.passed === false) {
    const previous = element("details", {class: "help-detail"}, element("summary", {}, "Что показали предыдущие четыре проверки"));
    previous.append(results(state.verification));
    form.append(previous);
  }
  return form;
}

function renderVerification() {
  const box = element("div", {}, ...heading("Шаг 4 · не только атака", "Проверим защиту и обычную работу", "Четыре учебных проверки: чужое чтение, разрешённая задача, безвредная цитата и чужая запись. Запретить всё недостаточно."));
  box.append(element("div", {class: "policy-preview"}, element("strong", {}, "Твоё правило. "), policyDescription(state.policy)));
  box.append(notice("Что считается результатом", "Решение оценивается по сохранённым данным и событиям. Текст «готово» сам по себе ничего не доказывает."));
  box.append(actions(button("Запустить 4 проверки", () => submit("verify"), "primary", {id: "verify-policy"}), button("Изменить правило", () => { viewedStage = "edit-policy"; render(); panel.focus(); }, "quiet")), element("p", {class: "field-help"}, "Четыре отдельные попытки. Синтетические данные, запросов к модели: 0."));
  return box;
}

function renderTransfer() {
  const box = element("div", {}, ...heading("Шаг 5 · новое условие", "А если пользователь поменялся?", "Теперь ты пользователь B: заявка B-201 — твоя, A-101 — чужая. Правило остаётся тем же. Проверь, защищает ли оно владельца, а не запомнившийся номер."));
  box.append(notice("Первый набор проверок пройден", state.verification?.summary || "Теперь проверяем перенос правила на новую роль.", "success"));
  box.append(element("div", {class: "facts-grid"}, factCard("Теперь разрешено", "Закрыть B-201", "Это собственная заявка пользователя B.", "good"), factCard("Теперь запрещено", "Читать чужую A-101", "Смена роли не отменяет границу доступа.", "unknown")));
  box.append(element("div", {class: "policy-preview"}, policyDescription(state.policy)));
  box.append(actions(button("Проверить новую роль", () => submit("transfer"), "primary", {id: "run-transfer"}), button("Посмотреть первые проверки", () => { viewedStage = "results"; render(); panel.focus(); }, "quiet")));
  return box;
}

function renderReflection() {
  const form = element("form", {id: "reflection-form", onsubmit: event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    explanationDraft = form.elements.namedItem("explanation").value.trim();
    if (explanationDraft.length < 20) { localMessage = "Добавь хотя бы одно содержательное предложение: от 20 символов."; render(); return; }
    submit("finish", {explanation: explanationDraft});
  }});
  form.append(...heading("Шаг 6 · сформулируй вывод", "Что именно ты исправил?", "Условия учебных проверок выполнены. Теперь коротко объясни, где была граница доступа и какой факт подтверждает исправление."));
  form.append(results(state.transfer));
  form.append(element("label", {class: "field-label", for: "reflection-text"}, "Твоё объяснение своими словами"));
  const area = element("textarea", {id: "reflection-text", name: "explanation", class: "field-control reflection-text", required: true, minlength: 20, maxlength: 2000, placeholder: "Раньше приложение… Я изменил… Исправление подтверждается тем, что…", disabled: busy});
  area.value = explanationDraft || state.reflection?.text || "";
  area.addEventListener("input", () => { explanationDraft = area.value; });
  form.append(area, element("p", {class: "field-help"}, "20–2000 символов. Это твоя заметка, а не экзамен с LLM-судьёй. Эталон для сравнения появится после сохранения."), actions(element("button", {type: "submit", class: "button primary", disabled: busy, id: "finish-lesson"}, "Сохранить вывод и закончить")));
  return form;
}

async function downloadExport() {
  if (busy || !synchronized) return;
  busy = true;
  render();
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch("/api/export", {signal: controller.signal, headers: {"X-Lab-Token": accessToken}, cache: "no-store", credentials: "omit", mode: "same-origin", redirect: "error"});
    if (!response.ok) throw new Error("Не удалось получить карточку эксперимента. Обнови состояние урока и повтори экспорт.");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = element("a", {href: url, download: blob.type.includes("zip") ? "llm-test-lab-experiment.zip" : "llm-test-lab-experiment.json"});
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    announce.textContent = "Карточка эксперимента передана на скачивание.";
  } catch (error) { localMessage = error.name === "AbortError" ? "Экспорт пока не получен. Проверь соединение с локальной лабораторией и повтори скачивание." : error.message; }
  finally { window.clearTimeout(timeout); busy = false; render(); }
}

function renderComplete() {
  const box = element("div", {}, element("div", {class: "completed-mark", "aria-hidden": "true"}, "✓"), ...heading("Урок завершён", "Теперь у вывода есть доказательства", "Ты прошёл условия этого учебного сценария: остановил чужой доступ, сохранил разрешённую работу и проверил правило с другим пользователем."));
  box.append(notice("Важная граница вывода", "Это результат учебного сценария, не испытание интеллекта модели и не гарантия безопасности другой системы. Смысл твоего объяснения автоматически не оценивался."));
  box.append(element("h3", {}, "Твоё объяснение"), element("p", {class: "stage-description"}, state.reflection?.text || "Объяснение сохранено в карточке эксперимента."));
  const rubric = element("ul", {class: "rubric"});
  for (const line of state.reflection?.rubric || []) rubric.append(element("li", {}, line));
  if (rubric.childNodes.length) box.append(element("h3", {}, "Проверь свой вывод по этим пунктам"), rubric);
  if (state.reflection?.reference) box.append(element("details", {class: "help-detail"}, element("summary", {}, "Сравнить с разбором"), element("p", {}, state.reflection.reference)));
  box.append(element("dl", {class: "export-summary"}, element("dt", {}, "В карточке эксперимента"), element("dd", {}, "Твой прогноз, выбранное доказательство, правило защиты, результаты проверок, объяснение и ограничения вывода.")));
  box.append(actions(button("Скачать мой эксперимент", downloadExport, "primary", {id: "export-experiment"}), button("Посмотреть доказательства", () => { viewedStage = "results"; render(); panel.focus(); }, "quiet")));
  return box;
}

function renderRunning() {
  const names = {baseline: ["Проверяем исходное поведение", "Помощник читает заявки и пытается закрыть свою. Сохраняем реальные действия и итог базы."], verify: ["Проверяем твоё правило", "Выполняем четыре отдельные проверки. Важно одновременно сохранить разрешённую задачу и границу доступа."], verification: ["Проверяем твоё правило", "Выполняем четыре отдельные проверки: доступ, обычная задача, цитата и чужая запись."], transfer: ["Проверяем другую роль", "Тот же контроль, но теперь действует пользователь B. Готовое правило не меняем."]};
  const [title, description] = names[state.job.kind] || ["Выполняется учебный опыт", "Прогресс сохранён. После обновления страницы мы проверим состояние, а не запустим действие повторно."];
  return element("div", {}, ...heading("Сейчас выполняется", title, description), element("div", {class: "progress-running", role: "status"}, element("span", {class: "spinner", "aria-hidden": "true"}), element("div", {}, element("h3", {}, "Ждём завершения проверки"), element("p", {}, "Это настоящий локальный запуск, не анимация готового ответа."), element("div", {class: "running-detail"}, "Запросов к модели: 0. Страницу можно обновить — запуск не повторится."))), actions(button(state.job.cancelRequested ? "Останавливаем запуск…" : "Остановить этот запуск", () => submit("cancel"), "secondary", {id: "cancel-run", disabled: busy || state.job.cancelRequested})), notice("После остановки", "Уже выполненные действия не исчезнут. Неполный опыт не будет считаться безопасным или успешным."));
}

function renderProgress() {
  const position = STAGES.indexOf(state?.stage || "prediction");
  const children = [];
  STAGES.forEach((stage, index) => {
    const li = element("li", {class: `progress-step ${index === position ? "current" : index < position ? "done" : ""}`, ...(index === position ? {"aria-current": "step"} : {})}, element("span", {class: "progress-number", "aria-hidden": "true"}, index < position ? "✓" : index + 1), STAGE_NAMES[index]);
    children.push(li);
    if (index < STAGES.length - 1) children.push(element("li", {class: "progress-connector", "aria-hidden": "true"}));
  });
  progress.replaceChildren(...children);
}

function updateContext() {
  // Evidence back-view belongs to the original actor, even when the active lesson moved on.
  // A failed transfer is shown above the policy editor, so keep that changed role explicit.
  const originalEvidence = viewedStage === "evidence";
  const transferContext = !originalEvidence && (
    ["transfer", "reflection", "complete"].includes(state.stage)
    || state.job.running && state.job.kind === "transfer"
    || state.stage === "policy" && currentPolicyFailure()?.kind === "transfer"
  );
  document.querySelector("#context-role").textContent = originalEvidence ? "Исходное доказательство · пользователь A" : transferContext ? "Текущая роль · пользователь B" : "Текущая роль · пользователь A";
  const own = document.querySelector(".ticket.own");
  const foreign = document.querySelector(".ticket.foreign");
  own.querySelector(".ticket-icon").textContent = transferContext ? "B" : "A";
  own.querySelector("strong").textContent = transferContext ? "B-201" : "A-101";
  foreign.querySelector(".ticket-icon").textContent = transferContext ? "A" : "B";
  foreign.querySelector("strong").textContent = transferContext ? "A-101" : "B-201";
  document.querySelector("#session-note").textContent = state.job.running ? "Запуск идёт. Обновление страницы не создаёт повторный опыт." : "Прогресс сохранён на этом компьютере. Можно вернуться к уроку после обновления страницы.";
}

function render() {
  if (!state) return;
  renderProgress();
  updateContext();
  resetButton.disabled = busy || state.job.running || !synchronized;
  let content;
  if (state.job.running) content = renderRunning();
  else if (viewedStage) {
    content = element("div", {}, element("div", {class: "notice"}, "Материалы предыдущего шага. Текущий прогресс не изменяется."));
    if (viewedStage === "evidence") content.append(renderEvidence(true));
    else if (viewedStage === "edit-policy") content = renderPolicy(true);
    else content.append(results(state.verification), results(state.transfer));
    content.append(actions(button("Вернуться к текущему шагу", () => { viewedStage = null; render(); panel.focus(); }, "secondary")));
  } else {
    content = {prediction: renderPrediction, evidence: renderEvidence, policy: renderPolicy, verification: renderVerification, transfer: renderTransfer, reflection: renderReflection, complete: renderComplete}[state.stage]();
  }
  if (state.job.error) content.prepend(notice("Запуск не удалось завершить", typeof state.job.error === "string" ? state.job.error : state.job.error.message || "Проверь, что лаборатория работает. Неполный результат не считается успешным.", "error"));
  if (localMessage) content.prepend(element("p", {class: "inline-error", role: "alert"}, localMessage));
  panel.replaceChildren(content);
  if (!synchronized) panel.querySelectorAll("button,input,select,textarea").forEach(control => { control.disabled = true; });
  panel.setAttribute("aria-busy", state.job.running || busy ? "true" : "false");
}

resetButton.addEventListener("click", () => { if (!busy && !state?.job.running) resetDialog.showModal(); });
document.querySelector("#confirm-reset").addEventListener("click", () => {
  resetDialog.close();
  // Clear learner drafts only after the server confirms a genuinely new session.
  submit("reset");
});
window.addEventListener("pagehide", () => { stopped = true; window.clearTimeout(pollTimer); });
window.addEventListener("pageshow", event => { if (event.persisted) { stopped = false; loadState(false); } });
readAccessToken();
loadState(false);

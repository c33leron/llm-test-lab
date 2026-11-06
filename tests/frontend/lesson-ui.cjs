// Pure client contract tests. This deliberately is not a browser or visual QA substitute.
// A tiny DOM supports the actual app.js; network and timer effects remain explicit.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

class NodeStub {
  constructor(tag = "#text", text = "") {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.listeners = {};
    this.text = text; this.disabled = false; this.checked = false; this.hidden = false; this.value = "";
  }
  set innerHTML(_) { throw new Error("HTML injection sink must not be used"); }
  get childNodes() { return this.children; }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(""); }
  append(...children) { for (const child of children) { const node = child instanceof NodeStub ? child : new NodeStub("#text", String(child)); node.parent = this; this.children.push(node); } }
  prepend(...children) { const old = this.children; this.children = []; this.append(...children, ...old); }
  replaceChildren(...children) { this.children = []; this.text = ""; this.append(...children); }
  setAttribute(key, value) { this.attrs[key] = value; if (["name", "value", "id", "type"].includes(key)) this[key] = value; }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn({preventDefault() {}, target: this}); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
  reportValidity() { return true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  click() { this.dispatch("click"); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return selector.slice(1).split(".").every(name => (this.className || "").split(" ").includes(name));
    return this.tagName.toLowerCase() === selector;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",");
    return this.children.flatMap(node => [...(selectors.some(value => node.matches(value)) ? [node] : []), ...node.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get elements() { return {namedItem: name => this.querySelectorAll("input,select,textarea").find(node => node.name === name) || null}; }
}

function fresh(extra = {}) {
  return {id: "test-session", revision: 0, stage: "prediction", mode: "fixture", prediction: null, diagnosis: null,
    policy: null, baseline: null, verification: null, transfer: null, reflection: null,
    job: {running: false, kind: null, error: null}, ...extra};
}
function reply(payload, status = 200) { return {ok: status < 400, status, json: async () => payload}; }
function pending() { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; }
async function flush() { for (let count = 0; count < 12; count++) await Promise.resolve(); }

async function harness(initial = fresh()) {
  const body = new NodeStub("body");
  const add = (tag, id, parent = body) => { const node = new NodeStub(tag); if (id) node.setAttribute("id", id); parent.append(node); return node; };
  for (const [tag, id] of [["section", "lesson"], ["ol", "progress"], ["div", "connection"], ["button", "reset-button"], ["dialog", "reset-dialog"], ["div", "announcements"], ["div", "session-note"], ["p", "context-role"], ["button", "confirm-reset"]]) add(tag, id);
  for (const name of ["own", "foreign"]) {
    const ticket = add("div"); ticket.className = `ticket ${name}`;
    add("span", null, ticket).className = "ticket-icon"; add("strong", null, ticket);
  }
  const requests = [], queue = [], timers = new Map(), storage = new Map();
  let nextTimer = 0;
  const location = {hash: "#token=synthetic-local-capability", pathname: "/", search: ""};
  const context = vm.createContext({
    console, Node: NodeStub, URLSearchParams, AbortController,
    FormData: class { constructor(form) { this.values = new Map(form.querySelectorAll("input,select,textarea").filter(node => !node.disabled && (node.type !== "radio" || node.checked)).map(node => [node.name, node.value])); } get(key) { return this.values.get(key) ?? null; } },
    document: {body, createElement: tag => new NodeStub(tag), createTextNode: text => new NodeStub("#text", text), querySelector: selector => body.querySelector(selector)},
    window: {location, setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, delay}); return id; }, clearTimeout: id => timers.delete(id), addEventListener() {}},
    history: {replaceState: (_, __, url) => { assert.equal(url, "/"); location.hash = ""; }},
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    fetch: async (url, options) => { requests.push({url, options}); const next = queue.shift(); return next ? await next(url, options) : reply(initial); },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../src/llm_test_lab/learning/static/app.js"), "utf8"), context);
  await flush();
  return {body, requests, queue, timers, location, storage, context, run: source => vm.runInContext(source, context), find: id => body.querySelector(`#${id}`)};
}

const policy = {identitySource: "session", accessRule: "owner", operations: "both", timing: "before"};

test("reload reads saved state, clears fragment and never launches an experiment", async () => {
  const h = await harness(fresh({stage: "policy", revision: 7}));
  assert.equal(h.location.hash, "");
  assert.equal(h.storage.get("llm-test-lab-local-access"), "synthetic-local-capability");
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, "/api/session");
  assert.equal(h.requests[0].options.headers["X-Lab-Token"], "synthetic-local-capability");
  assert.equal(h.requests[0].options.method, undefined);
  assert.equal(h.find("lesson").scrolled, undefined, "initial arrival must not scroll away from introduction");
  assert.equal(h.run("state.stage"), "policy");
  assert.ok(h.find("save-policy"));
});

test("policy has no correct default and submits the learner choice without client grading", async () => {
  const h = await harness(fresh({stage: "policy", revision: 2}));
  const selected = {identitySource: "note", accessRule: "allow_all", operations: "read", timing: "after"};
  for (const [name, value] of Object.entries(selected)) {
    const field = h.find(`policy-${name}`); assert.equal(field.value, ""); field.value = value;
  }
  h.queue.push(() => reply(fresh({stage: "verification", revision: 3, policy: selected})));
  h.find("policy-form").dispatch("submit"); await flush();
  assert.deepEqual(JSON.parse(h.requests[1].options.body), {revision: 2, ...selected});
  assert.equal(h.requests[1].url, "/api/policy");
  assert.equal(h.run("state.stage"), "verification");
  assert.ok(h.find("verify-policy"));
  assert.equal(h.requests.length, 2, "saving does not silently verify");
});

test("cancelled baseline retry reuses the immutable first prediction", async () => {
  const prediction = {ownClosed: false, foreignDisclosed: true};
  const h = await harness(fresh({revision: 4, prediction, job: {running: false, error: "Прервано"}}));
  const radios = h.find("prediction-form").querySelectorAll("input");
  assert.equal(radios.length, 4); assert.ok(radios.every(node => node.disabled));
  h.queue.push(() => reply(fresh({revision: 5, prediction, job: {running: true, kind: "baseline"}})));
  h.find("prediction-form").dispatch("submit"); await flush();
  assert.deepEqual(JSON.parse(h.requests[1].options.body), {revision: 4, ...prediction});
  assert.equal(h.requests[1].url, "/api/predict");
  assert.ok(h.find("cancel-run"));
  assert.equal(h.requests.length, 2);
});

test("double action sends one POST, then polls only a running server job", async () => {
  const h = await harness(fresh({stage: "verification", revision: 3, policy}));
  const response = pending(); h.queue.push(() => response.promise);
  const first = h.run('submit("verify")'); const second = h.run('submit("verify")');
  assert.equal(h.requests.filter(item => item.options.method === "POST").length, 1);
  response.resolve(reply(fresh({stage: "verification", revision: 4, policy, job: {running: true, kind: "verify"}})));
  await first; await second;
  assert.deepEqual([...h.timers.values()].map(item => item.delay), [600]);
  const poll = [...h.timers.values()][0];
  h.queue.push(() => reply(fresh({stage: "transfer", revision: 5, policy, verification: {passed: true, trials: []}})));
  await poll.fn();
  assert.equal(h.run("state.stage"), "transfer");
  assert.equal(h.timers.size, 0, "completed jobs do not keep polling");
});

test("CAS conflict refreshes authoritative state and never replays the mutation", async () => {
  const h = await harness(fresh({stage: "verification", revision: 5, policy}));
  h.queue.push(() => reply({error: {code: "STALE_REVISION", message: "Already changed"}}, 409));
  h.queue.push(() => reply(fresh({stage: "transfer", revision: 6, policy, verification: {passed: true, trials: []}})));
  await h.run('submit("verify")');
  assert.deepEqual(h.requests.map(item => item.url), ["/api/session", "/api/verify", "/api/session"]);
  assert.equal(h.run("state.stage"), "transfer"); assert.ok(h.find("run-transfer"));
});

test("unknown POST outcome is read back before allowing a retry", async () => {
  const h = await harness(fresh({stage: "verification", revision: 5, policy}));
  h.queue.push(() => { throw new TypeError("Connection lost"); });
  h.queue.push(() => reply(fresh({stage: "verification", revision: 6, policy, job: {running: true, kind: "verify"}})));
  await h.run('submit("verify")');
  assert.deepEqual(h.requests.map(item => item.url), ["/api/session", "/api/verify", "/api/session"]);
  assert.equal(h.run("state.job.running"), true); assert.equal(h.find("verify-policy"), null);
  assert.ok(h.find("cancel-run"));
});

test("offline reads disable stale controls; reconnect resumes without mutation", async () => {
  const h = await harness(fresh({stage: "verification", revision: 5, policy}));
  h.queue.push(() => { throw new TypeError("Offline"); });
  await h.run("loadState()");
  assert.equal(h.find("verify-policy").disabled, true); assert.equal(h.find("reset-button").disabled, true);
  assert.equal(h.find("connection").hidden, false);
  await h.run('submit("verify")'); assert.equal(h.requests.length, 2);
  h.queue.push(() => reply(fresh({stage: "verification", revision: 5, policy})));
  await h.run("loadState()");
  assert.equal(h.find("verify-policy").disabled, false); assert.equal(h.find("connection").hidden, true);
  assert.ok(h.requests.every(item => item.options.method !== "POST"));
});

test("evidence payload is literal text, and incomplete trial never becomes a client safety pass", async () => {
  const payload = '<img src=x onerror="alert(1)"><script>window.stolen=true</script>';
  const h = await harness(fresh({stage: "evidence", baseline: {ownClosed: true, foreignDisclosed: true,
    evidenceCandidates: [{sequence: 3, title: "Observed event", description: payload, payload: {note: payload}}]}}));
  assert.ok(h.find("lesson").textContent.includes(payload));
  assert.equal(h.body.querySelectorAll("img,script").length, 0);
  h.run('state.stage="policy";state.verification={passed:false,summary:"Partial",trials:[{caseId:"L01",task:"pass",goalMet:true,terminal:"error",security:"violation",evidenceIntegrity:"partial",reason:"Actual violation"}]};render()');
  assert.ok(h.find("lesson").textContent.includes("Чужое действие состоялось"));
  assert.ok(h.find("lesson").textContent.includes("Опыт не завершён"));
  assert.ok(h.find("lesson").textContent.includes("Правило пока не прошло проверку"));
  assert.ok(!h.find("lesson").textContent.includes("Контракт соблюдён"));
});

test("policy can be edited at verification without stale back-view trapping the learner", async () => {
  const h = await harness(fresh({stage: "verification", revision: 3, policy}));
  h.run('viewedStage="edit-policy";render()');
  h.queue.push(() => reply(fresh({stage: "verification", revision: 4, policy})));
  h.find("policy-form").dispatch("submit"); await flush();
  assert.ok(h.find("verify-policy")); assert.equal(h.find("policy-form"), null);
});

test("reset discards old unsaved drafts only after a new server session exists", async () => {
  const h = await harness(fresh({stage: "policy", revision: 3, policy}));
  h.run('draftPolicy={identitySource:"note"};explanationDraft="Old text"');
  h.queue.push(() => reply(fresh({id: "new-session"})));
  await h.run('submit("reset")');
  assert.equal(h.run("state.id"), "new-session"); assert.equal(h.run("draftPolicy"), null);
  assert.equal(h.run("explanationDraft"), ""); assert.ok(h.find("run-baseline"));
});

test("expired local capability disables actions rather than presenting a retry as safe", async () => {
  const h = await harness(fresh({stage: "verification", revision: 3, policy}));
  h.queue.push(() => reply({error: {code: "SESSION_LINK_REQUIRED", message: "Reopen local URL"}}, 403));
  await h.run('submit("verify")');
  assert.equal(h.find("verify-policy").disabled, true); assert.equal(h.find("reset-button").disabled, true);
  assert.ok(h.find("connection").textContent.includes("Повторно открой адрес из терминала"));
  await h.run('submit("verify")'); assert.equal(h.requests.length, 2);
});

test("a read that started before reset cannot restore the previous session", async () => {
  const h = await harness(fresh({stage: "policy", revision: 3, policy}));
  const response = pending(); h.queue.push(() => response.promise);
  const oldRead = h.run("loadState()");
  h.run(`applyState(${JSON.stringify(fresh({id: "new-session"}))})`);
  response.resolve(reply(fresh({stage: "policy", revision: 3, policy})));
  await oldRead;
  assert.equal(h.run("state.id"), "new-session"); assert.ok(h.find("run-baseline"));
});

test("failed reset through its real handler retains the learner draft", async () => {
  const original = fresh({stage: "policy", revision: 3, policy});
  const h = await harness(original);
  h.run('draftPolicy={identitySource:"note",accessRule:"deny_all",operations:"write",timing:"after"}');
  h.queue.push(() => { throw new TypeError("Offline"); });
  h.queue.push(() => reply(original));
  h.find("confirm-reset").click(); await flush();
  assert.equal(h.run("state.id"), original.id);
  assert.equal(h.find("policy-identitySource").value, "note");
  assert.equal(h.find("policy-accessRule").value, "deny_all");
});


test("sidebar switches ownership for transfer and explicitly names the current role", async () => {
  const h = await harness(fresh({stage: "transfer", revision: 8, policy, verification: {passed: true, trials: []}}));
  const own = h.body.querySelector(".ticket.own");
  const foreign = h.body.querySelector(".ticket.foreign");
  assert.equal(own.querySelector("strong").textContent, "B-201");
  assert.equal(foreign.querySelector("strong").textContent, "A-101");
  assert.ok(h.find("context-role").textContent.includes("пользователь B"));
  h.run('viewedStage="evidence";render()');
  assert.equal(own.querySelector("strong").textContent, "A-101");
  assert.equal(foreign.querySelector("strong").textContent, "B-201");
  assert.ok(h.find("context-role").textContent.includes("Исходное доказательство"));
  h.run('viewedStage=null;render()');
  assert.equal(own.querySelector("strong").textContent, "B-201");
});

test("failed transfer keeps actor B visible while fixing, then verification returns to actor A", async () => {
  const h = await harness(fresh({stage: "policy", revision: 9, policy, transfer: {passed: false, trials: []}}));
  assert.equal(h.body.querySelector(".ticket.own").querySelector("strong").textContent, "B-201");
  assert.ok(h.find("context-role").textContent.includes("пользователь B"));
  h.queue.push(() => reply(fresh({stage: "verification", revision: 10, policy, transfer: {passed: false, trials: []}})));
  h.find("policy-form").dispatch("submit"); await flush();
  assert.equal(h.body.querySelector(".ticket.own").querySelector("strong").textContent, "A-101");
  assert.ok(h.find("context-role").textContent.includes("пользователь A"));
});

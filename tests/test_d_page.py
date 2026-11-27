#!/usr/bin/env python3
"""Tests for issue #9 part D & #34: the page follows the new blocks. Structural + Node VM DOM checks.

Assumed names (the implementer must match):
  static/slots.js:    onState(['block:slots', ...]); entries carry absolute `since` and `ttl_until`; the page derives
                      idle_seconds, age_s, ttl_left_s from them with Date.now() and re-renders on a timer
  static/sessions.js: onState(['block:sessions', ...]); displays held_slots (and +N in children when kidsHeld > 0)
                      without per-session -/+ limit buttons, over-by badges, adjustLimit(), or notifySession()
                      limit-nag text; keeps checkbox selection and /api/send messaging intact
  static/cleaners.js: onState(['block:cleaners', ...])
  static/topbar.js:   follows block:host, block:slots, block:sessions, block:runners, block:autoreview
  static/app.js:      assembleState builds S from the blocks with no legacy block and no default_limit:
                      S.slots = slots.slots, S.machine_max, S.machine_in_use = slots.length, S.sessions = sessions.sessions,
                      S.ci = runners, S.cleaners = cleaners.cleaners, S.rescue_bundles
"""

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def subscription(src, key):
    """The onState/store.subscribe calls whose key list names `key`."""
    return [m for m in re.findall(r"(?:onState|store\.subscribe)\(\s*\[([^\]]*)\]", src) if key in m]


class TestOwnBlocks(unittest.TestCase):
    def test_each_renderer_subscribes_to_its_own_block(self):
        for js, key in (("slots", "block:slots"), ("sessions", "block:sessions"), ("cleaners", "block:cleaners")):
            self.assertTrue(subscription(text("static/%s.js" % js), key), "%s.js must subscribe to %s" % (js, key))

    def test_no_renderer_subscribes_to_the_legacy_block(self):
        for p in sorted((ROOT / "static").glob("*.js")):
            self.assertNotIn("block:legacy", p.read_text(encoding="utf-8"), p.name)
        self.assertNotIn("block:legacy", text("page.html"))

    def test_topbar_follows_host_slots_sessions_runners_autoreview(self):
        src = text("static/topbar.js")
        for key in ("block:host", "block:slots", "block:sessions", "block:runners", "block:autoreview"):
            self.assertTrue(subscription(src, key), "topbar.js must follow " + key)

    def test_harness_pills_follow_the_sessions_block(self):
        src = text("static/topbar.js")
        pills = [m for m in re.findall(r"onState\(\s*\[([^\]]*)\]\s*,\s*renderHarnessPills", src)]
        self.assertTrue(pills and "block:sessions" in pills[0], pills)

    def test_rescue_follows_the_cleaners_block_and_commands(self):
        src = text("static/cleaners.js")
        rescue = re.findall(r"onState\(\s*\[([^\]]*)\]\s*,\s*renderRescue", src)
        self.assertTrue(rescue and "block:cleaners" in rescue[0] and "commands" in rescue[0], rescue)


class TestAssembleState(unittest.TestCase):
    def test_state_is_built_from_the_new_blocks_without_legacy(self):
        src = text("static/app.js")
        self.assertNotRegex(src, r"legacy")
        for needle in ("slots", "sessions", "runners", "cleaners", "host"):
            self.assertRegex(src, r"b\.%s\b" % needle, "assembleState must read the %s block" % needle)

    def test_state_is_assembled_once_the_blocks_it_needs_have_arrived(self):
        src = text("static/app.js")
        self.assertNotRegex(src, r"if \(!b\.legacy")


class TestSlotsDeriveRelativeTimes(unittest.TestCase):
    def test_slots_js_derives_the_three_relative_fields_from_absolute_ones(self):
        src = text("static/slots.js")
        for field in ("idle_seconds", "age_s", "ttl_left_s"):
            self.assertRegex(src, r"\b%s\b\s*[:=]" % field, "slots.js must compute " + field)
        self.assertIn("ttl_until", src)
        self.assertRegex(src, r"\.since\b")
        self.assertIn("Date.now()", src)

    def test_slots_js_ticks_so_the_derived_values_move_without_a_block_change(self):
        self.assertIn("setInterval", text("static/slots.js"))


class TestSessionsTableLimitControls(unittest.TestCase):
    def test_page_html_has_sessions_limits_header_and_no_project_limits_bar(self):
        html = text("page.html")
        self.assertNotIn("project-limits-bar", html)
        self.assertNotIn("project-limit-chip", html)
        self.assertIn("Agent Sessions &amp; Limits", html)
        self.assertIn("Slots / Limit", html)
        sec_m = re.search(r'<section id="sec-sessions">.*?</section>', html, re.S)
        self.assertIsNotNone(sec_m, "page.html missing #sec-sessions")
        sec = sec_m.group(0)
        self.assertNotIn("<th>Harness</th>", sec)
        self.assertNotIn("<th>Session / Title</th>", sec)
        self.assertIn("<th>Session / Project</th>", sec)
        thead_m = re.search(r"<thead>(.*?)</thead>", sec, re.S)
        self.assertIsNotNone(thead_m)
        self.assertEqual(len(re.findall(r"<th\b", thead_m.group(1))), 4)
        self.assertIn('colspan="4"', sec)
        self.assertNotIn('colspan="5"', sec)

    def test_slots_js_has_cap_pause_and_no_project_limits(self):
        src = text("static/slots.js")
        self.assertNotIn("renderProjectLimits", src)
        self.assertNotIn("project-limits-bar", src)
        self.assertNotIn("/api/project-limit", src)
        self.assertIn("cap-pause", src)
        self.assertIsNotNone(
            re.search(r"""(?:api|store\.command)\(\s*["']/api/cap["']\s*,\s*\{[^}]*mode\s*:\s*["']pause["']""", src),
            "slots.js must wire cap-pause to /api/cap with mode: 'pause'",
        )

    def test_assemble_state_sets_default_limit_and_omits_projects(self):
        src = text("static/app.js")
        self.assertNotIn("project_default", src)
        js = r"""
const vm = require("vm");
const assert = require("assert");
const [appSrc] = process.argv.slice(1);
const ctxObj = {
  window: { addEventListener() {} },
  document: { querySelectorAll: () => [] },
  store: {
    blocks: {
      slots: { data: { slots: [], machine_max: 20 } },
      sessions: { data: { sessions: [{ id: "s1", held_slots: 5, tree_held: 5 }], default_limit: 0 } },
    },
    commands: {},
    logs: [],
    subscribe() {},
    connect() {},
  },
};
const ctx = vm.createContext(ctxObj);
vm.runInContext(appSrc, ctx);
const S0 = vm.runInContext("assembleState()", ctx);
assert.ok(S0 && Array.isArray(S0.sessions), "S.sessions must be populated");
assert.strictEqual(S0.default_limit, 0, "default_limit 0 must be preserved (not || 1)");
assert.strictEqual("projects" in S0, false, "S must not carry projects");
assert.strictEqual("project_default" in S0, false, "S must not carry project_default");

ctxObj.store.blocks.sessions.data = { sessions: [] };
const S1 = vm.runInContext("assembleState()", ctx);
assert.strictEqual(S1.default_limit, 1, "missing default_limit must fall back to 1");
"""
        p = subprocess.run(["node", "-e", js, text("static/app.js")], capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)

    def test_the_machine_cap_is_still_a_command(self):
        src = text("static/slots.js")
        self.assertRegex(src, r"/api/cap")

    def test_sessions_js_renders_limit_controls_pending_pause_and_tree_over_limit(self):
        js = r"""
const vm = require("vm");
const assert = require("assert");
const [utilSrc, appSrc, sessionsSrc] = process.argv.slice(1);

let cachedCheckboxes = [];
let lastHtml = "";

function parseCheckboxes(html) {
  const out = [];
  const re = /<input\b[^>]*class="[^"]*session-chk[^"]*"[^>]*>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const tag = m[0];
    const refM = /data-ref="([^"]*)"/.exec(tag);
    out.push({
      checked: /\bchecked\b/.test(tag),
      disabled: /\bdisabled\b/.test(tag),
      getAttribute: (name) => (name === "data-ref" ? (refM ? refM[1] : null) : null),
      onchange: null,
    });
  }
  return out;
}

const elements = {};
for (const id of [
  "sessions-tbody", "sessions-count", "chk-all-sessions", "session-search",
  "btn-select-active", "btn-clear-selection", "btn-send-msg", "msg-input",
]) {
  elements[id] = { id, value: "", textContent: "", innerHTML: "", checked: false, addEventListener() {}, onclick: null, onchange: null };
}
Object.defineProperty(elements["sessions-tbody"], "innerHTML", {
  get() { return lastHtml; },
  set(v) {
    lastHtml = String(v);
    cachedCheckboxes = parseCheckboxes(lastHtml);
  },
});

const subscribers = [];
const sent = [];
const alerts = [];
const pendingMap = {};

const ctxObj = {
  Date: { now: () => 1700000000000 },
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  alert: (m) => alerts.push(m),
  window: { scrollY: 0, addEventListener() {} },
  document: {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (sel) => (sel === ".session-chk" ? cachedCheckboxes : []),
  },
  store: {
    blocks: {},
    commands: {},
    logs: [],
    ui: { harness: "all", search: "", selected: new Set(), expanded: new Set(), collapsed: new Set() },
    subscribe(keys, fn) { subscribers.push({ keys, fn }); },
    setUI(patch) {
      Object.assign(this.ui, patch);
      for (const s of subscribers) {
        if (s.keys.includes("ui")) s.fn();
      }
    },
    command(path, body, opts) { sent.push({ path, body, opts }); },
    pendingFor(key) { return pendingMap[key] || null; },
    connect() {},
  },
};

const ctx = vm.createContext(ctxObj);
vm.runInContext(utilSrc, ctx);
vm.runInContext(appSrc, ctx);
vm.runInContext(sessionsSrc, ctx);

ctxObj.store.blocks = {
  slots: { data: { slots: [], machine_max: 20 } },
  sessions: {
    data: {
      default_limit: 2,
      sessions: [
        { id: "agy-5", ref: "local_agy-5", harness: "antigravity", title: "Over Limit", cwd: "/w/p1", held_slots: 5, tree_held: 5, limit: 1, is_over_limit: true, is_active: true, last_active: 1699999900 },
        { id: "paused-0", ref: "local_paused-0", harness: "claude", title: "Paused Zero", cwd: "/w/p2", held_slots: 0, tree_held: 0, limit: 0, is_over_limit: false, is_active: true, last_active: 1699999900 },
        { id: "max-20", ref: "local_max-20", harness: "claude", title: "At Twenty", cwd: "/w/p3", held_slots: 2, tree_held: 2, limit: 20, is_over_limit: false, is_active: false, last_active: 1000 },
        { id: "root-over", ref: "local_root-over", harness: "claude", title: "Root Over", cwd: "/w/p4", held_slots: 1, tree_held: 4, limit: 2, is_over_limit: false, tree_over_limit: true, is_active: true, last_active: 1699999900 },
        { id: "child-over", ref: "local_child-over", harness: "claude", title: "Child Over", parent_id: "root-over", root_id: "root-over", cwd: "/w/p5", held_slots: 3, tree_held: 0, limit: 1, is_over_limit: true, is_active: true, last_active: 1699999900 },
        { id: "arch-1", ref: "local_arch-1", harness: "claude", title: "Archived", cwd: "/w/p6", held_slots: 0, tree_held: 0, limit: 1, is_over_limit: false, is_active: false, archived: true, last_active: 100 },
      ],
    },
  },
};

function notify(key) {
  for (const sub of subscribers) {
    if (sub.keys.some((k) => k === key || (key.startsWith("block:") && k === "block:*"))) sub.fn();
  }
}

notify("block:sessions");

function parseRows(html) {
  const rows = [];
  const trRe = /<tr\b[^>]*>([\s\S]*?)<\/tr>/g;
  let trM;
  while ((trM = trRe.exec(html)) !== null) {
    const tds = [];
    const tdRe = /<td\b[^>]*>([\s\S]*?)<\/td>/g;
    let tdM;
    while ((tdM = tdRe.exec(trM[1])) !== null) tds.push(tdM[1]);
    rows.push({ raw: trM[0], tds });
  }
  return rows;
}

function parseSessionRows(html) {
  return parseRows(html).filter((r) => /\bsess-(?:root|child)\b/.test(r.raw));
}

function parseButtons(html) {
  const out = [];
  const re = /<button\b([^>]*)>([\s\S]*?)<\/button>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const attrs = m[1];
    const text = m[2].replace(/<[^>]+>/g, "").trim();
    const onclickM = /onclick="([^"]*)"/.exec(attrs);
    out.push({
      text,
      disabled: /\bdisabled\b/.test(attrs),
      onclick: onclickM ? onclickM[1].replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&") : "",
    });
  }
  return out;
}

function stripTags(s) {
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

// tree_over_limit auto-expands child-over without manual expand
const initialRows = parseSessionRows(elements["sessions-tbody"].innerHTML);
assert.strictEqual(initialRows.length, 6, "tree_over_limit must auto-expand child-over row");

// Collapsing root-over hides child-over despite tree_over_limit
ctxObj.store.ui.collapsed.add("root-over");
notify("ui");
assert.strictEqual(parseSessionRows(elements["sessions-tbody"].innerHTML).length, 5, "collapsed tree must stay closed");
ctxObj.store.ui.collapsed.delete("root-over");
notify("ui");

const agyRow = parseSessionRows(elements["sessions-tbody"].innerHTML).find((r) => r.raw.includes("local_agy-5"));
assert.strictEqual(agyRow.tds.length, 4, "session row must have 4 columns (no Harness column)");
const agyText = stripTags(agyRow.tds[2]);
assert.match(agyText, /5\s*\/\s*1/, agyText);
assert.ok(agyText.includes("over by 4"), agyText);
const agyBtns = parseButtons(agyRow.tds[2]);
assert.strictEqual(agyBtns.find((b) => b.text === "−").disabled, false);
assert.strictEqual(agyBtns.find((b) => b.text === "+").disabled, false);
assert.strictEqual(agyBtns.find((b) => b.text === "Pause").disabled, false);

// Boundary 0: − and Pause disabled, Paused yellow badge shown
const pausedRow = parseSessionRows(elements["sessions-tbody"].innerHTML).find((r) => r.raw.includes("local_paused-0"));
const pausedText = stripTags(pausedRow.tds[2]);
assert.match(pausedText, /0\s*\/\s*0/, pausedText);
assert.ok(pausedRow.tds[2].includes("badge-yellow") && /Paused/i.test(pausedText), pausedRow.tds[2]);
const pausedBtns = parseButtons(pausedRow.tds[2]);
assert.strictEqual(pausedBtns.find((b) => b.text === "−").disabled, true);
assert.strictEqual(pausedBtns.find((b) => b.text === "Pause").disabled, true);
assert.strictEqual(pausedBtns.find((b) => b.text === "+").disabled, false);

// Boundary 20: + disabled
const maxRow = parseSessionRows(elements["sessions-tbody"].innerHTML).find((r) => r.raw.includes("local_max-20"));
const maxBtns = parseButtons(maxRow.tds[2]);
assert.strictEqual(maxBtns.find((b) => b.text === "+").disabled, true);
assert.strictEqual(maxBtns.find((b) => b.text === "−").disabled, false);

// Clicking -, +, Pause invokes store.command('/api/limit', ...)
for (const b of agyBtns) {
  if (["−", "+", "Pause"].includes(b.text)) vm.runInContext(b.onclick, ctx);
}
assert.deepStrictEqual(JSON.parse(JSON.stringify(sent)), [
  { path: "/api/limit", body: { ref: "local_agy-5", delta: -1 }, opts: { key: "limit:local_agy-5", value: 0 } },
  { path: "/api/limit", body: { ref: "local_agy-5", delta: 1 }, opts: { key: "limit:local_agy-5", value: 2 } },
  { path: "/api/limit", body: { ref: "local_agy-5", mode: "pause" }, opts: { key: "limit:local_agy-5", value: 0 } },
]);

// Pending state for local_agy-5 only (value: 0 pause in flight)
pendingMap["limit:local_agy-5"] = { value: 0 };
notify("pending");
const agyPendingRow = parseSessionRows(elements["sessions-tbody"].innerHTML).find((r) => r.raw.includes("local_agy-5"));
assert.ok(agyPendingRow.tds[2].includes("is-pending"), agyPendingRow.tds[2]);
assert.match(stripTags(agyPendingRow.tds[2]), /5\s*\/\s*0/, agyPendingRow.tds[2]);
assert.match(stripTags(agyPendingRow.tds[2]), /Paused/i, agyPendingRow.tds[2]);
for (const b of parseButtons(agyPendingRow.tds[2])) {
  assert.strictEqual(b.disabled, true, "pending session buttons must be disabled");
}
const maxSiblingRow = parseSessionRows(elements["sessions-tbody"].innerHTML).find((r) => r.raw.includes("local_max-20"));
assert.ok(!maxSiblingRow.tds[2].includes("is-pending"), "non-pending sibling must not carry is-pending");
assert.strictEqual(parseButtons(maxSiblingRow.tds[2]).find((b) => b.text === "−").disabled, false);

// Selection + /api/send messaging stays intact
sent.length = 0;
const agyChk = cachedCheckboxes.find((c) => c.getAttribute("data-ref") === "local_agy-5");
const archChk = cachedCheckboxes.find((c) => c.getAttribute("data-ref") === "local_arch-1");
assert.ok(agyChk && !agyChk.disabled);
assert.ok(archChk && archChk.disabled);
agyChk.checked = true;
agyChk.onchange();
elements["msg-input"].value = "hello operator";
elements["btn-send-msg"].onclick();
assert.deepStrictEqual(JSON.parse(JSON.stringify(sent)), [{ path: "/api/send", body: { to: [{ ref: "local_agy-5", harness: "antigravity" }], text: "hello operator" } }]);
assert.deepStrictEqual(alerts, []);

// Session ref with single quote / backslash / script-injection chars in inline onclick
sent.length = 0;
ctxObj.store.blocks.sessions.data.sessions = [
  { id: "q-1", ref: "a'b", harness: "claude", title: "Quoted", cwd: "/w/q", held_slots: 0, tree_held: 0, limit: 2, is_over_limit: false, is_active: true, last_active: 1699999900 },
  { id: "q-2", ref: "x\\\\');alert(1);//", harness: "claude", title: "Crafted", cwd: "/w/q2", held_slots: 0, tree_held: 0, limit: 2, is_over_limit: false, is_active: true, last_active: 1699999900 },
];
notify("block:sessions");
const qRows = parseSessionRows(elements["sessions-tbody"].innerHTML);
assert.strictEqual(qRows.length, 2);
for (const b of parseButtons(qRows[0].tds[2])) {
  if (["−", "+", "Pause"].includes(b.text)) vm.runInContext(b.onclick, ctx);
}
for (const b of parseButtons(qRows[1].tds[2])) {
  if (["−", "+", "Pause"].includes(b.text)) vm.runInContext(b.onclick, ctx);
}
assert.deepStrictEqual(JSON.parse(JSON.stringify(sent)), [
  { path: "/api/limit", body: { ref: "a'b", delta: -1 }, opts: { key: "limit:a'b", value: 1 } },
  { path: "/api/limit", body: { ref: "a'b", delta: 1 }, opts: { key: "limit:a'b", value: 3 } },
  { path: "/api/limit", body: { ref: "a'b", mode: "pause" }, opts: { key: "limit:a'b", value: 0 } },
  { path: "/api/limit", body: { ref: "x\\\\');alert(1);//", delta: -1 }, opts: { key: "limit:x\\\\');alert(1);//", value: 1 } },
  { path: "/api/limit", body: { ref: "x\\\\');alert(1);//", delta: 1 }, opts: { key: "limit:x\\\\');alert(1);//", value: 3 } },
  { path: "/api/limit", body: { ref: "x\\\\');alert(1);//", mode: "pause" }, opts: { key: "limit:x\\\\');alert(1);//", value: 0 } },
]);
assert.deepStrictEqual(alerts, []);
"""
        p = subprocess.run(
            ["node", "-e", js, text("static/util.js"), text("static/app.js"), text("static/sessions.js")],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)


class TestCapPause(unittest.TestCase):
    def test_page_html_has_cap_pause(self):
        html = text("page.html")
        self.assertIsNotNone(re.search(r"""id\s*=\s*["']cap-pause["']""", html), "page.html missing #cap-pause")

    def test_topbar_js_indicates_paused_state_when_machine_max_is_zero(self):
        m = re.search(r"function renderTopbar\(\).*?\n\}\n", text("static/topbar.js"), re.S)
        self.assertIsNotNone(m, "renderTopbar must stay")
        body = m.group(0)
        self.assertIsNotNone(
            re.search(r"machine_max[\s\S]{0,120}===?\s*0", body),
            "renderTopbar must check machine_max === 0",
        )
        self.assertIsNotNone(re.search(r"(?i)paused?", body), "renderTopbar must indicate paused state")


class TestSlotsAndTopbarPending(unittest.TestCase):
    VM_HARNESS = r"""
const vm = require("vm");
const assert = require("assert");
const [utilSrc, appSrc, topbarSrc, slotsSrc] = process.argv.slice(1);

function decodeEntities(s) {
  return String(s)
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, ">")
    .replace(/&lt;/g, "<")
    .replace(/&amp;/g, "&");
}

function toCamel(s) {
  return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

function parseHtmlFragment(html, root, ctxRef) {
  root.children = [];
  const stack = [root];
  const voidTags = new Set(["br", "hr", "img", "input", "meta", "link"]);
  const tagRe = /<\/?([a-zA-Z0-9-]+)([^>]*)>/g;
  let lastIdx = 0;
  let m;
  while ((m = tagRe.exec(html)) !== null) {
    if (m.index > lastIdx) {
      const txt = decodeEntities(html.slice(lastIdx, m.index));
      const top = stack[stack.length - 1];
      top._chunks.push(txt);
    }
    lastIdx = tagRe.lastIndex;
    const rawTag = m[0];
    const tag = m[1].toLowerCase();
    const attrStr = m[2] || "";
    const isClose = rawTag.startsWith("</");
    if (isClose) {
      while (stack.length > 1 && stack[stack.length - 1].tagName.toLowerCase() !== tag) {
        stack.pop();
      }
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = makeElement(tag, "", ctxRef);
    const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let am;
    while ((am = attrRe.exec(attrStr)) !== null) {
      const k = am[1];
      const rawVal = am[2] ?? am[3] ?? am[4];
      const v = rawVal !== undefined ? decodeEntities(rawVal) : "";
      el.attrs[k] = v;
      if (k === "id") el.id = v;
      else if (k === "class") el.className = v;
      else if (k === "disabled") el.disabled = true;
      else if (k.startsWith("data-")) el.dataset[toCamel(k.slice(5))] = v;
    }
    const parent = stack[stack.length - 1];
    el.parentNode = parent;
    parent.children.push(el);
    parent._chunks.push(el);
    if (!voidTags.has(tag) && !rawTag.endsWith("/>")) {
      stack.push(el);
    }
  }
  if (lastIdx < html.length) {
    stack[stack.length - 1]._chunks.push(decodeEntities(html.slice(lastIdx)));
  }
}

function matchesSingleSelector(el, sel) {
  let s = sel.trim();
  if (!s) return false;
  const tagM = /^[a-zA-Z0-9-]+/.exec(s);
  if (tagM) {
    if (el.tagName.toLowerCase() !== tagM[0].toLowerCase()) return false;
    s = s.slice(tagM[0].length);
  }
  while (s.length > 0) {
    if (s[0] === "#") {
      const m = /^#([a-zA-Z0-9_-]+)/.exec(s);
      if (!m || el.id !== m[1]) return false;
      s = s.slice(m[0].length);
    } else if (s[0] === ".") {
      const m = /^\.([a-zA-Z0-9_-]+)/.exec(s);
      if (!m || !el.classList.contains(m[1])) return false;
      s = s.slice(m[0].length);
    } else if (s[0] === "[") {
      const m = /^\[([a-zA-Z0-9_:-]+)(?:=["']?([^"'\]]*)["']?)?\]/.exec(s);
      if (!m) return false;
      const val = el.getAttribute(m[1]);
      if (val === null) return false;
      if (m[2] !== undefined && val !== m[2]) return false;
      s = s.slice(m[0].length);
    } else {
      return false;
    }
  }
  return true;
}

function matchesSelectorChain(el, sel) {
  const tokens = sel.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return false;
  if (!matchesSingleSelector(el, tokens[tokens.length - 1])) return false;
  let cur = el.parentNode;
  for (let i = tokens.length - 2; i >= 0; i--) {
    while (cur && !matchesSingleSelector(cur, tokens[i])) cur = cur.parentNode;
    if (!cur) return false;
    cur = cur.parentNode;
  }
  return true;
}

function collectMatches(root, selector) {
  const parts = selector.split(",").map((p) => p.trim()).filter(Boolean);
  const out = [];
  function walk(node) {
    for (const ch of node.children) {
      if (parts.some((p) => matchesSelectorChain(ch, p))) out.push(ch);
      walk(ch);
    }
  }
  walk(root);
  return out;
}

function escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function serializeChunks(chunks) {
  return chunks.map((c) => {
    if (typeof c === "string") return escHtml(c);
    const tag = c.tagName.toLowerCase();
    const attrs = Object.entries(c.attrs).map(([k, v]) => ` ${k}="${escHtml(v)}"`).join("");
    return `<${tag}${attrs}>${serializeChunks(c._chunks)}</${tag}>`;
  }).join("");
}

function makeElement(tag, id, ctxRef) {
  const classSet = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    id: id || "",
    attrs: {},
    dataset: {},
    style: {},
    children: [],
    _chunks: [],
    _explicitText: null,
    _html: "",
    _disabled: false,
    parentNode: null,
    listeners: {},
    onclick: null,
    get disabled() { return this._disabled; },
    set disabled(v) { this._disabled = Boolean(v); },
    get className() { return Array.from(classSet).join(" "); },
    set className(v) {
      classSet.clear();
      String(v || "").split(/\s+/).filter(Boolean).forEach((c) => classSet.add(c));
    },
    classList: {
      add(...cs) { cs.forEach((c) => c && classSet.add(c)); },
      remove(...cs) { cs.forEach((c) => classSet.delete(c)); },
      contains(c) { return classSet.has(c); },
      toggle(c, force) {
        if (force === undefined) {
          if (classSet.has(c)) { classSet.delete(c); return false; }
          classSet.add(c); return true;
        }
        if (force) { classSet.add(c); return true; }
        classSet.delete(c); return false;
      },
    },
    getAttribute(k) {
      if (k === "id") return this.id || null;
      if (k === "class") return this.className || null;
      if (k === "disabled") return this._disabled ? "" : null;
      if (k.startsWith("data-")) {
        const dk = toCamel(k.slice(5));
        if (dk in this.dataset) return String(this.dataset[dk]);
      }
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    setAttribute(k, v) {
      const sv = String(v);
      this.attrs[k] = sv;
      if (k === "id") this.id = sv;
      else if (k === "class") this.className = sv;
      else if (k === "disabled") this._disabled = true;
      else if (k.startsWith("data-")) this.dataset[toCamel(k.slice(5))] = sv;
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      this._chunks.push(child);
      this._explicitText = null;
      return child;
    },
    append(...nodes) {
      for (const n of nodes) {
        if (typeof n === "string") this._chunks.push(n);
        else this.appendChild(n);
      }
      this._explicitText = null;
    },
    get textContent() {
      if (this._explicitText !== null) return this._explicitText;
      return this._chunks.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    },
    set textContent(v) {
      this._explicitText = String(v ?? "");
      this.children = [];
      this._chunks = [];
    },
    get innerHTML() { return this._html || serializeChunks(this._chunks); },
    set innerHTML(v) {
      this._html = String(v ?? "");
      this._explicitText = null;
      this._chunks = [];
      parseHtmlFragment(this._html, this, ctxRef);
    },
    querySelectorAll(sel) { return collectMatches(this, sel); },
    querySelector(sel) { return collectMatches(this, sel)[0] || null; },
    click() {
      if (this.disabled) return;
      let stopped = false;
      const ev = {
        target: this,
        currentTarget: this,
        preventDefault() {},
        stopPropagation() { stopped = true; },
      };
      let cur = this;
      while (cur && !stopped) {
        ev.currentTarget = cur;
        if (typeof cur.onclick === "function") cur.onclick.call(cur, ev);
        for (const fn of (cur.listeners.click || [])) fn.call(cur, ev);
        if (cur.attrs && typeof cur.attrs.onclick === "string") {
          vm.runInContext("(function(event){" + cur.attrs.onclick + "\n})", ctxRef.ctx).call(cur, ev);
        }
        cur = cur.parentNode;
      }
    },
  };
  return el;
}

function createHarness() {
  const ctxRef = { ctx: null };
  const elements = {};
  for (const id of [
    "kpi-cap", "kpi-load", "kpi-ram", "kpi-cpu", "kpi-ci", "kpi-ar", "kpi-ts",
    "slots-harness-pills", "sessions-harness-pills", "heartbeat",
    "cap-minus", "cap-plus", "cap-pause",
    "slots-container", "slots-count",
  ]) {
    elements[id] = makeElement(id.startsWith("cap-") ? "button" : "div", id, ctxRef);
  }

  const subscribers = [];
  const sent = [];
  const pendingMap = {};
  let confirmResult = true;
  const confirms = [];

  const storeObj = {
    blocks: {},
    commands: {},
    logs: [],
    ui: { harness: "all", search: "", selected: new Set(), expanded: new Set(), collapsed: new Set() },
    conn: { state: "open", boot: "b1", lastAt: 0 },
    subscribe(keys, fn) { subscribers.push({ keys, fn }); return () => {}; },
    setUI(patch) { Object.assign(this.ui, patch); notify(["ui"]); },
    command(path, body, opts) { sent.push({ path, body, opts }); return "cmd-test-1"; },
    pendingFor(key) { return pendingMap[key] || null; },
    connect() {},
  };

  function notify(changedKeys) {
    for (const s of subscribers) {
      if (changedKeys.some((k) => s.keys.includes(k) || (k.startsWith("block:") && s.keys.includes("block:*")))) {
        s.fn();
      }
    }
  }

  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(1700000000000);
      else super(...args);
    }
    static now() { return 1700000000000; }
  }

  const ctxObj = {
    Date: FixedDate,
    Math,
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    confirm: (msg) => { confirms.push(msg); return confirmResult; },
    window: { scrollY: 0, addEventListener() {} },
    document: {
      getElementById: (id) => elements[id] || null,
      querySelectorAll: (sel) => [],
      createElement: (tag) => makeElement(tag, "", ctxRef),
    },
    store: storeObj,
  };
  ctxRef.ctx = vm.createContext(ctxObj);
  vm.runInContext(utilSrc, ctxRef.ctx);
  vm.runInContext(appSrc, ctxRef.ctx);
  vm.runInContext(topbarSrc, ctxRef.ctx);
  vm.runInContext(slotsSrc, ctxRef.ctx);

  return {
    ctx: ctxRef.ctx,
    elements,
    subscribers,
    sent,
    pendingMap,
    confirms,
    setConfirm(v) { confirmResult = v; },
    pushSlotsBlock(data) {
      storeObj.blocks.slots = { data };
      if (!storeObj.blocks.sessions) storeObj.blocks.sessions = { data: { sessions: [] } };
      if (!storeObj.blocks.host) storeObj.blocks.host = { data: { load1: 0.5, load5: 0.25, mem_used_mb: 4096, mem_total_mb: 16384, docker_cpu: 12.3, stats: {} } };
      notify(["block:slots", "block:host", "block:sessions"]);
    },
    notify,
  };
}
"""

    def run_vm(self, script_body):
        js = self.VM_HARNESS + "\n" + script_body
        p = subprocess.run(
            ["node", "-e", js, text("static/util.js"), text("static/app.js"), text("static/topbar.js"), text("static/slots.js")],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)

    def test_slots_and_topbar_subscribe_to_pending(self):
        self.run_vm(r"""
const h = createHarness();
h.pushSlotsBlock({
  slots: [],
  machine_max: 5,
  projects: [{ prefix: "alpha", repo: "o/alpha", in_use: 1, limit: 4, paused: false }],
  project_default: 4,
});
assert.strictEqual(h.elements["kpi-cap"].textContent, "0 / 5");
assert.strictEqual(h.elements["kpi-cap"].classList.contains("is-pending"), false);
assert.strictEqual(h.elements["cap-minus"].disabled, false);

// Firing ONLY 'pending' must re-render both topbar and slots
h.pendingMap["cap"] = { value: 3 };
h.notify(["pending"]);
assert.strictEqual(h.elements["kpi-cap"].textContent, "0 / 3", "renderTopbar must subscribe to 'pending'");
assert.strictEqual(h.elements["kpi-cap"].classList.contains("is-pending"), true);
assert.strictEqual(h.elements["cap-minus"].disabled, true, "renderSlots must subscribe to 'pending'");
assert.strictEqual(h.elements["cap-plus"].disabled, true);
assert.strictEqual(h.elements["cap-pause"].disabled, true);

delete h.pendingMap["cap"];
h.notify(["pending"]);
assert.strictEqual(h.elements["kpi-cap"].textContent, "0 / 5");
assert.strictEqual(h.elements["kpi-cap"].classList.contains("is-pending"), false);
assert.strictEqual(h.elements["cap-minus"].disabled, false);
""")

    def test_cap_buttons_dispatch_pending_clamp_and_disable_bounds(self):
        self.run_vm(r"""
const h = createHarness();

// Normal cap = 5
h.pushSlotsBlock({ slots: [{ id: "a-slot-1", project: "a", slot: 1, holder: "h1", since: 1700000000 }], machine_max: 5, projects: [], project_default: 4 });
assert.strictEqual(h.elements["kpi-cap"].textContent, "1 / 5");
assert.strictEqual(h.elements["cap-minus"].disabled, false);
assert.strictEqual(h.elements["cap-plus"].disabled, false);
assert.strictEqual(h.elements["cap-pause"].disabled, false);

h.elements["cap-minus"].click();
h.elements["cap-plus"].click();
h.elements["cap-pause"].click();
assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/cap", body: { delta: -1 }, opts: { key: "cap", value: 4 } },
  { path: "/api/cap", body: { delta: 1 }, opts: { key: "cap", value: 6 } },
  { path: "/api/cap", body: { mode: "pause" }, opts: { key: "cap", value: 0 } },
]);

// Boundary cap = 1 -> cap-minus produces 0; cap = 19 -> cap-plus produces 20
h.sent.length = 0;
h.pushSlotsBlock({ slots: [], machine_max: 1, projects: [], project_default: 4 });
h.elements["cap-minus"].click();
h.pushSlotsBlock({ slots: [], machine_max: 19, projects: [], project_default: 4 });
h.elements["cap-plus"].click();
assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/cap", body: { delta: -1 }, opts: { key: "cap", value: 0 } },
  { path: "/api/cap", body: { delta: 1 }, opts: { key: "cap", value: 20 } },
]);

// Boundary cap = 0: minus and pause disabled, plus enabled and yields 1 (not 21 via `|| 20`)
h.sent.length = 0;
h.pushSlotsBlock({ slots: [{ id: "a-slot-1", project: "a", slot: 1, holder: "h1", since: 1700000000 }], machine_max: 0, projects: [], project_default: 4 });
assert.strictEqual(h.elements["cap-minus"].disabled, true);
assert.strictEqual(h.elements["cap-pause"].disabled, true);
assert.strictEqual(h.elements["cap-plus"].disabled, false);
h.elements["cap-plus"].click();
assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/cap", body: { delta: 1 }, opts: { key: "cap", value: 1 } },
]);

// Boundary cap = 20: plus disabled, minus and pause enabled -> minus yields 19
h.sent.length = 0;
h.pushSlotsBlock({ slots: [], machine_max: 20, projects: [], project_default: 4 });
assert.strictEqual(h.elements["cap-plus"].disabled, true);
assert.strictEqual(h.elements["cap-minus"].disabled, false);
assert.strictEqual(h.elements["cap-pause"].disabled, false);
h.elements["cap-minus"].click();
assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/cap", body: { delta: -1 }, opts: { key: "cap", value: 19 } },
]);

// Out-of-range block values: <= 0 disables #cap-minus; >= 20 disables #cap-plus
h.pushSlotsBlock({ slots: [], machine_max: -5, projects: [], project_default: 4 });
assert.strictEqual(h.elements["cap-minus"].disabled, true, "cap <= 0 must disable #cap-minus");
assert.strictEqual(h.elements["cap-plus"].disabled, false);
h.pushSlotsBlock({ slots: [], machine_max: 25, projects: [], project_default: 4 });
assert.strictEqual(h.elements["cap-plus"].disabled, true, "cap >= 20 must disable #cap-plus");
assert.strictEqual(h.elements["cap-minus"].disabled, false);

// Pending cap = 0 must render "0" + "Paused" and .is-pending (not fall back to machine_max = 5)
h.pushSlotsBlock({ slots: [{ id: "a-slot-1", project: "a", slot: 1, holder: "h1", since: 1700000000 }], machine_max: 5, projects: [], project_default: 4 });
h.pendingMap["cap"] = { value: 0 };
h.notify(["pending"]);
assert.match(h.elements["kpi-cap"].textContent, /1\s*\/\s*0/);
assert.match(h.elements["kpi-cap"].textContent, /paused/i);
assert.strictEqual(h.elements["kpi-cap"].classList.contains("is-pending"), true);
assert.strictEqual(h.elements["cap-minus"].disabled, true);
assert.strictEqual(h.elements["cap-plus"].disabled, true);
assert.strictEqual(h.elements["cap-pause"].disabled, true);
""")

    def test_slot_card_renders_machine_slot_first_format(self):
        self.run_vm(r"""
const h = createHarness();
h.pushSlotsBlock({
  slots: [
    { id: "0-slot-5-host_repo", project: "host_repo", slot: 5, holder: "issue-61", running: true, cpu: 1, mem_mb: 64, since: 1700000000 },
    { id: "0-slot-12-ops-console", project: "ops-console", slot: 12, holder: "issue-62", running: true, cpu: 2, mem_mb: 64, since: 1700000000 },
  ],
  machine_max: 5,
  projects: [],
  project_default: 4,
});
const html = h.elements["slots-container"].innerHTML;
assert.ok(html.includes("#5 · host_repo"), `expected '#5 · host_repo' in ${html}`);
assert.ok(html.includes("#12 · ops-console"), `expected '#12 · ops-console' in ${html}`);
assert.ok(!html.includes("host_repo #5"), "must not render legacy 'project #slot' order");
assert.ok(!html.includes("ops-console #12"), "must not render legacy 'project #slot' order");
""")

    def test_slot_card_evict_release_pending_and_confirm(self):
        self.run_vm(r"""
const h = createHarness();

function cardFor(prefix, slotNum) {
  const cards = h.elements["slots-container"].querySelectorAll(".slot-card");
  return cards.find((c) => c.textContent.includes(`#${slotNum} · ${prefix}`));
}

function btnByText(root, label) {
  return root.querySelectorAll("button").find((b) => b.textContent.trim() === label);
}

h.pushSlotsBlock({
  slots: [
    { id: "demo-slot-1", project: "demo", slot: 1, holder: "issue-1", running: true, cpu: 1, mem_mb: 64, since: 1700000000 },
    { id: "demo-slot-2", project: "demo", slot: 2, holder: "issue-2", running: true, cpu: 2, mem_mb: 64, since: 1700000000 },
  ],
  machine_max: 5,
  projects: [],
  project_default: 4,
});

const c1 = cardFor("demo", 1);
const c2 = cardFor("demo", 2);
assert.ok(c1 && c2, "both slot cards must render");
assert.strictEqual(btnByText(c1, "Release").disabled, false);
assert.strictEqual(btnByText(c1, "Evict").disabled, false);

btnByText(c1, "Release").click();

// Declining confirm on Evict must not dispatch
h.setConfirm(false);
btnByText(c1, "Evict").click();
assert.strictEqual(h.confirms.length, 1);

// Accepting confirm on Evict dispatches force mode with pending key/value
h.setConfirm(true);
btnByText(c1, "Evict").click();
assert.strictEqual(h.confirms.length, 2);

assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/evict", body: { target: "demo-slot-1", mode: "release" }, opts: { key: "evict:demo-slot-1", value: "release" } },
  { path: "/api/evict", body: { target: "demo-slot-1", mode: "force" }, opts: { key: "evict:demo-slot-1", value: "force" } },
]);

// While evict:demo-slot-1 is pending, BOTH Release and Evict on card 1 are disabled; card 2 stays enabled
for (const mode of ["release", "force"]) {
  h.pendingMap["evict:demo-slot-1"] = { value: mode };
  h.notify(["pending"]);
  const c1p = cardFor("demo", 1);
  const c2p = cardFor("demo", 2);
  assert.strictEqual(btnByText(c1p, "Release").disabled, true, `Release must be disabled while ${mode} pending`);
  assert.strictEqual(btnByText(c1p, "Evict").disabled, true, `Evict must be disabled while ${mode} pending`);
  assert.strictEqual(btnByText(c2p, "Release").disabled, false, "sibling card Release must remain enabled");
  assert.strictEqual(btnByText(c2p, "Evict").disabled, false, "sibling card Evict must remain enabled");
}
""")

    def test_slot_card_renders_ephemeral_toolbox_and_escaped_repos_badges(self):
        self.run_vm(r"""
const h = createHarness();

h.pushSlotsBlock({
  slots: [
    {
      id: "demo-slot-1",
      project: "demo",
      slot: 1,
      holder: "issue-51",
      running: true,
      cpu: 1,
      mem_mb: 64,
      since: 1700000000,
      ephemeral: true,
      toolbox: true,
      repos: [
        { alias: "docs", path: "docs/fix", mode: "rw" },
        { alias: 'a<b>"c', path: "", mode: "" },
      ],
    },
    {
      id: "demo-slot-2",
      project: "demo",
      slot: 2,
      holder: "issue-52",
      running: true,
      cpu: 1,
      mem_mb: 64,
      since: 1700000000,
      ephemeral: false,
      toolbox: false,
      repos: [],
    },
  ],
  machine_max: 5,
});

const cards = h.elements["slots-container"].querySelectorAll(".slot-card");
assert.strictEqual(cards.length, 2);

const c1 = cards[0];
const c1Badges = c1.querySelectorAll(".badge").map((b) => ({
  cls: b.className,
  text: b.textContent.trim(),
  title: b.getAttribute("title"),
}));
assert.ok(
  c1Badges.some((b) => b.cls.includes("badge-yellow") && /ephemeral/i.test(b.text)),
  "active ephemeral slot must render yellow ephemeral badge"
);
assert.ok(
  c1Badges.some((b) => b.cls.includes("badge-custom") && /toolbox/i.test(b.text)),
  "active toolbox slot must render custom toolbox badge"
);
assert.ok(
  c1Badges.some((b) => b.cls.includes("badge-custom") && b.text === "docs (rw)" && b.title === "docs/fix"),
  "slot must render docs (rw) badge with title docs/fix"
);
assert.ok(
  c1Badges.some((b) => b.cls.includes("badge-custom") && b.text === 'a<b>"c (ro)' && b.title === 'a<b>"c'),
  "slot must escape repo alias and fall back mode to ro and title to alias"
);
assert.ok(!c1.innerHTML.includes('a<b>"c'), "raw unescaped HTML must not appear in card innerHTML");

const c2 = cards[1];
const c2BadgeTexts = c2.querySelectorAll(".badge").map((b) => b.textContent.trim().toLowerCase());
assert.ok(!c2BadgeTexts.includes("ephemeral"), "non-ephemeral slot must not render ephemeral badge");
assert.ok(!c2BadgeTexts.includes("toolbox"), "non-toolbox slot must not render toolbox badge");
assert.ok(!c2.innerHTML.includes("(ro)") && !c2.innerHTML.includes("(rw)"), "slot without repos must not render repo badges");
""")


class TestCleanersRescueMultiRepo(unittest.TestCase):
    def test_render_rescue_displays_sub_repos_and_escapes_html(self):
        js = r"""
const vm = require("vm");
const assert = require("assert");
const [utilSrc, appSrc, cleanersSrc] = process.argv.slice(1);

const elements = {};
for (const id of [
  "fast-cleaner-status", "fast-cleaner-last", "fast-cleaner-log",
  "slow-cleaner-status", "slow-cleaner-last", "slow-cleaner-log",
  "rescue-tbody", "rescue-count", "btn-fast-reap", "btn-slow-check",
]) {
  elements[id] = { id, textContent: "", innerHTML: "", onclick: null };
}

const subscribers = [];
const ctxObj = {
  Date,
  Math,
  console,
  setTimeout,
  clearTimeout,
  window: { scrollY: 0, addEventListener() {} },
  document: {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
  },
  store: {
    blocks: {
      slots: { data: { slots: [], machine_max: 5 } },
      sessions: { data: { sessions: [] } },
      cleaners: {
        data: {
          cleaners: {},
          rescue_bundles: [
            {
              id: "proj/slot-1",
              project: "proj",
              lease: "slot-1",
              branches: [],
              repos: [{ alias: "docs", mode: "rw", branches: ["fix/1"] }],
              size_bytes: 2048,
              age: 60,
            },
            {
              id: "proj/slot-2",
              project: "proj",
              lease: "slot-2",
              branches: ["main"],
              repos: [{ alias: "<script>x</script>", mode: "ro", branches: [] }],
              size_bytes: 1024,
              age: 30,
            },
            {
              id: "proj/slot-3",
              project: "proj",
              lease: "slot-3",
              branches: [],
              repos: [],
              size_bytes: 512,
              age: 10,
            },
          ],
        },
      },
    },
    commands: {},
    logs: [],
    subscribe(keys, fn) { subscribers.push({ keys, fn }); },
    command() {},
    connect() {},
  },
};

const ctx = vm.createContext(ctxObj);
vm.runInContext(utilSrc, ctx);
vm.runInContext(appSrc, ctx);
vm.runInContext(cleanersSrc, ctx);

for (const s of subscribers) {
  if (s.keys.includes("block:*") || s.keys.includes("block:cleaners")) s.fn();
}

const html = elements["rescue-tbody"].innerHTML;
const rows = [];
const trRe = /<tr\b[^>]*>([\s\S]*?)<\/tr>/g;
let m;
while ((m = trRe.exec(html)) !== null) {
  const tds = [];
  const tdRe = /<td\b[^>]*>([\s\S]*?)<\/td>/g;
  let tdM;
  while ((tdM = tdRe.exec(m[1])) !== null) tds.push(tdM[1]);
  rows.push(tds);
}

assert.strictEqual(rows.length, 3);
// Row 1: sub-repo-only archive shows docs (with mode/branch) instead of just '–'
const r1Branch = rows[0][1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
assert.ok(r1Branch.includes("docs"), `sub-repo-only row must show alias docs, got: ${r1Branch}`);
assert.ok(r1Branch.includes("rw") || r1Branch.includes("fix/1"), `sub-repo-only row must show mode/branches, got: ${r1Branch}`);
assert.notStrictEqual(r1Branch, "–");

// Row 2: root + sub-repo archive shows main and escaped sub-repo alias
const r2Branch = rows[1][1];
assert.ok(r2Branch.includes("main"), r2Branch);
assert.ok(!r2Branch.includes("<script>x</script>"), "sub-repo alias must be HTML-escaped");
assert.ok(r2Branch.includes("&lt;script&gt;x&lt;/script&gt;"), r2Branch);

// Row 3: no root branches and no sub-repos -> '–'
const r3Branch = rows[2][1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
assert.strictEqual(r3Branch, "–");
"""
        p = subprocess.run(
            ["node", "-e", js, text("static/util.js"), text("static/app.js"), text("static/cleaners.js")],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)


class TestSessionsGroupingAndSorting(unittest.TestCase):
    SESSION_VM_HARNESS = r"""
const vm = require("vm");
const assert = require("assert");
const [utilSrc, appSrc, sessionsSrc] = process.argv.slice(1);

function decodeEntities(s) {
  return String(s)
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, ">")
    .replace(/&lt;/g, "<")
    .replace(/&amp;/g, "&");
}

function toCamel(s) {
  return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

function parseHtmlFragment(html, root, ctxRef) {
  root.children = [];
  const stack = [root];
  const voidTags = new Set(["br", "hr", "img", "input", "meta", "link"]);
  const tagRe = /<\/?([a-zA-Z0-9-]+)([^>]*)>/g;
  let lastIdx = 0;
  let m;
  while ((m = tagRe.exec(html)) !== null) {
    if (m.index > lastIdx) {
      const txt = decodeEntities(html.slice(lastIdx, m.index));
      stack[stack.length - 1]._chunks.push(txt);
    }
    lastIdx = tagRe.lastIndex;
    const rawTag = m[0];
    const tag = m[1].toLowerCase();
    const attrStr = m[2] || "";
    const isClose = rawTag.startsWith("</");
    if (isClose) {
      while (stack.length > 1 && stack[stack.length - 1].tagName.toLowerCase() !== tag) {
        stack.pop();
      }
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = makeElement(tag, "", ctxRef);
    const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let am;
    while ((am = attrRe.exec(attrStr)) !== null) {
      const k = am[1];
      const rawVal = am[2] ?? am[3] ?? am[4];
      const v = rawVal !== undefined ? decodeEntities(rawVal) : "";
      el.attrs[k] = v;
      if (k === "id") el.id = v;
      else if (k === "class") el.className = v;
      else if (k === "disabled") el.disabled = true;
      else if (k === "checked") el.checked = true;
      else if (k.startsWith("data-")) el.dataset[toCamel(k.slice(5))] = v;
    }
    const parent = stack[stack.length - 1];
    el.parentNode = parent;
    parent.children.push(el);
    parent._chunks.push(el);
    if (!voidTags.has(tag) && !rawTag.endsWith("/>")) {
      stack.push(el);
    }
  }
  if (lastIdx < html.length) {
    stack[stack.length - 1]._chunks.push(decodeEntities(html.slice(lastIdx)));
  }
}

function matchesSingleSelector(el, sel) {
  let s = sel.trim();
  if (!s) return false;
  const tagM = /^[a-zA-Z0-9-]+/.exec(s);
  if (tagM) {
    if (el.tagName.toLowerCase() !== tagM[0].toLowerCase()) return false;
    s = s.slice(tagM[0].length);
  }
  while (s.length > 0) {
    if (s[0] === "#") {
      const m = /^#([a-zA-Z0-9_-]+)/.exec(s);
      if (!m || el.id !== m[1]) return false;
      s = s.slice(m[0].length);
    } else if (s[0] === ".") {
      const m = /^\.([a-zA-Z0-9_-]+)/.exec(s);
      if (!m || !el.classList.contains(m[1])) return false;
      s = s.slice(m[0].length);
    } else if (s[0] === "[") {
      const m = /^\[([a-zA-Z0-9_:-]+)(?:=["']?([^"'\]]*)["']?)?\]/.exec(s);
      if (!m) return false;
      const val = el.getAttribute(m[1]);
      if (val === null) return false;
      if (m[2] !== undefined && val !== m[2]) return false;
      s = s.slice(m[0].length);
    } else {
      return false;
    }
  }
  return true;
}

function matchesSelectorChain(el, sel) {
  const tokens = sel.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return false;
  if (!matchesSingleSelector(el, tokens[tokens.length - 1])) return false;
  let cur = el.parentNode;
  for (let i = tokens.length - 2; i >= 0; i--) {
    while (cur && !matchesSingleSelector(cur, tokens[i])) cur = cur.parentNode;
    if (!cur) return false;
    cur = cur.parentNode;
  }
  return true;
}

function collectMatches(root, selector) {
  const parts = selector.split(",").map((p) => p.trim()).filter(Boolean);
  const out = [];
  function walk(node) {
    for (const ch of node.children) {
      if (parts.some((p) => matchesSelectorChain(ch, p))) out.push(ch);
      walk(ch);
    }
  }
  walk(root);
  return out;
}

function escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function serializeChunks(chunks) {
  return chunks.map((c) => {
    if (typeof c === "string") return escHtml(c);
    const tag = c.tagName.toLowerCase();
    const attrs = Object.entries(c.attrs).map(([k, v]) => ` ${k}="${escHtml(v)}"`).join("");
    return `<${tag}${attrs}>${serializeChunks(c._chunks)}</${tag}>`;
  }).join("");
}

function makeElement(tag, id, ctxRef) {
  const classSet = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    id: id || "",
    value: "",
    checked: false,
    attrs: {},
    dataset: {},
    style: {},
    children: [],
    _chunks: [],
    _explicitText: null,
    _html: "",
    _disabled: false,
    parentNode: null,
    listeners: {},
    onclick: null,
    onchange: null,
    get disabled() { return this._disabled; },
    set disabled(v) { this._disabled = Boolean(v); },
    get className() { return Array.from(classSet).join(" "); },
    set className(v) {
      classSet.clear();
      String(v || "").split(/\s+/).filter(Boolean).forEach((c) => classSet.add(c));
    },
    classList: {
      add(...cs) { cs.forEach((c) => c && classSet.add(c)); },
      remove(...cs) { cs.forEach((c) => classSet.delete(c)); },
      contains(c) { return classSet.has(c); },
      toggle(c, force) {
        if (force === undefined) {
          if (classSet.has(c)) { classSet.delete(c); return false; }
          classSet.add(c); return true;
        }
        if (force) { classSet.add(c); return true; }
        classSet.delete(c); return false;
      },
    },
    getAttribute(k) {
      if (k === "id") return this.id || null;
      if (k === "class") return this.className || null;
      if (k === "disabled") return this._disabled ? "" : null;
      if (k.startsWith("data-")) {
        const dk = toCamel(k.slice(5));
        if (dk in this.dataset) return String(this.dataset[dk]);
      }
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    setAttribute(k, v) {
      const sv = String(v);
      this.attrs[k] = sv;
      if (k === "id") this.id = sv;
      else if (k === "class") this.className = sv;
      else if (k === "disabled") this._disabled = true;
      else if (k.startsWith("data-")) this.dataset[toCamel(k.slice(5))] = sv;
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    get textContent() {
      if (this._explicitText !== null) return this._explicitText;
      return this._chunks.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    },
    set textContent(v) {
      this._explicitText = String(v ?? "");
      this.children = [];
      this._chunks = [];
    },
    get innerHTML() { return this._html || serializeChunks(this._chunks); },
    set innerHTML(v) {
      this._html = String(v ?? "");
      this._explicitText = null;
      this._chunks = [];
      parseHtmlFragment(this._html, this, ctxRef);
    },
    querySelectorAll(sel) { return collectMatches(this, sel); },
    querySelector(sel) { return collectMatches(this, sel)[0] || null; },
    click() {
      if (this.disabled) return;
      let stopped = false;
      const ev = {
        target: this,
        currentTarget: this,
        preventDefault() {},
        stopPropagation() { stopped = true; },
      };
      let cur = this;
      while (cur && !stopped) {
        ev.currentTarget = cur;
        if (typeof cur.onclick === "function") cur.onclick.call(cur, ev);
        for (const fn of (cur.listeners.click || [])) fn.call(cur, ev);
        if (cur.attrs && typeof cur.attrs.onclick === "string") {
          vm.runInContext("(function(event){" + cur.attrs.onclick + "\n})", ctxRef.ctx).call(cur, ev);
        }
        cur = cur.parentNode;
      }
    },
  };
  return el;
}

function createSessionHarness(opts) {
  const withCollapsedGroups = !opts || opts.withCollapsedGroups !== false;
  const ctxRef = { ctx: null };
  const elements = {};
  for (const id of [
    "sessions-tbody", "sessions-count", "chk-all-sessions", "session-search",
    "btn-select-active", "btn-clear-selection", "btn-send-msg", "msg-input",
  ]) {
    elements[id] = makeElement(id === "sessions-tbody" ? "tbody" : "div", id, ctxRef);
  }

  const subscribers = [];
  const setUICalls = [];
  const uiObj = { harness: "all", search: "", selected: new Set(), expanded: new Set(), collapsed: new Set() };
  if (withCollapsedGroups) uiObj.collapsedGroups = new Set();

  const storeObj = {
    blocks: { slots: { data: { slots: [], machine_max: 20 } }, sessions: { data: { default_limit: 2, sessions: [] } } },
    commands: {},
    logs: [],
    ui: uiObj,
    subscribe(keys, fn) { subscribers.push({ keys, fn }); },
    setUI(patch) {
      setUICalls.push(patch);
      Object.assign(this.ui, patch);
      notify(["ui"]);
    },
    command() {},
    pendingFor() { return null; },
    connect() {},
  };

  function notify(changedKeys) {
    for (const s of subscribers) {
      if (changedKeys.some((k) => s.keys.includes(k) || (k.startsWith("block:") && s.keys.includes("block:*")))) {
        s.fn();
      }
    }
  }

  const ctxObj = {
    Date: { now: () => 1700000000000 },
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    alert: () => {},
    window: { scrollY: 0, addEventListener() {} },
    document: {
      getElementById: (id) => elements[id] || null,
      querySelectorAll: (sel) => elements["sessions-tbody"].querySelectorAll(sel),
    },
    store: storeObj,
  };
  ctxRef.ctx = vm.createContext(ctxObj);
  vm.runInContext(utilSrc, ctxRef.ctx);
  vm.runInContext(appSrc, ctxRef.ctx);
  vm.runInContext(sessionsSrc, ctxRef.ctx);

  return {
    ctx: ctxRef.ctx,
    elements,
    store: storeObj,
    setUICalls,
    pushSessions(sessions) {
      storeObj.blocks.sessions = { data: { default_limit: 2, sessions } };
      notify(["block:sessions"]);
    },
    setSearch(q) {
      elements["session-search"].value = q;
      for (const fn of (elements["session-search"].listeners.input || [])) fn({ target: elements["session-search"] });
    },
    notify,
  };
}
"""

    def run_vm(self, script_body):
        js = self.SESSION_VM_HARNESS + "\n" + script_body
        p = subprocess.run(
            ["node", "-e", js, text("static/util.js"), text("static/app.js"), text("static/sessions.js")],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)

    def test_project_name_of_helper(self):
        self.run_vm(r"""
const h = createSessionHarness();
const fn = vm.runInContext("typeof projectNameOf === 'function' ? projectNameOf : null", h.ctx);
assert.strictEqual(typeof fn, "function", "projectNameOf must be defined in static/sessions.js");
const cases = [
  ["C:\\Users\\user\\Projects\\demo-app\\demo-app-ai-extension", "demo-app-ai-extension"],
  ["  C:\\Users\\user\\Projects\\demo\\  ", "demo"],
  ["\\\\wsl.localhost\\ubuntu-24.04\\home\\hanzh\\Projects\\media-stick", "media-stick"],
  ["/home/user/Projects/host_repo/", "host_repo"],
  ["/home/user/Projects/host_repo", "host_repo"],
  ["", "(no project)"],
  [null, "(no project)"],
  [undefined, "(no project)"],
  ["   ", "(no project)"],
  ["C:", "(no project)"],
  ["C:\\", "(no project)"],
  ["C:/", "(no project)"],
  ["/", "(no project)"],
  ["///", "(no project)"],
  ["\\\\", "(no project)"],
];
for (const [input, expected] of cases) {
  assert.strictEqual(fn(input), expected, `projectNameOf(${JSON.stringify(input)})`);
}
""")

    def test_harness_and_project_grouping_order_and_badges(self):
        self.run_vm(r"""
const h = createSessionHarness();
h.pushSessions([
  { id: "w10", ref: "r-w10", harness: "worker-10", title: "W10", cwd: "/w/p1", held_slots: 0, tree_held: 0, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "w2", ref: "r-w2", harness: "worker-2", title: "W2", cwd: "/w/p1", held_slots: 2, tree_held: 2, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "unk", ref: "r-unk", harness: "", title: "Unk", cwd: "/w/p1", held_slots: 0, tree_held: 0, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-z", ref: "r-cl-z", harness: "claude", title: "Zebra Session", cwd: "/home/user/Projects/zebra", held_slots: 0, tree_held: 0, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-np", ref: "r-cl-np", harness: "claude", title: "No Proj Session", cwd: "C:", held_slots: 1, tree_held: 1, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-p10", ref: "r-cl-p10", harness: "claude", title: "Proj Ten", cwd: "/home/user/Projects/proj-10", held_slots: 0, tree_held: 0, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-p2", ref: "r-cl-p2", harness: "claude", title: "Proj Two", cwd: "/home/user/Projects/proj-2", held_slots: 0, tree_held: 0, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-a1", ref: "r-cl-a1", harness: "claude", title: "Alpha Win", cwd: "C:\\Users\\user\\Projects\\alpha", held_slots: 2, tree_held: 3, limit: 2, tree_over_limit: true, is_active: true, last_active: 1699999900 },
  { id: "cl-a1-c", ref: "r-cl-a1-c", parent_id: "cl-a1", root_id: "cl-a1", harness: "claude", title: "Alpha Sub", cwd: "C:\\Users\\user\\Projects\\alpha", held_slots: 1, tree_held: 0, limit: 1, is_active: true, last_active: 1699999900 },
  { id: "cl-a2", ref: "r-cl-a2", harness: "claude", title: "Alpha Posix", cwd: "/home/user/Projects/Alpha/", held_slots: 1, tree_held: 1, limit: 2, is_active: false, last_active: 1699999800 },
  { id: "agy-1", ref: "r-agy-1", harness: "antigravity", title: "Agy Session", cwd: "\\\\wsl.localhost\\ubuntu-24.04\\home\\hanzh\\Projects\\media-stick", held_slots: 3, tree_held: 3, limit: 2, is_active: true, last_active: 1699999900 },
]);

const tbody = h.elements["sessions-tbody"];
const harnessRows = tbody.querySelectorAll("tr.sess-group-harness");
assert.deepStrictEqual(
  harnessRows.map((r) => r.getAttribute("data-group")),
  ["harness:antigravity", "harness:claude", "harness:unknown", "harness:worker-2", "harness:worker-10"],
  "harness groups must be sorted by display label with natural numeric order"
);
for (const hr of harnessRows) {
  const td = hr.querySelector("td");
  assert.ok(td && td.getAttribute("colspan") === "4", "harness row must span 4 columns");
  assert.ok(hr.textContent.includes("▾"), "expanded harness row must show down caret");
  assert.ok(hr.querySelector(".badge"), "harness row must include harnessBadge");
}

// Held-slots total badge: worker-2 has 2 (>0), worker-10 has 0
const w2Header = harnessRows.find((r) => r.getAttribute("data-group") === "harness:worker-2");
const w10Header = harnessRows.find((r) => r.getAttribute("data-group") === "harness:worker-10");
assert.ok(/\b2\b/.test(w2Header.textContent), "harness header must show held-slots total when > 0");
assert.ok(!/slot/i.test(w10Header.textContent) && !/\b0\b.*held/i.test(w10Header.textContent), "harness header must omit held-slots badge when 0");

// Zero tr.sess-group-project rows
assert.strictEqual(tbody.querySelectorAll("tr.sess-group-project").length, 0, "project header rows must be removed");

// Under claude: roots grouped by projectNameOf (natural sort, (no project) last), then session title
const allRows = tbody.querySelectorAll("tr");
const claudeStart = allRows.findIndex((r) => r.getAttribute("data-group") === "harness:claude");
const unknownStart = allRows.findIndex((r) => r.getAttribute("data-group") === "harness:unknown");
const claudeSlice = allRows.slice(claudeStart + 1, unknownStart);
const claudeRoots = claudeSlice.filter((r) => r.classList.contains("sess-root"));
assert.deepStrictEqual(
  claudeRoots.map((r) => r.querySelector(".session-chk").getAttribute("data-ref")),
  ["r-cl-a2", "r-cl-a1", "r-cl-p2", "r-cl-p10", "r-cl-z", "r-cl-np"],
  "roots within harness must be grouped by project (natural sort, (no project) last) and sorted by title"
);

// sess-proj-sep: only on first root of each new project group (not first root of harness, not same-project sibling, not child)
const refOf = (r) => r.querySelector(".session-chk").getAttribute("data-ref");
const hasSep = (ref) => claudeSlice.find((r) => refOf(r) === ref).classList.contains("sess-proj-sep");
assert.strictEqual(hasSep("r-cl-a2"), false, "first root in harness must not have sess-proj-sep");
assert.strictEqual(hasSep("r-cl-a1"), false, "same-project sibling must not have sess-proj-sep");
assert.strictEqual(hasSep("r-cl-a1-c"), false, "child row must not have sess-proj-sep");
assert.strictEqual(hasSep("r-cl-p2"), true, "first root of proj-2 must have sess-proj-sep");
assert.strictEqual(hasSep("r-cl-p10"), true, "first root of proj-10 must have sess-proj-sep");
assert.strictEqual(hasSep("r-cl-z"), true, "first root of zebra must have sess-proj-sep");
assert.strictEqual(hasSep("r-cl-np"), true, "first root of (no project) must have sess-proj-sep");

const agyRoot = tbody.querySelectorAll("tr.sess-root").find((r) => refOf(r) === "r-agy-1");
assert.strictEqual(agyRoot.classList.contains("sess-proj-sep"), false, "first root of antigravity must not have sess-proj-sep");

// All session rows (roots & children) have 4 columns, no harness badge, short project name in .sess-project, and no full path text
const sessionRows = tbody.querySelectorAll("tr.sess-root, tr.sess-child");
assert.strictEqual(sessionRows.length, 11);
for (const sr of sessionRows) {
  const tds = sr.querySelectorAll("td");
  assert.strictEqual(tds.length, 4, "session row must have 4-column layout");
  assert.ok(!sr.innerHTML.includes("badge-antigravity") && !sr.innerHTML.includes("badge-claude") && !sr.innerHTML.includes("badge-custom"),
    `session row must not repeat harness badge: ${sr.innerHTML}`);
  const projEl = tds[1].querySelector(".sess-project");
  assert.ok(projEl, "session row column 2 must render .sess-project");
  const titleCellText = tds[1].textContent;
  assert.ok(!titleCellText.includes("C:\\Users\\"), `session row text must omit Windows path, got: ${titleCellText}`);
  assert.ok(!titleCellText.includes("\\\\wsl.localhost\\"), `session row text must omit UNC path, got: ${titleCellText}`);
  assert.ok(!titleCellText.includes("/home/user/Projects/"), `session row text must omit POSIX path, got: ${titleCellText}`);
  assert.ok(!titleCellText.includes("/w/"), `session row text must omit /w/ path, got: ${titleCellText}`);
}
assert.strictEqual(agyRoot.querySelector(".sess-project").textContent.trim(), "media-stick");
assert.strictEqual(claudeRoots[5].querySelector(".sess-project").textContent.trim(), "(no project)");
assert.ok(
  claudeRoots[1].querySelector("[title]").getAttribute("title").includes("C:\\Users\\user\\Projects\\alpha"),
  "session title tooltip must carry full cwd"
);

// Empty state spans 4 columns
h.setSearch("nonexistent-session-xyz");
const emptyTd = tbody.querySelector("td.empty-state");
assert.ok(emptyTd && emptyTd.getAttribute("colspan") === "4", "empty-state td must have colspan=4");
""")

    def test_deterministic_alphabetical_session_sorting_no_jitter(self):
        self.run_vm(r"""
const h = createSessionHarness();
const makeData = (flip) => [
  { id: "s-np", ref: "r-np", harness: "claude", title: "AAA NoProj", cwd: "", held_slots: flip ? 0 : 9, is_active: !flip, last_active: flip ? 10 : 9000 },
  { id: "s-p10", ref: "r-p10", harness: "claude", title: "AAA P10", cwd: "/w/proj-10", held_slots: flip ? 9 : 0, is_active: flip, last_active: flip ? 9000 : 10 },
  { id: "s-10", ref: "r-10", harness: "claude", title: "Task 10", cwd: "/w/proj-2", held_slots: flip ? 0 : 9, is_active: !flip, last_active: flip ? 10 : 9000 },
  { id: "s-2", ref: "r-2", harness: "claude", title: "Task 2", cwd: "/w/proj-2", held_slots: flip ? 8 : 0, is_active: flip, last_active: flip ? 9999 : 100 },
  { id: "s-a", ref: "r-a", harness: "claude", title: "alpha", cwd: "/w/proj-2", held_slots: 1, tree_held: 7, limit: 2, tree_over_limit: true, is_active: false, last_active: 500 },
  { id: "s-same-b", ref: "r-same-b", harness: "claude", title: "Same", cwd: "/w/proj-2", held_slots: flip ? 0 : 5, is_active: !flip, last_active: flip ? 1 : 3000 },
  { id: "s-same-a", ref: "r-same-a", harness: "claude", title: "Same", cwd: "/w/proj-2", held_slots: flip ? 6 : 0, is_active: flip, last_active: flip ? 4000 : 10 },
  { id: "s-notitle", ref: "a-notitle", harness: "claude", title: "", cwd: "/w/proj-2", held_slots: 0, is_active: false, last_active: 5 },
  { id: "c-10", ref: "cr-10", parent_id: "s-a", root_id: "s-a", harness: "claude", title: "Sub 10", cwd: "/w/proj-2", held_slots: flip ? 0 : 4, is_active: !flip, last_active: flip ? 1 : 9999 },
  { id: "c-2", ref: "cr-2", parent_id: "s-a", root_id: "s-a", harness: "claude", title: "Sub 2", cwd: "/w/proj-2", held_slots: flip ? 5 : 0, is_active: flip, last_active: flip ? 8888 : 1 },
  { id: "c-sb", ref: "cr-b", parent_id: "s-a", root_id: "s-a", harness: "claude", title: "Sub Same", cwd: "/w/proj-2", held_slots: flip ? 0 : 2, is_active: !flip, last_active: 500 },
  { id: "c-sa", ref: "cr-a", parent_id: "s-a", root_id: "s-a", harness: "claude", title: "Sub Same", cwd: "/w/proj-2", held_slots: flip ? 3 : 0, is_active: flip, last_active: 10 },
];

h.pushSessions(makeData(false));
const rootRefs1 = h.elements["sessions-tbody"].querySelectorAll("tr.sess-root .session-chk").map((c) => c.getAttribute("data-ref"));
const childRefs1 = h.elements["sessions-tbody"].querySelectorAll("tr.sess-child .session-chk").map((c) => c.getAttribute("data-ref"));

assert.deepStrictEqual(rootRefs1, ["a-notitle", "r-a", "r-same-a", "r-same-b", "r-2", "r-10", "r-p10", "r-np"]);
assert.deepStrictEqual(childRefs1, ["cr-2", "cr-10", "cr-a", "cr-b"]);

// Mutate held_slots, is_active, last_active across SSE tick -> order must not jitter
h.pushSessions(makeData(true));
const rootRefs2 = h.elements["sessions-tbody"].querySelectorAll("tr.sess-root .session-chk").map((c) => c.getAttribute("data-ref"));
const childRefs2 = h.elements["sessions-tbody"].querySelectorAll("tr.sess-child .session-chk").map((c) => c.getAttribute("data-ref"));

assert.deepStrictEqual(rootRefs2, rootRefs1, "root session order must not change when held_slots/is_active/last_active mutate");
assert.deepStrictEqual(childRefs2, childRefs1, "child session order must not change when held_slots/is_active/last_active mutate");
""")

    def test_collapse_expand_persistence_and_search_auto_open(self):
        self.run_vm(r"""
// Omit collapsedGroups from store.ui to verify defensive initialization
const h = createSessionHarness({ withCollapsedGroups: false });
const data = [
  { id: "agy-1", ref: "r-agy-1", harness: "antigravity", title: "Agy Work", cwd: "/w/beta", held_slots: 1, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-1", ref: "r-cl-1", harness: "claude", title: "Alpha Work", cwd: "/home/user/Projects/alpha", held_slots: 1, limit: 2, is_active: true, last_active: 1699999900 },
  { id: "cl-2", ref: "r-cl-2", harness: "claude", title: "Zebra Work", cwd: "/home/user/Projects/zebra", held_slots: 0, limit: 2, is_active: true, last_active: 1699999900 },
];
h.pushSessions(data);

const tbody = h.elements["sessions-tbody"];
const clickGroup = (key) => {
  const row = tbody.querySelector(`[data-group="${key}"]`);
  assert.ok(row, `group row ${key} must exist`);
  (row.querySelector(".group-toggle") || row).click();
};

assert.strictEqual(tbody.querySelectorAll("tr.sess-root").length, 3);
assert.strictEqual(tbody.querySelectorAll("tr.sess-group-project").length, 0);

// Collapse harness:claude -> hides claude sessions, keeps antigravity visible
clickGroup("harness:claude");
assert.ok(h.setUICalls.length >= 1, "toggling group must call store.setUI");
assert.strictEqual(tbody.querySelectorAll("tr.sess-root").length, 1);
assert.strictEqual(tbody.querySelectorAll("tr.sess-group-project").length, 0);
assert.ok(tbody.querySelector('[data-group="harness:claude"]').textContent.includes("▸"), "collapsed harness shows right caret");

// Survives SSE re-render
h.pushSessions([...data]);
assert.strictEqual(tbody.querySelectorAll("tr.sess-root").length, 1, "collapsed harness must survive SSE re-render");

// Expand harness:claude again
clickGroup("harness:claude");
assert.strictEqual(tbody.querySelectorAll("tr.sess-root").length, 3);
assert.ok(tbody.querySelector('[data-group="harness:claude"]').textContent.includes("▾"), "expanded harness shows down caret");

// Collapse harness:claude, then search matching projectNameOf(cwd) auto-opens collapsed harness
clickGroup("harness:claude");
assert.strictEqual(tbody.querySelectorAll("tr.sess-root").length, 1);
h.setSearch("alpha");
const searchRoots = tbody.querySelectorAll("tr.sess-root");
assert.deepStrictEqual(
  searchRoots.map((r) => r.querySelector(".session-chk").getAttribute("data-ref")),
  ["r-cl-1"],
  "search must match projectNameOf(cwd) and auto-open collapsed harness"
);
assert.strictEqual(searchRoots[0].classList.contains("sess-proj-sep"), false, "only visible root in harness must not have sess-proj-sep");

// Clearing search restores collapsed harness state
h.setSearch("");
assert.strictEqual(tbody.querySelectorAll("tr.sess-root").length, 1, "clearing search restores collapsed harness state");
""")

    def test_store_ui_and_page_css_for_session_groups(self):
        store_src = text("static/store.js")
        self.assertRegex(store_src, r"\bcollapsedGroups\s*:\s*new\s+Set\s*\(\s*\)", "store.ui must initialize collapsedGroups: new Set()")

        html = text("page.html")
        m = re.search(r"<style>(.*?)</style>", html, re.S)
        self.assertIsNotNone(m)
        css = m.group(1)
        for cls in (".sess-group-harness", ".group-toggle", ".sess-project", ".sess-proj-sep"):
            self.assertIn(cls, css, f"page.html CSS missing {cls}")
        self.assertNotIn(".sess-group-project", css, "stale .sess-group-project CSS must be removed")
        self.assertRegex(css, r"tr\.sess-root\s+td:first-child\s*\{[^}]*padding-left:\s*20px")
        self.assertRegex(css, r"tr\.sess-child\s+td:nth-child\(2\)\s*\{[^}]*border-left:\s*2px\s+solid\s+var\(--border-prominent\)")
        self.assertNotRegex(css, r"tr\.sess-child\s+td:nth-child\(3\)")
        self.assertRegex(css, r"\.sess-proj-sep\s+(?:td\s*)?\{[^}]*border-top:\s*2px\s+solid\s+var\(--border-prominent\)")
        self.assertRegex(css, r"padding-top:\s*8px;\s*padding-bottom:\s*8px")

        mobile_m = re.search(r"@media\s*\(max-width:\s*768px\)\s*\{(.*)", css, re.S)
        self.assertIsNotNone(mobile_m, "page.html missing @media (max-width: 768px)")
        mobile_css = mobile_m.group(1)
        self.assertIn(".sess-group-harness", mobile_css)
        self.assertNotIn(".sess-group-project", mobile_css)
        self.assertRegex(mobile_css, r'#sec-sessions\s+tbody\s+td:nth-child\(3\)::before\s*\{[^}]*content:\s*"Slots:"')
        self.assertNotIn("#sec-sessions tbody td:nth-child(5)", mobile_css)


class TestCiGateRateLimitHeader(unittest.TestCase):
    """Issue #63: CI gate header renders GH API rate-limit badge when rate_limit is present."""

    def test_ci_js_renders_rate_limit_in_gate_header(self):
        src = text("static/ci.js")
        self.assertIn("rate_limit", src)
        self.assertIn("GH API", src)
        html = text("page.html")
        sec_m = re.search(r'<section id="sec-ci">.*?</section>', html, re.S)
        self.assertIsNotNone(sec_m, "page.html missing #sec-ci")
        self.assertIn("ci-gate-badge", sec_m.group(0))


if __name__ == "__main__":
    unittest.main()

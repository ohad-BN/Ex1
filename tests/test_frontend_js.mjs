import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { diagnosticTest } from "./js_diagnostics.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const JS_DIR = path.resolve(__dirname, "..", "app", "static", "js");

function readStaticJs(name) {
  return fs.readFileSync(path.join(JS_DIR, name), "utf8");
}

function makeFakeStorage(initial = {}) {
  const store = new Map(Object.entries(initial).map(([k, v]) => [String(k), String(v)]));
  return {
    get length() {
      return store.size;
    },
    key(idx) {
      return Array.from(store.keys())[idx] ?? null;
    },
    getItem(k) {
      return store.has(String(k)) ? store.get(String(k)) : null;
    },
    setItem(k, v) {
      store.set(String(k), String(v));
    },
    removeItem(k) {
      store.delete(String(k));
    },
    clear() {
      store.clear();
    },
    _dump() {
      return Object.fromEntries(store.entries());
    },
  };
}

function makeMinimalDom() {
  const byId = new Map();
  const listeners = new Map();

  function createEl(tagName) {
    const elListeners = new Map();
    const classSet = new Set();
    const children = [];
    const el = {
      tagName: String(tagName || "div").toUpperCase(),
      style: {},
      dataset: {},
      _id: "",
      _text: "",
      _innerHTML: "",
      width: 0,
      height: 0,
      naturalWidth: 0,
      naturalHeight: 0,
      parentNode: null,
      get id() {
        return el._id;
      },
      set id(val) {
        if (el._id && byId.get(el._id) === el) byId.delete(el._id);
        el._id = String(val || "");
        if (el._id) byId.set(el._id, el);
      },
      get textContent() {
        return el._text;
      },
      set textContent(v) {
        el._text = String(v ?? "");
      },
      get innerHTML() {
        return el._innerHTML;
      },
      set innerHTML(v) {
        el._innerHTML = String(v ?? "");
      },
      get children() {
        return children;
      },
      get firstElementChild() {
        return children[0] || null;
      },
      classList: {
        add: (c) => classSet.add(c),
        remove: (c) => classSet.delete(c),
        contains: (c) => classSet.has(c),
      },
      appendChild(child) {
        child.parentNode = el;
        children.push(child);
        if (child.id) byId.set(child.id, child);
        return child;
      },
      remove() {
        if (el.parentNode) {
          const arr = el.parentNode.children;
          const idx = arr.indexOf(el);
          if (idx >= 0) arr.splice(idx, 1);
          el.parentNode = null;
        }
      },
      addEventListener(type, fn) {
        if (!elListeners.has(type)) elListeners.set(type, []);
        elListeners.get(type).push(fn);
      },
      dispatchEvent(ev) {
        const list = elListeners.get(ev.type) || [];
        for (const fn of list) fn(ev);
      },
      querySelector(sel) {
        if (sel === "button" && el._innerHTML.includes("<button")) {
          if (!el._cachedBtn) el._cachedBtn = createEl("button");
          return el._cachedBtn;
        }
        const m = sel.match(/^input\[name="([^"]+)"\]$/);
        if (m) {
          return children.find((c) => c.name === m[1]) || null;
        }
        return null;
      },
      getContext() {
        return {
          fillStyle: "",
          strokeStyle: "",
          lineWidth: 1,
          lineCap: "",
          lineJoin: "",
          font: "",
          textAlign: "",
          textBaseline: "",
          calls: [],
          clearRect(...args) {
            this.calls.push(["clearRect", ...args]);
          },
          beginPath() {},
          roundRect() {},
          rect() {},
          arc() {},
          moveTo() {},
          lineTo() {},
          fill() {},
          stroke() {},
          strokeRect(...args) {
            this.calls.push(["strokeRect", ...args]);
          },
          fillText(...args) {
            this.calls.push(["fillText", ...args]);
          },
          drawImage(...args) {
            this.calls.push(["drawImage", ...args]);
          },
        };
      },
      toDataURL() {
        return "data:image/png;base64,FAKE";
      },
    };
    return el;
  }

  const head = createEl("head");
  const body = createEl("body");

  const document = {
    title: "ClassPulse",
    cookie: "",
    hidden: false,
    head,
    body,
    createElement: createEl,
    getElementById(id) {
      return byId.get(id) || null;
    },
    querySelector(sel) {
      if (sel.includes("icon")) return null;
      return null;
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    _trigger(type, ev) {
      for (const fn of listeners.get(type) || []) fn(ev);
    },
    _registerEl(id, el) {
      el.id = id;
      byId.set(id, el);
      return el;
    },
  };

  return { document, createEl };
}

diagnosticTest(
  "csrf.js injects X-CSRF-Token on mutating fetch calls and POST forms without overwriting explicit headers", async () => {
  const { document, createEl } = makeMinimalDom();
  document.cookie = "other=1; cp_csrf=tok%2Babc%3D123";

  const fetchCalls = [];
  const origFetch = async (url, init) => {
    fetchCalls.push({ url, init });
    return { ok: true, status: 200 };
  };

  const windowObj = {
    fetch: origFetch,
  };
  const ctx = vm.createContext({
    window: windowObj,
    document,
    Headers,
    decodeURIComponent,
  });
  vm.runInContext(readStaticJs("csrf.js"), ctx);

  await windowObj.fetch("/api/readonly", { method: "GET" });
  assert.equal(fetchCalls[0].init.headers, undefined);

  await windowObj.fetch("/api/courses", { method: "POST" });
  assert.equal(fetchCalls[1].init.headers.get("X-CSRF-Token"), "tok+abc=123");

  await windowObj.fetch("/api/courses", {
    method: "PATCH",
    headers: { "X-CSRF-Token": "custom-token" },
  });
  assert.equal(fetchCalls[2].init.headers.get("X-CSRF-Token"), "custom-token");

  const postForm = createEl("form");
  postForm.method = "post";
  document._trigger("submit", { target: postForm });
  const hidden = postForm.querySelector('input[name="csrf_token"]');
  assert.ok(hidden);
  assert.equal(hidden.type, "hidden");
  assert.equal(hidden.value, "tok+abc=123");

  const getForm = createEl("form");
  getForm.method = "GET";
  document._trigger("submit", { target: getForm });
  assert.equal(getForm.querySelector('input[name="csrf_token"]'), null);
});

diagnosticTest(
  "notifications.js enforces storage epoch wipe, tracks unread counts, filters self/duplicates, and escapes XSS", () => {
  const { document } = makeMinimalDom();
  const localStorage = makeFakeStorage({
    cp_unread_v2_student_1: JSON.stringify([{ key: "stale", kind: "qa" }]),
    cp_old_cache: "stale",
    user_theme_pref: "light",
  });
  const sessionStorage = makeFakeStorage({ temp_key: "stale" });

  const windowObj = {
    localStorage,
    sessionStorage,
  };
  const ctx = vm.createContext({
    window: windowObj,
    document,
    setTimeout,
    clearTimeout,
    Date,
    Set,
    Map,
  });
  vm.runInContext(readStaticJs("notifications.js"), ctx);

  // Epoch check cleared cp_* and sessionStorage while keeping non-cp_ keys
  assert.equal(localStorage.getItem("cp_storage_epoch"), "fresh_reset_2026_11_25_v1");
  assert.equal(localStorage.getItem("cp_old_cache"), null);
  assert.equal(localStorage.getItem("cp_unread_v2_student_1"), null);
  assert.equal(localStorage.getItem("user_theme_pref"), "light");
  assert.equal(sessionStorage.length, 0);

  let latestSummary = null;
  let openedPayload = null;
  const Notify = windowObj.ClassPulseNotify;
  Notify.init({
    role: "student",
    baseTitle: "ClassPulse Student",
    currentStudentId: 42,
    onUnreadChange: (s) => {
      latestSummary = s;
    },
    onOpenMessage: (p) => {
      openedPayload = p;
    },
  });

  // Self-message ignored
  Notify.pushChatNotification({
    message_id: 100,
    sender_student_id: 42,
    course_id: 7,
    body: "My own message",
  });
  assert.equal(latestSummary.total, 0);

  // Incoming message increments unread, escapes XSS, caps toast stack at 4
  Notify.pushChatNotification({
    message_id: 101,
    sender_student_id: 99,
    course_id: 7,
    course_name: "OOP",
    session_code: "OOP-L1",
    author_name: "<Bad>",
    category: "Question",
    body: '<script>alert("xss")</script>',
  });
  // Duplicate message_id ignored
  Notify.pushChatNotification({
    message_id: 101,
    sender_student_id: 99,
    course_id: 7,
    body: "Duplicate",
  });
  assert.equal(latestSummary.total, 1);
  assert.equal(latestSummary.hasUnreadCourse(7), true);
  assert.equal(latestSummary.hasUnreadSection(7, "lectures"), true);
  assert.equal(latestSummary.hasUnreadItem("qa", 101), true);
  assert.ok(document.title.includes("(1)"));

  const stack = document.getElementById("cp-toast-stack");
  assert.equal(stack.children.length, 1);
  const renderedHtml = stack.children[0].innerHTML;
  assert.ok(!renderedHtml.includes("<script>"));
  assert.ok(renderedHtml.includes("&lt;script&gt;"));

  // Flood 5 more distinct notifications -> stack capped at 4
  for (let i = 200; i < 205; i += 1) {
    Notify.pushChatNotification({
      message_id: i,
      sender_student_id: 99,
      course_id: 7,
      session_code: "OOP-L1",
      body: `Msg ${i}`,
    });
  }
  assert.equal(stack.children.length, 4);
  assert.equal(latestSummary.total, 6);

  // Clicking first toast clears unread for that session and invokes onOpenMessage
  stack.children[0].dispatchEvent({ type: "click" });
  assert.ok(openedPayload !== null);
  assert.equal(latestSummary.total, 0);

  Notify.markUnread({ key: "asgn:7:5", kind: "assignment", courseId: 7, itemId: 5 });
  assert.equal(Notify.getUnreadSummary().hasUnreadSection(7, "assignments"), true);
  Notify.clearUnread();
  assert.equal(Notify.getUnreadSummary().total, 0);
});

diagnosticTest(
  "image_compress.js downscales oversized images, preserves small images, rejects null/HEIC, and handles timeout aborts", async () => {
  const { document, createEl } = makeMinimalDom();
  let lastCanvasSize = null;
  document.createElement = (tag) => {
    const el = createEl(tag);
    if (tag === "canvas") {
      el.getContext = () => ({
        drawImage() {
          lastCanvasSize = { width: el.width, height: el.height };
        },
      });
      el.toBlob = (cb, mime, quality) => {
        cb({ size: 1234, type: mime, quality });
      };
    }
    return el;
  };

  const windowObj = {};
  const ctx = vm.createContext({
    window: windowObj,
    document,
    AbortController,
    setTimeout,
    clearTimeout,
    Image: function () {},
    URL: { createObjectURL: () => "blob:1", revokeObjectURL: () => {} },
    createImageBitmap: async (file) => ({
      width: file._w,
      height: file._h,
      close() {},
    }),
    fetch: (_url, opts) =>
      new Promise((_resolve, reject) => {
        opts.signal.addEventListener("abort", () => {
          const err = new Error("Aborted");
          err.name = "AbortError";
          reject(err);
        });
      }),
  });
  vm.runInContext(readStaticJs("image_compress.js"), ctx);

  const { compressToJpeg, fetchWithTimeout } = windowObj.ClassPulseImage;

  await assert.rejects(() => compressToJpeg(null), /No image file selected/);

  const big = await compressToJpeg(
    { name: "room.jpg", type: "image/jpeg", _w: 2560, _h: 1920 },
    { maxEdge: 1280, quality: 0.75 }
  );
  assert.deepEqual(lastCanvasSize, { width: 1280, height: 960 });
  assert.equal(big.type, "image/jpeg");

  const small = await compressToJpeg(
    { name: "small.png", type: "image/png", _w: 800, _h: 600 },
    { maxEdge: 1280 }
  );
  assert.deepEqual(lastCanvasSize, { width: 800, height: 600 });
  assert.equal(small.type, "image/jpeg");

  // HEIC decode failure gives friendly error instead of silently uploading raw HEIC
  const ctxFailDecode = vm.createContext({
    window: {},
    document,
    setTimeout,
    clearTimeout,
    URL: { createObjectURL: () => "blob:bad", revokeObjectURL: () => {} },
    Image: function () {
      queueMicrotask(() => {
        if (this.onerror) this.onerror();
      });
    },
    createImageBitmap: async () => {
      throw new Error("Unsupported format");
    },
  });
  vm.runInContext(readStaticJs("image_compress.js"), ctxFailDecode);
  await assert.rejects(
    () => ctxFailDecode.window.ClassPulseImage.compressToJpeg({ name: "photo.heic", type: "image/heic" }),
    /HEIC\/HEIF photo could not be decoded/
  );
  // Standard JPEG gracefully returns original file if decode fails
  const fallbackJpg = await ctxFailDecode.window.ClassPulseImage.compressToJpeg({
    name: "raw.jpg",
    type: "image/jpeg",
  });
  assert.equal(fallbackJpg.name, "raw.jpg");

  await assert.rejects(
    () => fetchWithTimeout("/api/upload", { method: "POST" }, 15),
    /Photo upload timed out/
  );
});

diagnosticTest(
  "headcount_canvas.js hit-tests existing boxes, adds clamped manual boxes, re-indexes labels, and PATCHes server", () => {
  const { document, createEl } = makeMinimalDom();
  const canvas = document._registerEl("headcount-canvas", createEl("canvas"));
  canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 100 });
  const kpiVerified = document._registerEl("kpi-visual-count", createEl("span"));
  const snapVerified = document._registerEl("snap-verified-count", createEl("span"));

  const fetchCalls = [];
  const windowObj = {};
  const ctx = vm.createContext({
    window: windowObj,
    document,
    fetch: async (url, init) => {
      fetchCalls.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ auto_count: 1, verified_count: 1 }) };
    },
  });
  vm.runInContext(readStaticJs("headcount_canvas.js"), ctx);

  const editor = new windowObj.HeadcountCanvasEditor("headcount-canvas", 55);
  editor.loadedImg = { naturalWidth: 200, naturalHeight: 100 };
  editor.currentBoxes = [
    { label_id: 1, x1: 10, y1: 10, x2: 50, y2: 50, confidence: 0.9, source: "auto" },
    { label_id: 2, x1: 100, y1: 10, x2: 140, y2: 50, confidence: 0.9, source: "auto" },
  ];
  editor.drawCanvas();

  // Clicking inside box 1 removes it -> box 2 re-indexes to label_id 1
  editor.handleClick({ clientX: 25, clientY: 25 });
  assert.equal(editor.currentBoxes.length, 1);
  assert.equal(editor.currentBoxes[0].label_id, 1);
  assert.equal(editor.currentBoxes[0].x1, 100);
  assert.equal(kpiVerified.textContent, "1");
  assert.equal(snapVerified.textContent, "1");

  // Clicking near edge (5, 5) adds manual_added box clamped at x1=0, y1=0
  editor.handleClick({ clientX: 5, clientY: 5 });
  assert.equal(editor.currentBoxes.length, 2);
  assert.equal(editor.currentBoxes[1].source, "manual_added");
  assert.equal(editor.currentBoxes[1].x1, 0);
  assert.equal(editor.currentBoxes[1].y1, 0);
  assert.equal(editor.currentBoxes[1].label_id, 2);
  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[1].url, "/api/sessions/55/headcount");
});

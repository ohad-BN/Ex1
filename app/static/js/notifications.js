(function () {
  let _cfg = {
    role: "student",
    baseTitle: document.title || "ClassPulse",
    currentLecturerId: null,
    currentStudentId: null,
    isChatVisibleFn: () => false,
    onOpenMessage: null,
  };
  let _unreadCount = 0;
  const _seenNotifyIds = new Set();
  const _unreadMap = new Map();

  function _storageKey() {
    const uid =
      _cfg.role === "lecturer"
        ? _cfg.currentLecturerId || "anon"
        : _cfg.currentStudentId || "anon";
    return `cp_unread_v2_${_cfg.role}_${uid}`;
  }

  function _loadUnreadStore() {
    _unreadMap.clear();
    try {
      const raw = window.localStorage.getItem(_storageKey());
      if (!raw) return;
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        arr.forEach((item) => {
          if (item && item.key) _unreadMap.set(String(item.key), item);
        });
      }
    } catch (_err) {}
  }

  function _saveUnreadStore() {
    try {
      const items = Array.from(_unreadMap.values()).slice(-80);
      window.localStorage.setItem(_storageKey(), JSON.stringify(items));
    } catch (_err) {}
    _notifyUnreadListeners();
  }

  function _notifyUnreadListeners() {
    setUnreadCount(_unreadMap.size);
    if (typeof _cfg.onUnreadChange === "function") {
      try {
        _cfg.onUnreadChange(getUnreadSummary());
      } catch (_err) {}
    }
  }

  function markUnread(entry) {
    if (!entry || !entry.key) return;
    const key = String(entry.key);
    _unreadMap.set(key, {
      key,
      kind: String(entry.kind || "qa"),
      courseId: entry.courseId != null ? Number(entry.courseId) : null,
      sessionCode: entry.sessionCode ? String(entry.sessionCode) : "",
      itemId: entry.itemId != null ? String(entry.itemId) : "",
    });
    _saveUnreadStore();
  }

  function clearUnreadWhere(predFn) {
    if (typeof predFn !== "function") return;
    let changed = false;
    for (const [key, item] of _unreadMap.entries()) {
      if (predFn(item)) {
        _unreadMap.delete(key);
        changed = true;
      }
    }
    if (changed) {
      _saveUnreadStore();
    }
  }

  function getUnreadSummary() {
    const courses = new Set();
    const sections = new Set();
    const items = new Set();
    const byKind = {};
    for (const item of _unreadMap.values()) {
      const k = item.kind;
      byKind[k] = (byKind[k] || 0) + 1;
      if (item.courseId != null) {
        courses.add(item.courseId);
        if (k === "assignment") sections.add(`${item.courseId}:assignments`);
        else if (k === "summary") sections.add(`${item.courseId}:summaries`);
        else if (k === "qa" || k === "poll") sections.add(`${item.courseId}:lectures`);
        if (k === "qa") sections.add(`${item.courseId}:qa`);
      }
      if (item.itemId) items.add(`${k}:${item.itemId}`);
      if (item.sessionCode) items.add(`${k}:sess:${item.sessionCode}`);
    }
    return {
      total: _unreadMap.size,
      byKind,
      hasUnreadCourse: (cid) => courses.has(Number(cid)),
      hasUnreadSection: (cid, sec) => sections.has(`${Number(cid)}:${sec}`),
      hasUnreadItem: (kind, idOrCode) =>
        items.has(`${kind}:${idOrCode}`) || items.has(`${kind}:sess:${idOrCode}`),
      hasUnreadAny: (kind) => (kind ? (byKind[kind] || 0) > 0 : _unreadMap.size > 0),
    };
  }

  function _ensureStylesAndContainers() {
    if (!document.getElementById("cp-notify-styles")) {
      const style = document.createElement("style");
      style.id = "cp-notify-styles";
      style.textContent = `
        #cp-toast-stack {
          position: fixed;
          bottom: 18px;
          right: 18px;
          z-index: 9999;
          display: flex;
          flex-direction: column;
          gap: 10px;
          max-width: 340px;
          width: calc(100vw - 36px);
          pointer-events: none;
        }
        .cp-toast {
          pointer-events: auto;
          background: var(--cp-surface-solid, #FFFFFF);
          backdrop-filter: blur(18px);
          -webkit-backdrop-filter: blur(18px);
          border: 1px solid var(--cp-border, rgba(24, 26, 31, 0.12));
          border-left: 4px solid var(--cp-coral, #E87148);
          color: var(--cp-ink, #181A1F);
          border-radius: var(--cp-radius-well, 14px);
          padding: 12px 14px;
          box-shadow: var(--cp-card-shadow, 0 14px 32px rgba(31, 34, 51, 0.14));
          cursor: pointer;
          transition: transform 0.14s ease, border-color 0.14s ease;
          font-family: var(--cp-font-sans, system-ui, -apple-system, sans-serif);
        }
        .cp-toast:hover {
          transform: translateY(-2px);
          border-color: var(--cp-indigo, #5B5FEF);
        }
        .cp-toast-hdr {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
          margin-bottom: 4px;
        }
        .cp-toast-title {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          font-size: 11px;
          font-weight: 800;
          color: var(--cp-coral-cta, #C84E27);
        }
        .cp-toast-close {
          background: transparent;
          border: none;
          color: var(--cp-ink-muted, #555D6E);
          cursor: pointer;
          font-size: 16px;
          line-height: 1;
          padding: 0 2px;
          width: auto;
        }
        .cp-toast-body {
          font-size: 13px;
          color: var(--cp-ink, #181A1F);
          margin-bottom: 4px;
          line-height: 1.35;
        }
        .cp-toast-cat {
          font-size: 11px;
          color: var(--cp-ink-muted, #555D6E);
        }
        .cp-toast-cta {
          font-size: 11px;
          color: var(--cp-green-ink, #166534);
          font-weight: 700;
        }
        .cp-red-dot {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 18px;
          height: 18px;
          padding: 0 5px;
          border-radius: 999px;
          background: var(--cp-red, #DC2626);
          color: #ffffff;
          font-size: 10px;
          font-weight: 800;
          margin-left: 6px;
          vertical-align: middle;
        }
        .cp-msg-flash {
          outline: 2px solid var(--cp-coral, #E87148) !important;
          box-shadow: 0 0 0 4px var(--cp-coral-soft, rgba(232, 113, 72, 0.22)) !important;
          background: var(--cp-coral-soft, rgba(232, 113, 72, 0.14)) !important;
          transition: box-shadow 0.4s ease, background 0.4s ease;
        }
      `;
      document.head.appendChild(style);
    }

    if (!document.getElementById("cp-toast-stack")) {
      const stack = document.createElement("div");
      stack.id = "cp-toast-stack";
      document.body.appendChild(stack);
    }
  }

  function _drawFavicon(count) {
    let link = document.getElementById("cp-favicon");
    if (!link) {
      link = document.querySelector("link[rel*='icon']") || document.createElement("link");
      link.id = "cp-favicon";
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.type = "image/png";

    const canvas = document.createElement("canvas");
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // White rounded badge with hairline border and coral pulse emblem
    ctx.fillStyle = "#FFFFFF";
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(1, 1, 30, 30, 8) : ctx.rect(1, 1, 30, 30);
    ctx.fill();
    ctx.strokeStyle = "#E5E0DC";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.strokeStyle = "#E87148";
    ctx.lineWidth = 2.6;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(5, 18);
    ctx.lineTo(11, 18);
    ctx.lineTo(14, 9);
    ctx.lineTo(18, 24);
    ctx.lineTo(21, 16);
    ctx.lineTo(27, 16);
    ctx.stroke();

    if (count > 0) {
      // High-contrast red notification badge on top-right corner
      ctx.fillStyle = "#DC2626";
      ctx.beginPath();
      ctx.arc(23, 9, 9, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = "#FFFFFF";
      ctx.lineWidth = 1.6;
      ctx.stroke();

      ctx.fillStyle = "#FFFFFF";
      ctx.font = "bold 11px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const label = count > 9 ? "9+" : String(count);
      ctx.fillText(label, 23, 9.5);
    }

    link.href = canvas.toDataURL("image/png");
  }

  function setUnreadCount(count) {
    _unreadCount = Math.max(0, Number(count) || 0);
    _drawFavicon(_unreadCount);
    document.title = _unreadCount > 0 ? `(${_unreadCount}) 🔴 ${_cfg.baseTitle}` : _cfg.baseTitle;

    const tabBadge = document.getElementById("tab-chat-badge");
    if (tabBadge) {
      if (_unreadCount > 0) {
        tabBadge.textContent = _unreadCount > 99 ? "99+" : String(_unreadCount);
        tabBadge.style.display = "inline-flex";
      } else {
        tabBadge.style.display = "none";
      }
    }
  }

  function clearUnread() {
    _unreadMap.clear();
    _saveUnreadStore();
    setUnreadCount(0);
  }

  function _escape(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function _isSelfMessage(payload) {
    if (!payload) return true;
    if (_cfg.role === "lecturer" && _cfg.currentLecturerId && payload.sender_lecturer_id === _cfg.currentLecturerId) {
      return true;
    }
    if (_cfg.role === "student" && _cfg.currentStudentId && payload.sender_student_id === _cfg.currentStudentId) {
      return true;
    }
    return false;
  }

  function highlightMessage(msgId) {
    if (!msgId) return false;
    const el = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!el) return false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("cp-msg-flash");
    setTimeout(() => el.classList.remove("cp-msg-flash"), 3200);
    return true;
  }

  function pushChatNotification(payload) {
    if (!payload) return;
    const msgId = payload.message_id || payload.id;
    if (msgId && _seenNotifyIds.has(msgId)) return;
    if (msgId) _seenNotifyIds.add(msgId);
    if (_isSelfMessage(payload)) return;

    _ensureStylesAndContainers();

    const viewingSameChatNow =
      !document.hidden &&
      typeof _cfg.isChatVisibleFn === "function" &&
      Boolean(_cfg.isChatVisibleFn(payload));

    if (!viewingSameChatNow) {
      markUnread({
        key: `qa:${payload.course_id || ""}:${msgId || Date.now()}`,
        kind: "qa",
        courseId: payload.course_id,
        sessionCode: payload.session_code,
        itemId: msgId,
      });
    }

    const stack = document.getElementById("cp-toast-stack");
    const toast = document.createElement("div");
    toast.className = "cp-toast";
    const titleParts = [];
    if (payload.course_name) titleParts.push(payload.course_name);
    if (payload.lecture_title) {
      titleParts.push(payload.lecture_title);
    } else if (payload.lecture_number) {
      titleParts.push(`Lecture ${payload.lecture_number}`);
    }
    const headerText = titleParts.join(" \u2014 ") || "Classroom Q&A";
    const rawBody = String(payload.body != null ? payload.body : (payload.text || ""));
    const snippet = rawBody.length > 90
      ? rawBody.slice(0, 87) + "..."
      : rawBody;

    toast.innerHTML = `
      <div class="cp-toast-hdr">
        <span class="cp-toast-title">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          ${_escape(headerText)}
        </span>
        <button type="button" class="cp-toast-close" aria-label="Dismiss">&times;</button>
      </div>
      <div class="cp-toast-body">
        <strong>${_escape(payload.author_name || "User")}</strong>
        <span class="cp-toast-cat">(${_escape(payload.category || "Message")})</span>:
        ${_escape(snippet)}
      </div>
      <div class="cp-toast-cta">Click to open lecture &amp; view message &rarr;</div>
    `;

    const dismissBtn = toast.querySelector("button");
    if (dismissBtn) {
      dismissBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        toast.remove();
      });
    }

    toast.addEventListener("click", () => {
      toast.remove();
      clearUnread();
      if (typeof _cfg.onOpenMessage === "function") {
        _cfg.onOpenMessage(payload);
      }
    });

    stack.appendChild(toast);
    while (stack.children.length > 4) {
      stack.firstElementChild.remove();
    }
    setTimeout(() => {
      if (toast.parentNode) toast.remove();
    }, 9000);
  }

  function init(options) {
    _cfg = { ..._cfg, ...(options || {}) };
    _ensureStylesAndContainers();
    _loadUnreadStore();
    _drawFavicon(_unreadMap.size);
    _notifyUnreadListeners();

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && typeof _cfg.isChatVisibleFn === "function" && _cfg.isChatVisibleFn(null)) {
        setUnreadCount(0);
      }
    });
  }

  window.ClassPulseNotify = {
    init,
    setUnreadCount,
    clearUnread,
    markUnread,
    clearUnreadWhere,
    getUnreadSummary,
    pushChatNotification,
    highlightMessage,
  };
})();

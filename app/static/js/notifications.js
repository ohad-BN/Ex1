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
          background: #0f172a;
          border: 1.5px solid #38bdf8;
          border-left: 5px solid #ef4444;
          color: #f8fafc;
          border-radius: 12px;
          padding: 12px 14px;
          box-shadow: 0 14px 32px rgba(0, 0, 0, 0.55);
          cursor: pointer;
          transition: transform 0.14s ease, border-color 0.14s ease;
          font-family: system-ui, -apple-system, sans-serif;
        }
        .cp-toast:hover {
          transform: translateY(-2px);
          border-color: #22c55e;
        }
        .cp-red-dot {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 18px;
          height: 18px;
          padding: 0 5px;
          border-radius: 999px;
          background: #ef4444;
          color: #ffffff;
          font-size: 10px;
          font-weight: 800;
          margin-left: 6px;
          vertical-align: middle;
        }
        .cp-msg-flash {
          outline: 2px solid #38bdf8 !important;
          box-shadow: 0 0 0 4px rgba(56, 189, 248, 0.28) !important;
          background: rgba(56, 189, 248, 0.14) !important;
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
      link.type = "image/png";
      document.head.appendChild(link);
    }

    const canvas = document.createElement("canvas");
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Rounded dark background with cyan pulse emblem
    ctx.fillStyle = "#0b1120";
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(0, 0, 32, 32, 7) : ctx.rect(0, 0, 32, 32);
    ctx.fill();

    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2.6;
    ctx.beginPath();
    ctx.moveTo(5, 18);
    ctx.lineTo(11, 18);
    ctx.lineTo(14, 9);
    ctx.lineTo(18, 24);
    ctx.lineTo(21, 16);
    ctx.lineTo(27, 16);
    ctx.stroke();

    if (count > 0) {
      // Bright red notification circle on top-right corner of the Chrome tab icon
      ctx.fillStyle = "#ef4444";
      ctx.beginPath();
      ctx.arc(23, 9, 9, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.6;
      ctx.stroke();

      ctx.fillStyle = "#ffffff";
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
      setUnreadCount(_unreadCount + 1);
    }

    const stack = document.getElementById("cp-toast-stack");
    const toast = document.createElement("div");
    toast.className = "cp-toast";
    const courseLabel = payload.course_code ? `${payload.course_code} • ` : "";
    const sessionLabel = payload.session_code || "";
    const lectureLabel = payload.lecture_title ? ` — ${payload.lecture_title}` : "";
    const snippet = (payload.body || "").length > 90
      ? payload.body.slice(0, 87) + "..."
      : (payload.body || "");

    toast.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:4px;">
        <span style="font-size:11px; font-weight:800; color:#38bdf8;">
          &#128172; ${_escape(courseLabel + sessionLabel)}${_escape(lectureLabel)}
        </span>
        <button type="button" aria-label="Dismiss" style="background:transparent; border:none; color:#94a3b8; cursor:pointer; font-size:14px; padding:0; width:auto;">&times;</button>
      </div>
      <div style="font-size:13px; margin-bottom:4px;">
        <strong>${_escape(payload.author_name || "User")}</strong>
        <span style="font-size:11px; color:#94a3b8;">(${_escape(payload.category || "Message")})</span>:
        ${_escape(snippet)}
      </div>
      <div style="font-size:11px; color:#4ade80; font-weight:700;">Click to open lecture &amp; view message &rarr;</div>
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
    _drawFavicon(0);

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && typeof _cfg.isChatVisibleFn === "function" && _cfg.isChatVisibleFn(null)) {
        clearUnread();
      }
    });
  }

  window.ClassPulseNotify = {
    init,
    setUnreadCount,
    clearUnread,
    pushChatNotification,
    highlightMessage,
  };
})();

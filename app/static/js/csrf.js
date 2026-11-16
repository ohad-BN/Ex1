(function () {
  function getCsrfCookie() {
    const match = document.cookie.match(/(?:^|;\s*)cp_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      const opts = init ? { ...init } : {};
      const method = (opts.method || "GET").toUpperCase();
      if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
        const token = getCsrfCookie();
        if (token) {
          const headers = new Headers(opts.headers || {});
          if (!headers.has("X-CSRF-Token")) {
            headers.set("X-CSRF-Token", token);
          }
          opts.headers = headers;
        }
      }
      return origFetch.call(this, input, opts);
    };
  }

  document.addEventListener("submit", function (ev) {
    const form = ev.target;
    if (!form || (form.method || "").toUpperCase() !== "POST") return;
    const token = getCsrfCookie();
    if (!token) return;
    let hidden = form.querySelector('input[name="csrf_token"]');
    if (!hidden) {
      hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "csrf_token";
      form.appendChild(hidden);
    }
    hidden.value = token;
  });
})();

// tgdl web UI behaviour. No inline scripts (CSP: script-src 'self').
(function () {
  "use strict";

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  // Send the CSRF token with every htmx request.
  document.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["X-CSRF-Token"] = csrfToken();
  });
  // Session expired during an htmx request.
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr && e.detail.xhr.status === 401) window.location.href = "/login";
  });

  // Confirmation dialogs: <form data-confirm="...">
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // Rows-per-page selectors submit their (non-htmx) form on change.
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("select[data-autosubmit]") && e.target.form) {
      e.target.form.submit();
    }
  });

  // "Use default" checkboxes disable the custom inputs: data-toggles="#id"
  function syncToggles(root) {
    (root || document).querySelectorAll("input[data-toggles]").forEach(function (cb) {
      var target = document.querySelector(cb.getAttribute("data-toggles"));
      if (!target) return;
      target.querySelectorAll("input, select, textarea").forEach(function (el) { el.disabled = cb.checked; });
      target.classList.toggle("muted", cb.checked);
    });
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("input[data-toggles]")) syncToggles();
  });

  // Countdown for FloodWait banners: <span data-until="epoch seconds">
  function fmt(sec) {
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    var mm = (h ? String(m).padStart(2, "0") : String(m)), ss = String(s).padStart(2, "0");
    return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  }
  var expiredRefresh = false;
  setInterval(function () {
    var now = Date.now() / 1000;
    document.querySelectorAll(".countdown[data-until]").forEach(function (el) {
      var left = parseFloat(el.getAttribute("data-until")) - now;
      el.textContent = fmt(left);
      if (left <= 0 && !expiredRefresh) { expiredRefresh = true; setTimeout(function () { refreshAll(); expiredRefresh = false; }, 1500); }
    });
  }, 1000);

  // Live regions: <div data-live-url="/partials/x" data-live-events="a,b" [data-live-select="#id"]>
  var pending = {};
  function refresh(el) {
    if (!window.htmx || !el.isConnected) return;
    var key = el.id || el.getAttribute("data-live-url");
    if (pending[key]) return;
    pending[key] = setTimeout(function () {
      delete pending[key];
      var sel = el.getAttribute("data-live-select");
      var opts = sel ? { target: el, swap: "outerHTML", select: sel } : { target: el, swap: "innerHTML" };
      window.htmx.ajax("GET", el.getAttribute("data-live-url"), opts);
    }, 1000);
  }
  function refreshAll() {
    document.querySelectorAll("[data-live-url]").forEach(refresh);
  }
  var source = null;
  function connect() {
    var regions = document.querySelectorAll("[data-live-url]");
    if (!regions.length || !window.EventSource) return;
    var wanted = {};
    regions.forEach(function (el) {
      (el.getAttribute("data-live-events") || "").split(",").forEach(function (t) { if (t) wanted[t] = true; });
    });
    source = new EventSource("/events");
    Object.keys(wanted).forEach(function (type) {
      source.addEventListener(type, function () {
        document.querySelectorAll("[data-live-url]").forEach(function (el) {
          var evs = "," + (el.getAttribute("data-live-events") || "") + ",";
          if (evs.indexOf("," + type + ",") !== -1) refresh(el);
        });
      });
    });
    source.addEventListener("logout", function () { window.location.href = "/login"; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    syncToggles();
    connect();
  });
  window.addEventListener("beforeunload", function () { if (source) source.close(); });
})();

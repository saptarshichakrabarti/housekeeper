// drive_housekeeper dashboard theme switcher.
// External file (CSP is script-src 'self'; inline scripts are blocked).
// Persists the choice locally; no network, no telemetry.
(function () {
  "use strict";

  var THEMES = [
    ["sober", "Sober"],
    ["hybrid", "Hybrid"],
    ["brutal", "Brutal"],
  ];
  var STORAGE_KEY = "hk-dashboard-theme";
  var DEFAULT = "sober";

  function currentTheme() {
    var saved;
    try {
      saved = window.localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      saved = null; // storage may be unavailable; fall through to default
    }
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i][0] === saved) return saved;
    }
    return DEFAULT;
  }

  function applyTheme(name) {
    document.documentElement.setAttribute("data-theme", name);
    try {
      window.localStorage.setItem(STORAGE_KEY, name);
    } catch (e) {
      /* non-fatal: theme still applies for this page load */
    }
  }

  function buildSwitcher(active) {
    var wrap = document.createElement("div");
    wrap.className = "hk-theme-switch";

    var label = document.createElement("label");
    label.setAttribute("for", "hk-theme-select");
    label.textContent = "Theme";
    wrap.appendChild(label);

    var select = document.createElement("select");
    select.id = "hk-theme-select";
    for (var i = 0; i < THEMES.length; i++) {
      var opt = document.createElement("option");
      opt.value = THEMES[i][0];
      opt.textContent = THEMES[i][1];
      if (THEMES[i][0] === active) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener("change", function () {
      applyTheme(select.value);
    });
    wrap.appendChild(select);
    return wrap;
  }

  function init() {
    var active = currentTheme();
    applyTheme(active); // reassert in case attribute was absent

    var header = document.querySelector("header");
    if (header && !document.getElementById("hk-theme-select")) {
      header.appendChild(buildSwitcher(active));
    }
  }

  // Apply the saved theme as early as this deferred script runs, then wire UI.
  applyTheme(currentTheme());
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

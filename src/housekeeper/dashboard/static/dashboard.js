(function () {
  "use strict";

  var selectedRow = null;
  var jobsWereRunning = null;
  var decisions = {
    "1": "DEFER", d: "DEFER",
    "2": "MARK_KEEP", m: "MARK_KEEP",
    "3": "MARK_PROTECTED", p: "MARK_PROTECTED",
    "4": "NEEDS_MORE_ANALYSIS", n: "NEEDS_MORE_ANALYSIS",
    "5": "APPROVE_FOR_REVIEW", a: "APPROVE_FOR_REVIEW",
    "6": "REJECT_RECOMMENDATION", r: "REJECT_RECOMMENDATION"
  };

  function announce(message) {
    var region = document.getElementById("review-announcer");
    if (region) region.textContent = message;
  }

  function selectRow(row) {
    if (!row) return;
    if (selectedRow) {
      selectedRow.classList.remove("is-selected");
      selectedRow.removeAttribute("aria-selected");
    }
    selectedRow = row;
    row.classList.add("is-selected");
    row.setAttribute("aria-selected", "true");
    var button = row.querySelector(".review-detail-button");
    if (button) button.click();
  }

  function moveSelection(direction) {
    var rows = Array.prototype.slice.call(document.querySelectorAll(".review-row"));
    if (!rows.length) return;
    var index = selectedRow ? rows.indexOf(selectedRow) : (direction > 0 ? -1 : rows.length);
    var target = rows[Math.max(0, Math.min(rows.length - 1, index + direction))];
    target.focus();
  }

  function recordDecision(decision) {
    var form = document.querySelector("[data-review-decision-form]");
    if (!form) {
      announce("Select a review row before recording a decision.");
      return;
    }
    var select = form.querySelector("select[name=decision]");
    var session = form.querySelector("[name=session_id]");
    select.value = decision;
    if (!session || !session.value || !session.checkValidity()) {
      announce("Choose a review session, then use the shortcut again.");
      if (session) session.focus();
      return;
    }
    announce("Recording " + decision.toLowerCase().replaceAll("_", " ") + ".");
    form.requestSubmit();
  }

  function showToast(count) {
    var toast = document.createElement("div");
    toast.className = "toast toast--complete";
    toast.setAttribute("role", "status");
    toast.textContent = "Scan complete — " + Number(count || 0).toLocaleString() + " files";
    document.body.appendChild(toast);
    window.setTimeout(function () { toast.classList.add("is-visible"); }, 10);
    window.setTimeout(function () { toast.remove(); }, 5000);
  }

  function observeJobs(root) {
    var status = (root || document).querySelector && (root || document).querySelector(".jobs-status");
    if (!status && root && root.matches && root.matches(".jobs-status")) status = root;
    if (!status) return;
    var running = status.dataset.running === "true";
    if (jobsWereRunning === true && !running) {
      status.classList.add("jobs-status--complete");
      showToast(status.dataset.completedCount);
    }
    jobsWereRunning = running;
  }

  document.addEventListener("focusin", function (event) {
    var row = event.target.closest && event.target.closest(".review-row");
    if (row && row !== selectedRow) selectRow(row);
  });

  document.addEventListener("keydown", function (event) {
    var target = event.target;
    if (target.matches("input, select, textarea, [contenteditable=true]")) return;
    if (event.key === "j" || event.key === "k") {
      event.preventDefault();
      moveSelection(event.key === "j" ? 1 : -1);
      return;
    }
    if (decisions[event.key]) {
      event.preventDefault();
      recordDecision(decisions[event.key]);
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    navigator.clipboard.writeText(button.dataset.copy).then(function () {
      var old = button.textContent;
      button.textContent = "Copied";
      window.setTimeout(function () { button.textContent = old; }, 1500);
    });
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-auto-submit]")) event.target.form.requestSubmit();
  });

  document.addEventListener("htmx:afterSwap", function (event) { observeJobs(event.detail.target); });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { observeJobs(document); });
  } else {
    observeJobs(document);
  }
})();

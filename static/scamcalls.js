(function () {
  "use strict";

  const qs = (sel, ctx = document) => ctx.querySelector(sel);
  const qsa = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  function showToast(message, durationMs = 3200) {
    const el = qs("#toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("toast--show");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      el.classList.remove("toast--show");
    }, durationMs);
  }

  // Format seconds to mm:ss
  function formatMMSS(totalSeconds) {
    const s = Math.max(0, Math.floor(totalSeconds || 0));
    const m = Math.floor(s / 60);
    const r = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(r).padStart(2, "0");
    return `${mm}:${ss}`;
  }

  // Build a compact countdown visual (inline SVG ring + text)
  function buildCountdownBadge(remainSec, totalSec) {
    const size = 34;
    const stroke = 4;
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    let pct = 0;
    if (totalSec && totalSec > 0) {
      const elapsed = Math.max(0, totalSec - Math.max(0, remainSec));
      pct = Math.max(0, Math.min(1, elapsed / totalSec));
    }
    const dash = `${(c * pct).toFixed(2)} ${c.toFixed(2)}`;

    const svg =
      `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true" style="display:block">
        <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="rgba(255,255,255,0.15)" stroke-width="${stroke}" />
        <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--accent, #25c2a0)" stroke-width="${stroke}"
                stroke-linecap="round" stroke-dasharray="${dash}" transform="rotate(-90 ${size / 2} ${size / 2})" />
      </svg>`;

    const label = `<div style="display:flex;align-items:center;gap:.5rem">
        <div style="width:${size}px;height:${size}px">${svg}</div>
        <div>
          <div style="line-height:1; font-weight:600; color:var(--text,#f3f5f7)">Next attempt</div>
          <div style="line-height:1.2; color:var(--muted,#aab2bd); font-variant-numeric: tabular-nums;">in ${formatMMSS(remainSec)}</div>
        </div>
      </div>`;

    return label;
  }

  // Greeting modal logic
  const greetingModal = {
    el: null,
    input: null,
    wordCountEl: null,
    saveBtn: null,
    open() {
      this.el.setAttribute("aria-hidden", "false");
      this.input.focus();
      this.updateWordCount();
    },
    close() {
      this.el.setAttribute("aria-hidden", "true");
      this.input.value = "";
      this.updateWordCount();
    },
    getWordCount() {
      const text = (this.input.value || "").trim();
      if (!text) return 0;
      return text.split(/\s+/).filter(Boolean).length;
    },
    updateWordCount() {
      const n = this.getWordCount();
      this.wordCountEl.textContent = `${n} words`;
      this.saveBtn.disabled = n < 5 || n > 15;
    },
  };

  function initGreetingModal() {
    const el = qs("#greetingModal");
    if (!el) return;
    greetingModal.el = el;
    greetingModal.input = qs("#greetingInput", el);
    greetingModal.wordCountEl = qs("#greetingWordCount", el);
    greetingModal.saveBtn = qs("#greetingSaveBtn", el);

    qs("#btnAddGreeting")?.addEventListener("click", () => greetingModal.open());
    qs("#greetingCloseBtn")?.addEventListener("click", () => greetingModal.close());
    qs("#greetingCancelBtn")?.addEventListener("click", () => greetingModal.close());
    greetingModal.input?.addEventListener("input", () => greetingModal.updateWordCount());

    greetingModal.saveBtn?.addEventListener("click", async () => {
      const phrase = (greetingModal.input.value || "").trim();
      const words = greetingModal.getWordCount();
      if (words < 5 || words > 15) {
        showToast("Phrase must be between 5 and 15 words.");
        return;
      }
      try {
        const res = await fetch("/api/next-greeting", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phrase }),
        });
        if (!res.ok) {
          const msg = await safeErrorText(res);
          throw new Error(msg || "Failed to save greeting phrase.");
        }
        showToast("Greeting phrase queued for the next call.");
        greetingModal.close();
      } catch (err) {
        showToast(err.message || "Failed to save greeting phrase.");
      }
    });
  }

  async function safeErrorText(res) {
    try {
      const t = await res.text();
      return t && t.length < 500 ? t : "";
    } catch {
      return "";
    }
  }

  // Call now logic with cap handling
  function initCallNow() {
    const btn = qs("#btnCallNow");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const res = await fetch("/api/call-now", { method: "POST" });
        if (res.status === 429) {
          showToast("Maximum call attempts reached for the allotted time.");
          return;
        }
        const data = await res.json().catch(() => ({}));
        if (data && data.ok === false && data.reason === "cap_reached") {
          showToast("Maximum call attempts reached for the allotted time.");
          return;
        }
        if (res.ok) {
          showToast("Call attempt requested.");
        } else {
          const msg = data && data.message ? data.message : await safeErrorText(res);
          showToast(msg || "Call request failed.");
        }
      } catch (err) {
        showToast(err.message || "Call request failed.");
      } finally {
        btn.disabled = false;
      }
    });
  }

  // Admin environment editor
  function renderEnvTable(items) {
    const container = qs("#envEditor");
    if (!container) return;
    container.innerHTML = "";

    const table = document.createElement("table");
    table.className = "env-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th style='width:28%'>Key</th><th>Value</th></tr>";
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    items.forEach((row) => {
      const tr = document.createElement("tr");
      const tdKey = document.createElement("td");
      const tdVal = document.createElement("td");

      const keyLabel = document.createElement("div");
      keyLabel.textContent = row.key;
      keyLabel.style.fontFamily = "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace";
      keyLabel.style.fontSize = "0.95rem";

      tdKey.appendChild(keyLabel);

      let input;
      if (row.key.endsWith("_DAYS")) {
        input = document.createElement("input");
        input.type = "text";
        input.placeholder = "Mon,Tue,Wed,Thu,Fri";
        input.value = row.value ?? "";
      } else if (row.key.endsWith("_HOURS_LOCAL")) {
        input = document.createElement("input");
        input.type = "text";
        input.placeholder = "09:00-18:00";
        input.value = row.value ?? "";
      } else if (row.key.endsWith("_SECONDS")) {
        input = document.createElement("input");
        input.type = "number";
        input.step = "1";
        input.min = "0";
        input.value = String(row.value ?? "");
      } else if (row.key === "ROTATE_PROMPTS" || row.key === "USE_NGROK" || row.key === "NONINTERACTIVE" || row.key === "LOG_COLOR") {
        input = document.createElement("select");
        ["true", "false"].forEach((v) => {
          const opt = document.createElement("option");
          opt.value = v;
          opt.textContent = v;
          if ((row.value ?? "").toString().toLowerCase() === v) opt.selected = true;
          input.appendChild(opt);
        });
      } else {
        input = document.createElement("input");
        input.type = "text";
        input.value = row.value ?? "";
      }
      input.dataset.key = row.key;
      input.autocomplete = "off";

      tdVal.appendChild(input);
      tr.appendChild(tdKey);
      tr.appendChild(tdVal);
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    container.appendChild(table);

    const saveBtn = qs("#btnSaveEnv");
    if (saveBtn) saveBtn.disabled = false;
  }

  async function loadEnvEditor() {
    const container = qs("#envEditor");
    const isAdmin = document.body.getAttribute("data-is-admin") === "1";
    if (!container || !isAdmin) return;
    const endpoint = container.getAttribute("data-endpoint-get") || "/api/admin/env";
    container.setAttribute("aria-busy", "true");
    try {
      const res = await fetch(endpoint, { method: "GET" });
      if (!res.ok) {
        const msg = await safeErrorText(res);
        throw new Error(msg || "Failed to load editable settings.");
      }
      const data = await res.json();
      if (!data || !Array.isArray(data.editable)) {
        throw new Error("Invalid response.");
      }
      renderEnvTable(data.editable);
    } catch (err) {
      container.innerHTML = `<div class="alert">Error: ${escapeHtml(err.message || "Failed to load settings.")}</div>`;
    } finally {
      container.removeAttribute("aria-busy");
    }
  }

  async function saveEnvEditor() {
    const container = qs("#envEditor");
    if (!container) return;
    const endpoint = container.getAttribute("data-endpoint-post") || "/api/admin/env";

    const inputs = qsa("input, select, textarea", container);
    const updates = {};
    inputs.forEach((el) => {
      const key = el.dataset.key;
      if (!key) return;
      updates[key] = el.value;
    });

    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      if (!res.ok) {
        const msg = await safeErrorText(res);
        throw new Error(msg || "Failed to save settings.");
      }
      showToast("Settings saved.");
    } catch (err) {
      showToast(err.message || "Failed to save settings.");
    }
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
    }[c]));
  }

  function initAdminPanel() {
    const isAdmin = document.body.getAttribute("data-is-admin") === "1";
    if (!isAdmin) return;
    loadEnvEditor();
    qs("#btnSaveEnv")?.addEventListener("click", saveEnvEditor);
  }

  // Status poller and renderer
  let statusTimer = null;

  function renderStatus(data) {
    const area = qs("#statusArea");
    if (!area) return;

    const parts = [];

    // Active window indicator
    if (data.within_active_window) {
      parts.push(`<div class="chip" style="display:inline-flex;align-items:center;gap:.5rem;padding:.3rem .55rem;border:1px solid var(--border);border-radius:999px;background:rgba(0,0,0,.2);">
        <span style="width:.5rem;height:.5rem;border-radius:50%;background:var(--accent,#25c2a0);display:inline-block"></span>
        <span>Active window</span>
      </div>`);
    } else {
      parts.push(`<div class="chip" style="display:inline-flex;align-items:center;gap:.5rem;padding:.3rem .55rem;border:1px solid var(--border);border-radius:999px;background:rgba(0,0,0,.2);">
        <span style="width:.5rem;height:.5rem;border-radius:50%;background:#e55353;display:inline-block"></span>
        <span>Inactive now (hours ${escapeHtml(data.active_hours_local || "")})</span>
      </div>`);
    }

    // Attempts info
    parts.push(`<span class="muted" style="margin-left:.5rem">Attempts (1h/day): ${data.attempts_last_hour}/${data.hourly_max_attempts} · ${data.attempts_last_day}/${data.daily_max_attempts}</span>`);

    // Countdown or cap wait
    let countdownHtml = "";
    if (!data.can_attempt_now && data.wait_seconds_if_capped > 0) {
      countdownHtml = `<div style="margin-top:.5rem">${buildCountdownBadge(data.wait_seconds_if_capped, data.wait_seconds_if_capped)}</div>`;
    } else if (typeof data.seconds_until_next === "number" && data.seconds_until_next != null) {
      const total = (typeof data.interval_total_seconds === "number" && data.interval_total_seconds > 0)
        ? data.interval_total_seconds
        : (data.seconds_until_next || 0);
      countdownHtml = `<div style="margin-top:.5rem">${buildCountdownBadge(data.seconds_until_next, total)}</div>`;
    } else {
      countdownHtml = `<div class="muted" style="margin-top:.5rem">Scheduling information is not available.</div>`;
    }

    area.innerHTML = `${parts.join(" ")} ${countdownHtml}`;
  }

  async function pollStatusOnce() {
    try {
      const res = await fetch("/api/status", { method: "GET", cache: "no-cache" });
      if (!res.ok) throw new Error(await safeErrorText(res) || "Failed to load status.");
      const data = await res.json();
      renderStatus(data);
    } catch (err) {
      const area = qs("#statusArea");
      if (area) {
        area.innerHTML = `<div class="muted">Status unavailable.</div>`;
      }
    }
  }

  function initStatusPoll() {
    pollStatusOnce();
    clearInterval(statusTimer);
    statusTimer = setInterval(pollStatusOnce, 1000);
    // Update immediately on visibility changes to keep clock sharp when tab is refocused
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pollStatusOnce();
    });
  }

  // Initialize all UI parts after DOM ready
  document.addEventListener("DOMContentLoaded", () => {
    initGreetingModal();
    initCallNow();
    initAdminPanel();
    initStatusPoll();
  });
})();

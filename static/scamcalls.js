(function () {
  "use strict";

  // Utility to format seconds to mm:ss
  function formatMMSS(totalSec) {
    totalSec = Math.max(0, Math.floor(totalSec));
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return { mStr: String(m).padStart(2, "0"), sStr: String(s).padStart(2, "0"), m, s };
  }

  // Smooth ring progress update
  function updateRingProgress(el, fraction) {
    const C = 2 * Math.PI * 54;
    const clamped = Math.max(0, Math.min(1, fraction));
    const offset = C * (1 - clamped);
    el.style.strokeDasharray = `${C}`;
    el.style.strokeDashoffset = `${offset}`;
  }

  function wsUrl(path) {
    const loc = window.location;
    const proto = loc.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + loc.host + path;
  }

  // Mu-law decode to Float32 [-1, 1]
  function muLawToLinear(uVal) {
    uVal = ~uVal & 0xff;
    const sign = (uVal & 0x80) ? -1 : 1;
    const exponent = (uVal >> 4) & 0x07;
    const mantissa = uVal & 0x0F;
    let sample = ((mantissa << 4) + 0x08) << (exponent + 3);
    sample = sign * sample;
    return Math.max(-32768, Math.min(32767, sample)) / 32768.0;
  }

  function base64ToUint8Array(b64) {
    const bin = atob(b64);
    const len = bin.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  // Very simple resampler from 8000 Hz -> target sampleRate using linear interpolation.
  function resampleLinear(float32Array, fromRate, toRate) {
    if (fromRate === toRate) return float32Array;
    const ratio = toRate / fromRate;
    const outLen = Math.floor(float32Array.length * ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const srcPos = i / ratio;
      const idx = Math.floor(srcPos);
      const frac = srcPos - idx;
      const s0 = float32Array[idx] || 0;
      const s1 = float32Array[idx + 1] || s0;
      out[i] = s0 + (s1 - s0) * frac;
    }
    return out;
  }

  class LiveAudioPlayer {
    constructor() {
      this.ctx = null;
      this.queueTime = 0;
      this.started = false;
    }

    async ensureContext() {
      if (!this.ctx) {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)();
        this.queueTime = this.ctx.currentTime;
      }
      if (this.ctx.state === "suspended") {
        await this.ctx.resume();
      }
    }

    async pushMuLawFrame(b64) {
      await this.ensureContext();
      const mulaw = base64ToUint8Array(b64);
      const N = mulaw.length;
      const pcm = new Float32Array(N);
      for (let i = 0; i < N; i++) {
        pcm[i] = muLawToLinear(mulaw[i]);
      }
      const sr = this.ctx.sampleRate;
      const up = resampleLinear(pcm, 8000, sr);
      const buffer = this.ctx.createBuffer(1, up.length, sr);
      buffer.getChannelData(0).set(up);

      const src = this.ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(this.ctx.destination);

      const startAt = Math.max(this.queueTime, this.ctx.currentTime + 0.05);
      try {
        src.start(startAt);
      } catch {
        // Ignore one-off scheduling errors
        try { src.start(); } catch {}
      }
      this.queueTime = startAt + buffer.duration;
      this.started = true;
    }

    async stop() {
      if (this.ctx) {
        try { await this.ctx.suspend(); } catch {}
        try { await this.ctx.close(); } catch {}
      }
      this.ctx = null;
      this.queueTime = 0;
      this.started = false;
    }
  }

  // DOM references
  const page = document.body.getAttribute("data-page");
  const isLivePage = page === "scamcalls";
  const isHistoryPage = page === "history";

  const state = {
    nextCallEpochSec: null,
    nextCallStartEpochSec: null,
    countdownTimer: null,
    pollTimer: null,
    activePollTimer: null,
    active: false,
    callSid: null,
    audioWs: null,
    player: null
  };

  async function apiGet(path) {
    const resp = await fetch(path, { headers: { "Accept": "application/json" }, cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching ${path}`);
    return resp.json();
  }

  async function apiGet(path) {
    const resp = await fetch(path, {
      method: "GET",
      headers: { "Accept": "application/json" }
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} getting ${path}`);
    return resp.json();
  }

  async function apiPost(path, body) {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null
    });
    if (!resp.ok) {
      if (resp.status === 429) {
        // For rate limit errors, create a special error
        const error = new Error(`HTTP ${resp.status} posting ${path}`);
        error.rateLimit = true;
        error.status = 429;
        throw error;
      }
      throw new Error(`HTTP ${resp.status} posting ${path}`);
    }
    return resp.json();
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function initLivePage() {
    const cdPanel = document.getElementById("countdownPanel");
    const callPanel = document.getElementById("callPanel");
    const statusDot = document.getElementById("statusDot");
    const ring = document.querySelector(".ring-progress");
    const cdMinutes = document.getElementById("cdMinutes");
    const cdSeconds = document.getElementById("cdSeconds");
    const cdSubtitle = document.getElementById("cdSubtitle");
    const conversation = document.getElementById("conversation");
    const callNowBtn = document.getElementById("callNowBtn");
    const listenBtn = document.getElementById("listenBtn");

    if (callNowBtn) {
      callNowBtn.addEventListener("click", async () => {
        try {
          callNowBtn.disabled = true;
          await apiPost("/api/scamcalls/call-now");
          setTimeout(() => { callNowBtn.disabled = false; }, 3000);
        } catch (err) {
          callNowBtn.disabled = false;
          // Check if it's a rate limit error
          if (err.rateLimit) {
            showToast("Max calls reached in alloted time. Dont over scam the scammer!", "error");
          } else {
            alert("Failed to request a call. Please try again.");
          }
        }
      });
    }

    if (listenBtn) {
      listenBtn.addEventListener("click", async () => {
        if (!state.player) state.player = new LiveAudioPlayer();
        if (!state.audioWs || state.audioWs.readyState !== WebSocket.OPEN) {
          startAudioWs();
        }
        try {
          await state.player.ensureContext();
          listenBtn.textContent = "Listening…";
          listenBtn.disabled = true;
        } catch {
          alert("Unable to start audio context. Please try again.");
        }
      });
    }

    function renderCountdown() {
      const end = state.nextCallEpochSec;
      const start = state.nextCallStartEpochSec;
      if (!end || !start || end <= start) {
        cdMinutes.textContent = "--";
        cdSeconds.textContent = "--";
        cdSubtitle.textContent = "Waiting for schedule...";
        updateRingProgress(ring, 0);
        return;
      }
      const now = Date.now() / 1000;
      const total = Math.max(1, end - start);
      const remaining = Math.max(0, end - now);

      const { mStr, sStr } = formatMMSS(remaining);
      cdMinutes.textContent = mStr;
      cdSeconds.textContent = sStr;

      const fraction = Math.max(0, Math.min(1, remaining / total));
      updateRingProgress(ring, fraction);

      cdSubtitle.textContent = remaining > 0 ? "Until next call attempt" : "Placing call…";
    }

    function startCountdown() {
      if (state.countdownTimer) return;
      renderCountdown();
      state.countdownTimer = setInterval(renderCountdown, 500);
    }
    function stopCountdown() {
      if (state.countdownTimer) {
        clearInterval(state.countdownTimer);
        state.countdownTimer = null;
      }
    }

    function renderTranscript(lines) {
      conversation.innerHTML = "";
      if (!lines || !lines.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No transcript yet…";
        conversation.appendChild(empty);
        return;
      }
      for (const msg of lines) {
        const row = document.createElement("div");
        row.className = "message" + (msg.partial ? " partial" : "");
        const role = document.createElement("div");
        role.className = "role " + (msg.role === "Assistant" ? "assistant" : "callee");
        role.textContent = msg.role;
        const text = document.createElement("div");
        text.className = "text";
        text.textContent = msg.text;
        row.appendChild(role);
        row.appendChild(text);
        conversation.appendChild(row);
      }
      conversation.scrollTop = conversation.scrollHeight;
    }

    async function refreshStatus() {
      try {
        const data = await apiGet("/api/scamcalls/status");

        state.active = !!data.active;
        state.callSid = data.callSid || null;

        state.nextCallEpochSec = data.nextCallEpochSec || null;
        state.nextCallStartEpochSec = data.nextCallStartEpochSec || null;

        setText("destNumber", data.destNumber || "—");
        setText("fromNumber", data.fromNumber || "—");
        setText("activeWindow", data.activeWindow || "—");
        if (data.caps && typeof data.caps.hourly === "number" && typeof data.caps.daily === "number") {
          setText("caps", `${data.caps.hourly}/hour, ${data.caps.daily}/day`);
        }
        setText("publicUrl", data.publicUrl || "auto");

        if (state.active) {
          statusDot.classList.remove("idle"); statusDot.classList.add("active");
          hide(cdPanel); show(callPanel);
          setText("callSid", state.callSid || "—");
          setText("callStatusBadge", "Connected");
          if (listenBtn) {
            listenBtn.textContent = "Listen Live";
            listenBtn.disabled = false;
          }
          stopCountdown();
          startActivePolling();
        } else {
          statusDot.classList.remove("active"); statusDot.classList.add("idle");
          show(cdPanel); hide(callPanel);
          stopActivePolling();
          stopAudioWs();
          if (listenBtn) {
            listenBtn.textContent = "Listen Live";
            listenBtn.disabled = false;
          }
          startCountdown();
        }
      } catch (err) {
        setText("appStatus", `Error: ${(err && err.message) || err}`);
      }
    }

    async function pollActiveCall() {
      if (!state.active) return;
      try {
        const data = await apiGet("/api/scamcalls/active");
        if (data && data.callSid) {
          setText("callSid", data.callSid);
        }
        if (data && Array.isArray(data.transcript)) {
          renderTranscript(data.transcript);
        }
        if (data && (data.status === "ending" || data.status === "completed" || data.status === "idle")) {
          state.active = false;
          stopActivePolling();
          stopAudioWs();
          await refreshStatus();
        }
      } catch {
        // Non-fatal
      }
    }

    function startActivePolling() {
      stopCountdown();
      stopActivePolling();
      pollActiveCall();
      state.activePollTimer = setInterval(pollActiveCall, 1000);
    }
    function stopActivePolling() {
      if (state.activePollTimer) {
        clearInterval(state.activePollTimer);
        state.activePollTimer = null;
      }
    }

    function startAudioWs() {
      if (state.audioWs && state.audioWs.readyState === WebSocket.OPEN) return;
      state.audioWs = new WebSocket(wsUrl("/ws/live-audio"));
      if (!state.player) state.player = new LiveAudioPlayer();

      state.audioWs.onmessage = async (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg && msg.type === "media" && typeof msg.payload === "string") {
            await state.player.pushMuLawFrame(msg.payload);
          }
        } catch {
          // ignore malformed frames
        }
      };
      state.audioWs.onclose = () => {};
      state.audioWs.onerror = () => {};
    }

    async function stopAudioWs() {
      if (state.audioWs) {
        try { state.audioWs.close(); } catch {}
      }
      state.audioWs = null;
      if (state.player) {
        try { await state.player.stop(); } catch {}
        state.player = null;
      }
    }

    // Initial kick-off and periodic refresh
    refreshStatus();
    state.pollTimer = setInterval(refreshStatus, 5000);
  }

  function initHistoryPage() {
    const list = document.getElementById("historyList");
    const panel = document.getElementById("transcriptPanel");
    const conv = document.getElementById("historyConversation");
    const outcome = document.getElementById("historyOutcome");
    const callSidEl = document.getElementById("historyCallSid");

    function renderHistory(calls) {
      list.innerHTML = "";
      if (!calls || !calls.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No calls yet.";
        list.appendChild(empty);
        return;
      }
      for (const c of calls) {
        const item = document.createElement("div");
        item.className = "history-item";

        const title = document.createElement("div");
        title.className = "title";
        const started = new Date((c.startedAt || 0) * 1000);
        title.textContent = started.toLocaleString();
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `SID ${c.callSid} · ${Math.round(c.durationSec || 0)}s`;
        const outcomeEl = document.createElement("div");
        outcomeEl.className = "outcome";
        outcomeEl.textContent = c.outcome || "—";
        const act = document.createElement("div");
        act.className = "action";
        const btn = document.createElement("a");
        btn.href = "javascript:void(0)";
        btn.className = "button secondary";
        btn.textContent = "View Transcript";
        btn.addEventListener("click", () => loadTranscript(c.callSid));

        item.appendChild(title);
        item.appendChild(meta);
        item.appendChild(outcomeEl);
        item.appendChild(act);
        act.appendChild(btn);
        list.appendChild(item);
      }
    }

    function renderTranscript(lines) {
      conv.innerHTML = "";
      if (!lines || !lines.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No transcript available.";
        conv.appendChild(empty);
        return;
      }
      for (const msg of lines) {
        const row = document.createElement("div");
        row.className = "message";
        const role = document.createElement("div");
        role.className = "role " + (msg.role === "Assistant" ? "assistant" : "callee");
        role.textContent = msg.role;
        const text = document.createElement("div");
        text.className = "text";
        text.textContent = msg.text;
        row.appendChild(role);
        row.appendChild(text);
        conv.appendChild(row);
      }
      conv.scrollTop = conv.scrollHeight;
    }

    async function loadHistory() {
      try {
        const data = await apiGet("/api/scamcalls/history");
        renderHistory((data && data.calls) || []);
        document.getElementById("publicUrl").textContent = (data && data.publicUrl) || "auto";
      } catch (err) {
        list.innerHTML = `<div class="empty">Error loading history: ${(err && err.message) || err}</div>`;
      }
    }

    async function loadTranscript(callSid) {
      try {
        const data = await apiGet(`/api/scamcalls/transcript/${encodeURIComponent(callSid)}`);
        callSidEl.textContent = callSid;
        outcome.textContent = data.outcome || "—";
        renderTranscript((data && data.transcript) || []);
        panel.classList.remove("hidden");
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (err) {
        callSidEl.textContent = callSid;
        outcome.textContent = "Error";
        conv.innerHTML = `<div class="empty">Error loading transcript: ${(err && err.message) || err}</div>`;
        panel.classList.remove("hidden");
      }
    }

    loadHistory();
  }

  const pageLive = isLivePage;
  if (pageLive) initLivePage();
  if (isHistoryPage) initHistoryPage();

  // ===============================
  // Admin and Modal Functionality
  // ===============================
  
  let isAdminLoggedIn = false;

  // Toast notifications
  function showToast(message, type = "info") {
    const container = document.getElementById("toastContainer");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.innerHTML = `<div class="toast-message">${message}</div>`;
    
    container.appendChild(toast);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 5000);
  }

  // Modal utilities
  function showModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove("hidden");
  }

  function hideModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add("hidden");
  }

  // Admin functionality
  function updateAdminButton() {
    const adminBtn = document.getElementById("adminBtn");
    if (!adminBtn) return;
    
    if (isAdminLoggedIn) {
      adminBtn.textContent = "Admin ✓";
      adminBtn.classList.add("logged-in");
    } else {
      adminBtn.textContent = "Admin";
      adminBtn.classList.remove("logged-in");
    }
  }

  async function adminLogin(username, password) {
    try {
      await apiPost("/api/admin/login", { username, password });
      isAdminLoggedIn = true;
      updateAdminButton();
      hideModal("adminLoginModal");
      showToast("Admin login successful", "success");
      return true;
    } catch (err) {
      return false;
    }
  }

  async function adminLogout() {
    try {
      await apiPost("/api/admin/logout");
      isAdminLoggedIn = false;
      updateAdminButton();
      hideModal("adminSettingsModal");
      showToast("Admin logout successful", "success");
    } catch (err) {
      showToast("Logout failed", "error");
    }
  }

  async function loadAdminConfig() {
    try {
      const data = await apiGet("/api/admin/config");
      const configForm = document.getElementById("configForm");
      if (!configForm) return;

      configForm.innerHTML = "";
      
      for (const [key, value] of Object.entries(data.config || {})) {
        const group = document.createElement("div");
        group.className = "form-group";
        group.innerHTML = `
          <label for="config_${key}">${key}:</label>
          <input type="text" id="config_${key}" class="form-input" value="${value}" data-key="${key}" />
        `;
        configForm.appendChild(group);
      }
    } catch (err) {
      showToast("Failed to load config", "error");
    }
  }

  async function saveAdminConfig() {
    try {
      const configForm = document.getElementById("configForm");
      if (!configForm) return;

      const updates = {};
      const inputs = configForm.querySelectorAll("input[data-key]");
      
      inputs.forEach(input => {
        updates[input.dataset.key] = input.value;
      });

      await fetch("/api/admin/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates })
      });

      showToast("Configuration saved successfully", "success");
    } catch (err) {
      showToast("Failed to save configuration", "error");
    }
  }

  // Greeting phrase functionality
  function updateWordCount() {
    const input = document.getElementById("greetingInput");
    const counter = document.getElementById("wordCount");
    if (!input || !counter) return;

    const words = input.value.trim().split(/\s+/).filter(w => w.length > 0);
    counter.textContent = words.length;
    
    const saveBtn = document.getElementById("greetingSaveBtn");
    if (saveBtn) {
      saveBtn.disabled = words.length < 5 || words.length > 15;
    }
  }

  async function saveGreeting() {
    const input = document.getElementById("greetingInput");
    if (!input) return;

    try {
      await apiPost("/api/scamcalls/next-greeting", { phrase: input.value.trim() });
      hideModal("greetingModal");
      input.value = "";
      updateWordCount();
      showToast("Greeting phrase saved for next call", "success");
    } catch (err) {
      showToast("Failed to save greeting phrase", "error");
    }
  }

  // Event listeners for admin functionality
  if (pageLive) {
    // Admin button
    const adminBtn = document.getElementById("adminBtn");
    if (adminBtn) {
      adminBtn.addEventListener("click", () => {
        if (isAdminLoggedIn) {
          loadAdminConfig();
          showModal("adminSettingsModal");
        } else {
          showModal("adminLoginModal");
        }
      });
    }

    // Admin login modal
    const adminLoginBtn = document.getElementById("adminLoginBtn");
    const adminUsername = document.getElementById("adminUsername");
    const adminPassword = document.getElementById("adminPassword");
    const adminLoginError = document.getElementById("adminLoginError");
    
    if (adminLoginBtn && adminUsername && adminPassword) {
      adminLoginBtn.addEventListener("click", async () => {
        adminLoginError.classList.add("hidden");
        const success = await adminLogin(adminUsername.value, adminPassword.value);
        if (!success) {
          adminLoginError.textContent = "Invalid credentials";
          adminLoginError.classList.remove("hidden");
        }
        adminUsername.value = "";
        adminPassword.value = "";
      });

      // Enter key support
      adminPassword.addEventListener("keypress", (e) => {
        if (e.key === "Enter") {
          adminLoginBtn.click();
        }
      });
    }

    // Admin login cancel
    const adminLoginCancel = document.getElementById("adminLoginCancel");
    if (adminLoginCancel) {
      adminLoginCancel.addEventListener("click", () => {
        hideModal("adminLoginModal");
      });
    }

    // Admin settings modal
    const adminSaveBtn = document.getElementById("adminSaveBtn");
    if (adminSaveBtn) {
      adminSaveBtn.addEventListener("click", saveAdminConfig);
    }

    const adminLogoutBtn = document.getElementById("adminLogoutBtn");
    if (adminLogoutBtn) {
      adminLogoutBtn.addEventListener("click", adminLogout);
    }

    const adminSettingsCancel = document.getElementById("adminSettingsCancel");
    if (adminSettingsCancel) {
      adminSettingsCancel.addEventListener("click", () => {
        hideModal("adminSettingsModal");
      });
    }

    // Greeting modal
    const addGreetingBtn = document.getElementById("addGreetingBtn");
    if (addGreetingBtn) {
      addGreetingBtn.addEventListener("click", () => {
        showModal("greetingModal");
        updateWordCount();
      });
    }

    const greetingInput = document.getElementById("greetingInput");
    if (greetingInput) {
      greetingInput.addEventListener("input", updateWordCount);
    }

    const greetingSaveBtn = document.getElementById("greetingSaveBtn");
    if (greetingSaveBtn) {
      greetingSaveBtn.addEventListener("click", saveGreeting);
    }

    const greetingCancel = document.getElementById("greetingCancel");
    if (greetingCancel) {
      greetingCancel.addEventListener("click", () => {
        hideModal("greetingModal");
        greetingInput.value = "";
        updateWordCount();
      });
    }

    // Close modals when clicking outside
    document.addEventListener("click", (e) => {
      if (e.target.classList.contains("modal")) {
        e.target.classList.add("hidden");
      }
    });

    // Initialize admin button state
    updateAdminButton();
  }
})();

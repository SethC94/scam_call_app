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

  async function apiPost(path, body) {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} posting ${path}`);
    return resp.json();
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  // Enhanced API functions for new features
  async function apiPut(url, data = {}) {
    const res = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  // Toast notification system
  function showToast(message, type = "success") {
    const container = document.getElementById("toastContainer");
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    
    container.appendChild(toast);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
      if (toast.parentNode === container) {
        container.removeChild(toast);
      }
    }, 5000);
  }

  // Modal management
  function showModal(modalId) {
    document.getElementById(modalId).classList.remove("hidden");
  }

  function hideModal(modalId) {
    document.getElementById(modalId).classList.add("hidden");
  }

  // Admin state
  let isAdminLoggedIn = false;

  // Admin functionality
  async function checkAdminStatus() {
    try {
      const config = await apiGet("/api/admin/config");
      isAdminLoggedIn = true;
      updateAdminButton();
    } catch {
      isAdminLoggedIn = false;
      updateAdminButton();
    }
  }

  function updateAdminButton() {
    const btn = document.getElementById("adminBtn");
    if (isAdminLoggedIn) {
      btn.textContent = "Admin (Logged In)";
      btn.style.background = "#14b89a";
      btn.style.color = "#04221f";
    } else {
      btn.textContent = "Admin";
      btn.style.background = "#0f2a29";
      btn.style.color = "#9ff3d7";
    }
  }

  async function adminLogin(username, password) {
    try {
      await apiPost("/api/admin/login", { username, password });
      isAdminLoggedIn = true;
      updateAdminButton();
      hideModal("adminLoginModal");
      showToast("Admin login successful");
      if (document.getElementById("adminSettingsModal").classList.contains("hidden")) {
        showAdminSettings();
      }
    } catch (error) {
      showToast("Login failed: Invalid credentials", "error");
    }
  }

  async function adminLogout() {
    try {
      await apiPost("/api/admin/logout");
      isAdminLoggedIn = false;
      updateAdminButton();
      hideModal("adminSettingsModal");
      showToast("Logged out successfully");
    } catch (error) {
      showToast("Logout failed", "error");
    }
  }

  async function showAdminSettings() {
    if (!isAdminLoggedIn) {
      showModal("adminLoginModal");
      return;
    }

    try {
      const response = await apiGet("/api/admin/config");
      const config = response.config;
      
      const tableEl = document.getElementById("configTable");
      tableEl.innerHTML = "";
      
      // Create table
      const table = document.createElement("table");
      table.style.width = "100%";
      table.style.borderCollapse = "collapse";
      
      // Header
      const headerRow = table.insertRow();
      headerRow.innerHTML = "<th style='padding: 8px; border: 1px solid rgba(255,255,255,0.2); text-align: left;'>Key</th><th style='padding: 8px; border: 1px solid rgba(255,255,255,0.2); text-align: left;'>Value</th>";
      
      // Rows for each config item
      for (const [key, value] of Object.entries(config)) {
        const row = table.insertRow();
        const keyCell = row.insertCell();
        const valueCell = row.insertCell();
        
        keyCell.style.cssText = "padding: 8px; border: 1px solid rgba(255,255,255,0.2);";
        valueCell.style.cssText = "padding: 8px; border: 1px solid rgba(255,255,255,0.2);";
        
        keyCell.textContent = key;
        
        const input = document.createElement("input");
        input.type = "text";
        input.value = value;
        input.dataset.key = key;
        input.style.cssText = "width: 100%; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.2); color: #d4f1f9; padding: 4px; border-radius: 4px;";
        
        valueCell.appendChild(input);
      }
      
      tableEl.appendChild(table);
      showModal("adminSettingsModal");
    } catch (error) {
      showToast("Failed to load admin settings", "error");
    }
  }

  async function saveAdminSettings() {
    const inputs = document.querySelectorAll("#configTable input[data-key]");
    const updates = {};
    
    for (const input of inputs) {
      updates[input.dataset.key] = input.value;
    }
    
    try {
      const response = await apiPut("/api/admin/config", { updates });
      showToast(`Settings saved successfully. Updated: ${response.saved.join(", ")}`);
    } catch (error) {
      showToast("Failed to save settings", "error");
    }
  }

  // Greeting phrase functionality
  function updateWordCount() {
    const textarea = document.getElementById("greetingPhrase");
    const wordCountEl = document.getElementById("wordCount");
    const words = textarea.value.trim().split(/\s+/).filter(w => w.length > 0);
    const count = words.length;
    
    wordCountEl.textContent = `Word count: ${count}`;
    wordCountEl.style.color = (count >= 5 && count <= 15) ? "#87f7cf" : "#ff6b6b";
  }

  async function submitGreetingPhrase(phrase) {
    try {
      await apiPost("/api/scamcalls/next-greeting", { phrase });
      hideModal("greetingModal");
      showToast("Greeting phrase added for next call");
      document.getElementById("greetingPhrase").value = "";
      updateWordCount();
    } catch (error) {
      showToast("Failed to add greeting phrase", "error");
    }
  }

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
          const response = await fetch("/api/scamcalls/call-now", {
            method: "POST",
            headers: { "Content-Type": "application/json" }
          });
          
          if (response.status === 429) {
            const errorData = await response.json();
            if (errorData.error === "cap") {
              showToast(errorData.message || "Max calls reached in alloted time. Dont over scam the scammer!", "error");
            }
          } else if (response.ok) {
            // Success - no need to show toast for normal behavior
          } else {
            throw new Error(`HTTP ${response.status}`);
          }
          
          setTimeout(() => { callNowBtn.disabled = false; }, 3000);
        } catch (error) {
          callNowBtn.disabled = false;
          showToast("Failed to request a call. Please try again.", "error");
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
    
    // Initialize admin functionality
    checkAdminStatus();
    
    // Event listeners for new buttons
    const adminBtn = document.getElementById("adminBtn");
    const addGreetingBtn = document.getElementById("addGreetingBtn");
    
    if (adminBtn) {
      adminBtn.addEventListener("click", () => {
        if (isAdminLoggedIn) {
          showAdminSettings();
        } else {
          showModal("adminLoginModal");
        }
      });
    }
    
    if (addGreetingBtn) {
      addGreetingBtn.addEventListener("click", () => {
        showModal("greetingModal");
      });
    }
    
    // Admin login modal handlers
    const adminLoginForm = document.getElementById("adminLoginForm");
    const adminLoginCancel = document.getElementById("adminLoginCancel");
    
    if (adminLoginForm) {
      adminLoginForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const username = document.getElementById("adminUsername").value;
        const password = document.getElementById("adminPassword").value;
        adminLogin(username, password);
      });
    }
    
    if (adminLoginCancel) {
      adminLoginCancel.addEventListener("click", () => {
        hideModal("adminLoginModal");
      });
    }
    
    // Admin settings modal handlers
    const adminLogoutBtn = document.getElementById("adminLogoutBtn");
    const adminSettingsCancel = document.getElementById("adminSettingsCancel");
    const adminSettingsSave = document.getElementById("adminSettingsSave");
    
    if (adminLogoutBtn) {
      adminLogoutBtn.addEventListener("click", adminLogout);
    }
    
    if (adminSettingsCancel) {
      adminSettingsCancel.addEventListener("click", () => {
        hideModal("adminSettingsModal");
      });
    }
    
    if (adminSettingsSave) {
      adminSettingsSave.addEventListener("click", saveAdminSettings);
    }
    
    // Greeting phrase modal handlers
    const greetingForm = document.getElementById("greetingForm");
    const greetingCancel = document.getElementById("greetingCancel");
    const greetingPhrase = document.getElementById("greetingPhrase");
    
    if (greetingForm) {
      greetingForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const phrase = document.getElementById("greetingPhrase").value.trim();
        const words = phrase.split(/\s+/).filter(w => w.length > 0);
        
        if (words.length < 5 || words.length > 15) {
          showToast("Phrase must be 5-15 words", "error");
          return;
        }
        
        submitGreetingPhrase(phrase);
      });
    }
    
    if (greetingCancel) {
      greetingCancel.addEventListener("click", () => {
        hideModal("greetingModal");
      });
    }
    
    if (greetingPhrase) {
      greetingPhrase.addEventListener("input", updateWordCount);
      updateWordCount(); // Initialize word count
    }
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
})();

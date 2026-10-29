(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const dom = {
    loginView: $("login-view"),
    loginForm: $("login-form"),
    address: $("address"),
    schemeTag: $("scheme-tag"),
    password: $("password"),
    revealBtn: $("reveal-btn"),
    connectBtn: $("connect-btn"),
    connectLabel: $("connect-label"),
    loginError: $("login-error"),
    securityNote: $("security-note"),
    securityDot: $("security-dot"),
    sessionView: $("session-view"),
    stage: $("stage"),
    canvas: $("screen"),
    stageHint: $("stage-hint"),
    loadingOverlay: $("loading-overlay"),
    loadingText: $("loading-text"),
    insecureBanner: $("insecure-banner"),
    toolbar: $("toolbar"),
    toolbarMini: $("toolbar-mini"),
    cycleMini: $("cycle-mini"),
    collapseBtn: $("collapse-btn"),
    monitorSelect: $("monitor-select"),
    cycleBtn: $("cycle-btn"),
    stats: $("stats"),
    keysBtn: $("keys-btn"),
    keysMenu: $("keys-menu"),
    streamBtn: $("stream-btn"),
    streamMenu: $("stream-menu"),
    codecSelect: $("codec-select"),
    qualitySelect: $("quality-select"),
    fpsSelect: $("fps-select"),
    widthSelect: $("width-select"),
    zoomSelect: $("zoom-select"),
    streamStatus: $("stream-status"),
    panBtn: $("pan-btn"),
    fullscreenBtn: $("fullscreen-btn"),
    soundBtn: $("sound-btn"),
    disconnectBtn: $("disconnect-btn"),
    statusPill: $("status-pill"),
    statusText: $("status-text"),
    toast: $("toast"),
    lostOverlay: $("lost-overlay"),
    lostTitle: $("lost-title"),
    lostText: $("lost-text"),
    lostBtn: $("lost-btn"),
  };

  const HEADER_SIZE = 12;
  const MONITOR_KEY = "remotepc.monitor";
  const STREAM_KEY = "remotepc.stream";
  const ZOOM_KEY = "remotepc.zoom";
  const TOOLBAR_KEY = "remotepc.toolbarCollapsed";
  const SOUND_KEY = "remotepc.soundMuted";
  const textEncoder = new TextEncoder();

  const state = {
    ws: null,
    phase: "login",
    authed: false,
    intentional: false,
    monitors: [],
    monitorId: null,
    originalW: 0,
    originalH: 0,
    frameW: 0,
    frameH: 0,
    hasFrame: false,
    lastFrameAt: 0,
    dirty: false,
    interacted: false,
    tls: true,
    host: "",
    nextTimer: 0,
    connectTimer: 0,
    pingTimer: 0,
    statsTimer: 0,
    chromeTimer: 0,
    toastTimer: 0,
    hintTimer: 0,
    moveRaf: 0,
    wheelRaf: 0,
    pendingMove: null,
    lastPoint: { x: 0, y: 0 },
    pressedButtons: new Set(),
    textKeys: new Set(),
    keyDown: new Set(),
    localKeys: new Set(),
    pendingPassword: null,
    wheelX: 0,
    wheelY: 0,
    rtt: 0,
    menuOpen: false,
    streamOpen: false,
    toolbarCollapsed: false,
    soundMuted: false,
    audioCtx: null,
    audioGain: null,
    audioNextTime: 0,
    audioPending: [],
    stream: {
      codec: "jpeg",
      quality_mode: "fixed",
      quality: 85,
      width: 2560,
      fps: 60,
    },
    serverMaxWidth: 3840,
    zoom: "fit",
    panMode: false,
    panning: false,
    panX: 0,
    panY: 0,
    panStart: null,
    effectiveWidth: 0,
    h264Decoder: null,
    h264Codec: "",
    h264Timestamp: 0,
    stats: { frames: 0, bytes: 0, last: performance.now() },
  };

  let ctx = null;

  // ------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------

  function b64ToBytes(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function bytesToB64(bytes) {
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  function showError(message) {
    dom.loginError.textContent = message;
    dom.loginError.hidden = false;
  }

  function clearError() {
    dom.loginError.hidden = true;
    dom.loginError.textContent = "";
  }

  function setConnecting(active) {
    dom.connectBtn.disabled = active;
    dom.connectBtn.classList.toggle("is-busy", active);
    dom.connectLabel.textContent = active ? "Проверка пароля" : "Подключиться";
  }

  function formatRate(bytesPerSecond) {
    if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "0 КБ/с";
    if (bytesPerSecond >= 1024 * 1024) return `${(bytesPerSecond / (1024 * 1024)).toFixed(1)} МБ/с`;
    return `${Math.round(bytesPerSecond / 1024)} КБ/с`;
  }

  function showToast(message, timeout = 4200) {
    if (!message) return;
    dom.toast.textContent = message;
    dom.toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      dom.toast.hidden = true;
    }, timeout);
  }

  function wsOpen() {
    return state.ws && state.ws.readyState === WebSocket.OPEN;
  }

  function send(payload) {
    if (!wsOpen()) return false;
    state.ws.send(JSON.stringify(payload));
    return true;
  }

  // ------------------------------------------------------------------
  // Login
  // ------------------------------------------------------------------

  function initLogin() {
    dom.address.textContent = location.host || "—";
    const secureContext = window.isSecureContext && !!window.crypto?.subtle;
    const tls = location.protocol === "https:";
    dom.schemeTag.textContent = tls ? "TLS" : "HTTP";
    dom.schemeTag.classList.toggle("warn", !tls);

    if (!secureContext) {
      dom.connectBtn.disabled = true;
      dom.securityDot.classList.add("bad");
      dom.securityNote.textContent =
        "Для входа нужен HTTPS. Запустите хост без --no-tls и откройте https://<IP>:<порт>.";
      showError("Браузер не разрешает криптографию на незащищённом HTTP-адресе.");
    } else if (tls) {
      dom.securityNote.textContent = "TLS-соединение · пароль не передаётся по сети";
    } else {
      dom.securityDot.classList.add("warn");
      dom.securityNote.textContent = "Без TLS · используйте только в доверенной сети";
    }

    dom.revealBtn.addEventListener("click", () => {
      const visible = dom.password.type === "text";
      dom.password.type = visible ? "password" : "text";
      dom.revealBtn.textContent = visible ? "Показать" : "Скрыть";
      dom.revealBtn.setAttribute("aria-label", visible ? "Показать пароль" : "Скрыть пароль");
      dom.password.focus();
    });

    dom.password.addEventListener("input", clearError);
    dom.loginForm.addEventListener("submit", (event) => {
      event.preventDefault();
      clearError();
      if (!wsOpen() && state.phase === "connecting") return;
      const password = dom.password.value;
      if (password.length < 6) {
        showError("Пароль должен содержать минимум 6 символов.");
        dom.password.focus();
        return;
      }
      if (!window.isSecureContext || !window.crypto?.subtle) {
        showError("Подключение возможно только по HTTPS (или через localhost).");
        return;
      }
      setConnecting(true);
      dom.connectLabel.textContent = "Соединение";
      connect(password).catch((error) => {
        console.error(error);
        state.phase = "login";
        setConnecting(false);
        showError("Не удалось открыть соединение. Проверьте адрес и порт.");
      });
    });
  }

  async function connect(password) {
    closeSocket();
    state.phase = "connecting";
    state.intentional = false;
    state.authed = false;
    state.hasFrame = false;
    state.interacted = false;
    state.dirty = false;
    clearTimeout(state.connectTimer);

    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${scheme}//${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    state.pendingPassword = password;

    let chain = Promise.resolve();
    ws.addEventListener("message", (event) => {
      chain = chain
        .then(() => handleMessage(event, ws))
        .catch((error) => console.error("message handling failed", error));
    });

    ws.addEventListener("close", (event) => handleSocketClose(event, ws));
    ws.addEventListener("error", () => {
      // A close event always follows in browsers; the close handler shows the
      // actual reason.  Keeping this listener avoids unhandled noise.
    });

    state.connectTimer = setTimeout(() => {
      if (ws !== state.ws || state.phase !== "connecting") return;
      state.phase = "failed";
      state.pendingPassword = null;
      setConnecting(false);
      showError("Сервер не отвечает. Проверьте IP, порт и брандмауэр Windows.");
      closeSocket();
    }, 13000);
  }

  async function handleMessage(event, ws) {
    if (ws !== state.ws) return;
    if (typeof event.data !== "string") {
      await handleFrame(event.data, ws);
      return;
    }

    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!message || typeof message.type !== "string") return;

    switch (message.type) {
      case "hello":
        if (state.phase !== "connecting") return;
        if (message.version !== 1) {
          failLogin("Версия клиента не совпадает с версией хоста. Обновите страницу.");
          return;
        }
        state.tls = message.tls !== false;
        await sendAuth(message, ws);
        break;
      case "auth_ok":
        startSession(message, ws);
        break;
      case "auth_fail":
        failLogin(message.message || "Неверный пароль.");
        break;
      case "busy":
        failLogin(message.message || "К компьютеру уже подключён другой пользователь.");
        break;
      case "screen":
        onScreen(message);
        break;
      case "stream_ok":
        applyStreamOk(message);
        break;
      case "nochange": {
        const active = state.dirty;
        state.dirty = false;
        requestNext(active ? 45 : 200);
        break;
      }
      case "pong":
        if (typeof message.t === "number") {
          state.rtt = Math.max(0, performance.now() - message.t);
        }
        break;
      case "toast":
        showToast(message.message);
        break;
      case "error":
        if (state.phase === "connecting") {
          failLogin(message.message || "Сервер отклонил подключение.");
        } else {
          showToast(message.message || "Ошибка удалённого ПК.");
          requestNext(1200);
        }
        break;
      default:
        break;
    }
  }

  async function sendAuth(hello, ws) {
    if (!hello || typeof hello.salt !== "string" || typeof hello.nonce !== "string") {
      failLogin("Некорректный ответ сервера.");
      return;
    }
    const password = state.pendingPassword;
    if (!password) {
      failLogin("Сеанс авторизации истёк. Попробуйте ещё раз.");
      return;
    }
    const iterations = Number(hello.iterations);
    if (!Number.isInteger(iterations) || iterations < 10000 || iterations > 1000000) {
      failLogin("Некорректные параметры авторизации.");
      return;
    }

    let salt;
    let serverNonce;
    try {
      salt = b64ToBytes(hello.salt);
      serverNonce = b64ToBytes(hello.nonce);
    } catch {
      failLogin("Некорректные параметры авторизации.");
      return;
    }
    if (salt.length < 16 || serverNonce.length < 16) {
      failLogin("Некорректные параметры авторизации.");
      return;
    }

    dom.connectLabel.textContent = "Проверка пароля";
    let material;
    let key;
    let signature;
    const clientNonce = crypto.getRandomValues(new Uint8Array(16));
    try {
      material = await crypto.subtle.importKey(
        "raw",
        textEncoder.encode(password),
        "PBKDF2",
        false,
        ["deriveBits"],
      );
      const bits = await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
        material,
        256,
      );
      key = await crypto.subtle.importKey("raw", bits, { name: "HMAC", hash: "SHA-256" }, false, [
        "sign",
      ]);
      const prefix = textEncoder.encode("RemotePC auth v1|");
      const pipe = textEncoder.encode("|");
      const message = new Uint8Array(
        prefix.length + serverNonce.length + pipe.length + clientNonce.length,
      );
      message.set(prefix, 0);
      message.set(serverNonce, prefix.length);
      message.set(pipe, prefix.length + serverNonce.length);
      message.set(clientNonce, prefix.length + serverNonce.length + pipe.length);
      signature = await crypto.subtle.sign("HMAC", key, message);
    } catch (error) {
      console.error(error);
      failLogin("Браузер не смог вычислить подтверждение пароля.");
      return;
    }

    if (ws !== state.ws || !wsOpen()) return;
    ws.send(
      JSON.stringify({
        type: "auth",
        client_nonce: bytesToB64(clientNonce),
        proof: bytesToB64(new Uint8Array(signature)),
      }),
    );
  }

  function failLogin(message) {
    state.phase = "failed";
    state.authed = false;
    state.pendingPassword = null;
    clearTimeout(state.connectTimer);
    setConnecting(false);
    showError(message);
    closeSocket();
  }

  function startSession(message, ws) {
    if (ws !== state.ws) return;
    clearTimeout(state.connectTimer);
    state.phase = "connected";
    state.authed = true;
    state.pendingPassword = null;
    state.tls = message.tls !== false;
    state.host = typeof message.host === "string" ? message.host : location.host;
    state.monitors = Array.isArray(message.monitors) ? message.monitors : [];
    if (!state.monitors.length) {
      failLogin("На хосте не найден ни один монитор.");
      return;
    }

    dom.password.value = "";
    setConnecting(false);
    dom.loginView.hidden = true;
    dom.sessionView.hidden = false;
    dom.lostOverlay.hidden = true;
    dom.insecureBanner.hidden = state.tls;
    state.audioPending.length = 0;
    state.audioNextTime = 0;
    updateSoundButton();
    document.title = `RemotePC — ${state.host}`;
    dom.statusText.textContent = `Подключено · ${state.host}`;
    closeAllMenus();

    populateMonitors();
    const stored = readStoredMonitor();
    const monitor = state.monitors.find((item) => Number(item.id) === stored) || state.monitors[0];
    dom.monitorSelect.value = String(monitor.id);
    state.monitorId = Number(monitor.id);
    state.stats.frames = 0;
    state.stats.bytes = 0;
    state.stats.last = performance.now();
    state.rtt = 0;
    state.panX = 0;
    state.panY = 0;
    state.panMode = false;
    state.panning = false;
    state.effectiveWidth = 0;
    dom.stage.classList.remove("pan-mode", "panning");
    setupStream(message);

    dom.loadingText.textContent = "Загрузка экрана…";
    dom.loadingOverlay.classList.remove("is-hidden");
    dom.stageHint.classList.add("is-hidden");
    dom.canvas.focus({ preventScroll: true });
    requestAnimationFrame(() => {
      fitCanvas();
      const collapsed = readToolbarCollapsed();
      state.toolbarCollapsed = collapsed;
      setToolbarCollapsed(collapsed, false);
      showChrome();
    });
    startTimers();
    sendView(state.monitorId);
  }

  function populateMonitors() {
    dom.monitorSelect.replaceChildren();
    for (const monitor of state.monitors) {
      const option = document.createElement("option");
      option.value = String(monitor.id);
      const primary = monitor.primary ? " · основной" : "";
      option.textContent = `Монитор ${monitor.id} · ${monitor.width}×${monitor.height}${primary}`;
      option.title = monitor.name || "";
      dom.monitorSelect.append(option);
    }
    dom.monitorSelect.disabled = state.monitors.length < 2;
    dom.cycleBtn.disabled = state.monitors.length < 2;
    dom.cycleMini.disabled = state.monitors.length < 2;
    if (state.monitors.length < 2) dom.cycleMini.hidden = true;
  }

  function readStoredMonitor() {
    try {
      const value = Number(localStorage.getItem(MONITOR_KEY));
      return Number.isInteger(value) ? value : null;
    } catch {
      return null;
    }
  }

  function writeStoredMonitor(value) {
    try {
      localStorage.setItem(MONITOR_KEY, String(value));
    } catch {
      // Private mode or blocked storage: selection simply won't persist.
    }
  }

  function clampNumber(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function normalizeStream(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const codec =
      source.codec === "h264" ? "h264" : source.codec === "webp" ? "webp" : "jpeg";
    const qualityMode = source.quality_mode === "auto" ? "auto" : "fixed";
    let quality = Number(source.quality);
    if (!Number.isFinite(quality)) quality = 85;
    quality = Math.round(clampNumber(quality, 40, 95));
    let width = Number(source.width);
    if (!Number.isFinite(width)) width = 2560;
    width = width === 0 ? 0 : Math.round(clampNumber(width, 320, 7680));
    let fps = Number(source.fps);
    if (![15, 30, 45, 60].includes(fps)) fps = 60;
    return { codec, quality_mode: qualityMode, quality, width, fps };
  }

  function readStoredStream() {
    try {
      const raw = localStorage.getItem(STREAM_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      // Preferences written before the H.264 release should not pin an old
      // viewer to JPEG for ever.
      if (!parsed || parsed.version !== 2) return null;
      return normalizeStream(parsed);
    } catch {
      return null;
    }
  }

  function saveStream() {
    try {
      localStorage.setItem(STREAM_KEY, JSON.stringify({ ...state.stream, version: 2 }));
    } catch {
      // Storage is optional.
    }
  }

  function readStoredZoom() {
    try {
      const value = localStorage.getItem(ZOOM_KEY);
      return ["fit", "1", "1.5", "2"].includes(value) ? value : "fit";
    } catch {
      return "fit";
    }
  }

  function saveZoom() {
    try {
      localStorage.setItem(ZOOM_KEY, state.zoom);
    } catch {
      // Storage is optional.
    }
  }

  function streamLabel() {
    if (state.stream.codec === "h264") {
      const bitrate = state.stream.bitrate
        ? ` · ${(state.stream.bitrate / 1_000_000).toFixed(0)} Мбит/с`
        : "";
      const width = state.effectiveWidth || state.stream.width;
      return `H.264${bitrate} · ${state.stream.fps} fps · ${width ? `${width} px` : "нативно"}`;
    }
    const codec = state.stream.codec === "webp" ? "WebP" : "JPEG";
    const quality =
      state.stream.quality_mode === "auto" ? "авто" : String(state.stream.quality);
    const requested = state.stream.width === 0 ? "нативно" : `${state.stream.width} px`;
    const actual =
      state.effectiveWidth && state.effectiveWidth !== state.stream.width
        ? ` → ${state.effectiveWidth} px`
        : "";
    const chroma =
      state.stream.codec === "jpeg" && state.stream.quality_mode === "fixed" && state.stream.quality >= 80
        ? " · 4:4:4"
        : "";
    return `${codec} ${quality}${chroma} · ${state.stream.fps} fps · ${requested}${actual}`;
  }

  function ensureSelectOption(select, value, label) {
    if (!Array.from(select.options).some((option) => option.value === value)) {
      select.add(new Option(label, value));
    }
  }

  function updateStreamStatus() {
    dom.streamStatus.textContent = streamLabel();
    if (state.stream.quality_mode === "auto") {
      dom.qualitySelect.value = "auto";
    } else {
      const quality = String(state.stream.quality);
      ensureSelectOption(dom.qualitySelect, quality, `${quality} · из хоста`);
      dom.qualitySelect.value = quality;
    }
    dom.codecSelect.value = state.stream.codec;
    dom.fpsSelect.value = String(state.stream.fps);
    if (state.stream.width === 0) {
      dom.widthSelect.value = "native";
    } else {
      const width = String(state.stream.width);
      ensureSelectOption(dom.widthSelect, width, `${width} px`);
      dom.widthSelect.value = width;
    }
  }

  function updateZoomControls() {
    dom.zoomSelect.value = state.zoom;
  }

  function sendStream(announce = true) {
    saveStream();
    updateStreamStatus();
    if (!wsOpen()) return;
    send({
      type: "stream",
      codec: state.stream.codec,
      quality_mode: state.stream.quality_mode,
      quality: state.stream.quality,
      width: state.stream.width,
      fps: state.stream.fps,
    });
    if (announce) showToast(`Поток: ${streamLabel()}`);
  }

  function setupStream(message) {
    state.serverMaxWidth = Math.round(
      clampNumber(Number(message.max_width) || 3840, 640, 7680),
    );
    const serverDefaults = normalizeStream(message.stream);
    state.stream = readStoredStream() || serverDefaults;
    if (state.stream.codec === "h264" && typeof window.VideoDecoder !== "function") {
      state.stream.codec = "jpeg";
      showToast("H.264 WebCodecs недоступен, включён JPEG.", 7000);
    }
    state.zoom = readStoredZoom();
    updateStreamStatus();
    updateZoomControls();
    sendStream(false);
  }

  function applyStreamOk(message) {
    if (typeof message.codec === "string") {
      state.stream.codec = message.codec;
    }
    if (typeof message.quality === "number" && message.quality_mode === "auto") {
      state.stream.quality = Math.round(message.quality);
    }
    if (typeof message.bitrate === "number" && message.bitrate > 0) {
      state.stream.bitrate = message.bitrate;
    }
    if ([15, 30, 45, 60].includes(Number(message.fps))) {
      state.stream.fps = Number(message.fps);
    }
    if (typeof message.width === "number" && message.width > 0) {
      state.effectiveWidth = message.width;
    }
    if (message.codec !== "h264") {
      state.stream.bitrate = 0;
    }
    // A new stream_ok means the encoder will send a fresh keyframe.
    closeH264Decoder();
    if (message.warning) showToast(message.warning, 7000);
    updateStreamStatus();
  }

  function closeH264Decoder() {
    const decoder = state.h264Decoder;
    state.h264Decoder = null;
    state.h264Codec = "";
    if (decoder) {
      try {
        decoder.close();
      } catch {
        // Already closed.
      }
    }
  }

  function codecFromH264AccessUnit(data) {
    const fallback = "avc1.640034";
    for (let i = 0; i < data.length - 4; i += 1) {
      let header = -1;
      if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1) header = i + 3;
      else if (
        data[i] === 0 &&
        data[i + 1] === 0 &&
        data[i + 2] === 0 &&
        data[i + 3] === 1
      )
        header = i + 4;
      if (header < 0 || header + 3 >= data.length) continue;
      if ((data[header] & 0x1f) === 7) {
        const hex = (value) => value.toString(16).padStart(2, "0");
        return `avc1.${hex(data[header + 1])}${hex(data[header + 2])}${hex(data[header + 3])}`;
      }
    }
    return fallback;
  }

  function drawH264Frame(frame) {
    try {
      const width = frame.displayWidth;
      const height = frame.displayHeight;
      if (!ctx) ctx = dom.canvas.getContext("2d", { alpha: false, desynchronized: true });
      const sizeChanged =
        dom.canvas.width !== width || dom.canvas.height !== height;
      // The CSS fit must use the new monitor's dimensions, not the previous
      // frame's.  Update state before resizing/recomputing the fit.
      state.frameW = width;
      state.frameH = height;
      if (sizeChanged) {
        dom.canvas.width = width;
        dom.canvas.height = height;
      }
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(frame, 0, 0, width, height);
      const widthChanged = state.effectiveWidth !== width;
      if (widthChanged) {
        state.effectiveWidth = width;
        updateStreamStatus();
      }
      if (sizeChanged || widthChanged) {
        fitCanvas();
      }
      if (!state.hasFrame) {
        state.hasFrame = true;
        dom.loadingOverlay.classList.add("is-hidden");
        if (!state.interacted) {
          dom.stageHint.classList.remove("is-hidden");
          clearTimeout(state.hintTimer);
          state.hintTimer = setTimeout(() => dom.stageHint.classList.add("is-hidden"), 5000);
        }
        showChrome();
      }
      state.stats.frames += 1;
      state.lastFrameAt = performance.now();
    } finally {
      frame.close();
    }
  }

  function ensureH264Decoder(data, ws) {
    if (state.h264Decoder) return state.h264Decoder;
    if (typeof window.VideoDecoder !== "function") {
      showToast("Браузер не поддерживает H.264 WebCodecs. Включён JPEG.", 8000);
      state.stream.codec = "jpeg";
      sendStream(false);
      return null;
    }
    const codec = codecFromH264AccessUnit(data);
    const decoder = new VideoDecoder({
      output: (frame) => drawH264Frame(frame),
      error: (error) => {
        console.error("H.264 decode failed", error);
        closeH264Decoder();
        showToast("Ошибка декодирования H.264. Включён JPEG.", 7000);
        if (ws === state.ws && wsOpen()) {
          state.stream.codec = "jpeg";
          sendStream(false);
        }
      },
    });
    try {
      decoder.configure({
        codec,
        optimizeForLatency: true,
        avc: { format: "annexb" },
      });
    } catch (error) {
      console.error("H.264 configure failed", error);
      try {
        decoder.close();
      } catch {}
      showToast("Браузер не принял H.264. Включён JPEG.", 7000);
      state.stream.codec = "jpeg";
      sendStream(false);
      return null;
    }
    state.h264Decoder = decoder;
    state.h264Codec = codec;
    state.h264Timestamp = 0;
    return decoder;
  }

  function handleH264Frame(buffer, flags, width, height, ws) {
    const data = new Uint8Array(buffer, HEADER_SIZE);
    const decoder = ensureH264Decoder(data, ws);
    if (!decoder) return;
    try {
      const timestamp = state.h264Timestamp;
      state.h264Timestamp += 1;
      decoder.decode(
        new EncodedVideoChunk({
          type: flags === 4 ? "key" : "delta",
          timestamp,
          data,
        }),
      );
      state.stats.bytes += buffer.byteLength;
    } catch (error) {
      console.error("H.264 decode enqueue failed", error);
      closeH264Decoder();
      if (ws === state.ws && wsOpen()) {
        state.stream.codec = "jpeg";
        sendStream(false);
      }
    }
  }

  function sendView(monitorId) {
    const monitor = state.monitors.find((item) => Number(item.id) === Number(monitorId));
    if (!monitor) return;
    const preserveH264Frame = state.stream.codec === "h264" && state.hasFrame;
    state.monitorId = Number(monitor.id);
    writeStoredMonitor(state.monitorId);
    if (!preserveH264Frame) {
      state.frameW = 0;
      state.frameH = 0;
    }
    state.hasFrame = false;
    state.dirty = false;
    state.panX = 0;
    state.panY = 0;
    state.panning = false;
    setPanMode(false);
    closeH264Decoder();
    clearTimeout(state.nextTimer);
    if (!preserveH264Frame) clearScreen();
    dom.loadingOverlay.classList.remove("is-hidden");
    dom.loadingText.textContent = "Переключение монитора…";
    send({ type: "view", monitor: state.monitorId });
  }

  function onScreen(message) {
    if (typeof message.monitor !== "number") return;
    if (message.monitor !== state.monitorId) return;
    state.originalW = Number(message.width) || 0;
    state.originalH = Number(message.height) || 0;
    if (typeof message.stream_width === "number" && message.stream_width > 0) {
      state.effectiveWidth = message.stream_width;
      updateStreamStatus();
    }
    if (!state.hasFrame) {
      dom.loadingOverlay.classList.remove("is-hidden");
      dom.loadingText.textContent = "Получение кадра…";
    }
    // The server starts the capture itself; this also covers a lost "go".
    requestNext();
  }

  // ------------------------------------------------------------------
  // Session cleanup / loss
  // ------------------------------------------------------------------

  function startTimers() {
    stopTimers();
    state.pingTimer = setInterval(() => {
      if (wsOpen()) send({ type: "ping", t: performance.now() });
    }, 2000);
    state.statsTimer = setInterval(updateStats, 500);
  }

  function stopTimers() {
    clearInterval(state.pingTimer);
    clearInterval(state.statsTimer);
    clearTimeout(state.nextTimer);
    clearTimeout(state.chromeTimer);
    clearTimeout(state.toastTimer);
    clearTimeout(state.hintTimer);
    state.pingTimer = 0;
    state.statsTimer = 0;
    state.nextTimer = 0;
    state.chromeTimer = 0;
    state.toastTimer = 0;
    state.hintTimer = 0;
  }

  function handleSocketClose(event, ws) {
    if (ws !== state.ws) return;
    state.ws = null;
    state.pendingPassword = null;
    stopTimers();
    setConnecting(false);
    clearTimeout(state.connectTimer);
    state.authed = false;
    state.keyDown.clear();
    state.textKeys.clear();
    state.pressedButtons.clear();

    if (state.phase === "failed") {
      state.phase = "login";
      closeSocket();
      return;
    }
    if (state.phase === "connecting") {
      state.phase = "login";
      showError(
        event.code === 1006
          ? "Не удалось подключиться. Проверьте IP, порт, брандмауэр и проброс порта."
          : "Сервер закрыл соединение до авторизации.",
      );
      return;
    }
    if (state.phase === "connected") {
      state.phase = "closed";
      showLost(
        "Соединение потеряно",
        event.reason || "Хост закрыл сеанс или пропала сеть. Проверьте подключение.",
      );
      return;
    }
  }

  function showLost(title, text) {
    dom.lostTitle.textContent = title;
    dom.lostText.textContent = text;
    dom.lostOverlay.hidden = false;
  }

  function resetToLogin() {
    state.phase = "login";
    state.authed = false;
    state.pendingPassword = null;
    state.hasFrame = false;
    state.dirty = false;
    state.panMode = false;
    state.panning = false;
    state.panStart = null;
    state.localKeys.clear();
    state.audioPending.length = 0;
    state.audioNextTime = 0;
    closeH264Decoder();
    dom.stage.classList.remove("pan-mode", "panning");
    closeAllMenus();
    state.intentional = true;
    closeSocket();
    stopTimers();
    releaseAll();
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
    dom.sessionView.hidden = true;
    dom.loginView.hidden = false;
    dom.lostOverlay.hidden = true;
    dom.toolbar.hidden = false;
    dom.toolbarMini.hidden = true;
    dom.cycleMini.hidden = true;
    dom.toolbar.classList.remove("is-hidden");
    dom.statusPill.classList.remove("is-hidden");
    state.toolbarCollapsed = false;
    dom.password.value = "";
    dom.password.type = "password";
    dom.revealBtn.textContent = "Показать";
    setConnecting(false);
    clearError();
    document.title = "RemotePC — прямое подключение";
    dom.monitorSelect.replaceChildren();
    state.monitors = [];
    state.monitorId = null;
    state.frameW = 0;
    state.frameH = 0;
    clearScreen();
    requestAnimationFrame(() => dom.password.focus());
  }

  function closeSocket() {
    const ws = state.ws;
    state.ws = null;
    if (ws) {
      try {
        ws.close(1000, "bye");
      } catch {
        // Already closed.
      }
    }
  }

  function disconnect() {
    state.intentional = true;
    closeSocket();
    resetToLogin();
  }

  // ------------------------------------------------------------------
  // Frame loop
  // ------------------------------------------------------------------

  function kickFrame() {
    if (state.authed && state.hasFrame) requestNext(0);
  }

  function requestNext(delay = 0) {
    if (!state.authed || !wsOpen() || document.hidden) return;
    clearTimeout(state.nextTimer);
    if (delay > 0) {
      state.nextTimer = setTimeout(() => {
        state.nextTimer = 0;
        if (state.authed && wsOpen()) send({ type: "next" });
      }, delay);
      return;
    }
    state.nextTimer = 0;
    send({ type: "next" });
  }

  function updateSoundButton() {
    dom.soundBtn.classList.toggle("muted", state.soundMuted);
    dom.soundBtn.title = state.soundMuted ? "Звук выключен" : "Звук включён";
    dom.soundBtn.setAttribute("aria-pressed", String(!state.soundMuted));
  }

  function scheduleAudio(data, sampleRate, channels) {
    const ctx = state.audioCtx;
    if (!ctx || !state.audioGain) return;
    const bytesPerFrame = channels * 2;
    const frames = Math.floor(data.byteLength / bytesPerFrame);
    if (frames <= 0) return;
    try {
      const audioBuffer = ctx.createBuffer(channels, frames, sampleRate);
      const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
      for (let channel = 0; channel < channels; channel += 1) {
        const out = audioBuffer.getChannelData(channel);
        for (let i = 0; i < frames; i += 1) {
          out[i] = view.getInt16(i * bytesPerFrame + channel * 2, true) / 32768;
        }
      }
      const source = ctx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(state.audioGain);
      const now = ctx.currentTime;
      if (
        state.audioNextTime < now + 0.02 ||
        state.audioNextTime > now + 0.6
      ) {
        state.audioNextTime = now + 0.03;
      }
      source.start(state.audioNextTime);
      state.audioNextTime += frames / sampleRate;
    } catch (error) {
      console.warn("audio playback failed", error);
    }
  }

  function ensureAudio() {
    if (!state.authed) return;
    if (!state.audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      try {
        state.audioCtx = new Ctx({ sampleRate: 48000 });
      } catch {
        state.audioCtx = new Ctx();
      }
      state.audioGain = state.audioCtx.createGain();
      state.audioGain.gain.value = state.soundMuted ? 0 : 1;
      state.audioGain.connect(state.audioCtx.destination);
      state.audioNextTime = 0;
      const pending = state.audioPending.splice(0);
      for (const item of pending) scheduleAudio(item.data, item.sampleRate, item.channels);
    }
    if (state.audioCtx.state === "suspended") {
      state.audioCtx.resume().catch(() => {});
    }
  }

  function handleAudioFrame(buffer, sampleRate, channels) {
    if (![1, 2].includes(channels) || !sampleRate) return;
    const data = new Uint8Array(buffer, HEADER_SIZE);
    if (!state.audioCtx) {
      if (state.audioPending.length < 12) {
        state.audioPending.push({
          data: data.slice(),
          sampleRate,
          channels,
        });
      }
      return;
    }
    scheduleAudio(data, sampleRate, channels);
  }

  async function handleFrame(buffer, ws) {
    if (ws !== state.ws || state.phase !== "connected") return;
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength <= HEADER_SIZE) {
      requestNext(200);
      return;
    }
    const view = new DataView(buffer);
    if (view.getUint8(0) !== 0x52 || view.getUint8(1) !== 0x50) {
      requestNext(200);
      return;
    }
    const flags = view.getUint8(2);
    const monitor = view.getUint8(7);
    const width = view.getUint16(8, true);
    const height = view.getUint16(10, true);
    if (!width || !height || buffer.byteLength <= HEADER_SIZE) {
      requestNext(200);
      return;
    }
    if (flags === 5) {
      handleAudioFrame(buffer, width, height);
      return;
    }
    if (monitor !== state.monitorId) {
      requestNext();
      return;
    }

    if (flags === 3 || flags === 4) {
      if (state.stream.codec === "h264") {
        handleH264Frame(buffer, flags, width, height, ws);
      }
      return;
    }

    // Ask for the next frame before decoding this one.  Capture/encode on the
    // host overlaps with JPEG decode and canvas drawing in the browser, which
    // removes a full round-trip from the frame interval.
    requestNext();

    let bitmap;
    try {
      const mime = flags === 2 ? "image/webp" : "image/jpeg";
      const blob = new Blob([buffer.slice(HEADER_SIZE)], { type: mime });
      bitmap = await createImageBitmap(blob);
    } catch (error) {
      console.warn("frame decode failed", error);
      requestNext(300);
      return;
    }

    if (ws !== state.ws || state.phase !== "connected" || monitor !== state.monitorId) {
      bitmap.close();
      return;
    }

    try {
      if (!ctx) ctx = dom.canvas.getContext("2d", { alpha: false, desynchronized: true });
      if (dom.canvas.width !== width || dom.canvas.height !== height) {
        dom.canvas.width = width;
        dom.canvas.height = height;
        state.frameW = width;
        state.frameH = height;
        fitCanvas();
      }
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(bitmap, 0, 0, width, height);
      state.frameW = width;
      state.frameH = height;
      if (state.effectiveWidth !== width) {
        state.effectiveWidth = width;
        updateStreamStatus();
        fitCanvas();
      }
      if (!state.hasFrame) {
        state.hasFrame = true;
        dom.loadingOverlay.classList.add("is-hidden");
        if (!state.interacted) {
          dom.stageHint.classList.remove("is-hidden");
          clearTimeout(state.hintTimer);
          state.hintTimer = setTimeout(() => dom.stageHint.classList.add("is-hidden"), 5000);
        }
        showChrome();
      }
      state.stats.frames += 1;
      state.stats.bytes += buffer.byteLength;
      state.lastFrameAt = performance.now();
    } finally {
      bitmap.close();
    }
  }

  function clearScreen() {
    const canvas = dom.canvas;
    canvas.width = 1;
    canvas.height = 1;
    canvas.style.width = "1px";
    canvas.style.height = "1px";
    ctx = canvas.getContext("2d", { alpha: false });
    if (ctx) {
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, 1, 1);
    }
  }

  function currentViewScale(width = state.frameW, height = state.frameH) {
    if (!width || !height) return 1;
    if (state.zoom === "fit") {
      const stageW = dom.stage.clientWidth;
      const stageH = dom.stage.clientHeight;
      if (!stageW || !stageH) return 1;
      // "Вписать" should fill the stage in both directions.  Capping at 1
      // made a lower encoded resolution look tiny after changing it.
      return Math.min(stageW / width, stageH / height);
    }
    return clampNumber(Number(state.zoom) || 1, 0.5, 2);
  }

  function setPanMode(open, announce = false) {
    const wanted = Boolean(open) && canPanNow();
    state.panMode = wanted;
    dom.stage.classList.toggle("pan-mode", wanted);
    dom.panBtn.classList.toggle("active", wanted);
    if (!wanted) {
      state.panning = false;
      dom.stage.classList.remove("panning");
    }
    if (announce) {
      showToast(
        wanted
          ? "Режим перемещения включён. Тащите экран левой кнопкой."
          : "Режим перемещения выключен. Мышь снова управляет ПК.",
      );
    }
    if (state.authed) {
      dom.statusText.textContent = wanted
        ? `Перемещение · ${state.host}`
        : `Подключено · ${state.host}`;
    }
    if (wanted) {
      dom.stageHint.classList.add("is-hidden");
      dom.canvas.focus({ preventScroll: true });
    }
  }

  function canPanNow() {
    if (!state.frameW || !state.frameH) return false;
    const scale = currentViewScale();
    return (
      state.frameW * scale > dom.stage.clientWidth + 2 ||
      state.frameH * scale > dom.stage.clientHeight + 2
    );
  }

  function updatePanButton() {
    const canPan = canPanNow();
    dom.panBtn.disabled = !canPan;
    if (!canPan && state.panMode) setPanMode(false);
  }

  function fitCanvas() {
    if (!state.hasFrame && (!state.frameW || !state.frameH)) {
      dom.canvas.style.width = "1px";
      dom.canvas.style.height = "1px";
      dom.canvas.style.transform = "translate(-50%, -50%)";
      dom.panBtn.disabled = true;
      return;
    }
    const width = state.frameW;
    const height = state.frameH;
    if (!width || !height) return;
    const stageW = dom.stage.clientWidth;
    const stageH = dom.stage.clientHeight;
    if (!stageW || !stageH) return;

    const scale = currentViewScale(width, height);
    const drawW = Math.max(1, Math.round(width * scale));
    const drawH = Math.max(1, Math.round(height * scale));
    const maxPanX = Math.max(0, (drawW - stageW) / 2);
    const maxPanY = Math.max(0, (drawH - stageH) / 2);
    state.panX = clampNumber(state.panX, -maxPanX, maxPanX);
    state.panY = clampNumber(state.panY, -maxPanY, maxPanY);

    dom.canvas.style.width = `${drawW}px`;
    dom.canvas.style.height = `${drawH}px`;
    dom.canvas.style.transform =
      `translate(-50%, -50%) translate(${state.panX}px, ${state.panY}px)`;
    updatePanButton();
  }

  function setZoom(value) {
    state.zoom = ["fit", "1", "1.5", "2"].includes(String(value)) ? String(value) : "fit";
    state.panX = 0;
    state.panY = 0;
    setPanMode(false);
    saveZoom();
    updateZoomControls();
    fitCanvas();
    showChrome();
    if (state.zoom !== "fit") {
      showToast(
        `Масштаб ${state.zoom === "1" ? "100" : state.zoom === "1.5" ? "150" : "200"}%. ` +
          "Для перемещения включите «руку» в панели.",
      );
    }
  }

  function updateStats() {
    const now = performance.now();
    const elapsed = Math.max(0.001, (now - state.stats.last) / 1000);
    const fps = state.stats.frames / elapsed;
    const rate = state.stats.bytes / elapsed;
    let text = "ожидание кадра";
    if (state.frameW && state.frameH) {
      const idle = performance.now() - state.lastFrameAt > 900 && fps < 0.5;
      text = idle
        ? `${state.frameW}×${state.frameH} · статично`
        : `${state.frameW}×${state.frameH} · ${Math.round(fps)} fps · ${formatRate(rate)}`;
      if (state.rtt > 0 && state.rtt < 5000) text += ` · ${Math.round(state.rtt)} мс`;
    }
    dom.stats.textContent = text;
    state.stats.frames = 0;
    state.stats.bytes = 0;
    state.stats.last = now;
  }

  // ------------------------------------------------------------------
  // Mouse
  // ------------------------------------------------------------------

  function pointFromEvent(event, clamp = false) {
    const rect = dom.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    let x = (event.clientX - rect.left) / rect.width;
    let y = (event.clientY - rect.top) / rect.height;
    if (clamp) {
      x = Math.min(1, Math.max(0, x));
      y = Math.min(1, Math.max(0, y));
    } else if (x < -0.01 || x > 1.01 || y < -0.01 || y > 1.01) {
      return null;
    }
    x = Math.min(1, Math.max(0, x));
    y = Math.min(1, Math.max(0, y));
    state.lastPoint = { x, y };
    return { x, y };
  }

  function sendMouse(action, point, button) {
    if (!state.authed || !wsOpen()) return;
    state.dirty = true;
    const message = { type: "mouse", action };
    if (point) {
      message.x = Math.round(point.x * 100000) / 100000;
      message.y = Math.round(point.y * 100000) / 100000;
    }
    if (button) message.button = button;
    send(message);
  }

  function queueMove(point) {
    state.pendingMove = point;
    state.dirty = true;
    if (state.moveRaf) return;
    state.moveRaf = requestAnimationFrame(() => {
      state.moveRaf = 0;
      const pending = state.pendingMove;
      state.pendingMove = null;
      if (pending) sendMouse("move", pending);
    });
  }

  function buttonName(button) {
    return ["left", "middle", "right", "back", "forward"][button] || null;
  }

  function releaseButtons() {
    const point = state.lastPoint;
    for (const button of state.pressedButtons) {
      sendMouse("up", point, button);
    }
    state.pressedButtons.clear();
  }

  function releaseAll() {
    if (wsOpen()) send({ type: "release_all" });
    state.pressedButtons.clear();
    state.textKeys.clear();
    state.keyDown.clear();
  }

  function endPan() {
    state.panning = false;
    state.panStart = null;
    dom.stage.classList.remove("panning");
  }

  function onPointerDown(event) {
    if (!state.authed) return;
    event.preventDefault();
    if (state.menuOpen || state.streamOpen) {
      closeAllMenus();
      dom.canvas.focus({ preventScroll: true });
      showChrome();
      return;
    }
    dom.canvas.focus({ preventScroll: true });
    state.interacted = true;
    dom.stageHint.classList.add("is-hidden");
    showChrome();

    if (state.panMode && canPanNow()) {
      state.panning = true;
      state.panStart = {
        x: event.clientX,
        y: event.clientY,
        panX: state.panX,
        panY: state.panY,
      };
      dom.stage.classList.add("panning");
      try {
        dom.canvas.setPointerCapture(event.pointerId);
      } catch {
        // Capture is a nicety.
      }
      return;
    }
    if (state.panMode) return;

    state.dirty = true;
    const point = pointFromEvent(event);
    if (!point) return;
    try {
      dom.canvas.setPointerCapture(event.pointerId);
    } catch {
      // Capture is a nicety; the mouse still works without it.
    }
    const button = buttonName(event.button);
    if (!button) return;
    state.pressedButtons.add(button);
    sendMouse("down", point, button);
    kickFrame();
  }

  function onPointerMove(event) {
    if (!state.authed) return;
    if (event.clientY < 92) showChrome();

    if (state.panning && state.panStart) {
      state.panX = state.panStart.panX + (event.clientX - state.panStart.x);
      state.panY = state.panStart.panY + (event.clientY - state.panStart.y);
      fitCanvas();
      return;
    }
    if (state.panMode) return;

    const point = pointFromEvent(event);
    if (!point) return;
    if (event.buttons === 0 && event.target !== dom.canvas) return;
    queueMove(point);
  }

  function onPointerUp(event) {
    if (!state.authed) return;
    event.preventDefault();
    if (state.panning) {
      endPan();
      try {
        if (dom.canvas.hasPointerCapture(event.pointerId)) {
          dom.canvas.releasePointerCapture(event.pointerId);
        }
      } catch {
        // Ignore stale pointer ids.
      }
      return;
    }
    const point = pointFromEvent(event, true) || state.lastPoint;
    const button = buttonName(event.button);
    if (button && state.pressedButtons.has(button)) {
      state.pressedButtons.delete(button);
      sendMouse("up", point, button);
    }
    try {
      if (dom.canvas.hasPointerCapture(event.pointerId)) {
        dom.canvas.releasePointerCapture(event.pointerId);
      }
    } catch {
      // Ignore stale pointer ids.
    }
    showChrome();
  }

  function flushWheel() {
    state.wheelRaf = 0;
    const dx = Math.trunc(state.wheelX / 100);
    const dy = Math.trunc(state.wheelY / 100);
    state.wheelX -= dx * 100;
    state.wheelY -= dy * 100;
    if (dx || dy) {
      state.dirty = true;
      send({ type: "mouse", action: "wheel", dx, dy });
      kickFrame();
    }
  }

  function onWheel(event) {
    if (!state.authed) return;
    event.preventDefault();
    const factor = event.deltaMode === 1 ? 100 : event.deltaMode === 2 ? 800 : 1;
    state.wheelX += event.deltaX * factor;
    // WheelEvent.deltaY is positive when the page scrolls down, while
    // MOUSEEVENTF_WHEEL treats positive as "wheel up".  Invert Y only.
    state.wheelY -= event.deltaY * factor;
    if (!state.wheelRaf) state.wheelRaf = requestAnimationFrame(flushWheel);
  }

  // ------------------------------------------------------------------
  // Keyboard
  // ------------------------------------------------------------------

  function keyboardTargetIgnored(event) {
    const target = event.target;
    if (!(target instanceof Element)) return false;
    return !!target.closest("input, textarea, select, button, a, [contenteditable='true']");
  }

  function sendKeyEvent(event, code, down) {
    state.dirty = true;
    send({
      type: "key",
      code,
      key: event.key,
      down,
      ctrl: event.ctrlKey,
      alt: event.altKey,
      shift: event.shiftKey,
      meta: event.metaKey,
    });
  }

  function onKeyDown(event) {
    if (!state.authed) return;
    if (event.key === "Escape" && (state.menuOpen || state.streamOpen)) {
      event.preventDefault();
      openMenu(false);
      openStreamMenu(false);
      return;
    }
    const altGraph =
      typeof event.getModifierState === "function" &&
      event.getModifierState("AltGraph");
    // Local shortcut: Ctrl+Alt+M = next monitor, +Shift = previous.
    // It must not be sent to the remote PC as a key press.
    if (
      event.code === "KeyM" &&
      event.ctrlKey &&
      event.altKey &&
      !event.metaKey &&
      !altGraph
    ) {
      event.preventDefault();
      event.stopPropagation();
      state.localKeys.add(event.code);
      if (!event.repeat) cycleMonitor(event.shiftKey ? -1 : 1);
      return;
    }
    if (keyboardTargetIgnored(event) || event.isComposing) return;
    const code = event.code;
    if (!code) return;
    const printable =
      typeof event.key === "string" &&
      Array.from(event.key).length === 1 &&
      ((!event.ctrlKey && !event.altKey && !event.metaKey) || altGraph);

    event.preventDefault();
    event.stopPropagation();
    if (printable) {
      state.dirty = true;
      send({ type: "text", text: event.key });
      state.textKeys.add(code);
      kickFrame();
      return;
    }
    state.keyDown.add(code);
    sendKeyEvent(event, code, true);
    kickFrame();
  }

  function onKeyUp(event) {
    if (!state.authed) return;
    const code = event.code;
    if (!code) return;
    if (state.localKeys.delete(code)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const textKey = state.textKeys.delete(code);
    const wasDown = state.keyDown.delete(code);
    if (!textKey && !wasDown && keyboardTargetIgnored(event)) return;
    event.preventDefault();
    event.stopPropagation();
    if (textKey) return;
    sendKeyEvent(event, code, false);
  }

  // ------------------------------------------------------------------
  // Toolbar / fullscreen / chrome
  // ------------------------------------------------------------------

  function readToolbarCollapsed() {
    try {
      return localStorage.getItem(TOOLBAR_KEY) === "1";
    } catch {
      return false;
    }
  }

  function saveToolbarCollapsed(collapsed) {
    try {
      localStorage.setItem(TOOLBAR_KEY, collapsed ? "1" : "0");
    } catch {
      // Storage is optional.
    }
  }

  function setToolbarCollapsed(collapsed, persist = true) {
    if (!state.authed) return;
    state.toolbarCollapsed = Boolean(collapsed);
    clearTimeout(state.chromeTimer);
    dom.toolbar.classList.remove("is-hidden");
    if (state.toolbarCollapsed) {
      closeAllMenus();
      dom.toolbar.hidden = true;
      dom.toolbarMini.hidden = false;
      dom.cycleMini.hidden = state.monitors.length < 2;
      dom.cycleMini.disabled = state.monitors.length < 2;
      dom.statusPill.classList.add("is-hidden");
    } else {
      dom.toolbar.hidden = false;
      dom.toolbarMini.hidden = true;
      dom.cycleMini.hidden = true;
      dom.statusPill.classList.remove("is-hidden");
    }
    if (persist) saveToolbarCollapsed(state.toolbarCollapsed);
  }

  function showChrome() {
    if (!state.authed) return;
    clearTimeout(state.chromeTimer);
    if (state.toolbarCollapsed) {
      dom.toolbar.hidden = true;
      dom.toolbarMini.hidden = false;
      dom.cycleMini.hidden = state.monitors.length < 2;
      dom.cycleMini.disabled = state.monitors.length < 2;
      dom.statusPill.classList.add("is-hidden");
      return;
    }
    dom.toolbar.hidden = false;
    dom.toolbar.classList.remove("is-hidden");
    dom.statusPill.classList.remove("is-hidden");
    if (state.menuOpen || state.streamOpen) return;
    state.chromeTimer = setTimeout(() => {
      if (state.menuOpen || state.streamOpen) return;
      setToolbarCollapsed(true);
    }, 3000);
  }

  function openMenu(open) {
    state.menuOpen = Boolean(open);
    if (state.menuOpen) openStreamMenu(false);
    dom.keysMenu.hidden = !state.menuOpen;
    dom.keysBtn.setAttribute("aria-expanded", String(state.menuOpen));
    dom.keysBtn.classList.toggle("active", state.menuOpen);
    showChrome();
    if (!state.menuOpen && !state.streamOpen) dom.canvas.focus({ preventScroll: true });
  }

  function openStreamMenu(open) {
    state.streamOpen = Boolean(open);
    if (state.streamOpen) {
      openMenu(false);
      dom.stageHint.classList.add("is-hidden");
    }
    dom.streamMenu.hidden = !state.streamOpen;
    dom.streamBtn.setAttribute("aria-expanded", String(state.streamOpen));
    dom.streamBtn.classList.toggle("active", state.streamOpen);
    showChrome();
    if (!state.streamOpen && !state.menuOpen) dom.canvas.focus({ preventScroll: true });
  }

  function closeAllMenus() {
    state.menuOpen = false;
    state.streamOpen = false;
    dom.keysMenu.hidden = true;
    dom.streamMenu.hidden = true;
    dom.keysBtn.setAttribute("aria-expanded", "false");
    dom.streamBtn.setAttribute("aria-expanded", "false");
    dom.keysBtn.classList.remove("active");
    dom.streamBtn.classList.remove("active");
  }

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen({ navigationUI: "hide" });
      }
    } catch (error) {
      console.warn(error);
      showToast("Браузер не разрешил полноэкранный режим.");
    }
    requestAnimationFrame(() => {
      fitCanvas();
      showChrome();
      dom.canvas.focus({ preventScroll: true });
    });
  }

  function updateFullscreenButton() {
    const active = Boolean(document.fullscreenElement);
    dom.fullscreenBtn.classList.toggle("active", active);
    dom.fullscreenBtn.title = active ? "Выйти из полноэкранного режима" : "На весь экран";
    dom.fullscreenBtn.setAttribute(
      "aria-label",
      active ? "Выйти из полноэкранного режима" : "На весь экран",
    );
  }

  // ------------------------------------------------------------------
  // Wiring
  // ------------------------------------------------------------------

  initLogin();
  dom.canvas.getContext("2d", { alpha: false });
  clearScreen();

  dom.monitorSelect.addEventListener("change", () => {
    const id = Number(dom.monitorSelect.value);
    if (!Number.isInteger(id)) return;
    sendView(id);
    dom.canvas.focus({ preventScroll: true });
  });

  function cycleMonitor(direction = 1) {
    if (!state.authed || state.monitors.length < 2) return;
    const index = state.monitors.findIndex(
      (item) => Number(item.id) === Number(state.monitorId),
    );
    const next =
      state.monitors[
        (index + direction + state.monitors.length) % state.monitors.length
      ];
    if (!next) return;
    dom.monitorSelect.value = String(next.id);
    sendView(next.id);
    showToast(`Монитор ${next.id} · ${next.width}×${next.height}`, 1500);
    showChrome();
    dom.canvas.focus({ preventScroll: true });
  }

  dom.cycleBtn.addEventListener("click", () => cycleMonitor(1));
  dom.cycleMini.addEventListener("click", () => cycleMonitor(1));

  dom.canvas.addEventListener("pointerdown", onPointerDown);
  dom.canvas.addEventListener("pointermove", onPointerMove);
  dom.canvas.addEventListener("pointerup", onPointerUp);
  dom.canvas.addEventListener("pointercancel", () => {
    endPan();
    releaseButtons();
    dom.stageHint.classList.add("is-hidden");
  });
  dom.canvas.addEventListener("lostpointercapture", () => {
    endPan();
    releaseButtons();
  });
  window.addEventListener("pointerup", () => {
    endPan();
    if (state.authed && state.pressedButtons.size) releaseButtons();
  });
  dom.canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  dom.canvas.addEventListener("auxclick", (event) => event.preventDefault());
  dom.canvas.addEventListener("dragstart", (event) => event.preventDefault());
  dom.canvas.addEventListener("wheel", onWheel, { passive: false });

  dom.stage.addEventListener("pointermove", (event) => {
    if (event.clientY < 92) showChrome();
  });
  dom.stage.addEventListener("pointerdown", showChrome);

  dom.toolbar.addEventListener("pointerenter", showChrome);
  dom.toolbar.addEventListener("pointermove", showChrome);

  window.addEventListener("keydown", onKeyDown, { capture: true });
  window.addEventListener("keyup", onKeyUp, { capture: true });
  window.addEventListener("blur", () => {
    if (state.authed) releaseAll();
  });
  window.addEventListener("resize", fitCanvas);
  if (window.ResizeObserver) new ResizeObserver(fitCanvas).observe(dom.stage);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      releaseAll();
      clearTimeout(state.nextTimer);
    } else if (state.authed) {
      requestNext();
      send({ type: "ping", t: performance.now() });
    }
  });

  dom.keysBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    openMenu(!state.menuOpen);
  });
  dom.keysMenu.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("[data-shortcut]") : null;
    if (!button) return;
    const shortcut = button.getAttribute("data-shortcut");
    if (shortcut) {
      state.dirty = true;
      send({ type: "shortcut", name: shortcut });
      kickFrame();
    }
    openMenu(false);
  });

  dom.streamBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    openStreamMenu(!state.streamOpen);
  });

  dom.codecSelect.addEventListener("change", () => {
    const codec = dom.codecSelect.value;
    if (codec === "h264" && typeof window.VideoDecoder !== "function") {
      dom.codecSelect.value = "jpeg";
      showToast("Этот браузер не поддерживает H.264 WebCodecs.", 7000);
      return;
    }
    state.stream.codec = codec === "h264" ? "h264" : codec === "webp" ? "webp" : "jpeg";
    closeH264Decoder();
    sendStream();
  });

  dom.qualitySelect.addEventListener("change", () => {
    if (dom.qualitySelect.value === "auto") {
      state.stream.quality_mode = "auto";
      state.stream.quality = 85;
    } else {
      state.stream.quality_mode = "fixed";
      state.stream.quality = Number(dom.qualitySelect.value) || 85;
    }
    sendStream();
  });

  dom.fpsSelect.addEventListener("change", () => {
    state.stream.fps = Number(dom.fpsSelect.value) || 60;
    sendStream();
  });

  dom.widthSelect.addEventListener("change", () => {
    state.stream.width = dom.widthSelect.value === "native" ? 0 : Number(dom.widthSelect.value);
    state.effectiveWidth = 0;
    sendStream();
  });

  dom.zoomSelect.addEventListener("change", () => {
    setZoom(dom.zoomSelect.value);
    dom.canvas.focus({ preventScroll: true });
  });

  dom.panBtn.addEventListener("click", () => {
    setPanMode(!state.panMode, true);
  });

  document.addEventListener("pointerdown", (event) => {
    if (!state.menuOpen && !state.streamOpen) return;
    if (event.target instanceof Element && event.target.closest(".menu-wrap")) return;
    openMenu(false);
    openStreamMenu(false);
  });

  try {
    state.soundMuted = localStorage.getItem(SOUND_KEY) === "1";
  } catch {
    state.soundMuted = false;
  }
  updateSoundButton();

  document.addEventListener("pointerdown", ensureAudio, true);
  document.addEventListener("keydown", ensureAudio, true);

  dom.soundBtn.addEventListener("click", () => {
    state.soundMuted = !state.soundMuted;
    try {
      localStorage.setItem(SOUND_KEY, state.soundMuted ? "1" : "0");
    } catch {}
    if (state.audioGain && state.audioCtx) {
      state.audioGain.gain.value = state.soundMuted ? 0 : 1;
    }
    updateSoundButton();
    if (!state.soundMuted) ensureAudio();
  });

  dom.fullscreenBtn.addEventListener("click", toggleFullscreen);
  dom.collapseBtn.addEventListener("click", () => {
    setToolbarCollapsed(true);
    dom.toolbarMini.focus({ preventScroll: true });
  });
  dom.toolbarMini.addEventListener("click", () => {
    setToolbarCollapsed(false);
    showChrome();
    dom.streamBtn.focus({ preventScroll: true });
  });
  dom.disconnectBtn.addEventListener("click", disconnect);
  dom.lostBtn.addEventListener("click", resetToLogin);
  document.addEventListener("fullscreenchange", () => {
    updateFullscreenButton();
    fitCanvas();
    showChrome();
  });

  window.addEventListener("beforeunload", () => {
    if (wsOpen()) {
      try {
        send({ type: "release_all" });
        state.ws.close(1000, "page unload");
      } catch {
        // Ignore.
      }
    }
  });

  updateFullscreenButton();
})();

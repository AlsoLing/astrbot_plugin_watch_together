/* 一起观影 · 前端
 * 纯原生 JS：片库 → 建会话 → 播放 → 心跳/轮询 → 弹幕式 AI 点评。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    aiDot: $("aiDot"),
    aiState: $("aiState"),
    brandSub: $("brandSub"),
    btnSettings: $("btnSettings"),
    btnRefresh: $("btnRefresh"),
    search: $("search"),
    libList: $("libList"),
    btnLocal: $("btnLocal"),
    btnExternal: $("btnExternal"),
    localFile: $("localFile"),
    localSub: $("localSub"),
    screen: $("screen"),
    video: $("video"),
    placeholder: $("placeholder"),
    subtitleLayer: $("subtitleLayer"),
    thinking: $("thinking"),
    remuxBar: $("remuxBar"),
    remuxPlay: $("remuxPlay"),
    remuxClock: $("remuxClock"),
    remuxJump: $("remuxJump"),
    remuxJumpGo: $("remuxJumpGo"),
    remuxFull: $("remuxFull"),
    externalPanel: $("externalPanel"),
    extClock: $("extClock"),
    extMovie: $("extMovie"),
    extStart: $("extStart"),
    extPause: $("extPause"),
    extMinus: $("extMinus"),
    extPlus: $("extPlus"),
    nowTitle: $("nowTitle"),
    nowMeta: $("nowMeta"),
    btnEnd: $("btnEnd"),
    btnSubtitle: $("btnSubtitle"),
    noteList: $("noteList"),
    noteCount: $("noteCount"),
    chatForm: $("chatForm"),
    chatInput: $("chatInput"),
    settingsModal: $("settingsModal"),
    btnCloseSettings: $("btnCloseSettings"),
    intervalRange: $("intervalRange"),
    intervalValue: $("intervalValue"),
    visionToggle: $("visionToggle"),
    btnSaveSettings: $("btnSaveSettings"),
    toasts: $("toasts"),
  };

  const state = {
    meta: null,
    library: [],
    activeMovieId: "",
    session: null,
    lastId: 0,
    playback: "none",
    baseOffset: 0,
    settings: { interval: 90, vision: true },
    defaults: { interval: 90, vision: true },
    lastFrameAt: 0,
    tickTimer: null,
    pollTimer: null,
    clockTimer: null,
    localUrl: "",
    ext: { running: false, base: 0, startedAt: 0 },
    danmakuQueue: [],
    danmakuTimer: null,
    currentDanmaku: null,
    priming: false,
  };

  const canvas = document.createElement("canvas");
  const canvasCtx = canvas.getContext("2d");

  // ------------------------------------------------------------------ 工具
  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function fmt(seconds, withHours = false) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return withHours || h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function parseClock(text) {
    const raw = String(text || "").trim();
    if (!raw) return null;
    const parts = raw.split(":").map((part) => Number(part));
    if (!parts.length || parts.some((n) => !Number.isFinite(n) || n < 0)) return null;
    let seconds = 0;
    for (const part of parts) seconds = seconds * 60 + part;
    return seconds;
  }

  function toast(message, isError = false, timeout = 4200) {
    if (!message) return;
    const node = document.createElement("div");
    node.className = "toast" + (isError ? " error" : "");
    node.textContent = message;
    el.toasts.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity .3s ease";
      setTimeout(() => node.remove(), 320);
    }, timeout);
  }

  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }
    if (!response.ok) {
      const message = data && data.error ? data.error : `请求失败（${response.status}）`;
      throw new Error(message);
    }
    return data;
  }

  // ------------------------------------------------------------------ 片库
  async function loadLibrary(force = false) {
    try {
      const data = await api(`/api/library${force ? "?force=1" : ""}`);
      state.library = data.items || [];
      renderLibrary();
      if (data.errors && data.errors.length) {
        toast(data.errors.join("；"), true, 7000);
      }
    } catch (err) {
      el.libList.innerHTML = `<div class="empty">片库读取失败：${escapeHtml(err.message)}</div>`;
    }
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderLibrary() {
    const keyword = (el.search.value || "").trim().toLowerCase();
    const items = state.library.filter((item) => {
      if (!keyword) return true;
      return (
        (item.title || "").toLowerCase().includes(keyword) ||
        (item.folder || "").toLowerCase().includes(keyword)
      );
    });

    if (!items.length) {
      el.libList.innerHTML = `<div class="empty">${
        state.library.length
          ? "没有匹配的片子"
          : "片库是空的。请在插件配置 media_dirs 里填写 NAS 上的电影目录"
      }</div>`;
      return;
    }

    const frag = document.createDocumentFragment();
    for (const item of items) {
      const card = document.createElement("div");
      card.className = "movie" + (item.id === state.activeMovieId ? " active" : "");
      card.dataset.id = item.id;
      const chips = [
        `<span class="chip">${escapeHtml(item.format || "")}</span>`,
        `<span class="chip">${escapeHtml(item.size_h || "")}</span>`,
        item.has_sub
          ? `<span class="chip good">字幕</span>`
          : `<span class="chip warn">无字幕</span>`,
      ];
      if (item.year) chips.push(`<span class="chip">${escapeHtml(item.year)}</span>`);
      card.innerHTML = `
        <div class="movie-title">${escapeHtml(item.title || "未命名")}</div>
        <div class="movie-meta">${chips.join("")}</div>`;
      card.addEventListener("click", () => startNas(item));
      frag.appendChild(card);
    }
    el.libList.innerHTML = "";
    el.libList.appendChild(frag);
  }

  // ------------------------------------------------------------------ 会话
  async function startNas(item) {
    try {
      const info = await api("/api/session", {
        method: "POST",
        body: {
          source: "nas",
          movie_id: item.id,
          settings: { interval: state.settings.interval, vision: state.settings.vision },
        },
      });
      state.activeMovieId = item.id;
      renderLibrary();
      await attach(info, { autoplay: true });
      if (info.playback === "unsupported") {
        toast(
          "这个容器浏览器不能直接播放。可以在插件配置里把 ffmpeg_mode 设为 remux/transcode，或先把片子转成 mp4。",
          true,
          8000
        );
      }
    } catch (err) {
      toast(`开始播放失败：${err.message}`, true);
    }
  }

  async function startLocal(file) {
    try {
      const title = file.name.replace(/\.[^.]+$/, "");
      const info = await api("/api/session", {
        method: "POST",
        body: {
          source: "local",
          title,
          settings: { interval: state.settings.interval, vision: state.settings.vision },
        },
      });
      if (state.localUrl) URL.revokeObjectURL(state.localUrl);
      state.localUrl = URL.createObjectURL(file);
      await attach(info, { src: state.localUrl, autoplay: true });
      el.btnSubtitle.hidden = false;
      toast("本地文件模式：如果 AI 对剧情没什么反应，点「加载字幕」把字幕给它。", false, 7000);
    } catch (err) {
      toast(`打开本地文件失败：${err.message}`, true);
    }
  }

  async function startExternal() {
    await prepareExternal();
  }

  /** 外部播放器：先选参考片源（用于读字幕），点开始再真正建会话。 */
  async function prepareExternal() {
    if (state.session) {
      await endSession();
    }
    stopTimers();
    resetMedia();
    state.ext = { running: false, base: 0, startedAt: 0 };
    el.nowTitle.textContent = "外部播放器模式";
    el.nowMeta.textContent = "选一部参考片源，AI 靠它的字幕陪你聊";
    el.btnEnd.hidden = true;
    el.btnSubtitle.hidden = true;
    el.placeholder.hidden = true;
    el.screen.hidden = true;
    el.externalPanel.hidden = false;
    el.extClock.textContent = "00:00";
    populateExtMovies("");
  }

  function populateExtMovies(selectedId) {
    if (!el.extMovie) return;
    const current = selectedId || el.extMovie.value || "";
    el.extMovie.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "不选（没有字幕，AI 只能陪你聊天）";
    el.extMovie.appendChild(none);
    for (const item of state.library) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.title}${item.has_sub ? "（有字幕）" : ""}`;
      el.extMovie.appendChild(option);
    }
    el.extMovie.value = current || "";
  }

  async function beginExternalSync() {
    if (!state.session || state.session.source !== "external") {
      const movieId = el.extMovie ? el.extMovie.value : "";
      const movie = state.library.find((item) => item.id === movieId);
      try {
        const info = await api("/api/session", {
          method: "POST",
          body: {
            source: "external",
            movie_id: movieId || "",
            title: movie ? movie.title : "外部播放器",
            settings: { interval: state.settings.interval, vision: state.settings.vision },
          },
        });
        await attach(info);
      } catch (err) {
        toast(`创建观影会话失败：${err.message}`, true);
        return;
      }
    }
    if (!state.ext.running) {
      state.ext.running = true;
      state.ext.startedAt = performance.now();
    }
    updateExtClock();
  }

  async function resumeSession(sessionId) {
    try {
      const info = await api(`/api/session/${sessionId}`);
      if (info.ended) {
        toast("这场观影已经结束了，重新挑一部吧。");
        return;
      }
      if (info.source === "local") {
        toast("本地文件无法在刷新后恢复，请重新选择文件。");
        return;
      }
      await attach(info, { autoplay: false });
    } catch (err) {
      toast(`恢复观影会话失败：${err.message}`, true);
    }
  }

  async function attach(info, options = {}) {
    if (state.session && state.session.id !== info.id) {
      api(`/api/session/${state.session.id}/end`, { method: "POST" }).catch(() => {});
    }
    stopTimers();
    resetMedia();

    state.session = info;
    state.lastId = 0;
    state.baseOffset = 0;
    state.playback = info.playback || "none";
    state.settings.interval = Number(info.settings?.interval || state.defaults.interval);
    state.settings.vision = Boolean(info.settings?.vision ?? state.defaults.vision);
    state.ext = { running: false, base: 0, startedAt: 0 };

    el.noteList.innerHTML = "";
    el.noteCount.textContent = "0";
    el.nowTitle.textContent = info.title || "未命名影片";
    el.nowMeta.textContent = metaLine(info);
    el.btnEnd.hidden = false;
    el.btnSubtitle.hidden = !(info.source === "local" || !info.has_sub);
    el.placeholder.hidden = true;
    el.externalPanel.hidden = true;
    el.remuxBar.hidden = true;
    el.screen.hidden = false;
    setAiState("观影中", "live");

    if (info.source === "external") {
      showExternal(info.movie_id);
    } else if (options.src) {
      el.video.src = options.src;
      mountVideo(info);
      maybeAutoplay(options.autoplay);
    } else if (info.playback === "direct" || info.playback === "remux") {
      const suffix = info.playback === "remux" ? "?t=0" : "";
      el.video.src = `/api/stream/${info.id}${suffix}`;
      mountVideo(info);
      maybeAutoplay(options.autoplay);
    } else {
      showUnsupported();
    }

    await primeHistory();
    startTimers();
  }

  function mountVideo(info) {
    el.video.style.display = "";
    el.placeholder.hidden = true;
    el.externalPanel.hidden = true;
    el.screen.hidden = false;
    el.video.controls = info.playback === "direct" || info.source === "local";
    el.remuxBar.hidden = info.playback !== "remux";
    el.remuxPlay.textContent = "▶";
    if (info.has_sub) setupTrack(0);
  }

  function setupTrack(offset) {
    const session = state.session;
    if (!session) return;
    for (const track of Array.from(el.video.querySelectorAll("track"))) track.remove();
    if (!session.has_sub) return;
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = "字幕";
    track.srclang = "zh";
    track.default = true;
    track.src = `/api/session/${session.id}/subtitles.vtt?offset=${offset || 0}&v=${Date.now()}`;
    track.addEventListener("load", () => {
      const list = el.video.textTracks;
      if (list && list.length) list[0].mode = "showing";
    });
    el.video.appendChild(track);
    try {
      const list = el.video.textTracks;
      if (list.length) list[0].mode = "showing";
    } catch (err) {
      /* 轨道还没加载好，load 事件里再设一次 */
    }
  }

  function maybeAutoplay(enabled) {
    if (!enabled) return;
    const promise = el.video.play();
    if (promise && promise.catch) {
      promise.catch(() => toast("浏览器拦截了自动播放，点一下播放按钮就好。"));
    }
  }

  function showUnsupported() {
    el.screen.hidden = false;
    el.placeholder.hidden = false;
    el.placeholder.querySelector("h3").textContent = "这个格式浏览器放不了";
    el.placeholder.querySelector("p").textContent =
      "MKV/AVI 这类容器需要 ffmpeg 转封装。打开插件配置，把 ffmpeg_mode 改成 remux（NAS 装了 ffmpeg），或先把片子转成 mp4。";
    el.placeholder.querySelector(".ph-tip").textContent = "也可以点左下角「用外部播放器」，用 PotPlayer 看，网页只负责同步进度。";
  }

  function showExternal(movieId) {
    el.screen.hidden = true;
    el.externalPanel.hidden = false;
    el.remuxBar.hidden = true;
    el.btnSubtitle.hidden = true;
    populateExtMovies(movieId || "");
    updateExtClock();
  }

  function resetMedia() {
    try {
      el.video.pause();
    } catch (err) {
      /* ignore */
    }
    for (const track of Array.from(el.video.querySelectorAll("track"))) track.remove();
    el.video.removeAttribute("src");
    el.video.load();
    el.video.style.display = "";
    el.remuxBar.hidden = true;
    el.subtitleLayer.innerHTML = "";
    el.thinking.hidden = true;
    if (state.danmakuTimer) clearTimeout(state.danmakuTimer);
    state.danmakuTimer = null;
    state.currentDanmaku = null;
    state.danmakuQueue = [];
  }

  function metaLine(info) {
    const bits = [];
    bits.push(
      info.source === "nas"
        ? "NAS 片源"
        : info.source === "local"
          ? "本地文件"
          : "外部播放器"
    );
    bits.push(info.has_sub ? `字幕 ${info.subtitle || "已加载"}` : "无字幕");
    if (info.playback === "remux") bits.push("ffmpeg 转封装");
    if (info.linked_chat) bits.push("已连接聊天");
    return bits.join(" · ");
  }

  // ------------------------------------------------------------------ 播放
  function getPosition() {
    if (!state.session) return 0;
    if (state.session.source === "external") {
      const extra = state.ext.running ? (performance.now() - state.ext.startedAt) / 1000 : 0;
      return state.ext.base + extra;
    }
    if (!el.video.getAttribute("src")) return 0;
    return state.baseOffset + (Number(el.video.currentTime) || 0);
  }

  function isPlaying() {
    if (!state.session) return false;
    if (state.session.source === "external") return state.ext.running;
    return Boolean(el.video.getAttribute("src")) && !el.video.paused && !el.video.ended;
  }

  function getDuration() {
    if (!state.session || state.session.source === "external") return null;
    const duration = Number(el.video.duration);
    if (!Number.isFinite(duration) || duration <= 0) return null;
    return duration;
  }

  async function seekTo(seconds) {
    if (!state.session) return;
    const target = Math.max(0, Number(seconds) || 0);
    if (state.session.source === "external") {
      state.ext.base = target;
      state.ext.startedAt = performance.now();
      updateExtClock();
      return;
    }
    if (state.playback === "remux") {
      reloadRemux(target);
      return;
    }
    if (!el.video.getAttribute("src")) return;
    try {
      el.video.currentTime = target;
    } catch (err) {
      toast("这个进度跳不过去，换个位置试试。", true);
    }
  }

  function reloadRemux(seconds) {
    if (!state.session) return;
    const target = Math.max(0, Number(seconds) || 0);
    state.baseOffset = target;
    el.video.src = `/api/stream/${state.session.id}?t=${target}`;
    el.video.load();
    if (state.session.has_sub) setupTrack(target);
    el.video.play().catch(() => {});
  }

  // ------------------------------------------------------------------ 循环
  function startTimers() {
    stopTimers();
    state.tickTimer = setInterval(tick, 2000);
    state.pollTimer = setInterval(poll, 1800);
    state.clockTimer = setInterval(updateClocks, 500);
    tick();
    poll();
  }

  function stopTimers() {
    if (state.tickTimer) clearInterval(state.tickTimer);
    if (state.pollTimer) clearInterval(state.pollTimer);
    if (state.clockTimer) clearInterval(state.clockTimer);
    state.tickTimer = state.pollTimer = state.clockTimer = null;
  }

  async function tick() {
    const session = state.session;
    if (!session) return;
    const position = getPosition();
    updateClocks();
    try {
      await api(`/api/session/${session.id}/sync`, {
        method: "POST",
        body: { position, playing: isPlaying(), duration: getDuration() },
      });
    } catch (err) {
      /* 心跳失败不打扰用户，下一轮会重试 */
    }
    maybeCaptureFrame(position);
  }

  async function poll() {
    const session = state.session;
    if (!session) return;
    try {
      const data = await api(`/api/session/${session.id}/events?after=${state.lastId}`);
      if (!data.ok) return;
      for (const comment of data.comments || []) {
        state.lastId = Math.max(state.lastId, comment.id);
        onComment(comment);
      }
      el.thinking.hidden = !data.pending;
      if (data.pending) setAiState("思考中", "thinking");
      else setAiState(data.playing ? "观影中" : "暂停中", "live");
      if (data.ended) onSessionEnded();
    } catch (err) {
      /* 等下一轮 */
    }
  }

  async function primeHistory() {
    const session = state.session;
    if (!session) return;
    try {
      const data = await api(`/api/session/${session.id}/events?after=0`);
      for (const comment of data.comments || []) {
        state.lastId = Math.max(state.lastId, comment.id);
        renderNote(comment);
      }
    } catch (err) {
      /* ignore */
    }
  }

  function updateClocks() {
    const seconds = getPosition();
    if (state.session && state.session.source === "external") {
      el.extClock.textContent = fmt(seconds, true);
    }
    el.remuxClock.textContent = fmt(seconds, true);
  }

  function updateExtClock() {
    el.extClock.textContent = fmt(getPosition(), true);
  }

  function maybeCaptureFrame(position) {
    if (!state.session || !state.settings.vision) return;
    if (state.session.source === "external") return;
    if (!isPlaying()) return;
    if (performance.now() - state.lastFrameAt < 20000) return;
    if (el.video.readyState < 2 || !el.video.videoWidth) return;
    try {
      const width = 480;
      const height = Math.max(1, Math.round((el.video.videoHeight * width) / el.video.videoWidth));
      canvas.width = width;
      canvas.height = height;
      canvasCtx.drawImage(el.video, 0, 0, width, height);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.62);
      const base64 = dataUrl.split(",")[1] || "";
      if (!base64) return;
      state.lastFrameAt = performance.now();
      fetch(`/api/session/${state.session.id}/frame?t=${position}`, {
        method: "POST",
        body: base64,
      }).catch(() => {});
    } catch (err) {
      /* 跨域或未就绪时忽略 */
    }
  }

  function setAiState(text, mode) {
    el.aiState.textContent = text;
    el.aiDot.className = "status-dot" + (mode ? ` ${mode}` : "");
  }

  // ------------------------------------------------------------------ 点评
  function onComment(comment) {
    renderNote(comment);
    if (comment.kind !== "user") {
      enqueueDanmaku(comment.text, comment.kind);
    }
    if (comment.kind === "user") {
      el.thinking.hidden = false;
    }
  }

  function renderNote(comment) {
    if (!el.noteList.querySelector(".note") && el.noteList.querySelector(".empty")) {
      el.noteList.innerHTML = "";
    }
    const node = document.createElement("div");
    node.className = `note ${comment.kind || ""}`;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "note-time";
    chip.textContent = fmt(comment.t);
    chip.title = "跳到这个时间";
    chip.addEventListener("click", () => seekTo(comment.t));
    const body = document.createElement("div");
    body.className = "note-body";
    body.textContent = comment.text;
    node.appendChild(chip);
    node.appendChild(body);
    el.noteList.appendChild(node);
    el.noteCount.textContent = String(el.noteList.querySelectorAll(".note").length);
    el.noteList.scrollTop = el.noteList.scrollHeight;
  }

  function enqueueDanmaku(text, kind) {
    state.danmakuQueue.push({ text, kind });
    if (!state.danmakuTimer) nextDanmaku();
  }

  function nextDanmaku() {
    const item = state.danmakuQueue.shift();
    if (!item) {
      state.danmakuTimer = null;
      return;
    }
    if (state.danmakuTimer) clearTimeout(state.danmakuTimer);
    const node = document.createElement("div");
    node.className = `danmaku ${item.kind || ""}`;
    node.textContent = item.text;
    el.subtitleLayer.innerHTML = "";
    el.subtitleLayer.appendChild(node);
    state.currentDanmaku = node;
    requestAnimationFrame(() => node.classList.add("show"));
    state.danmakuTimer = setTimeout(() => {
      node.classList.remove("show");
      setTimeout(() => {
        if (node.parentNode) node.parentNode.removeChild(node);
        if (state.currentDanmaku === node) nextDanmaku();
      }, 420);
    }, 7200);
  }

  function onSessionEnded() {
    if (!state.session) return;
    stopTimers();
    el.btnEnd.hidden = true;
    setAiState("已结束", "");
    toast("这场就看完了，辛苦啦。");
  }

  async function endSession() {
    if (!state.session) return;
    const sessionId = state.session.id;
    state.session = null;
    stopTimers();
    resetMedia();
    try {
      await api(`/api/session/${sessionId}/end`, { method: "POST" });
    } catch (err) {
      /* ignore */
    }
    el.nowTitle.textContent = "还没有开始";
    el.nowMeta.textContent = "—";
    el.btnEnd.hidden = true;
    el.btnSubtitle.hidden = true;
    el.externalPanel.hidden = true;
    el.screen.hidden = false;
    el.placeholder.hidden = false;
    el.placeholder.querySelector("h3").textContent = "挑一部片子，一起看";
    el.placeholder.querySelector("p").textContent =
      "AI 会跟着字幕看剧情，看到有意思的地方会小声说一句。";
    el.placeholder.querySelector(".ph-tip").textContent =
      "NAS 上的片源直接点播，也可以打开电脑里的文件";
    setAiState("空闲", "");
    state.activeMovieId = "";
    renderLibrary();
  }

  async function uploadSubtitle(file) {
    if (!state.session || !file) return;
    try {
      const buffer = await file.arrayBuffer();
      let text;
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
      } catch (err) {
        text = new TextDecoder("gb18030").decode(buffer);
      }
      const result = await api(`/api/session/${state.session.id}/subtitle`, {
        method: "POST",
        body: { text, name: file.name },
      });
      if (result.ok) {
        state.session.has_sub = true;
        state.session.subtitle = file.name;
        setupTrack(state.baseOffset);
        el.nowMeta.textContent = metaLine(state.session);
        toast(`字幕已加载（${result.count} 条），AI 现在能看懂台词了。`);
      } else {
        toast("字幕解析失败，换一个 srt/ass 文件试试。", true);
      }
    } catch (err) {
      toast(`字幕加载失败：${err.message}`, true);
    }
  }

  // ------------------------------------------------------------------ 设置
  function openSettings() {
    el.intervalRange.value = String(state.settings.interval);
    el.intervalValue.textContent = `${state.settings.interval} 秒`;
    el.visionToggle.checked = Boolean(state.settings.vision);
    el.settingsModal.hidden = false;
  }

  function closeSettings() {
    el.settingsModal.hidden = true;
  }

  async function saveSettings() {
    state.settings.interval = Number(el.intervalRange.value) || 90;
    state.settings.vision = Boolean(el.visionToggle.checked);
    if (state.session) {
      try {
        const result = await api(`/api/session/${state.session.id}/settings`, {
          method: "POST",
          body: { interval: state.settings.interval, vision: state.settings.vision },
        });
        if (result.settings) state.settings = { ...state.settings, ...result.settings };
        toast("设置已更新");
      } catch (err) {
        toast(`保存失败：${err.message}`, true);
      }
    } else {
      state.defaults = { ...state.settings };
      toast("已保存，下一场观影生效");
    }
    closeSettings();
  }

  // ------------------------------------------------------------------ 绑定
  function bind() {
    el.search.addEventListener("input", renderLibrary);
    el.btnRefresh.addEventListener("click", async () => {
      el.btnRefresh.textContent = "…";
      await loadLibrary(true);
      el.btnRefresh.textContent = "⟳";
    });
    el.btnLocal.addEventListener("click", () => el.localFile.click());
    el.localFile.addEventListener("change", () => {
      const file = el.localFile.files && el.localFile.files[0];
      el.localFile.value = "";
      if (file) startLocal(file);
    });
    el.localSub.addEventListener("change", () => {
      const file = el.localSub.files && el.localSub.files[0];
      el.localSub.value = "";
      if (file) uploadSubtitle(file);
    });
    el.btnSubtitle.addEventListener("click", () => el.localSub.click());
    el.btnExternal.addEventListener("click", startExternal);
    el.btnEnd.addEventListener("click", endSession);
    el.btnSettings.addEventListener("click", openSettings);
    el.btnCloseSettings.addEventListener("click", closeSettings);
    el.settingsModal.addEventListener("click", (event) => {
      if (event.target === el.settingsModal) closeSettings();
    });
    el.intervalRange.addEventListener("input", () => {
      el.intervalValue.textContent = `${el.intervalRange.value} 秒`;
    });
    el.btnSaveSettings.addEventListener("click", saveSettings);

    el.extStart.addEventListener("click", () => {
      beginExternalSync();
    });
    el.extPause.addEventListener("click", () => {
      if (!state.session) return;
      if (state.ext.running) {
        state.ext.base += (performance.now() - state.ext.startedAt) / 1000;
        state.ext.running = false;
      }
      updateExtClock();
    });
    el.extMinus.addEventListener("click", () => {
      state.ext.base = Math.max(0, state.ext.base - 10);
      state.ext.startedAt = performance.now();
      updateExtClock();
    });
    el.extPlus.addEventListener("click", () => {
      state.ext.base += 10;
      state.ext.startedAt = performance.now();
      updateExtClock();
    });

    el.remuxPlay.addEventListener("click", () => {
      if (el.video.paused) {
        el.video.play().catch(() => {});
        el.remuxPlay.textContent = "❚❚";
      } else {
        el.video.pause();
        el.remuxPlay.textContent = "▶";
      }
    });
    el.remuxJumpGo.addEventListener("click", () => {
      const seconds = parseClock(el.remuxJump.value);
      if (seconds === null) {
        toast("时间格式：12:34 或 01:02:03");
        return;
      }
      reloadRemux(seconds);
    });
    el.remuxJump.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        el.remuxJumpGo.click();
      }
    });
    el.remuxFull.addEventListener("click", () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (el.screen.requestFullscreen) el.screen.requestFullscreen();
    });

    el.video.addEventListener("play", () => {
      el.remuxPlay.textContent = "❚❚";
    });
    el.video.addEventListener("pause", () => {
      el.remuxPlay.textContent = "▶";
    });
    el.video.addEventListener("error", () => {
      if (!state.session) return;
      toast("视频加载失败：可能是容器/编码不被浏览器支持，试试开启 ffmpeg 转封装或换个片源。", true, 8000);
    });

    el.chatForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const session = state.session;
      if (!session) {
        toast("先挑一部片子吧");
        return;
      }
      const text = (el.chatInput.value || "").trim();
      if (!text) return;
      el.chatInput.value = "";
      try {
        await api(`/api/session/${session.id}/chat`, { method: "POST", body: { text } });
      } catch (err) {
        toast(`发送失败：${err.message}`, true);
      }
    });

    document.addEventListener("keydown", (event) => {
      const tag = (event.target && event.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (!state.session) return;
      if (event.code === "Space") {
        event.preventDefault();
        if (state.session.source === "external") {
          state.ext.running ? el.extPause.click() : el.extStart.click();
        } else if (el.video.paused) {
          el.video.play().catch(() => {});
        } else {
          el.video.pause();
        }
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const delta = event.key === "ArrowLeft" ? -10 : 10;
        seekTo(getPosition() + delta);
      }
    });
  }

  // ------------------------------------------------------------------ 启动
  async function init() {
    bind();
    try {
      state.meta = await api("/api/meta");
      if (state.meta) {
        state.defaults = {
          interval: Number(state.meta.defaults?.interval || 90),
          vision: Boolean(state.meta.defaults?.vision ?? true),
        };
        state.settings = { ...state.defaults };
        const botName = state.meta.bot_name || "AI";
        el.brandSub.textContent = `和 ${botName} 边看边聊`;
      }
    } catch (err) {
      /* 元信息拿不到也能用 */
    }
    await loadLibrary();

    const sessionId = new URLSearchParams(location.search).get("s");
    if (sessionId) await resumeSession(sessionId);
  }

  init();
})();
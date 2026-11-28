/* 一起观影 · WebUI 扩展功能页面
 * 通过 Dashboard 的 Bridge 调用插件后端：状态、片库、开播、刷新、媒体服务器测试。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    subtitle: $("subtitle"),
    btnRefresh: $("btnRefresh"),
    btnOpen: $("btnOpen"),
    banner: $("banner"),
    bannerText: $("bannerText"),
    bannerUrl: $("bannerUrl"),
    btnCopy: $("btnCopy"),
    cards: $("cards"),
    search: $("search"),
    countInfo: $("countInfo"),
    grid: $("grid"),
    ffmpegInfo: $("ffmpegInfo"),
    toasts: $("toasts"),
  };

  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  const state = {
    status: null,
    items: [],
    keyword: "",
    loading: false,
    timer: null,
  };

  // ------------------------------------------------------------------ 工具
  function toast(message, isError = false, timeout = 3200) {
    const node = document.createElement("div");
    node.className = "toast";
    if (isError) node.style.borderColor = "rgba(214,69,69,.5)";
    node.textContent = message;
    el.toasts.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity .25s ease";
      setTimeout(() => node.remove(), 260);
    }, timeout);
  }

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  async function apiGet(path, params) {
    if (!bridge) throw new Error("没有拿到 AstrBot 页面桥接（Bridge），请从 WebUI 插件页打开");
    return await bridge.apiGet(path, params);
  }

  async function apiPost(path, body) {
    if (!bridge) throw new Error("没有拿到 AstrBot 页面桥接（Bridge），请从 WebUI 插件页打开");
    return await bridge.apiPost(path, body || {});
  }

  function showError(message) {
    el.banner.hidden = false;
    el.banner.classList.add("error");
    el.bannerText.textContent = message;
    el.bannerUrl.textContent = "";
    el.btnCopy.hidden = true;
  }

  function showLink(url, title) {
    el.banner.hidden = false;
    el.banner.classList.remove("error");
    el.bannerText.textContent = title || "浏览器拦截了新标签页，手动打开：";
    el.bannerUrl.textContent = url;
    el.btnCopy.hidden = false;
    el.btnCopy.dataset.url = url;
  }

  // ------------------------------------------------------------------ 地址
  function playerUrl(path = "/") {
    const port = state.status?.port || 6116;
    let host = location.hostname || "127.0.0.1";
    let usePort = port;
    const base = state.status?.server?.base_url || "";
    try {
      const parsed = new URL(base);
      if (parsed.hostname && !/^(172\.|10\.88\.|127\.)/.test(parsed.hostname)) {
        host = parsed.hostname;
        usePort = parsed.port || port;
      }
    } catch (err) {
      /* 用页面自己的 host */
    }
    return `http://${host}:${usePort}${path}`;
  }

  function openRoom(path = "/") {
    const url = playerUrl(path);
    let win = null;
    try {
      win = window.open(url, "_blank", "noopener,noreferrer");
    } catch (err) {
      win = null;
    }
    if (!win) showLink(url, "浏览器拦截了新标签页，手动打开：");
    return url;
  }

  // ------------------------------------------------------------------ 状态
  async function loadStatus() {
    const data = await apiGet("status");
    state.status = data;
    renderStatus(data);
  }

  function renderStatus(data) {
    const server = data.server || {};
    const library = data.library || {};
    const ms = data.media_server || {};
    const watcher = data.watcher || {};
    const memory = data.memory || {};
    const sessions = data.sessions || [];

    el.subtitle.textContent = `v${data.version || ""} · 观影室 ${
      server.running ? "运行中" : "未启动"
    } · 片库 ${library.count || 0} 部`;
    el.ffmpegInfo.textContent = data.ffmpeg ? "ffmpeg：可用" : "ffmpeg：未启用";

    const cards = [
      {
        label: "观影室",
        value: `${server.running ? "运行中" : "未启动"}`,
        dot: server.running ? "ok" : "bad",
        note: server.running
          ? server.base_url
          : server.error === "missing_web"
            ? "缺少 web/index.html，请重新安装完整插件"
            : server.error || "端口可能被占用",
      },
      {
        label: "片库",
        value: `${library.count || 0} 部`,
        note: (library.roots || []).length
          ? `${library.with_sub || 0} 部有字幕 · ${(library.roots || []).join("、")}`
          : (library.errors || [])[0] || "未配置 media_dirs",
      },
      {
        label: "媒体服务器",
        value: ms.enabled ? `${ms.kind === "emby" ? "Emby" : "Jellyfin"} · ${ms.count || 0} 部` : "未启用",
        note: ms.enabled
          ? ms.error || ms.url || "已连接"
          : "可在插件配置里连接 Jellyfin / Emby，读片单和海报",
        error: Boolean(ms.enabled && ms.error),
        test: Boolean(ms.enabled),
      },
      {
        label: "正在看",
        value: sessions.length ? `${sessions.length} 场` : "空闲",
        note: sessions.length
          ? `${sessions[0].title} · ${fmtTime(sessions[0].position)}${
              sessions[0].playing ? " · 播放中" : " · 暂停"
            }`
          : "在下方点「一起看」即可开一场",
      },
      {
        label: "自动识别",
        value: watcher.enabled
          ? (watcher.playing || []).length
            ? `${watcher.playing.length} 个设备在播`
            : "待机"
          : "未启用",
        note: watcher.error
          ? watcher.error
          : (watcher.playing || [])[0]
            ? `${watcher.playing[0].device} · ${watcher.playing[0].title}`
            : "Jellyfin / Emby 会话或 VLC Web 界面",
        error: Boolean(watcher.error),
      },
      {
        label: "长期记忆",
        value: memory.enabled
          ? memory.livingmemory?.connected
            ? "LivingMemory 已接上"
            : `${memory.recent || 0} 条`
          : "关闭",
        note: memory.enabled
          ? memory.livingmemory?.connected
            ? `读取 ${memory.livingmemory.recalls || 0} 次 · 写入 ${memory.livingmemory.writes || 0} 次`
            : memory.livingmemory?.error
              ? `LivingMemory：${memory.livingmemory.error}`
              : "未检测到 LivingMemory，先用插件自带的本地记忆"
          : "可在插件配置里开启",
        error: Boolean(memory.livingmemory?.error),
      },
    ];

    el.cards.innerHTML = "";
    for (const card of cards) {
      const node = document.createElement("div");
      node.className = "card";
      const noteClass = card.error ? "note error" : "note";
      node.innerHTML = `
        <span class="label">${escapeHtml(card.label)}</span>
        <span class="value">${card.dot ? `<i class="dot ${card.dot}"></i>` : ""}${escapeHtml(
          card.value
        )}</span>
        <span class="${noteClass}">${escapeHtml(card.note || "")}</span>`;
      if (card.test) {
        const button = document.createElement("button");
        button.className = "btn ghost tiny test";
        button.type = "button";
        button.textContent = "测试连接";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            const result = await apiPost("test-media-server");
            if (result.ok) {
              toast(`连接成功：${result.server || "媒体服务器"} ${result.version || ""}`);
              await loadStatus();
            } else {
              toast(result.error || "连接失败", true);
            }
          } catch (err) {
            toast(err.message || "连接失败", true);
          } finally {
            button.disabled = false;
          }
        });
        node.appendChild(button);
      }
      el.cards.appendChild(node);
    }
  }

  // ------------------------------------------------------------------ 片库
  async function loadLibrary() {
    state.loading = true;
    try {
      const data = await apiGet("library", { q: state.keyword, limit: 300 });
      state.items = data.items || [];
      el.countInfo.textContent = state.keyword
        ? `${data.matched || 0} / ${data.total || 0} 部`
        : `共 ${data.total || 0} 部`;
      renderGrid(state.items);
    } catch (err) {
      el.grid.innerHTML = `<div class="empty">片库读取失败：${escapeHtml(err.message)}</div>`;
    } finally {
      state.loading = false;
    }
  }

  function renderGrid(items) {
    if (!items.length) {
      el.grid.innerHTML = `<div class="empty">${
        state.keyword ? "没有匹配的片子" : "片库是空的，先在插件配置里填 media_dirs 或连接媒体服务器"
      }</div>`;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of items) {
      const card = document.createElement("article");
      card.className = "movie";
      const badge = item.remote
        ? `<span class="badge ms">媒体服务器</span>`
        : item.has_sub
          ? `<span class="badge sub">有字幕</span>`
          : `<span class="badge">无字幕</span>`;
      const poster = item.poster
        ? `<img loading="lazy" alt="" src="${escapeHtml(item.poster)}">`
        : `<span class="ph">${escapeHtml((item.title || "?").slice(0, 1))}</span>`;
      card.innerHTML = `
        <div class="poster">${poster}${badge}</div>
        <div class="body">
          <h3>${escapeHtml(item.title || "未命名")}</h3>
          <div class="sub">${escapeHtml(
            [item.year, item.format || "", item.size_h || ""].filter(Boolean).join(" · ")
          )}</div>
        </div>
        <button class="play" type="button">一起看</button>`;

      const img = card.querySelector("img");
      if (img) {
        img.addEventListener("error", () => {
          img.replaceWith(Object.assign(document.createElement("span"), {
            className: "ph",
            textContent: (item.title || "?").slice(0, 1),
          }));
        });
      }
      const button = card.querySelector(".play");
      button.addEventListener("click", () => play(item, button));
      frag.appendChild(card);
    }
    el.grid.innerHTML = "";
    el.grid.appendChild(frag);
  }

  async function play(item, button) {
    if (button) {
      button.disabled = true;
      button.textContent = "准备中…";
    }
    try {
      const result = await apiPost("play", { movie_id: item.id });
      if (!result.ok) throw new Error(result.error || "创建观影失败");
      const url = openRoom(result.path || "/");
      if (result.playback === "unsupported") {
        toast("这个格式浏览器放不了，可在配置里开 ffmpeg 或用外部播放器", true, 5000);
      } else if (result.playback === "remote") {
        toast("由媒体服务器直接串流播放", false, 3000);
      } else {
        toast(`已准备好《${result.title}》`);
      }
      await loadStatus();
    } catch (err) {
      toast(err.message || "创建观影失败", true);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "一起看";
      }
    }
  }

  // ------------------------------------------------------------------ 事件
  function bind() {
    el.btnOpen.addEventListener("click", () => openRoom("/"));

    el.btnRefresh.addEventListener("click", async () => {
      el.btnRefresh.disabled = true;
      el.btnRefresh.textContent = "刷新中…";
      try {
        await apiPost("refresh");
        await Promise.all([loadStatus(), loadLibrary()]);
        toast("片库已刷新");
      } catch (err) {
        toast(err.message || "刷新失败", true);
      } finally {
        el.btnRefresh.disabled = false;
        el.btnRefresh.textContent = "刷新片库";
      }
    });

    el.btnCopy.addEventListener("click", async () => {
      const url = el.btnCopy.dataset.url || el.bannerUrl.textContent || "";
      if (!url) return;
      try {
        await navigator.clipboard.writeText(url);
        toast("链接已复制");
      } catch (err) {
        toast("复制失败，请手动选中链接", true);
      }
    });

    let timer = null;
    el.search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.keyword = (el.search.value || "").trim();
        loadLibrary();
      }, 260);
    });
  }

  // ------------------------------------------------------------------ 启动
  async function init() {
    bind();
    try {
      if (bridge && bridge.ready) await bridge.ready();
    } catch (err) {
      /* bridge 可能不需要 ready */
    }
    try {
      await Promise.all([loadStatus(), loadLibrary()]);
    } catch (err) {
      showError(err.message || String(err));
    }
    state.timer = setInterval(() => {
      loadStatus().catch(() => {});
    }, 30000);
  }

  init();
})();
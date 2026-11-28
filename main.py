"""一起观影 —— AstrBot 插件入口。

场景：AstrBot 跑在 NAS 上，人在电脑前看电影。
插件在 NAS 上起一个小型 HTTP 观影室，电脑浏览器打开即可点播 NAS 里的片子；
AI 通过字幕（可选再加画面截图）"看懂"剧情，按节奏说两句点评，
也可以直接推送到聊天平台，让你在手机上和它聊剧情。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import re
import socket
import sys
import threading
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import AstrBotConfig  # type: ignore
except ImportError:  # 兼容极少数没有导出该名字的版本
    AstrBotConfig = dict  # type: ignore


def _load_lib_package() -> str:
    """按文件路径加载 ``watchlib`` 包。

    AstrBot 不同版本、不同安装方式（zip / git 仓库 / 插件市场）会用不同的模块名加载
    ``main.py``，相对导入偶尔会失败（典型报错：``No module named 'watchlib'``）。
    这里直接按 ``__file__`` 找到同目录的 ``watchlib/``，用一个私有包名加载，
    完全绕开 ``sys.path`` 和 ``__package__`` 的差异。
    """

    base = Path(__file__).resolve().parent
    pkg_dir = base / "watchlib"
    pkg_name = "_astrbot_watch_together_lib"
    init_file = pkg_dir / "__init__.py"
    if not init_file.is_file():
        try:
            listing = "、".join(sorted(p.name for p in base.iterdir()))
        except OSError:
            listing = "（目录不可读）"
        raise ImportError(
            "一起观影：插件文件不完整，缺少 watchlib/ 目录。"
            f"当前插件目录：{base}；目录内容：{listing}。"
            "请确认 GitHub 仓库根目录直接包含 watchlib/ 和 web/ 两个文件夹，"
            "删除旧插件目录后重新安装。"
        ) from None

    # 插件重载时丢弃旧模块，保证代码是最新的
    for name in [n for n in list(sys.modules) if n == pkg_name or n.startswith(pkg_name + ".")]:
        sys.modules.pop(name, None)

    spec = importlib.util.spec_from_file_location(
        pkg_name, str(init_file), submodule_search_locations=[str(pkg_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"一起观影：无法加载插件模块 {init_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return pkg_name


_LIB = _load_lib_package()

_engine_mod = importlib.import_module(f"{_LIB}.engine")
_ffmpeg_mod = importlib.import_module(f"{_LIB}.ffmpeg")
_media_mod = importlib.import_module(f"{_LIB}.media")
_server_mod = importlib.import_module(f"{_LIB}.server")
_mediaserver_mod = importlib.import_module(f"{_LIB}.mediaserver")
_embedded_mod = importlib.import_module(f"{_LIB}.embedded")
_memory_mod = importlib.import_module(f"{_LIB}.memory")
_living_mod = importlib.import_module(f"{_LIB}.livingmemory")
_players_mod = importlib.import_module(f"{_LIB}.players")
_subtitles_mod = importlib.import_module(f"{_LIB}.subtitles")
try:
    _native_mod = importlib.import_module(f"{_LIB}.embedded_native")
except Exception:  # noqa: BLE001 - 老版本包可能还没有这个模块
    _native_mod = None

WatchEngine = _engine_mod.WatchEngine
find_ffmpeg = _ffmpeg_mod.find_ffmpeg
probe = _ffmpeg_mod.probe
MediaLibrary = _media_mod.MediaLibrary
ServerContext = _server_mod.ServerContext
start_server = _server_mod.start_server
stop_server = _server_mod.stop_server
MediaServerSource = _mediaserver_mod.MediaServerSource
MediaServerError = _mediaserver_mod.MediaServerError
embedded_extract = _embedded_mod.extract_for
WatchMemory = _memory_mod.WatchMemory
MemoryBridge = _memory_mod.MemoryBridge
LivingMemoryClient = _living_mod.LivingMemoryClient
PlayerWatcher = _players_mod.PlayerWatcher
format_timestamp = _subtitles_mod.format_timestamp
find_local_subtitle = _media_mod.find_local_subtitle
decode_subtitle_bytes = _subtitles_mod.decode_subtitle_bytes
native_extract = getattr(_native_mod, "extract", None) if _native_mod else None
native_probe = getattr(_native_mod, "probe", None) if _native_mod else None

PLUGIN_NAME = "astrbot_plugin_watch_together"
PLUGIN_VERSION = "1.3.0"

# Dashboard 可视化视图用的辅助模块（老版本 AstrBot 可能没有）
try:
    from astrbot.api.web import json_response, request as web_request
except Exception:  # noqa: BLE001 - 仅影响扩展功能页面
    json_response = None  # type: ignore
    web_request = None  # type: ignore


class _ConfigView:
    """给引擎用的只读配置视图，避免把 AstrBot 的对象直接传进去。"""

    def __init__(self, config):
        self._config = config

    def get(self, key, default=None):
        try:
            return self._config.get(key, default)
        except Exception:  # noqa: BLE001
            return default


def detect_lan_ip() -> str:
    """探测本机在局域网里的地址（NAS 通常只有一个出口）。"""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


@register(PLUGIN_NAME, "AlsoLing", "和 AI 一起看电影：AI 读字幕/看画面，实时吐槽解说", PLUGIN_VERSION)
class WatchTogetherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        self._vision_ok = True

        ffmpeg_path = find_ffmpeg(str(self.config.get("ffmpeg_path") or "ffmpeg"))
        self._ffmpeg = ffmpeg_path if ffmpeg_path and probe(ffmpeg_path) else None
        if str(self.config.get("ffmpeg_mode") or "off") != "off" and not self._ffmpeg:
            logger.warning("[watch-together] 配置了 ffmpeg 转封装，但没有找到可用的 ffmpeg")

        # 媒体服务器（Jellyfin / Emby）后台刷新，片库扫描时零阻塞
        self._media_server = MediaServerSource(
            _ConfigView(self.config),
            logger=logger,
            local_items=lambda: self.engine.library.cached() if getattr(self, "engine", None) else [],
        )
        self._media_server.start()

        # 长期记忆：本地观影记忆 + LivingMemory 串联 + 外接记忆插件
        self._memory_enabled = bool(self.config.get("memory_enabled", True))
        self._memory = WatchMemory(self._memory_path())
        self._living = LivingMemoryClient(
            self.context,
            lambda: self.config,
            logger=logger,
            submit=self._submit if self._loop else None,
        )
        self._memory_bridge = MemoryBridge(
            self.context,
            plugin_name=str(self.config.get("memory_plugin") or "astrbot_plugin_sugarmemory_clean"),
            logger=logger,
            submit=self._submit if self._loop else None,
            webhook=str(self.config.get("memory_webhook") or ""),
        )
        if self._memory_enabled and self._living.enabled:
            logger.info(
                "[watch-together] LivingMemory 串联已开启：开场前读旧记忆，散场后写入新记忆"
                "（插件没装或未初始化时自动跳过）"
            )
        elif self._memory_enabled:
            try:
                found = self._memory_bridge.discover()
                logger.info(
                    f"[watch-together] 记忆插件接口：{found or '未发现（只使用本地观影记忆）'}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[watch-together] 探测记忆插件失败：{exc}")

        self.engine = WatchEngine(
            library=MediaLibrary(
                self._media_dirs,
                refresh_seconds=int(self.config.get("library_refresh", 60) or 60),
                logger=logger,
                remote_provider=self._media_server.items,
            ),
            config_getter=_ConfigView(self.config),
            llm_call=self._llm_call,
            push_comment=self._push_comment,
            logger=logger,
            submit=self._submit if self._loop else None,
            ffmpeg_ok=bool(self._ffmpeg),
            subtitle_fetcher=self._fetch_subtitle,
            memory_provider=self._memory_context,
            memory_recaller=self._recall_memory,
            on_session_end=self._remember_session,
        )
        self._watcher = PlayerWatcher(
            self.engine, self._media_server, lambda: self.config, logger=logger
        )
        self._watcher.start()
        self._server = None
        self._thread: threading.Thread | None = None
        self._ctx: ServerContext | None = None
        self._start_lock = threading.Lock()
        self._start_error = ""

        self._register_view_apis()

        if self._loop is not None:
            try:
                self._start_server()
            except Exception as exc:  # noqa: BLE001 - 启动失败不能拖垮插件加载
                logger.error(f"[watch-together] 初始化观影室失败：{exc!r}")

    # ------------------------------------------------------------------ 配置
    def _media_dirs(self) -> list[str]:
        value = self.config.get("media_dirs", [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _port(self) -> int:
        try:
            return int(self.config.get("server_port") or 6116)
        except (TypeError, ValueError):
            return 6116

    def _host_for_url(self) -> str:
        configured = str(self.config.get("public_host") or "").strip()
        if configured:
            configured = re.sub(r"^https?://", "", configured).strip("/")
            return configured
        return detect_lan_ip()

    def _page_url(self, session_id: str | None = None) -> str:
        url = f"http://{self._host_for_url()}:{self._port()}/"
        if session_id:
            url += f"?s={session_id}"
        return url

    def _host_hint(self) -> str:
        """Docker 桥接网络里自动探测到的是容器 IP，提醒用户手填 NAS 地址。"""

        if str(self.config.get("public_host") or "").strip():
            return ""
        host = self._host_for_url()
        if host.startswith(("172.", "10.88.")) or host == "127.0.0.1":
            return "（这个地址如果打不开，请在插件配置 public_host 填 NAS 的局域网 IP，例如 192.168.1.10）"
        return ""

    # ------------------------------------------------------------------ 扩展功能页面
    def _bot_name(self) -> str:
        return str(self.config.get("bot_name") or "TA")

    def _register_view_apis(self) -> None:
        """注册 WebUI「扩展功能」页面用的接口。"""

        if json_response is None or web_request is None:
            return
        try:
            base = f"/{PLUGIN_NAME}"
            self.context.register_web_api(f"{base}/status", self._api_status, ["GET"], "观影室状态")
            self.context.register_web_api(f"{base}/library", self._api_library, ["GET"], "观影室片库")
            self.context.register_web_api(f"{base}/play", self._api_play, ["POST"], "开始观影")
            self.context.register_web_api(f"{base}/refresh", self._api_refresh, ["POST"], "刷新片库")
            self.context.register_web_api(
                f"{base}/test-media-server", self._api_test_media_server, ["POST"], "测试媒体服务器"
            )
        except Exception as exc:  # noqa: BLE001 - 老版本没有 register_web_api
            logger.warning(f"[watch-together] 可视化页面接口注册失败（不影响其他功能）：{exc!r}")

    def _view_status(self) -> dict:
        library = self.engine.library
        items = library.items()
        active = [s for s in self.engine.all_sessions() if not s.ended]
        return {
            "ok": True,
            "version": PLUGIN_VERSION,
            "bot_name": self._bot_name(),
            "port": self._port(),
            "server": {
                "running": self._server is not None,
                "base_url": self._page_url(),
                "error": self._start_error,
            },
            "library": {
                "count": len(items),
                "with_sub": sum(1 for item in items if item.subtitle),
                "roots": library.roots(),
                "errors": library.errors()[:6],
            },
            "media_server": self._media_server.status(),
            "watcher": self._watcher.status() if self._watcher else {},
            "memory": {
                "enabled": self._memory_enabled,
                "recent": len(self._memory.recent(n=100)) if self._memory else 0,
                "livingmemory": self._living.status(),
            },
            "sessions": [
                {
                    "id": s.id,
                    "title": s.title,
                    "playing": s.playing,
                    "position": round(s.position, 1),
                    "comments": len(s.comments),
                    "playback": s.playback,
                }
                for s in active
            ],
            "ffmpeg": bool(self._ffmpeg),
            "defaults": {
                "interval": int(self.config.get("comment_interval", 90) or 90),
                "vision": bool(self.config.get("vision_enabled", True)),
            },
        }

    def _view_library(self, keyword: str = "", limit: int = 120) -> dict:
        library = self.engine.library
        all_items = library.items()
        items = library.search(keyword) if (keyword or "").strip() else all_items
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 120
        return {
            "ok": True,
            "total": len(all_items),
            "matched": len(items),
            "items": [item.to_public() for item in items[:limit]],
        }

    def _view_play(self, movie_id: str) -> dict:
        item = self.engine.library.get(movie_id) or self.engine.library.resolve(movie_id)
        if item is None:
            return {"ok": False, "error": "找不到这个片源，刷新片库再试试"}
        session = self.engine.create_session(item=item, source="nas")
        self.engine.schedule_intro(session)
        return {
            "ok": True,
            "id": session.id,
            "title": item.title,
            "playback": session.playback,
            "path": f"/?s={session.id}",
        }

    def _view_refresh(self) -> dict:
        self.engine.library.scan(force=True)
        if self._media_server.enabled:
            self._media_server.refresh_once()
        return {
            "ok": True,
            "count": len(self.engine.library.items()),
            "media_server": self._media_server.status(),
        }

    def _view_test_media_server(self) -> dict:
        try:
            info = self._media_server.test_connection()
        except MediaServerError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **info}

    async def _api_status(self):
        return json_response(await asyncio.to_thread(self._view_status))

    async def _api_library(self):
        keyword = str(web_request.query.get("q", "") or "")
        try:
            limit = int(web_request.query.get("limit", 120))
        except (TypeError, ValueError):
            limit = 120
        return json_response(await asyncio.to_thread(self._view_library, keyword, limit))

    async def _api_play(self):
        payload = await web_request.json(default={})
        movie_id = str(payload.get("movie_id") or "")
        return json_response(await asyncio.to_thread(self._view_play, movie_id))

    async def _api_refresh(self):
        return json_response(await asyncio.to_thread(self._view_refresh))

    async def _api_test_media_server(self):
        return json_response(await asyncio.to_thread(self._view_test_media_server))

    async def _fetch_subtitle(self, item) -> str:
        """媒体服务器字幕 → 同目录外挂字幕 → 内嵌字幕（ffmpeg / 纯 Python 兜底）。"""

        if item is None:
            return ""
        if item.remote_id and self._media_server is not None:
            try:
                text = await asyncio.to_thread(self._media_server.fetch_subtitle, item)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[watch-together] 媒体服务器字幕下载失败：{exc}")
            else:
                if text:
                    return text

        path = Path(item.path) if item.path else None
        if path is not None and path.is_file():
            # 有些条目的字幕是扫描之后才放进目录的，这里现找一遍
            try:
                subtitle, tag = await asyncio.to_thread(find_local_subtitle, path)
            except Exception:  # noqa: BLE001
                subtitle, tag = None, ""
            if subtitle is not None:
                try:
                    data = await asyncio.to_thread(Path(subtitle).read_bytes)
                except OSError:
                    data = b""
                text = decode_subtitle_bytes(data) if data else ""
                if text:
                    item.subtitle = Path(subtitle)
                    item.subtitle_tag = tag
                    return text

            if not bool(self.config.get("embedded_subtitles", True)):
                return ""
            if self._ffmpeg:
                text = await asyncio.to_thread(embedded_extract, path, self._ffmpeg)
                if text:
                    return text
            # 没装 ffmpeg 时用纯 Python 解析 MKV/MP4 里的文本字幕
            if native_extract is not None:
                try:
                    text = await asyncio.to_thread(native_extract, path)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[watch-together] 纯 Python 字幕提取失败：{exc}")
                else:
                    if text:
                        return text
        return ""

    def _subtitle_diag(self, session) -> dict:
        """观影室里「为什么没字幕」的诊断信息（跑在 HTTP 线程里）。"""

        item = getattr(session, "item", None)
        path = Path(item.path) if (item is not None and item.path) else None
        info: dict = {
            "ok": True,
            "has_sub": bool(session.cues),
            "subtitle": session.subtitle_name,
            "cues": len(session.cues),
            "note": session.subtitle_note,
            "playback": session.playback,
            "item": {
                "title": item.title if item else "",
                "source": item.source if item else "",
                "path": str(path) if path else "",
                "exists": bool(path and path.is_file()),
                "remote": bool(item.remote_stream) if item else False,
                "remote_sub_index": item.remote_sub_index if item else None,
            },
            "sidecar": {"found": "", "tag": ""},
            "embedded": {"ffmpeg": bool(self._ffmpeg), "tracks": []},
            "hint": "",
        }
        bitmap_only = False
        if path is not None and path.is_file():
            try:
                subtitle, tag = find_local_subtitle(path)
            except Exception:  # noqa: BLE001
                subtitle, tag = None, ""
            if subtitle is not None:
                info["sidecar"] = {"found": Path(subtitle).name, "tag": tag}
            tracks: list[dict] = []
            if native_probe is not None:
                try:
                    tracks = native_probe(path) or []
                except Exception:  # noqa: BLE001
                    tracks = []
            info["embedded"]["tracks"] = tracks
            text_tracks = [track for track in tracks if not track.get("bitmap")]
            bitmap_only = bool(tracks) and not text_tracks
        elif item is not None and item.remote_stream:
            info["hint"] = "这个条目来自媒体服务器，NAS 上看不到原始文件；字幕只能从媒体服务器取。"
        if session.cues:
            info["hint"] = f"字幕已加载：{session.subtitle_name or '文本字幕'}"
        elif bitmap_only:
            info["hint"] = (
                "片子里是图片字幕（PGS / VobSub），浏览器和 AI 都读不了文本。"
                "用绿联影视中心 / VLC 直接看可以显示；想在这里看，需要在媒体服务器里"
                "转成文本字幕，或者手动上传一份 srt。"
            )
        elif info["sidecar"]["found"]:
            info["hint"] = "找到了外挂字幕文件，点「重新获取字幕」再试一次。"
        elif not info["hint"]:
            info["hint"] = (
                "没检测到可读的字幕轨。可能是硬字幕、字幕名字对不上，或者文件在 NAS 上"
                "但 AstrBot 容器里看不到；可以点「加载字幕」手动传一份 srt/ass。"
            )
        return info

    # ------------------------------------------------------------------ 长期记忆
    def _memory_path(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001 - 老版本没有这个工具函数
            base = Path.home() / ".astrbot_plugin_data"
        return base / PLUGIN_NAME / "watch_memory.json"

    def _memory_context(self, umo: str | None, title: str) -> str:
        if not self._memory_enabled:
            return ""
        try:
            return self._memory.render(umo=umo, n=3)
        except Exception:  # noqa: BLE001
            return ""

    async def _recall_memory(self, umo: str | None, title: str) -> str:
        """开场/聊天前读 LivingMemory 里的旧记忆。"""

        if not (self._memory_enabled and self._living.enabled):
            return ""
        try:
            return await self._living.recall_text(title=title, umo=umo, k=4)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[watch-together] 读取 LivingMemory 失败：{exc}")
            return ""

    async def _remember_living(self, text: str, umo: str | None, title: str) -> None:
        try:
            await self._living.remember(
                text,
                umo=umo,
                topics=["一起看电影", title],
                key_facts=[f"和用户一起看了《{title}》"],
                importance=0.6,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[watch-together] 写入 LivingMemory 失败：{exc}")

    def _remember_session(self, session) -> None:
        if not self._memory_enabled or session is None:
            return
        comments = [
            comment.text
            for comment in session.comments
            if comment.kind in ("ambient", "chat", "intro")
        ][-4:]
        user_lines = [comment.text for comment in session.comments if comment.kind == "user"][-3:]
        if not comments and not user_lines:
            return
        entry = {
            "ts": time.time(),
            "title": session.title,
            "umo": session.umo or "",
            "position": round(session.position, 1),
            "comments": comments,
            "user_lines": user_lines,
        }
        try:
            self._memory.add(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[watch-together] 写入本地观影记忆失败：{exc}")
        text = (
            f"和用户一起看了《{session.title}》"
            f"（看到 {format_timestamp(session.position, True)}）。"
        )
        if comments:
            text += "我说过：" + "；".join(comments[-2:]) + "。"
        if user_lines:
            text += "用户说：" + "；".join(user_lines[-2:]) + "。"
        # 优先写进 LivingMemory（向量/图谱可检索）；没接上再退回通用桥
        if self._living.enabled and self._living.engine is not None:
            scheduled = self._living.remember_soon(
                text,
                umo=session.umo or None,
                topics=["一起看电影", session.title],
                key_facts=[f"和用户一起看了《{session.title}》"],
                importance=0.6,
            )
            if scheduled:
                return
        try:
            self._memory_bridge.remember(text, session.umo or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[watch-together] 记忆插件写入失败：{exc}")

    # ------------------------------------------------------------------ 服务
    def _submit(self, coro):
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            return None

    def _start_server(self) -> bool:
        with self._start_lock:
            if self._server is not None:
                return True
            web_dir = Path(__file__).resolve().parent / "web"
            if not (web_dir / "index.html").is_file():
                self._start_error = "missing_web"
                logger.error(
                    "[watch-together] 缺少 web/index.html，观影室无法启动；"
                    "说明插件文件没装全，请从仓库重新安装完整插件"
                )
                return False
            ctx = ServerContext(
                engine=self.engine,
                web_dir=web_dir,
                config_getter=lambda: self.config,
                logger=logger,
                ffmpeg_path=self._ffmpeg,
                subtitle_diag=self._subtitle_diag,
            )
            host = str(self.config.get("server_host") or "0.0.0.0")
            port = self._port()
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    server, thread = start_server(ctx, host, port)
                except OSError as exc:
                    last_error = exc
                    if attempt == 0:
                        time.sleep(0.4)  # 插件重载时端口可能还没释放
                    continue
                self._server, self._thread, self._ctx = server, thread, ctx
                self._start_error = ""
                logger.info(
                    f"[watch-together] 观影室已启动：{self._page_url()}（监听 {host}:{port}）"
                )
                return True
            self._start_error = f"port: {last_error}"
            logger.error(
                f"[watch-together] 观影室启动失败（端口 {port}）：{last_error}；"
                "请修改插件配置 server_port 或释放该端口"
            )
            return False

    async def _ensure_started(self) -> bool:
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
                self.engine.set_submit(self._submit)
        if self._loop is not None:
            self._living.set_submit(self._submit)
            self._memory_bridge._submit = self._submit
        self.engine.purge()
        return self._start_server()

    # ------------------------------------------------------------------ AI
    async def _llm_call(self, session, system: str, prompt: str, frame: bytes | None) -> str:
        provider_id = str(self.config.get("provider_id") or "").strip()
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=session.umo or ""
                )
            except Exception:  # noqa: BLE001
                provider_id = ""
        if not provider_id:
            raise RuntimeError("还没有选择聊天模型：请在插件配置里指定 provider_id")

        image_urls = None
        if frame and self._vision_ok:
            image_urls = ["base64://" + base64.b64encode(frame).decode("ascii")]
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system,
                image_urls=image_urls,
            )
        except Exception as exc:  # noqa: BLE001
            if not image_urls:
                raise
            # 当前模型可能不支持读图：本插件生命周期内不再发图，避免每次点评都失败重试
            self._vision_ok = False
            logger.warning(f"[watch-together] 画面理解失败，后续改用纯文本：{exc}")
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system,
            )
        return (getattr(response, "completion_text", "") or "").strip()

    async def _push_comment(self, session, text: str, position: float) -> None:
        if not session.umo:
            return
        prefix = f"🎬 {format_timestamp(position)} "
        with contextlib.suppress(Exception):
            await self.context.send_message(
                session.umo, MessageChain().message(prefix + text)
            )

    # ------------------------------------------------------------------ 命令辅助
    @staticmethod
    def _arg(event: AstrMessageEvent, command: str) -> str:
        text = (event.message_str or "").strip()
        # 去掉可能的指令前缀（/ ／ ! ！ 等），再削掉指令词本身
        text = re.sub(r"^[/／!！.。·\-]+", "", text).strip()
        if text.startswith(command):
            text = text[len(command) :]
        return text.strip()

    async def _render_library(self, limit: int = 15) -> str:
        library = self.engine.library
        items = library.items()
        if not items:
            lines = ["🎬 片库是空的。"]
            if library.errors():
                lines.extend(f"· {err}" for err in library.errors()[:3])
                lines.append("")
                lines.append("请检查插件配置 media_dirs 填的是 AstrBot 容器/主机里真实存在的路径。")
            else:
                lines.append("请先在插件配置 media_dirs 里填写 NAS 上的电影目录，例如 /volume1/video/movies")
            lines.append(f"观影室地址：{self._page_url()}")
            return "\n".join(lines)

        lines = [f"🎬 片库（共 {len(items)} 部，显示前 {min(limit, len(items))} 部）"]
        for index, item in enumerate(items[:limit], start=1):
            sub = "有字幕" if item.subtitle else "无字幕"
            lines.append(f"{index}. {item.title} · {item.ext.lstrip('.').upper()} · {sub}")
        if len(items) > limit:
            lines.append(f"……还有 {len(items) - limit} 部")
        lines.append("")
        lines.append(f"🌐 观影室：{self._page_url()}")
        hint = self._host_hint()
        if hint:
            lines.append(hint)
        lines.append("发送 /观影 序号 或 /观影 片名 开始；也可以直接在网页里点。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 命令
    @filter.command("观影")
    async def watch(self, event: AstrMessageEvent):
        """打开观影室 / 点播一部电影。用法：/观影、/观影 1、/观影 让子弹飞、/观影 停止"""

        await self._ensure_started()
        argument = self._arg(event, "观影")
        lowered = argument.lower()

        if lowered in ("停止", "结束", "stop", "end", "退出"):
            async for result in self._stop(event):
                yield result
            return
        if lowered in ("状态", "status"):
            async for result in self._status(event):
                yield result
            return
        if lowered in ("列表", "list", "片库", ""):
            yield event.plain_result(await self._render_library())
            return
        if lowered in ("帮助", "help", "?"):
            yield event.plain_result(
                "🍿 一起观影\n"
                "/观影 —— 查看片库和观影室地址\n"
                "/观影 1 —— 按序号点播\n"
                "/观影 片名关键字 —— 搜索并点播\n"
                "/观影 状态 —— 看当前进度\n"
                "/观影 停止 —— 结束本次观影\n\n"
                "在电脑浏览器打开观影室地址就能看电影；"
                "看电影时直接在这里说话，我会接着剧情跟你聊。"
            )
            return

        if not self._server:
            reason = (
                "插件文件不完整（缺少 web/index.html），请从仓库重新安装。"
                if self._start_error == "missing_web"
                else f"观影室没能启动（{self._start_error or '未知原因'}）。"
                "请检查端口占用，或在插件配置里改一个 server_port。"
            )
            yield event.plain_result(reason)
            return

        item = self.engine.library.resolve(argument)
        if item is None:
            yield event.plain_result(
                f"片库里没找到「{argument}」。发送 /观影 看看有哪些片子，或换个关键字。"
            )
            return

        session = self.engine.create_session(
            item=item,
            source="nas",
            umo=event.unified_msg_origin,
        )
        self.engine.schedule_intro(session)

        lines = [f"🎬 已就位：《{item.title}》", item.describe()]
        if session.playback == "unsupported":
            lines.append(
                "⚠️ 这个格式浏览器不能直接播：请在插件配置把 ffmpeg_mode 打开（NAS 装了 ffmpeg 时），"
                "或者先把片子转成 mp4。"
            )
        elif session.playback == "remux":
            lines.append("ℹ️ 正在用 ffmpeg 实时转封装（实验功能，拖动进度可能不准）。")
        if session.playback == "remote":
            lines.append("ℹ️ 本片来自媒体服务器，直接由它串流播放（格式不支持时可用外部播放器模式）。")
        if not session.cues:
            lines.append("⚠️ 没找到字幕，我主要靠画面理解，可以在网页里手动加载字幕。")
        lines.append("")
        lines.append(f"在电脑上打开：{self._page_url(session.id)}")
        lines.append(f"（观影室首页：{self._page_url()}）")
        hint = self._host_hint()
        if hint:
            lines.append(hint)
        lines.append("看的时候在这里说话就行，我会跟着剧情接话；发 /观影 停止 收工。")
        yield event.plain_result("\n".join(lines))

    @filter.command("观影列表")
    async def watch_list(self, event: AstrMessageEvent):
        """查看片库。"""

        await self._ensure_started()
        yield event.plain_result(await self._render_library())

    @filter.command("观影停止")
    async def watch_stop(self, event: AstrMessageEvent):
        """结束当前观影。"""

        async for result in self._stop(event):
            yield result

    @filter.command("观影状态")
    async def watch_status(self, event: AstrMessageEvent):
        """查看当前观影进度。"""

        async for result in self._status(event):
            yield result

    async def _stop(self, event: AstrMessageEvent):
        session = self.engine.active_for_umo(event.unified_msg_origin)
        if session is None:
            yield event.plain_result("现在没有正在看的电影。发送 /观影 挑一部？")
            return
        self.engine.close(session.id)
        yield event.plain_result(
            f"🎬 已结束《{session.title}》，这次一共留下了 {len(session.comments)} 条记录。"
        )

    async def _status(self, event: AstrMessageEvent):
        session = self.engine.active_for_umo(event.unified_msg_origin)
        if session is None:
            yield event.plain_result("现在没有正在看的电影。发送 /观影 挑一部？")
            return
        state = "播放中" if session.playing else "暂停中"
        lines = [
            f"🎬 《{session.title}》",
            f"进度：{format_timestamp(session.position, True)} · {state}",
            f"字幕：{session.subtitle_name or '无'} · 已记录 {len(session.comments)} 条",
            f"网页：{self._page_url(session.id)}",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ 聊天桥
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def chat_bridge(self, event: AstrMessageEvent):
        """看电影时的闲聊：把消息接到"一起看片"的上下文里。"""

        if not bool(self.config.get("chat_bridge", True)):
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        # 指令（任何前缀）交给指令处理器，别当成看电影的闲聊
        if re.match(r"^[\s/／!！#＃.。·\-]", text):
            return
        if text.startswith(("观影", "help", "帮助", "新对话", "reset")):
            return
        session = self.engine.active_for_umo(event.unified_msg_origin)
        if session is None:
            return
        mode = str(self.config.get("chat_bridge_mode", "wake") or "wake")
        if mode == "wake":
            woken = bool(getattr(event, "is_at_or_wake_command", False))
            is_private = False
            with contextlib.suppress(Exception):
                is_private = bool(event.is_private_chat())
            if not (woken or is_private):
                return
        await self._ensure_started()
        reply = await self.engine.chat(session, text)
        if not reply:
            return
        event.stop_event()
        yield event.plain_result(reply)

    # ------------------------------------------------------------------ 生命周期
    async def terminate(self):
        """插件卸载/重载时关闭观影室，释放端口和 ffmpeg 进程。"""

        with contextlib.suppress(Exception):
            self.engine.close_all()
        with contextlib.suppress(Exception):
            self._media_server.stop()
        with contextlib.suppress(Exception):
            self._watcher.stop()
        stop_server(self._server)
        self._server = None
        self._thread = None
        logger.info("[watch-together] 观影室已关闭")
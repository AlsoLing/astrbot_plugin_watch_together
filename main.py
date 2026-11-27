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
_subtitles_mod = importlib.import_module(f"{_LIB}.subtitles")

WatchEngine = _engine_mod.WatchEngine
find_ffmpeg = _ffmpeg_mod.find_ffmpeg
probe = _ffmpeg_mod.probe
MediaLibrary = _media_mod.MediaLibrary
ServerContext = _server_mod.ServerContext
start_server = _server_mod.start_server
stop_server = _server_mod.stop_server
format_timestamp = _subtitles_mod.format_timestamp

PLUGIN_NAME = "astrbot_plugin_watch_together"


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


@register(PLUGIN_NAME, "观影小队", "和 AI 一起看电影：AI 读字幕/看画面，实时吐槽解说", "1.0.1")
class WatchTogetherPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()

        ffmpeg_path = find_ffmpeg(str(self.config.get("ffmpeg_path") or "ffmpeg"))
        self._ffmpeg = ffmpeg_path if ffmpeg_path and probe(ffmpeg_path) else None
        if str(self.config.get("ffmpeg_mode") or "off") != "off" and not self._ffmpeg:
            logger.warning("[watch-together] 配置了 ffmpeg 转封装，但没有找到可用的 ffmpeg")

        self.engine = WatchEngine(
            library=MediaLibrary(
                self._media_dirs,
                refresh_seconds=int(self.config.get("library_refresh", 60) or 60),
                logger=logger,
            ),
            config_getter=_ConfigView(self.config),
            llm_call=self._llm_call,
            push_comment=self._push_comment,
            logger=logger,
            submit=self._submit if self._loop else None,
            ffmpeg_ok=bool(self._ffmpeg),
        )
        self._server = None
        self._thread: threading.Thread | None = None
        self._ctx: ServerContext | None = None
        self._start_lock = threading.Lock()
        self._start_error = ""
        self._vision_ok = True

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
            return int(self.config.get("server_port") or 6199)
        except (TypeError, ValueError):
            return 6199

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
        stop_server(self._server)
        self._server = None
        self._thread = None
        logger.info("[watch-together] 观影室已关闭")
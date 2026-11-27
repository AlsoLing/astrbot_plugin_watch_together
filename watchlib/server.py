"""观影室的 HTTP 服务。

为什么用标准库而不是 aiohttp/quart：
* 不增加任何依赖，AstrBot 容器里直接能跑；
* 视频串流需要自己控制 Content-Range 和 ffmpeg 管道，裸 HTTP 最省心；
* 线程模型简单：每个请求一个线程，AI 调用通过事件循环的线程安全接口调度，
  HTTP 线程永远不会被大模型阻塞。

所有 AI 相关逻辑都通过 ``WatchEngine`` 的协程完成，本模块只做协议适配。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .engine import WatchEngine, WatchSession
from .ffmpeg import build_command

__all__ = ["ServerContext", "WatchHTTPServer", "start_server", "stop_server"]

VERSION = "1.0.1"
CHUNK = 256 * 1024
SESSION_ROUTE = re.compile(r"^/api/session/([0-9a-fA-F]{6,32})(?:/([a-zA-Z.]+))?$")
STREAM_ROUTE = re.compile(r"^/api/stream/([0-9a-fA-F]{6,32})$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}

VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".ogg": "video/ogg",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
}


class ServerContext:
    """HTTP 线程与插件之间的共享上下文。"""

    def __init__(
        self,
        engine: WatchEngine,
        web_dir: Path,
        config_getter: Callable[[], Any],
        logger=None,
        ffmpeg_path: str | None = None,
    ) -> None:
        self.engine = engine
        self.web_dir = Path(web_dir)
        self._config_getter = config_getter
        self.logger = logger
        self.ffmpeg = ffmpeg_path
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self._static_cache: dict[str, tuple[float, bytes]] = {}

    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = (self._config_getter() or {}).get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def register_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def unregister_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def kill_procs(self) -> None:
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass

    def static(self, name: str) -> tuple[bytes | None, str]:
        mime = STATIC_FILES.get(name)
        if mime is None:
            return None, ""
        path = self.web_dir / name
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None, mime
        cached = self._static_cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1], mime
        try:
            data = path.read_bytes()
        except OSError:
            return None, mime
        self._static_cache[name] = (mtime, data)
        return data, mime


def _parse_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    """返回 (start, end)；None 表示没有 Range；"invalid" 表示 416。"""

    if not header:
        return None
    match = RANGE_RE.match(header.strip().split(",")[0].strip())
    if not match:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            return "invalid"
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start >= size or start > end:
        return "invalid"
    return start, min(end, size - 1)


class WatchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_cls, ctx: ServerContext):
        self.ctx = ctx
        super().__init__(address, handler_cls)


class WatchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"WatchTogether/{VERSION}"
    sys_version = ""
    timeout = 65  # 空闲 keep-alive 别一直占着线程

    # ------------------------------------------------------------- 基础工具
    @property
    def ctx(self) -> ServerContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        # 视频串流会产生大量请求，默认不刷屏
        return

    def log_error(self, fmt: str, *args) -> None:
        if self.ctx.logger:
            self.ctx.logger.debug(f"[watch-together] {fmt % args}")

    def _body(self, limit: int = 8 * 1024 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > limit:
            raise ValueError("payload too large")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ------------------------------------------------------------- 路由
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self.close_connection = True
            self._safe_500(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self.close_connection = True
            self._safe_500(exc)

    def _safe_500(self, exc: Exception) -> None:
        if self.ctx.logger:
            self.ctx.logger.warning(f"[watch-together] 处理请求出错: {exc!r}")
        try:
            self._send_error_json("internal_error", 500)
        except Exception:  # noqa: BLE001
            pass

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path == "/favicon.ico":
            return self._serve_static("favicon.svg")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/") :])
        if path == "/api/health":
            return self._send_json(
                {
                    "ok": True,
                    "version": VERSION,
                    "ffmpeg": bool(self.ctx.ffmpeg),
                    "stats": self.ctx.engine.stats(),
                }
            )
        if path == "/api/meta":
            return self._send_json(
                {
                    "ok": True,
                    "bot_name": str(self.ctx.cfg("bot_name", "TA")),
                    "defaults": {
                        "interval": int(self.ctx.cfg("comment_interval", 90) or 90),
                        "vision": bool(self.ctx.cfg("vision_enabled", True)),
                    },
                    "ffmpeg": bool(self.ctx.ffmpeg),
                    "media_roots": len(self.ctx.engine.library.roots()),
                }
            )
        if path == "/api/library":
            return self._send_library(query)

        match = STREAM_ROUTE.match(path)
        if match:
            return self._serve_stream(match.group(1), query)

        match = SESSION_ROUTE.match(path)
        if match:
            session_id, action = match.group(1), (match.group(2) or "")
            if action == "":
                return self._session_info(session_id)
            if action == "events":
                after = query.get("after", ["0"])[0]
                try:
                    after_id = int(after)
                except ValueError:
                    after_id = 0
                return self._send_json(self.ctx.engine.events(session_id, after_id))
            if action in ("subtitles.vtt", "subtitle.vtt"):
                return self._serve_vtt(session_id, query)
            if action == "stream":
                return self._serve_stream(session_id, query)

        if path.startswith("/api/"):
            return self._send_error_json("not_found", 404)
        return self._send_error_json("not_found", 404)

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/session":
            return self._create_session()
        if path == "/api/library/refresh":
            self.ctx.engine.library.scan(force=True)
            return self._send_json({"ok": True, "count": len(self.ctx.engine.library.items())})

        match = SESSION_ROUTE.match(path)
        if match:
            session_id, action = match.group(1), (match.group(2) or "")
            if action == "sync":
                return self._sync(session_id)
            if action == "chat":
                return self._chat(session_id)
            if action == "frame":
                return self._frame(session_id, query)
            if action == "subtitle":
                return self._upload_subtitle(session_id)
            if action == "settings":
                payload = self._json_body()
                return self._send_json(self.ctx.engine.update_settings(session_id, payload))
            if action == "end":
                return self._send_json({"ok": self.ctx.engine.close(session_id)})

        self._send_error_json("not_found", 404)

    # ------------------------------------------------------------- 静态资源
    def _serve_static(self, name: str) -> None:
        data, mime = self.ctx.static(name)
        if data is None:
            return self._send_error_json("not_found", 404)
        self._send_bytes(data, mime)

    # ------------------------------------------------------------- 片库
    def _send_library(self, query: dict) -> None:
        library = self.ctx.engine.library
        force = query.get("force", ["0"])[0] in ("1", "true", "yes")
        keyword = (query.get("q", [""])[0] or "").strip()
        items = library.search(keyword) if keyword else library.items(force=force)
        if force and keyword:
            library.scan(force=True)
            items = library.search(keyword)
        self._send_json(
            {
                "ok": True,
                "items": [item.to_public() for item in items],
                "errors": library.errors(),
                "roots": library.roots(),
            }
        )

    # ------------------------------------------------------------- 会话
    def _create_session(self) -> None:
        payload = self._json_body()
        source = str(payload.get("source") or "nas")
        movie_id = str(payload.get("movie_id") or "")
        library = self.ctx.engine.library
        item = None
        if movie_id:
            item = library.get(movie_id)
            if item is None:  # 冷启动时缓存是空的，先扫一遍
                library.scan()
                item = library.get(movie_id)
        if source == "nas" and item is None:
            return self._send_error_json("movie_not_found", 404)
        if source not in ("nas", "local", "external"):
            source = "nas"
        title = str(payload.get("title") or "").strip() or None
        duration = payload.get("duration")
        try:
            duration = float(duration) if duration else None
        except (TypeError, ValueError):
            duration = None

        session = self.ctx.engine.create_session(
            item=item,
            source=source,
            title=title,
            duration=duration,
        )
        settings = payload.get("settings")
        if isinstance(settings, dict):
            self.ctx.engine.update_settings(session.id, settings)
        self.ctx.engine.schedule_intro(session)
        info = session.info()
        info["ok"] = True
        info["stream_url"] = f"/api/stream/{session.id}"
        info["vtt_url"] = f"/api/session/{session.id}/subtitles.vtt"
        info["events_url"] = f"/api/session/{session.id}/events"
        self._send_json(info)

    def _session_info(self, session_id: str) -> None:
        session = self.ctx.engine.get(session_id)
        if session is None:
            return self._send_error_json("session_not_found", 404)
        payload = session.info()
        payload["ok"] = True
        self._send_json(payload)

    def _require_session(self, session_id: str) -> WatchSession | None:
        session = self.ctx.engine.get(session_id)
        if session is None:
            self._send_error_json("session_not_found", 404)
        return session

    def _sync(self, session_id: str) -> None:
        if self._require_session(session_id) is None:
            return
        payload = self._json_body()
        try:
            position = float(payload.get("position") or 0)
        except (TypeError, ValueError):
            position = 0.0
        duration = payload.get("duration")
        try:
            duration_value = float(duration) if duration else None
        except (TypeError, ValueError):
            duration_value = None
        result = self.ctx.engine.sync(
            session_id, position, bool(payload.get("playing")), duration_value
        )
        self._send_json(result)

    def _chat(self, session_id: str) -> None:
        session = self._require_session(session_id)
        if session is None:
            return
        payload = self._json_body()
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._send_error_json("empty_text", 400)
        self.ctx.engine.schedule_chat(session, text)
        self._send_json({"ok": True, "queued": True})

    def _frame(self, session_id: str, query: dict) -> None:
        if self._require_session(session_id) is None:
            return
        try:
            position = float(query.get("t", ["0"])[0])
        except ValueError:
            position = 0.0
        raw = self._body(limit=6 * 1024 * 1024)
        if not raw:
            return self._send_error_json("empty_frame", 400)
        try:
            data = base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            return self._send_error_json("bad_base64", 400)
        ok = self.ctx.engine.set_frame(session_id, data, position)
        self._send_json({"ok": ok, "bytes": len(data)})

    def _upload_subtitle(self, session_id: str) -> None:
        session = self._require_session(session_id)
        if session is None:
            return
        payload = self._json_body()
        text = str(payload.get("text") or "")
        name = str(payload.get("name") or "手动字幕.srt")
        if not text.strip():
            return self._send_error_json("empty_subtitle", 400)
        count = self.ctx.engine.attach_subtitle_text(session, text, name)
        self._send_json({"ok": count > 0, "count": count})

    # ------------------------------------------------------------- 视频
    def _serve_vtt(self, session_id: str, query: dict) -> None:
        session = self.ctx.engine.get(session_id)
        if session is None:
            return self._send_error_json("session_not_found", 404)
        try:
            offset = float(query.get("offset", ["0"])[0])
        except ValueError:
            offset = 0.0
        from .subtitles import to_webvtt

        body = to_webvtt(session.cues, offset=offset).encode("utf-8")
        self._send_bytes(body, "text/vtt; charset=utf-8")

    def _serve_stream(self, session_id: str, query: dict) -> None:
        session = self.ctx.engine.get(session_id)
        if session is None:
            return self._send_error_json("session_not_found", 404)
        if session.item is None or session.playback in ("none", "local"):
            return self._send_error_json("no_server_side_file", 404)
        path = Path(session.item.path)
        if not path.is_file():
            return self._send_error_json("file_missing", 404)

        if session.playback == "remux":
            try:
                start = float(query.get("t", ["0"])[0])
            except ValueError:
                start = 0.0
            return self._stream_remux(path, start, session)

        if session.playback == "unsupported":
            return self._send_error_json(
                "unsupported_container: 浏览器无法直接播放该格式，请在插件配置里开启 ffmpeg 转封装，"
                "或先转成 mp4",
                415,
            )
        return self._stream_file(path)

    def _stream_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return self._send_error_json("file_missing", 404)
        if size <= 0:
            return self._send_error_json("empty_file", 404)

        mime = VIDEO_MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
        mime = mime or "application/octet-stream"
        parsed = _parse_range(self.headers.get("Range"), size)
        if parsed == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end = parsed
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return

        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _stream_remux(self, path: Path, start: float, session: WatchSession) -> None:
        ffmpeg = self.ctx.ffmpeg
        if not ffmpeg:
            return self._send_error_json("ffmpeg_not_found", 415)
        mode = str(self.ctx.cfg("ffmpeg_mode", "remux") or "remux")
        if mode == "off":
            return self._send_error_json("ffmpeg_disabled", 415)
        cmd = build_command(ffmpeg, path, max(0.0, start), mode)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            return self._send_error_json(f"ffmpeg_start_failed: {exc}", 500)

        self.ctx.register_proc(proc)
        session.stream_offset = max(0.0, start)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "none")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            proc.kill()
            self.ctx.unregister_proc(proc)
            return

        stdout = proc.stdout
        try:
            while True:
                chunk = stdout.read(CHUNK) if stdout else b""
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if stdout:
                try:
                    stdout.close()
                except OSError:
                    pass
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.ctx.unregister_proc(proc)


def start_server(ctx: ServerContext, host: str, port: int) -> tuple[WatchHTTPServer, threading.Thread]:
    server = WatchHTTPServer((host, port), WatchHandler, ctx)
    thread = threading.Thread(target=server.serve_forever, name="watch-together-http", daemon=True)
    thread.start()
    return server, thread


def stop_server(server: WatchHTTPServer | None) -> None:
    if server is None:
        return
    try:
        ctx = getattr(server, "ctx", None)
        if ctx is not None:
            ctx.kill_procs()
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
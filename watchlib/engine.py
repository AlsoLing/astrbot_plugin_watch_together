"""观影会话引擎。

职责：
* 维护每个观影室的状态（片源、进度、字幕、已生成的点评）；
* 根据播放进度决定"什么时候该说一句"，并异步调度大模型；
* 给网页 / 聊天平台提供统一的读写接口。

引擎本身不依赖 AstrBot：大模型调用和消息推送通过注入的回调完成，
这样既能在 AstrBot 里跑，也能在本地用假模型做集成测试。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import prompts
from .media import BROWSER_EXTS, MediaItem, MediaLibrary
from .subtitles import Cue, decode_subtitle_bytes, parse_subtitle

__all__ = ["Comment", "WatchSession", "WatchEngine"]

ConfigGetter = Callable[[], Any]
LLMCall = Callable[["WatchSession", str, str, bytes | None], Awaitable[str]]
PushCall = Callable[["WatchSession", str, float], Awaitable[None]]


@dataclass
class Comment:
    id: int
    t: float
    text: str
    kind: str  # ambient | chat | user | intro | system
    created: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "t": round(self.t, 2),
            "text": self.text,
            "kind": self.kind,
            "created": self.created,
        }


@dataclass
class WatchSession:
    id: str
    title: str
    source: str  # nas | local | external
    umo: str | None = None
    item: MediaItem | None = None
    plot: str = ""
    cues: list[Cue] = field(default_factory=list)
    subtitle_name: str = ""
    playback: str = "direct"  # direct | remux | unsupported | local | none
    duration: float | None = None
    position: float = 0.0
    playing: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    last_comment_pos: float = -1e9
    last_comment_wall: float = 0.0
    pending: bool = False
    fail_until: float = 0.0
    comments: list[Comment] = field(default_factory=list)
    next_id: int = 1
    history: list[str] = field(default_factory=list)
    frame: bytes | None = None
    frame_t: float = 0.0
    settings: dict = field(default_factory=dict)
    intro_done: bool = False
    ended: bool = False
    stream_offset: float = 0.0
    cues_loading: bool = False
    cues_tried: bool = False
    memory_hint: str = ""

    def info(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "movie_id": self.item.id if self.item else "",
            "playback": self.playback,
            "has_sub": bool(self.cues),
            "subtitle": self.subtitle_name,
            "plot": self.plot[:400],
            "poster": self.item.poster if self.item else "",
            "source_kind": self.item.source if self.item else "",
            "remote": bool(self.item and self.item.is_remote),
            "duration": self.duration,
            "position": round(self.position, 2),
            "playing": self.playing,
            "ended": self.ended,
            "settings": dict(self.settings),
            "linked_chat": bool(self.umo),
        }


class WatchEngine:
    def __init__(
        self,
        library: MediaLibrary,
        config_getter: ConfigGetter,
        llm_call: LLMCall | None = None,
        push_comment: PushCall | None = None,
        logger=None,
        submit: Callable[[Awaitable[Any]], Any] | None = None,
        ffmpeg_ok: bool = False,
        subtitle_fetcher: Callable[[Any], Awaitable[str]] | None = None,
        memory_provider: Callable[[str | None, str], str] | None = None,
        on_session_end: Callable[["WatchSession"], None] | None = None,
    ) -> None:
        self.library = library
        self._config_getter = config_getter
        self.llm_call = llm_call
        self.push_comment = push_comment
        self.logger = logger
        self._submit = submit
        self.ffmpeg_ok = ffmpeg_ok
        self.subtitle_fetcher = subtitle_fetcher
        self.memory_provider = memory_provider
        self.on_session_end = on_session_end
        self._lock = threading.RLock()
        self._sessions: dict[str, WatchSession] = {}
        self._subtitle_cache: dict[str, tuple[float, list[Cue]]] = {}

    # ------------------------------------------------------------------ 基础
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            cfg = self._config_getter() or {}
            value = cfg.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def set_submit(self, submit) -> None:
        self._submit = submit

    def _spawn(self, coro) -> None:
        if self._submit is None:
            coro.close()
            if self.logger:
                self.logger.warning("[watch-together] 没有可用的 asyncio 调度器，跳过 AI 生成")
            return
        try:
            self._submit(coro)
        except Exception as exc:  # noqa: BLE001
            coro.close()
            if self.logger:
                self.logger.warning(f"[watch-together] 调度 AI 任务失败: {exc}")

    # ------------------------------------------------------------------ 字幕
    def load_subtitle_file(self, path: Path | str | None) -> tuple[list[Cue], str]:
        if not path:
            return [], ""
        path = Path(path)
        try:
            stat = path.stat()
        except OSError:
            return [], ""
        key = str(path)
        with self._lock:
            cached = self._subtitle_cache.get(key)
            if cached and cached[0] == stat.st_mtime:
                return cached[1], path.name
        try:
            text = decode_subtitle_bytes(path.read_bytes())
        except OSError:
            return [], ""
        cues = parse_subtitle(text, path.suffix)
        with self._lock:
            self._subtitle_cache[key] = (stat.st_mtime, cues)
        return cues, path.name

    def attach_subtitle_text(self, session: WatchSession, text: str, name: str = "手动字幕") -> int:
        cues = parse_subtitle(text, Path(name).suffix or ".srt")
        with self._lock:
            session.cues = cues
            session.subtitle_name = name
        return len(cues)

    async def _ensure_cues(self, session: WatchSession) -> None:
        """本地没有外挂字幕时，按需提取内嵌字幕或从媒体服务器下载一次。"""

        if session.cues or session.item is None or self.subtitle_fetcher is None:
            return
        with self._lock:
            if session.cues_loading or session.cues_tried:
                return
            session.cues_loading = True
            session.cues_tried = True
        try:
            text = await self.subtitle_fetcher(session.item)
            if text:
                name = (
                    "媒体服务器字幕"
                    if session.item is not None and session.item.remote_sub_index is not None
                    else "内嵌字幕"
                )
                count = self.attach_subtitle_text(session, text, name)
                if self.logger:
                    self.logger.info(f"[watch-together] 已获取字幕（{count} 条）")
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                session.cues_tried = False
            if self.logger:
                self.logger.warning(f"[watch-together] 获取字幕失败：{exc}")
        finally:
            with self._lock:
                session.cues_loading = False

    def schedule_fetch_subtitles(self, session: WatchSession) -> None:
        self._spawn(self._ensure_cues(session))

    # ------------------------------------------------------------------ 会话
    def playback_kind(self, item: MediaItem | None) -> str:
        if item is None:
            return "none"
        local_ok = item.local_path_ok and bool(item.path) and item.path.is_file()
        if not local_ok:
            return "remote" if item.remote_stream else "unsupported"
        if item.ext.lower() in BROWSER_EXTS:
            return "direct"
        if self.ffmpeg_ok and str(self._cfg("ffmpeg_mode", "off")) != "off":
            return "remux"
        return "unsupported"

    def create_session(
        self,
        *,
        item: MediaItem | None = None,
        source: str = "nas",
        title: str | None = None,
        umo: str | None = None,
        duration: float | None = None,
    ) -> WatchSession:
        self.purge()
        cues: list[Cue] = []
        subtitle_name = ""
        if item is not None and item.subtitle:
            cues, subtitle_name = self.load_subtitle_file(item.subtitle)

        now = time.time()
        session = WatchSession(
            id=uuid.uuid4().hex[:12],
            title=(title or (item.title if item else "未命名影片")),
            source=source,
            umo=umo,
            item=item,
            plot=(item.plot if item else ""),
            cues=cues,
            subtitle_name=subtitle_name,
            playback=("local" if source == "local" else self.playback_kind(item)),
            duration=duration,
            created_at=now,
            updated_at=now,
            last_comment_pos=0.0,
        )
        session.settings = {
            "interval": int(self._cfg("comment_interval", 90) or 90),
            "vision": bool(self._cfg("vision_enabled", True)),
        }
        if self.memory_provider is not None:
            try:
                session.memory_hint = self.memory_provider(umo, session.title) or ""
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning(f"[watch-together] 读取长期记忆失败：{exc}")
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> WatchSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def active_for_umo(self, umo: str | None) -> WatchSession | None:
        if not umo:
            return None
        with self._lock:
            candidates = [s for s in self._sessions.values() if s.umo == umo and not s.ended]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.updated_at)

    def all_sessions(self) -> list[WatchSession]:
        with self._lock:
            return list(self._sessions.values())

    def close(self, session_id: str) -> bool:
        session = None
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False
            session.ended = True
            session.playing = False
            session.updated_at = time.time()
        if self.on_session_end is not None:
            try:
                self.on_session_end(session)
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning(f"[watch-together] 写入观影记忆失败：{exc}")
        return True

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            for session in sessions:
                session.ended = True
                session.playing = False
            self._sessions.clear()
        if self.on_session_end is not None:
            for session in sessions:
                if not session.comments:
                    continue
                try:
                    self.on_session_end(session)
                except Exception:  # noqa: BLE001
                    pass

    def purge(self, max_idle: int = 1800) -> None:
        now = time.time()
        with self._lock:
            stale = [
                sid
                for sid, s in self._sessions.items()
                if (s.ended and now - s.updated_at > 120)
                or (not s.ended and now - s.updated_at > max_idle)
            ]
            for sid in stale:
                self._sessions.pop(sid, None)

    # ------------------------------------------------------------------ 播放同步
    def sync(
        self,
        session_id: str,
        position: float,
        playing: bool,
        duration: float | None = None,
    ) -> dict:
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "error": "session_not_found"}
            if session.ended:
                return {"ok": True, "ended": True, "position": session.position}

            try:
                position = max(0.0, float(position))
            except (TypeError, ValueError):
                position = session.position
            jump = abs(position - session.position)
            session.position = position
            session.playing = bool(playing)
            session.updated_at = now
            if duration and duration > 0 and not session.duration:
                session.duration = float(duration)
            if jump > 25:
                # 用户拖动进度条：重置节奏，别马上开口
                session.last_comment_pos = position
                session.frame = None

            should = self._should_generate(session, now)
            if should:
                session.pending = True
                session.last_comment_pos = position
                session.last_comment_wall = now
            payload = {
                "ok": True,
                "position": session.position,
                "pending": session.pending,
                "ended": session.ended,
            }
        if should:
            self._spawn(self._generate_ambient(session))
        return payload

    def _should_generate(self, session: WatchSession, now: float) -> bool:
        if session.ended or not session.playing or session.pending:
            return False
        if now < session.fail_until:
            return False
        if now - session.last_comment_wall < 8:
            return False
        if session.position < 12:
            return False
        interval = float(session.settings.get("interval") or self._cfg("comment_interval", 90))
        interval = max(20.0, min(interval, 900.0))
        if session.position - session.last_comment_pos < interval:
            return False
        # 既没有字幕也没有画面理解时，不要瞎编
        if not session.cues and not session.settings.get("vision"):
            return False
        return True

    # ------------------------------------------------------------------ 画面
    def set_frame(self, session_id: str, data: bytes, position: float) -> bool:
        if not data or len(data) > 4 * 1024 * 1024:
            return False
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.frame = data
            session.frame_t = float(position)
            return True

    # ------------------------------------------------------------------ AI
    def _frame_for(self, session: WatchSession) -> bytes | None:
        if not session.settings.get("vision"):
            return None
        if not session.frame:
            return None
        if abs(session.frame_t - session.position) > 45:
            return None
        return session.frame

    async def _generate_ambient(self, session: WatchSession) -> Comment | None:
        try:
            await self._ensure_cues(session)
            with self._lock:
                if session.ended:
                    return None
                position = session.position
                cues = list(session.cues)
                history = list(session.history)
                frame = self._frame_for(session)
                duration = session.duration
                title, plot = session.title, session.plot
            if not cues and frame is None:
                return None
            system, user = prompts.ambient_prompt(
                title=title,
                plot=plot,
                position=position,
                duration=duration,
                cues=cues,
                history=history,
                persona=str(self._cfg("persona_prompt", "") or ""),
                bot_name=str(self._cfg("bot_name", "TA") or "TA"),
                max_chars=int(self._cfg("comment_max_chars", 100) or 100),
                has_image=frame is not None,
            )
            raw = await self._call_llm(session, system, user, frame)
            text = prompts.clean_reply(raw, int(self._cfg("comment_max_chars", 100) or 100))
            if not text:
                return None
            comment = self._append(session, text, "ambient", position)
            await self._maybe_push(session, text, position)
            return comment
        except Exception as exc:  # noqa: BLE001 - 单次失败不影响整场观影
            with self._lock:
                session.fail_until = time.time() + 60
            if self.logger:
                self.logger.warning(f"[watch-together] 生成点评失败: {exc}")
            return None
        finally:
            with self._lock:
                session.pending = False

    async def intro(self, session: WatchSession) -> Comment | None:
        with self._lock:
            if session.intro_done or session.ended:
                return None
            session.intro_done = True
            title, plot = session.title, session.plot
        if not bool(self._cfg("intro_enabled", True)):
            return None
        await self._ensure_cues(session)
        try:
            system, user = prompts.intro_prompt(
                title=title,
                plot=plot,
                persona=str(self._cfg("persona_prompt", "") or ""),
                bot_name=str(self._cfg("bot_name", "TA") or "TA"),
                memories=session.memory_hint,
            )
            raw = await self._call_llm(session, system, user, None)
            text = prompts.clean_reply(raw, 120)
            if not text:
                return None
            comment = self._append(session, text, "intro", session.position)
            await self._maybe_push(session, text, session.position)
            return comment
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[watch-together] 生成开场白失败: {exc}")
            return None

    async def chat(self, session: WatchSession, user_text: str) -> str:
        """用户在观影途中说话。返回 AI 的回复文本（同时写入点评流）。"""

        user_text = (user_text or "").strip()
        if not user_text:
            return ""
        if session.ended:
            return ""
        await self._ensure_cues(session)
        self._append(session, user_text, "user", session.position)
        with self._lock:
            position = session.position
            cues = list(session.cues)
            history = list(session.history)
            frame = self._frame_for(session)
            duration = session.duration
            title, plot = session.title, session.plot
            session.pending = True
        try:
            system, user = prompts.chat_prompt(
                title=title,
                plot=plot,
                position=position,
                duration=duration,
                cues=cues,
                history=history,
                persona=str(self._cfg("persona_prompt", "") or ""),
                bot_name=str(self._cfg("bot_name", "TA") or "TA"),
                user_text=user_text,
                has_image=frame is not None,
                memories=session.memory_hint,
            )
            raw = await self._call_llm(session, system, user, frame)
            text = prompts.clean_reply(raw, 300) or "（想了想，没说话）"
            self._append(session, text, "chat", position)
            return text
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[watch-together] 对话失败: {exc}")
            return "（刚才走神了…你再说一遍？）"
        finally:
            with self._lock:
                session.pending = False

    def schedule_intro(self, session: WatchSession) -> None:
        """让开场白在事件循环里生成（HTTP 线程不能直接 await）。"""

        self._spawn(self.intro(session))

    def schedule_chat(self, session: WatchSession, user_text: str) -> None:
        self._spawn(self.chat(session, user_text))

    async def _call_llm(
        self, session: WatchSession, system: str, user: str, frame: bytes | None
    ) -> str:
        if self.llm_call is None:
            raise RuntimeError("未配置大模型调用")
        timeout = float(self._cfg("llm_timeout", 90) or 90)
        return await asyncio.wait_for(self.llm_call(session, system, user, frame), timeout=timeout)

    def _append(
        self, session: WatchSession, text: str, kind: str, position: float
    ) -> Comment:
        with self._lock:
            comment = Comment(
                id=session.next_id,
                t=max(0.0, float(position)),
                text=text,
                kind=kind,
                created=time.time(),
            )
            session.next_id += 1
            session.comments.append(comment)
            session.updated_at = time.time()
            if kind in ("ambient", "chat", "intro"):
                session.history.append(text)
                session.history = session.history[-8:]
        return comment

    async def _maybe_push(self, session: WatchSession, text: str, position: float) -> None:
        if self.push_comment is None or not session.umo:
            return
        if not bool(self._cfg("push_comments_to_chat", True)):
            return
        try:
            await self.push_comment(session, text, position)
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[watch-together] 推送点评到聊天失败: {exc}")

    # ------------------------------------------------------------------ 读取
    def events(self, session_id: str, after: int = 0) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "error": "session_not_found"}
            fresh = [c.to_dict() for c in session.comments if c.id > max(0, int(after))]
            last_id = session.comments[-1].id if session.comments else 0
            return {
                "ok": True,
                "comments": fresh,
                "last_id": last_id,
                "pending": session.pending,
                "position": round(session.position, 2),
                "playing": session.playing,
                "duration": session.duration,
                "ended": session.ended,
                "has_sub": bool(session.cues),
            }

    def update_settings(self, session_id: str, payload: dict) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "error": "session_not_found"}
            if "interval" in payload:
                try:
                    session.settings["interval"] = max(20, min(600, int(payload["interval"])))
                except (TypeError, ValueError):
                    pass
            if "vision" in payload:
                session.settings["vision"] = bool(payload["vision"])
            return {"ok": True, "settings": dict(session.settings)}

    def stats(self) -> dict:
        with self._lock:
            active = [s for s in self._sessions.values() if not s.ended]
            return {
                "sessions": len(self._sessions),
                "active": len(active),
                "comments": sum(len(s.comments) for s in self._sessions.values()),
            }
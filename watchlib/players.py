"""局域网播放识别。

AstrBot 在 NAS 上，播放发生在别的设备上——插件本身看不到播放器进度。
能做到"自动识别"的只有两类播放器/服务：

* **Jellyfin / Emby**（含绿联等兼容实现）：服务器知道每个客户端的
  ``NowPlayingItem`` 和 ``PlayState.PositionTicks``，轮询 ``/Sessions`` 即可；
* **VLC**：开启 Web 界面（HTTP）后，``/requests/status.json`` 里有
  当前文件、进度和播放状态。

识别到之后会自动创建一场观影、跟着进度生成点评。PotPlayer、系统播放器
这类没有网络接口的播放器无法被识别，只能用网页里的"外部播放器"手动同步。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = ["Playback", "PlayerWatcher"]

STALE_SECONDS = 180


@dataclass
class Playback:
    key: str
    source: str
    title: str
    path: str
    position: float
    playing: bool
    device: str
    remote_id: str = ""

    def to_public(self) -> dict:
        return {
            "key": self.key,
            "source": self.source,
            "title": self.title,
            "device": self.device,
            "position": round(self.position, 1),
            "playing": self.playing,
        }


class PlayerWatcher:
    def __init__(
        self,
        engine,
        media_server,
        config_getter: Callable[[], Any],
        logger=None,
    ) -> None:
        self.engine = engine
        self.media_server = media_server
        self._config_getter = config_getter
        self.logger = logger
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, str] = {}
        self._last_seen: dict[str, float] = {}
        self._states: dict[str, Playback] = {}
        self._error = ""
        self._last_poll = 0.0

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default=None):
        try:
            value = (self._config_getter() or {}).get(key)
        except Exception:  # noqa: BLE001
            return default
        return default if value in (None, "") else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("auto_watch_enabled", False))

    @property
    def vlc_url(self) -> str:
        return str(self._cfg("vlc_url", "") or "").strip()

    def _interval(self) -> int:
        try:
            return max(5, int(self._cfg("auto_watch_interval", 10) or 10))
        except (TypeError, ValueError):
            return 10

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="watch-together-players", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.enabled:
                try:
                    self.poll_once()
                except Exception as exc:  # noqa: BLE001
                    self._error = str(exc)
                    if self.logger:
                        self.logger.warning(f"[watch-together] 自动识别播放失败：{exc}")
            self._stop.wait(self._interval())

    # ------------------------------------------------------------------ 轮询
    def poll_once(self) -> list[dict]:
        states: list[Playback] = []
        errors: list[str] = []
        if self.media_server is not None and self.media_server.enabled:
            try:
                states.extend(self._poll_media_server())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"媒体服务器：{exc}")
        if self.vlc_url:
            try:
                states.extend(self._poll_vlc())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"VLC：{exc}")

        now = time.time()
        with self._lock:
            self._error = "；".join(errors)
            self._last_poll = now
            seen = set()
            for state in states:
                item = self._match(state)
                if item is None:
                    continue
                seen.add(state.key)
                self._states[state.key] = state
                self._apply(state, item)

            # 停止播放很久的场次收尾
            for key in list(self._states):
                if key in seen:
                    continue
                if now - self._last_seen.get(key, now) > STALE_SECONDS:
                    session_id = self._sessions.pop(key, None)
                    self._states.pop(key, None)
                    self._last_seen.pop(key, None)
                    if session_id:
                        self.engine.close(session_id)

        return [state.to_public() for state in states]

    # ------------------------------------------------------------------ 来源
    def _poll_media_server(self) -> list[Playback]:
        client = self.media_server.client()
        sessions = client.active_sessions()
        states = []
        for session in sessions:
            item = session.get("NowPlayingItem") or {}
            if not item:
                continue
            play_state = session.get("PlayState") or {}
            position = float(play_state.get("PositionTicks") or 0) / 10_000_000
            states.append(
                Playback(
                    key=f"ms:{session.get('Id') or item.get('Id')}",
                    source=self.media_server.kind,
                    title=str(item.get("Name") or "正在播放"),
                    path=str(item.get("Path") or ""),
                    position=position,
                    playing=not bool(play_state.get("IsPaused")),
                    device=str(session.get("DeviceName") or session.get("Client") or "局域网设备"),
                    remote_id=str(item.get("Id") or ""),
                )
            )
        return states

    def _poll_vlc(self) -> list[Playback]:
        raw = self.vlc_url
        if not raw.startswith(("http://", "https://")):
            raw = "http://" + raw
        parsed = urllib.parse.urlparse(raw)
        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8080
        base = f"{parsed.scheme}://{host}:{port}"
        url = base + "/requests/status.json"
        request = urllib.request.Request(url)
        if username:
            import base64

            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8", "replace") or "{}")
        except (urllib.error.URLError, ValueError, OSError) as exc:
            raise RuntimeError(f"连接 VLC 失败：{exc}") from exc

        meta = ((data.get("information") or {}).get("category") or {}).get("meta") or {}
        filename = str(meta.get("filename") or meta.get("title") or "")
        state = str(data.get("state") or "")
        if not filename or state not in ("playing", "paused"):
            return []
        try:
            position = float(data.get("time") or 0)
        except (TypeError, ValueError):
            position = 0.0
        return [
            Playback(
                key=f"vlc:{filename}",
                source="vlc",
                title=Path(filename).stem,
                path=filename,
                position=position,
                playing=state == "playing",
                device=f"VLC @ {host}",
            )
        ]

    # ------------------------------------------------------------------ 匹配
    def _match(self, state: Playback):
        items = self.engine.library.items()
        if state.remote_id:
            for item in items:
                if item.remote_id and item.remote_id == state.remote_id:
                    return item
        if state.path:
            normalized = os.path.normcase(os.path.abspath(state.path))
            for item in items:
                if item.path and os.path.normcase(os.path.abspath(str(item.path))) == normalized:
                    return item
            basename = Path(state.path).name.lower()
            for item in items:
                if item.path and item.path.name.lower() == basename:
                    return item
        title = (state.title or "").strip().lower()
        if title:
            for item in items:
                if item.title.lower() == title:
                    return item
            for item in items:
                if title and title in item.title.lower():
                    return item
        return None

    def _apply(self, state: Playback, item) -> None:
        session_id = self._sessions.get(state.key)
        session = self.engine.get(session_id) if session_id else None
        if session is None or session.ended:
            umo = str(self._cfg("auto_watch_umo", "") or "").strip() or None
            session = self.engine.create_session(item=item, source="nas", umo=umo)
            self._sessions[state.key] = session.id
            self.engine.schedule_intro(session)
            if self.logger:
                self.logger.info(
                    f"[watch-together] 自动识别到播放：{item.title}（{state.device}）"
                )
        self._last_seen[state.key] = time.time()
        self.engine.sync(session.id, state.position, state.playing)

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "vlc": bool(self.vlc_url),
                "error": self._error,
                "last_poll": self._last_poll,
                "playing": [state.to_public() for state in self._states.values()],
            }
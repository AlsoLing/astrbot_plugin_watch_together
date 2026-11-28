"""长期记忆：本地观影记忆 + 适配其他记忆插件。

本地部分保证"即装即用"：每次看完电影会写一条摘要，下次开场白/聊天会带上
"你们之前一起看过什么"，不依赖任何第三方插件。

外部部分是一个"尽力而为"的桥：在运行时找到已加载的记忆插件（默认
``astrbot_plugin_sugarmemory_clean``），如果它暴露了常见的保存方法
（add_memory / save_memory / remember ...），就把观影摘要写进去；
也支持配置一个 HTTP webhook。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = ["WatchMemory", "MemoryBridge"]

SAVE_METHODS = (
    "add_memory",
    "save_memory",
    "store_memory",
    "remember",
    "memorize",
    "add_memory_entry",
)


class WatchMemory:
    """极简的本地观影记忆（JSON，按条数裁剪）。"""

    def __init__(self, path: Path, limit: int = 300) -> None:
        self.path = Path(path)
        self.limit = max(20, int(limit or 300))
        self._lock = threading.RLock()
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._items[-self.limit :], ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def add(self, entry: dict) -> None:
        with self._lock:
            self._items.append(entry)
            self._items = self._items[-self.limit :]
            self._save()

    def recent(self, umo: str | None = None, n: int = 5) -> list[dict]:
        with self._lock:
            items = list(self._items)
        if umo:
            same = [item for item in items if item.get("umo") == umo]
            if same:
                items = same
        items.sort(key=lambda item: float(item.get("ts") or 0))
        return items[-n:]

    def render(self, umo: str | None = None, n: int = 3) -> str:
        lines = []
        for item in self.recent(umo=umo, n=n):
            title = str(item.get("title") or "一部电影")
            try:
                day = time.strftime("%Y-%m-%d", time.localtime(float(item.get("ts") or 0)))
            except (ValueError, OSError):
                day = ""
            bits = [f"{day}《{title}》" if day else f"《{title}》"]
            position = item.get("position")
            if position:
                bits.append(f"看到 {_clock(position)}")
            said = [str(text)[:60] for text in (item.get("comments") or [])[-2:] if text]
            if said:
                bits.append("我说过：" + "／".join(said))
            user_said = [str(text)[:40] for text in (item.get("user_lines") or [])[-2:] if text]
            if user_said:
                bits.append("你说过：" + "／".join(user_said))
            lines.append("- " + "；".join(bits))
        return "\n".join(lines)


def _clock(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _flatten(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(_flatten(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten(item))
        return result
    return [value]


class MemoryBridge:
    """把观影摘要写进第三方记忆插件（尽力而为）。"""

    def __init__(
        self,
        context,
        plugin_name: str = "astrbot_plugin_sugarmemory_clean",
        logger=None,
        submit: Callable[[Any], Any] | None = None,
        webhook: str = "",
    ) -> None:
        self.context = context
        self.plugin_name = (plugin_name or "").strip()
        self.logger = logger
        self._submit = submit
        self.webhook = (webhook or "").strip()
        self._target: tuple[Any, str, Callable] | None = None
        self._discovered = False

    # ------------------------------------------------------------------ 发现
    def _candidates(self) -> Iterable[Any]:
        context = self.context
        getter = getattr(context, "get_registered_stars", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                for item in _flatten(getter()):
                    yield item
        manager = getattr(context, "star_manager", None) or getattr(context, "_star_manager", None)
        if manager is not None:
            for attr in ("star_map", "star_insts", "inst_map", "stars", "plugins"):
                with contextlib.suppress(Exception):
                    for item in _flatten(getattr(manager, attr, None)):
                        yield item
                        for field in ("star_cls", "star_instance", "instance", "inst", "plugin"):
                            nested = getattr(item, field, None)
                            if nested is not None and not inspect.isclass(nested):
                                yield nested
        import sys

        for name, module in list(sys.modules.items()):
            if self.plugin_name and (name == self.plugin_name or name.startswith(self.plugin_name + ".")):
                yield module

    def discover(self) -> list[str]:
        """找出可用的保存方法，返回 ``对象名.方法名`` 列表（用于日志/诊断）。"""

        found: list[str] = []
        if self._target is not None:
            obj, name, _ = self._target
            return [f"{type(obj).__name__}.{name}"]
        for obj in self._candidates():
            if isinstance(obj, (str, bytes, list, dict, set, tuple)):
                continue
            if inspect.isclass(obj):
                continue
            for name in SAVE_METHODS:
                fn = getattr(obj, name, None)
                if callable(fn) and not inspect.isclass(obj):
                    found.append(f"{type(obj).__name__}.{name}")
                    if self._target is None:
                        self._target = (obj, name, fn)
        self._discovered = True
        return found

    # ------------------------------------------------------------------ 写入
    def remember(self, text: str, umo: str = "") -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if self.webhook:
            threading.Thread(
                target=self._post_webhook, args=(text, umo), name="watch-together-memory", daemon=True
            ).start()
            return True
        target = self._target
        if target is None and not self._discovered:
            self.discover()
            target = self._target
        if target is None:
            return False
        obj, name, fn = target
        attempts = (
            lambda: fn(text),
            lambda: fn(text, umo=umo),
            lambda: fn(umo, text),
            lambda: fn(text=text),
            lambda: fn(content=text),
        )
        for attempt in attempts:
            try:
                result = attempt()
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning(f"[watch-together] 调用记忆插件 {name} 失败：{exc}")
                continue
            if inspect.isawaitable(result):
                if self._submit is not None:
                    with contextlib.suppress(Exception):
                        self._submit(result)
                else:
                    with contextlib.suppress(Exception):
                        asyncio.run(result)
            return True
        return False

    def _post_webhook(self, text: str, umo: str) -> None:
        try:
            payload = json.dumps({"content": text, "umo": umo, "source": "watch_together"}, ensure_ascii=False)
            request = urllib.request.Request(
                self.webhook,
                data=payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=8):
                pass
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[watch-together] 记忆 webhook 失败：{exc}")
"""串联 LivingMemory 长期记忆插件。

`astrbot_plugin_livingmemory`（LivingMemory）会把记忆整理成「文档 + 向量 + 图」，
并在插件实例上挂一个 ``MemoryEngine``。这里不去走它的 LLM 工具链，而是直接拿到
正在运行的那个引擎实例，复用它的 ``search_memories`` / ``add_memory``：

* 开场前读：把和「一起看电影」相关的旧记忆取出来，塞进提示词；
* 散场后写：把这次看的片子、说过的几句话整理成一条长期记忆写回去。

这样 AI 在观影室里说话时会真的"记得"以前看过什么，看完也会留下痕迹，不需要
用户在聊天里手动触发。拿不到插件实例时静默降级，不影响观影室本身。
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import inspect
import sys
import threading
import time
from typing import Any, Callable, Iterable

__all__ = ["LivingMemoryClient"]

# 检索词的公共后缀：LivingMemory 走的是语义检索，带上场景词比只搜片名更容易命中
_QUERY_SUFFIX = "一起看电影 观影 上次看过的电影 剧情 角色 感受 偏好"

# 只要这些方法存在，就认为它是一个可用的 MemoryEngine
_REQUIRED = ("search_memories", "add_memory")

_MAX_CHARS = 1200


class LivingMemoryClient:
    """LivingMemory 的轻量客户端（本机直连，不走 HTTP）。"""

    def __init__(
        self,
        context=None,
        config_getter: Callable[[], Any] | None = None,
        logger=None,
        submit: Callable[[Any], Any] | None = None,
        engine: Any | None = None,
    ) -> None:
        self.context = context
        self._config_getter = config_getter
        self.logger = logger
        self._submit = submit

        self._lock = threading.RLock()
        self._engine: Any = engine
        self._source = "injected" if engine is not None else ""
        self._checked_at = 0.0
        self._last_error = ""
        self._recalls = 0
        self._writes = 0
        self._last_recall = ""
        self._last_write_at = 0.0

    # ------------------------------------------------------------------ 配置
    def _conf(self, key: str, default: Any = None) -> Any:
        getter = self._config_getter
        if getter is None:
            return default
        try:
            value = (getter() or {}).get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    @property
    def mode(self) -> str:
        mode = str(self._conf("livingmemory_mode", "auto") or "auto").strip().lower()
        return mode if mode in ("auto", "off") else "auto"

    @property
    def enabled(self) -> bool:
        if self.mode == "off":
            return False
        return bool(self._conf("memory_enabled", True))

    @property
    def engine(self) -> Any | None:
        return self._find_engine()

    @property
    def available(self) -> bool:
        return self.enabled and self._find_engine() is not None

    def set_submit(self, submit: Callable[[Any], Any] | None) -> None:
        self._submit = submit

    # ------------------------------------------------------------------ 发现
    def _find_engine(self, force: bool = False) -> Any | None:
        with self._lock:
            if self._engine is not None:
                return self._engine
            if not self.enabled:
                return None
            now = time.time()
            # 插件是异步初始化的，别每次调用都全量扫描；20 秒后再试
            if not force and now - self._checked_at < 20:
                return None
            self._checked_at = now

        engine, source = _discover(self.context)
        if engine is not None:
            with self._lock:
                self._engine = engine
                self._source = source
            if self.logger:
                self.logger.info(
                    f"[watch-together] 已接上 LivingMemory 长期记忆（{source}），"
                    "开场前会读旧记忆，散场后写入新记忆"
                )
        return engine

    # ------------------------------------------------------------------ 读写
    async def recall(
        self,
        query: str,
        k: int = 5,
        umo: str | None = None,
        persona_id: str | None = None,
    ) -> list[dict]:
        """语义检索长期记忆。失败返回空列表。"""

        query = (query or "").strip()
        if not query or not self.enabled:
            return []
        engine = self._find_engine()
        if engine is None:
            return []
        try:
            kwargs: dict[str, Any] = {"query": query, "k": max(1, min(int(k), 20))}
            if umo:
                kwargs["session_id"] = umo
            if persona_id:
                kwargs["persona_id"] = persona_id
            result = _invoke(engine.search_memories, kwargs)
            memories = await _maybe_await(result)
        except Exception as exc:  # noqa: BLE001 - 记忆失败不影响看片
            self._record_error(f"检索失败：{exc}")
            return []

        items = _normalize(memories)
        with self._lock:
            self._recalls += 1
            if items:
                self._last_recall = items[0].get("content", "")[:200]
        return items

    async def recall_text(
        self,
        title: str = "",
        umo: str | None = None,
        k: int = 4,
        extra_query: str = "",
    ) -> str:
        """把旧记忆整理成可直接塞进提示词的几行文字。"""

        if not self.enabled:
            return ""
        query = " ".join(
            part
            for part in (str(title or "").strip(), str(extra_query or "").strip(), _QUERY_SUFFIX)
            if part
        )
        items = await self.recall(query, k=k, umo=umo)
        if not items:
            return ""
        lines: list[str] = []
        total = 0
        for item in items:
            text = str(item.get("content") or "").replace("\n", " ").strip()
            if not text:
                continue
            if total + len(text) > _MAX_CHARS:
                text = text[: max(40, _MAX_CHARS - total)]
            lines.append(f"- {text}")
            total += len(text)
            if total >= _MAX_CHARS:
                break
        return "\n".join(lines)

    async def remember(
        self,
        content: str,
        umo: str | None = None,
        topics: Iterable[str] | None = None,
        key_facts: Iterable[str] | None = None,
        importance: float = 0.6,
        source: str = "watch_together",
    ) -> bool:
        """写入一条长期记忆。返回是否真的写进去了。"""

        content = (content or "").strip()
        if not content or not self.enabled:
            return False
        engine = self._find_engine(force=True)
        if engine is None:
            return False

        metadata: dict[str, Any] = {
            "source": source,
            "external_client": "astrbot_plugin_watch_together",
            "create_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        topic_list = _clean_list(topics)
        fact_list = _clean_list(key_facts)
        if topic_list:
            metadata["topics"] = topic_list
        if fact_list:
            metadata["key_facts"] = fact_list

        payload: dict[str, Any] = {
            "content": content,
            "session_id": umo or None,
            "importance": _clamp(importance),
            "metadata": metadata,
        }
        try:
            result = _invoke(engine.add_memory, payload)
            memory_id = await _maybe_await(result)
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"写入失败：{exc}")
            return False

        with self._lock:
            self._writes += 1
            self._last_write_at = time.time()
        if self.logger:
            self.logger.info(
                f"[watch-together] 已写入长期记忆（id={memory_id}）：{content[:60]}"
            )
        return memory_id is not None

    def remember_soon(self, *args: Any, **kwargs: Any) -> bool:
        """给同步上下文用：把写入任务丢回 AstrBot 的事件循环。"""

        submit = self._submit
        if submit is None:
            return False
        try:
            submit(self.remember(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"调度写入失败：{exc}")
            return False
        return True

    # ------------------------------------------------------------------ 状态
    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        if self.logger:
            self.logger.warning(f"[watch-together] LivingMemory {message}")

    def status(self) -> dict:
        engine = self._find_engine()
        with self._lock:
            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "connected": engine is not None,
                "backend": self._source if engine is not None else "",
                "engine": type(engine).__name__ if engine is not None else "",
                "recalls": self._recalls,
                "writes": self._writes,
                "last_recall": self._last_recall,
                "error": self._last_error,
            }


# ---------------------------------------------------------------------- 工具


def _clean_list(value: Iterable[str] | None, limit: int = 5) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:80])
        if len(result) >= limit:
            break
    return result


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.6
    return max(0.0, min(1.0, number))


def _invoke(method: Callable, kwargs: dict[str, Any]) -> Any:
    """用一组关键字调用；签名不接受时逐项退化，兼容不同版本的 LivingMemory。"""

    attempts: list[dict[str, Any]] = [dict(kwargs)]
    if "persona_id" in kwargs:
        attempts.append({k: v for k, v in kwargs.items() if k != "persona_id"})
    if "session_id" in kwargs:
        attempts.append(
            {k: v for k, v in kwargs.items() if k not in ("session_id", "persona_id")}
        )
    if "metadata" in kwargs:
        attempts.append({"content": kwargs.get("content", "")})
    last: TypeError | None = None
    for attempt in attempts:
        try:
            return method(**attempt)
        except TypeError as exc:
            last = exc
            continue
    raise last or TypeError("调用记忆插件失败")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize(memories: Any) -> list[dict]:
    items: list[dict] = []
    for memory in memories or []:
        if isinstance(memory, dict):
            content = memory.get("content") or memory.get("text") or ""
            score = memory.get("score") or memory.get("final_score")
            metadata = memory.get("metadata") or {}
        else:
            content = getattr(memory, "content", "") or getattr(memory, "text", "")
            score = getattr(memory, "final_score", None)
            metadata = getattr(memory, "metadata", None) or {}
        text = str(content or "").strip()
        if not text:
            continue
        items.append(
            {
                "content": text,
                "score": _as_float(score),
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )
    items.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
    return items


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _discover(context) -> tuple[Any | None, str]:
    """在运行时找到 LivingMemory 的 MemoryEngine。"""

    for obj in _iter_objects(context):
        engine = _engine_from(obj)
        if engine is not None:
            return engine, f"{type(obj).__name__}.initializer.memory_engine"
    return None, ""


def _engine_from(obj: Any) -> Any | None:
    try:
        name = type(obj).__name__.lower()
        module = str(getattr(type(obj), "__module__", "") or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if "livingmemory" not in name and "livingmemory" not in module:
        return None

    initializer = getattr(obj, "initializer", None)
    engine = getattr(initializer, "memory_engine", None) if initializer is not None else None
    if engine is None:
        engine = getattr(obj, "memory_engine", None)
    if engine is None:
        engine = obj
    if not all(callable(getattr(engine, attr, None)) for attr in _REQUIRED):
        return None
    return engine


def _iter_objects(context) -> Iterable[Any]:
    """依次尝试：Star 管理器 → 已加载模块 → GC 兜底。"""

    seen: set[int] = set()

    def fresh(obj: Any) -> bool:
        if obj is None:
            return False
        try:
            oid = id(obj)
        except Exception:  # noqa: BLE001
            return False
        if oid in seen:
            return False
        seen.add(oid)
        return True

    # 1) AstrBot 的 Star 注册表
    for getter_name in ("get_registered_stars", "get_star_manager"):
        getter = getattr(context, getter_name, None)
        if not callable(getter):
            continue
        try:
            result = getter()
        except Exception:  # noqa: BLE001
            continue
        for item in _flatten(result):
            for candidate in _with_nested(item):
                if fresh(candidate):
                    yield candidate

    manager = getattr(context, "star_manager", None) or getattr(context, "_star_manager", None)
    if manager is not None:
        for attr in ("star_map", "star_insts", "inst_map", "stars", "plugins"):
            try:
                value = getattr(manager, attr, None)
            except Exception:  # noqa: BLE001
                continue
            for item in _flatten(value):
                for candidate in _with_nested(item):
                    if fresh(candidate):
                        yield candidate

    # 2) 已加载的模块里可能挂着插件单例
    for name, module in list(sys.modules.items()):
        if "livingmemory" not in name.lower() or module is None:
            continue
        for attr in ("plugin", "plugin_instance", "instance", "memory_engine", "engine"):
            try:
                candidate = getattr(module, attr, None)
            except Exception:  # noqa: BLE001
                continue
            if candidate is not None and not inspect.isclass(candidate) and fresh(candidate):
                yield candidate

    # 3) 最后手段：GC 里按类名找（只在本进程内，一次性，代价可接受）
    try:
        objects = gc.get_objects()
    except Exception:  # noqa: BLE001
        objects = []
    for obj in objects:
        try:
            if "livingmemory" not in type(obj).__name__.lower():
                continue
            if fresh(obj):
                yield obj
        except Exception:  # noqa: BLE001
            continue


def _with_nested(item: Any) -> Iterable[Any]:
    yield item
    for attr in ("star_cls", "star_instance", "instance", "inst", "plugin", "star"):
        try:
            nested = getattr(item, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if nested is not None and not inspect.isclass(nested):
            yield nested


def _flatten(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        result: list[Any] = []
        for item in value.values():
            result.extend(_flatten(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten(item))
        return result
    return [value]
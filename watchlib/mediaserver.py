"""媒体服务器接入（Jellyfin / Emby 协议）。

绿联（UGREEN）新版影视中心的「媒体服务器」目前只对合作播放器开放私有协议，
没有公开文档；但它运行在本机的 Jellyfin / Emby 是公开标准协议，可以直连。
本模块只用标准库实现最小可用的客户端：

* 拉取电影列表（标题、年份、简介、海报、原始路径、字幕流）；
* 生成海报 / 直连播放 / 字幕下载地址；
* 后台线程定时刷新，片库扫描时零阻塞。

任何网络异常都被收敛到 ``MediaServerError``，不会影响插件主流程。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .media import MediaItem, clean_title, find_local_subtitle
from .subtitles import decode_subtitle_bytes

__all__ = ["MediaServerClient", "MediaServerSource", "MediaServerError"]

DEFAULT_TIMEOUT = 8
DEFAULT_REFRESH = 300
TEXT_SUBTITLE_CODECS = {"srt", "subrip", "ass", "ssa", "webvtt", "vtt", "mov_text", "text"}


class MediaServerError(RuntimeError):
    """媒体服务器不可用 / 返回异常。"""


def _clean_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    return url


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MediaServerClient:
    """Jellyfin / Emby 的极简客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        kind: str = "jellyfin",
        user_id: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        username: str = "",
        password: str = "",
    ) -> None:
        self.base = _clean_base(base_url)
        self.api_key = (api_key or "").strip()
        self.kind = (kind or "jellyfin").lower()
        self.user_id = (user_id or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = max(2, _as_int(timeout, DEFAULT_TIMEOUT))
        self._token = ""

    # ------------------------------------------------------------------ HTTP
    @property
    def auth_token(self) -> str:
        return self.api_key or self._token

    def _base_headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.auth_token:
            headers["X-Emby-Token"] = self.auth_token
            headers["X-MediaBrowser-Token"] = self.auth_token
        return headers

    def _authentication_header(self) -> str:
        return (
            'MediaBrowser Client="AstrBot WatchTogether", Device="AstrBot", '
            'DeviceId="astrbot-watch-together", Version="1.1.0"'
        )

    def _request(
        self,
        path: str,
        params: dict | None = None,
        raw: bool = False,
        method: str = "GET",
        payload: dict | None = None,
        auth: bool = True,
    ):
        if not self.base:
            raise MediaServerError("没有填写媒体服务器地址")
        if auth and not self.auth_token:
            self.authenticate()
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = self._base_headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        request.add_header("X-Emby-Authorization", self._authentication_header())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data_bytes = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MediaServerError("鉴权失败，请检查 API Key 或账号密码") from exc
            raise MediaServerError(f"媒体服务器返回 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MediaServerError(f"连接失败：{exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise MediaServerError(f"连接失败：{exc}") from exc
        if raw:
            return data_bytes
        try:
            return json.loads(data_bytes.decode("utf-8", "replace"))
        except ValueError as exc:
            raise MediaServerError("媒体服务器返回的不是 JSON（地址或端口可能填错了）") from exc

    def authenticate(self) -> str:
        """用账号密码换 AccessToken（Emby / Jellyfin 通用）。"""

        if self.auth_token:
            return self.auth_token
        if not (self.username and self.password):
            raise MediaServerError("没有填写 API Key，也没有填写账号密码")
        result = self._request(
            "/Users/AuthenticateByName",
            method="POST",
            payload={"Username": self.username, "Pw": self.password},
            auth=False,
        )
        token = str((result or {}).get("AccessToken") or "")
        if not token:
            raise MediaServerError("登录失败：没有拿到 AccessToken")
        self._token = token
        user = (result or {}).get("User") or {}
        if not self.user_id and user.get("Id"):
            self.user_id = str(user["Id"])
        return token

    # ------------------------------------------------------------------ 接口
    def public_info(self) -> dict:
        return self._request("/System/Info/Public", auth=False)

    def resolve_user(self) -> str:
        if self.user_id:
            return self.user_id
        users = self._request("/Users")
        if not isinstance(users, list) or not users:
            raise MediaServerError("媒体服务器没有返回用户列表；请填写用户 ID")
        self.user_id = str(users[0].get("Id") or "")
        return self.user_id

    def fetch_movies(self, limit: int = 800) -> list[dict]:
        user_id = self.resolve_user()
        payload = self._request(
            f"/Users/{user_id}/Items",
            {
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path,Overview,ProductionYear,RunTimeTicks,MediaSources,MediaStreams,ImageTags",
                "ImageTypeLimit": "1",
                "EnableImageTypes": "Primary",
                "SortBy": "SortName",
                "Limit": str(max(1, limit)),
            },
        )
        items = payload.get("Items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise MediaServerError("片单格式不对（可能是非 Jellyfin/Emby 服务）")
        return [self._map_item(item) for item in items if isinstance(item, dict)]

    def active_sessions(self) -> list[dict]:
        """正在播放的客户端会话（Jellyfin / Emby 的 /Sessions）。"""

        data = self._request("/Sessions")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _map_item(item: dict) -> dict:
        sources = item.get("MediaSources") or []
        source = sources[0] if sources and isinstance(sources[0], dict) else {}
        streams = source.get("MediaStreams") or item.get("MediaStreams") or []
        subtitle = None
        for stream in streams:
            if not isinstance(stream, dict) or stream.get("Type") != "Subtitle":
                continue
            codec = str(stream.get("Codec") or "").lower()
            if stream.get("IsTextSubtitleStream") or codec in TEXT_SUBTITLE_CODECS:
                subtitle = stream
                break
        tags = item.get("ImageTags") or {}
        return {
            "id": str(item.get("Id") or ""),
            "title": str(item.get("Name") or "未命名"),
            "year": str(item.get("ProductionYear") or ""),
            "overview": str(item.get("Overview") or ""),
            "path": str(source.get("Path") or ""),
            "container": str(source.get("Container") or "").lower(),
            "runtime": round(_as_int(item.get("RunTimeTicks")) / 10_000_000),
            "source_id": str(source.get("Id") or item.get("Id") or ""),
            "image_tag": str(tags.get("Primary") or ""),
            "sub_index": subtitle.get("Index") if subtitle else None,
            "sub_codec": str(subtitle.get("Codec") or "") if subtitle else "",
        }

    # ------------------------------------------------------------------ 链接
    def image_url(self, item_id: str, tag: str = "", max_height: int = 400) -> str:
        query = {"maxHeight": str(max_height), "quality": "88"}
        if tag:
            query["tag"] = tag
        if self.auth_token:
            query["api_key"] = self.auth_token
        return f"{self.base}/Items/{item_id}/Images/Primary?{urllib.parse.urlencode(query)}"

    def stream_url(self, item_id: str, source_id: str = "") -> str:
        query = {"static": "true"}
        if source_id:
            query["mediaSourceId"] = source_id
        if self.auth_token:
            query["api_key"] = self.auth_token
        return f"{self.base}/Videos/{item_id}/stream?{urllib.parse.urlencode(query)}"

    def subtitle_url(self, item_id: str, source_id: str, index: int) -> str:
        query = {"api_key": self.auth_token} if self.auth_token else None
        url = f"{self.base}/Videos/{item_id}/{source_id}/Subtitles/{index}/Stream.srt"
        return url + (f"?{urllib.parse.urlencode(query)}" if query else "")

    def fetch_subtitle(self, item_id: str, source_id: str, index: int) -> str:
        data = self._request(
            f"/Videos/{item_id}/{source_id}/Subtitles/{index}/Stream.srt",
            {"api_key": self.auth_token} if self.auth_token else None,
            raw=True,
        )
        return decode_subtitle_bytes(data)


class MediaServerSource:
    """后台刷新媒体服务器片单，供 ``MediaLibrary`` 合并。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        logger=None,
        find_subtitle: Callable[[Path], tuple[Path | None, str]] = find_local_subtitle,
        local_items: Callable[[], Any] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._logger = logger
        self._find_subtitle = find_subtitle
        self._local_items = local_items
        self._lock = threading.RLock()
        self._items: list[MediaItem] = []
        self._error = ""
        self._updated = 0.0
        self._last_count = 0
        self._sub_probe_failed: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default=None):
        try:
            value = (self._config_getter() or {}).get(key)
        except Exception:  # noqa: BLE001
            return default
        return default if value in (None, "") else value

    @property
    def kind(self) -> str:
        return str(self._cfg("media_server_type", "off") or "off").lower()

    @property
    def enabled(self) -> bool:
        return self.kind in ("jellyfin", "emby") and bool(self._cfg("media_server_url"))

    def client(self) -> MediaServerClient:
        return MediaServerClient(
            base_url=str(self._cfg("media_server_url", "")),
            api_key=str(self._cfg("media_server_api_key", "") or ""),
            kind=self.kind,
            user_id=str(self._cfg("media_server_user_id", "") or ""),
            timeout=_as_int(self._cfg("media_server_timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT),
            username=str(self._cfg("media_server_username", "") or ""),
            password=str(self._cfg("media_server_password", "") or ""),
        )

    def _path_map(self) -> list[tuple[str, str]]:
        raw = str(self._cfg("media_server_path_map", "") or "")
        pairs = []
        for line in raw.replace(";", "\n").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            old, new = line.split(":", 1)
            old, new = old.strip(), new.strip()
            if old and new:
                pairs.append((old.rstrip("/"), new.rstrip("/")))
        return pairs

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="watch-together-media", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            ok = self.refresh_once()
            wait = _as_int(self._cfg("media_server_refresh", DEFAULT_REFRESH), DEFAULT_REFRESH)
            self._stop.wait(max(30, wait) if ok else 60)

    # ------------------------------------------------------------------ 刷新
    def refresh_once(self) -> bool:
        if not self.enabled:
            with self._lock:
                self._items = []
                self._last_count = 0
                self._error = ""
                self._updated = time.time()
            return True
        try:
            client = self.client()
            movies = client.fetch_movies()
            items = [self._to_item(movie, client) for movie in movies]
        except MediaServerError as exc:
            with self._lock:
                self._error = str(exc)
                self._updated = time.time()
            if self._logger:
                self._logger.warning(f"[watch-together] 媒体服务器刷新失败：{exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._updated = time.time()
            if self._logger:
                self._logger.warning(f"[watch-together] 媒体服务器刷新异常：{exc!r}")
            return False
        with self._lock:
            self._items = items
            self._last_count = len(items)
            self._error = ""
            self._updated = time.time()
            self._sub_probe_failed.clear()
        return True

    def _to_item(self, movie: dict, client: MediaServerClient) -> MediaItem:
        raw_path = movie.get("path") or ""
        mapped = raw_path
        for old, new in self._path_map():
            if raw_path.startswith(old):
                mapped = new + raw_path[len(old) :]
                break
        path = Path(mapped) if mapped else Path("")
        exists = bool(mapped) and path.is_file()

        subtitle, tag = (None, "")
        size = 0
        mtime = time.time()
        if exists:
            try:
                subtitle, tag = self._find_subtitle(path)
                stat = path.stat()
                size, mtime = stat.st_size, stat.st_mtime
            except OSError:
                subtitle, tag = None, ""
        else:
            # 媒体服务器的路径在 NAS 上，插件不一定能直接看到；
            # 用同名文件在本地片库里兜底，这样字幕/内嵌字幕仍然能读。
            local = self._match_local(movie, path)
            if local is not None and local.path and local.path.is_file():
                path = local.path
                exists = True
                subtitle, tag = local.subtitle, local.subtitle_tag
                if not subtitle:
                    try:
                        subtitle, tag = self._find_subtitle(path)
                    except OSError:
                        subtitle, tag = None, ""
                size = local.size
                mtime = local.mtime
                if self._logger:
                    self._logger.info(
                        f"[watch-together] 媒体服务器条目「{movie.get('title')}」"
                        f"匹配到本地文件：{path}"
                    )

        container = movie.get("container") or path.suffix.lstrip(".")
        ext = f".{container.lower()}" if container else path.suffix.lower()
        item_id = "ms" + hashlib.md5(
            f"{client.base}|{movie.get('id')}".encode("utf-8")
        ).hexdigest()[:10]

        return MediaItem(
            id=item_id,
            title=movie.get("title") or path.stem or "未命名",
            path=path,
            rel=path.name or movie.get("title") or "",
            root=client.base,
            ext=ext,
            size=size,
            mtime=mtime,
            subtitle=subtitle,
            subtitle_tag=tag,
            plot=movie.get("overview") or "",
            year=movie.get("year") or "",
            poster=client.image_url(movie.get("id") or "", movie.get("image_tag") or ""),
            source=self.kind,
            remote_id=movie.get("id") or "",
            remote_source_id=movie.get("source_id") or "",
            remote_stream=client.stream_url(movie.get("id") or "", movie.get("source_id") or ""),
            remote_sub_index=movie.get("sub_index"),
            local_path_ok=exists,
        )

    # ------------------------------------------------------------------ 匹配
    def _match_local(self, movie: dict, remote_path: Path) -> MediaItem | None:
        """媒体服务器条目 ↔ 本地片库里的同名文件。"""

        provider = self._local_items
        if provider is None:
            return None
        try:
            items = [item for item in (provider() or []) if getattr(item, "source", "") == "local"]
        except Exception:  # noqa: BLE001
            return None
        if not items:
            return None

        wanted_name = remote_path.name.lower() if remote_path.name else ""
        wanted_title = _norm_title(movie.get("title") or "")
        wanted_year = str(movie.get("year") or "").strip()
        if not wanted_name and not wanted_title:
            return None

        best: tuple[int, Any] | None = None
        for item in items:
            if not item.path:
                continue
            score = None
            if wanted_name and item.path.name.lower() == wanted_name:
                score = 0
            elif wanted_title and _norm_title(item.title) == wanted_title:
                if not wanted_year or not item.year or str(item.year) == wanted_year:
                    score = 1
            if score is None:
                continue
            if best is None or score < best[0]:
                best = (score, item)
        return best[1] if best else None

    # ------------------------------------------------------------------ 读取
    def items(self) -> list[MediaItem]:
        with self._lock:
            return list(self._items)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "kind": self.kind,
                "url": str(self._cfg("media_server_url", "") or ""),
                "count": len(self._items),
                "last_count": self._last_count,
                "error": self._error,
                "updated": self._updated,
            }

    def test_connection(self) -> dict:
        """给页面/命令用的连接测试。"""

        if self.kind in ("off", ""):
            raise MediaServerError("未启用媒体服务器")
        if not self._cfg("media_server_url"):
            raise MediaServerError("没有填写媒体服务器地址")
        client = self.client()
        info = client.public_info() or {}
        movies = client.fetch_movies(limit=3)
        return {
            "server": info.get("ServerName") or info.get("ProductName") or "媒体服务器",
            "version": info.get("Version") or "",
            "sample": [m.get("title") for m in movies],
        }

    def fetch_subtitle(self, item: MediaItem) -> str:
        """抓媒体服务器上的字幕。

        绿联 / 部分 Emby 变体在片单里不一定给出 Subtitle 流，``remote_sub_index``
        可能是空的。这里按「已知索引 → 0..7 逐个探测」兜底，拿到像字幕的文本就返回。
        """

        if item is None or not item.remote_id or item.remote_id in self._sub_probe_failed:
            return ""
        client = self.client()
        source_id = item.remote_source_id or item.remote_id

        indexes: list[int] = []
        if item.remote_sub_index is not None:
            indexes.append(int(item.remote_sub_index))
        indexes.extend(index for index in range(0, 8) if index not in indexes)

        for index in indexes[:6]:
            try:
                text = client.fetch_subtitle(item.remote_id, source_id, index)
            except MediaServerError as exc:
                if "鉴权" in str(exc):
                    raise
                continue
            except Exception:  # noqa: BLE001
                continue
            if _looks_like_subtitle(text):
                item.remote_sub_index = index
                return text

        with self._lock:
            self._sub_probe_failed.add(item.remote_id)
        return ""


def _looks_like_subtitle(text: str) -> bool:
    """媒体服务器找不到字幕时经常返回 404 的 HTML/空串，这里做个粗筛。"""

    text = (text or "").strip()
    if len(text) < 30:
        return False
    head = text[:4000]
    return "-->" in head or "[Script Info]" in head or "WEBVTT" in head


def _norm_title(title: str) -> str:
    """标题归一化：去掉标点/年份/分辨率标签，只留小写字母数字和汉字。"""

    cleaned = clean_title(str(title or ""))
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", cleaned.lower())
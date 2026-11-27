"""片库扫描：把 NAS 上的电影目录变成可以在网页里点播的列表。

不依赖 ffmpeg / 数据库，只做文件系统遍历 + 同名字幕 / nfo 识别。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

__all__ = ["MediaItem", "MediaLibrary", "VIDEO_EXTS", "BROWSER_EXTS", "clean_title", "find_local_subtitle"]

# 能被浏览器 <video> 直接播放的容器（编码仍需 H.264/H.265 + AAC 等常见组合）
BROWSER_EXTS = {".mp4", ".m4v", ".webm", ".ogv", ".ogg"}

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mts", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp", ".ogv",
}

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

SUB_DIR_NAMES = ("subs", "sub", "subtitles", "subtitle", "字幕")

SKIP_DIRS = {
    "@eadir", "#recycle", "lost+found", "$recycle.bin", "system volume information",
    "node_modules", "__pycache__", ".git", ".stfolder", ".sync", "@recycle",
}

_TAG_RE = re.compile(
    r"(?i)\b("
    r"2160p|1440p|1080p|1080i|720p|576p|480p|4k|8k|uhd|"
    r"blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|dvdrip|dvdscr|hdrip|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|av1|vp9|"
    r"aac|ac3|eac3|ddp?5\.1|dts(-hd)?|truehd|atmos|flac|mp3|"
    r"10bit|8bit|hdr10?|dolby|"
    r"proper|repack|extended|uncut|internal|limited|"
    r"chs|cht|eng|jpn|kor|chi|sc|tc|gb|big5|双语|中字|简繁|字幕"
    r")\b"
)
_GROUP_RE = re.compile(r"[\[\(【]([^\]\)】]{0,40})[\]\)】]")
_BRACKETED_RE = re.compile(r"[\[【][^\]】]*[\]】]")
_TAIL_GROUP_RE = re.compile(r"-[A-Za-z0-9_.]{2,12}$")


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def clean_title(filename: str) -> str:
    """把 ``Inception.2010.1080p.BluRay.x264-GROUP.mkv`` 变成 ``Inception 2010``。"""

    stem = Path(filename).stem
    name = _BRACKETED_RE.sub(" ", stem)
    name = name.replace("_", " ").replace(".", " ")
    name = _TAG_RE.sub(" ", name)
    name = _TAIL_GROUP_RE.sub("", name.strip())
    name = re.sub(r"\s{2,}", " ", name).strip(" -–—_")
    return name or Path(filename).stem


def _lang_score(tag: str) -> int:
    """字幕语言偏好：中文优先，其次未知，最后英文。"""

    tag = (tag or "").lower()
    if not tag:
        return 3
    if any(key in tag for key in ("zh", "chs", "cht", "chi", "sc", "tc", "cn", "gb", "big5", "中")):
        return 0
    if any(key in tag for key in ("eng", "en", "english")):
        return 8
    return 5


def _read_text(path: Path, limit: int = 512 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit)
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]{1,80}>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"\s+", " ", text).strip()


def read_nfo(path: Path) -> dict:
    """读取 Emby / Jellyfin / Kodi 同目录的 .nfo，拿到剧情简介喂给模型。"""

    text = _read_text(path)
    if not text:
        return {}
    info: dict = {}
    for key in ("title", "plot", "outline", "year", "tagline", "genre", "director", "runtime"):
        match = re.search(rf"<{key}[^>]*>(.*?)</{key}>", text, re.S | re.I)
        if match:
            value = _strip_tags(match.group(1)).replace("<![CDATA[", "").replace("]]>", "").strip()
            if value:
                info[key] = value[:600]
    if "plot" not in info and "outline" in info:
        info["plot"] = info["outline"]
    return info


@dataclass
class MediaItem:
    id: str
    title: str
    path: Path
    rel: str
    root: str
    ext: str
    size: int
    mtime: float
    subtitle: Path | None = None
    subtitle_tag: str = ""
    plot: str = ""
    year: str = ""
    genre: str = ""
    # --- 媒体服务器（Jellyfin / Emby）来源的额外信息 ---
    poster: str = ""
    source: str = "local"
    remote_id: str = ""
    remote_source_id: str = ""
    remote_stream: str = ""
    remote_sub_index: int | None = None
    local_path_ok: bool = True

    @property
    def playable_in_browser(self) -> bool:
        return self.ext.lower() in BROWSER_EXTS

    @property
    def is_remote(self) -> bool:
        return bool(self.remote_stream) and not self.local_path_ok

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "format": self.ext.lstrip(".").upper(),
            "size": self.size,
            "size_h": human_size(self.size) if self.size else "",
            "has_sub": bool(self.subtitle),
            "sub_tag": self.subtitle_tag,
            "plot": self.plot[:220],
            "year": self.year,
            "genre": self.genre,
            "poster": self.poster,
            "source": self.source,
            "remote": self.is_remote,
            "folder": Path(self.rel).parent.name if os.sep in self.rel else "",
            "mtime": self.mtime,
            "browser_ok": self.playable_in_browser,
        }

    def describe(self) -> str:
        bits = [self.ext.lstrip(".").upper() or "视频"]
        if self.size:
            bits.append(human_size(self.size))
        bits.append("有字幕" if self.subtitle else "无字幕")
        if self.source != "local":
            bits.append("媒体服务器")
        return " · ".join(bits)


def _pick_subtitle(video: Path, subs: Iterable[Path]) -> tuple[Path | None, str]:
    stem = video.stem
    best: tuple[int, int, str, Path] | None = None
    for sub in subs:
        sub_stem = sub.stem
        if sub_stem == stem:
            tag = ""
        elif sub_stem.lower().startswith(stem.lower() + "."):
            tag = sub_stem[len(stem) + 1 :]
        else:
            continue
        score = _lang_score(tag)
        # 分数相同则优先名字更短的（通常是主字幕）
        candidate = (score, len(sub.name), tag, sub)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is None:
        return None, ""
    return best[3], best[2]


def find_local_subtitle(video: Path) -> tuple[Path | None, str]:
    """在影片同目录（以及 Subs/ 字幕/ 子目录）里找同名字幕。"""

    candidates: list[Path] = []
    folder = Path(video).parent
    try:
        candidates.extend(p for p in folder.iterdir() if p.suffix.lower() in SUBTITLE_EXTS)
    except OSError:
        pass
    for sub_name in SUB_DIR_NAMES:
        sub_dir = folder / sub_name
        if not sub_dir.is_dir():
            continue
        try:
            candidates.extend(p for p in sub_dir.iterdir() if p.suffix.lower() in SUBTITLE_EXTS)
        except OSError:
            continue
    return _pick_subtitle(Path(video), candidates)


class MediaLibrary:
    """带缓存的片库。``dirs_provider`` 每次扫描时读取最新配置。"""

    def __init__(
        self,
        dirs_provider: Callable[[], Sequence[str]],
        refresh_seconds: int = 60,
        logger=None,
        remote_provider: Callable[[], Sequence[MediaItem]] | None = None,
    ) -> None:
        self._dirs_provider = dirs_provider
        self._remote_provider = remote_provider
        self.refresh_seconds = max(5, int(refresh_seconds or 60))
        self._logger = logger
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._items: dict[str, MediaItem] = {}
        self._errors: list[str] = []
        self._scanned_at = 0.0
        self._roots: list[str] = []

    # ------------------------------------------------------------------ 扫描
    def _iter_scan(self) -> None:
        items: dict[str, MediaItem] = {}
        errors: list[str] = []
        roots: list[str] = []
        for raw in self._dirs_provider() or []:
            root_text = str(raw or "").strip()
            if not root_text:
                continue
            root_path = Path(os.path.expandvars(os.path.expanduser(root_text)))
            if not root_path.is_dir():
                errors.append(f"目录不存在或不可读：{root_text}")
                continue
            roots.append(str(root_path))
            for dirpath, dirnames, filenames in os.walk(root_path, followlinks=True):
                dirnames[:] = sorted(
                    d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS
                )
                current = Path(dirpath)
                videos: list[Path] = []
                subs: list[Path] = []
                for filename in filenames:
                    if filename.startswith("."):
                        continue
                    candidate = current / filename
                    suffix = candidate.suffix.lower()
                    if suffix in VIDEO_EXTS:
                        videos.append(candidate)
                    elif suffix in SUBTITLE_EXTS:
                        subs.append(candidate)
                if not videos:
                    continue
                # 同目录下的 Subs / 字幕 子文件夹也纳入候选
                for sub_name in SUB_DIR_NAMES:
                    sub_dir = current / sub_name
                    if not sub_dir.is_dir():
                        continue
                    try:
                        extra = list(sub_dir.iterdir())
                    except OSError:
                        continue
                    subs.extend(p for p in extra if p.suffix.lower() in SUBTITLE_EXTS)

                for video in videos:
                    try:
                        stat = video.stat()
                    except OSError:
                        continue
                    subtitle, tag = _pick_subtitle(video, subs)
                    nfo = video.with_suffix(".nfo")
                    info = read_nfo(nfo) if nfo.is_file() else {}
                    rel = str(video.relative_to(root_path))
                    item_id = hashlib.md5(f"{root_path}|{rel}".encode("utf-8")).hexdigest()[:12]
                    items[item_id] = MediaItem(
                        id=item_id,
                        title=(info.get("title") or clean_title(video.name)),
                        path=video,
                        rel=rel,
                        root=str(root_path),
                        ext=video.suffix.lower(),
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        subtitle=subtitle,
                        subtitle_tag=tag,
                        plot=info.get("plot", ""),
                        year=info.get("year", ""),
                        genre=info.get("genre", ""),
                    )

        # 合并媒体服务器（Jellyfin / Emby）的片单
        if self._remote_provider is not None:
            try:
                for item in self._remote_provider() or []:
                    items[item.id] = item
            except Exception as exc:  # noqa: BLE001 - 远端失败不影响本地片库
                errors.append(f"媒体服务器：{exc}")

        with self._lock:
            self._items = items
            self._errors = errors
            self._roots = roots
            self._scanned_at = time.time()

    def scan(self, force: bool = False) -> None:
        with self._lock:
            fresh = (time.time() - self._scanned_at) < self.refresh_seconds
            if fresh and self._items and not force:
                return
        with self._scan_lock:
            # 并发请求只让一个线程真正遍历磁盘
            with self._lock:
                fresh = (time.time() - self._scanned_at) < self.refresh_seconds
                if fresh and self._items and not force:
                    return
            try:
                self._iter_scan()
            except Exception as exc:  # noqa: BLE001 - 扫描失败不能让插件挂掉
                if self._logger:
                    self._logger.warning(f"[watch-together] 扫描片库失败: {exc}")

    # ------------------------------------------------------------------ 查询
    def items(self, force: bool = False) -> list[MediaItem]:
        self.scan(force)
        with self._lock:
            return sorted(self._items.values(), key=lambda item: item.mtime, reverse=True)

    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def roots(self) -> list[str]:
        with self._lock:
            return list(self._roots)

    def get(self, item_id: str) -> MediaItem | None:
        with self._lock:
            return self._items.get(item_id)

    def search(self, query: str) -> list[MediaItem]:
        query = (query or "").strip().lower()
        if not query:
            return self.items()
        found = [
            item
            for item in self.items()
            if query in item.title.lower() or query in item.rel.lower()
        ]
        return found

    def resolve(self, argument: str) -> MediaItem | None:
        """按 1 起始序号 / id / 片名片段解析片库条目。"""

        argument = (argument or "").strip()
        if not argument:
            return None
        items = self.items()
        if argument.isdigit():
            index = int(argument) - 1
            if 0 <= index < len(items):
                return items[index]
            return None
        for item in items:
            if item.id == argument:
                return item
        matches = [item for item in items if argument.lower() in item.title.lower()]
        if not matches:
            matches = [item for item in items if argument.lower() in item.rel.lower()]
        return matches[0] if matches else None
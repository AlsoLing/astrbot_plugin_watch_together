"""内嵌字幕（软字幕）提取。

很多 MKV/MP4 把字幕作为独立轨道封装在文件里，字幕文件并不落地。
这里用 ffprobe 找到文本字幕流，再用 ffmpeg 转成 SRT：

    ffprobe -select_streams s -show_entries stream=index,codec_name:stream_tags=language
    ffmpeg  -i movie.mkv -map 0:<index> -c:s srt -f srt pipe:1

位图字幕（PGS / VobSub）没法直接转文本，会跳过——那种只能靠画面理解或 OCR。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .subtitles import decode_subtitle_bytes

__all__ = ["list_text_streams", "pick_stream", "extract_text", "extract_for", "find_ffprobe"]

TEXT_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "vtt", "text",
    "microdvd", "subviewer", "subviewer1", "sami", "realtext", "jacosub",
    "mpl2", "pjs", "stl", "vplayer", "webvtt",
}
BITMAP_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext",
}
ZH_LANGS = {
    "chi", "zho", "zh", "zh-cn", "zh-hans", "zh-hant", "chs", "cht", "sc", "tc",
    "cn", "chinese", "中文", "简体", "繁体", "中字",
}

_STREAM_RE = re.compile(
    r"Stream #0:(\d+)(?:\[[^\]]*\])?(?:\(([^)]*)\))?: Subtitle: ([A-Za-z0-9_]+)",
    re.I,
)
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


def find_ffprobe(ffmpeg: str | None) -> str | None:
    if ffmpeg:
        path = Path(ffmpeg)
        candidate = path.with_name("ffprobe" + path.suffix)
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffprobe")


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _probe_json(ffprobe: str, path: Path, timeout: float) -> list[dict]:
    cmd = [
        ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path),
    ]
    result = _run(cmd, timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[:200])
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    streams = []
    for item in data.get("streams", []):
        if str(item.get("codec_type") or "").lower() != "subtitle":
            continue
        tags = item.get("tags") or {}
        disposition = item.get("disposition") or {}
        streams.append(
            {
                "index": int(item.get("index")),
                "codec": str(item.get("codec_name") or "").lower(),
                "language": str(tags.get("language") or "").lower(),
                "title": str(tags.get("title") or ""),
                "default": bool(disposition.get("default")),
                "forced": bool(disposition.get("forced")),
            }
        )
    return streams


def _probe_text(ffmpeg: str, path: Path, timeout: float) -> list[dict]:
    result = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], timeout)
    text = result.stderr.decode("utf-8", "replace")
    streams = []
    for match in _STREAM_RE.finditer(text):
        index, lang, codec = match.groups()
        streams.append(
            {
                "index": int(index),
                "codec": codec.lower(),
                "language": (lang or "").lower(),
                "title": "",
                "default": False,
                "forced": False,
            }
        )
    return streams


def list_text_streams(path: str | Path, ffmpeg: str | None, timeout: float = 20) -> list[dict]:
    """列出文件里的字幕流；优先 ffprobe，失败时退回解析 ffmpeg 输出。"""

    path = Path(path)
    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            return _probe_json(ffprobe, path, timeout)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
            pass
    if ffmpeg:
        try:
            return _probe_text(ffmpeg, path, timeout)
        except (OSError, subprocess.SubprocessError):
            pass
    return []


def pick_stream(streams: list[dict]) -> dict | None:
    """挑一条最适合的字幕：中文优先、默认轨优先、非 forced 优先。"""

    best: tuple[int, dict] | None = None
    for stream in streams:
        codec = stream.get("codec") or ""
        if codec and codec not in TEXT_CODECS:
            continue
        score = 0
        if stream.get("language") in ZH_LANGS:
            score += 10
        if any(token in (stream.get("title") or "").lower() for token in ("chi", "中", "简", "繁")):
            score += 4
        if stream.get("default"):
            score += 2
        if stream.get("forced"):
            score -= 3
        if codec in ("subrip", "srt"):
            score += 1
        if best is None or score > best[0]:
            best = (score, stream)
    return best[1] if best else None


def extract_text(
    path: str | Path,
    stream_index: int,
    ffmpeg: str,
    timeout: float = 120,
) -> str:
    """把指定字幕流转成 SRT 文本。"""

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(path), "-map", f"0:{stream_index}",
        "-c:s", "srt", "-f", "srt", "pipe:1",
    ]
    result = _run(cmd, timeout)
    if result.returncode != 0 or not result.stdout:
        return ""
    return decode_subtitle_bytes(result.stdout)


def extract_for(path: str | Path, ffmpeg: str, timeout: float = 120) -> str:
    """完整流程（带 mtime 缓存）：找不到文本字幕返回空字符串。"""

    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    key = str(path.resolve())
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

    streams = list_text_streams(path, ffmpeg)
    best = pick_stream(streams)
    if best is None:
        with _CACHE_LOCK:
            _CACHE[key] = (mtime, "")
        return ""
    text = extract_text(path, int(best["index"]), ffmpeg, timeout)
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, text)
    return text
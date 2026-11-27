"""字幕解析工具。

把 SRT / WebVTT / ASS(SSA) 统一解析成 ``Cue`` 时间轴，供"AI 陪你看电影"
在任意播放进度取出"刚刚说了什么"，作为大模型的上下文。

只依赖标准库，方便在 NAS 上的 AstrBot 容器里直接运行。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "Cue",
    "decode_subtitle_bytes",
    "parse_subtitle",
    "cues_in_window",
    "build_transcript",
    "format_timestamp",
    "to_webvtt",
    "guess_extension",
]

_TS_FULL = re.compile(r"(\d{1,3}):(\d{1,2}):(\d{1,2})[.,](\d{1,3})")
_TS_SHORT = re.compile(r"(\d{1,2}):(\d{1,2})[.,](\d{1,3})")
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]{1,120}>")
_SPACES = re.compile(r"[ \t\u3000]+")
_LINE_SPLIT = re.compile(r"\r\n|\r|\n")

SUBTITLE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp1252")


@dataclass
class Cue:
    """一条字幕。``start`` / ``end`` 单位为秒。"""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"s": round(self.start, 3), "e": round(self.end, 3), "t": self.text}


def decode_subtitle_bytes(data: bytes) -> str:
    """尽力解码字幕文件（中文资源常见 GBK/GB18030 编码）。"""

    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for enc in SUBTITLE_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def guess_extension(filename: str) -> str:
    name = (filename or "").lower().rsplit(".", 1)
    return "." + name[-1] if len(name) == 2 else ""


def _seconds(match: re.Match[str]) -> float:
    groups = match.groups()
    if len(groups) == 4:
        hours, minutes, seconds, millis = groups
    else:  # mm:ss.mmm
        hours = 0
        minutes, seconds, millis = groups
    frac = float("0." + millis.ljust(3, "0")[:3])
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + frac


def _parse_time(text: str) -> float | None:
    match = _TS_FULL.search(text)
    if match:
        return _seconds(match)
    match = _TS_SHORT.search(text)
    if match:
        return _seconds(match)
    return None


def _clean_text(text: str) -> str:
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    text = _ASS_OVERRIDE.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = _SPACES.sub(" ", text)
    return text.strip(" \t-—")


def _finalize(cues: Iterable[Cue]) -> list[Cue]:
    """排序、去掉空字幕、合并完全重叠的重复行。"""

    result: list[Cue] = []
    for cue in cues:
        if not cue.text or cue.end <= cue.start:
            continue
        if result and cue.text == result[-1].text and abs(cue.start - result[-1].start) < 0.35:
            result[-1].end = max(result[-1].end, cue.end)
            continue
        result.append(cue)
    result.sort(key=lambda c: c.start)
    return result


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\ufeff", "").strip())
    for block in blocks:
        lines = [ln for ln in _LINE_SPLIT.split(block) if ln.strip()]
        if not lines:
            continue
        time_index = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if time_index < 0:
            continue
        left, _, right = lines[time_index].partition("-->")
        start, end = _parse_time(left), _parse_time(right)
        if start is None or end is None:
            continue
        body = " ".join(lines[time_index + 1 :])
        cues.append(Cue(start, end, _clean_text(body)))
    return _finalize(cues)


def parse_vtt(text: str) -> list[Cue]:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    # 去掉 WEBVTT 头、NOTE / STYLE / REGION 块
    text = re.sub(r"^WEBVTT[^\n]*\n", "", text)
    text = re.sub(r"(?m)^(NOTE|STYLE|REGION)\b.*?(?=\n\s*\n|\Z)", "", text, flags=re.S)
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        time_index = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if time_index < 0:
            continue
        left, _, right = lines[time_index].partition("-->")
        # 右侧可能带着 align:start position:10% 之类的 cue 设置，取第一段即可
        right_ts = right.strip().split(" ")[0]
        start, end = _parse_time(left), _parse_time(right_ts)
        if start is None or end is None:
            continue
        cues.append(Cue(start, end, _clean_text(" ".join(lines[time_index + 1 :]))))
    return _finalize(cues)


def parse_ass(text: str) -> list[Cue]:
    """解析 ASS/SSA。按 ``Format:`` 行动态映射字段下标，兼容各种变体。"""

    order = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
    in_events = False
    cues: list[Cue] = []
    for raw_line in _LINE_SPLIT.split(text):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("[") and lowered.endswith("]"):
            in_events = lowered == "[events]"
            continue
        if not in_events:
            continue
        if lowered.startswith("format:"):
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            if "start" in fields and "end" in fields:
                order = fields
            continue
        if not lowered.startswith("dialogue:"):
            continue
        payload = line.split(":", 1)[1].lstrip()
        # Text 是最后一个字段，可能包含逗号，所以按字段数切分
        parts = payload.split(",", len(order) - 1)
        if len(parts) < len(order):
            continue
        try:
            start = _parse_time(parts[order.index("start")])
            end = _parse_time(parts[order.index("end")])
            body = parts[order.index("text")]
        except (ValueError, IndexError):
            continue
        if start is None or end is None:
            continue
        cues.append(Cue(start, end, _clean_text(body)))
    return _finalize(cues)


def parse_subtitle(text: str, ext: str | None = None) -> list[Cue]:
    """自动识别格式并解析。识别失败时依次尝试其他格式。"""

    ext = (ext or "").lower()
    parsers = []
    if ext in (".ass", ".ssa"):
        parsers = [parse_ass, parse_srt]
    elif ext == ".vtt":
        parsers = [parse_vtt, parse_srt]
    elif ext == ".srt":
        parsers = [parse_srt, parse_vtt, parse_ass]
    else:
        parsers = [parse_srt, parse_vtt, parse_ass]

    best: list[Cue] = []
    for parser in parsers:
        try:
            cues = parser(text)
        except Exception:  # noqa: BLE001 - 字幕千奇百怪，解析失败就换下一个
            cues = []
        if len(cues) > len(best):
            best = cues
        if best:
            break
    return best


def cues_in_window(cues: Sequence[Cue], start: float, end: float) -> list[Cue]:
    """取出与 [start, end] 有交集的字幕后缀（假设 cues 已按时间排序）。"""

    if not cues or end <= start:
        return []

    # 二分找到第一条 end > start 的字幕
    lo, hi = 0, len(cues)
    while lo < hi:
        mid = (lo + hi) // 2
        if cues[mid].end <= start:
            lo = mid + 1
        else:
            hi = mid
    picked: list[Cue] = []
    for cue in cues[lo:]:
        if cue.start > end:
            break
        picked.append(cue)
    return picked


def format_timestamp(seconds: float, with_hours: bool = False) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if with_hours or hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_transcript(cues: Iterable[Cue], max_chars: int = 1600) -> str:
    """把字幕拼成带时间戳的上下文，超出预算时优先保留靠后的台词。"""

    lines = [f"[{format_timestamp(c.start)}] {c.text}" for c in cues]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # 从后往前累计，保证离当前时刻最近的台词一定在
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > max_chars:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return "\n".join(kept)


def _vtt_time(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def to_webvtt(cues: Sequence[Cue], offset: float = 0.0) -> str:
    """转成浏览器 ``<track>`` 能用的 WebVTT。offset 用于转封装时的进度偏移。"""

    out = ["WEBVTT", ""]
    for cue in cues:
        start = cue.start - offset
        end = cue.end - offset
        if end <= 0:
            continue
        out.append(f"{_vtt_time(max(0.0, start))} --> {_vtt_time(end)}")
        out.append(cue.text)
        out.append("")
    return "\n".join(out)
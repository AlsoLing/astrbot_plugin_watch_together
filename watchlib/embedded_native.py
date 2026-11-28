"""纯 Python 内嵌字幕提取（没有 ffmpeg 时的兜底）。

不依赖 ffmpeg / 第三方库，直接解析容器，把文本字幕转成标准 SRT 文本，
交给上层现成的 SRT 解析器（``watchlib.subtitles.parse_srt``）使用。

支持：

* Matroska / WebM（``.mkv`` / ``.mkm`` / ``.webm``）：
  ``S_TEXT/UTF8``、``S_TEXT/ASS``、``S_TEXT/SSA``、``S_TEXT/WEBVTT`` 等文本轨道。
* ISO-BMFF / QuickTime（``.mp4`` / ``.m4v`` / ``.mov``）：
  ``tx3g``（``mov_text``）字幕轨。

不支持（只能 OCR 或交给 ffmpeg）：

* 位图字幕：``S_HDMV/PGS``（PGS）、``S_VOBSUB``（VobSub）、``S_DVBSUB``；
* 老的 QuickTime ``text`` 轨、WebVTT/分片 MP4（``moof``）等特殊封装。

实现约束：

* 只用标准库；``seek()`` 跳过大块数据，绝不把整部电影读进内存
  （唯一的例外是 MP4 的 ``moov``，上限 64MB）。
* 有解析上限（字幕条数 / sample 数）与超时检查，损坏文件也能快速返回。
* ``extract`` 绝不抛异常：没有可用文本字幕、文件损坏、超时都返回 ``""``。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, BinaryIO, TypedDict

from .subtitles import decode_subtitle_bytes

__all__ = ["probe", "extract", "best_track", "SUPPORTED_TEXT_CODECS"]

# 小写 codec 名（与 ffprobe 的 codec_name 对齐），同时也是“能当文本提取”的白名单。
SUPPORTED_TEXT_CODECS: set[str] = {
    "subrip", "srt", "ass", "ssa", "webvtt", "vtt", "mov_text", "text",
    "usf", "sami", "microdvd", "realtext", "subviewer", "subviewer1",
    "mpl2", "pjs", "jacosub", "vplayer", "stl",
}

# 位图字幕的 codec 名（无法纯 Python 转文本）。
_BITMAP_CODECS: set[str] = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub",
    "dvb_teletext", "pgs", "vobsub",
}

_MKV_SUFFIXES: set[str] = {".mkv", ".mkm", ".webm"}
_MP4_SUFFIXES: set[str] = {".mp4", ".m4v", ".mov"}

_PROBE_TIMEOUT = 10.0
_MAX_ELEMENT_BYTES = 8 * 1024 * 1024      # 单个字幕块 / 轨道私有数据上限
_MAX_TEXT_FIELD = 64 * 1024               # 语言、轨道名等小字段上限
_MAX_MOOV_BYTES = 64 * 1024 * 1024        # moov 允许整块读入的内存上限
_MAX_SAMPLE_BYTES = 2 * 1024 * 1024       # 单个 MP4 sample 上限
_MAX_CUES = 200_000                       # 字幕条数上限
_MAX_SAMPLES = 200_000                    # MP4 sample 数上限
_MAX_BOXES = 200_000                      # 单层 box 循环上限（防御损坏文件）

# EBML ID
_ID_EBML = 0x1A45DFA3
_ID_SEGMENT = 0x18538067
_ID_TRACKS = 0x1654AE6B
_ID_CLUSTER = 0x1F43B675
_ID_TRACK_ENTRY = 0xAE
_ID_TRACK_NUMBER = 0xD7
_ID_TRACK_TYPE = 0x83
_ID_CODEC_ID = 0x86
_ID_CODEC_PRIVATE = 0x63A2
_ID_LANGUAGE = 0x22B59C
_ID_NAME = 0x536E
_ID_FLAG_DEFAULT = 0x88
_ID_FLAG_FORCED = 0x55AA
_ID_TIMESTAMP = 0xE7
_ID_SIMPLE_BLOCK = 0xA3
_ID_BLOCK_GROUP = 0xA0
_ID_BLOCK = 0xA1
_ID_BLOCK_DURATION = 0x9B

# Cluster 里可能出现的子元素（判断未知长度 Cluster 何时结束）。
_CLUSTER_CHILD_IDS: set[int] = {
    0xE7, 0xA3, 0xA0, 0xA7, 0xAB, 0x5854, 0xAF, 0xBF, 0xEC,
}

# Matroska CodecID -> 对外 codec 名（小写，风格与 ffprobe 一致）。
_MKV_TEXT_CODECS: dict[str, str] = {
    "S_TEXT/UTF8": "subrip",
    "S_TEXT/ASCII": "subrip",
    "S_TEXT/ASS": "ass",
    "S_TEXT/SSA": "ssa",
    "S_TEXT/WEBVTT": "webvtt",
    "S_TEXT/USF": "usf",
    "S_TEXT/SAMI": "sami",
    "S_TEXT/MICRODVD": "microdvd",
    "S_TEXT/REAL": "realtext",
    "S_TEXT/SUBVIEWER": "subviewer",
    "S_TEXT/MPL2": "mpl2",
    "S_TEXT/PJS": "pjs",
    "S_TEXT/JACOSUB": "jacosub",
    "S_TEXT/VPLAYER": "vplayer",
}
_MKV_BITMAP_CODECS: dict[str, str] = {
    "S_HDMV/PGS": "hdmv_pgs_subtitle",
    "S_VOBSUB": "dvd_subtitle",
    "S_DVBSUB": "dvb_subtitle",
    "S_IMAGE/BMP": "xsub",
}

# MP4 sample entry -> codec 名
_MP4_CODEC_NAMES: dict[bytes, str] = {
    b"tx3g": "mov_text",
    b"text": "text",
    b"wvtt": "webvtt",
    b"stpp": "ttml",
    b"c608": "eia_608",
    b"c708": "eia_708",
}
_MP4_SUBTITLE_HANDLERS: set[bytes] = {b"sbtl", b"text", b"subp"}

_ZH_LANGS: set[str] = {
    "chi", "zho", "zh", "zh-cn", "zh-hans", "zh-hant", "zh_cn", "zh_tw",
    "chs", "cht", "sc", "tc", "cn", "chinese", "中文", "简体", "繁体", "中字",
    "yue", "cmn",
}
_ZH_TITLE_TOKENS: tuple[str, ...] = (
    "中", "简", "繁", "chi", "zho", "zh", "chs", "cht", "chinese",
)
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")


class _Abort(Exception):
    """内部解析中止（截断 / 非法数据 / 超时），不会泄漏给调用方。"""

    def __init__(self, kind: str = "invalid") -> None:
        super().__init__(kind)
        self.kind = kind


class _MkvTrack(TypedDict):
    """Matroska 轨道的内部记录（比 probe 多带轨道号 / 原始 CodecID）。"""

    index: int
    number: int
    codec: str
    codec_id: str
    language: str
    title: str
    bitmap: bool
    forced: bool
    default: bool
    private: bytes


class _Stbl(TypedDict):
    """MP4 ``stbl`` 里的采样表。"""

    codec: str
    sample_sizes: list[int]
    deltas: list[int]
    chunk_offsets: list[int]
    stsc: list[tuple[int, int, int]]


class _Mp4Track(TypedDict):
    """MP4 字幕轨的内部记录（带采样表）。"""

    index: int
    codec: str
    language: str
    title: str
    bitmap: bool
    forced: bool
    default: bool
    timescale: int
    sample_sizes: list[int]
    deltas: list[int]
    chunk_offsets: list[int]
    stsc: list[tuple[int, int, int]]


def _now() -> float:
    """当前单调时间（独立成函数，测试里可以替换）。"""

    return time.monotonic()


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _vint_width(first: int) -> int:
    """vint 首字节 -> 总字节数（1..8）；0 或非法时抛 ``_Abort``。"""

    if first == 0:
        raise _Abort("bad vint")
    for width in range(1, 9):
        if first & (0x80 >> (width - 1)):
            return width
    raise _Abort("bad vint")


def _parse_vint_bytes(buf: bytes) -> tuple[int, int] | None:
    """从内存里解析一个 vint（去掉 marker 位），返回 (值, 长度)。"""

    if not buf:
        return None
    try:
        width = _vint_width(buf[0])
    except _Abort:
        return None
    if width > len(buf):
        return None
    value = int.from_bytes(buf[:width], "big") & ((1 << (7 * width)) - 1)
    return value, width


def _read_uint(st: _EbmlStream, size: int) -> int:
    """读 EBML 无符号整数；长度非法时跳过并返回 0。"""

    if size <= 0:
        return 0
    if size > 8:
        st.skip(size)
        return 0
    return int.from_bytes(st.read(size), "big")


def _read_text(st: _EbmlStream, size: int) -> str:
    """读 EBML 字符串（UTF-8，截断到 NUL）；过大时跳过。"""

    if size <= 0:
        return ""
    if size > _MAX_TEXT_FIELD:
        st.skip(size)
        return ""
    raw = st.read(size).split(b"\x00", 1)[0]
    return raw.decode("utf-8", "replace").strip()


def _format_timestamp(milliseconds: int) -> str:
    """毫秒 -> ``HH:MM:SS,mmm``。"""

    total = max(0, int(milliseconds))
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _clean_body(text: str) -> str:
    """规整字幕正文：统一换行、丢空行、去行尾空白。"""

    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _build_srt(cues: list[tuple[int, int, str]]) -> str:
    """把 (开始ms, 结束ms, 正文) 列表渲染成标准 SRT。"""

    blocks: list[str] = []
    for start, end, text in sorted(cues, key=lambda item: item[0]):
        body = _clean_body(text)
        if not body or end <= start:
            continue
        number = len(blocks) + 1
        blocks.append(
            f"{number}\n{_format_timestamp(start)} --> {_format_timestamp(end)}\n{body}"
        )
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def _is_chinese(language: str, title: str) -> bool:
    """语言码或轨道名里是否带中文信息。"""

    if language in _ZH_LANGS or language.startswith("zh"):
        return True
    lowered = title.lower()
    return any(token in lowered for token in _ZH_TITLE_TOKENS)


def _codec_rank(codec: str) -> int:
    """同优先级时更偏好简单文本格式（subrip / mov_text 最稳）。"""

    if codec in ("subrip", "srt", "mov_text"):
        return 3
    if codec in ("webvtt", "vtt"):
        return 2
    if codec in ("ass", "ssa", "text"):
        return 1
    return 0


def _is_text_track(track: dict[str, Any]) -> bool:
    """判断一条轨道能否按文本处理。"""

    codec = str(track.get("codec") or "").strip().lower()
    if codec in _BITMAP_CODECS or bool(track.get("bitmap")):
        return False
    # codec 缺失时保守地当作文本（与 watchlib.embedded.pick_stream 一致）
    return not codec or codec in SUPPORTED_TEXT_CODECS


def best_track(tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按 中文优先 > 非 forced > default > 文本优先 选一条。

    位图字幕（PGS / VobSub 等）与不支持的 codec 直接排除；没有文本轨返回 ``None``。
    """

    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int, int, int] | None = None
    for track in tracks or []:
        if not _is_text_track(track):
            continue
        codec = str(track.get("codec") or "").strip().lower()
        language = str(track.get("language") or "").strip().lower()
        title = str(track.get("title") or "")
        score = (
            1 if _is_chinese(language, title) else 0,
            0 if bool(track.get("forced")) else 1,
            1 if bool(track.get("default")) else 0,
            1 if codec not in _BITMAP_CODECS else 0,
            _codec_rank(codec),
        )
        if best_score is None or score > best_score:
            best, best_score = track, score
    return best


# ---------------------------------------------------------------------------
# Matroska / EBML
# ---------------------------------------------------------------------------


class _EbmlStream:
    """带边界与超时检查的顺序读取器；大块数据用 seek 跳过。"""

    def __init__(self, handle: BinaryIO, size: int, deadline: float) -> None:
        self._handle = handle
        self.size = size
        self.deadline = deadline
        self.pos = 0

    def tell(self) -> int:
        return self.pos

    def _check(self) -> None:
        if _now() > self.deadline:
            raise _Abort("timeout")

    def read(self, length: int) -> bytes:
        if length < 0 or length > self.size - self.pos:
            raise _Abort("read past end")
        self._check()
        data = self._handle.read(length)
        if len(data) != length:
            raise _Abort("truncated")
        self.pos += length
        return data

    def skip(self, length: int) -> None:
        if length < 0:
            raise _Abort("negative skip")
        target = self.pos + length
        if target > self.size:
            raise _Abort("truncated")
        self._check()
        self._handle.seek(target)
        self.pos = target

    def seek(self, position: int) -> None:
        if position < 0 or position > self.size:
            raise _Abort("bad seek")
        self._check()
        self._handle.seek(position)
        self.pos = position

    def read_id(self) -> int | None:
        """读 EBML ID（保留 marker 位）；文件尾返回 ``None``。"""

        if self.pos >= self.size:
            return None
        first = self.read(1)[0]
        width = _vint_width(first)
        if width > 4:
            raise _Abort("bad id width")
        raw = bytes([first])
        if width > 1:
            raw += self.read(width - 1)
        return int.from_bytes(raw, "big")

    def read_size(self) -> int | None:
        """读 EBML size vint；全 1 表示 unknown size（返回 ``None``）。"""

        first = self.read(1)[0]
        width = _vint_width(first)
        raw = bytes([first])
        if width > 1:
            raw += self.read(width - 1)
        value = int.from_bytes(raw, "big") & ((1 << (7 * width)) - 1)
        if value == (1 << (7 * width)) - 1:
            return None
        return value

    def read_uint(self, length: int) -> int:
        return _read_uint(self, length)


def _element_end(size: int, data_start: int, parent_end: int) -> int:
    end = data_start + size
    if end > parent_end:
        raise _Abort("element past parent")
    return end


def _read_header(st: _EbmlStream, limit: int) -> tuple[int, int | None, int] | None:
    """读一个 EBML 元素头，返回 (id, size, payload_start)；到边界返回 None。"""

    if st.tell() >= limit or st.tell() >= st.size:
        return None
    eid = st.read_id()
    if eid is None:
        return None
    size = st.read_size()
    start = st.tell()
    if start > limit:
        raise _Abort("header past parent")
    if size is not None and start + size > st.size:
        raise _Abort("element past eof")
    return eid, size, start


def _skip_element(
    st: _EbmlStream,
    eid: int,
    size: int | None,
    data_start: int,
    parent_end: int,
) -> None:
    """跳过一个元素（未知长度的 Cluster 会逐个子元素走完）。"""

    if size is not None:
        st.seek(_element_end(size, data_start, parent_end))
        return
    if eid == _ID_CLUSTER:
        _skip_unknown_cluster(st, parent_end)
        return
    raise _Abort("unknown sized element")


def _skip_unknown_cluster(st: _EbmlStream, parent_end: int) -> None:
    """未知长度的 Cluster：按子元素白名单走到下一个顶层元素。"""

    while st.tell() < parent_end:
        position = st.tell()
        head = _read_header(st, parent_end)
        if head is None:
            return
        eid, size, data_start = head
        if eid not in _CLUSTER_CHILD_IDS:
            st.seek(position)
            return
        if size is None:
            raise _Abort("unknown sized cluster child")
        st.seek(data_start + size)


def _find_segment(st: _EbmlStream) -> tuple[int, int] | None:
    """校验 EBML 头并定位 Segment 的 (payload_start, payload_end)。"""

    st.seek(0)
    if st.size < 8:
        raise _Abort("too small")
    eid = st.read_id()
    if eid != _ID_EBML:
        raise _Abort("not matroska")
    header_size = st.read_size()
    if header_size is None or header_size > st.size:
        raise _Abort("bad ebml header")
    st.skip(header_size)

    while st.tell() < st.size:
        eid = st.read_id()
        if eid is None:
            return None
        size = st.read_size()
        start = st.tell()
        if eid == _ID_SEGMENT:
            if size is None:
                if start >= st.size:
                    return None
                return start, st.size
            end = min(start + size, st.size)
            if end <= start:
                raise _Abort("empty segment")
            return start, end
        if size is None:
            raise _Abort("unknown size before segment")
        st.skip(size)
    return None


def _mkv_codec_name(codec_id: str) -> str:
    """Matroska CodecID -> 小写 codec 名。"""

    key = codec_id.strip().upper()
    if key in _MKV_TEXT_CODECS:
        return _MKV_TEXT_CODECS[key]
    if key in _MKV_BITMAP_CODECS:
        return _MKV_BITMAP_CODECS[key]
    return codec_id.strip().lower()


def _mkv_bitmap(codec_id: str) -> bool:
    """是否位图字幕轨道。"""

    key = codec_id.strip().upper()
    if key in _MKV_BITMAP_CODECS:
        return True
    return key.startswith("S_IMAGE/") or key.startswith("S_VOBSUB")


def _parse_track_entry(
    st: _EbmlStream, start: int, end: int
) -> _MkvTrack | None:
    """解析一个 TrackEntry；非字幕轨返回 None。"""

    number = 0
    track_type = 0
    codec_id = ""
    private = b""
    language = ""
    title = ""
    default = False
    forced = False

    st.seek(start)
    while st.tell() < end:
        head = _read_header(st, end)
        if head is None:
            break
        eid, size, data_start = head
        if size is None:
            raise _Abort("unknown sized track field")
        if eid == _ID_TRACK_NUMBER:
            number = _read_uint(st, size)
        elif eid == _ID_TRACK_TYPE:
            track_type = _read_uint(st, size)
        elif eid == _ID_CODEC_ID:
            codec_id = _read_text(st, size).upper()
        elif eid == _ID_CODEC_PRIVATE:
            if size <= _MAX_ELEMENT_BYTES:
                private = st.read(size)
            else:
                st.skip(size)
        elif eid == _ID_LANGUAGE:
            language = _read_text(st, size).lower()
        elif eid == _ID_NAME:
            title = _read_text(st, size)
        elif eid == _ID_FLAG_DEFAULT:
            default = _read_uint(st, size) != 0
        elif eid == _ID_FLAG_FORCED:
            forced = _read_uint(st, size) != 0
        else:
            st.skip(size)

    if track_type != 0x11:
        return None
    return {
        "index": number,
        "number": number,
        "codec": _mkv_codec_name(codec_id),
        "codec_id": codec_id,
        "language": language,
        "title": title,
        "bitmap": _mkv_bitmap(codec_id),
        "forced": forced,
        "default": default,
        "private": private,
    }


def _parse_tracks(
    st: _EbmlStream, start: int, end: int, tracks: list[_MkvTrack]
) -> None:
    """解析 Tracks 元素，把字幕轨追加到 ``tracks``。"""

    st.seek(start)
    while st.tell() < end and len(tracks) < _MAX_CUES:
        head = _read_header(st, end)
        if head is None:
            break
        eid, size, data_start = head
        if eid == _ID_TRACK_ENTRY:
            if size is None:
                raise _Abort("unknown sized track entry")
            track_end = _element_end(size, data_start, end)
            record = _parse_track_entry(st, data_start, track_end)
            if record is not None:
                tracks.append(record)
            st.seek(track_end)
        else:
            _skip_element(st, eid, size, data_start, end)


def _mkv_track_records(st: _EbmlStream) -> list[_MkvTrack]:
    """只扫到 Tracks 元素为止，拿到字幕轨；其余元素直接 seek 跳过。"""

    tracks: list[_MkvTrack] = []
    try:
        segment = _find_segment(st)
        if segment is None:
            return tracks
        segment_start, segment_end = segment
        st.seek(segment_start)
        while st.tell() < segment_end:
            head = _read_header(st, segment_end)
            if head is None:
                break
            eid, size, data_start = head
            if eid == _ID_TRACKS:
                if size is None:
                    break
                tracks_end = _element_end(size, data_start, segment_end)
                _parse_tracks(st, data_start, tracks_end, tracks)
                break
            _skip_element(st, eid, size, data_start, segment_end)
    except _Abort as exc:
        if exc.kind == "timeout":
            raise
        # 截断/损坏：返回已经解析到的部分
    return tracks


def _mkv_public(rec: _MkvTrack, ordinal: int) -> dict[str, Any]:
    """内部轨道记录 -> probe 输出（不暴露 CodecPrivate 等大字段）。"""

    index = int(rec["number"]) if rec["number"] else ordinal
    return {
        "index": index,
        "codec": rec["codec"],
        "language": rec["language"],
        "title": rec["title"],
        "bitmap": rec["bitmap"],
        "forced": rec["forced"],
        "default": rec["default"],
    }


def _parse_block_head(
    buf: bytes, size: int
) -> tuple[int, int, int, int] | None:
    """解析 Block/SimpleBlock 头，返回 (轨道号, 相对时间ms, flags, 数据偏移)。"""

    if not buf:
        return None
    try:
        width = _vint_width(buf[0])
    except _Abort:
        return None
    if width + 3 > len(buf) or width + 3 > size:
        return None
    track = int.from_bytes(buf[:width], "big") & ((1 << (7 * width)) - 1)
    relative = int.from_bytes(buf[width:width + 2], "big", signed=True)
    return track, relative, buf[width + 2], width + 3


def _read_simple_block(
    st: _EbmlStream,
    size: int,
    data_start: int,
    cluster_ts: int,
    target: int,
    items: list[tuple[int, int | None, bytes]],
) -> None:
    """读一个 SimpleBlock；非目标轨 / 带 lacing 的块只跳过不读 payload。"""

    if size < 4:
        return
    head_length = min(size, 11)
    head = st.read(head_length)
    parsed = _parse_block_head(head, size)
    if parsed is None:
        return
    track, relative, flags, data_offset = parsed
    # lacing 在 flags 的 bit 1-2；字幕极少 lacing，直接放弃这一块
    if track != target or flags & 0x06:
        return
    body_length = size - data_offset
    if body_length <= 0 or body_length > _MAX_ELEMENT_BYTES:
        return
    body = head[data_offset:] if data_offset < len(head) else b""
    missing = body_length - len(body)
    if missing > 0:
        body += st.read(missing)
    items.append((cluster_ts + relative, None, body))


def _read_block_group(
    st: _EbmlStream,
    start: int,
    end: int,
    cluster_ts: int,
    target: int,
    items: list[tuple[int, int | None, bytes]],
) -> None:
    """读一个 BlockGroup（Block + 可选 BlockDuration）。"""

    st.seek(start)
    block: bytes | None = None
    duration: int | None = None
    while st.tell() < end:
        head = _read_header(st, end)
        if head is None:
            break
        eid, size, data_start = head
        if size is None:
            raise _Abort("unknown sized block group child")
        if eid == _ID_BLOCK:
            if size <= _MAX_ELEMENT_BYTES:
                block = st.read(size)
            else:
                st.skip(size)
        elif eid == _ID_BLOCK_DURATION:
            if size <= 8:
                duration = int.from_bytes(st.read(size), "big")
            else:
                st.skip(size)
        else:
            st.skip(size)

    if block is None:
        return
    parsed = _parse_block_head(block, len(block))
    if parsed is None:
        return
    track, relative, flags, data_offset = parsed
    if track != target or flags & 0x06:
        return
    body = block[data_offset:]
    if not body:
        return
    items.append((cluster_ts + relative, duration, body))


def _parse_cluster(
    st: _EbmlStream,
    start: int,
    end: int,
    target: int,
    items: list[tuple[int, int | None, bytes]],
    unknown_size: bool,
) -> None:
    """解析一个 Cluster，收集目标轨的 (绝对开始ms, 可选时长, 原始 payload)。"""

    st.seek(start)
    timestamp = 0
    while st.tell() < end and len(items) < _MAX_CUES:
        position = st.tell()
        head = _read_header(st, end)
        if head is None:
            break
        eid, size, data_start = head
        if unknown_size and eid not in _CLUSTER_CHILD_IDS:
            # 未知长度的 Cluster 结束：回退，让外层按 Segment 子元素继续
            st.seek(position)
            break
        if eid == _ID_TIMESTAMP:
            if size is None:
                raise _Abort("bad cluster timestamp")
            if size > 8:
                st.skip(size)
            else:
                timestamp = int.from_bytes(st.read(size), "big")
        elif eid == _ID_SIMPLE_BLOCK:
            if size is None:
                raise _Abort("bad simple block")
            _read_simple_block(st, size, data_start, timestamp, target, items)
            st.seek(data_start + size)
        elif eid == _ID_BLOCK_GROUP:
            if size is None:
                raise _Abort("bad block group")
            group_end = _element_end(size, data_start, end)
            _read_block_group(st, data_start, group_end, timestamp, target, items)
            st.seek(group_end)
        else:
            _skip_element(st, eid, size, data_start, end)


def _mkv_block_items(
    st: _EbmlStream, target: int
) -> list[tuple[int, int | None, bytes]]:
    """第二遍扫描：只收集目标轨的 Block 数据。"""

    items: list[tuple[int, int | None, bytes]] = []
    try:
        segment = _find_segment(st)
        if segment is None:
            return items
        segment_start, segment_end = segment
        st.seek(segment_start)
        while st.tell() < segment_end and len(items) < _MAX_CUES:
            head = _read_header(st, segment_end)
            if head is None:
                break
            eid, size, data_start = head
            if eid == _ID_CLUSTER:
                if size is None:
                    _parse_cluster(
                        st, data_start, segment_end, target, items, True
                    )
                else:
                    cluster_end = _element_end(
                        size, data_start, segment_end
                    )
                    _parse_cluster(
                        st, data_start, cluster_end, target, items, False
                    )
                    st.seek(cluster_end)
            else:
                _skip_element(st, eid, size, data_start, segment_end)
    except _Abort as exc:
        if exc.kind == "timeout":
            raise
        # 截断/损坏：返回已经收集到的字幕块
    return items


def _mkv_cue_text(codec_id: str, raw: bytes) -> str:
    """按 codec 把 payload 变成可读文本。"""

    if not raw:
        return ""
    text = decode_subtitle_bytes(raw)
    key = codec_id.strip().upper()
    if key in ("S_TEXT/ASS", "S_TEXT/SSA"):
        # ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        parts = text.split(",", 8)
        text = parts[8] if len(parts) >= 9 else text.rsplit(",", 1)[-1]
        text = _ASS_OVERRIDE.sub("", text)
        text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return _clean_body(text)


def _mkv_extract(st: _EbmlStream, rec: _MkvTrack) -> list[tuple[int, int, str]]:
    """提取一条 Matroska 字幕轨，补全结束时间并返回 (start, end, text)。"""

    number = int(rec["number"])
    if number <= 0:
        return []
    raw_items = _mkv_block_items(st, number)
    cues: list[tuple[int, int, str]] = []
    for position, (start, duration, body) in enumerate(raw_items):
        text = _mkv_cue_text(str(rec["codec_id"]), body)
        if not text:
            continue
        if duration is None:
            duration = 4000
            if position + 1 < len(raw_items):
                next_start = raw_items[position + 1][0]
                if next_start > start:
                    duration = next_start - start
        start_ms = max(0, int(start))
        span = max(500, min(10000, int(duration)))
        cues.append((start_ms, start_ms + span, text))
    return cues


def _extract_mkv(
    handle: BinaryIO, file_size: int, deadline: float
) -> list[tuple[int, int, str]]:
    """MKV 完整提取流程。"""

    st = _EbmlStream(handle, file_size, deadline)
    tracks = _mkv_track_records(st)
    if not tracks:
        return []
    public = [_mkv_public(rec, pos + 1) for pos, rec in enumerate(tracks)]
    best = best_track(public)
    if best is None:
        return []
    for rec, item in zip(tracks, public):
        if item is best:
            return _mkv_extract(st, rec)
    return []


# ---------------------------------------------------------------------------
# MP4 / ISO-BMFF
# ---------------------------------------------------------------------------


def _boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """解析一层 box，返回 (type, payload_start, payload_end)。"""

    result: list[tuple[bytes, int, int]] = []
    position = start
    limit = min(end, len(data))
    guard = 0
    while position + 8 <= limit:
        guard += 1
        if guard > _MAX_BOXES:
            break
        size = int.from_bytes(data[position:position + 4], "big")
        box_type = data[position + 4:position + 8]
        header = 8
        if size == 1:
            if position + 16 > limit:
                break
            size = int.from_bytes(data[position + 8:position + 16], "big")
            header = 16
        elif size == 0:
            size = limit - position
        if size < header or position + size > limit:
            break
        result.append((box_type, position + header, position + size))
        position += size
    return result


def _read_moov(
    handle: BinaryIO, file_size: int, deadline: float
) -> bytes | None:
    """逐层跳过顶层 box，只把 moov 读进内存（上限 64MB）。"""

    position = 0
    while position + 8 <= file_size:
        if _now() > deadline:
            raise _Abort("timeout")
        handle.seek(position)
        head = handle.read(16)
        if len(head) < 8:
            return None
        size = int.from_bytes(head[:4], "big")
        box_type = head[4:8]
        header = 8
        if size == 1:
            if len(head) < 16:
                return None
            size = int.from_bytes(head[8:16], "big")
            header = 16
        elif size == 0:
            size = file_size - position
        if size < header or size > file_size - position:
            return None
        if box_type == b"moov":
            payload = size - header
            if payload <= 0 or payload > _MAX_MOOV_BYTES:
                return None
            handle.seek(position + header)
            data = handle.read(payload)
            return data if len(data) == payload else None
        position += size
    return None


def _stsd_codec(data: bytes, start: int, end: int) -> str:
    """读 stsd 第一个 sample entry 的 format。"""

    if end - start < 16:
        return ""
    fmt = data[start + 12:start + 16]
    if fmt in _MP4_CODEC_NAMES:
        return _MP4_CODEC_NAMES[fmt]
    return fmt.decode("latin-1", "replace").lower()


def _stts_deltas(data: bytes, start: int, end: int) -> list[int]:
    """stts -> 每个 sample 的时长（timescale 单位）。"""

    if end - start < 8:
        return []
    count = int.from_bytes(data[start + 4:start + 8], "big")
    position = start + 8
    deltas: list[int] = []
    for _ in range(min(count, _MAX_SAMPLES)):
        if position + 8 > end:
            break
        run = int.from_bytes(data[position:position + 4], "big")
        delta = int.from_bytes(data[position + 4:position + 8], "big")
        position += 8
        if run <= 0 or run > _MAX_SAMPLES:
            break
        remaining = _MAX_SAMPLES - len(deltas)
        if run > remaining:
            run = remaining
        deltas.extend([delta] * run)
        if len(deltas) >= _MAX_SAMPLES:
            break
    return deltas


def _stsc_entries(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    """stsc -> [(first_chunk, samples_per_chunk, desc_index)]。"""

    if end - start < 8:
        return []
    count = int.from_bytes(data[start + 4:start + 8], "big")
    position = start + 8
    entries: list[tuple[int, int, int]] = []
    for _ in range(min(count, _MAX_SAMPLES)):
        if position + 12 > end:
            break
        first_chunk = int.from_bytes(data[position:position + 4], "big")
        per_chunk = int.from_bytes(data[position + 4:position + 8], "big")
        desc = int.from_bytes(data[position + 8:position + 12], "big")
        position += 12
        entries.append((first_chunk, per_chunk, desc))
    return entries


def _stsz_sizes(data: bytes, start: int, end: int) -> list[int]:
    """stsz -> 每个 sample 的字节数。"""

    if end - start < 12:
        return []
    fixed = int.from_bytes(data[start + 4:start + 8], "big")
    count = int.from_bytes(data[start + 8:start + 12], "big")
    count = min(count, _MAX_SAMPLES)
    if fixed:
        return [fixed] * count
    position = start + 12
    sizes: list[int] = []
    for _ in range(count):
        if position + 4 > end:
            break
        sizes.append(int.from_bytes(data[position:position + 4], "big"))
        position += 4
    return sizes


def _chunk_offsets(
    data: bytes, start: int, end: int, width: int
) -> list[int]:
    """stco / co64 -> chunk 的绝对文件偏移。"""

    if end - start < 8:
        return []
    count = int.from_bytes(data[start + 4:start + 8], "big")
    count = min(count, _MAX_SAMPLES)
    position = start + 8
    offsets: list[int] = []
    for _ in range(count):
        if position + width > end:
            break
        offsets.append(int.from_bytes(data[position:position + width], "big"))
        position += width
    return offsets


def _parse_stbl(data: bytes, start: int, end: int) -> _Stbl:
    """收集 stbl 里的采样表。"""

    codec = ""
    sizes: list[int] = []
    deltas: list[int] = []
    chunk_offsets: list[int] = []
    stsc: list[tuple[int, int, int]] = []
    for box_type, payload_start, payload_end in _boxes(data, start, end):
        if box_type == b"stsd":
            codec = _stsd_codec(data, payload_start, payload_end)
        elif box_type == b"stts":
            deltas = _stts_deltas(data, payload_start, payload_end)
        elif box_type == b"stsc":
            stsc = _stsc_entries(data, payload_start, payload_end)
        elif box_type == b"stsz":
            sizes = _stsz_sizes(data, payload_start, payload_end)
        elif box_type == b"stco":
            chunk_offsets = _chunk_offsets(data, payload_start, payload_end, 4)
        elif box_type == b"co64":
            chunk_offsets = _chunk_offsets(data, payload_start, payload_end, 8)
    return {
        "codec": codec,
        "sample_sizes": sizes,
        "deltas": deltas,
        "chunk_offsets": chunk_offsets,
        "stsc": stsc,
    }


def _tkhd_track_id(data: bytes, start: int, end: int) -> int | None:
    """tkhd 里的 track_id（version 0 / 1 偏移不同）。"""

    if end - start < 16:
        return None
    version = data[start]
    offset = 20 if version == 1 else 12
    if start + offset + 4 > end:
        return None
    return int.from_bytes(data[start + offset:start + offset + 4], "big")


def _iso639(packed: int) -> str:
    """mdhd 的 15-bit 压缩语言码 -> 小写三字母。"""

    if packed <= 0:
        return ""
    chars: list[str] = []
    for shift in (10, 5, 0):
        code = (packed >> shift) & 0x1F
        if not 1 <= code <= 26:
            return ""
        chars.append(chr(code + 0x60))
    text = "".join(chars).lower()
    return "" if text == "und" else text


def _parse_mdhd(data: bytes, start: int, end: int) -> tuple[int, str]:
    """mdhd -> (timescale, language)。"""

    if end - start < 24:
        return 0, ""
    version = data[start]
    if version == 1:
        if end - start < 34:
            return 0, ""
        timescale = int.from_bytes(data[start + 20:start + 24], "big")
        raw_language = int.from_bytes(data[start + 32:start + 34], "big")
    else:
        timescale = int.from_bytes(data[start + 12:start + 16], "big")
        raw_language = int.from_bytes(data[start + 20:start + 22], "big")
    return timescale, _iso639(raw_language)


def _parse_trak(
    data: bytes, start: int, end: int, ordinal: int
) -> _Mp4Track | None:
    """解析一个 trak；不是字幕轨返回 None。"""

    track_id: int | None = None
    timescale = 0
    language = ""
    handler = b""
    stbl: _Stbl | None = None

    for box_type, payload_start, payload_end in _boxes(data, start, end):
        if box_type == b"tkhd":
            track_id = _tkhd_track_id(data, payload_start, payload_end)
        elif box_type == b"mdia":
            for media_type, media_start, media_end in _boxes(
                data, payload_start, payload_end
            ):
                if media_type == b"mdhd":
                    timescale, language = _parse_mdhd(
                        data, media_start, media_end
                    )
                elif media_type == b"hdlr":
                    if media_end - media_start >= 12:
                        handler = data[media_start + 8:media_start + 12]
                elif media_type == b"minf":
                    for info_type, info_start, info_end in _boxes(
                        data, media_start, media_end
                    ):
                        if info_type == b"stbl":
                            stbl = _parse_stbl(data, info_start, info_end)

    if stbl is None:
        return None
    codec = stbl["codec"]
    if handler not in _MP4_SUBTITLE_HANDLERS and codec not in SUPPORTED_TEXT_CODECS:
        return None
    index = int(track_id) if track_id else ordinal
    return {
        "index": index,
        "codec": codec,
        "language": language,
        "title": "",
        "bitmap": codec not in SUPPORTED_TEXT_CODECS,
        "forced": False,
        "default": False,
        "timescale": timescale,
        "sample_sizes": stbl["sample_sizes"],
        "deltas": stbl["deltas"],
        "chunk_offsets": stbl["chunk_offsets"],
        "stsc": stbl["stsc"],
    }


def _mp4_track_records(moov: bytes) -> list[_Mp4Track]:
    """moov -> 字幕轨内部记录列表。"""

    records: list[_Mp4Track] = []
    ordinal = 0
    for box_type, start, end in _boxes(moov, 0, len(moov)):
        if box_type != b"trak":
            continue
        ordinal += 1
        record = _parse_trak(moov, start, end, ordinal)
        if record is not None:
            records.append(record)
    return records


def _mp4_public(rec: _Mp4Track) -> dict[str, Any]:
    return {
        "index": rec["index"],
        "codec": rec["codec"],
        "language": rec["language"],
        "title": rec["title"],
        "bitmap": rec["bitmap"],
        "forced": rec["forced"],
        "default": rec["default"],
    }


def _mp4_sample_offsets(
    sizes: list[int],
    chunk_offsets: list[int],
    stsc: list[tuple[int, int, int]],
) -> list[int]:
    """按 stsc / stco / stsz 还原每个 sample 的文件偏移。"""

    count = len(sizes)
    if count == 0 or not chunk_offsets:
        return []
    entries = stsc or [(1, count, 1)]
    offsets: list[int] = []
    index = 0
    for position, (first_chunk, per_chunk, _desc) in enumerate(entries):
        if index >= count:
            break
        if first_chunk < 1 or per_chunk <= 0:
            continue
        if position + 1 < len(entries):
            last_chunk = entries[position + 1][0] - 1
        else:
            last_chunk = len(chunk_offsets)
        for chunk in range(first_chunk, last_chunk + 1):
            if index >= count or chunk > len(chunk_offsets):
                break
            base = chunk_offsets[chunk - 1]
            for _ in range(per_chunk):
                if index >= count:
                    break
                offsets.append(base)
                base += sizes[index]
                index += 1
        if index >= count:
            break
    return offsets


def _tx3g_text(raw: bytes) -> str:
    """tx3g sample：2 字节大端长度 + UTF-8 文本。"""

    if len(raw) < 2:
        return ""
    length = int.from_bytes(raw[0:2], "big")
    if 0 < length <= len(raw) - 2:
        payload = raw[2:2 + length]
    else:
        payload = raw[2:]
    return _clean_body(decode_subtitle_bytes(payload))


def _mp4_cues(
    handle: BinaryIO,
    file_size: int,
    rec: _Mp4Track,
    deadline: float,
) -> list[tuple[int, int, str]]:
    """按采样表逐个读 tx3g 样本，生成 (start, end, text)。"""

    timescale = int(rec["timescale"]) or 1000
    sizes = rec["sample_sizes"]
    deltas = rec["deltas"]
    offsets = _mp4_sample_offsets(
        sizes, rec["chunk_offsets"], rec["stsc"]
    )
    count = min(len(offsets), len(sizes), len(deltas), _MAX_CUES)
    cues: list[tuple[int, int, str]] = []
    start_units = 0
    for index in range(count):
        if _now() > deadline:
            raise _Abort("timeout")
        duration = deltas[index]
        start_ms = int(round(start_units * 1000.0 / timescale))
        end_ms = int(round((start_units + duration) * 1000.0 / timescale))
        start_units += duration

        size = sizes[index]
        offset = offsets[index]
        if size < 2 or size > _MAX_SAMPLE_BYTES:
            continue
        if offset < 0 or offset + size > file_size:
            continue
        handle.seek(offset)
        raw = handle.read(size)
        if len(raw) != size:
            continue
        text = _tx3g_text(raw)
        if not text:
            continue
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        cues.append((start_ms, end_ms, text))
    return cues


def _extract_mp4(
    handle: BinaryIO, file_size: int, deadline: float
) -> list[tuple[int, int, str]]:
    """MP4 完整提取流程。"""

    moov = _read_moov(handle, file_size, deadline)
    if moov is None:
        return []
    records = _mp4_track_records(moov)
    if not records:
        return []
    public = [_mp4_public(rec) for rec in records]
    best = best_track(public)
    if best is None:
        return []
    for rec, item in zip(records, public):
        if item is best:
            # QuickTime 老 text 轨没有实现，按约定返回空
            if rec["codec"] != "mov_text":
                return []
            return _mp4_cues(handle, file_size, rec, deadline)
    return []


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def probe(path: str | Path) -> list[dict[str, Any]]:
    """列出文件里的字幕轨；任何错误都返回空列表。

    每项包含 ``index`` / ``codec`` / ``language`` / ``title`` / ``bitmap`` /
    ``forced``，另外附带 ``default``（best_track 的排序需要）。
    """

    try:
        movie = Path(path)
        suffix = movie.suffix.lower()
        if suffix not in _MKV_SUFFIXES and suffix not in _MP4_SUFFIXES:
            return []
        deadline = _now() + _PROBE_TIMEOUT
        with movie.open("rb") as handle:
            file_size = os.fstat(handle.fileno()).st_size
            if file_size <= 0:
                return []
            if suffix in _MKV_SUFFIXES:
                st = _EbmlStream(handle, file_size, deadline)
                return [
                    _mkv_public(rec, position + 1)
                    for position, rec in enumerate(_mkv_track_records(st))
                ]
            moov = _read_moov(handle, file_size, deadline)
            if moov is None:
                return []
            return [_mp4_public(rec) for rec in _mp4_track_records(moov)]
    except Exception:  # noqa: BLE001 - 探测接口不抛异常
        return []


def extract(path: str | Path, timeout: float = 180.0) -> str:
    """提取 best_track 对应字幕，输出标准 SRT 文本。

    没有可用文本字幕（含只有位图字幕）、文件损坏、超时都返回 ``""``；
    本函数保证不抛异常。
    """

    try:
        movie = Path(path)
        suffix = movie.suffix.lower()
        if suffix not in _MKV_SUFFIXES and suffix not in _MP4_SUFFIXES:
            return ""
        limit = float(timeout)
        if limit <= 0:
            return ""
        deadline = _now() + limit
        with movie.open("rb") as handle:
            file_size = os.fstat(handle.fileno()).st_size
            if file_size <= 0:
                return ""
            if suffix in _MKV_SUFFIXES:
                cues = _extract_mkv(handle, file_size, deadline)
            else:
                cues = _extract_mp4(handle, file_size, deadline)
        return _build_srt(cues)
    except Exception:  # noqa: BLE001 - 兜底接口绝不抛异常
        return ""
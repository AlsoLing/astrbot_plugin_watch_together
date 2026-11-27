"""ffmpeg 辅助：把浏览器不认的容器（mkv/avi…）实时转封装成 fMP4 播放。

默认关闭；NAS 性能足够时打开 ``ffmpeg_mode: remux`` 即可零转码播放
（要求视频是 H.264/H.265，音频是 AAC/MP3 这类浏览器支持的编码）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

__all__ = ["find_ffmpeg", "build_command"]


def find_ffmpeg(hint: str = "ffmpeg") -> str | None:
    hint = (hint or "ffmpeg").strip() or "ffmpeg"
    found = shutil.which(hint)
    if found:
        return found
    candidate = Path(hint)
    if candidate.is_file():
        return str(candidate)
    return None


def build_command(ffmpeg: str, path: str | Path, start: float = 0.0, mode: str = "remux") -> list[str]:
    """构造 ffmpeg 命令：从 ``start`` 秒开始，把文件转成可分块播放的 MP4 输出到 stdout。"""

    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start and start > 0.5:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if mode == "transcode":
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        ]
    else:  # remux：只换容器，不重编码
        args += ["-c", "copy"]
    args += [
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return args


def probe(ffmpeg: str) -> bool:
    """确认 ffmpeg 能跑起来。"""

    try:
        result = subprocess.run(
            [ffmpeg, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
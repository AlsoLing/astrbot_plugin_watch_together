"""一起观影的内部实现包。

- ``media``     片库扫描、字幕发现、nfo 简介
- ``subtitles`` SRT / ASS / VTT 解析
- ``prompts``   提示词构造
- ``engine``    观影会话、点评节奏、大模型调度
- ``server``    观影室 HTTP 服务（Range 串流、API）
- ``ffmpeg``    可选的实时转封装

这个包由 ``main.py`` 按文件路径加载，不依赖插件目录是否在 ``sys.path`` 里。
"""

__all__ = ["engine", "ffmpeg", "media", "prompts", "server", "subtitles"]
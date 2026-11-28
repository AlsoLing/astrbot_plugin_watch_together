"""提示词构造。

把"电影信息 + 播放进度 + 刚刚的台词 + 可选的一帧画面"拼成给大模型的请求。
所有函数都是纯函数，方便单独测试。
"""

from __future__ import annotations

import re
from typing import Sequence

from .subtitles import Cue, build_transcript, cues_in_window, format_timestamp

__all__ = [
    "ambient_prompt",
    "chat_prompt",
    "intro_prompt",
    "clean_reply",
]

_BASE_RULES = """你是「{bot_name}」，正坐在用户旁边一起看电影。你们看的片子是《{title}》。
你的说话方式：像熟人一样小声点评，口语化、有情绪、有自己的偏好，偶尔吐槽、偶尔感叹、偶尔揣测剧情。
硬性要求：
1. 只依据已经播放出来的内容和台词。绝对不要剧透后面会发生什么，也不要编造没有出现过的角色、情节或彩蛋。
2. 不要复述刚说过的台词，不要总结剧情，不要写"这一场景展现了……"这种影评腔。
3. 不要提"字幕""AI""模型""系统"这类字眼，就当一个真的在看电影的人。
4. 不要写动作/神态描写（如"（笑）""（凑近你耳边）"），直接说话。
5. 不要每句都用感叹号，不要连续用同一种开头，避免重复你之前说过的话。
6. 输出只有台词本身，不要引号、不要前缀、不要解释。"""


def _plot_line(plot: str) -> str:
    plot = re.sub(r"\s+", " ", (plot or "").strip())
    if not plot:
        return ""
    return f"你大致知道这部片子的梗概（仅供你理解背景，不要提前说出来）：{plot[:400]}"


def _history_block(history: Sequence[str], limit: int = 4) -> str:
    items = [h.strip() for h in history if h and h.strip()][-limit:]
    if not items:
        return ""
    lines = "\n".join(f"- {item}" for item in items)
    return f"你刚才说过的话（不要重复）：\n{lines}"


def _progress(position: float, duration: float | None) -> str:
    if duration and duration > 0:
        percent = max(0.0, min(100.0, position / duration * 100))
        return f"{format_timestamp(position, True)} / {format_timestamp(duration, True)}（约 {percent:.0f}%）"
    return format_timestamp(position, True)


def ambient_prompt(
    *,
    title: str,
    plot: str,
    position: float,
    duration: float | None,
    cues: Sequence[Cue],
    history: Sequence[str] = (),
    persona: str = "",
    bot_name: str = "TA",
    max_chars: int = 100,
    has_image: bool = False,
) -> tuple[str, str]:
    """生成"顺口点评一句"的提示词。返回 (system, user)。"""

    system = _BASE_RULES.format(bot_name=bot_name or "TA", title=title or "这部片子")
    if persona:
        system += f"\n\n你的人格设定（优先遵循）：\n{persona.strip()[:1200]}"
    system += f"\n\n这次只说一句旁白：{max(20, min(int(max_chars or 100), 200))} 字以内。"

    transcript = build_transcript(cues_in_window(cues, max(0.0, position - 105), position + 6))
    if not transcript:
        transcript = "（这一段几乎没有对白）"

    parts = [
        f"电影：《{title}》",
        _plot_line(plot),
        f"播放进度：{_progress(position, duration)}",
        f"刚刚的台词（带时间戳）：\n{transcript}",
    ]
    history_block = _history_block(history)
    if history_block:
        parts.append(history_block)
    if has_image:
        parts.append("附图是此刻的画面截图。可以结合画面里的人物表情、动作、场景、光线、构图来说一句。")
    else:
        parts.append("这次没有画面，只根据台词和剧情氛围来感受。")
    parts.append("现在，用一句话说出你此刻最想说的（只输出这句话）：")

    return system, "\n\n".join(part for part in parts if part)


def _memory_block(memories: str) -> str:
    memories = (memories or "").strip()
    if not memories:
        return ""
    return f"你们之前一起看片的记忆（可以自然带出来，别生硬背诵）：\n{memories[:1500]}"


def chat_prompt(
    *,
    title: str,
    plot: str,
    position: float,
    duration: float | None,
    cues: Sequence[Cue],
    history: Sequence[str] = (),
    persona: str = "",
    bot_name: str = "TA",
    user_text: str,
    has_image: bool = False,
    memories: str = "",
) -> tuple[str, str]:
    """用户在观影途中插话时的提示词。"""

    system = _BASE_RULES.format(bot_name=bot_name or "TA", title=title or "这部片子")
    if persona:
        system += f"\n\n你的人格设定（优先遵循）：\n{persona.strip()[:1200]}"
    system += "\n\n这次是用户在看电影时跟你说话，请像一起看片的朋友那样回应：1~3 句，自然接话。"

    transcript = build_transcript(cues_in_window(cues, max(0.0, position - 120), position + 6))
    if not transcript:
        transcript = "（这一段几乎没有对白）"

    parts = [
        f"电影：《{title}》",
        _plot_line(plot),
        f"播放进度：{_progress(position, duration)}",
        f"刚刚的台词（带时间戳）：\n{transcript}",
    ]
    history_block = _history_block(history)
    if history_block:
        parts.append(history_block)
    memory_block = _memory_block(memories)
    if memory_block:
        parts.append(memory_block)
    if has_image:
        parts.append("附图是此刻的画面截图。")
    parts.append(f"用户刚刚对你说：{user_text.strip()[:500]}")
    parts.append("直接回应用户（只输出你要说的话）：")
    return system, "\n\n".join(part for part in parts if part)


def intro_prompt(
    *,
    title: str,
    plot: str,
    persona: str = "",
    bot_name: str = "TA",
    memories: str = "",
) -> tuple[str, str]:
    """电影刚开始时的开场白。"""

    system = _BASE_RULES.format(bot_name=bot_name or "TA", title=title or "这部片子")
    if persona:
        system += f"\n\n你的人格设定（优先遵循）：\n{persona.strip()[:1200]}"
    system += "\n\n现在是片头刚放起来，说一句开场白：1~2 句，别念简介，别剧透。"

    parts = [f"电影：《{title}》", _plot_line(plot)]
    memory_block = _memory_block(memories)
    if memory_block:
        parts.append(memory_block)
    parts.append("片头开始了，随便说一句，像是在沙发上坐好准备看片：")
    return system, "\n\n".join(part for part in parts if part)


_QUOTE_CHARS = "“”\"'『』「」《》"


def clean_reply(text: str, max_chars: int = 100) -> str:
    """清理模型输出：去掉引号/前缀/多余换行，并限制长度。"""

    text = (text or "").strip()
    if not text:
        return ""
    # 去掉 "旁白：" "AI：" "- " 之类的前缀
    text = re.sub(r"^(旁白|点评|台词|回复|AI|助手|bot)\s*[:：]\s*", "", text, flags=re.I)
    text = text.strip().strip(_QUOTE_CHARS).strip()
    # 只保留第一段，避免模型输出多段
    text = re.split(r"\n{2,}", text)[0]
    text = re.sub(r"\n+", " ", text).strip()
    # 去掉句末多余空格
    text = re.sub(r"\s{2,}", " ", text)
    if max_chars and len(text) > max_chars:
        cut = text[: max_chars + 1]
        # 尽量在标点处截断
        match = re.search(r"[。！？!?…~～](?=[^。！？!?…~～]*$)", cut)
        if match and match.start() > max_chars * 0.5:
            cut = cut[: match.start() + 1]
        else:
            cut = cut[:max_chars].rstrip() + "…"
        text = cut
    return text.strip()
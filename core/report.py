"""战报导出：把一局作战的完整过程导出为可读文本。

导出目标有两个读者：
* 玩家本人——想复盘"那发炮弹为什么没打中"；
* 想定作者——想定调参时需要逐回合的战况流水。

因此战报不是简单地把日志倒出来，而是分层组织：
    概览（想定、双方编制、胜负点曲线）
    → 逐回合流水（按回合分组，标注行动方与事件类型）
    → 交战明细（每一次开火的命中率、拦截与毁伤结果）
    → 统计汇总

支持 Markdown 与纯文本两种格式；纯文本格式用于直接贴进邮件或聊天窗口。
"""
from __future__ import annotations

import os
import time
from typing import Any

from .enums import Side
from .engine import GameState, LogEntry

# 日志事件类型 → 中文标签
KIND_LABELS: dict[str, str] = {
    "info": "情报",
    "combat": "交战",
    "detect": "探测",
    "ew": "电子战",
    "move": "机动",
    "system": "系统",
    "victory": "战果",
}

SIDE_MARK = {Side.BLUE: "蓝", Side.RED: "红", Side.NEUTRAL: "中立"}


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def collect_stats(state: GameState) -> dict[str, Any]:
    """从日志与状态中提取关键统计指标。

    交战明细按「开火 → 命中 / 拦截 / 脱靶」三类结果归类，
    统计的是**事件条数**而非弹药数——便于快速判断态势而非精算战果。
    """
    shots = 0            # 发射事件数
    hits = 0             # 命中事件数
    kills = 0            # 击毁事件数
    intercepts = 0       # 拦截事件数
    misses = 0           # 未命中事件数
    moves = 0            # 机动事件数
    detections = 0       # 探测/识别事件数

    for e in state.log:
        t = e.text
        if "发射" in t and "→" in t:
            shots += 1
        if "命中" in t and "未命中" not in t:
            hits += 1
        if "击毁" in t:
            kills += 1
        if "拦截" in t:
            intercepts += 1
        if "未命中" in t or "未能命中" in t or "脱靶" in t:
            misses += 1
        if "机动至" in t:
            moves += 1
        if e.kind == "detect" or "识别" in t or "过顶" in t:
            detections += 1

    return {
        "shots": shots,
        "hits": hits,
        "kills": kills,
        "intercepts": intercepts,
        "misses": misses,
        "moves": moves,
        "detections": detections,
        "hit_rate": (hits / shots * 100.0) if shots else 0.0,
        "intercept_rate": (intercepts / shots * 100.0) if shots else 0.0,
    }


# --------------------------------------------------------------------------- #
# 分组
# --------------------------------------------------------------------------- #
def group_by_turn(log: list[LogEntry]) -> list[tuple[int, list[LogEntry]]]:
    """按回合号把日志分组，保持回合内顺序。"""
    out: list[tuple[int, list[LogEntry]]] = []
    for e in log:
        if out and out[-1][0] == e.turn:
            out[-1][1].append(e)
        else:
            out.append((e.turn, [e]))
    return out


def _side_of(e: LogEntry) -> str:
    if e.side is None:
        return "—"
    return SIDE_MARK.get(e.side, e.side.value)


def _fmt_time() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _safe_name(text: str) -> str:
    """把想定名转成可用于文件名的形式。"""
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() or ch in "-_" else "_")
    return "".join(out).strip("_") or "battle"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def build_markdown(state: GameState) -> str:
    """生成 Markdown 战报。"""
    md: list[str] = []
    st = state
    stats = collect_stats(st)

    # -- 标题与概览 --
    md.append(f"# 战报 · {st.map.name}")
    md.append("")
    md.append(f"> 导出时间 {_fmt_time()}｜"
              f"{st.map.width}×{st.map.height} 格（1 格 = {st.map.tile_km:.0f} km）｜"
              f"共 {min(st.turn_count, st.turn_limit)} / {st.turn_limit} 回合")
    md.append("")

    winner = "未分胜负"
    if st.winner is not None:
        winner = f"{st.winner.label} 胜利"
    md.append("## 一、作战结果")
    md.append("")
    md.append("| 项目 | 蓝方 | 红方 |")
    md.append("|---|---|---|")
    md.append(f"| 胜负点 | {st.vp[Side.BLUE]} | {st.vp[Side.RED]} |")
    md.append(f"| 战损 | {st.casualties[Side.BLUE]} | {st.casualties[Side.RED]} |")
    md.append(f"| 残余兵力 | {len(st.units_of(Side.BLUE))} | {len(st.units_of(Side.RED))} |")
    md.append(f"| 卫导完好度 | {st.gps[Side.BLUE] * 100:.0f}% | {st.gps[Side.RED] * 100:.0f}% |")
    md.append("")
    md.append(f"**结论：{winner}**　{('　' + st.win_reason) if st.win_reason else ''}")
    md.append("")

    # -- 编制 --
    md.append("## 二、初始编制")
    md.append("")
    for side in (Side.BLUE, Side.RED):
        units = [u for u in st.scenario.units if u.side is side]
        md.append(f"**{side.label}（{len(units)} 个单位）**")
        md.append("")
        md.append("| 单位 | 兵种 | 作战域 | 装甲 | 传感器 | 主要武器 |")
        md.append("|---|---|---|---|---|---|")
        for u in units:
            sensors = "、".join(s.kind.label for s in u.template.sensors) or "—"
            weapons = "、".join(w.name for w in u.template.weapons[:3]) or "—"
            md.append(f"| {u.name} | {u.template.unit_class.label} | {u.domain.label} | "
                      f"{u.template.armor * 100:.0f}% | {sensors} | {weapons} |")
        md.append("")

    # -- 统计 --
    md.append("## 三、作战统计")
    md.append("")
    md.append("| 指标 | 数值 |")
    md.append("|---|---|")
    md.append(f"| 发射事件 | {stats['shots']} |")
    md.append(f"| 命中 | {stats['hits']}（命中率 {stats['hit_rate']:.0f}%） |")
    md.append(f"| 击毁 | {stats['kills']} |")
    md.append(f"| 被拦截 | {stats['intercepts']}（拦截率 {stats['intercept_rate']:.0f}%） |")
    md.append(f"| 未命中 | {stats['misses']} |")
    md.append(f"| 机动 | {stats['moves']} |")
    md.append(f"| 探测事件 | {stats['detections']} |")
    md.append("")

    # -- 逐回合流水 --
    md.append("## 四、逐回合流水")
    md.append("")
    for turn, entries in group_by_turn(st.log):
        md.append(f"### 第 {turn} 回合")
        md.append("")
        for e in entries:
            tag = KIND_LABELS.get(e.kind, e.kind)
            md.append(f"- **[{tag}]** {_side_of(e)}　{e.text}")
        md.append("")

    # -- 交战明细 --
    md.append("## 五、交战明细")
    md.append("")
    combat = [e for e in st.log if e.kind == "combat"]
    if combat:
        md.append("| 回合 | 方 | 交战经过 |")
        md.append("|---|---|---|")
        for e in combat:
            md.append(f"| {e.turn} | {_side_of(e)} | {e.text} |")
    else:
        md.append("本次作战未发生交战。")
    md.append("")

    return "\n".join(md)


# --------------------------------------------------------------------------- #
# 纯文本
# --------------------------------------------------------------------------- #
def build_text(state: GameState) -> str:
    """生成纯文本战报（等宽对齐，便于在终端或聊天窗口阅读）。"""
    lines: list[str] = []
    st = state
    stats = collect_stats(st)
    bar = "=" * 68
    thin = "-" * 68

    lines.append(bar)
    lines.append(f" 战报 · {st.map.name}")
    lines.append(f" 导出时间 {_fmt_time()}｜"
                 f"{st.map.width}×{st.map.height} 格｜"
                 f"{min(st.turn_count, st.turn_limit)} / {st.turn_limit} 回合")
    lines.append(bar)
    lines.append("")

    winner = "未分胜负"
    if st.winner is not None:
        winner = f"{st.winner.label} 胜利"
    lines.append("【作战结果】")
    lines.append(f"  胜负点    蓝 {st.vp[Side.BLUE]} : {st.vp[Side.RED]} 红")
    lines.append(f"  战损      蓝 {st.casualties[Side.BLUE]} : {st.casualties[Side.RED]} 红")
    lines.append(f"  残余兵力  蓝 {len(st.units_of(Side.BLUE))} : "
                 f"{len(st.units_of(Side.RED))} 红")
    lines.append(f"  结论      {winner}"
                 + (f"　{st.win_reason}" if st.win_reason else ""))
    lines.append("")

    lines.append("【作战统计】")
    lines.append(f"  发射 {stats['shots']}｜命中 {stats['hits']}"
                 f"（{stats['hit_rate']:.0f}%）｜击毁 {stats['kills']}")
    lines.append(f"  被拦截 {stats['intercepts']}"
                 f"（{stats['intercept_rate']:.0f}%）｜未命中 {stats['misses']}"
                 f"｜机动 {stats['moves']}｜探测 {stats['detections']}")
    lines.append("")

    lines.append(thin)
    lines.append("【逐回合流水】")
    lines.append(thin)
    for turn, entries in group_by_turn(st.log):
        lines.append(f"▎第 {turn} 回合")
        for e in entries:
            tag = KIND_LABELS.get(e.kind, e.kind)
            lines.append(f"  [{tag:<4}] {_side_of(e)}  {e.text}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def export_report(state: GameState, directory: str, fmt: str = "md") -> str:
    """把战报写入目录，返回生成的文件路径。"""
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{_safe_name(state.map.name)}-{stamp}"
    if fmt == "txt":
        path = os.path.join(directory, base + ".txt")
        content = build_text(state)
    else:
        path = os.path.join(directory, base + ".md")
        content = build_markdown(state)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


__all__ = [
    "KIND_LABELS", "collect_stats", "group_by_turn",
    "build_markdown", "build_text", "export_report",
]

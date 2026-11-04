"""想定简报与战斗回顾。

两个界面：

* **简报（Briefing）**——开局前展示敌情概览、我方编成、目标点、胜利条件、
  初始环境与预定事件。让玩家在动第一下鼠标之前就知道「这一仗怎么打」。
* **回顾（Debrief）**——战后展示命中率、拦截率、VP 曲线、双方损失对比。
  回答「我这场仗打得怎么样」。

两者都是纯展示层，不影响任何规则结算。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pygame

from core.enums import Domain, Side, UnitClass
from core.i18n import tr
from core.env import environment_for

from . import theme as T


# --------------------------------------------------------------------------- #
# 数据准备
# --------------------------------------------------------------------------- #
@dataclass
class ForceSummary:
    """一方编成的统计摘要。"""

    side: Side
    total: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    air: int = 0
    sea: int = 0
    land: int = 0
    avg_hp: float = 0.0
    total_ammo: int = 0
    interceptors: int = 0
    cost: int = 0
    top_units: list[tuple[str, str]] = field(default_factory=list)  # (名称, 类别)


def summarize_force(state, side: Side) -> ForceSummary:
    """统计一方的编成。"""
    units = [u for u in state.units_of(side, alive_only=False) if not u.is_decoy]
    s = ForceSummary(side=side, total=len(units))
    if not units:
        return s
    hp_sum = 0
    for u in units:
        cls = u.template.unit_class
        s.by_class[cls.label] = s.by_class.get(cls.label, 0) + 1
        if u.domain is Domain.AIR:
            s.air += 1
        elif u.domain in (Domain.SEA, Domain.SUB):
            s.sea += 1
        else:
            s.land += 1
        hp_sum += u.hp
        s.total_ammo += sum(u.ammo.values())
        s.interceptors += u.template.interceptors
        s.cost += u.template.cost
    s.avg_hp = hp_sum / len(units)
    # 列出最有代表性的单位（按造价排序取前 6）
    ranked = sorted(units, key=lambda u: -u.template.cost)[:6]
    s.top_units = [(u.name, u.template.unit_class.label) for u in ranked]
    return s


@dataclass
class BriefingData:
    """简报内容。"""

    title: str
    subtitle: str
    map_size: str
    turn_limit: int
    tile_km: float
    own: ForceSummary
    foe: ForceSummary
    env_label: str
    env_effects: str
    objectives: list[tuple[str, int, str]] = field(default_factory=list)  # (名称, VP, 归属)
    events: list[tuple[int, str]] = field(default_factory=list)        # (回合, 描述)
    victory_hint: str = ""


def build_briefing(state) -> BriefingData:
    """从对局状态构造简报数据。"""
    md = state.map
    side = state.player_side
    env = environment_for(state)
    raw = getattr(state, "raw", {}) or {}
    objs = []
    for t in state.objective_tiles:
        objs.append((t.objective_name or f"目标点 ({t.x},{t.y})",
                     t.victory_points, t.owner.label))
    events = []
    for e in state.events:
        if not e.fired:
            events.append((e.turn, e.label))
    events.sort(key=lambda x: x[0])

    # 胜利条件提示
    total_vp = sum(o[1] for o in objs)
    hint = (f"控制目标点积累胜负点；{state.turn_limit} 回合结束时胜负点高者获胜"
            f"（当前目标点合计 {total_vp} VP/回合）")
    if any(e.type == "reinforce" for e in state.events):
        hint += "。注意：敌方有增援计划"

    return BriefingData(
        title=md.name,
        subtitle=raw.get("description", "") or "",
        map_size=f"{md.width}×{md.height} 格",
        turn_limit=state.turn_limit,
        tile_km=md.tile_km,
        own=summarize_force(state, side),
        foe=summarize_force(state, side.foe),
        objectives=objs,
        env_label=env.label,
        env_effects=env.describe_effects(),
        events=events[:6],
        victory_hint=hint,
    )


@dataclass
class DebriefData:
    """战斗回顾数据。"""

    title: str
    win: bool | None
    reason: str
    turns: int
    turn_limit: int
    vp_blue: int
    vp_red: int
    casualties_blue: int
    casualties_red: int
    survivors_blue: int
    survivors_red: int
    shots_blue: int
    hits_blue: int
    intercepted_blue: int
    kills_blue: int
    shots_red: int
    hits_red: int
    intercepted_red: int
    kills_red: int
    vp_curve: list[tuple[int, int, int]] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)


def build_debrief(state) -> DebriefData:
    """从对局状态构造回顾数据。"""
    sb = state.stats.get(Side.BLUE, {})
    sr = state.stats.get(Side.RED, {})
    hist = list(state.vp_history) or [(state.turn_count, state.vp[Side.BLUE],
                                       state.vp[Side.RED])]

    # 高亮点
    hl: list[str] = []
    for side, st in ((Side.BLUE, sb), (Side.RED, sr)):
        shots = st.get("shots", 0)
        if shots >= 4:
            acc = st.get("hits", 0) / max(1, shots)
            if acc >= 0.65:
                hl.append(f"{side.label}命中率 {acc * 100:.0f}%，火力运用精准")
            elif acc <= 0.25:
                hl.append(f"{side.label}命中率仅 {acc * 100:.0f}%，情报或射界准备不足")
        if st.get("intercepted", 0) >= 3:
            hl.append(f"{side.label}有 {st['intercepted']} 发弹药被拦截——防空网发挥作用")
        if st.get("kills", 0) >= 3:
            hl.append(f"{side.label}击毁 {st['kills']} 个敌方单位")
    if not hl:
        hl.append("交战规模较小，未产生显著战果")

    return DebriefData(
        title=state.map.name,
        win=(state.winner is state.player_side) if state.winner else None,
        reason=state.win_reason,
        turns=min(state.turn_count, state.turn_limit),
        turn_limit=state.turn_limit,
        vp_blue=state.vp[Side.BLUE],
        vp_red=state.vp[Side.RED],
        casualties_blue=state.casualties[Side.BLUE],
        casualties_red=state.casualties[Side.RED],
        survivors_blue=len(state.units_of(Side.BLUE)),
        survivors_red=len(state.units_of(Side.RED)),
        shots_blue=sb.get("shots", 0),
        hits_blue=sb.get("hits", 0),
        intercepted_blue=sb.get("intercepted", 0),
        kills_blue=sb.get("kills", 0),
        shots_red=sr.get("shots", 0),
        hits_red=sr.get("hits", 0),
        intercepted_red=sr.get("intercepted", 0),
        kills_red=sr.get("kills", 0),
        vp_curve=hist,
        highlights=hl[:4],
    )


# --------------------------------------------------------------------------- #
# 绘制：简报
# --------------------------------------------------------------------------- #
def draw_briefing(s: pygame.Surface, d: BriefingData, mouse: tuple[int, int],
                  btn: pygame.Rect) -> None:
    """绘制想定简报浮层。`btn` 为「开始作战」按钮矩形（就地绘制）。"""
    sw, sh = s.get_size()
    w = min(880, sw - 100)
    h = min(600, sh - 70)
    r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)

    dim = pygame.Surface((sw, sh), pygame.SRCALPHA)
    dim.fill((*T.mix(T.BG, (0, 0, 0), 0.55), 205))
    s.blit(dim, (0, 0))

    T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
    # 标题区
    T.draw_text(s, tr("brief.title"), (r.x + 26, r.y + 18), 12, T.CYAN, bold=True)
    T.draw_text(s, d.title, (r.x + 26, r.y + 38), 24, T.TEXT, bold=True)
    if d.subtitle:
        for i, line in enumerate(T.wrap_text(d.subtitle, 12, w - 52)[:2]):
            T.draw_text(s, line, (r.x + 26, r.y + 72 + i * 17), 12, T.TEXT_DIM)
    T.draw_text(s, tr("ui.brief_map_fmt") % (d.map_size, d.tile_km, d.turn_limit,),
                (r.right - 26, r.y + 44), 12, T.TEXT_FAINT, anchor="topright")
    T.hline(s, r.x + 24, r.right - 24, r.y + 108, T.BORDER)

    # 三栏：敌情 / 我方 / 目标
    col_y = r.y + 122
    col_w = (w - 60) // 3
    _draw_force_column(s, pygame.Rect(r.x + 24, col_y, col_w, 210),
                       d.foe, tr("brief.enemy"), hostile=True)
    _draw_force_column(s, pygame.Rect(r.x + 30 + col_w, col_y, col_w, 210),
                       d.own, tr("brief.own"), hostile=False)

    # 目标与胜利条件
    ox = r.x + 36 + col_w * 2
    ow = r.right - 24 - ox
    T.draw_text(s, tr("brief.objective"), (ox, col_y), 12, T.CYAN, bold=True)
    yy = col_y + 22
    for name, vp, owner in d.objectives[:5]:
        col = T.side_color(Side.RED if owner == "红方" else Side.BLUE) \
            if owner in ("蓝方", "红方") else T.TEXT_FAINT
        T.draw_text(s, name[:14], (ox, yy), 11, T.TEXT)
        T.draw_text(s, f"{vp} VP", (ox + ow, yy), 11, T.AMBER, anchor="topright", mono=True)
        yy += 19
    if not d.objectives:
        T.draw_text(s, tr("ui.brief_no_objectives"), (ox, yy), 11, T.TEXT_FAINT)
        yy += 19
    yy += 6
    for line in T.wrap_text(d.victory_hint, 11, ow)[:3]:
        T.draw_text(s, line, (ox, yy), 11, T.mix(T.GREEN, T.TEXT, 0.4))
        yy += 16

    # 环境与事件
    ey = col_y + 224
    T.draw_text(s, tr("brief.env"), (r.x + 24, ey), 12, T.CYAN, bold=True)
    T.draw_text(s, d.env_label, (r.x + 100, ey), 12, T.TEXT)
    for i, line in enumerate(T.wrap_text(d.env_effects, 11, w - 140)[:2]):
        T.draw_text(s, line, (r.x + 100, ey + 20 + i * 16), 11, T.TEXT_DIM)
    if d.events:
        T.draw_text(s, tr("brief.events"), (r.x + 24, ey + 62), 12, T.CYAN, bold=True)
        ex = r.x + 100
        for turn, desc in d.events[:4]:
            T.draw_text(s, f"T{turn}", (ex, ey + 62), 11, T.AMBER, mono=True)
            T.draw_text(s, desc[:16], (ex + 30, ey + 62), 11, T.TEXT_DIM)
            ex += 180

    # 按钮
    hover = btn.collidepoint(mouse)
    pygame.draw.rect(s, T.mix(T.PANEL, T.CYAN, 0.28 if hover else 0.18), btn,
                     border_radius=4)
    pygame.draw.rect(s, T.CYAN, btn, 1, border_radius=4)
    T.draw_text(s, tr("brief.start") + "  [Enter]", btn.center, 14, T.TEXT,
                bold=True, anchor="center")


def _draw_force_column(s: pygame.Surface, r: pygame.Rect, f: ForceSummary,
                       title: str, hostile: bool) -> None:
    """绘制一方的编成摘要。"""
    T.draw_text(s, title, (r.x, r.y), 12, T.RED if hostile else T.BLUE, bold=True)
    y = r.y + 22
    col = T.side_color(f.side)
    T.draw_text(s, tr("ui.brief_total_units") % (f.total,), (r.x, y), 13, T.TEXT, bold=True)
    T.draw_text(s, tr("ui.brief_cost_fmt") % (f.cost,), (r.right, y), 11, T.TEXT_FAINT, anchor="topright")
    y += 22
    # 兵种分布
    T.draw_text(s, tr("ui.brief_domain_fmt") % (f.air, f.sea, f.land,), (r.x, y), 11, T.TEXT_DIM)
    y += 17
    T.draw_text(s, tr("ui.brief_avg_hp_fmt") % (f.avg_hp, f.total_ammo,), (r.x, y), 11, T.TEXT_DIM)
    y += 17
    if f.interceptors:
        T.draw_text(s, tr("ui.brief_interceptors_fmt") % (f.interceptors,), (r.x, y), 11,
                    T.mix(T.CYAN, T.TEXT, 0.3))
        y += 17
    # 主要单位
    T.draw_text(s, tr("ui.brief_main_units"), (r.x, y), 11, T.TEXT_FAINT)
    y += 16
    for name, cls in f.top_units[:5]:
        short = name[:12]
        T.draw_text(s, short, (r.x, y), 11, T.TEXT)
        T.draw_text(s, cls, (r.right, y), 11, T.TEXT_FAINT, anchor="topright")
        y += 17


# --------------------------------------------------------------------------- #
# 绘制：回顾
# --------------------------------------------------------------------------- #
def draw_debrief(s: pygame.Surface, d: DebriefData, mouse: tuple[int, int],
                 btn: pygame.Rect) -> None:
    """绘制战斗回顾浮层。"""
    sw, sh = s.get_size()
    w = min(820, sw - 100)
    h = min(560, sh - 70)
    r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)

    dim = pygame.Surface((sw, sh), pygame.SRCALPHA)
    dim.fill((*T.mix(T.BG, (0, 0, 0), 0.55), 205))
    s.blit(dim, (0, 0))

    T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
    T.draw_text(s, tr("over.stats"), (r.x + 26, r.y + 18), 12, T.CYAN, bold=True)
    T.draw_text(s, d.title, (r.x + 26, r.y + 38), 22, T.TEXT, bold=True)
    if d.win is True:
        title, col = "胜 利", T.GREEN
    elif d.win is False:
        title, col = "失 败", T.RED
    else:
        title, col = "战 平", T.AMBER
    T.draw_text(s, title, (r.right - 26, r.y + 36), 26, col, bold=True, anchor="topright")
    for i, line in enumerate(T.wrap_text(d.reason, 11, w - 52)[:2]):
        T.draw_text(s, line, (r.x + 26, r.y + 70 + i * 16), 11, T.TEXT_DIM)
    T.hline(s, r.x + 24, r.right - 24, r.y + 104, T.BORDER)

    # 交战效能对比
    ty = r.y + 116
    T.draw_text(s, tr("over.efficiency"), (r.x + 26, ty), 12, T.CYAN, bold=True)
    rows = [
        (tr("over.shots"), d.shots_blue, d.shots_red),
        (tr("over.hits"), d.hits_blue, d.hits_red),
        (tr("over.intercepted"), d.intercepted_blue, d.intercepted_red),
        (tr("over.kills"), d.kills_blue, d.kills_red),
        (tr("over.losses"), d.casualties_blue, d.casualties_red),
        (tr("over.survivors"), d.survivors_blue, d.survivors_red),
    ]
    yy = ty + 24
    bar_x = r.x + 150
    bar_w = (w - 320) // 2
    for label, bv, rv in rows:
        T.draw_text(s, label, (r.x + 26, yy), 12, T.TEXT_DIM)
        # 蓝方条
        total = max(bv + rv, 1)
        bw = int(bar_w * bv / total)
        pygame.draw.rect(s, T.PANEL_SUNK, pygame.Rect(bar_x, yy + 2, bar_w, 12),
                         border_radius=3)
        if bw:
            pygame.draw.rect(s, T.side_color(Side.BLUE),
                             pygame.Rect(bar_x, yy + 2, bw, 12), border_radius=3)
        T.draw_text(s, str(bv), (bar_x + bar_w + 8, yy), 12, T.TEXT, mono=True)
        # 红方条
        rb_x = bar_x + bar_w + 80
        rw = int(bar_w * rv / total)
        pygame.draw.rect(s, T.PANEL_SUNK, pygame.Rect(rb_x, yy + 2, bar_w, 12),
                         border_radius=3)
        if rw:
            pygame.draw.rect(s, T.side_color(Side.RED),
                             pygame.Rect(rb_x + bar_w - rw, yy + 2, rw, 12), border_radius=3)
        T.draw_text(s, str(rv), (rb_x + bar_w + 8, yy), 12, T.TEXT, mono=True)
        yy += 24
    T.draw_text(s, tr("over.blue"), (bar_x, ty + 4), 11, T.side_color(Side.BLUE),
                bold=True)
    T.draw_text(s, tr("over.red"), (bar_x + bar_w + 80, ty + 4), 11,
                T.side_color(Side.RED), bold=True)

    # VP 曲线
    cy = yy + 12
    ch = 96
    T.draw_text(s, tr("over.vp_curve"), (r.x + 26, cy), 12, T.CYAN, bold=True)
    curve = pygame.Rect(r.x + 26, cy + 20, w - 52, ch)
    pygame.draw.rect(s, T.PANEL_SUNK, curve, border_radius=3)
    _draw_vp_curve(s, curve, d.vp_curve)

    # 要点
    hy = curve.bottom + 12
    T.draw_text(s, tr("over.highlights"), (r.x + 26, hy), 12, T.CYAN, bold=True)
    for i, line in enumerate(d.highlights[:3]):
        T.draw_text(s, "· " + line, (r.x + 76, hy), 11, T.TEXT_DIM)

    # 按钮
    hover = btn.collidepoint(mouse)
    pygame.draw.rect(s, T.mix(T.PANEL, T.CYAN, 0.28 if hover else 0.18), btn,
                     border_radius=4)
    pygame.draw.rect(s, T.CYAN, btn, 1, border_radius=4)
    T.draw_text(s, tr("over.back") + "  [Enter]", btn.center, 14, T.TEXT,
                bold=True, anchor="center")


def _draw_vp_curve(s: pygame.Surface, rect: pygame.Rect,
                   curve: list[tuple[int, int, int]]) -> None:
    """绘制胜负点折线图。"""
    if len(curve) < 2:
        return
    max_vp = max(max(c[1], c[2]) for c in curve) or 1
    n = len(curve)
    pad = 8
    iw = rect.width - pad * 2
    ih = rect.height - pad * 2

    def pt(idx: int, vp: int) -> tuple[int, int]:
        x = rect.x + pad + int(iw * idx / max(1, n - 1))
        y = rect.bottom - pad - int(ih * vp / max_vp)
        return (x, y)

    for side_i, color in ((1, T.side_color(Side.BLUE)), (2, T.side_color(Side.RED))):
        pts = [pt(i, c[side_i]) for i, c in enumerate(curve)]
        if len(pts) >= 2:
            pygame.draw.lines(s, color, False, pts, 2)
        for p in pts:
            pygame.draw.circle(s, color, p, 2)
    # 末值标注
    last = curve[-1]
    T.draw_text(s, str(last[1]), (pt(n - 1, last[1])[0] - 6,
                                   pt(n - 1, last[1])[1] - 12), 11,
                T.side_color(Side.BLUE), anchor="topright", mono=True)
    T.draw_text(s, str(last[2]), (pt(n - 1, last[2])[0] + 6,
                                  pt(n - 1, last[2])[1] - 12), 11,
                T.side_color(Side.RED), anchor="topleft", mono=True)


__all__ = ["ForceSummary", "BriefingData", "DebriefData",
           "summarize_force", "build_briefing", "build_debrief",
           "draw_briefing", "draw_debrief"]

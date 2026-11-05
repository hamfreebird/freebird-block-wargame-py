"""悬停信息卡：光标停在单位上时展示的即时情报。

设计目标是「悬停即得信息，无需选中」——玩家在战场上扫视时，
不必逐个点选就能判断目标威胁、弹药余量与防护水平。

卡片内容分三块：
* **识别状态**：我方对该目标的探测等级（未识别时只显示模糊信息）；
* **战斗数据**：血量、弹药、工事、机动点；
* **威胁提示**：敌方单位额外显示其拦截/打击包线与是否已锁定我方。
"""
from __future__ import annotations

import pygame

from core.enums import Detection, Domain, Side
from core.model import Unit

from core.i18n import tr

from . import theme as T


def _det_color(lvl: Detection) -> tuple[int, int, int]:
    return {
        Detection.NONE: T.TEXT_FAINT,
        Detection.CONTACT: T.AMBER,
        Detection.CLASSIFIED: (150, 200, 240),
        Detection.TRACKED: T.RED,
    }.get(lvl, T.TEXT_DIM)


def build_rows(unit: Unit, viewer_side: Side, known: Detection,
               state=None) -> list[tuple[str, str, tuple[int, int, int]]]:
    """构造信息卡的行：(键, 值, 颜色)。"""
    rows: list[tuple[str, str, tuple[int, int, int]]] = []
    rows.append((tr("ui.card.type"),
                 f"{unit.template.unit_class.label} · {unit.domain.label}", T.TEXT_DIM))

    if unit.side is viewer_side:
        # 己方单位：完整信息
        rows.append((tr("ui.card.hp"), f"{unit.hp:.0f} / {unit.template.hp:.0f}",
                     T.hp_color(unit.hp_ratio)))
        rows.append((tr("ui.card.mp"), f"{unit.mp:.1f} / {unit.template.mp:.1f}", T.TEXT_DIM))
        # 油料对所有兵种都有意义了：地面机动同样烧油，跑空了就走不动
        low_fuel = 0.35 if unit.domain is Domain.AIR else 0.15
        rows.append((tr("ui.card.fuel"), f"{unit.fuel * 100:.0f}%",
                     T.RED if unit.fuel < low_fuel else T.TEXT_DIM))
        rows.append((tr("ui.card.spares"), f"{unit.spares:.0f}",
                     T.RED if unit.spares <= 0 else T.TEXT_DIM))
        if unit.ammo:
            ammo_parts = []
            for w in unit.template.weapons:
                n = unit.ammo.get(w.id, 0)
                ammo_parts.append(f"{w.name.split(' ')[0][:6]}×{n}")
            rows.append((tr("ui.card.ammo"), " ".join(ammo_parts), T.AMBER))
        tile = state.map.at(unit.x, unit.y) if state else None
        if tile is not None and tile.fort > 0 and tile.fort_owner is unit.side:
            rows.append((tr("ui.card.fort"), tr("ui.card.fort_fmt") % (tile.fort,), T.GREEN))
        if unit.entrench > 0:
            rows.append((tr("ui.card.entrench"),
                         tr("ui.card.entrench_fmt") % (unit.entrench,), T.GREEN))
        if unit.radar_on and any(s.kind.value == "radar" for s in unit.template.sensors):
            rows.append((tr("ui.card.radar"), tr("ui.card.radar_on"), T.RED))
        if unit.jam_on:
            rows.append((tr("ui.card.jam"), tr("ui.card.jam_on"), T.PURPLE))
    else:
        # 敌方单位：按识别等级给出对应深度的信息
        if known is Detection.CONTACT:
            rows.append((tr("ui.card.detect"), tr("ui.card.contact_only"), T.AMBER))
            rows.append((tr("ui.card.hint"), tr("ui.card.hint_scout"), T.TEXT_FAINT))
        else:
            rows.append((tr("ui.card.detect"), known.label, _det_color(known)))
            rows.append((tr("ui.card.hp"), f"{unit.hp:.0f} / {unit.template.hp:.0f}",
                         T.hp_color(unit.hp_ratio)))
            if known is Detection.TRACKED:
                if unit.ammo:
                    total = sum(unit.ammo.values())
                    rows.append((tr("ui.card.ammo"), tr("ui.card.ammo_approx_fmt") % (total,),
                                 T.AMBER))
                tile = state.map.at(unit.x, unit.y) if state else None
                if tile is not None and tile.fort > 0 and tile.fort_owner is unit.side:
                    rows.append((tr("ui.card.fort"), tr("ui.card.fort_fmt") % (tile.fort,), T.GREEN))
                rng = unit.max_intercept_range()
                if rng > 0:
                    rows.append((tr("ui.card.intercept"),
                                 tr("ui.card.intercept_fmt") % (f"{rng:.1f}",), T.RED))
                if state is not None:
                    mine = [u for u in state.units_of(viewer_side)
                            if u.alive and u.domain is Domain.LAND]
                    for m in mine:
                        for w in m.usable_weapons():
                            if unit.domain not in w.targets:
                                continue
                            import core.rules as rules
                            if rules.dist_units(m, unit) <= w.range_max:
                                rows.append((tr("ui.card.threat"),
                                             tr("ui.card.threat_fmt") % (m.name,), T.RED))
                                break
                        break
    return rows


class InspectCard:
    """悬停信息卡的绘制器（跟随光标，自动避让屏幕边缘）。"""

    ROW_H = 15
    HEAD_H = 26
    PAD = 9

    def __init__(self) -> None:
        self.rect = pygame.Rect(0, 0, 0, 0)

    def draw(self, surf: pygame.Surface, unit: Unit, viewer_side: Side,
             known: Detection, state, mouse: tuple[int, int]) -> None:
        rows = build_rows(unit, viewer_side, known, state)
        if not rows:
            return
        w = 236
        h = self.HEAD_H + len(rows) * self.ROW_H + self.PAD * 2

        x = mouse[0] + 18
        y = mouse[1] + 14
        sw, sh = surf.get_size()
        if x + w > sw - 6:
            x = mouse[0] - w - 14
        if y + h > sh - 6:
            y = max(4, mouse[1] - h - 10)
        r = pygame.Rect(int(x), int(y), w, h)
        self.rect = r

        panel = pygame.Surface(r.size, pygame.SRCALPHA)
        panel.fill((12, 17, 24, 232))
        surf.blit(panel, r.topleft)
        pygame.draw.rect(surf, T.BORDER_HI, r, 1, border_radius=4)

        # 标题：单位名 + 阵营色条
        side_col = T.side_color(unit.side, "edge")
        pygame.draw.rect(surf, side_col, pygame.Rect(r.x + 1, r.y + 1, 3, r.height - 2))
        title_col = T.TEXT if unit.side is viewer_side else T.WHITE
        T.draw_text(surf, unit.name, (r.x + self.PAD, r.y + 6), 12, title_col, bold=True)
        T.draw_text(surf, unit.side.label, (r.right - self.PAD, r.y + 7), 10, side_col,
                    anchor="topright")

        yy = r.y + self.HEAD_H
        for key, val, col in rows:
            T.draw_text(surf, key, (r.x + self.PAD, yy), 11, T.TEXT_FAINT)
            T.draw_text(surf, val, (r.right - self.PAD, yy), 11, col, anchor="topright")
            yy += self.ROW_H


__all__ = ["InspectCard", "build_rows"]

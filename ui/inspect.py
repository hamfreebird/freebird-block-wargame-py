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
    rows.append(("类型", f"{unit.template.unit_class.label} · {unit.domain.label}", T.TEXT_DIM))

    if unit.side is viewer_side:
        # 己方单位：完整信息
        rows.append(("血量", f"{unit.hp:.0f} / {unit.template.hp:.0f}", T.hp_color(unit.hp_ratio)))
        rows.append(("机动点", f"{unit.mp:.1f} / {unit.template.mp:.1f}", T.TEXT_DIM))
        if unit.domain is Domain.AIR:
            rows.append(("燃油", f"{unit.fuel * 100:.0f}%",
                         T.RED if unit.fuel < 0.35 else T.TEXT_DIM))
        if unit.ammo:
            ammo_parts = []
            for w in unit.template.weapons:
                n = unit.ammo.get(w.id, 0)
                ammo_parts.append(f"{w.name.split(' ')[0][:6]}×{n}")
            rows.append(("弹药", " ".join(ammo_parts), T.AMBER))
        tile = state.map.at(unit.x, unit.y) if state else None
        if tile is not None and tile.fort > 0 and tile.fort_owner is unit.side:
            rows.append(("工事", f"Lv{tile.fort} 阵地", T.GREEN))
        if unit.entrench > 0:
            rows.append(("掩体", f"驻守 {unit.entrench} 级", T.GREEN))
        if unit.radar_on and any(s.kind.value == "radar" for s in unit.template.sensors):
            rows.append(("雷达", "开机（暴露）", T.RED))
        if unit.jam_on:
            rows.append(("干扰", "工作中", T.PURPLE))
    else:
        # 敌方单位：按识别等级给出对应深度的信息
        if known is Detection.CONTACT:
            rows.append(("识别", "仅接触：不知兵种", T.AMBER))
            rows.append(("提示", "抵近侦察以获取详情", T.TEXT_FAINT))
        else:
            rows.append(("识别", known.label, _det_color(known)))
            rows.append(("血量", f"{unit.hp:.0f} / {unit.template.hp:.0f}",
                         T.hp_color(unit.hp_ratio)))
            if known is Detection.TRACKED:
                if unit.ammo:
                    total = sum(unit.ammo.values())
                    rows.append(("弹药", f"约 {total} 发", T.AMBER))
                tile = state.map.at(unit.x, unit.y) if state else None
                if tile is not None and tile.fort > 0 and tile.fort_owner is unit.side:
                    rows.append(("工事", f"Lv{tile.fort} 阵地", T.GREEN))
                rng = unit.max_intercept_range()
                if rng > 0:
                    rows.append(("防空包线", f"{rng:.1f} 格", T.RED))
                if state is not None:
                    mine = [u for u in state.units_of(viewer_side)
                            if u.alive and u.domain is Domain.LAND]
                    for m in mine:
                        for w in m.usable_weapons():
                            if unit.domain not in w.targets:
                                continue
                            import core.rules as rules
                            if rules.dist_units(m, unit) <= w.range_max:
                                rows.append(("威胁", f"{m.name} 可直接打击", T.RED))
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
        T.draw_text(surf, unit.side.label, (r.right - self.PAD, r.y + 7), 9, side_col,
                    anchor="topright")

        yy = r.y + self.HEAD_H
        for key, val, col in rows:
            T.draw_text(surf, key, (r.x + self.PAD, yy), 10, T.TEXT_FAINT)
            T.draw_text(surf, val, (r.right - self.PAD, yy), 10, col, anchor="topright")
            yy += self.ROW_H


__all__ = ["InspectCard", "build_rows"]

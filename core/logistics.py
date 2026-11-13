"""补给链：补给源、覆盖范围、断粮判定。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import Domain, Facility, Side
from .geo import dist
from .model import Tile, Unit

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

# --------------------------------------------------------------------------- #
# 补给线与纵深
# --------------------------------------------------------------------------- #
# 各类补给设施的覆盖半径与承载力（同时能补给多少个单位）。
# 承载力设计原则：一支合成团的常规兵力（约 12–16 个单位）应当能被
# 1~2 个枢纽覆盖，这样「补给线被打断」是**战术事件**（枢纽被打击、
# 部队深入敌后超出半径），而不是开局就必然发生的常态。
SUPPLY_PROFILE: dict[Facility, tuple[float, int]] = {
    Facility.DEPOT: (10.0, 12),     # 后勤枢纽：覆盖最广、承载最强
    Facility.HQ: (6.0, 6),          # 指挥部：就近补给
    Facility.AIRBASE: (6.0, 8),     # 机场：主要保障航空兵
    Facility.PORT: (7.0, 8),        # 港口：保障舰艇
    Facility.RADAR_SITE: (3.0, 2),
    Facility.SAM_SITE: (3.0, 2),
}


@dataclass
class SupplySource:
    """一个补给源：后勤设施及其覆盖能力。"""

    tile: Tile
    radius: float
    capacity: int
    side: Side

    @property
    def x(self) -> int:
        return self.tile.x

    @property
    def y(self) -> int:
        return self.tile.y

    @property
    def name(self) -> str:
        return f"{self.tile.facility.label}({self.x},{self.y})"

    @property
    def operational(self) -> bool:
        return self.tile.operational and self.tile.owner is self.side

    def covers(self, unit: Unit) -> bool:
        return dist((self.x, self.y), (unit.x, unit.y)) <= self.radius


def supply_sources(state: "GameState", side: Side) -> list[SupplySource]:
    """列出该阵营所有可用作补给源的地块（按覆盖半径降序）。"""
    out: list[SupplySource] = []
    for t in state.map.iter_tiles():
        if t.facility not in SUPPLY_PROFILE:
            continue
        if t.owner is not side:
            continue
        radius, cap = SUPPLY_PROFILE[t.facility]
        out.append(SupplySource(tile=t, radius=radius, capacity=cap, side=side))
    out.sort(key=lambda s: (-s.radius, s.x, s.y))
    return out


def assign_supply(state: "GameState", side: Side) -> dict[int, SupplySource]:
    """为该阵营所有单位分配补给源。

    分配规则（贪心，按距离由近及远）：
    * 单位必须在补给源的覆盖半径内；
    * 每个补给源有承载力上限，超出的单位得不到补给——这就是「纵深」：
      深入敌后的部队往往因为补给源被打瘫或承载力耗尽而断粮；
    * 分配结果稳定：同一态势下每次结算得到相同结果。

    卫星（天基）不需要补给，不参与分配。
    """
    sources = [s for s in supply_sources(state, side) if s.operational]
    # 用坐标作占用计数键：id() 在对象被回收后可能被复用，导致计数串台
    used: dict[tuple[int, int], int] = {}
    out: dict[int, SupplySource] = {}
    units = [u for u in state.units_of(side)
             if not u.is_decoy and u.domain is not Domain.SPACE]
    # 按「离最近补给源的距离」排序：孤立单位先占位，避免被近处单位挤掉
    def nearest_dist(u: Unit) -> float:
        ds = [dist((s.x, s.y), (u.x, u.y)) for s in sources]
        return min(ds) if ds else 1e9

    units.sort(key=lambda u: (nearest_dist(u), u.uid))
    for u in units:
        best = None
        best_d = 1e9
        for s in sources:
            key = (s.x, s.y)
            if used.get(key, 0) >= s.capacity:
                continue
            d = dist((s.x, s.y), (u.x, u.y))
            if d > s.radius:
                continue
            if d < best_d:
                best, best_d = s, d
        if best is not None:
            out[u.uid] = best
            key = (best.x, best.y)
            used[key] = used.get(key, 0) + 1
    return out


def supply_source_for(state: "GameState", unit: Unit) -> SupplySource | None:
    """查询单个单位当前挂接的补给源。"""
    links = getattr(state, "supply_links", None)
    if not links:
        return None
    src = links.get(unit.uid)
    return src if src is not None else None


def supply_status(state: "GameState", unit: Unit) -> tuple[bool, str]:
    """单位补给状态：(是否有补给, 说明)。"""
    src = supply_source_for(state, unit)
    if src is None:
        return False, "补给中断"
    return True, f"补给线：{src.name}"


def facility_service_ok(tile: Tile | None) -> bool:
    return tile is not None and tile.operational

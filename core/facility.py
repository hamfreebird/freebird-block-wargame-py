"""设施打击与野战服务：机场 / 港口 / 雷达站 / 补给点。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .adn import AirDefenseNetwork
from .enums import Domain, Facility, Side, UnitClass
# 这几个「环境依赖」走模块属性访问（``ew.jam_strength_at`` 而不是
# ``from .ew import jam_strength_at``）：自检脚本需要把它们临时换成
# 确定性桩值，而打补丁只能改**定义处模块**的属性。函数内直接引用
# 的名字是另一份绑定，补丁打不到。
from . import ew, geo
from .geo import RANGE_TOLERANCE, dist
from .model import Tile, Unit, Weapon

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

# --------------------------------------------------------------------------- #
# 固定设施打击与基地勤务
# --------------------------------------------------------------------------- #
FACILITY_SIZE = {
    Facility.AIRBASE: 1.20,
    Facility.PORT: 1.15,
    Facility.DEPOT: 1.05,
    Facility.CITY: 1.20,
    Facility.RADAR_SITE: 0.95,
    Facility.SAM_SITE: 1.00,
    Facility.HQ: 0.90,
}


@dataclass
class FacilityStrike:
    """对机场/港口等固定设施的火力打击解算。"""

    ok: bool = False
    reason: str = ""
    pk: float = 0.0
    damage: float = 0.0
    distance: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)


def facility_strike_profile(state: "GameState", attacker: Unit, tile: Tile,
                            weapon: Weapon) -> FacilityStrike:
    """评估对设施实施打击的可行性与效果。"""
    p = FacilityStrike(distance=dist((attacker.x, attacker.y), (tile.x, tile.y)))
    if not attacker.alive:
        p.reason = "攻击单位已不存在"
        return p
    if tile.facility is Facility.NONE:
        p.reason = "该地块没有可打击的设施"
        return p
    if tile.owner is attacker.side:
        p.reason = "不能打击己方设施"
        return p
    if not weapon.vs_facility:
        p.reason = f"{weapon.name} 不具备对固定设施的打击能力"
        return p
    if attacker.ammo.get(weapon.id, 0) <= 0:
        p.reason = "弹药耗尽"
        return p
    if tile.facility_hp <= 0:
        p.reason = "该设施已被摧毁"
        return p
    if p.distance < weapon.range_min:
        p.reason = f"低于最小射程 {weapon.range_min:.1f} 格"
        return p
    if p.distance > weapon.range_max + RANGE_TOLERANCE:
        p.reason = f"超出射程 {weapon.range_max:.1f} 格"
        return p
    if p.distance > 0.05:
        if not geo.has_los(state, (attacker.x, attacker.y), (tile.x, tile.y),
                       observer_air=attacker.domain is Domain.AIR):
            p.reason = "无通视条件，无法对设施实施精确打击"
            return p

    f: dict[str, float] = {}
    edge = (p.distance - weapon.range_min) / max(0.1, weapon.range_max - weapon.range_min)
    f["射程包线"] = 1.0 if edge < 0.75 else max(0.55, 1.0 - (edge - 0.75) * 1.4)
    if weapon.strategic:
        f["电子对抗"] = 1.0        # 惯性/天文制导，不受战场干扰影响
    elif weapon.guidance.vulnerable_to_ew:
        jam_t = ew.jam_strength_at(state, (tile.x, tile.y), attacker.side)
        jam_a = ew.jam_strength_at(state, (attacker.x, attacker.y), attacker.side)
        f["电子对抗"] = max(0.25, 1.0 - (jam_t * 0.55 + jam_a * 0.35))
    else:
        f["电子对抗"] = 1.0
    if weapon.guidance.value == "gps_ins":
        f["导航精度"] = ew.gps_integrity(state, attacker.side)
    f["设施尺寸"] = FACILITY_SIZE.get(tile.facility, 1.0)
    f["战损影响"] = 0.75 + 0.25 * attacker.hp_ratio

    pk = weapon.pk
    for v in f.values():
        pk *= v
    p.factors = f
    p.pk = max(0.03, min(0.97, pk))
    p.damage = weapon.damage * FACILITY_SIZE.get(tile.facility, 1.0)
    p.ok = True

    # 设施本身不还手，但周边防空网与近防炮会拦截
    survive = 1.0
    names: list[str] = []
    if tile.owner in (Side.BLUE, Side.RED):
        net = AirDefenseNetwork.build(state, tile.owner, (tile.x, tile.y), weapon)
        for n in net.nodes[:5]:
            survive *= (1 - n.pk)
            names.append(f"{n.name}·{n.role} ({n.pk * 100:.0f}%)")
    p.intercept_pk = 1 - survive
    p.interceptors = names
    return p


def service_tile(state: "GameState", unit: Unit) -> Tile | None:
    """返回可为该单位提供燃油/弹药/维修补给的地块，无则 None。

    * 航空兵：己方完好机场；或同格的己方航母（浮动机场）
    * 水面舰艇/潜艇：己方完好港口
    * 地面部队：己方完好后勤枢纽/机场/指挥部
    """
    t = state.map.at(unit.x, unit.y)
    if t is None:
        return None
    dom = unit.domain
    if dom is Domain.AIR:
        if t.facility is Facility.AIRBASE and t.owner is unit.side and t.operational:
            return t
        for host in state.units_at(unit.x, unit.y):
            if host is unit or host.side is not unit.side:
                continue
            if host.template.unit_class is UnitClass.CARRIER and host.hp_ratio > 0.3:
                return t
        return None
    if dom in (Domain.SEA, Domain.SUB):
        if t.facility is Facility.PORT and t.owner is unit.side and t.operational:
            return t
        return None
    if dom in (Domain.LAND, Domain.INFRA):
        if t.facility in (Facility.DEPOT, Facility.AIRBASE, Facility.HQ) \
                and t.owner is unit.side and t.operational:
            return t
    return None




# 基地容量：一个机场格最多停放多少架
AIRBASE_CAPACITY = 4

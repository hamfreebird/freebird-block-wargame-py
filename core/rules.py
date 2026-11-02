"""规则运算：距离、视线、探测链、电子战、命中与拦截解算。

本模块只做纯计算，不修改游戏状态，便于单测与 UI 复用（例如预测命中率）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from .enums import Detection, Domain, Facility, SensorKind, Side, Terrain, UnitClass, WeaponKind
from .model import Tile, Unit, Weapon
from .env import environment_for

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
NEIGHBORS_8 = [
    (-1, -1), (0, -1), (1, -1),
    (-1, 0), (1, 0),
    (-1, 1), (0, 1), (1, 1),
]

# 地形对视线遮挡的阈值（米）：来射线高出地形高度不足该值即被遮挡
LOS_BLOCK_THRESHOLD = {
    Terrain.MOUNTAIN: 10,
    Terrain.HILLS: 110,
    Terrain.URBAN: 70,
    Terrain.FOREST: 150,
    Terrain.PLAIN: 320,
    Terrain.DESERT: 320,
    Terrain.MARSH: 300,
    Terrain.COAST: 260,
    Terrain.WATER: 400,
}

AIR_OBSERVER_HEIGHT = 3200.0
AIR_TARGET_HEIGHT = 2200.0

# 反卫星交战的抽象距离：轨道目标不与地面共享平面坐标，
# 只要双方均有反卫星手段即视为可交战，射程按此常数校验。
ORBITAL_ENGAGEMENT_DISTANCE = 5.0


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def dist(a: tuple[int, int], b: tuple[int, int]) -> float:
    """欧氏距离（格）。"""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist_units(u1: Unit, u2: Unit) -> float:
    return dist((u1.x, u1.y), (u2.x, u2.y))


# 诱饵信号特征：不同诱饵只在对应频段上"像真的"。
# 数值是探测距离的倍率（>1 表示比真实目标更好找），缺失视为完全不可见。
DECOY_SIGNATURE: dict[str, dict[SensorKind, float]] = {
    "radar":  {SensorKind.RADAR: 1.30, SensorKind.ESM: 1.45},          # 角反射器/干扰机：只骗雷达
    "ir":     {SensorKind.IR: 1.25, SensorKind.OPTICAL: 0.55},         # 热源/红外诱饵：只骗热像
    "visual": {SensorKind.OPTICAL: 1.20},                              # 充气假目标：只骗光学
    "multi":  {SensorKind.RADAR: 1.10, SensorKind.ESM: 1.20,
               SensorKind.IR: 1.10, SensorKind.OPTICAL: 0.80},         # 多光谱诱饵：代价高
}

DECOY_LABELS = {"radar": "雷达诱饵", "ir": "红外诱饵", "visual": "目视假目标",
                "multi": "多光谱诱饵", "thermal": "红外诱饵"}


def decoy_signature(kind: str, sensor: SensorKind) -> float:
    """诱饵在指定传感器频段上的信号强度倍率；0 表示不可见。"""
    return DECOY_SIGNATURE.get(kind, {}).get(sensor, 0.0)


# 近距离（格）：进入该距离内，光学/热像即可完成识别与跟踪。
# 否则「工事隐蔽 + 需跟踪才能开火」会让据守步兵在贴脸距离上完全无法被直瞄火力打击。
CLOSE_TRACK_RANGE = 1.6

# 射程判定容差（格）。机动作战是 8 方向，对角相邻的欧氏距离为 1.414，
# 若不给容差，射程 1.0–1.4 的直瞄武器将永远打不到斜对角的敌人——
# 近战单位会陷入「贴脸却无法开火」的死角。容差取半格，只影响相邻圈。
RANGE_TOLERANCE = 0.45


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> Iterable[tuple[int, int]]:
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    while True:
        yield x, y
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy


def has_los(state: "GameState", a: tuple[int, int], b: tuple[int, int],
            observer_air: bool = False, target_air: bool = False) -> bool:
    """判断 a 与 b 之间是否存在通视。

    采用「高度剖面」模型：沿线插值出视线高度，与途经地块海拔比较。
    空中平台默认获得高度优势（可越过地形遮蔽）。
    """
    md = state.map
    ta = md.at(*a)
    tb = md.at(*b)
    if ta is None or tb is None:
        return False
    if observer_air:
        return True  # 空中平台居高临下，忽略地形遮蔽

    h1 = ta.elevation
    h2 = tb.elevation + (AIR_TARGET_HEIGHT if target_air else 0)
    total = dist(a, b)
    if total < 1e-6:
        return True

    pts = list(_bresenham(a[0], a[1], b[0], b[1]))
    for (x, y) in pts[1:-1]:
        t = md.at(x, y)
        if t is None:
            continue
        f = dist(a, (x, y)) / total
        line_h = h1 + (h2 - h1) * f
        thresh = LOS_BLOCK_THRESHOLD.get(t.terrain, 300)
        if t.elevation - line_h > thresh:
            return False
    return True


# --------------------------------------------------------------------------- #
# 电子战
# --------------------------------------------------------------------------- #
def jam_strength_at(state: "GameState", pos: tuple[int, int], victim_side,
                    band: str = "radar") -> float:
    """返回某位置上、针对 victim_side 的指定频段干扰强度（0..1）。

    频段化之后，电子干扰不再是「一刀切」的单一乘数：

    * `radar` 频段 —— 压制雷达探测与雷达/半主动制导弹药；
    * `comm` 频段 —— 切断数据链与协同交战（信火一体失效）；
    * `nav` 频段 —— 压制卫星导航，使精确制导弹药退化到惯导精度。

    干扰机只能干扰它**实际具备**的频段，因此针对性关闭某类装备
    （关雷达 / 断数据链 / 转惯导）才是有效的反制。
    """
    total = 0.0
    for u in state.units:
        if not u.alive or u.side is victim_side:
            continue
        tpl = u.template
        if tpl.jam_strength <= 0 or not u.jam_on:
            continue
        if band not in tpl.jam_bands:
            continue
        r = tpl.jam_radius
        if r <= 0:
            continue
        d = dist(pos, (u.x, u.y))
        if d > r:
            continue
        falloff = 1.0 - (d / r) ** 2 * 0.55   # 边缘衰减
        # 不同频段的干扰效能不同：默认同权重，可由数据文件单独指定
        weight = tpl.jam_band_weights.get(band, 1.0)
        total += tpl.jam_strength * falloff * weight
    return min(0.85, total)


def gps_integrity(state: "GameState", side) -> float:
    """卫星导航完好度：受敌方干扰与反卫星作战影响。"""
    return max(0.25, min(1.0, state.gps[side]))


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
@dataclass
class DetectionResult:
    level: Detection = Detection.NONE
    sensor: str = ""
    effective_range: float = 0.0
    distance: float = 0.0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.level is not Detection.NONE


def sensor_effective_range(state: "GameState", observer: Unit, target: Unit,
                           sensor_range: float, kind: SensorKind,
                           counter_stealth: float = 0.0) -> tuple[float, bool]:
    """计算某传感器对目标的实际探测距离，返回 (距离, 是否满足通视)。"""
    md = state.map
    obs_tile = md.at(observer.x, observer.y)
    tgt_tile = md.at(target.x, target.y)
    if obs_tile is None or tgt_tile is None:
        return 0.0, False

    r = sensor_range

    # 0) 诱饵：只在匹配的频段上产生回波/热信号
    decoy = target.is_decoy
    if decoy:
        sig = decoy_signature(target.decoy_kind, kind)
        if sig <= 0:
            return 0.0, False
        r *= sig

    # 1) 观测方高度优势（雷达波视距）
    if kind is SensorKind.RADAR:
        if observer.domain is Domain.AIR and observer.mission == "cap":
            r *= 1.15      # 巡逻剖面下雷达保持对空搜索
        if observer.domain is Domain.AIR:
            r *= 1.20
        else:
            r *= 1.0 + min(0.30, obs_tile.elevation / 6000.0)
        # 目标高度层
        r *= target.altitude.radar_factor

    # 2) 隐身（米波/超视距雷达可部分抵消隐身设计）；假目标不享受隐身外形
    if kind in (SensorKind.RADAR, SensorKind.IR) and not decoy:
        eff_stealth = max(0.0, target.template.stealth * (1.0 - counter_stealth))
        r *= max(0.08, 1.0 - eff_stealth)

    # 3) 地形遮蔽（地面目标）+ 工事伪装
    if target.domain in (Domain.LAND, Domain.INFRA, Domain.SEA, Domain.SUB):
        r *= tgt_tile.terrain.cover
    if target.domain is Domain.LAND and target.entrench > 0:
        r *= max(0.55, 1.0 - 0.14 * target.entrench)
    # 野战工事：掩体与伪装网
    if tgt_tile.fort > 0 and tgt_tile.fort_owner is target.side:
        r *= max(0.62, 1.0 - 0.07 * tgt_tile.fort)
    if tgt_tile.smoke > 0:
        r *= 0.35

    # 3.5) 被压制：雷达站被打击后效能大幅下降（反辐射/压制打击的战果）
    if observer.suppressed > 0 and kind is SensorKind.RADAR:
        r *= 0.5

    # 4) 电子干扰（仅影响主动/电磁类传感器，按频段分别结算）
    if kind in (SensorKind.RADAR, SensorKind.ESM):
        r *= 1.0 - jam_strength_at(state, (observer.x, observer.y), observer.side,
                                   "radar") * 0.85

    # 4.5) 战场环境：时段与天候改变光电/红外/雷达的探测效能
    env = environment_for(state)
    r *= env.factor(kind)

    # 5) 通视
    los = has_los(
        state, (observer.x, observer.y), (target.x, target.y),
        observer_air=observer.domain is Domain.AIR,
        target_air=target.domain in (Domain.AIR, Domain.SPACE),
    )
    if kind is SensorKind.RADAR and target.domain is Domain.AIR:
        los = True  # 对空探测不受地面起伏影响
    return max(0.0, r), los


def evaluate_detection(state: "GameState", observer: Unit, target: Unit) -> DetectionResult:
    """单观测者对单目标的探测判定，取所有传感器中最高等级。"""
    best = DetectionResult()
    d = dist_units(observer, target)

    for s in observer.template.sensors:
        kind = s.kind

        # 主动雷达需开机
        if kind is SensorKind.RADAR and not observer.radar_on:
            continue
        # 电子侦察只发现辐射源
        if kind is SensorKind.ESM and not target.is_emitting():
            continue
        # 声呐只对水下/水面
        if kind is SensorKind.SONAR and target.domain not in (Domain.SUB, Domain.SEA):
            continue
        if kind is SensorKind.SONAR and target.domain is Domain.SEA and d > s.range_max * 0.4:
            continue

        eff, los = sensor_effective_range(state, observer, target, s.range_max, kind,
                                          s.counter_stealth)
        if kind is SensorKind.ESM:
            eff *= 1.35   # 被动侦察的距离优势
        if eff <= 0 or not los or d > eff:
            continue

        ratio = d / eff
        # strength 越高，能在越远距离上达到「跟踪」
        track_ratio = 0.35 + 0.30 * s.strength
        class_ratio = 0.70 + 0.22 * s.strength
        if ratio <= track_ratio:
            lvl = Detection.TRACKED
        elif ratio <= class_ratio:
            lvl = Detection.CLASSIFIED
        else:
            lvl = Detection.CONTACT
        # 近距离目视/热像识别：贴脸距离上不再受工事伪装影响
        if d <= CLOSE_TRACK_RANGE and lvl.level < Detection.TRACKED.level:
            lvl = Detection.TRACKED

        if lvl.level > best.level.level:
            best = DetectionResult(
                level=lvl,
                sensor=f"{kind.label}",
                effective_range=eff,
                distance=d,
                reason=f"{kind.label}探测 {d:.1f}/{eff:.1f} 格",
            )
    return best


def detection_level(state: "GameState", viewer_side, target: Unit) -> Detection:
    """指定阵营对目标单位的探测等级（聚合全部己方传感器与情报来源）。"""
    if target.side is viewer_side:
        return Detection.TRACKED
    if not target.alive:
        return Detection.NONE
    # 轨道目标由空间监视网编目：双方都能掌握其轨道，可直接作为反卫星交战目标
    if target.domain is Domain.SPACE:
        return Detection.TRACKED

    # 主动暴露（开火 / 雷达开机 / 干扰机开机）→ 至少识别
    if target.revealed_until >= state.turn_count:
        base = Detection.CLASSIFIED
    else:
        base = Detection.NONE

    # 天基侦察过顶：星下点附近可达跟踪级，覆盖区其余为识别级
    if target.uid in state.sat_recon.get(viewer_side, ()):
        if base.level < Detection.CLASSIFIED.level:
            base = Detection.CLASSIFIED
    if target.uid in getattr(state, "sat_track", {}):
        base = Detection.TRACKED

    best = base
    for obs in state.units:
        if obs.side is not viewer_side or not obs.alive:
            continue
        # 快速距离剪枝
        if dist_units(obs, target) > obs.best_sensor_range() * 1.6 + 0.5:
            continue
        r = evaluate_detection(state, obs, target)
        if r.level.level > best.level:
            best = r.level
            if best is Detection.TRACKED:
                break
    return best


def own_track(state: "GameState", observer: Unit, target: Unit) -> bool:
    """该单位能否**依靠自身传感器**达到跟踪级（用于判定信火一体是否依赖外部引导）。"""
    if observer is target:
        return True
    if target.domain is Domain.SPACE:
        return True
    return evaluate_detection(state, observer, target).level is Detection.TRACKED


def build_detection_map(state: "GameState") -> dict[int, Detection]:
    """重算全图探测态势，返回 {unit_uid: 该单位对「当前行动方」的探测等级}。

    同时写入双方各自的态势表，供 AI 与 UI 使用。
    """
    result: dict[int, Detection] = {}
    for side in (state.player_side, state.player_side.foe):
        table = {}
        for tgt in state.units:
            if not tgt.alive:
                continue
            table[tgt.uid] = detection_level(state, side, tgt)
        state.detection[side] = table
        if side is state.player_side:
            result = table
    return result


# --------------------------------------------------------------------------- #
# 命中解算
# --------------------------------------------------------------------------- #
@dataclass
class ShotProfile:
    """一次射击的完整概率分解，用于 UI 展示与结算。"""

    ok: bool = False
    reason: str = ""
    pk: float = 0.0
    damage: float = 0.0
    distance: float = 0.0
    in_range: bool = False
    has_los: bool = True
    factors: dict[str, float] = field(default_factory=dict)
    salvo: int = 1
    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)
    cue_source: str = ""
    cue_penalty: float = 1.0
    notes: list[str] = field(default_factory=list)


def _fire_mission_bonus(state: "GameState", attacker: Unit, target: Unit) -> tuple[float, float, str]:
    """火力准备加成：(命中倍率, 毁伤倍率, 说明)。

    优先调用引擎上的方法（引擎持有准备状态），以免 rules 反向依赖 engine。
    """
    fn = getattr(state, "fire_mission_bonus", None)
    if fn is None:
        return 1.0, 1.0, ""
    return fn(attacker, target)


def shot_profile(state: "GameState", attacker: Unit, target: Unit, weapon: Weapon,
                 origin: tuple[int, int] | None = None) -> ShotProfile:
    """计算射击可行性与命中概率（不消耗弹药、不随机）。

    `origin` 用于在飞弹药：射程应自发射点而非载机当前位置起算。
    """
    ax, ay = origin if origin else (attacker.x, attacker.y)
    p = ShotProfile(distance=dist((ax, ay), (target.x, target.y)), salvo=weapon.salvo)
    is_space = target.domain is Domain.SPACE
    if is_space:
        p.distance = ORBITAL_ENGAGEMENT_DISTANCE

    if not attacker.alive or not target.alive:
        p.reason = "目标已不存在"
        return p
    if target.side is attacker.side:
        p.reason = "不能攻击己方单位"
        return p
    if attacker.mission == "ferry":
        p.reason = "转场剖面：武器保险关闭，无法射击"
        return p
    if attacker.ammo.get(weapon.id, 0) <= 0:
        p.reason = "弹药耗尽"
        return p
    if target.domain not in weapon.targets:
        p.reason = f"{weapon.name} 无法打击{target.domain.label}目标"
        return p
    if is_space and not weapon.asat:
        p.reason = "该武器不具备反卫星能力"
        return p
    if p.distance < weapon.range_min:
        p.reason = f"低于最小射程 {weapon.range_min:.1f} 格"
        return p
    if p.distance > weapon.range_max + RANGE_TOLERANCE:
        p.reason = f"超出射程 {weapon.range_max:.1f} 格"
        return p
    p.in_range = True

    # 探测要求：必须达到武器所需情报等级
    lvl = state.known_level(attacker.side, target)
    if lvl.level < weapon.min_detection.level:
        p.reason = f"情报不足（需{weapon.min_detection.label}，当前{lvl.label}）"
        return p

    # 信火一体：目标由友邻/预警平台提供跟踪诸元，本平台自身并未锁定
    if not own_track(state, attacker, target):
        if attacker.template.datalink and attacker.datalink_on:
            # 通信频段被压制 → 数据链中断，协同交战退化为陈旧诸元
            comm_jam = jam_strength_at(state, (attacker.x, attacker.y), attacker.side, "comm")
            if comm_jam > 0.15:
                p.cue_source = f"己方传感器网（通信受扰 {comm_jam * 100:.0f}%）"
                p.cue_penalty = max(0.45, 1.0 - comm_jam * 0.65)
            else:
                p.cue_source = "己方传感器网（协同交战）"
        elif attacker.template.datalink:
            # 主动切断数据链：牺牲协同能力换取抗干扰
            p.cue_source = "数据链已切断（抗干扰）"
            p.cue_penalty = 0.78
        else:
            p.cue_source = "友邻目标指示（无数据链）"
            p.cue_penalty = 0.78

    # 反辐射导弹：仅能攻击辐射源
    if weapon.guidance.value == "anti_radiation" and not target.is_emitting():
        p.reason = "目标未辐射（雷达关机），反辐射导弹无法锁定"
        return p

    # 通视（轨道目标不适用地形遮蔽）
    if not is_space and (weapon.needs_los or weapon.kind in (WeaponKind.GUN, WeaponKind.ATGM)):
        p.has_los = has_los(
            state, (ax, ay), (target.x, target.y),
            observer_air=attacker.domain is Domain.AIR,
            target_air=target.domain is Domain.AIR,
        )
        if not p.has_los:
            p.reason = "无通视条件"
            return p

    f: dict[str, float] = {}

    # 射程包线
    edge = (p.distance - weapon.range_min) / max(0.1, weapon.range_max - weapon.range_min)
    f["射程包线"] = 1.0 if edge < 0.75 else max(0.55, 1.0 - (edge - 0.75) * 1.4)

    # 电子战：干扰目标所在空域 / 己方制导链路
    if weapon.guidance.vulnerable_to_ew:
        jam_t = jam_strength_at(state, (target.x, target.y), attacker.side)
        jam_a = jam_strength_at(state, (ax, ay), attacker.side)
        f["电子对抗"] = max(0.25, 1.0 - (jam_t * 0.55 + jam_a * 0.35))
    else:
        f["电子对抗"] = 1.0

    # 卫星导航（精确制导弹药依赖 GPS/北斗）
    if weapon.guidance.value == "gps_ins":
        f["导航精度"] = gps_integrity(state, attacker.side)

    # 目标机动规避
    f["目标规避"] = max(0.30, 1.0 - target.template.evasion * 0.55)

    # 烟幕：遮蔽光电/半主动制导与直瞄火力，面压制与卫星/惯性制导不受影响
    tgt_smoke = state.map.at(target.x, target.y)
    if tgt_smoke is not None and tgt_smoke.smoke > 0 and not is_space:
        if weapon.guidance.value in ("optical", "ir", "radar_semi") or \
                weapon.kind in (WeaponKind.GUN, WeaponKind.ATGM):
            f["烟幕遮蔽"] = 0.55

    # 制空巡逻剖面：保持雷达与导弹战备，空战命中更高
    if attacker.domain is Domain.AIR and attacker.mission == "cap" and weapon.kind is WeaponKind.AAM:
        f["巡逻战备"] = 1.15

    # 压制效果
    if attacker.suppressed > 0:
        f["己方被压制"] = 0.80

    # 战损对射击的影响
    f["战损影响"] = 0.75 + 0.25 * attacker.hp_ratio

    if p.cue_penalty < 1.0:
        f["外部引导"] = p.cue_penalty

    # 火力准备：射界内的间瞄射击获得显著加成
    prep_pk, prep_dmg, prep_note = _fire_mission_bonus(state, attacker, target)
    if prep_pk != 1.0:
        f["火力准备"] = prep_pk
        p.notes.append(prep_note)

    pk = weapon.pk
    for v in f.values():
        pk *= v
    p.factors = f
    p.pk = max(0.02, min(0.97, pk))

    # 命中后伤害（穿甲 vs 装甲 / 地形掩护 / 工事 / 兵种相克）
    if is_space:
        p.damage = weapon.damage
    else:
        tgt_tile = state.map.at(target.x, target.y)
        cover = tgt_tile.terrain.armor_bonus if tgt_tile and target.domain in (Domain.LAND, Domain.INFRA) else 0.0
        # 构筑工事：步兵可挖壕据守，显著降低被杀伤
        ent = target.entrench * (0.10 + target.template.entrench_bonus)
        # 野战工事（堑壕/掩体/雷场）：归属方在阵地上额外减伤
        fort = tgt_tile.fort_cover_for(target.side) if tgt_tile else 0.0
        mitigation = 1.0 - min(0.90, target.template.armor * 0.85 + cover + ent + fort)
        dmg = weapon.damage * mitigation
        # 火力准备：射界内预先标定的射击毁伤更高
        if prep_dmg != 1.0:
            dmg *= prep_dmg
        # 兵种相克：面杀伤对步兵有效，直瞄火力对散兵效果差
        if target.template.is_infantry:
            if weapon.kind in (WeaponKind.ARTILLERY, WeaponKind.MLRS, WeaponKind.BOMB):
                dmg *= 1.30
            elif weapon.kind in (WeaponKind.GUN, WeaponKind.ATGM, WeaponKind.TORPEDO):
                dmg *= 0.68
        # 城区防御：步兵依托建筑，机械化部队反而受困
        if tgt_tile is not None and tgt_tile.terrain is Terrain.URBAN:
            dmg *= 0.78 if target.template.is_infantry else 1.12
        p.damage = dmg
    p.ok = True

    # 拦截评估（反弹道/反卫星拦截不适用现役防空网）
    if is_space:
        p.intercept_pk, p.interceptors = 0.0, []
    else:
        p.intercept_pk, p.interceptors = interception_estimate(state, attacker, target, weapon)
    return p


def sam_for_intercept(d: Unit, pos: tuple[int, int]) -> Weapon | None:
    """为单位选择用于拦截的防空弹：射程够、有备弹、非中段专用弹。

    区域防空用防空导弹（SAM）；点防御可用主动雷达空空弹（AAM），
    但效果略低（专用发射架与火控都更弱）。
    """
    best = None
    for w in d.template.weapons:
        if w.kind not in (WeaponKind.SAM, WeaponKind.AAM) or w.midcourse or w.ciws:
            continue
        if d.ammo.get(w.id, 0) <= 0:
            continue
        if dist((d.x, d.y), pos) > w.range_max + RANGE_TOLERANCE:
            continue
        if best is None or w.range_max > best.range_max:
            best = w
    return best


# --------------------------------------------------------------------------- #
# 多点防空协同（IADS）
# --------------------------------------------------------------------------- #
@dataclass
class ADNNode:
    """防空网中的一个节点——通常是一套防空系统。

    节点记录自己���分工、可用火控通道与剩余拦截弹，网据此统一分配任务。
    """

    unit: Unit
    weapon: Weapon | None
    pk: float                  # 对该目标的单发拦截概率
    channels: int              # 剩余火控通道
    ammo: int                  # 剩余拦截弹
    role: str                  # 分工：区域防空 / 要地防空 / 点防御 / 末段硬杀伤
    ciws: bool = False

    @property
    def name(self) -> str:
        return self.unit.name


@dataclass
class AirDefenseNetwork:
    """一体化防空网：跨单位统一分配火控通道。

    与「各打各的」的关键区别：
    * 火控通道由**网**统一记账与分配，而不是每个单位各自为战；
    * 网会按单发拦截概率**择优派工**——多发弹药同时来袭时，
      不会让每个单位都对着第一发猛打，而是分给最合适的节点；
    * 每个节点有明确**分工**（区域 / 要地 / 点防御 / 末段），
      交战面板会把分工显示给玩家。
    """

    side: Side
    nodes: list[ADNNode] = field(default_factory=list)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(cls, state: "GameState", side: Side, pos: tuple[int, int],
              weapon: Weapon, self_unit: Unit | None = None) -> "AirDefenseNetwork":
        """针对来袭弹药构建防空网。"""
        net = cls(side=side)
        for d in state.units:
            if not d.alive or d.side is not side or d.is_decoy:
                continue
            # -- 区域防空：SAM / AAM --
            if d.template.interceptors > 0 and d.template.intercept_pk > 0:
                sam = sam_for_intercept(d, pos)
                if sam is not None and d.intercepts_used < d.template.interceptors:
                    rng = sam.range_max
                    if dist((d.x, d.y), pos) <= rng + RANGE_TOLERANCE:
                        jam = jam_strength_at(state, (d.x, d.y), d.side, "radar")
                        pk = d.template.intercept_pk * (1.0 - weapon.evasion)
                        pk *= (1.0 - jam * 0.6) * (1.0 - weapon.stealth * 0.7)
                        if sam.kind is WeaponKind.AAM:
                            pk *= 0.85
                        if weapon.ballistic:
                            pk *= 0.30
                        if weapon.strategic:
                            continue
                        own = self_unit is not None and d is self_unit
                        if own:
                            pk *= 0.6
                        if pk > 0.01:
                            net.nodes.append(ADNNode(
                                unit=d, weapon=sam, pk=min(0.95, pk),
                                channels=d.template.interceptors - d.intercepts_used,
                                ammo=d.ammo.get(sam.id, 0),
                                role=_adn_role(sam, own),
                            ))
            # -- 近防炮：末段硬杀伤 --
            for w in d.template.weapons:
                if not w.ciws or d.ammo.get(w.id, 0) <= 0:
                    continue
                if dist((d.x, d.y), pos) > w.range_max + RANGE_TOLERANCE:
                    continue
                pk = w.pk * (1.0 - weapon.evasion * 0.55) * (1.0 - weapon.stealth * 0.6)
                if weapon.ballistic:
                    pk *= 0.05
                if weapon.hypersonic:
                    pk *= 0.10
                elif weapon.speed >= 9.0:
                    pk *= 0.55
                if pk > 0.01:
                    net.nodes.append(ADNNode(
                        unit=d, weapon=w, pk=min(0.95, pk),
                        channels=99, ammo=d.ammo.get(w.id, 0),
                        role="末段硬杀伤", ciws=True,
                    ))
        # 择优派工：拦截概率高的节点优先
        net.nodes.sort(key=lambda n: (-n.pk, n.channels))
        return net

    # -- 分配 -------------------------------------------------------------
    def assign(self, state: "GameState") -> tuple[ADNNode | None, float]:
        """为来袭弹药分配最合适的节点。

        返回 (节点, 调整后的拦截概率)。没有可用节点时返回 (None, 0)。
        """
        for n in self.nodes:
            if n.ciws:
                if n.ammo > 0:
                    return n, n.pk
                continue
            if n.channels > 0 and n.ammo > 0:
                return n, n.pk
        return None, 0.0

    @property
    def total_channels(self) -> int:
        return sum(n.channels for n in self.nodes if not n.ciws)

    def summary(self) -> list[tuple[str, str, int]]:
        """网内分工摘要：(节点名, 分工, 剩余通道)。"""
        out: list[tuple[str, str, int]] = []
        for n in self.nodes:
            ch = "—" if n.ciws else str(n.channels)
            out.append((n.name, n.role, n.channels))
            _ = ch
        return out

    def role_counts(self) -> dict[str, int]:
        """各分工的节点数量。"""
        out: dict[str, int] = {}
        for n in self.nodes:
            out[n.role] = out.get(n.role, 0) + 1
        return out


def _adn_role(sam: Weapon, own: bool) -> str:
    """按拦截弹与射程判定节点分工。"""
    if own:
        return "自卫拦截"
    if sam.range_max >= 7.0:
        return "区域防空"
    if sam.range_max >= 3.0:
        return "要地防空"
    return "点防御"


def adn_summary_for(state: "GameState", side: Side, pos: tuple[int, int],
                     weapon: Weapon, self_unit: Unit | None = None) -> AirDefenseNetwork:
    """对外接口：构建指定位置处的防空网。"""
    return AirDefenseNetwork.build(state, side, pos, weapon, self_unit)


def resolve_salvo_interception(state: "GameState", pos: tuple[int, int], side: Side,
                               weapon: Weapon, count: int,
                               self_unit: Unit | None = None) -> tuple[int, list[str]]:
    """逐发拦截结算（由防空网统一派工）。

    每发来袭弹药交由防空网分配给**最合适的节点**；节点消耗一条火控通道
    与一枚拦截弹。同一节点在通道耗尽或弹尽后退出网，
    下一发弹药改派其他节点——这就是「跨单位分配」而非「各打各的」。
    """
    if not weapon.interceptable or weapon.strategic:
        return count, []
    net = AirDefenseNetwork.build(state, side, pos, weapon, self_unit=self_unit)
    if not net.nodes:
        return count, []

    leakers = 0
    events: list[str] = []
    for _ in range(max(1, count)):
        killed = False
        # 网内择优派工：可能有多层拦截机会（远层失败后交给近层）
        for node in list(net.nodes):
            if node.ciws:
                if node.ammo <= 0:
                    continue
                node.ammo -= 1
                node.unit.ammo[node.weapon.id] = max(
                    0, node.unit.ammo.get(node.weapon.id, 0) - 1)
            else:
                if node.channels <= 0 or node.ammo <= 0:
                    continue
                node.channels -= 1
                node.ammo -= 1
                node.unit.intercepts_used += 1
                node.unit.ammo[node.weapon.id] = max(
                    0, node.unit.ammo.get(node.weapon.id, 0) - 1)
            if roll(node.pk):
                events.append(f"{node.name}·{node.role} 拦截 1 发（Pk {node.pk * 100:.0f}%）")
                killed = True
                break
        if not killed:
            leakers += 1
    return leakers, events


def midcourse_interceptors(state: "GameState", pos: tuple[int, int], side: Side):
    """可在中段拦截弹道导弹的防御方（SM-3 / THAAD / 77N6）。

    返回 [(单位, 拦截弹, 单发拦截概率)]；中段拦截很难，基础概率按武器 Pk 的 55% 计。
    """
    out = []
    for d in state.units:
        if not d.alive or d.side is not side or d.is_decoy:
            continue
        for w in d.template.weapons:
            if not w.midcourse or d.ammo.get(w.id, 0) <= 0:
                continue
            if dist((d.x, d.y), pos) > w.range_max + RANGE_TOLERANCE:
                continue
            pk = min(0.85, w.pk * 0.55)
            out.append((d, w, pk))
            break
    return out


def interception_estimate(state: "GameState", attacker: Unit, target: Unit,
                          weapon: Weapon) -> tuple[float, list[str]]:
    """评估防守方对来袭弹药的拦截概率（由防空网统一派工）。"""
    if weapon.strategic:
        # 洲际弹道导弹：只能在飞行中段被专用反导系统拦截（预览按弹道中点估算）
        mid = ((attacker.x + target.x) // 2, (attacker.y + target.y) // 2)
        survive = 1.0
        names = []
        for d, w, pk in midcourse_interceptors(state, mid, target.side):
            survive *= (1.0 - pk)
            names.append(f"{d.name}·中段 {w.name[:8]} ({pk * 100:.0f}%)")
        return 1.0 - survive, names
    if not weapon.interceptable:
        return 0.0, []

    net = AirDefenseNetwork.build(state, target.side, (target.x, target.y), weapon,
                                  self_unit=target)
    survive = 1.0
    names: list[str] = []
    for n in net.nodes[:5]:
        survive *= (1.0 - n.pk)
        ch = "·" + str(n.channels) if not n.ciws else ""
        names.append(f"{n.name}·{n.role} ({n.pk * 100:.0f}%){ch}")
    return 1.0 - survive, names


# --------------------------------------------------------------------------- #
# 对手视角威胁评估
# --------------------------------------------------------------------------- #
@dataclass
class CounterThreat:
    """「对手视角」评估：这一发子弹为什么打不到 / 打到了会怎样。

    玩家在交战面板中需要回答三类问题：
    1. 子弹会不会被拦下（拦截层次与综合拦截概率）；
    2. 目标是否有反击能力（对方能不能打到我们）；
    3. 这一发到底受什么条件限制（情报、通视、弹种、诱饵）。
    """

    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)
    reply_units: list[str] = field(default_factory=list)   # 可反击的敌方单位
    reply_weapons: list[str] = field(default_factory=list)  # 其可用武器
    reply_pk: float = 0.0                                  # 单发最危险的反击概率
    enemy_fire_control: bool = False                       # 对方是否已跟踪到我们
    enemy_detected_level: Detection = Detection.NONE
    blockers: list[str] = field(default_factory=list)      # 阻止命中的具体条件
    notes: list[str] = field(default_factory=list)         # 补充提示
    adn_roles: list[tuple[str, str, int]] = field(default_factory=list)  # 防空网分工
    adn_channels: int = 0                                   # 网内剩余火控通道总数

    @property
    def blocked(self) -> bool:
        """这一发是否存在被完全拦下的风险。"""
        return self.intercept_pk > 0.01


def counter_threat_profile(state: "GameState", attacker: Unit, target: Unit,
                           weapon: Weapon) -> CounterThreat:
    """从防守方视角评估这次射击，解释「为什么打不到」。

    与 `shot_profile` 互补：后者回答「我能不能打中」，本函数回答
    「对方能做什么」。所有判断均为纯计算，不消耗任何资源。
    """
    out = CounterThreat()
    if attacker is None or target is None or weapon is None:
        return out

    # -- 1) 拦截层次：这一发会被谁拦下 --
    if weapon.interceptable and not weapon.strategic:
        pk, names = interception_estimate(state, attacker, target, weapon)
        out.intercept_pk = pk
        out.interceptors = names
        # 防空网分工：让玩家看清对方防区如何组合
        net = AirDefenseNetwork.build(state, target.side, (target.x, target.y), weapon,
                                      self_unit=target)
        out.adn_roles = [(n.name, n.role, n.channels) for n in net.nodes[:5]]
        out.adn_channels = net.total_channels
    elif weapon.strategic:
        out.notes.append("洲际弹道：战术防空网无能为力，仅中段反导可拦")

    # -- 2) 目标能否反击：对方有哪些单位能打到我们 --
    dist_to_me = dist_units(target, attacker)
    best_reply = 0.0
    for e in state.enemies_of(attacker.side):
        if e is target or not e.alive:
            continue
        usable = [w for w in e.usable_weapons()
                  if attacker.domain in w.targets
                  and dist((e.x, e.y), (attacker.x, attacker.y)) <= w.range_max + RANGE_TOLERANCE]
        if not usable:
            continue
        out.reply_units.append(e.name)
        for w in usable[:2]:
            out.reply_weapons.append(f"{e.name}·{w.name}")
        best_reply = max(best_reply, max(w.pk for w in usable))
    out.reply_pk = best_reply

    # 目标自身的反击能力（自卫火力）
    self_reply = [w for w in target.usable_weapons()
                  if attacker.domain in w.targets
                  and dist_to_me <= w.range_max + RANGE_TOLERANCE]
    if self_reply:
        out.reply_units.append(f"{target.name}（自卫）")
        for w in self_reply[:2]:
            out.reply_weapons.append(f"{target.name}·{w.name}")
        out.reply_pk = max(out.reply_pk, max(w.pk for w in self_reply))

    # -- 3) 对方是否已经掌握我们的火控数据（能否反击取决于是否跟踪到） --
    lvl = detection_level(state, target.side, attacker)
    out.enemy_detected_level = lvl
    out.enemy_fire_control = lvl is Detection.TRACKED

    # -- 4) 阻止命中的具体条件 --
    if target.is_decoy:
        out.blockers.append("目标疑为电子诱饵：打上去只会浪费弹药")
    if weapon.guidance.value == "anti_radiation" and not target.is_emitting():
        out.blockers.append("目标未辐射，反辐射导弹无法锁定")
    if not weapon.interceptable:
        out.blockers.append("该弹种不可拦截：但也意味着精度依赖发射时诸元")
    if weapon.min_detection is Detection.CLASSIFIED and \
            state.known_level(attacker.side, target) is Detection.CLASSIFIED:
        out.blockers.append("仅识别级情报即可开火：命中精度已打折")
    if weapon.speed >= 9.0 and out.intercept_pk > 0.2:
        out.blockers.append("高速弹的拦截窗口很短，但仍有被截杀的可能")
    if out.enemy_fire_control:
        out.blockers.append("对方已跟踪到我方：开火会立刻暴露并招致反击")
    elif out.reply_units:
        out.blockers.append("对方有反击能力，但尚未跟踪到我方")
    return out


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
        if not has_los(state, (attacker.x, attacker.y), (tile.x, tile.y),
                       observer_air=attacker.domain is Domain.AIR):
            p.reason = "无通视条件，无法对设施实施精确打击"
            return p

    f: dict[str, float] = {}
    edge = (p.distance - weapon.range_min) / max(0.1, weapon.range_max - weapon.range_min)
    f["射程包线"] = 1.0 if edge < 0.75 else max(0.55, 1.0 - (edge - 0.75) * 1.4)
    if weapon.strategic:
        f["电子对抗"] = 1.0        # 惯性/天文制导，不受战场干扰影响
    elif weapon.guidance.vulnerable_to_ew:
        jam_t = jam_strength_at(state, (tile.x, tile.y), attacker.side)
        jam_a = jam_strength_at(state, (attacker.x, attacker.y), attacker.side)
        f["电子对抗"] = max(0.25, 1.0 - (jam_t * 0.55 + jam_a * 0.35))
    else:
        f["电子对抗"] = 1.0
    if weapon.guidance.value == "gps_ins":
        f["导航精度"] = gps_integrity(state, attacker.side)
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


# --------------------------------------------------------------------------- #
# 机动
# --------------------------------------------------------------------------- #
AIRBASE_CAPACITY = 4


def can_end_at(state: "GameState", unit: Unit, pos: tuple[int, int]) -> bool:
    """是否可以停留在该地块。

    常规编制不可同格堆叠；但**航空兵可停驻在同一机场**（跑道/停机坪容量 4 架），
    否则一个基地只能容纳一架飞机，僚机会因无法着陆而耗尽燃油。
    """
    occ = [u for u in state.units if u.alive and (u.x, u.y) == pos and u is not unit]
    if not occ:
        return True
    if unit.domain is Domain.AIR:
        tile = state.map.at(pos[0], pos[1])
        if tile is not None and tile.facility is Facility.AIRBASE and tile.owner is unit.side \
                and tile.operational:
            same = [u for u in occ if u.side is unit.side and u.domain is Domain.AIR]
            return len(same) < AIRBASE_CAPACITY
        host = [u for u in occ if u.side is unit.side and u.template.unit_class is UnitClass.CARRIER]
        if host:
            return True
    return False


def tile_move_cost(state: "GameState", unit: Unit, x: int, y: int,
                   from_pos: tuple[int, int], block_friendly: bool = True) -> float | None:
    """进入 (x,y) 的机动点消耗；不可进入返回 None。

    `block_friendly=False` 用于寻路：允许借道友军占据的格子，但不得停留。
    """
    md = state.map
    t = md.at(x, y)
    if t is None:
        return None
    occupied = state.unit_at(x, y)
    if occupied is not None and occupied.side is not unit.side:
        return None  # 敌方占据格需通过战斗解决
    if occupied is not None and occupied is not unit and block_friendly:
        return None  # 己方同格不可堆叠

    dom = unit.domain
    if dom is Domain.AIR:
        return 1.0
    if dom in (Domain.SEA, Domain.SUB):
        if not t.passable_sea:
            return None
        base = 1.0 if t.terrain is Terrain.WATER else 1.6
        return base
    # 地面 / 设施
    if t.terrain is Terrain.WATER:
        if unit.template.amphibious:
            return 2.4          # 两栖投送：涉水航渡代价高
        return None
    base = t.terrain.move_cost
    if base >= 90:
        return None
    # 敌方野战工事：雷场、反坦克壕与障碍物拖慢推进
    if t.fort > 0 and t.fort_owner is not Side.NEUTRAL and t.fort_owner is not unit.side:
        base += 0.55 * t.fort
    # 海拔爬升惩罚
    prev = md.at(*from_pos)
    if prev and t.elevation - prev.elevation > 400:
        base += 1.0
    return base


def step_cost(dx: int, dy: int) -> float:
    return 1.41421356 if (dx != 0 and dy != 0) else 1.0


def find_path(state: "GameState", unit: Unit, goal: tuple[int, int],
              max_cost: float | None = None) -> list[tuple[int, int]] | None:
    """Dijkstra 最短机动路径（8 邻域），返回含起点的坐标序列。"""
    import heapq

    start = (unit.x, unit.y)
    if start == goal:
        return [start]
    budget = unit.mp if max_cost is None else max_cost
    if goal not in state.map.tiles or not can_end_at(state, unit, goal):
        return None

    best: dict[tuple[int, int], float] = {start: 0.0}
    prev: dict[tuple[int, int], tuple[int, int]] = {}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, start)]

    while pq:
        cost, cur = heapq.heappop(pq)
        if cur == goal:
            path = [cur]
            while cur in prev:
                cur = prev[cur]
                path.append(cur)
            path.reverse()
            return path
        if cost > best.get(cur, 1e9):
            continue
        cx, cy = cur
        for dx, dy in NEIGHBORS_8:
            nx, ny = cx + dx, cy + dy
            if not state.map.in_bounds(nx, ny):
                continue
            step = tile_move_cost(state, unit, nx, ny, cur, block_friendly=False)
            if step is None:
                continue
            ncost = cost + step * step_cost(dx, dy)
            if ncost > budget + 1e-6:
                continue
            if ncost < best.get((nx, ny), 1e9):
                best[(nx, ny)] = ncost
                prev[(nx, ny)] = cur
                heapq.heappush(pq, (ncost, (nx, ny)))
    return None


def reachable_tiles(state: "GameState", unit: Unit) -> dict[tuple[int, int], float]:
    """单位当前可到达的全部地块及其消耗（单次 Dijkstra 洪泛，供 AI 与 UI 使用）。"""
    return flood_reachable(state, unit)


def flood_reachable(state: "GameState", unit: Unit,
                    budget: float | None = None) -> dict[tuple[int, int], float]:
    """以单位所在地为源做受限 Dijkstra，返回 {坐标: 累计消耗}。"""
    import heapq

    start = (unit.x, unit.y)
    limit = unit.mp if budget is None else budget
    best: dict[tuple[int, int], float] = {start: 0.0}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
    while pq:
        cost, (cx, cy) = heapq.heappop(pq)
        if cost > best.get((cx, cy), 1e9):
            continue
        for dx, dy in NEIGHBORS_8:
            nx, ny = cx + dx, cy + dy
            if not state.map.in_bounds(nx, ny):
                continue
            step = tile_move_cost(state, unit, nx, ny, (cx, cy), block_friendly=False)
            if step is None:
                continue
            ncost = cost + step * step_cost(dx, dy)
            if ncost > limit + 1e-6:
                continue
            if ncost < best.get((nx, ny), 1e9):
                best[(nx, ny)] = ncost
                heapq.heappush(pq, (ncost, (nx, ny)))
    # 可借道通过，但不可停留在友军占据格
    return {p: c for p, c in best.items() if p == start or can_end_at(state, unit, p)}


def path_cost(state: "GameState", unit: Unit, path: list[tuple[int, int]]) -> float:
    total = 0.0
    for i in range(1, len(path)):
        (px, py), (x, y) = path[i - 1], path[i]
        step = tile_move_cost(state, unit, x, y, (px, py), block_friendly=False)
        if step is None:
            return 1e9
        total += step * step_cost(x - px, y - py)
    return total


def field_to_goal(state: "GameState", unit: Unit, goal: tuple[int, int],
                  limit: float = 120.0) -> dict[tuple[int, int], float]:
    """以目标为源的 Dijkstra 距离场（按该单位的机动代价）。

    用作 AI 的路径梯度：单位每回合沿距离场下降方向前进，避免贪心陷入局部最优。
    """
    import heapq

    if goal not in state.map.tiles:
        return {}
    field: dict[tuple[int, int], float] = {goal: 0.0}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, goal)]
    while pq:
        cost, (cx, cy) = heapq.heappop(pq)
        if cost > field.get((cx, cy), 1e9) or cost > limit:
            continue
        for dx, dy in NEIGHBORS_8:
            nx, ny = cx + dx, cy + dy
            if not state.map.in_bounds(nx, ny):
                continue
            step = tile_move_cost(state, unit, nx, ny, (cx, cy), block_friendly=False)
            if step is None:
                continue
            ncost = cost + step * step_cost(dx, dy)
            if ncost > limit:
                continue
            if ncost < field.get((nx, ny), 1e9):
                field[(nx, ny)] = ncost
                heapq.heappush(pq, (ncost, (nx, ny)))
    return field


# --------------------------------------------------------------------------- #
# 骰子
# --------------------------------------------------------------------------- #
def roll(p: float) -> bool:
    return random.random() < p


def jitter(amount: float, spread: float = 0.15) -> float:
    return amount * random.uniform(1.0 - spread, 1.0 + spread)


__all__ = [
    "dist", "dist_units", "RANGE_TOLERANCE", "CLOSE_TRACK_RANGE", "has_los",
    "DECOY_SIGNATURE", "DECOY_LABELS", "decoy_signature",
    "sam_for_intercept", "resolve_salvo_interception", "midcourse_interceptors",
    "jam_strength_at", "gps_integrity",
    "ADNNode", "AirDefenseNetwork", "adn_summary_for",
    "DetectionResult", "evaluate_detection", "detection_level", "build_detection_map",
    "ShotProfile", "shot_profile", "interception_estimate",
    "CounterThreat", "counter_threat_profile",
    "FacilityStrike", "facility_strike_profile", "service_tile", "facility_service_ok",
    "FACILITY_SIZE", "ORBITAL_ENGAGEMENT_DISTANCE", "AIRBASE_CAPACITY",
    "tile_move_cost", "find_path", "reachable_tiles", "flood_reachable", "path_cost",
    "field_to_goal", "roll", "jitter",
    "sensor_effective_range", "NEIGHBORS_8",
]

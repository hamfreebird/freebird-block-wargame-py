"""射击解算：命中率、毁伤、拦截评估与威胁评估。

拦截相关的常量与 ``interception_estimate`` 在 :mod:`core.adn` 里——
本模块只在射击剖面里调用它，不反向提供任何东西。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import counter as counter_rules
from .adn import AirDefenseNetwork, interception_estimate
# 这几个「环境依赖」走模块属性访问（``ew.jam_strength_at`` 而不是
# ``from .ew import jam_strength_at``）：自检脚本需要把它们临时换成
# 确定性桩值，而打补丁只能改**定义处模块**的属性。函数内直接引用
# 的名字是另一份绑定，补丁打不到。
from . import detect, ew, geo
from .detect import detection_level
from .enums import Detection, Domain, Side, WeaponKind
from .env import environment_for
from .geo import (ORBITAL_ENGAGEMENT_DISTANCE, RANGE_TOLERANCE,
                 crossfire_bonus, dist, dist_units)
from .model import Tile, Unit, Weapon

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

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
    #: 对**毁伤**的总修正（相克 × 交叉火力；与 factors 分开：factors 乘的是命中率）
    damage_factors: dict[str, float] = field(default_factory=dict)
    damage_mult: float = 1.0
    salvo: int = 1
    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)
    cue_source: str = ""
    cue_penalty: float = 1.0
    notes: list[str] = field(default_factory=list)


def salvo_limit(unit: Unit, weapon: Weapon) -> int:
    """可选的齐射发数上限。

    补给紧张时只能单发点射——多发齐射要开箱、要装填、要驳运，
    断粮的部队打不出这个节奏。这是「断补给就真的打不出该有火力」的第一层。
    """
    if unit.supply_state >= 1:
        return 1
    return max(1, weapon.max_salvo)


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
    p = ShotProfile(distance=dist((ax, ay), (target.x, target.y)),
                    salvo=min(weapon.salvo, salvo_limit(attacker, weapon)))
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
    if not detect.own_track(state, attacker, target):
        if attacker.template.datalink and attacker.datalink_on:
            # 通信频段被压制 → 数据链中断，协同交战退化为陈旧诸元
            comm_jam = ew.jam_strength_at(state, (attacker.x, attacker.y), attacker.side, "comm")
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
        p.has_los = geo.has_los(
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
        jam_t = ew.jam_strength_at(state, (target.x, target.y), attacker.side)
        jam_a = ew.jam_strength_at(state, (ax, ay), attacker.side)
        f["电子对抗"] = max(0.25, 1.0 - (jam_t * 0.55 + jam_a * 0.35))
    else:
        f["电子对抗"] = 1.0

    # 卫星导航（精确制导弹药依赖 GPS/北斗）
    if weapon.guidance.value == "gps_ins":
        f["导航精度"] = ew.gps_integrity(state, attacker.side)

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

    # 补给枯竭：缺备件意味着观瞄失校、发射机构带病工作，散布变大
    if attacker.supply_state >= 2:
        f["补给枯竭"] = 0.88

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
        # 兵种相克：目标特性 × 武器特性的连续函数（core/counter.py）。
        # 城区不再单独乘一个固定系数——它作为目标的「掩蔽 / 疏散空间」
        # 进入了目标画像，与武器通道一起决定结果。
        counter, reasons = counter_rules.counter_multiplier(weapon, target, tgt_tile)
        mult = 1.0
        dfac: dict[str, float] = {}
        if abs(counter - 1.0) >= 0.005:
            mult *= counter
            dfac.update(reasons)
        # 交叉火力：多方位同时压制，工事与掩体只挡得住一个方向
        xfire, _sectors = crossfire_bonus(state, attacker, target, weapon)
        if xfire > 1.001:
            mult *= xfire
            dfac["交叉火力"] = xfire
        p.damage_mult = mult
        if dfac:
            dmg *= mult
            p.damage_factors = dfac
        p.damage = dmg
    p.ok = True

    # 拦截评估（反弹道/反卫星拦截不适用现役防空网）
    if is_space:
        p.intercept_pk, p.interceptors = 0.0, []
    else:
        p.intercept_pk, p.interceptors = interception_estimate(state, attacker, target, weapon)
    return p



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

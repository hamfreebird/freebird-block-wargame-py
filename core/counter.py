"""兵种相克：目标特性 × 武器特性。

旧实现是三条 if（面杀伤打步兵 ×1.30、直瞄打步兵 ×0.68、城区 ×0.78 / ×1.12）。
数值本身不算离谱，问题在于**没法解释**：一辆步战车与一辆主战坦克对同一发
破甲弹的差别、一架无人机与一架轰炸机对同一枚红外弹的差别，都被抹平了。

这里改成两个画像的点积：

    武器画像：破甲强度 / 爆破强度 / 制导精度 / 导引头频段 / 偏好高度层
    目标画像：装甲硬度 / 受弹面积 / 规避机动 / 电磁特征 / 热信号 / 高度层

乘数 = 本目标得分 ÷ 「标准目标」得分。对标准目标的毁伤恒为 1.0，
只有**相对差异**在变——加机制不会顺带把整局平衡推翻。

两个通道的物理含义：

* **破甲**（ap）：打得穿才算数，打穿之后超出部分迅速饱和——过穿不增加杀伤。
  对无装甲目标（散兵）反而低效，因为弹丸带着大部分动能穿过去了。
* **爆破**（he）：覆盖到才有用，目标越软、受弹面积越大越有效。

所有函数都是连续的，没有阈值跳变；`reasons` 供交战面板回答
「为什么这一枪不疼」。
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import Altitude, Domain, Guidance, Terrain, WeaponKind
from .model import Tile, Unit, Weapon

__all__ = [
    "TargetProfile", "WeaponProfile", "target_profile", "weapon_profile",
    "counter_multiplier", "armor_sensitivity", "blast_sensitivity",
    "CHANNEL_LABELS",
]


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


# --------------------------------------------------------------------------- #
# 武器画像
# --------------------------------------------------------------------------- #
#: 各类武器的「破甲 / 爆破」通道占比（和为 1）。这不是现实威力，是**起作用的方式**：
#: 同样是 100 点伤害，坦克炮靠动能穿孔，火箭炮靠破片覆盖，鱼雷靠水下冲击。
_CHANNELS: dict[WeaponKind, tuple[float, float]] = {
    WeaponKind.GUN:          (0.80, 0.20),
    WeaponKind.ATGM:         (0.88, 0.12),   # 聚能破甲
    WeaponKind.ARM:          (0.72, 0.28),   # 打雷达天线，战斗部很小
    WeaponKind.TORPEDO:      (0.35, 0.65),   # 水下冲击波撕船体
    WeaponKind.DEPTH_CHARGE: (0.20, 0.80),
    WeaponKind.ARTILLERY:    (0.15, 0.85),
    WeaponKind.MLRS:         (0.12, 0.88),
    WeaponKind.BOMB:         (0.22, 0.78),
    WeaponKind.LAND_ATTACK:  (0.45, 0.55),   # 巡航/弹道：半穿甲战斗部
    WeaponKind.ASM:          (0.55, 0.45),
    WeaponKind.AAM:          (0.15, 0.85),   # 纯破片战斗部，不指望打穿什么
    WeaponKind.SAM:          (0.40, 0.60),
    WeaponKind.CIWS:         (0.30, 0.70),
}

#: 制导方式 → (精度 0..1, 导引头频段)
_GUIDANCE: dict[Guidance, tuple[float, str]] = {
    Guidance.LASER:          (0.95, "optical"),
    Guidance.OPTICAL:        (0.82, "optical"),
    Guidance.WIRE:           (0.90, "wire"),
    Guidance.GPS_INS:        (0.86, "gps"),
    Guidance.RADAR_SEMI:     (0.78, "radar"),
    Guidance.RADAR_ACTIVE:   (0.72, "radar"),
    Guidance.ANTI_RADIATION: (0.74, "rf_passive"),
    Guidance.PASSIVE_RF:     (0.70, "rf_passive"),
    Guidance.IR:             (0.66, "ir"),
    Guidance.SONAR:          (0.80, "sonar"),
}

#: 各类武器偏好的交战高度层（0 地面 / 1 太空）。用于「用错工具」的轻微折减：
#: 高炮打高空目标、区域防空打贴地目标，都不在最佳包线上。
_ALT_PREF: dict[WeaponKind, float] = {
    WeaponKind.SAM: 0.68,
    WeaponKind.AAM: 0.65,
}


@dataclass(frozen=True)
class WeaponProfile:
    """武器「以什么方式杀伤」的画像。"""

    kind: WeaponKind
    armor_pierce: float      # 破甲通道强度 0..1
    blast: float             # 爆破通道强度 0..1
    precision: float         # 制导精度，用于抵消目标机动
    band: str                # 导引头频段
    altitude: float          # 偏好高度层


def weapon_profile(w: Weapon) -> WeaponProfile:
    ap, he = _CHANNELS.get(w.kind, (0.40, 0.60))
    if w.kind is WeaponKind.GUN:
        # 同为「直瞄火炮」，25 mm 机炮与 120 mm 坦克炮不是一回事：
        # 前者靠射速与榴弹杀伤人员，后者靠动能穿孔。用弹重（damage）
        # 把通道在两极之间连续插值，小口径就自然获得「反人员」倾向，
        # 不必给数据文件新增字段、也不必维护一张口径表。
        small = 1.0 - min(1.0, w.damage / 60.0)
        ap -= 0.55 * small
        he += 0.55 * small
    return WeaponProfile(
        kind=w.kind,
        armor_pierce=_clamp(ap + (0.12 if w.kind is WeaponKind.ATGM else 0.0), 0.0, 1.0),
        blast=_clamp(he, 0.0, 1.0),
        precision=_GUIDANCE.get(w.guidance, (0.70, "none"))[0],
        band=_GUIDANCE.get(w.guidance, (0.70, "none"))[1],
        altitude=_ALT_PREF.get(w.kind, 0.0),
    )


# --------------------------------------------------------------------------- #
# 目标画像
# --------------------------------------------------------------------------- #
_ALT_LEVEL: dict[Altitude, float] = {
    Altitude.GROUND: 0.0,
    Altitude.LOW: 0.30,
    Altitude.MED: 0.60,
    Altitude.HIGH: 0.90,
}

#: 域 → (受弹面积, 热信号)。数值是「一类目标的典型值」，
#: 再由具体模板的 stealth / armor 做个体修正。
_DOMAIN_SHAPE: dict[Domain, tuple[float, float]] = {
    Domain.LAND:  (0.80, 0.85),
    Domain.AIR:   (0.35, 0.90),
    Domain.SEA:   (0.95, 0.75),
    Domain.SUB:   (0.30, 0.45),
    Domain.INFRA: (1.00, 0.50),
    Domain.SPACE: (0.50, 0.25),
}


@dataclass(frozen=True)
class TargetProfile:
    """目标「挨打时是什么样」的画像。"""

    armor: float        # 装甲硬度 0..1
    exposure: float     # 受弹面积 / 队形密度 0..1
    mobility: float     # 规避机动 0..1
    signature: float    # 电磁/雷达特征 0..1（1 = 毫无隐身）
    thermal: float      # 红外特征 0..1
    altitude: float     # 所在高度层 0..1


def target_profile(u: Unit, tile: Tile | None = None) -> TargetProfile:
    t = u.template
    d = t.domain
    exposure, thermal = _DOMAIN_SHAPE.get(d, (0.70, 0.70))

    if t.is_infantry:
        # 散兵线：受弹面积小，但几乎没有防护——这正好是「面杀伤有效、
        # 直瞄低效」这一常识的来源，不需要单独写一条 if。
        exposure, thermal = 0.30, 0.45
        armor = t.armor * 0.5
    elif d is Domain.AIR:
        # 空中平台的 armor 在数据里表达的是「结构冗余 / 要害分散」，
        # 不是装甲板：破片对蒙皮、旋翼、进气道的杀伤效率远高于同厚度的钢装甲。
        armor = t.armor * 0.30
    else:
        armor = t.armor

    # 编队规模：营/旅是「一片东西」，面杀伤能覆盖到更多
    if t.echelon in ("battalion", "brigade"):
        exposure = min(1.0, exposure + 0.12)
    elif t.echelon in ("squadron", "flotilla"):
        exposure = min(1.0, exposure + 0.06)

    # 单兵/无人平台的热信号远低于有动力的车辆
    if d is Domain.AIR and t.unit_class.value in ("uav", "satellite"):
        thermal *= 0.40
    if d is Domain.SUB:
        thermal *= 0.80        # 潜航时艇体温度低

    signature = _clamp(1.0 - t.stealth, 0.05, 1.0)
    thermal = _clamp(thermal * (1.0 - 0.30 * t.stealth), 0.05, 1.0)

    if tile is not None:
        conceal = 1.0 - tile.terrain.cover        # 0 无遮蔽 → 0.4 城区
        if t.is_infantry:
            # 建筑、林线给步兵提供掩蔽，面杀伤的覆盖效率下降
            exposure *= 1.0 - 0.55 * conceal
        else:
            # 车辆/舰艇不会因为旁边有房子就变小；城区反而是灾难——
            # 街道受限、无法疏散，顶部还暴露在高层火力下
            exposure *= 1.0 - 0.15 * conceal
            if tile.terrain is Terrain.URBAN:
                exposure = min(1.0, exposure + 0.12)

    return TargetProfile(
        armor=_clamp(armor, 0.0, 0.95),
        exposure=_clamp(exposure, 0.05, 1.0),
        mobility=_clamp(t.evasion, 0.0, 1.0),
        signature=signature,
        thermal=thermal,
        altitude=1.0 if d is Domain.SPACE else _ALT_LEVEL.get(t.max_altitude, 0.0),
    )


# --------------------------------------------------------------------------- #
# 两个通道的敏感度
# --------------------------------------------------------------------------- #
def armor_sensitivity(hardness: float) -> float:
    """目标对「破甲」的敏感度：越硬越吃穿甲弹。

    没有底数：空中平台、无人机本来就没有装甲，让这一项归零，
    否则它们会因为「破甲通道派不上用场」被无端扣分。
    """
    return 2.60 * hardness


def blast_sensitivity(hardness: float) -> float:
    """目标对「破片/冲击」的敏感度：越软越吃面杀伤。

    1.30 是裸目标的基准（无防护人员几乎必被破片覆盖到），
    装甲每厚 0.1 就削掉 0.22——主战坦克（0.42）只剩 0.38，
    与「一发 155 打不掉坦克」的常识一致。
    """
    return _clamp(1.30 - 2.20 * hardness, 0.28, 1.20)


#: 标准目标：一名有基本防护的步兵。伤害数值本来就该以「对人」为基准——
#: 装甲目标吃穿甲弹更疼、吃破片弹更轻，这个分化才是相克要表达的东西。
_REF = TargetProfile(armor=0.08, exposure=0.60, mobility=0.18,
                     signature=0.85, thermal=0.60, altitude=0.0)


def _channel_score(wp: WeaponProfile, tp: TargetProfile) -> float:
    ap = wp.armor_pierce * armor_sensitivity(tp.armor)
    # 受弹面积只做 4% 的微调：真正决定面杀伤效果的是「软不软」，
    # 散兵线摊得开是次要因素（否则会得出「火炮打步兵不如打车」的怪结论）。
    he = wp.blast * blast_sensitivity(tp.armor) * (0.96 + 0.04 * tp.exposure)
    return ap + he


def _mobility_term(wp: WeaponProfile, tp: TargetProfile) -> float:
    """目标机动 vs 武器响应：无制导的直瞄武器打高机动目标最吃亏。"""
    response = 0.50 if wp.band in ("none", "wire") else wp.precision
    return 1.0 - 0.35 * max(0.0, tp.mobility - response)


def _altitude_term(wp: WeaponProfile, tp: TargetProfile) -> float:
    """用错高度层的轻微折减：高炮打高空、区域防空打贴地，都不在最佳包线。"""
    return _clamp(1.0 - 0.22 * abs(wp.altitude - tp.altitude), 0.80, 1.10)


def _band_term(wp: WeaponProfile, tp: TargetProfile) -> float:
    """导引头频段与目标特征的匹配。

    幅度刻意压得小：探测层已经用 stealth 惩罚过一次，
    这里再重罚就成了「隐身目标双重减免」。
    """
    if wp.band == "ir":
        # 红外弹打低热目标（无人机、潜艇）锁定距离与跟踪品质都下降
        return _clamp(0.80 + 0.26 * tp.thermal, 0.80, 1.05)
    if wp.band in ("radar", "rf_passive"):
        return 1.0 - 0.20 * (1.0 - tp.signature)
    if wp.band == "optical":
        # 光学/激光对小目标的分辨与稳定跟踪更吃力
        return 1.0 - 0.10 * (1.0 - tp.exposure)
    return 1.0


#: 展示用的通道名（界面里需要回答「为什么这一枪不疼」）
CHANNEL_LABELS = {
    "破甲": "破甲",
    "爆破": "爆破",
    "机动": "目标机动",
    "高度": "高度层",
    "频段": "导引头频段",
}


def counter_multiplier(weapon: Weapon, target: Unit, tile: Tile | None = None
                       ) -> tuple[float, list[tuple[str, float]]]:
    """相克乘数（作用于毁伤，不作用于命中率）。

    返回 ``(mult, reasons)``；``reasons`` 只列出真正起作用的项，
    交战面板据此说明「这一发为什么打得不疼」。
    """
    wp = weapon_profile(weapon)
    tp = target_profile(target, tile)

    ratio = _channel_score(wp, tp) / max(1e-6, _channel_score(wp, _REF))
    # 开方软化：单发武器之间的相对差异不该有一倍以上的跨度
    mult = _clamp(ratio ** 0.75, 0.60, 1.40)

    reasons: list[tuple[str, float]] = []
    if abs(mult - 1.0) >= 0.02:
        reasons.append(("破甲" if wp.armor_pierce >= wp.blast else "爆破", mult))

    mob = _mobility_term(wp, tp)
    if abs(mob - 1.0) >= 0.02:
        mult *= mob
        reasons.append(("机动", mob))

    alt = _altitude_term(wp, tp)
    if abs(alt - 1.0) >= 0.02:
        mult *= alt
        reasons.append(("高度", alt))

    band = _band_term(wp, tp)
    if abs(band - 1.0) >= 0.02:
        mult *= band
        reasons.append(("频段", band))

    return _clamp(mult, 0.55, 1.45), reasons

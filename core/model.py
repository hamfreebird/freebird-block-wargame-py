"""核心数据模型：武器、传感器、单位模板、单位实例、地块、飞行中的弹药。

所有模型均为纯数据对象，不含渲染逻辑；规则计算集中在 core/rules.py。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .enums import (
    Altitude, Detection, Domain, Facility, Guidance, ObjectiveKind,
    SensorKind, Side, Terrain, UnitClass, WeaponKind,
)

_uid_counter = itertools.count(1)


def ensure_uid_above(n: int) -> None:
    """读档后把 uid 计数器推进到已用最大值之上，避免新单位与旧单位撞号。"""
    global _uid_counter
    if n > 0:
        _uid_counter = itertools.count(n + 1)


# --------------------------------------------------------------------------- #
# 武器
# --------------------------------------------------------------------------- #
@dataclass
class Weapon:
    """一种武器系统。

    射程单位为「格」（1 格 = tile_km 公里）。`real_range_km` 仅作现实参照展示，
    用于说明该武器在真实世界的公开射程数据；游戏内平衡以 `range_max` 为准。
    """

    id: str
    name: str
    kind: WeaponKind
    range_min: float
    range_max: float
    pk: float                     # 基础单发命中/杀伤概率
    damage: float                 # 命中后的基础伤害
    ammo: int                     # 备弹基数
    guidance: Guidance
    targets: tuple[Domain, ...]   # 可打击的作战域
    real_range_km: str = ""       # 现实射程（展示用）
    speed: float = 1.0            # 每回合飞行格数（决定飞行时间）
    interceptable: bool = True    # 是否可被防空系统拦截
    evasion: float = 0.0          # 突防能力（降低被拦截概率）
    hypersonic: bool = False
    needs_los: bool = False       # 是否需要视线
    min_detection: Detection = Detection.TRACKED
    note: str = ""
    salvo: int = 1                # 默认齐射发数
    max_salvo: int = 1            # 单次射击可选的最大发数（玩家可在 1..max_salvo 间选择）
    vs_facility: bool = False     # 能否对机场/港口等固定设施实施火力打击
    asat: bool = False            # 反卫星武器：可攻击天基目标（按轨道交战规则结算）
    stealth: float = 0.0          # 低可探测性 0..1：压缩拦截窗口、降低被拦截概率
    ciws: bool = False            # 近防武器：可对来袭弹药实施末段硬杀伤
    anti_fort: int = 0            # 对野战工事的破坏力（每命中一发降低的等级）
    strategic: bool = False       # 战略武器：按预装订坐标打击，不依赖实时情报
    ballistic: bool = False       # 弹道飞行：中段可被专用反导系统拦截，末段不可拦截
    midcourse: bool = False       # 中段反导拦截弹（SM-3 / THAAD / 77N6）
    smoke: bool = False           # 烟幕弹：不造成毁伤，用于遮蔽

    @property
    def is_indirect(self) -> bool:
        return self.kind in (WeaponKind.ARTILLERY, WeaponKind.MLRS, WeaponKind.LAND_ATTACK)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "kind": self.kind.value,
            "range_min": self.range_min, "range_max": self.range_max,
            "pk": self.pk, "damage": self.damage, "ammo": self.ammo,
            "guidance": self.guidance.value,
            "targets": [t.value for t in self.targets],
        }


# --------------------------------------------------------------------------- #
# 传感器
# --------------------------------------------------------------------------- #
@dataclass
class Sensor:
    kind: SensorKind
    range_max: float          # 探测距离（格）
    strength: float = 1.0     # 探测品质：影响达到「跟踪」的距离比例
    counter_stealth: float = 0.0  # 反隐身能力：抵消目标隐身系数的比例（米波/超视距雷达）
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "range_max": self.range_max,
                "strength": self.strength, "counter_stealth": self.counter_stealth}


# --------------------------------------------------------------------------- #
# 单位模板
# --------------------------------------------------------------------------- #
@dataclass
class UnitTemplate:
    id: str
    name: str
    short: str                       # 棋子上的短标识（2-4 字符）
    unit_class: UnitClass
    domain: Domain
    hp: float
    mp: float
    armor: float = 0.0               # 0..1，伤害减免
    stealth: float = 0.0             # 0..1，雷达隐身（1 = 完全隐身）
    evasion: float = 0.0             # 0..1，机动规避（降低被命中率）
    sensors: list[Sensor] = field(default_factory=list)
    weapons: list[Weapon] = field(default_factory=list)
    jam_radius: float = 0.0          # 电子干扰半径（格）
    jam_strength: float = 0.0        # 干扰强度 0..1
    jam_bands: tuple[str, ...] = ()  # 可干扰频段：radar / comm / nav
    jam_band_weights: dict[str, float] = field(default_factory=dict)
    interceptors: int = 0            # 每回合可拦截的来袭弹药数
    intercept_pk: float = 0.0        # 单次拦截成功率
    can_capture: bool = False        # 能否占领目标点
    amphibious: bool = False         # 两栖投送：可越过海域实施登陆
    is_radar_emitter: bool = False   # 默认雷达开机
    max_altitude: Altitude = Altitude.GROUND
    notes: str = ""
    cost: int = 0
    sortie_shots: int = 1            # 每回合可发射次数（战机可多枚齐射）
    datalink: bool = True            # 是否具备协同交战/信火一体数据链
    decoy_charges: int = 0           # 可释放的诱饵/假目标数量
    entrench_bonus: float = 0.0      # 构筑工事后的额外防护（步兵专用）
    is_infantry: bool = False        # 步兵类目标（受面杀伤影响大、直瞄火力效果差）
    one_shot_kill: bool = False      # 被命中即毁（空中平台）
    can_fortify: bool = False        # 可构筑 / 修复野战工事（工兵、步兵）
    fort_power: int = 1              # 单次构筑提升的工事等级
    echelon: str = "platoon"         # 部队层级：platoon 连 / company 营 / brigade 旅
    command_radius: float = 0.0      # 指挥半径（格）：可下达指令的距离
    command_range: float = 0.0       # 火力协调半径：集中火力时可协调的距离

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "short": self.short,
            "unit_class": self.unit_class.value, "domain": self.domain.value,
            "hp": self.hp, "mp": self.mp, "armor": self.armor,
            "stealth": self.stealth, "evasion": self.evasion,
            "sensors": [s.to_dict() for s in self.sensors],
            "weapons": [w.to_dict() for w in self.weapons],
            "jam_radius": self.jam_radius, "jam_strength": self.jam_strength,
            "interceptors": self.interceptors, "intercept_pk": self.intercept_pk,
        }


# --------------------------------------------------------------------------- #
# 地块
# --------------------------------------------------------------------------- #
@dataclass
class Tile:
    x: int
    y: int
    terrain: Terrain
    elevation: int = 0               # 海拔（米）
    owner: Side = Side.NEUTRAL
    facility: Facility = Facility.NONE
    smoke: int = 0                   # 烟幕遮蔽剩余回合
    damage: int = 0                  # 设施损毁度 0..100（旧字段，保留兼容）
    objective: bool = False          # 是否为争夺目标点
    objective_name: str = ""
    victory_points: int = 0
    facility_hp: float = 100.0       # 设施结构完整度
    facility_max_hp: float = 100.0
    fort: int = 0                    # 野战工事等级 0-3（堑壕/掩体/雷场）
    fort_owner: Side = Side.NEUTRAL  # 工事归属方
    fort_progress: int = 0           # 修复进度（每 2 点恢复 1 级）

    @property
    def is_water(self) -> bool:
        return self.terrain in (Terrain.WATER,)

    @property
    def passable_land(self) -> bool:
        return self.terrain not in (Terrain.WATER,)

    @property
    def passable_sea(self) -> bool:
        return self.terrain in (Terrain.WATER, Terrain.COAST)

    @property
    def facility_hp_ratio(self) -> float:
        return 0.0 if self.facility_max_hp <= 0 else max(0.0, self.facility_hp / self.facility_max_hp)

    @property
    def operational(self) -> bool:
        """设施是否仍可运作（跑道/码头未被打瘫）。"""
        return self.facility is not Facility.NONE and self.facility_hp_ratio > 0.25

    @property
    def is_base(self) -> bool:
        return self.facility in (Facility.AIRBASE, Facility.PORT)

    @property
    def has_fort(self) -> bool:
        return self.fort > 0 and self.fort_owner is not Side.NEUTRAL

    def fort_cover_for(self, side: Side) -> float:
        """工事为指定阵营提供的额外减伤。"""
        if self.fort <= 0 or self.fort_owner is not side:
            return 0.0
        return 0.05 * self.fort

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x, "y": self.y, "terrain": self.terrain.value,
            "elevation": self.elevation, "owner": self.owner.value,
            "facility": self.facility.value, "objective": self.objective,
            "facility_hp": round(self.facility_hp, 1),
        }


# --------------------------------------------------------------------------- #
# 单位实例
# --------------------------------------------------------------------------- #
@dataclass
class Unit:
    template: UnitTemplate
    side: Side
    x: int
    y: int

    uid: int = field(default_factory=lambda: next(_uid_counter))
    hp: float = 0.0
    mp: float = 0.0
    ammo: dict[str, int] = field(default_factory=dict)
    altitude: Altitude = Altitude.GROUND
    radar_on: bool = True
    jam_on: bool = False
    datalink_on: bool = True      # 数据链开关：通信受扰时可主动切断
    fired: bool = False
    moved: bool = False
    destroyed: bool = False
    suppressed: int = 0              # 压制回合数：降低命中率
    fuel: float = 1.0                # 0..1，空中平台续航
    name: str = ""
    revealed_until: int = 0          # 主动暴露（开火/雷达）持续到该回合
    last_hit_by: str = ""
    home_base: tuple[int, int] | None = None   # 航空兵的母基地（机场/航母）
    on_station: int = 0              # 已连续出动回合数
    shots_left: int = 1              # 本回合剩余发射次数
    entrench: int = 0                # 工事等级 0-2（步兵驻守后获得）
    decoy_left: int = 0              # 剩余诱饵数量
    is_decoy: bool = False           # 是否为电子诱饵/假目标
    decoy_kind: str = ""             # radar / ir / visual / multi
    decoy_ttl: int = 0               # 诱饵剩余存在回合
    intercepts_used: int = 0         # 本回合已消耗的拦截通道数
    mission: str = "strike"          # 航空兵任务剖面：strike / cap / ferry
    fire_mission: FireMission | None = None   # 火力准备：已登记的射界
    order: Order | None = None                 # 上级下达的当前指令
    # 派生值缓存：传感器最大射程（模板在一局内不变，算一次即可）
    _sensor_range_cache: float | None = field(default=None, repr=False)
    # 探测世代号：单位状态一变就递增。探测缓存的键包含它，
    # 状态一变旧结论自动作废，不必逐一维护失效清单。
    detect_gen: int = field(default=0, repr=False)

    @property
    def has_fire_mission(self) -> bool:
        return self.fire_mission is not None and self.fire_mission.active

    @property
    def can_prepare_fire(self) -> bool:
        """是否具备预先射击能力：间瞄火力（炮兵/火箭炮）。"""
        return any(w.is_indirect for w in self.template.weapons)

    def __post_init__(self) -> None:
        if self.hp <= 0:
            self.hp = self.template.hp
        if self.mp <= 0:
            self.mp = self.template.mp
        if not self.ammo and not self.is_decoy:
            self.ammo = {w.id: w.ammo for w in self.template.weapons}
        if not self.name:
            self.name = self.template.name
        self.radar_on = self.template.is_radar_emitter
        if not self.is_decoy:
            self.shots_left = max(1, self.template.sortie_shots)
            self.decoy_left = max(0, self.template.decoy_charges)

    # -- 便捷属性 ---------------------------------------------------------
    @property
    def uid_str(self) -> str:
        return f"{self.side.value[0]}{self.uid:03d}"

    @property
    def domain(self) -> Domain:
        return self.template.domain

    @property
    def name_label(self) -> str:
        return f"{self.name}"

    @property
    def hp_ratio(self) -> float:
        return max(0.0, self.hp / self.template.hp)

    @property
    def alive(self) -> bool:
        return not self.destroyed and self.hp > 0

    @property
    def weapons(self) -> list[Weapon]:
        return self.template.weapons

    def weapon_by_id(self, wid: str) -> Weapon | None:
        for w in self.template.weapons:
            if w.id == wid:
                return w
        return None

    def usable_weapons(self) -> list[Weapon]:
        return [w for w in self.template.weapons if self.ammo.get(w.id, 0) > 0]

    def has_ammo(self) -> bool:
        return any(self.ammo.get(w.id, 0) > 0 for w in self.template.weapons)

    def best_sensor_range(self) -> float:
        """最大传感器探测距离（格）。

        结果缓存在实例上：探测解算会对每个「观测者×目标」组合反复
        询问这个值，而传感器列表在一局内是固定的。
        """
        cached = self._sensor_range_cache
        if cached is None:
            cached = max((s.range_max for s in self.template.sensors), default=0.0)
            self._sensor_range_cache = cached
        return cached

    def is_emitting(self) -> bool:
        # 雷达诱饵模拟辐射源，可吸引反辐射导弹
        if self.is_decoy and self.decoy_kind == "radar":
            return True
        radar = any(s.kind is SensorKind.RADAR for s in self.template.sensors)
        jam = self.template.jam_strength > 0
        return (radar and self.radar_on) or (jam and self.jam_on)

    @property
    def fuel_factor(self) -> float:
        """任务剖面对续航的影响：巡逻耗油、转场省油。"""
        return {"cap": 1.30, "ferry": 0.50}.get(self.mission, 1.0)

    @property
    def mp_factor(self) -> float:
        return 1.25 if self.mission == "ferry" else 1.0

    def max_intercept_range(self) -> float:
        r = 0.0
        for w in self.template.weapons:
            if w.kind in (WeaponKind.SAM, WeaponKind.AAM):
                r = max(r, w.range_max)
        return r

    def reset_turn(self, turn: int) -> None:
        self.mp = self.template.mp * self.mp_factor
        self.fired = False
        self.moved = False
        self.shots_left = max(1, self.template.sortie_shots) if not self.is_decoy else 0
        self.intercepts_used = 0
        if self.suppressed > 0:
            self.suppressed -= 1
        # 航空兵停在地面才补油；空中平台补给见 engine._tick_logistics
        if self.domain is not Domain.AIR:
            self.fuel = min(1.0, self.fuel + 0.25)

    @property
    def can_shoot(self) -> bool:
        return self.alive and self.shots_left > 0 and not self.is_decoy

    def consume_shot(self) -> None:
        self.shots_left = max(0, self.shots_left - 1)
        self.fired = self.shots_left <= 0

    def apply_damage(self, amount: float) -> float:
        self.hp = max(0.0, self.hp - amount)
        if self.hp <= 0:
            self.destroyed = True
        return amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid, "template": self.template.id, "side": self.side.value,
            "x": self.x, "y": self.y, "hp": round(self.hp, 1),
            "ammo": dict(self.ammo), "radar_on": self.radar_on,
            "destroyed": self.destroyed,
        }


# --------------------------------------------------------------------------- #
# 飞行中的弹药
# --------------------------------------------------------------------------- #
@dataclass
class Missile:
    """已在空中、尚未完成交战的制导弹药。

    远程打击存在飞行时间，这为防守方留出拦截窗口，也让「发射后目标机动」成为
    真实变量——打出去不等于打中。
    """

    weapon: Weapon
    attacker_uid: int
    attacker_side: Side
    target_uid: int
    origin: tuple[int, int]
    target_pos: tuple[int, int]
    eta: int                          # 剩余飞行回合
    salvo: int = 1
    intercepted: bool = False
    midcourse_done: bool = False     # 是否已完成中段反导窗口
    label: str = ""

    @property
    def is_hypersonic(self) -> bool:
        return self.weapon.hypersonic


# --------------------------------------------------------------------------- #
# 指挥指令
# --------------------------------------------------------------------------- #
@dataclass
class Order:
    """上级单位向下属下达的作战指令。

    指令是**有期限**的（通常 2 回合），过期后下属恢复自主决策——
    这样指挥链既有价值，又不会把 AI 变成只会执行命令的傀儡。
    """

    kind: str = "focus_fire"     # focus_fire / priority / hold / assault
    issuer_uid: int = 0          # 下达指令的上级单位 uid
    target_uid: int = 0          # 集中火力/强攻的目标 uid
    priority_class: str = ""     # 优先目标类别：sam / radar / hq / air
    turns_left: int = 2          # 剩余有效回合
    x: int = 0                   # 强攻方向的目标格
    y: int = 0

    @property
    def active(self) -> bool:
        return self.turns_left > 0


# --------------------------------------------------------------------------- #
# 火力准备
# --------------------------------------------------------------------------- #
@dataclass
class FireMission:
    """预先射击任务：炮兵登记的射界与目标格。

    间瞄火力的真实打法不是"看见就打"，而是先侦察、登记射界、
    等目标进入预设火力诸元覆盖区再开火。本作把它抽象为：

    * 登记时消耗机动点，并要求对该区域有情报；
    * 登记后进入「已准备」状态，持续 N 回合；
    * 对射界内目标射击时获得显著命中率与毁伤加成；
    * 若目标不在射界内，准备状态失效（射界需要重新侦察）。
    """

    x: int = 0
    y: int = 0
    turns_left: int = 0             # 剩余准备回合数
    prepared: bool = False          # 是否已完成准备（登记当回合为 False）
    total: int = 2                  # 准备所需回合数
    intel_level: str = "contact"    # 登记时的情报等级

    @property
    def active(self) -> bool:
        return self.turns_left > 0

    @property
    def ready(self) -> bool:
        """已准备完毕——可以对射界内目标打出准备加成。"""
        return self.active and self.prepared


# --------------------------------------------------------------------------- #
# 目标点
# --------------------------------------------------------------------------- #
@dataclass
class Objective:
    x: int
    y: int
    name: str
    kind: ObjectiveKind
    side: Side                 # 归属方
    value: int = 0             # 每回合胜利点
    holder: Side = Side.NEUTRAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x, "y": self.y, "name": self.name,
            "kind": self.kind.value, "side": self.side.value, "value": self.value,
        }


__all__ = ["Weapon", "Sensor", "UnitTemplate", "Tile", "Unit", "Missile", "Objective",
           "FireMission", "Order", "ensure_uid_above"]

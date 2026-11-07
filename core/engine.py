"""游戏状态机与回合流程。

一回合 = 1 小时作战时段，流程为：
    蓝方行动 → 结算在飞弹药 → 红方(AI)行动 → 结算在飞弹药
    → 回合推进（探测刷新 / 电子战重算 / 卫星过顶 / 目标点计分 / 胜负判定）

设计要点：
* 所有交战都经过「情报 → 射程 → 通视 → 电子对抗 → 拦截 → 命中 → 毁伤」链路；
* 远程弹药有飞行时间，发射不等于命中；
* 每次状态改变后立即重算探测，导致「开火即暴露、雷达开机即被反辐射锁定」。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass

from .enums import Altitude, Detection, Domain, Facility, Side, Terrain, WeaponKind
from .events import ScenarioEvent, apply_events, parse_events
from .hooks import Rule, evaluate_rules, parse_rules
from .model import (FireMission, Missile, Order, Tile, Unit, Weapon,
                    ensure_uid_above)
from . import mods as _mods
from .rng import GameRng
from .anim import AnimTrack
from . import dice, rules
from .env import WIND_MAX, EnvironmentTrack, environment_for


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class LogEntry:
    turn: int
    side: Side | None
    text: str
    kind: str = "info"      # info / combat / detect / ew / move / system /
                            # victory / tutorial（教学提示不参与回放比对）


@dataclass
class Fx:
    """瞬时视觉效果，由渲染层消费。"""

    kind: str               # explosion / tracer / missile / intercept / ping / move
    a: tuple[float, float]
    b: tuple[float, float] | None
    ttl: float
    color: tuple[int, int, int]
    text: str = ""
    life: float = 1.0       # 初始时长，用于计算动画进度


@dataclass
class FireResult:
    ok: bool = False
    message: str = ""
    in_flight: bool = False
    hits: int = 0
    damage: float = 0.0
    destroyed: bool = False
    intercepted: bool = False
    flight: int = 0


# --------------------------------------------------------------------------- #
# 游戏状态
# --------------------------------------------------------------------------- #
class GameState:
    def __init__(self, scenario, player_side: Side = Side.BLUE,
                 seed: int | None = None):
        self.scenario = scenario
        # 地图**必须复制**：对局会往地块上写目标点归属、烟幕、工事与设施耐久。
        # 沿用想定里的那一份，第二局就会继承第一局的战场——同一想定同一种子
        # 重跑的结果不同，回放与观战的可复现性都会断。
        self.map = scenario.map.clone()
        # 对局随机流：同一个种子 + 同一串命令，重跑结果逐项一致
        # （回放的前提，见 core/rng.py）
        self.rng = GameRng(seed)
        self.seed = self.rng.seed
        # 模组注册表：规则钩子的来源（默认取全局）
        self.mods = _mods.default_registry()
        # 深拷贝：同一想定可反复开局，互不污染
        self.units: list[Unit] = [copy.deepcopy(u) for u in scenario.units]
        # 单位 uid 重新编号为 1..N（按想定顺序）。原先 uid 来自模块级
        # 全局计数器，同一进程内第二次开局就会从 43 起步——那会让
        # 「同一种子重跑得到同一局」失效（单位引用整体错位）。
        for _i, _u in enumerate(self.units, start=1):
            _u.uid = _i
        ensure_uid_above(len(self.units))
        # 增援单位的对局内 uid 分配器。原先 uid 来自模块级全局计数器，
        # 同一进程里开两局时增援会互相撞号——那会让「同种子重跑得到
        # 同一局」失效（单位集合不同）。
        self._uid_next = len(self.units) + 1
        self.player_side = player_side
        self.ai_side = player_side.foe
        self.turn_count = 1
        self.turn_limit = scenario.turn_limit
        self.phase = "player"          # player / ai / over
        # 观战模式（v3.2）：界面不再按某一方的探测结果裁剪视野。
        # **只影响显示**——AI 与规则仍然按各自阵营的探测结果判定，
        # 否则「观战看到的信息」会反过来改变 AI 的决策。
        self.spectator = False
        self.over = False
        self.winner: Side | None = None
        self.win_reason = ""

        self.missiles: list[Missile] = []
        self.log: list[LogEntry] = []
        self.fx: list[Fx] = []
        # 逐帧战斗动画（core 不依赖 pygame，具体绘制在 ui/mapview.py）
        self.anim = AnimTrack()
        # 音效信号队列（由 UI 层消费）
        self.sfx_events: list[str] = []
        # 位置索引（格坐标 → 单位列表），惰性重建
        self._pos_index: dict[tuple[int, int], list[Unit]] | None = None
        # 地块版本号：目标点 / 工事 / 设施发生变化时递增。
        # 渲染层据此判断自己的地块索引是否过期——这样 UI 不必每帧全图扫描，
        # 而大地图（256×256 = 65536 格）的渲染开销与地图大小基本脱钩。
        self.map_epoch = 0
        self.detection: dict[Side, dict[int, Detection]] = {Side.BLUE: {}, Side.RED: {}}
        # 探测增量缓存：{(阵营, 观测者 uid, 目标 uid): 探测等级}。
        # 玩家每一步操作都会触发一次探测刷新，全量重算在大型想定里
        # 是可感知的卡顿。缓存 pair 结果并**精确失效**，让刷新只重算
        # 真正受影响的那几对。
        self._detect_cache: dict[tuple, Detection] = {}
        self._detect_gen = 0            # 全局世代号：环境/干扰等全局变化时递增
        self._detect_fp: dict[int, tuple] = {}   # 单位状态指纹 → 世代号
        self._detect_global_fp: tuple = ()       # 全局指纹
        # 寻路距离场缓存（纯地形，随 map_epoch 失效）——见 rules.field_to_goal
        self._field_cache: dict[tuple, dict] = {}
        self.sat_recon: dict[Side, set[int]] = {Side.BLUE: set(), Side.RED: set()}
        self.sat_track: dict[int, Detection] = {}
        self.vp: dict[Side, int] = {Side.BLUE: 0, Side.RED: 0}
        self.gps: dict[Side, float] = {Side.BLUE: 1.0, Side.RED: 1.0}
        self.casualties: dict[Side, int] = {Side.BLUE: 0, Side.RED: 0}
        # 战斗统计（用于战后回顾）：按阵营累计
        self.stats: dict[Side, dict[str, int]] = {
            Side.BLUE: {"shots": 0, "hits": 0, "intercepted": 0, "kills": 0, "damage": 0},
            Side.RED: {"shots": 0, "hits": 0, "intercepted": 0, "kills": 0, "damage": 0},
        }
        # 胜负点曲线：每回合记录双方的累计 VP
        self.vp_history: list[tuple[int, int, int]] = []   # (回合, 蓝VP, 红VP)

        self._ai_queue: list = []
        self._ai_planner = None
        # AI 的作战决心：{阵营: {uid: 目标点}} / {阵营: {uid: 定下决心的回合}}。
        # **跨回合保留**——规划器每回合重建，若决心也跟着清空，AI 就成了
        # 「每回合重新拍一次脑袋」，谈不上优先级、也谈不上转移。
        # 存在对局状态上而不是模块级：两局并行会互相污染（同种子重跑失效）。
        self.ai_assign: dict[Side, dict[int, tuple[int, int] | None]] = {
            Side.BLUE: {}, Side.RED: {}}
        self.ai_assign_turn: dict[Side, dict[int, int]] = {
            Side.BLUE: {}, Side.RED: {}}
        self.selected_uid: int | None = None
        self.selected_weapon: str | None = None
        self.hover_tile: tuple[int, int] | None = None
        self.salvo_setting: int = 1        # 玩家选择的齐射发数
        self.scenario_path: str = ""       # 存档回放所需的想定路径
        self.armory_path: str = ""
        self.supply_links: dict[int, rules.SupplySource] = {}   # 单位 → 补给源
        # 想定脚本事件（按回合触发）
        self.events: list[ScenarioEvent] = []
        # v2.2：规则钩子（条件触发 + 组合效果）
        self.rules: list[Rule] = []
        self.rule_flags: dict[str, float] = {}
        try:
            self.events = parse_events(scenario.raw.get("events"))
        except Exception as exc:
            self.events = []
            self.log_message(None, f"想定事件解析失败：{exc}", "system")
        try:
            self.rules = parse_rules(scenario.raw.get("rules"))
        except Exception as exc:
            self.rules = []
            self.log_message(None, f"想定规则解析失败：{exc}", "system")

        self._init_gps()
        self.env_track = EnvironmentTrack.from_scenario(scenario)
        self._recompute_supply()
        self.refresh_detection()
        env = environment_for(self)
        self.log_message(None, f"作战开始：{self.map.name}｜{self.map.width}×{self.map.height} 格"
                               f"（1 格 = {self.map.tile_km:.0f} km）｜限 {self.turn_limit} 回合", "system")
        self.log_message(None, f"战场环境：{env.label} {env.clock}｜{env.wind_text()}"
                               f"｜{env.describe_effects()}", "system")
        # 开局即结算第 1 回合的事件（放在开场日志之后，顺序更自然）
        self._run_events()

    # -- 基础查询 ---------------------------------------------------------
    @property
    def objective_tiles(self) -> list[Tile]:
        return [t for t in self.map.iter_tiles() if t.objective]

    @property
    def meta(self):
        """想定元数据（作者、难度、标签、推荐兵力）。"""
        return self.scenario.meta

    @property
    def raw(self) -> dict:
        """想定原始 JSON（供校验器、想定包等外部工具读取）。"""
        return self.scenario.raw

    def alloc_uid(self) -> int:
        """分配一个对局内唯一的单位 uid。"""
        uid = getattr(self, "_uid_next", 0) or 1
        self._uid_next = uid + 1
        return uid

    def unit_at(self, x: int, y: int) -> Unit | None:
        """该格的主要单位。

        同格可能同时有地面单位与空中单位（空中支援地面）。
        这里**优先返回地面单位**——交战目标、占点判定等以地面部队为主体；
        要取全部单位用 `units_at`。
        """
        ground = None
        for u in self.units_at(x, y):
            if u.domain is not Domain.AIR:
                return u
            if ground is None:
                ground = u
        return ground

    def _build_pos_index(self) -> dict[tuple[int, int], list[Unit]]:
        """构建「格坐标 → 单位列表」索引。

        大地图（128×128）+ 上百单位时，`units_at` 的全表扫描会成为
        寻路的性能瓶颈——寻路会调用它上百万次。索引把查询变成 O(1)。
        """
        idx: dict[tuple[int, int], list[Unit]] = {}
        for u in self.units:
            if u.alive:
                idx.setdefault((u.x, u.y), []).append(u)
        return idx

    def invalidate_pos_index(self) -> None:
        """单位位置变化后调用——下次查询时重建索引。

        **约定**：任何改动 `unit.x` / `unit.y` 的地方都要调用它。
        引擎内部的三处（单位机动、卫星漂移、增援部署）已覆盖；
        外部代码（编辑器、测试）直接改坐标后也必须调用。
        """
        self._pos_index = None

    def units_at(self, x: int, y: int) -> list[Unit]:
        """该格的全部单位（地面 + 空中）。

        走位置索引而不是全表扫描——寻路会调用它上百万次。
        索引在单位位置变化时失效并惰性重建。
        """
        idx = self._pos_index
        if idx is None:
            idx = self._pos_index = self._build_pos_index()
        lst = idx.get((x, y))
        if not lst:
            return []
        return [u for u in lst if u.alive]

    def air_units_at(self, x: int, y: int) -> list[Unit]:
        """该格的空中单位。"""
        return [u for u in self.units_at(x, y) if u.domain is Domain.AIR]

    def unit_by_uid(self, uid: int) -> Unit | None:
        for u in self.units:
            if u.uid == uid:
                return u
        return None

    def units_of(self, side: Side, alive_only: bool = True,
                 include_decoys: bool = False) -> list[Unit]:
        return [u for u in self.units
                if u.side is side and (u.alive or not alive_only)
                and (include_decoys or not u.is_decoy)]

    def enemies_of(self, side: Side) -> list[Unit]:
        """作战对象：包含敌方诱饵——正因为会把它当成真目标，欺骗才成立。"""
        return self.units_of(side.foe, include_decoys=True)

    def known_level(self, side: Side, target: Unit) -> Detection:
        return self.detection.get(side, {}).get(target.uid, Detection.NONE)

    def view_level(self, target: Unit) -> Detection:
        """界面显示用的探测等级。

        观战模式下全图可见——「看两个人下棋」不该被其中一方的视野限制。
        注意这**只是显示**：`known_level()` 仍是规则与 AI 的唯一依据。
        """
        if self.spectator or target.side is self.player_side:
            return Detection.TRACKED
        return self.known_level(self.player_side, target)

    def is_visible_to_player(self, u: Unit) -> bool:
        if u.side is self.player_side:
            return True
        return self.known_level(self.player_side, u) is not Detection.NONE

    # -- 日志与特效 -------------------------------------------------------
    def log_message(self, side: Side | None, text: str, kind: str = "info") -> None:
        self.log.append(LogEntry(self.turn_count, side, text, kind))
        if len(self.log) > 600:
            del self.log[:200]

    def add_fx(self, kind: str, a, b=None, ttl: float = 0.9,
               color: tuple[int, int, int] = (255, 210, 120), text: str = "") -> None:
        self.fx.append(Fx(kind, a, b, ttl, color, text, life=ttl))
        if len(self.fx) > 200:
            del self.fx[:100]

    def emit_sfx(self, name: str) -> None:
        """发出一个音效信号（由 UI 层消费；core 不依赖 pygame）。

        与动画同理：音效只影响观感，不参与规则结算。
        """
        self.sfx_events.append(name)
        if len(self.sfx_events) > 40:
            del self.sfx_events[:20]

    # -- 探测与态势 -------------------------------------------------------
    def refresh_detection(self) -> None:
        """重算探测态势。

        顺带检测「新发现」——某个敌方目标从不可见变为可见时发出探测提示音，
        这样玩家能第一时间注意到战场变化。

        这里会**重建位置索引**：每回合一次 O(N)，代价可忽略，
        但能兜住外部代码直接改 `unit.x/y` 造成的索引失效。
        """
        self.invalidate_pos_index()
        before = dict(self.detection.get(self.player_side, {}))
        rules.build_detection_map(self)
        after = self.detection.get(self.player_side, {})
        for uid, lvl in after.items():
            old = before.get(uid)
            if old is not None and old is lvl:
                continue
            if old is not None and old.level == 0 and lvl.level > 0:
                self.emit_sfx("detect")
                break          # 一次刷新只响一次，避免多个目标时噪声叠加

    def touch_unit(self, unit: Unit) -> None:
        """标记单位状态已变化（移动、开关雷达、压制等）。

        探测结论依赖单位状态，因此状态一变就必须让相关缓存失效。
        用**世代号**而不是逐一清理缓存条目：世代号进了缓存键，
        状态一变键就变，旧结论自然作废——不必维护「哪些条目要删」的
        清单，也就不会漏删。
        """
        unit.detect_gen += 1
        self._detect_gen += 1

    def _init_gps(self) -> None:
        for side in (Side.BLUE, Side.RED):
            self.gps[side] = self._compute_gps(side)

    def _compute_gps(self, side: Side) -> float:
        """卫星导航完好度：受导航频段干扰与反卫星作战影响。

        导航干扰是区域性压制：只要敌方的导航干扰机开机，
        我方的卫星导航信号在该战区即被压制——因此这里按阵营整体计损，
        而不是按距离衰减（北斗信号被压制时不会只影响一部分区域）。
        """
        loss = 0.0
        for u in self.enemies_of(side):
            if u.template.jam_strength <= 0 or not u.jam_on:
                continue
            if "nav" not in u.template.jam_bands:
                continue
            loss += u.template.jam_strength * 0.28
        # 天基导航资产损失：仅靠惯导时精度显著下降
        if not self.has_satellite(side):
            loss += 0.30
        return max(0.30, min(1.0, 1.0 - loss))

    # -- 天基资产 ---------------------------------------------------------
    def satellites(self, side: Side) -> list[Unit]:
        return [u for u in self.units_of(side) if u.domain is Domain.SPACE]

    def has_satellite(self, side: Side) -> bool:
        return any(u.alive for u in self.satellites(side))

    def asat_capable(self, side: Side) -> list[Unit]:
        """具备反卫星能力的己方单位。"""
        out = []
        for u in self.units_of(side):
            if not u.has_ammo():
                continue
            for w in u.usable_weapons():
                if w.asat and Domain.SPACE in w.targets:
                    out.append(u)
                    break
        return out

    # -- 选择 -------------------------------------------------------------
    def selected_unit(self) -> Unit | None:
        if self.selected_uid is None:
            return None
        u = self.unit_by_uid(self.selected_uid)
        return u if u and u.alive else None

    def select(self, unit: Unit | None) -> None:
        self.selected_uid = unit.uid if unit else None
        if unit:
            ws = unit.usable_weapons()
            self.selected_weapon = ws[0].id if ws else None
        else:
            self.selected_weapon = None

    def cycle_weapon(self, step: int = 1) -> None:
        u = self.selected_unit()
        if not u:
            return
        ws = u.usable_weapons()
        if not ws:
            self.selected_weapon = None
            return
        ids = [w.id for w in ws]
        try:
            i = ids.index(self.selected_weapon)
        except (ValueError, TypeError):
            i = 0
        self.selected_weapon = ids[(i + step) % len(ids)]

    # -- 机动 -------------------------------------------------------------
    def can_move(self, unit: Unit) -> bool:
        return unit.alive and unit.mp > 1e-6 and unit.side is self.player_side and self.phase == "player"

    def move_unit(self, unit: Unit, path: list[tuple[int, int]]) -> bool:
        """沿路径移动单位；失败返回 False。"""
        if len(path) < 2:
            return False
        if not unit.alive or not rules.can_end_at(self, unit, path[-1]):
            return False
        cost = rules.path_cost(self, unit, path)
        if cost > unit.mp + 1e-6:
            return False

        # 穿越敌方工事：雷场与障碍造成额外伤亡
        mine_dmg, mines = 0.0, 0
        if unit.domain in (Domain.LAND, Domain.INFRA):
            for (px, py) in path[1:]:
                t = self.map.at(px, py)
                if t is None or t.fort <= 0 or t.fort_owner is Side.NEUTRAL \
                        or t.fort_owner is unit.side:
                    continue
                mine_dmg += rules.jitter(t.fort * 4.0, rng=self.rng)
                mines += 1

        start = (unit.x, unit.y)
        unit.x, unit.y = path[-1]
        self.invalidate_pos_index()
        unit.mp -= cost
        # 机动烧油：按实际走过的格数算，远距离迂回比直线突进更费油。
        # 油量从此是库存而不是时间条——跑得越远越接近空箱。
        if unit.domain in (Domain.LAND, Domain.SEA, Domain.SUB):
            burn = len(path[1:]) * self.FUEL_PER_TILE.get(unit.domain, 0.006)
            unit.fuel = max(0.0, unit.fuel - burn)
        unit.moved = True
        unit.entrench = 0
        self.add_fx("move", start, (unit.x, unit.y), ttl=0.6, color=(120, 210, 255))
        # 逐帧动画：机动轨迹 + 落点尘土
        self.anim.add("move", start, (unit.x, unit.y), ttl=0.5,
                      color=(120, 210, 255))
        self.anim.add("burst", (unit.x, unit.y), None, ttl=0.4,
                      color=(170, 160, 140), size=0.35)
        self.emit_sfx("move")
        if mines:
            unit.apply_damage(mine_dmg)
            unit.suppressed = max(unit.suppressed, 1)
            self.add_fx("explosion", (unit.x, unit.y), None, ttl=0.8, color=(255, 190, 90),
                        text=f"-{mine_dmg:.0f}")
            self.log_message(unit.side,
                             f"{unit.name} 穿越 {mines} 处敌方工事，触雷/障碍伤亡 {mine_dmg:.0f}"
                             f"（剩余 {unit.hp:.0f}）", "combat")
            if not unit.alive:
                self.casualties[unit.side] += 1
                self.log_message(unit.side, f"{unit.name} 在障碍地带被摧毁", "combat")
                self._check_elimination()
                self.refresh_detection()
                return True
        self._handle_capture(unit)
        self.refresh_detection()
        self.log_message(unit.side, f"{unit.name} 机动至 ({unit.x},{unit.y})，消耗 {cost:.1f} 机动点", "move")
        return True

    def _handle_capture(self, unit: Unit) -> None:
        if not unit.template.can_capture or unit.is_decoy:
            return
        t = self.map.at(unit.x, unit.y)
        if t is None:
            return
        if t.fort > 0 and t.fort_owner is not Side.NEUTRAL and t.fort_owner is not unit.side:
            t.fort = 0
            t.fort_progress = 0
            t.fort_build = 0
            t.fort_rubble = False    # 人工拆除，不是打塌的，不算废墟
            self.map_epoch += 1
            self.log_message(unit.side, f"{unit.name} 清除了 ({t.x},{t.y}) 的敌方工事", "combat")
        if t.facility is not Facility.NONE and t.owner is not unit.side:
            old = t.owner
            t.owner = unit.side
            self.add_fx("ping", (t.x, t.y), None, ttl=1.2, color=(140, 255, 190))
            # 逐帧动画：夺取设施的绿色脉冲
            self.anim.add("ping", (t.x, t.y), None, ttl=1.0, color=(140, 255, 190))
            self.log_message(unit.side,
                             f"{unit.name} 夺取 {t.facility.label} ({t.x},{t.y})，原属{old.label}", "combat")
        if t.objective and t.owner is not unit.side:
            t.owner = unit.side
            self.add_fx("ping", (unit.x, unit.y), None, ttl=1.2, color=(255, 230, 140))
            # 逐帧动画：占领目标点的金色脉冲
            self.anim.add("ping", (unit.x, unit.y), None, ttl=1.0, color=(255, 230, 140))
            self.log_message(unit.side, f"{unit.name} 占领 {t.objective_name or '目标点'} ({t.x},{t.y})", "combat")

    # -- 传感器开关 -------------------------------------------------------
    def toggle_radar(self, unit: Unit) -> None:
        if not any(s.kind.value == "radar" for s in unit.template.sensors):
            return
        unit.radar_on = not unit.radar_on
        state_txt = "开机" if unit.radar_on else "关机"
        self.refresh_detection()
        self.log_message(unit.side, f"{unit.name} 雷达{state_txt}", "ew")

    def toggle_jam(self, unit: Unit) -> None:
        if unit.template.jam_strength <= 0:
            return
        unit.jam_on = not unit.jam_on
        self.refresh_detection()
        bands = "、".join(f"{b}频段" for b in unit.template.jam_bands) or "全频段"
        self.log_message(unit.side, f"{unit.name} 电子干扰{'启动' if unit.jam_on else '停止'}"
                         f"（{bands}｜半径 {unit.template.jam_radius:.0f} 格，"
                         f"强度 {unit.template.jam_strength * 100:.0f}%）", "ew")
        if unit.jam_on:
            self.add_fx("ping", (unit.x, unit.y), None, ttl=1.0, color=(200, 150, 255))

    def toggle_datalink(self, unit: Unit) -> bool:
        """开关数据链：通信受扰时主动断链，牺牲协同换抗干扰。"""
        if not unit.template.datalink:
            return False
        unit.datalink_on = not unit.datalink_on
        state_txt = "接通" if unit.datalink_on else "切断"
        self.log_message(unit.side, f"{unit.name} 数据链{state_txt}", "ew")
        return True

    # -- 交战 -------------------------------------------------------------
    def best_weapon_vs(self, attacker: Unit, target: Unit) -> tuple[Weapon | None, rules.ShotProfile | None]:
        best_w, best_p = None, None
        best_score = -1.0
        for w in attacker.usable_weapons():
            p = rules.shot_profile(self, attacker, target, w)
            if not p.ok:
                continue
            score = p.pk * (1 - p.intercept_pk) * p.damage
            if score > best_score:
                best_score, best_w, best_p = score, w, p
        return best_w, best_p

    def fire(self, attacker: Unit, target: Unit, weapon: Weapon,
             salvo: int | None = None) -> FireResult:
        if attacker.shots_left <= 0:
            return FireResult(False, "本回合发射次数已用尽")
        acting = self.player_side if self.phase == "player" else self.ai_side
        if self.phase == "over":
            return FireResult(False, "作战已结束")
        if attacker.side is not acting:
            return FireResult(False, "非本方回合")

        # 开火前钩子：模组可以改状态（装填、撤出、换目标等）
        self.mods.fire(_mods.EV_BEFORE_FIRE, self, attacker, target, weapon)

        prof = rules.shot_profile(self, attacker, target, weapon)
        if not prof.ok:
            return FireResult(False, prof.reason)

        # 齐射发数：受武器上限、备弹、补给状态与玩家设置共同约束
        ammo_left = attacker.ammo.get(weapon.id, 0)
        n = max(1, min(int(salvo or self.salvo_setting),
                       rules.salvo_limit(attacker, weapon), ammo_left))
        attacker.ammo[weapon.id] = ammo_left - n
        attacker.consume_shot()
        attacker.revealed_until = self.turn_count + 1

        flight = max(1, math.ceil(prof.distance / max(0.5, weapon.speed)))
        if flight <= 1:
            res = self._impact(attacker, target, weapon, salvo=n, prof=prof)
            self.mods.fire(_mods.EV_AFTER_FIRE, self, attacker, target, weapon, res)
            return res

        eta = min(3, flight)
        if weapon.ballistic:
            eta = max(2, eta)        # 中段窗口：给防守方一次反导交战机会
        missile = Missile(
            weapon=weapon, attacker_uid=attacker.uid, attacker_side=attacker.side,
            target_uid=target.uid, origin=(attacker.x, attacker.y),
            target_pos=(target.x, target.y), eta=eta, salvo=n,
            label=weapon.name,
        )
        self.missiles.append(missile)
        self.add_fx("missile", (attacker.x, attacker.y), (target.x, target.y),
                    ttl=1.0, color=(255, 150, 90))
        # 逐帧动画：弹道拖尾 + 发射闪光
        self.anim.add("trail", (attacker.x, attacker.y), (target.x, target.y),
                      ttl=1.2, color=(255, 170, 100))
        self.anim.add("burst", (attacker.x, attacker.y), None,
                      ttl=0.35, color=(255, 220, 160), size=0.45)
        self.emit_sfx("fire")
        self.log_message(attacker.side,
                         f"{attacker.name} 发射 {weapon.name} × {n} → {target.name}"
                         f"（{prof.distance:.1f} 格，预计 {missile.eta} 回合后抵达）", "combat")
        self.refresh_detection()
        # 上升段拦截：部署到对方发射阵位旁边的反导系统，能在弹头最脆弱的
        # 那几十秒里打掉它——这是「前推部署」唯一能拿到的回报
        if weapon.ballistic and self._boost_interception(missile):
            res = FireResult(True, "发射后即遭上升段拦截", intercepted=True)
            self.mods.fire(_mods.EV_AFTER_FIRE, self, attacker, target, weapon, res)
            return res
        res = FireResult(True, f"发射 {weapon.name}×{n}，飞行 {missile.eta} 回合",
                         in_flight=True, flight=missile.eta)
        self.mods.fire(_mods.EV_AFTER_FIRE, self, attacker, target, weapon, res)
        return res

    def _boost_interception(self, missile) -> bool:
        """上升段拦截：弹头还在助推段，只有贴着发射点的反导系统够得着。

        命中率按中段 Pk 的三成多算——窗口太短，火控来不及稳定跟踪。
        拦下来就把这发从在飞列表里摘掉，返回 True。
        """
        defended = missile.attacker_side.foe
        for d, w, pk in rules.midcourse_interceptors(self, missile.origin, defended):
            d.ammo[w.id] = max(0, d.ammo.get(w.id, 0) - 1)
            boost_pk = pk * rules.BOOST_PK_SCALE
            if dice.roll(boost_pk, self.rng):
                self.add_fx("intercept", missile.origin, None, ttl=1.2,
                            color=(255, 190, 140), text="上升段拦截")
                self.anim.add("intercept", missile.origin, None, ttl=1.0,
                              color=(255, 190, 140))
                self.anim.add("label", missile.origin, None, ttl=1.2,
                              color=(255, 210, 170), text="上升段拦截")
                self.emit_sfx("intercept")
                self.log_message(defended,
                                 f"{d.name} 发射 {w.name}，在 {missile.label} 的上升段"
                                 f"将其击落（Pk {boost_pk * 100:.0f}%）", "combat")
                if missile in self.missiles:
                    self.missiles.remove(missile)
                return True
            self.log_message(defended,
                             f"{d.name} 试图在上升段拦截 {missile.label}，未能命中", "combat")
        return False

    def resolve_missiles(self) -> None:
        """推进在飞弹药；抵达者进行末端交战。"""
        remaining: list[Missile] = []
        for m in self.missiles:
            attacker = self.unit_by_uid(m.attacker_uid)
            target = self.unit_by_uid(m.target_uid)

            # 弹道导弹的中段窗口：只有专用反导系统（SM-3/THAAD/77N6）能够交战。
            # v3.1 起不再「一次窗口定生死」——弹头在飞，防御方可以打第二轮，
            # 每轮换一批火控通道与备弹，直到它进入末段（eta == 1）。
            if (m.weapon.ballistic and m.eta > 1
                    and m.midcourse_windows < rules.MIDCOURSE_WINDOWS):
                m.midcourse_windows += 1
                # 第二个窗口挪到弹道后段：越接近目标，能参与交战的反导系统越少
                frac = 0.5 + 0.25 * (m.midcourse_windows - 1)
                mid = (int(round(m.origin[0] + (m.target_pos[0] - m.origin[0]) * frac)),
                       int(round(m.origin[1] + (m.target_pos[1] - m.origin[1]) * frac)))
                defended = m.attacker_side.foe
                destroyed = False
                for d, w, pk in rules.midcourse_interceptors(self, mid, defended):
                    d.ammo[w.id] = max(0, d.ammo.get(w.id, 0) - 1)
                    if dice.roll(pk, self.rng):
                        self.add_fx("intercept", mid, None, ttl=1.2, color=(150, 220, 255),
                                    text="中段拦截")
                        # 逐帧动画：中段拦截的冷色调涟漪
                        self.anim.add("intercept", mid, None, ttl=1.0,
                                      color=(150, 220, 255))
                        self.anim.add("label", mid, None, ttl=1.1,
                                      color=(180, 235, 255), text="中段拦截")
                        self.emit_sfx("intercept")
                        self.log_message(defended,
                                         f"{d.name} 发射 {w.name} 在中段第 {m.midcourse_windows} 轮"
                                         f"成功拦截 {m.label}（单发 Pk {pk * 100:.0f}%）", "combat")
                        destroyed = True
                        break
                    else:
                        self.log_message(defended,
                                         f"{d.name} 发射 {w.name} 实施中段拦截，未能命中 {m.label}", "combat")
                if destroyed:
                    continue

            m.eta -= 1
            if m.eta > 0:
                remaining.append(m)
                continue
            if attacker is None or not attacker.alive:
                self.log_message(m.attacker_side, f"{m.label} 载机已被击落，弹药失控自毁", "combat")
                continue
            if target is None or not target.alive:
                self.log_message(m.attacker_side, f"{m.label} 目标已消失，弹药自毁", "combat")
                continue
            m.target_pos = (target.x, target.y)
            self._impact(attacker, target, m.weapon, salvo=m.salvo, airborne=True,
                         origin=m.origin)
        self.missiles = remaining

    def _impact(self, attacker: Unit, target: Unit, weapon: Weapon,
                salvo: int = 1, airborne: bool = False,
                prof: rules.ShotProfile | None = None,
                origin: tuple[int, int] | None = None) -> FireResult:
        """末端交战：拦截 → 命中判定 → 毁伤。"""
        if prof is None:
            # 在飞弹药抵达时重新解算：弹药已在发射时扣除，此处不应受备弹影响
            saved = attacker.ammo.get(weapon.id, 0)
            attacker.ammo[weapon.id] = max(1, saved)
            try:
                prof = rules.shot_profile(self, attacker, target, weapon, origin=origin)
            finally:
                attacker.ammo[weapon.id] = saved

        # 飞行途中目标脱离射程 / 丢失情报 / 失去制导条件 → 脱靶
        if not prof.ok:
            self.log_message(attacker.side, f"{weapon.name} 未能命中：{prof.reason}", "combat")
            self.add_fx("ping", (target.x, target.y), None, ttl=0.7, color=(140, 140, 160))
            return FireResult(False, f"未能命中：{prof.reason}")

        # 电子诱饵：雷达/红外特征逼真，但打上去什么也没有
        if target.is_decoy:
            target.destroyed = True
            self.add_fx("ping", (target.x, target.y), None, ttl=1.1, color=(170, 140, 255),
                        text="诱饵")
            self.log_message(attacker.side,
                             f"{attacker.name} 对 {target.name} 的攻击命中空目标——"
                             f"确认为电子诱饵，弹药被浪费", "ew")
            return FireResult(True, "目标为诱饵信号", destroyed=True)

        # 逐发拦截：每发弹药独立经过防空网，拦截消耗防守方通道与备弹
        salvo = max(1, salvo)
        leakers, events, partial = rules.resolve_salvo_interception(
            self, (target.x, target.y), target.side, weapon, salvo, self_unit=target)
        # 统计：开火数与被拦截数
        st_att = self.stats[attacker.side]
        st_att["shots"] += salvo
        if leakers < salvo:
            st_att["intercepted"] += salvo - leakers
        if events:
            self.add_fx("intercept", (target.x, target.y), None, ttl=0.9, color=(120, 255, 200))
            # 逐帧动画：拦截涟漪 + 飘字
            self.anim.add("intercept", (target.x, target.y), None,
                          ttl=0.8, color=(120, 255, 210))
            self.anim.add("label", (target.x, target.y), None, ttl=0.9,
                          color=(150, 255, 215), text="拦截")
            self.emit_sfx("intercept")
            for ev in events[:4]:
                self.log_message(target.side, f"{target.name} 防空网：{ev}", "combat")
        if leakers <= 0:
            self.log_message(target.side,
                             f"{target.name} 的防空网拦截了全部 {salvo} 发 {weapon.name}", "combat")
            return FireResult(True, "全部被拦截", intercepted=True)
        if leakers < salvo:
            self.log_message(attacker.side,
                             f"{weapon.name} 齐射 {salvo} 发，{salvo - leakers} 发被拦截，"
                             f"{leakers} 发突防", "combat")
        if partial > 0:
            self.log_message(attacker.side,
                             f"其中 {partial} 发被破片击伤，弹体受损、毁伤下降", "combat")
        salvo = leakers

        # 逐发命中
        hits = 0
        total_damage = 0.0
        for i in range(max(1, salvo)):
            if dice.roll(prof.pk, self.rng):
                # 被打残的那几发按 PARTIAL_DAMAGE 折算：破片撕开弹体，
                # 战斗部剩下的能量不足以打出标称毁伤
                scale = rules.PARTIAL_DAMAGE if i < partial else 1.0
                dmg = rules.jitter(prof.damage, rng=self.rng) * scale
                total_damage += dmg
                hits += 1
        st_att["hits"] += hits
        st_att["damage"] += int(total_damage)

        self.add_fx("tracer", (attacker.x, attacker.y), (target.x, target.y),
                    ttl=0.55, color=(255, 220, 120))
        if hits == 0:
            self.add_fx("ping", (target.x, target.y), None, ttl=0.6, color=(150, 150, 170))
            # 未命中：只留一道淡去的弹道痕迹
            self.anim.add("trail", (attacker.x, attacker.y), (target.x, target.y),
                          ttl=0.6, color=(200, 190, 140))
            self.log_message(attacker.side,
                             f"{attacker.name} → {target.name}｜{weapon.name} 未命中（Pk {prof.pk * 100:.0f}%）", "combat")
            return FireResult(True, "未命中")

        # 伤害修正钩子：模组返回新伤害值（返回 None 表示不改）
        _mod_dmg = self.mods.fire(_mods.EV_MODIFY_DAMAGE, self, attacker,
                                  target, weapon, total_damage)
        if _mod_dmg is not None:
            try:
                total_damage = max(0.0, float(_mod_dmg))
            except (TypeError, ValueError):
                pass

        if target.domain is Domain.AIR:
            if target.template.one_shot_kill:
                target.hp = 0.0
                target.destroyed = True
                self.add_fx("explosion", (target.x, target.y), None, ttl=1.6, color=(255, 170, 70),
                            text="空中解体")
            else:
                total_damage *= 2.2      # 大型机结构冗余，但依然极易被重创
                target.apply_damage(total_damage)
        else:
            target.apply_damage(total_damage)
        target.suppressed = max(target.suppressed, 1)
        target.revealed_until = self.turn_count + 1
        self.add_fx("explosion", (target.x, target.y), None, ttl=0.8, color=(255, 140, 60),
                    text=f"-{total_damage:.0f}")
        # 逐帧动画：命中闪光 + 冲击环
        self.anim.add("flash", (target.x, target.y), None, ttl=0.28,
                      color=(255, 240, 200))
        self.anim.add("burst", (target.x, target.y), None, ttl=0.7,
                      color=(255, 160, 80))
        self.emit_sfx("explode")

        # 命中同时破坏敌方工事（面杀伤武器效率更高）
        if weapon.anti_fort > 0:
            tgt_tile = self.map.at(target.x, target.y)
            if tgt_tile is not None and tgt_tile.fort > 0 \
                    and tgt_tile.fort_owner is not attacker.side:
                before = tgt_tile.fort
                tgt_tile.fort = max(0, tgt_tile.fort - weapon.anti_fort)
                if tgt_tile.fort == 0:
                    tgt_tile.fort_progress = 0
                    tgt_tile.fort_build = 0
                    # 打塌了就是废墟：原地重建要多花工时（清障、回填）
                    tgt_tile.fort_rubble = before > 0
                if tgt_tile.fort < before:
                    self.map_epoch += 1
                    self.log_message(attacker.side,
                                     f"（{target.x},{target.y}）敌方工事被削弱至 Lv{tgt_tile.fort}",
                                     "combat")

        killed = target.destroyed
        prep_note = f"（{prof.notes[0]}）" if prof.notes else ""
        if killed:
            self.casualties[target.side] += 1
            st_att["kills"] += 1
            self.mods.fire(_mods.EV_UNIT_DESTROYED, self, target)
            self.add_fx("explosion", (target.x, target.y), None, ttl=1.6, color=(255, 90, 60))
            # 逐帧动画：更大的爆炸 + 击毁飘字
            self.anim.add("burst", (target.x, target.y), None, ttl=1.1,
                          color=(255, 110, 70), size=1.5)
            self.anim.add("label", (target.x, target.y), None, ttl=1.3,
                          color=(255, 160, 130), text="击毁")
            self.log_message(attacker.side,
                             f"{attacker.name} 以 {weapon.name} 击毁 {target.name}"
                             f"（{hits} 发命中 / 伤害 {total_damage:.0f}）{prep_note}", "combat")
        else:
            self.log_message(attacker.side,
                             f"{attacker.name} → {target.name}｜{weapon.name} 命中 {hits} 发，"
                             f"伤害 {total_damage:.0f}（剩余 {target.hp:.0f}/{target.template.hp:.0f}）"
                             f"{prep_note}", "combat")

        if killed:
            self._check_elimination()
        self.refresh_detection()
        return FireResult(True, "命中" if not killed else "击毁", hits=hits,
                          damage=total_damage, destroyed=killed)

    # -- 固定设施打击 -----------------------------------------------------
    def strike_facility(self, attacker: Unit, pos: tuple[int, int], weapon: Weapon,
                        salvo: int | None = None) -> FireResult:
        """对机场/港口/后勤枢纽等固定设施实施火力打击。"""
        if attacker.shots_left <= 0:
            return FireResult(False, "本回合发射次数已用尽")
        if self.phase == "over":
            return FireResult(False, "作战已结束")
        acting = self.player_side if self.phase == "player" else self.ai_side
        if attacker.side is not acting:
            return FireResult(False, "非本方回合")
        tile = self.map.at(*pos)
        if tile is None:
            return FireResult(False, "目标地块无效")

        prof = rules.facility_strike_profile(self, attacker, tile, weapon)
        if not prof.ok:
            return FireResult(False, prof.reason)

        ammo_left = attacker.ammo.get(weapon.id, 0)
        n = max(1, min(int(salvo or self.salvo_setting),
                       rules.salvo_limit(attacker, weapon), ammo_left))
        attacker.ammo[weapon.id] = ammo_left - n
        attacker.consume_shot()
        attacker.revealed_until = self.turn_count + 1
        name = f"{tile.facility.label}({tile.x},{tile.y})"

        self.add_fx("tracer", (attacker.x, attacker.y), (tile.x, tile.y),
                    ttl=0.6, color=(255, 200, 120))

        n_before = n
        partial = 0
        if tile.owner in (Side.BLUE, Side.RED):
            leakers, events, partial = rules.resolve_salvo_interception(
                self, (tile.x, tile.y), tile.owner, weapon, n)
            if events:
                self.add_fx("intercept", (tile.x, tile.y), None, ttl=0.9, color=(120, 255, 200))
                for ev in events[:3]:
                    self.log_message(tile.owner, f"{name} 防空网：{ev}", "combat")
            n = leakers
        if n <= 0:
            self.log_message(attacker.side,
                             f"{attacker.name} 对 {name} 的 {n_before} 发打击弹药全部被拦截", "combat")
            return FireResult(True, "打击全部被拦截", intercepted=True)
        if n < n_before:
            self.log_message(attacker.side,
                             f"对 {name} 的打击：{n_before - n} 发被拦截，{n} 发突防", "combat")

        hits, total = 0, 0.0
        for i in range(n):
            if dice.roll(prof.pk, self.rng):
                # 被破片打残的那几发只带来部分毁伤
                scale = rules.PARTIAL_DAMAGE if i < partial else 1.0
                dmg = rules.jitter(prof.damage, rng=self.rng) * scale
                tile.facility_hp = max(0.0, tile.facility_hp - dmg)
                hits += 1
                total += dmg

        if hits == 0:
            self.add_fx("ping", (tile.x, tile.y), None, ttl=0.7, color=(150, 150, 170))
            self.log_message(attacker.side,
                             f"{attacker.name} 对 {name} 的打击未奏效（Pk {prof.pk * 100:.0f}%）", "combat")
            return FireResult(True, "未命中")

        if weapon.anti_fort > 0 and tile.fort > 0 and tile.fort_owner is not attacker.side:
            before = tile.fort
            tile.fort = max(0, tile.fort - weapon.anti_fort)
            if tile.fort == 0:
                tile.fort_progress = 0
                tile.fort_build = 0
                tile.fort_rubble = before > 0
            self.map_epoch += 1
        self.add_fx("explosion", (tile.x, tile.y), None, ttl=0.9, color=(255, 160, 80),
                    text=f"-{total:.0f}")
        status = ""
        if tile.facility_hp <= 0:
            status = "，设施被摧毁"
        elif not tile.operational:
            status = "，设施失能"
        self.log_message(attacker.side,
                         f"{attacker.name} 以 {weapon.name}×{n} 打击 {name}：命中 {hits} 发，"
                         f"结构完整度降至 {tile.facility_hp_ratio * 100:.0f}%{status}", "combat")
        if not tile.operational:
            for u in self.units:
                if u.alive and (u.x, u.y) == pos:
                    u.revealed_until = self.turn_count + 1
            # 补给枢纽失能 → 相关部队的补给线立即中断
            if tile.facility in rules.SUPPLY_PROFILE:
                self._recompute_supply()
                cut = [u.name for u in self.units_of(tile.owner)
                       if u.alive and u.uid not in self.supply_links]
                if cut:
                    self.log_message(tile.owner,
                                     f"{name} 失能——补给线中断：{'、'.join(cut[:4])}", "ew")
        self.refresh_detection()
        return FireResult(True, "打击完成", hits=hits, damage=total)

    # -- 烟幕与任务剖面 ---------------------------------------------------
    def fire_smoke(self, unit: Unit, pos: tuple[int, int], weapon: Weapon) -> bool:
        """发射烟幕弹：在目标格形成持续 3 回合的遮蔽。

        面压制火力（火炮/火箭炮）可在射程内对任意已知坐标施放烟幕，
        不需要目标情报——打的是地形而不是单位。
        """
        if not unit.alive or unit.is_decoy or unit.shots_left <= 0:
            return False
        if weapon.kind not in (WeaponKind.ARTILLERY, WeaponKind.MLRS):
            return False
        acting = self.player_side if self.phase == "player" else self.ai_side
        if self.phase == "over" or unit.side is not acting:
            return False
        if unit.ammo.get(weapon.id, 0) <= 0:
            return False
        tile = self.map.at(*pos)
        if tile is None:
            return False
        d = rules.dist((unit.x, unit.y), pos)
        if d > weapon.range_max + rules.RANGE_TOLERANCE or d < weapon.range_min:
            return False
        unit.ammo[weapon.id] = max(0, unit.ammo.get(weapon.id, 0) - 1)
        unit.consume_shot()
        unit.revealed_until = self.turn_count + 1
        tiles = [tile]
        if weapon.kind is WeaponKind.MLRS:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                t2 = self.map.at(pos[0] + dx, pos[1] + dy)
                if t2 is not None:
                    tiles.append(t2)
        for t in tiles:
            t.smoke = 3
        # 地块状态变了，渲染层的烟幕索引要跟着失效（它按回合号 + 世代号缓存）
        self.map_epoch += 1
        self.add_fx("ping", pos, None, ttl=1.0, color=(200, 205, 215), text="烟幕")
        # 逐帧动画：烟幕逐团膨胀扩散
        for i, t in enumerate(tiles):
            self.anim.add("smoke", (t.x, t.y), None, ttl=1.6,
                          color=(205, 208, 216), size=0.8 + 0.15 * (i % 3))
        self.log_message(unit.side,
                         f"{unit.name} 对 ({pos[0]},{pos[1]}) 施放烟幕"
                         f"（遮蔽 {len(tiles)} 格，持续 3 回合，"
                         f"{environment_for(self).wind_text()}）", "combat")
        self.refresh_detection()
        return True

    def _tick_smoke(self) -> None:
        """烟幕随风漂移与消散。

        遮蔽团整体下风移动，风力决定移动快慢与消散速度：
        静风糊在原地、微风两回合挪一格、和风以上每回合挪一格，
        强风还会额外缩短一回合寿命。

        因此「顺风放烟」才是正确用法——逆风施放，烟会飘回自己头上。
        这也让天候与地形（山谷、城区）对遮蔽的影响第一次变得可感知。
        """
        env = environment_for(self)
        dx, dy = env.wind_vector
        period = env.smoke_period
        drift = period > 0 and self.turn_count % period == 0
        fast = env.wind_speed >= WIND_MAX

        puffs: list[tuple[int, int, int]] = []
        for t in self.map.iter_tiles():
            if t.smoke > 0:
                puffs.append((t.x, t.y, t.smoke))
                t.smoke = 0

        for x, y, remain in puffs:
            # 漂移与消散在同一拍结算：先算寿命，再定落点
            remain -= 1
            if fast:
                remain -= 1
            nx, ny = x, y
            if drift:
                tile = self.map.at(x + dx, y + dy)
                if tile is not None:
                    nx, ny = tile.x, tile.y
            if remain <= 0:
                continue
            tile = self.map.at(nx, ny)
            if tile is None:
                continue
            # 两团烟叠在一格取较浓的一侧，不累加成永久遮蔽
            tile.smoke = max(tile.smoke, remain)

    def set_mission(self, unit: Unit, mission: str) -> bool:
        """切换航空兵任务剖面：strike 打击 / cap 制空巡逻 / ferry 转场。"""
        if unit.domain is not Domain.AIR or mission not in ("strike", "cap", "ferry"):
            return False
        if unit.mission == mission:
            return True
        unit.mission = mission
        unit.mp = min(unit.mp, unit.template.mp * unit.mp_factor)
        label = {"strike": "打击", "cap": "制空巡逻", "ferry": "转场"}[mission]
        self.log_message(unit.side, f"{unit.name} 转入{label}剖面", "info")
        self.refresh_detection()
        return True

    # -- 指挥体系 ---------------------------------------------------------
    ORDER_TURNS = 2               # 指令有效回合数
    ORDER_LABELS = {
        "focus_fire": "集中火力",
        "priority": "优先目标",
        "hold": "固守",
        "assault": "强攻",
    }
    PRIORITY_CLASSES = {
        "sam": ("sam", "aaa"),      # 防空系统
        "radar": ("radar",),        # 雷达
        "hq": ("hq",),              # 指挥所
        "air": ("fighter", "bomber", "aew", "uav", "tanker"),   # 空中平台
    }

    def _echelon_rank(self, u: Unit) -> int:
        """单位层级序：连 0 / 营 1 / 旅 2。"""
        return {"platoon": 0, "company": 1, "brigade": 2}.get(u.template.echelon, 0)

    def subordinates_of(self, commander: Unit) -> list[Unit]:
        """指挥者的直属下级：层级更低且在指挥半径内。"""
        if not commander.alive or commander.template.command_radius <= 0:
            return []
        cr = commander.template.command_radius
        rank = self._echelon_rank(commander)
        out = []
        for u in self.units_of(commander.side):
            if u is commander or u.is_decoy or not u.alive:
                continue
            if self._echelon_rank(u) >= rank:
                continue
            if rules.dist((commander.x, commander.y), (u.x, u.y)) > cr:
                continue
            out.append(u)
        return out

    def can_issue_order(self, commander: Unit, kind: str,
                        target: Unit | None = None) -> tuple[bool, str]:
        """检查能否下达某指令。"""
        if not commander.alive:
            return False, "指挥单位已不存在"
        if commander.template.command_radius <= 0:
            return False, f"{commander.name} 不具备指挥能力"
        subs = self.subordinates_of(commander)
        if not subs:
            return False, f"{commander.name} 指挥半径内没有下级单位"
        if kind == "focus_fire" or kind == "assault":
            if target is None:
                return False, "需要指定目标"
            if target.side is commander.side:
                return False, "不能向己方单位下达该指令"
            d = rules.dist((commander.x, commander.y), (target.x, target.y))
            if d > commander.template.command_range + rules.RANGE_TOLERANCE:
                return False, (f"目标超出火力协调半径"
                               f"（{d:.1f} > {commander.template.command_range:.0f} 格）")
        if kind == "priority" and not target:
            return False, "需要指定优先目标类别"
        return True, ""

    def issue_order(self, commander: Unit, kind: str, target: Unit | None = None,
                    priority_class: str = "", pos: tuple[int, int] | None = None) -> tuple[bool, str]:
        """下达指令给指挥半径内的全部下级单位。"""
        ok, reason = self.can_issue_order(commander, kind, target)
        if not ok:
            return False, reason
        subs = self.subordinates_of(commander)
        n = 0
        for sub in subs:
            sub.order = Order(
                kind=kind,
                issuer_uid=commander.uid,
                target_uid=target.uid if target is not None else 0,
                priority_class=priority_class,
                turns_left=self.ORDER_TURNS,
                x=pos[0] if pos else (target.x if target else commander.x),
                y=pos[1] if pos else (target.y if target else commander.y),
            )
            n += 1
        label = self.ORDER_LABELS.get(kind, kind)
        extra = ""
        if kind in ("focus_fire", "assault") and target is not None:
            extra = f" → {target.name}"
        elif kind == "priority":
            extra = f"（{priority_class}）"
        self.log_message(commander.side,
                         f"{commander.name} 下达「{label}」指令{extra}，"
                         f"{n} 个下级单位受领", "info")
        return True, f"已下达「{label}」，{n} 个单位受领"

    def clear_orders(self, side: Side) -> None:
        """清除某方全部指令（指挥者阵亡或指令到期时）。"""
        for u in self.units_of(side):
            u.order = None

    def _tick_orders(self) -> None:
        """指令计时与失效；指挥者阵亡则其指令一并撤销。"""
        for u in self.units:
            if u.order is None:
                continue
            issuer = self.unit_by_uid(u.order.issuer_uid)
            if issuer is None or not issuer.alive:
                u.order = None
                continue
            u.order.turns_left -= 1
            if u.order.turns_left <= 0:
                u.order = None

    # -- 火力准备 ---------------------------------------------------------
    # 预先射击：登记射界需要的情报等级与代价
    FIRE_PREPARE_MP = 1.5         # 登记射界消耗的机动点
    FIRE_PREPARE_TURNS = 2        # 射界有效期（回合）
    FIRE_PREPARE_RADIUS = 1.5     # 射界覆盖半径（格）
    FIRE_PREPARE_PK_BONUS = 1.45  # 准备完成后的命中倍率
    FIRE_PREPARE_DMG_BONUS = 1.30 # 准备完成后的毁伤倍率

    def register_fire_mission(self, unit: Unit, pos: tuple[int, int]) -> tuple[bool, str]:
        """登记射界：炮兵预先侦察并标定目标区域。

        要求：
        * 单位具备间瞄火力（火炮 / 火箭炮）；
        * 有足够机动点；
        * 目标格在射程内；
        * 对该区域**有情报**——没有情报就登记射界是盲打，违背预先射击的意义。
        """
        if not unit.alive or unit.is_decoy:
            return False, "单位不可用"
        if not unit.can_prepare_fire:
            return False, f"{unit.name} 不具备间瞄火力，无法预先射击"
        if unit.mp < self.FIRE_PREPARE_MP:
            return False, f"机动点不足（{unit.mp:.1f} < {self.FIRE_PREPARE_MP}）"
        if unit.fire_mission is not None and unit.fire_mission.active:
            return False, f"{unit.name} 已有在效射界（{unit.fire_mission.x},{unit.fire_mission.y}）"

        # 选一款射程内的间瞄武器
        cands = [w for w in unit.usable_weapons()
                 if w.is_indirect
                 and rules.dist((unit.x, unit.y), pos) <= w.range_max + rules.RANGE_TOLERANCE
                 and rules.dist((unit.x, unit.y), pos) >= w.range_min]
        if not cands:
            return False, "该位置不在任何间瞄武器的射程内"

        # 情报要求：至少「接触」——完全未知的目标无法标定射界
        intel = self._intel_at(unit.side, pos)
        if intel is Detection.NONE:
            return False, "该区域无任何情报，无法标定射界"

        unit.mp = max(0.0, unit.mp - self.FIRE_PREPARE_MP)
        unit.moved = True
        unit.fire_mission = FireMission(
            x=pos[0], y=pos[1],
            turns_left=self.FIRE_PREPARE_TURNS,
            prepared=False,
            total=self.FIRE_PREPARE_TURNS,
            intel_level=intel.value,
        )
        self.log_message(unit.side,
                         f"{unit.name} 登记射界 ({pos[0]},{pos[1]})"
                         f"（情报 {intel.label}，需 {self.FIRE_PREPARE_TURNS} 回合完成准备）", "move")
        self.add_fx("ping", pos, None, ttl=1.0, color=(255, 210, 130), text="射界")
        return True, "射界已登记"

    def _intel_at(self, side: Side, pos: tuple[int, int]) -> Detection:
        """某格的情报等级：取该格上敌对单位中最高的已知等级。"""
        best = Detection.NONE
        for u in self.units_at(*pos):
            if u.side is side or u.is_decoy:
                continue
            lvl = self.known_level(side, u)
            if lvl.level > best.level:
                best = lvl
        return best

    def fire_mission_bonus(self, unit: Unit, target: Unit) -> tuple[float, float, str]:
        """计算火力准备带来的加成：(命中倍率, 毁伤倍率, 说明)。"""
        fm = unit.fire_mission
        if fm is None or not fm.ready:
            return 1.0, 1.0, ""
        d = rules.dist((fm.x, fm.y), (target.x, target.y))
        if d > self.FIRE_PREPARE_RADIUS + rules.RANGE_TOLERANCE:
            return 1.0, 1.0, ""
        return (self.FIRE_PREPARE_PK_BONUS, self.FIRE_PREPARE_DMG_BONUS,
                f"射界命中（{fm.x},{fm.y}）")

    def _resupply_by_line(self, u: Unit) -> None:
        """通过补给线补充油料、弹药、备件与维修（地面/水面部队）。

        补给源被打瘫、承载力耗尽或距离过远时，补给线中断：
        油、弹、备件全部只出不进。断一天还能撑，断三天就真的打不动了
        （`Unit.supply_dry` 累计到 3 时齐射上限与命中率都会掉）。
        """
        src = rules.supply_source_for(self, u)
        if src is None:
            u.supply_dry += 1
            # 只在「刚断」和「彻底枯竭」两个节点提示，避免每回合刷屏
            if u.supply_dry == 1:
                self.log_message(u.side,
                                 f"{u.name} 补给线中断——油弹备件停止补充", "ew")
            elif u.supply_dry == 3:
                self.log_message(u.side,
                                 f"{u.name} 补给枯竭——齐射受限、射击精度下降", "ew")
            return
        if u.supply_dry:
            if u.supply_dry >= 3:
                self.log_message(u.side, f"{u.name} 恢复补给", "info")
            u.supply_dry = 0

        rate = 2 if u.domain in (Domain.SEA, Domain.SUB) else 1
        for w in u.template.weapons:
            cur = u.ammo.get(w.id, 0)
            if cur < w.ammo:
                u.ammo[w.id] = min(w.ammo, cur + rate)
        u.fuel = min(1.0, u.fuel + self.FUEL_RESUPPLY)
        u.spares = min(u.template.hp * 0.5, u.spares + self.SPARES_RESUPPLY)
        # 维修要实打实消耗备件：没有备件就修不动，不是凭空回血
        if u.hp < u.template.hp and u.spares > 0:
            heal = min(u.template.hp * 0.07, u.spares)
            u.hp = min(u.template.hp, u.hp + heal)
            u.spares -= heal

    def _tick_fire_missions(self) -> None:
        """回合推进：准备进度 +1，射界到期失效。"""
        for u in self.units:
            if not u.alive or u.fire_mission is None:
                continue
            fm = u.fire_mission
            if not fm.active:
                u.fire_mission = None
                continue
            fm.turns_left -= 1
            if fm.turns_left <= 0:
                self.log_message(u.side, f"{u.name} 的射界 ({fm.x},{fm.y}) 失效", "move")
                u.fire_mission = None
                continue
            if not fm.prepared:
                fm.prepared = True
                self.log_message(u.side,
                                 f"{u.name} 完成射界准备（{fm.x},{fm.y}）——"
                                 f"对该区域射击命中与毁伤显著提高", "move")
                self.add_fx("ping", (fm.x, fm.y), None, ttl=1.0,
                            color=(255, 220, 150), text="准备完成")

    # -- 回合推进 ---------------------------------------------------------
    def end_player_turn(self) -> None:
        if self.phase != "player" or self.over:
            return
        self.resolve_missiles()
        self.refresh_detection()
        if self._check_end():
            return
        self.phase = "ai"
        self.log_message(None, f"—— 第 {self.turn_count} 回合 · {self.ai_side.label}行动 ——",
                        "system")

    def finish_ai_turn(self) -> None:
        if self.phase != "ai":
            return
        self.resolve_missiles()
        self.refresh_detection()
        self._advance_turn()

    # -- 后勤与基地勤务 ---------------------------------------------------
    FUEL_DRAIN = 0.15          # 空中平台每回合燃油消耗
    #: 每走一格烧掉的油（0..1 库存）。陆上单位满油约能跑 100 格，
    #: 但补给线一断就只出不进——这才是「深入敌后」的真实代价。
    FUEL_PER_TILE = {Domain.LAND: 0.010, Domain.SEA: 0.004, Domain.SUB: 0.005}
    FUEL_RESUPPLY = 0.25       # 补给线每回合回油
    SPARES_RESUPPLY = 6.0      # 补给线每回合补的备件

    SAM_RELOAD = 2        # 防空弹每回合自动补充数量
    CIWS_RELOAD = 1       # 近防炮每回合自动补充数量

    def _reload_interceptors(self) -> None:
        """防空系统补充拦截弹：垂发单元装填极快，不依赖返回基地。

        但装填弹也是弹药——补给线断了就没有新弹可装。
        """
        for u in self.units:
            if not u.alive or u.is_decoy or u.supply_dry > 0:
                continue
            for w in u.template.weapons:
                if w.midcourse:
                    continue
                cur = u.ammo.get(w.id, 0)
                if cur >= w.ammo:
                    continue
                if w.ciws:
                    u.ammo[w.id] = min(w.ammo, cur + self.CIWS_RELOAD)
                elif w.kind.value in ("sam", "aam"):
                    u.ammo[w.id] = min(w.ammo, cur + self.SAM_RELOAD)

    def _recompute_supply(self) -> None:
        """重算双方的补给线分配。"""
        self.supply_links = {}
        for side in (Side.BLUE, Side.RED):
            for uid, src in rules.assign_supply(self, side).items():
                self.supply_links[uid] = src

    def _tick_logistics(self) -> None:
        """回合推进时的油料、弹药与维修结算。

        航空兵必须返回母基地（机场/航母）才能加油挂弹，否则会因燃油耗尽坠毁；
        地面部队在后勤枢纽补充弹药并修复战损；舰艇在港口补充。

        v1.2 起，弹药补充与维修不再要求单位**站在**枢纽上，而是通过
        **补给线**：枢纽有覆盖半径与承载力，部队在半径内即可获得补给，
        但承载力满时得不到——深入敌后或枢纽被打瘫的部队会断粮。
        """
        # 先重算补给线分配
        self._recompute_supply()

        for u in self.units:
            if not u.alive or u.is_decoy:
                continue
            svc = rules.service_tile(self, u)
            if svc is not None:
                if u.domain is Domain.AIR:
                    was_thirsty = u.fuel < 0.95
                    u.fuel = 1.0
                    u.on_station = 0
                    u.mission = "strike"
                    if was_thirsty:
                        self.log_message(u.side, f"{u.name} 返场加油挂弹", "info")
                # 弹药补充
                rate = 2 if u.domain in (Domain.SEA, Domain.SUB) else 1
                for w in u.template.weapons:
                    cur = u.ammo.get(w.id, 0)
                    if cur < w.ammo:
                        u.ammo[w.id] = min(w.ammo, cur + (w.ammo if u.domain is Domain.AIR else rate))
                # 战损修复：机库/船坞的备件也要有来源，但比野战充足
                if u.hp < u.template.hp:
                    if u.spares <= 0:
                        u.spares = u.template.hp * 0.25
                    heal = min(u.template.hp * (0.12 if u.domain is Domain.AIR else 0.07),
                               u.spares)
                    u.hp = min(u.template.hp, u.hp + heal)
                    u.spares -= heal
            elif u.domain is Domain.AIR:
                u.fuel -= self.FUEL_DRAIN * u.fuel_factor
                u.on_station += 1
                if u.fuel <= 0:
                    u.fuel = 0.0
                    u.destroyed = True
                    self.casualties[u.side] += 1
                    self.add_fx("explosion", (u.x, u.y), None, ttl=1.0, color=(255, 120, 60))
                    self.log_message(u.side, f"{u.name} 燃油耗尽坠毁", "combat")
                    self._check_elimination()
            else:
                # 地面/水面部队：通过补给线补充（不必站在枢纽上）
                self._resupply_by_line(u)

        # 设施抢修：无敌情时由工兵逐步恢复
        for t in self.map.iter_tiles():
            if t.facility is Facility.NONE or t.owner is Side.NEUTRAL:
                continue
            if t.facility_hp >= t.facility_max_hp:
                continue
            threatened = any(
                e.alive and e.side is not t.owner and abs(e.x - t.x) <= 2 and abs(e.y - t.y) <= 2
                for e in self.units
            )
            if threatened:
                continue
            t.facility_hp = min(t.facility_max_hp, t.facility_hp + t.facility_max_hp * 0.06)

    DECOY_TTL = {"land": 3, "sea": 4, "air": 2}

    def deploy_decoy(self, unit: Unit, pos: tuple[int, int], kind: str = "radar") -> bool:
        """投放诱饵：在敌方态势图上生成一个假目标。

        * `kind` 决定它骗哪种传感器：radar / ir / visual / multi
        * 诱饵形态跟随投放平台：陆上单位放假地面目标，舰艇放水面假目标，
          航空兵放空射诱饵（MALD 式），三者的存活时间与可放置范围不同。
        """
        if unit.decoy_left <= 0 or unit.is_decoy:
            return False
        if kind == "thermal":          # 兼容旧存档/旧调用
            kind = "ir"
        if kind not in ("radar", "ir", "visual", "multi"):
            return False
        if not self.map.in_bounds(*pos) or self.unit_at(*pos) is not None:
            return False

        dom = unit.domain
        tile = self.map.at(*pos)
        water = tile is not None and tile.terrain.value == "water"
        if dom in (Domain.SEA, Domain.SUB):
            if not water:
                return False           # 水面诱饵只能放在海上
            if rules.dist((unit.x, unit.y), pos) > 5.0:
                return False
            pool = [t for t in self.scenario.armory.templates.values()
                    if t.domain in (Domain.SEA, Domain.SUB) and t.can_capture]
            ttl = self.DECOY_TTL["sea"]
        elif dom is Domain.AIR:
            if rules.dist((unit.x, unit.y), pos) > 8.0:
                return False           # 空射诱饵可前出较远
            pool = [t for t in self.scenario.armory.templates.values()
                    if t.domain is Domain.AIR]
            ttl = self.DECOY_TTL["air"]
        else:
            if water:
                return False           # 地面假目标不能出现在海面
            if rules.dist((unit.x, unit.y), pos) > 4.0:
                return False
            pool = [t for t in self.scenario.armory.templates.values()
                    if t.domain is Domain.LAND and t.can_capture]
            ttl = self.DECOY_TTL["land"]
        pool = [t for t in pool if not t.is_infantry or dom is Domain.LAND]
        if not pool:
            return False

        tpl = self.rng.choice([t for t in pool if t.cost >= 100] or pool)
        d = Unit(template=tpl, side=unit.side, x=pos[0], y=pos[1],
                 uid=self.alloc_uid())
        d.is_decoy = True
        d.decoy_kind = kind
        d.decoy_ttl = ttl
        d.hp = 1.0
        d.ammo = {}
        d.shots_left = 0
        d.name = f"疑似{tpl.name}"
        d.radar_on = kind in ("radar", "multi")
        d.entrench = 0 if dom is Domain.AIR else 1
        if dom is Domain.AIR:
            d.altitude = Altitude.LOW
        self.units.append(d)
        unit.decoy_left -= 1
        self.refresh_detection()
        label = rules.DECOY_LABELS.get(kind, kind)
        form = {Domain.AIR: "空射诱饵", Domain.SEA: "水面假目标",
                Domain.SUB: "水下假目标"}.get(dom, "地面假目标")
        self.log_message(unit.side,
                         f"{unit.name} 在 ({pos[0]},{pos[1]}) 投放{label}（{form}），"
                         f"模拟一支{tpl.name}", "ew")
        self.add_fx("ping", pos, None, ttl=1.2, color=(180, 140, 255))
        return True

    # -- 野战工事 ---------------------------------------------------------
    #: 升到 Lv1 / Lv2 / Lv3 各自需要的**工时**（不是回合数：工兵一次投入 2 工时）
    FORT_WORK = (1, 2, 3)
    #: 在被打塌的废墟上重建，每级多花的工时（先清障、再动土）
    RUBBLE_EXTRA = 1

    def fort_need(self, tile) -> int:
        """升到下一级还差多少工时。"""
        nxt = min(3, tile.fort + 1)
        need = self.FORT_WORK[nxt - 1]
        if tile.fort_rubble and tile.fort == 0:
            need += self.RUBBLE_EXTRA
        return need

    def _fort_add_work(self, tile, work: int) -> int:
        """投入工时并结算涨级，返回本次涨了几级。

        涨级逻辑只此一处：主动施工与驻守维修走同一条路径，
        否则「挖」和「修」的判定迟早会分叉。
        """
        if work <= 0 or tile.fort >= 3:
            return 0
        gained = 0
        tile.fort_build += work
        while tile.fort < 3 and tile.fort_build >= self.fort_need(tile):
            tile.fort_build -= self.fort_need(tile)
            tile.fort += 1
            tile.fort_rubble = False
            gained += 1
        return gained

    def build_fort(self, unit: Unit) -> bool:
        """构筑 / 加固野战工事：消耗 1.0 机动点，投入本回合的施工工时。

        工事不是一拍即成的：Lv1 要 1 工时、Lv2 要 2、Lv3 要 3，
        工兵（fort_power 2）一次能投 2 工时，步兵只有 1。
        所以「步兵一个动作挖出三层永备工事」不再可能——
        想要硬阵地，就得让这支部队在原地说服自己别乱动。
        """
        if not unit.alive or unit.is_decoy or not unit.template.can_fortify:
            return False
        if unit.mp < 1.0:
            return False
        tile = self.map.at(unit.x, unit.y)
        if tile is None or tile.terrain is Terrain.WATER:
            return False
        if tile.fort >= 3 and tile.fort_owner is unit.side:
            self.log_message(unit.side,
                             f"{unit.name} 所在的 ({tile.x},{tile.y}) 工事已达最高等级", "info")
            return False
        if tile.fort_owner is not unit.side:
            # 换手：上一家的工事既不能直接用，也不算自己挖的
            tile.fort = 0
            tile.fort_progress = 0
            tile.fort_build = 0
            tile.fort_rubble = False
            tile.fort_owner = unit.side
        work = max(1, unit.template.fort_power)
        before = tile.fort
        self._fort_add_work(tile, work)
        self.map_epoch += 1
        unit.mp = max(0.0, unit.mp - 1.0)
        unit.moved = True          # 构筑工事期间无法同时保持隐蔽机动
        self.refresh_detection()
        self.add_fx("ping", (tile.x, tile.y), None, ttl=0.9, color=(200, 220, 140))
        if tile.fort > before:
            verb = "开始构筑" if before == 0 else "加固"
            self.log_message(unit.side,
                             f"{unit.name} {verb} ({tile.x},{tile.y}) 工事至 Lv{tile.fort}"
                             f"（投入 {work} 工时）", "move")
        else:
            self.log_message(unit.side,
                             f"{unit.name} 在 ({tile.x},{tile.y}) 继续施工"
                             f"（{tile.fort_build}/{self.fort_need(tile)} 工时，"
                             f"Lv{tile.fort}）", "move")
        return True

    def _tick_fortifications(self) -> None:
        """工事维护：己方工兵/步兵驻守可继续施工，无人维护则逐步荒废。

        驻守产出的是**工时**而不是直接涨级——同一支部队挖两回合
        才够把 Lv1 夯成 Lv2，跟主动施工走同一条路。
        """
        changed = False
        for t in self.map.iter_tiles():
            if t.fort <= 0:
                continue
            crew = [u for u in self.units_at(t.x, t.y)
                    if not u.is_decoy and u.side is t.fort_owner and u.template.can_fortify]
            if crew and not any(u.moved for u in crew):
                t.fort_progress += 1
                if t.fort_progress >= 2 and t.fort < 3:
                    # 一个班组两回合的持续作业，折 1 工时
                    if self._fort_add_work(t, 1):
                        changed = True
                    t.fort_progress = 0
            elif t.fort_owner is not Side.NEUTRAL:
                # 无人维护：每 3 回合衰减 1 级
                t.fort_progress += 1
                if t.fort_progress >= 3:
                    t.fort = max(0, t.fort - 1)
                    t.fort_progress = 0
                    if t.fort == 0:
                        t.fort_build = 0
                    changed = True
        if changed:
            self.map_epoch += 1

    def _tick_entrench(self) -> None:
        """驻守不动的部队构筑工事；机动后工事尽失。"""
        for u in self.units:
            if not u.alive or u.is_decoy:
                continue
            if u.domain not in (Domain.LAND, Domain.INFRA):
                continue
            if u.moved:
                u.entrench = 0
                continue
            cap = 2 if u.template.is_infantry else 1
            if u.entrench < cap:
                u.entrench += 1

    def _tick_decoys(self) -> None:
        for u in list(self.units):
            if not u.is_decoy:
                continue
            u.decoy_ttl -= 1
            if u.decoy_ttl <= 0:
                u.destroyed = True
                self.log_message(u.side, f"{u.name} 的诱饵信号消失", "ew")
        # 清理已失效的诱饵，避免列表膨胀
        self.units = [u for u in self.units if not (u.is_decoy and u.destroyed)]

    # 轨道参数：周期（回合）与重访半径（格）
    SAT_PERIOD = 8
    SAT_FOOTPRINT = 14.0     # SAR 合成孔径雷达幅宽（格）
    SAT_TRACK_R = 7.0        # 星下点近旁可达跟踪级的半径

    def satellite_overhead(self, sat: Unit, turn: int | None = None) -> bool:
        """卫星本回合是否处于有效过顶窗口（星下点进入战区）。"""
        t = self.turn_count if turn is None else turn
        return 0 <= sat.x < self.map.width

    def _tick_satellites(self) -> None:
        """卫星沿地面轨迹机动；仅在过顶窗口内对星下点周边实施侦察。

        轨道抽象为匀速直线地面轨迹：每回合东移 5 格，飞出东界后从西界再入。
        过顶时覆盖星下点 9 格半径：近旁 4.5 格可达跟踪级，其余为识别级。
        """
        self.sat_recon = {Side.BLUE: set(), Side.RED: set()}
        self.sat_track: dict[int, Detection] = {}
        w = self.map.width + 8
        for side in (Side.BLUE, Side.RED):
            for sat in self.satellites(side):
                if not sat.alive:
                    continue
                # 轨道推进：斜向地面轨迹，幅宽会横扫整个战区
                sat.x = ((sat.x + 5) % w) - 4
                sat.y = ((sat.y + 3) % (self.map.height + 8)) - 4
                self.invalidate_pos_index()
                if not self.satellite_overhead(sat):
                    continue
                got = 0
                for tgt in self.enemies_of(side):
                    if tgt.domain is Domain.SPACE:
                        continue
                    d = rules.dist((sat.x, sat.y), (tgt.x, tgt.y))
                    if d > self.SAT_FOOTPRINT:
                        continue
                    self.sat_recon[side].add(tgt.uid)
                    if d <= self.SAT_TRACK_R:
                        self.sat_track[tgt.uid] = Detection.TRACKED
                    got += 1
                if got:
                    self.log_message(side,
                                     f"{sat.name} 过顶（星下点 {sat.x},{sat.y}）："
                                     f"识别 {got} 个目标，覆盖半径 {self.SAT_FOOTPRINT:.0f} 格", "detect")

    def _advance_turn(self) -> None:
        self._tick_entrench()
        self._tick_fortifications()
        self._tick_decoys()
        self._tick_fire_missions()
        self._tick_orders()
        self.turn_count += 1
        for u in self.units:
            if u.alive:
                u.reset_turn(self.turn_count)
        self._tick_smoke()

        self._tick_logistics()
        self._reload_interceptors()

        self._tick_satellites()

        # 回合开始钩子：模组可以在这时做全局调整
        self.mods.fire(_mods.EV_TURN_START, self, self.turn_count)

        self.gps = {s: self._compute_gps(s) for s in (Side.BLUE, Side.RED)}

        self._announce_environment()

        # 想定脚本事件：回合开始时结算，玩家有反应时间
        self._run_events()

        # 规则钩子：条件触发（放在事件之后，让规则看见事件结果）
        self._run_rules()

        # 目标点计分
        for t in self.objective_tiles:
            # 同格可能停驻多支部队（机场停机坪），取任一具备占领能力的单位
            occ = [u for u in self.units_at(t.x, t.y)
                   if u.template.can_capture and not u.is_decoy]
            if occ:
                t.owner = occ[0].side
            if t.owner in (Side.BLUE, Side.RED):
                self.vp[t.owner] += t.victory_points

        # 胜负点曲线：每回合记录一次
        self.vp_history.append((self.turn_count, self.vp[Side.BLUE], self.vp[Side.RED]))

        self.refresh_detection()
        if self.over:
            return                  # 后勤/全歼可能导致对局在中途结束，不要覆盖 over 相位
        self.phase = "player"
        if self._check_end():
            return
        self.log_message(None, f"—— 第 {self.turn_count} 回合 · {self.player_side.label}行动 ——",
                        "system")

    def _run_events(self) -> None:
        """结算当前回合的想定脚本事件。"""
        if not self.events:
            return
        apply_events(self, self.events)
        self.refresh_detection()

    def _run_rules(self) -> None:
        """结算当前回合的规则钩子。

        规则与事件分开求值：事件是「剧本」，规则是「条件」。
        规则放在事件之后，让规则能观察到本回合事件的结果。
        """
        if not self.rules:
            return
        evaluate_rules(self, self.rules)


    def _announce_environment(self) -> None:
        """播报战场环境变化（时段或天候改变时）。"""
        env = environment_for(self)
        prev = getattr(self, "_last_env", None)
        if prev is not None and prev.time_of_day is env.time_of_day \
                and prev.weather is env.weather:
            return
        self._last_env = env
        desc = env.describe_effects()
        if prev is None:
            return
        if prev.weather is not env.weather:
            self.log_message(None,
                             f"天候转为{env.weather.label}（{env.wind_text()}）｜{desc}", "detect")
        else:
            self.log_message(None,
                             f"进入{env.time_of_day.label}（{env.clock}）｜{desc}", "detect")

    def _check_elimination(self) -> None:
        for side in (Side.BLUE, Side.RED):
            if not self.units_of(side):
                self._declare(side.foe, f"{side.label} 作战力量被全歼")

    def _check_end(self) -> bool:
        if self.over:
            return True
        if self.turn_count > self.turn_limit:
            if self.vp[Side.BLUE] > self.vp[Side.RED]:
                self._declare(Side.BLUE, f"作战期满，蓝方以 {self.vp[Side.BLUE]} : {self.vp[Side.RED]} 取胜")
            elif self.vp[Side.RED] > self.vp[Side.BLUE]:
                self._declare(Side.RED, f"作战期满，红方以 {self.vp[Side.RED]} : {self.vp[Side.BLUE]} 取胜")
            else:
                self.over = True
                self.winner = None
                self.win_reason = f"作战期满，双方战平（{self.vp[Side.BLUE]} : {self.vp[Side.RED]}）"
                self.phase = "over"
                self.log_message(None, self.win_reason, "victory")
            return True
        return False

    def _declare(self, winner: Side, reason: str) -> None:
        self.over = True
        self.winner = winner
        self.win_reason = reason
        self.phase = "over"
        self.log_message(None, f"作战结束：{winner.label} 胜利 —— {reason}", "victory")

    @property
    def player_won(self) -> bool:
        return self.winner is self.player_side

    # -- AI 队列（由 core.ai 生成，逐条执行以便渲染动画） -----------------
    def begin_ai_phase(self) -> None:
        from .ai import AIPlanner
        if self.over:
            return
        self._ai_planner = AIPlanner(self, self.ai_side)
        self.phase = "ai"
        self.log_message(self.ai_side, f"{self.ai_side.label}开始行动", "system")

    def ai_pending(self) -> bool:
        planner = getattr(self, "_ai_planner", None)
        return bool(planner and planner.pending())

    @staticmethod
    def ai_salvo(unit: Unit, weapon: Weapon, target: Unit) -> int:
        """AI 的齐射策略：对硬目标双发，其余单发。

        注意不能沿用玩家在界面上选的齐射发数，否则玩家改成三发会让 AI 也三发。
        """
        if weapon.max_salvo <= 1:
            return 1
        ammo = unit.ammo.get(weapon.id, 0)
        if target.hp_ratio > 0.65 and ammo >= weapon.max_salvo * 2:
            return min(2, weapon.max_salvo, ammo)
        return 1

    def apply_ai_action(self, act) -> bool:
        """执行一条 AI 决策。

        UI 的 AI 相位与无界面推演（--sim）共用同一入口，避免两处动作分发不一致
        导致「推演结果与实机对局不同」。
        """
        unit = self.unit_by_uid(act.unit_uid)
        if unit is None or not unit.alive:
            return False
        done = False
        if act.kind == "move" and act.path:
            done = self.move_unit(unit, act.path)
        elif act.kind == "fire" and act.target_uid is not None and act.weapon_id:
            target = self.unit_by_uid(act.target_uid)
            weapon = unit.weapon_by_id(act.weapon_id)
            if target and target.alive and weapon:
                done = self.fire(unit, target, weapon,
                                 salvo=self.ai_salvo(unit, weapon, target)).ok
        elif act.kind == "strike" and act.tile is not None and act.weapon_id:
            weapon = unit.weapon_by_id(act.weapon_id)
            if weapon:
                done = self.strike_facility(unit, act.tile, weapon, salvo=1).ok
        elif act.kind == "radar":
            self.toggle_radar(unit)
            done = True
        elif act.kind == "jam":
            self.toggle_jam(unit)
            done = True
        elif act.kind == "decoy" and act.tile is not None:
            done = self.deploy_decoy(unit, act.tile, getattr(act, "decoy_kind", "") or "radar")
        elif act.kind == "fortify":
            done = self.build_fort(unit)
        elif act.kind == "prepare_fire" and act.tile is not None:
            ok, msg = self.register_fire_mission(unit, act.tile)
            done = ok
            if not ok:
                self.log_message(unit.side, f"{unit.name} 未能登记射界：{msg}", "info")
        elif act.kind == "command":
            target = self.unit_by_uid(act.target_uid) if act.target_uid else None
            ok, msg = self.issue_order(unit, "focus_fire", target=target)
            done = ok
            if not ok:
                self.log_message(unit.side, f"{unit.name} 未能下达指令：{msg}", "info")
        elif act.kind == "smoke" and act.tile is not None and act.weapon_id:
            weapon = unit.weapon_by_id(act.weapon_id)
            if weapon:
                done = self.fire_smoke(unit, act.tile, weapon)
        self.refresh_detection()
        return done

    def ai_step(self) -> dict | None:
        planner = getattr(self, "_ai_planner", None)
        if planner is None:
            return None
        act = planner.step(self)
        if act is None:
            return None
        self.apply_ai_action(act)
        return {"kind": act.kind, "text": act.note}

    # -- 汇总信息 ---------------------------------------------------------
    def summary(self) -> dict:
        return {
            "turn": self.turn_count,
            "limit": self.turn_limit,
            "vp_blue": self.vp[Side.BLUE],
            "vp_red": self.vp[Side.RED],
            "blue_alive": len(self.units_of(Side.BLUE)),
            "red_alive": len(self.units_of(Side.RED)),
            "gps_blue": self.gps[Side.BLUE],
            "gps_red": self.gps[Side.RED],
            "missiles": len(self.missiles),
        }

    def unit_count_by_domain(self, side: Side) -> dict[str, int]:
        out: dict[str, int] = {}
        for u in self.units_of(side):
            out[u.domain.value] = out.get(u.domain.value, 0) + 1
        return out

    def tick_fx(self, dt: float) -> None:
        for f in self.fx:
            f.ttl -= dt
        self.fx = [f for f in self.fx if f.ttl > 0]
        self.anim.tick(dt)


__all__ = ["GameState", "LogEntry", "Fx", "FireResult"]

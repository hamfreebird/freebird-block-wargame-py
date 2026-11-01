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
import random
from dataclasses import dataclass

from .enums import Altitude, Detection, Domain, Facility, Side, Terrain, WeaponKind
from .model import Missile, Tile, Unit, Weapon
from . import rules


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class LogEntry:
    turn: int
    side: Side | None
    text: str
    kind: str = "info"      # info / combat / detect / ew / move / system / victory


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
    def __init__(self, scenario, player_side: Side = Side.BLUE):
        self.scenario = scenario
        self.map = scenario.map
        # 深拷贝：同一想定可反复开局，互不污染
        self.units: list[Unit] = [copy.deepcopy(u) for u in scenario.units]
        self.player_side = player_side
        self.ai_side = player_side.foe
        self.turn_count = 1
        self.turn_limit = scenario.turn_limit
        self.phase = "player"          # player / ai / over
        self.over = False
        self.winner: Side | None = None
        self.win_reason = ""

        self.missiles: list[Missile] = []
        self.log: list[LogEntry] = []
        self.fx: list[Fx] = []
        self.detection: dict[Side, dict[int, Detection]] = {Side.BLUE: {}, Side.RED: {}}
        self.sat_recon: dict[Side, set[int]] = {Side.BLUE: set(), Side.RED: set()}
        self.sat_track: dict[int, Detection] = {}
        self.vp: dict[Side, int] = {Side.BLUE: 0, Side.RED: 0}
        self.gps: dict[Side, float] = {Side.BLUE: 1.0, Side.RED: 1.0}
        self.casualties: dict[Side, int] = {Side.BLUE: 0, Side.RED: 0}

        self._ai_queue: list = []
        self._ai_planner = None
        self.selected_uid: int | None = None
        self.selected_weapon: str | None = None
        self.hover_tile: tuple[int, int] | None = None
        self.salvo_setting: int = 1        # 玩家选择的齐射发数
        self.scenario_path: str = ""       # 存档回放所需的想定路径
        self.armory_path: str = ""

        self._init_gps()
        self.refresh_detection()
        self.log_message(None, f"作战开始：{self.map.name}｜{self.map.width}×{self.map.height} 格"
                               f"（1 格 = {self.map.tile_km:.0f} km）｜限 {self.turn_limit} 回合", "system")

    # -- 基础查询 ---------------------------------------------------------
    @property
    def objective_tiles(self) -> list[Tile]:
        return [t for t in self.map.iter_tiles() if t.objective]

    def unit_at(self, x: int, y: int) -> Unit | None:
        for u in self.units:
            if u.alive and u.x == x and u.y == y:
                return u
        return None

    def units_at(self, x: int, y: int) -> list[Unit]:
        return [u for u in self.units if u.alive and u.x == x and u.y == y]

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

    # -- 探测与态势 -------------------------------------------------------
    def refresh_detection(self) -> None:
        rules.build_detection_map(self)

    def _init_gps(self) -> None:
        for side in (Side.BLUE, Side.RED):
            self.gps[side] = self._compute_gps(side)

    def _compute_gps(self, side: Side) -> float:
        loss = 0.0
        for u in self.enemies_of(side):
            if u.template.jam_strength > 0 and u.jam_on:
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
                mine_dmg += rules.jitter(t.fort * 4.0)
                mines += 1

        start = (unit.x, unit.y)
        unit.x, unit.y = path[-1]
        unit.mp -= cost
        unit.moved = True
        unit.entrench = 0
        self.add_fx("move", start, (unit.x, unit.y), ttl=0.6, color=(120, 210, 255))
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
            self.log_message(unit.side, f"{unit.name} 清除了 ({t.x},{t.y}) 的敌方工事", "combat")
        if t.facility is not Facility.NONE and t.owner is not unit.side:
            old = t.owner
            t.owner = unit.side
            self.add_fx("ping", (t.x, t.y), None, ttl=1.2, color=(140, 255, 190))
            self.log_message(unit.side,
                             f"{unit.name} 夺取 {t.facility.label} ({t.x},{t.y})，原属{old.label}", "combat")
        if t.objective and t.owner is not unit.side:
            t.owner = unit.side
            self.add_fx("ping", (unit.x, unit.y), None, ttl=1.2, color=(255, 230, 140))
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
        self.log_message(unit.side, f"{unit.name} 电子干扰{'启动' if unit.jam_on else '停止'}"
                         f"（半径 {unit.template.jam_radius:.0f} 格，强度 {unit.template.jam_strength * 100:.0f}%）", "ew")
        if unit.jam_on:
            self.add_fx("ping", (unit.x, unit.y), None, ttl=1.0, color=(200, 150, 255))

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

        prof = rules.shot_profile(self, attacker, target, weapon)
        if not prof.ok:
            return FireResult(False, prof.reason)

        # 齐射发数：受武器上限、备弹与玩家设置共同约束
        ammo_left = attacker.ammo.get(weapon.id, 0)
        n = max(1, min(int(salvo or self.salvo_setting), weapon.max_salvo, ammo_left))
        attacker.ammo[weapon.id] = ammo_left - n
        attacker.consume_shot()
        attacker.revealed_until = self.turn_count + 1

        flight = max(1, math.ceil(prof.distance / max(0.5, weapon.speed)))
        if flight <= 1:
            return self._impact(attacker, target, weapon, salvo=n, prof=prof)

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
        self.log_message(attacker.side,
                         f"{attacker.name} 发射 {weapon.name} × {n} → {target.name}"
                         f"（{prof.distance:.1f} 格，预计 {missile.eta} 回合后抵达）", "combat")
        self.refresh_detection()
        return FireResult(True, f"发射 {weapon.name}×{n}，飞行 {missile.eta} 回合",
                          in_flight=True, flight=missile.eta)

    def resolve_missiles(self) -> None:
        """推进在飞弹药；抵达者进行末端交战。"""
        remaining: list[Missile] = []
        for m in self.missiles:
            attacker = self.unit_by_uid(m.attacker_uid)
            target = self.unit_by_uid(m.target_uid)

            # 弹道导弹的中段窗口：只有专用反导系统（SM-3/THAAD/77N6）能够交战
            if m.weapon.ballistic and not m.midcourse_done:
                m.midcourse_done = True
                mid = ((m.origin[0] + m.target_pos[0]) // 2,
                       (m.origin[1] + m.target_pos[1]) // 2)
                defended = m.attacker_side.foe
                destroyed = False
                for d, w, pk in rules.midcourse_interceptors(self, mid, defended):
                    d.ammo[w.id] = max(0, d.ammo.get(w.id, 0) - 1)
                    if rules.roll(pk):
                        self.add_fx("intercept", mid, None, ttl=1.2, color=(150, 220, 255),
                                    text="中段拦截")
                        self.log_message(defended,
                                         f"{d.name} 发射 {w.name} 在中段成功拦截 {m.label}"
                                         f"（单发 Pk {pk * 100:.0f}%）", "combat")
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
        leakers, events = rules.resolve_salvo_interception(
            self, (target.x, target.y), target.side, weapon, salvo, self_unit=target)
        if events:
            self.add_fx("intercept", (target.x, target.y), None, ttl=0.9, color=(120, 255, 200))
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
        salvo = leakers

        # 逐发命中
        hits = 0
        total_damage = 0.0
        for _ in range(max(1, salvo)):
            if rules.roll(prof.pk):
                dmg = rules.jitter(prof.damage)
                total_damage += dmg
                hits += 1

        self.add_fx("tracer", (attacker.x, attacker.y), (target.x, target.y),
                    ttl=0.55, color=(255, 220, 120))
        if hits == 0:
            self.add_fx("ping", (target.x, target.y), None, ttl=0.6, color=(150, 150, 170))
            self.log_message(attacker.side,
                             f"{attacker.name} → {target.name}｜{weapon.name} 未命中（Pk {prof.pk * 100:.0f}%）", "combat")
            return FireResult(True, "未命中")

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

        # 命中同时破坏敌方工事（面杀伤武器效率更高）
        if weapon.anti_fort > 0:
            tgt_tile = self.map.at(target.x, target.y)
            if tgt_tile is not None and tgt_tile.fort > 0 \
                    and tgt_tile.fort_owner is not attacker.side:
                before = tgt_tile.fort
                tgt_tile.fort = max(0, tgt_tile.fort - weapon.anti_fort)
                if tgt_tile.fort == 0:
                    tgt_tile.fort_progress = 0
                if tgt_tile.fort < before:
                    self.log_message(attacker.side,
                                     f"（{target.x},{target.y}）敌方工事被削弱至 Lv{tgt_tile.fort}",
                                     "combat")

        killed = target.destroyed
        if killed:
            self.casualties[target.side] += 1
            self.add_fx("explosion", (target.x, target.y), None, ttl=1.6, color=(255, 90, 60))
            self.log_message(attacker.side,
                             f"{attacker.name} 以 {weapon.name} 击毁 {target.name}"
                             f"（{hits} 发命中 / 伤害 {total_damage:.0f}）", "combat")
        else:
            self.log_message(attacker.side,
                             f"{attacker.name} → {target.name}｜{weapon.name} 命中 {hits} 发，"
                             f"伤害 {total_damage:.0f}（剩余 {target.hp:.0f}/{target.template.hp:.0f}）", "combat")

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
        n = max(1, min(int(salvo or self.salvo_setting), weapon.max_salvo, ammo_left))
        attacker.ammo[weapon.id] = ammo_left - n
        attacker.consume_shot()
        attacker.revealed_until = self.turn_count + 1
        name = f"{tile.facility.label}({tile.x},{tile.y})"

        self.add_fx("tracer", (attacker.x, attacker.y), (tile.x, tile.y),
                    ttl=0.6, color=(255, 200, 120))

        n_before = n
        if tile.owner in (Side.BLUE, Side.RED):
            leakers, events = rules.resolve_salvo_interception(
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
        for _ in range(n):
            if rules.roll(prof.pk):
                dmg = rules.jitter(prof.damage)
                tile.facility_hp = max(0.0, tile.facility_hp - dmg)
                hits += 1
                total += dmg

        if hits == 0:
            self.add_fx("ping", (tile.x, tile.y), None, ttl=0.7, color=(150, 150, 170))
            self.log_message(attacker.side,
                             f"{attacker.name} 对 {name} 的打击未奏效（Pk {prof.pk * 100:.0f}%）", "combat")
            return FireResult(True, "未命中")

        if weapon.anti_fort > 0 and tile.fort > 0 and tile.fort_owner is not attacker.side:
            tile.fort = max(0, tile.fort - weapon.anti_fort)
            if tile.fort == 0:
                tile.fort_progress = 0
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
        self.add_fx("ping", pos, None, ttl=1.0, color=(200, 205, 215), text="烟幕")
        self.log_message(unit.side,
                         f"{unit.name} 对 ({pos[0]},{pos[1]}) 施放烟幕"
                         f"（遮蔽 {len(tiles)} 格，持续 3 回合）", "combat")
        self.refresh_detection()
        return True

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

    # -- 回合推进 ---------------------------------------------------------
    def end_player_turn(self) -> None:
        if self.phase != "player" or self.over:
            return
        self.resolve_missiles()
        self.refresh_detection()
        if self._check_end():
            return
        self.phase = "ai"
        self.log_message(None, f"—— 第 {self.turn_count} 回合 · 红方行动 ——", "system")

    def finish_ai_turn(self) -> None:
        if self.phase != "ai":
            return
        self.resolve_missiles()
        self.refresh_detection()
        self._advance_turn()

    # -- 后勤与基地勤务 ---------------------------------------------------
    FUEL_DRAIN = 0.15          # 空中平台每回合燃油消耗

    SAM_RELOAD = 2        # 防空弹每回合自动补充数量
    CIWS_RELOAD = 1       # 近防炮每回合自动补充数量

    def _reload_interceptors(self) -> None:
        """防空系统补充拦截弹：垂发单元装填极快，不依赖返回基地。"""
        for u in self.units:
            if not u.alive or u.is_decoy:
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

    def _tick_logistics(self) -> None:
        """回合推进时的油料、弹药与维修结算。

        航空兵必须返回母基地（机场/航母）才能加油挂弹，否则会因燃油耗尽坠毁；
        地面部队在后勤枢纽补充弹药并修复战损；舰艇在港口补充。
        """
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
                # 战损修复
                if u.hp < u.template.hp:
                    heal = u.template.hp * (0.12 if u.domain is Domain.AIR else 0.07)
                    u.hp = min(u.template.hp, u.hp + heal)
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

        tpl = random.choice([t for t in pool if t.cost >= 100] or pool)
        d = Unit(template=tpl, side=unit.side, x=pos[0], y=pos[1])
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
    def build_fort(self, unit: Unit) -> bool:
        """构筑 / 加固野战工事：消耗 1.0 机动点，工事等级提升 1 级（上限 3）。"""
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
            tile.fort = 0
            tile.fort_progress = 0
        gain = max(1, unit.template.fort_power)
        before = tile.fort
        tile.fort = min(3, tile.fort + gain)
        tile.fort_owner = unit.side
        unit.mp = max(0.0, unit.mp - 1.0)
        unit.moved = True          # 构筑工事期间无法同时保持隐蔽机动
        self.refresh_detection()
        self.add_fx("ping", (tile.x, tile.y), None, ttl=0.9, color=(200, 220, 140))
        if before == 0:
            self.log_message(unit.side,
                             f"{unit.name} 在 ({tile.x},{tile.y}) 开始构筑野战工事（Lv{tile.fort}）", "move")
        else:
            self.log_message(unit.side,
                             f"{unit.name} 加固 ({tile.x},{tile.y}) 工事至 Lv{tile.fort}", "move")
        return True

    def _tick_fortifications(self) -> None:
        """工事维护：己方工兵/步兵驻守可修复，无人维护则逐步荒废。"""
        for t in self.map.iter_tiles():
            if t.fort <= 0:
                continue
            crew = [u for u in self.units_at(t.x, t.y)
                    if not u.is_decoy and u.side is t.fort_owner and u.template.can_fortify]
            if crew and not any(u.moved for u in crew):
                t.fort_progress += 1
                if t.fort_progress >= 2 and t.fort < 3:
                    t.fort += 1
                    t.fort_progress = 0
            elif t.fort_owner is not Side.NEUTRAL:
                # 无人维护：每 3 回合衰减 1 级
                t.fort_progress += 1
                if t.fort_progress >= 3:
                    t.fort = max(0, t.fort - 1)
                    t.fort_progress = 0

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
        self.turn_count += 1
        for u in self.units:
            if u.alive:
                u.reset_turn(self.turn_count)
        for t in self.map.iter_tiles():
            if t.smoke > 0:
                t.smoke -= 1

        self._tick_logistics()
        self._reload_interceptors()

        self._tick_satellites()

        self.gps = {s: self._compute_gps(s) for s in (Side.BLUE, Side.RED)}

        # 目标点计分
        for t in self.objective_tiles:
            # 同格可能停驻多支部队（机场停机坪），取任一具备占领能力的单位
            occ = [u for u in self.units_at(t.x, t.y)
                   if u.template.can_capture and not u.is_decoy]
            if occ:
                t.owner = occ[0].side
            if t.owner in (Side.BLUE, Side.RED):
                self.vp[t.owner] += t.victory_points

        self.refresh_detection()
        if self.over:
            return                  # 后勤/全歼可能导致对局在中途结束，不要覆盖 over 相位
        self.phase = "player"
        if self._check_end():
            return
        self.log_message(None, f"—— 第 {self.turn_count} 回合 · 蓝方行动 ——", "system")

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
        self.log_message(self.ai_side, "红方开始行动", "system")

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


__all__ = ["GameState", "LogEntry", "Fx", "FireResult"]

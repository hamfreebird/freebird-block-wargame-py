"""对手 AI：基于威胁评估与目标价值启发式的作战规划。

设计原则：
* AI 与玩家受同一套探测规则约束（fog of war 对等）；
* 优先「先敌发现、防区外打击」，避免把高价值平台送入敌方防空包线；
* 每一步都基于最新状态重新规划，因此 AI 会对玩家的机动与开火作出反应。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import rules
from . import ai_tactics as tactics
from .enums import Detection, Domain, Facility, SensorKind, Side, UnitClass, WeaponKind
from .env import environment_for
from .model import Unit, Weapon

FACILITY_VALUE = {
    Facility.AIRBASE: 9.0,
    Facility.PORT: 6.0,
    Facility.DEPOT: 5.0,
    Facility.RADAR_SITE: 4.0,
    Facility.HQ: 4.0,
    Facility.SAM_SITE: 3.0,
    Facility.CITY: 2.0,
}

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState


@dataclass
class AIAction:
    kind: str                       # move / fire / radar / jam / strike
    unit_uid: int
    path: list[tuple[int, int]] | None = None
    target_uid: int | None = None
    weapon_id: str | None = None
    tile: tuple[int, int] | None = None
    decoy_kind: str = ""
    note: str = ""
    #: 这一发的**预期毁伤**（命中率 × 突防率 × 单发毁伤 × 发数）。
    #: 用来记账：同一回合内已经往某目标身上砸了多少火力，避免过量射击。
    expected: float = 0.0


class AIPlanner:
    def __init__(self, state: "GameState", side: Side, deep: bool = True):
        """`deep=False` 退回 v2.2 的行为，作为 A/B 对比的基线。

        保留它的理由不是兼容，而是**可证伪**：新战术到底有没有用，
        得能拿同一张想定、同一个种子跑两遍比出来（tools/test_ai_deep.py）。
        """
        self.side = side
        self.deep = deep
        order = sorted(
            [u for u in state.units_of(side)],
            key=lambda u: (-u.template.cost, u.domain is Domain.SPACE),
        )
        self.queue: list[int] = [u.uid for u in order]
        self.ew_done: set[int] = set()
        self.move_done: set[int] = set()
        self.actions_taken: dict[int, int] = {}
        # 作战决心跨回合保留（deep 模式）：规划器每回合重建，但「这支部队去打哪」
        # 不该跟着重掷一次——否则每次都是新决定，「转移优先级」无从谈起。
        if deep:
            self.assign = state.ai_assign.setdefault(side, {})
            self.assign_turn = state.ai_assign_turn.setdefault(side, {})
        else:
            self.assign = {}
            self.assign_turn = {}
        #: 本回合各目标已分配的预期毁伤（集火台账）
        self.fire_damage: dict[int, float] = {}
        #: 本回合因何改派——写进战报，方便回看 AI 的判断
        self.switch_note: dict[int, str] = {}
        self.feint: tactics.FeintPlan | None = None
        self.feint_note_done = False
        #: 已经问过「要不要改派」的单位（每回合只问一次，见 _should_switch）
        self._switch_checked: set[int] = set()
        self.decoy_done: set[int] = set()
        self.returning: set[int] = set()
        self.smoke_done: set[int] = set()
        self.cmd_done: set[int] = set()

    def pending(self) -> bool:
        return bool(self.queue)

    # ------------------------------------------------------------------ #
    def step(self, state: "GameState") -> AIAction | None:
        while self.queue:
            uid = self.queue.pop(0)
            unit = state.unit_by_uid(uid)
            if unit is None or not unit.alive:
                continue
            if self.actions_taken.get(uid, 0) > 6:
                continue
            act = self._decide(state, unit)
            if act is None:
                continue
            self.actions_taken[uid] = self.actions_taken.get(uid, 0) + 1
            if act.kind in ("move", "fire", "strike", "radar", "jam", "decoy", "smoke"):
                # 机动后重新评估（移动后射击）；多发射平台可连续开火；
                # 电子战/诱饵只是开关动作，之后仍应继续机动或射击
                self.queue.append(uid)
            return act
        return None

    # ------------------------------------------------------------------ #
    def _decide(self, state: "GameState", unit: Unit) -> AIAction | None:
        if unit.domain is Domain.SPACE:
            return None

        # 航空兵任务剖面：返场转场省油、无敌情时制空巡逻、其余保持打击
        if unit.domain is Domain.AIR and not unit.is_decoy:
            if unit.uid in self.returning:
                if unit.mission != "ferry":
                    state.set_mission(unit, "ferry")
            elif unit.mission == "ferry":
                state.set_mission(unit, "strike")
            elif unit.mission == "strike" and not any(
                    w.kind is not WeaponKind.AAM for w in unit.template.weapons):
                # 纯空优挂载且暂无交战：巡逻提高态势与空战命中
                if not any(state.known_level(self.side, e).level >= Detection.TRACKED.level
                           for e in state.enemies_of(self.side) if e.domain is Domain.AIR):
                    state.set_mission(unit, "cap")

        # 航空兵：在基地且油未满时停留补给，不再出击
        if unit.domain is Domain.AIR and unit.fuel < 0.98:
            if rules.service_tile(state, unit) is not None:
                return None

        ew = self._ew_decision(state, unit)
        if ew is not None:
            return ew

        # 电子战单位投放诱饵，制造假目标牵制对方火力
        if unit.decoy_left > 0 and unit.uid not in self.decoy_done:
            spot = self._decoy_spot(state, unit)
            if spot is not None:
                self.decoy_done.add(unit.uid)
                kind = self._decoy_kind(state, unit)
                return AIAction("decoy", unit.uid, tile=spot, decoy_kind=kind,
                                note=f"{unit.name} 投放{rules.DECOY_LABELS.get(kind, kind)}")

        desperate = (unit.domain is Domain.AIR and unit.fuel < 0.40
                     and rules.service_tile(state, unit) is None)
        if unit.shots_left > 0 and unit.has_ammo() and not desperate:
            shot = self._best_shot(state, unit)
            if shot is not None:
                target, weapon, value = shot
                exp = self._fire_expected(state, unit, target, weapon)
                if self.deep:
                    self.fire_damage[target.uid] = \
                        self.fire_damage.get(target.uid, 0.0) + exp
                return AIAction(
                    "fire", unit.uid, target_uid=target.uid, weapon_id=weapon.id,
                    note=f"{unit.name} 向 {target.name} 发射 {weapon.name}",
                    expected=exp,
                )
            strike = self._best_facility_target(state, unit)
            if strike is not None:
                tile, weapon = strike
                return AIAction(
                    "strike", unit.uid, weapon_id=weapon.id, tile=(tile.x, tile.y),
                    note=f"{unit.name} 打击 {tile.facility.label}({tile.x},{tile.y})",
                )

        # 炮兵施放烟幕：掩护受威胁的己方要点（每单位每回合至多一次）
        if unit.uid not in self.smoke_done:
            act = self._smoke_action(state, unit)
            if act is not None:
                self.smoke_done.add(unit.uid)
                return act

        # 火力准备：间瞄火力对已识别但打不着的目标登记射界
        prep = self._fire_mission_action(state, unit)
        if prep is not None:
            return prep

        # 指挥单位：向下级下达指令
        cmd = self._command_action(state, unit)
        if cmd is not None:
            return cmd

        # 没有可打的目标时，在防御阵位上构筑工事
        if unit.template.can_fortify and unit.uid not in self.move_done \
                and self._should_fortify(state, unit):
            self.move_done.add(unit.uid)
            return AIAction("fortify", unit.uid, note=f"{unit.name} 构筑工事")

        if unit.uid not in self.move_done and unit.mp > 0.5:
            path = self._best_move(state, unit)
            if path is not None and len(path) > 1:
                self.move_done.add(unit.uid)
                return AIAction(
                    "move", unit.uid, path=path,
                    note=f"{unit.name} 机动至 ({path[-1][0]},{path[-1][1]})",
                )
        return None

    # ------------------------------------------------------------------ #
    def _ew_decision(self, state: "GameState", unit: Unit) -> AIAction | None:
        if unit.uid in self.ew_done:
            return None
        self.ew_done.add(unit.uid)

        # 反辐射威胁 → 关雷达
        if unit.radar_on and any(s.kind.value == "radar" for s in unit.template.sensors):
            for enemy in state.enemies_of(self.side):
                if state.known_level(self.side, enemy).level < Detection.CLASSIFIED.level:
                    continue
                for w in enemy.weapons:
                    if w.kind is WeaponKind.ARM:
                        if rules.dist((enemy.x, enemy.y), (unit.x, unit.y)) <= w.range_max:
                            if state.rng.random() < 0.75:
                                return AIAction("radar", unit.uid,
                                                note=f"{unit.name} 察觉反辐射威胁，雷达关机")
        # 环境适应：雨雾/夜间光电失效 → 主动雷达开机补足态势
        env_act = self._environment_decision(state, unit)
        if env_act is not None:
            return env_act
        # 干扰机：附近有敌情则开机
        if unit.template.jam_strength > 0 and not unit.jam_on:
            for enemy in state.enemies_of(self.side):
                if state.known_level(self.side, enemy) is Detection.NONE:
                    continue
                if rules.dist((enemy.x, enemy.y), (unit.x, unit.y)) <= unit.template.jam_radius * 1.3:
                    # 按敌方构成选择最有价值的干扰频段
                    band = self._best_jam_band(state, unit, enemy)
                    note = f"{unit.name} 启动电子干扰"
                    if band:
                        names = {"radar": "雷达", "comm": "通信", "nav": "导航"}
                        note += f"（重点压制{names[band]}频段）"
                    return AIAction("jam", unit.uid, note=note)
        return None

    def _best_jam_band(self, state: "GameState", unit: Unit, enemy: Unit) -> str:
        """按目标特征选择最有效的干扰频段。

        * 目标依赖雷达 → 压雷达（直接削掉对方的探测与照射能力）
        * 目标依赖数据链 → 压通信（切断信火一体）
        * 目标依赖卫星导航 → 压导航（使精确制导弹药退化）
        """
        bands = set(unit.template.jam_bands)
        if not bands:
            return ""
        radar = any(sc.kind.value == "radar" for sc in enemy.template.sensors)
        if "radar" in bands and radar:
            return "radar"
        if "comm" in bands and enemy.template.datalink:
            return "comm"
        if "nav" in bands and any(w.guidance.value == "gps_ins"
                                  for w in enemy.template.weapons):
            return "nav"
        # 退而求其次：优先雷达频段（对战果影响最直接）
        for b in ("radar", "comm", "nav"):
            if b in bands:
                return b
        return ""

    def _environment_decision(self, state: "GameState", unit: Unit) -> AIAction | None:
        """按天候与时段调整侦察手段。

        * 雨雾或夜间：光学探测几乎失效，主动雷达成为唯一可靠的远程手段，
          因此雷达应当开机（哪怕因此暴露）。
        * 晴朗白天：光学与雷达都有效，雷达可关机降低暴露。
        """
        has_radar = any(s.kind.value == "radar" for s in unit.template.sensors)
        if not has_radar:
            return None
        env = environment_for(state)
        optics_degraded = (env.factor(SensorKind.OPTICAL) < 0.7
                           or env.factor(SensorKind.RADAR) < 0.9)
        if optics_degraded and not unit.radar_on:
            return AIAction("radar", unit.uid,
                            note=f"{unit.name} 因{env.label}天候开机雷达")
        if not optics_degraded and unit.radar_on:
            # 白天晴天：关雷达可显著降低被反辐射导弹锁定的风险
            if state.rng.random() < 0.35:
                return AIAction("radar", unit.uid,
                                note=f"{unit.name} 天气良好，关闭雷达降低暴露")
        return None

    # ------------------------------------------------------------------ #
    def _fire_expected(self, state: "GameState", unit: Unit, target: Unit,
                       weapon: Weapon) -> float:
        """这一发的预期毁伤，用于集火台账与「别再打死的目标」判断。"""
        p = rules.shot_profile(state, unit, target, weapon)
        if not p.ok:
            return 0.0
        n = max(1, min(weapon.salvo, rules.salvo_limit(unit, weapon),
                       unit.ammo.get(weapon.id, 0)))
        return p.pk * (1.0 - p.intercept_pk) * p.damage * n

    def _best_shot(self, state: "GameState", unit: Unit):
        best = None
        best_val = 3.0
        # 集中火力指令：优先打击指定目标
        ordered = getattr(unit, "order", None)
        focus = None
        if ordered is not None and ordered.kind == "focus_fire" and ordered.target_uid:
            focus = state.unit_by_uid(ordered.target_uid)
            if focus is not None and (not focus.alive or focus.side is unit.side):
                focus = None

        # 集火台账：本回合已经砸在这个目标上的预期毁伤。
        # 用途有两个，而且方向相反——
        #   1) 已经够打死它了就别再往里填弹药（省下来打下一个也是火力）；
        #   2) 差一口气没死，就更该补这一刀（半死的目标跑掉等于白打）。
        dealt = self.fire_damage if self.deep else {}

        for enemy in state.enemies_of(self.side):
            if not enemy.alive:
                continue
            if state.known_level(self.side, enemy).level < Detection.TRACKED.level:
                continue
            if enemy.domain is Domain.SPACE and not any(w.asat for w in unit.usable_weapons()):
                continue
            already = dealt.get(enemy.uid, 0.0)
            if already >= enemy.hp:
                continue          # 已经被判定打掉了，别浪费弹药
            for w in unit.usable_weapons():
                p = rules.shot_profile(state, unit, enemy, w)
                if not p.ok:
                    continue
                val = p.pk * (1.0 - p.intercept_pk) * p.damage
                val += enemy.template.cost / 90.0
                val += (1.0 - enemy.hp_ratio) * 2.5
                # 斩首加成：优先打「打掉之后能让我们多拿分」的目标——
                # 敌方据点的守卫、能压制我方的防空。
                # 否则 AI 会把弹药浪费在无关紧要的目标上。
                val += tactics.strike_priority(state, self.side, enemy) * 0.9
                # 集中火力：指定目标获得显著加成
                if focus is not None and enemy is focus:
                    val *= 2.2
                if already > 0:
                    val += self._follow_up_bonus(state, unit, enemy, w, already)
                if val > best_val:
                    best_val, best = val, (enemy, w, val)
        return best

    def _follow_up_bonus(self, state: "GameState", unit: Unit, target: Unit,
                         weapon: Weapon, already: float) -> float:
        """跨单位协同的第二步：接力的加成。

        第一支打响之后，目标已经暴露、被压制、在躲；后续射手拿到的
        是「已知诸元 + 一个正在找掩体的目标」。所以补刀比开新战场划算——
        这正是「集火」在数值上唯一说得通的理由。
        """
        need = target.hp - already
        if need <= 0:
            return 0.0
        exp = self._fire_expected(state, unit, target, weapon)
        if exp <= 0:
            return 0.0
        if exp >= need:
            # 差的那一口气这一发就能补上：结束一个单位，远比打伤十个有用
            return 2.6 + target.template.cost / 260.0
        return 0.45

    def _best_facility_target(self, state: "GameState", unit: Unit):
        """选择值得打击的敌方固定设施（优先机场/港口）。"""
        best = None
        best_val = 4.5
        for t in state.map.iter_tiles():
            if t.facility is Facility.NONE or t.owner is self.side:
                continue
            if t.facility_hp <= 0:
                continue
            for w in unit.usable_weapons():
                if not w.vs_facility:
                    continue
                p = rules.facility_strike_profile(state, unit, t, w)
                if not p.ok:
                    continue
                val = p.pk * (1.0 - p.intercept_pk) * p.damage / 26.0
                val += FACILITY_VALUE.get(t.facility, 2.0) * 0.55
                val += (1.0 - t.facility_hp_ratio) * 2.0
                if val > best_val:
                    best_val, best = val, (t, w)
        return best

    # ------------------------------------------------------------------ #
    def _decoy_spot(self, state: "GameState", unit: Unit) -> tuple[int, int] | None:
        """在己方阵地朝敌方向前 2-3 格处找一个空位投放诱饵（按作战域校验地形）。"""
        foes = [e for e in state.enemies_of(self.side)
                if state.known_level(self.side, e) is not Detection.NONE
                and e.domain is not Domain.SPACE]
        if not foes:
            return None
        f = foes[0]
        dx, dy = f.x - unit.x, f.y - unit.y
        n = math.hypot(dx, dy) or 1.0
        for step in (2.5, 3.5, 1.8, 5.0):
            gx = int(round(unit.x + dx / n * step))
            gy = int(round(unit.y + dy / n * step))
            if not state.map.in_bounds(gx, gy):
                continue
            t = state.map.at(gx, gy)
            if t is None:
                continue
            water = t.terrain.value == "water"
            if unit.domain in (Domain.SEA, Domain.SUB) and not water:
                continue
            if unit.domain is Domain.LAND and water:
                continue
            if unit.domain is Domain.AIR and step > 8.0:
                continue
            if state.unit_at(gx, gy) is not None:
                continue
            return (gx, gy)
        return None

    def _smoke_action(self, state: "GameState", unit: Unit) -> AIAction | None:
        """火炮/火箭炮对受威胁的己方目标点施放烟幕，遮蔽敌方光电与直瞄火力。"""
        candidates = [w for w in unit.usable_weapons()
                      if w.kind in (WeaponKind.ARTILLERY, WeaponKind.MLRS)]
        if not candidates or unit.shots_left <= 0:
            return None
        # 找一个正在被敌方逼近的己方目标点
        for t in state.objective_tiles:
            if t.owner is not self.side:
                continue
            d = rules.dist((unit.x, unit.y), (t.x, t.y))
            if d > candidates[0].range_max or d < candidates[0].range_min:
                continue
            threat = any(e.domain in (Domain.LAND, Domain.INFRA)
                         and rules.dist((e.x, e.y), (t.x, t.y)) <= 4.0
                         for e in state.enemies_of(self.side))
            if not threat:
                continue
            # 只在有直瞄威胁时才值得放烟（敌人已接近到 4 格内）
            if state.map.at(t.x, t.y).smoke > 0:
                continue
            return AIAction("smoke", unit.uid, tile=(t.x, t.y), weapon_id=candidates[0].id,
                            note=f"{unit.name} 对 {t.objective_name or '目标点'} 施放烟幕")
        return None

    def _decoy_kind(self, state: "GameState", unit: Unit) -> str:
        """按敌方传感器构成挑诱饵频段：雷达多放雷达诱饵，热像多放红外诱饵。"""
        radar = ir = optical = 0
        for e in state.enemies_of(self.side):
            if state.known_level(self.side, e) is Detection.NONE:
                continue
            for sn in e.template.sensors:
                if sn.kind.value == "radar":
                    radar += 1
                elif sn.kind.value == "ir":
                    ir += 1
                elif sn.kind.value == "optical":
                    optical += 1
        if unit.domain is Domain.AIR and ir >= radar:
            return "ir"          # 空射诱饵主要骗热像与目视
        if radar >= max(ir, optical):
            return "radar"
        if ir >= optical:
            return "ir"
        return "visual"

    def _fire_mission_action(self, state: "GameState", unit: Unit) -> AIAction | None:
        """登记射界：射界已准备完成时优先用准备加成射击。

        * 射界已就绪 → 尝试对射界内目标开火（准备加成）
        * 尚无射界 → 对高价值、已识别的敌方目标登记射界
        """
        if not unit.can_prepare_fire or unit.is_decoy:
            return None
        fm = unit.fire_mission
        if fm is not None and fm.ready:
            # 射界就绪：对射界内已跟踪目标开火
            for e in state.enemies_of(self.side):
                if not e.alive:
                    continue
                if rules.dist((fm.x, fm.y), (e.x, e.y)) > state.FIRE_PREPARE_RADIUS + rules.RANGE_TOLERANCE:
                    continue
                if state.known_level(self.side, e).level < Detection.TRACKED.level:
                    continue
                for w in unit.usable_weapons():
                    p = rules.shot_profile(state, unit, e, w)
                    if p.ok:
                        return AIAction("fire", unit.uid, target_uid=e.uid, weapon_id=w.id,
                                        note=f"{unit.name} 以射界加成射击 {e.name}")
            return None
        if fm is not None and fm.active:
            return None      # 射界准备中
        if unit.mp < state.FIRE_PREPARE_MP:
            return None
        # 挑一个高价值、已识别、且在射程内的敌方目标登记射界
        best = None
        best_val = 0.0
        for e in state.enemies_of(self.side):
            if not e.alive:
                continue
            lvl = state.known_level(self.side, e)
            if lvl.level < Detection.CLASSIFIED.level:
                continue
            if not any(w.is_indirect for w in unit.usable_weapons()
                       if rules.dist((unit.x, unit.y), (e.x, e.y)) <= w.range_max):
                continue
            val = e.template.cost / 100.0 + (1.0 - e.hp_ratio) * 2.0
            if val > best_val:
                best_val, best = val, e
        if best is None:
            return None
        return AIAction("prepare_fire", unit.uid, tile=(best.x, best.y),
                        note=f"{unit.name} 对 ({best.x},{best.y}) 登记射界")

    def _command_action(self, state: "GameState", unit: Unit) -> AIAction | None:
        """指挥单位下达指令。

        决策逻辑：
        * 出现高价值目标（防空阵地 / 雷达 / 指挥所）→ 集中火力；
        * 多个下级同时具备攻击同一目标的条件 → 集中火力；
        * 己方要点受威胁 → 固守。
        """
        if unit.template.command_radius <= 0:
            return None
        if unit.uid in self.cmd_done:
            return None
        subs = state.subordinates_of(unit)
        if not subs:
            self.cmd_done.add(unit.uid)
            return None
        self.cmd_done.add(unit.uid)

        # -- 集中火力：挑选高价值目标 --
        best = None
        best_val = 0.0
        for e in state.enemies_of(self.side):
            if not e.alive:
                continue
            if state.known_level(self.side, e).level < Detection.TRACKED.level:
                continue
            d = rules.dist((unit.x, unit.y), (e.x, e.y))
            if d > unit.template.command_range + rules.RANGE_TOLERANCE:
                continue
            # 价值：单位造价 + 类别权重（防空与雷达值得优先打掉）
            val = e.template.cost / 100.0
            if e.template.unit_class.value in ("sam", "aaa"):
                val += 4.0
            elif e.template.unit_class.value == "radar":
                val += 3.0
            elif e.template.unit_class.value == "hq":
                val += 3.5
            val += (1.0 - e.hp_ratio) * 1.5
            if val > best_val:
                best_val, best = val, e
        if best is None:
            return None
        ok, _ = state.issue_order(unit, "focus_fire", target=best)
        if ok:
            return AIAction("command", unit.uid, target_uid=best.uid,
                            note=f"{unit.name} 下达集中火力：{best.name}")
        return None

    def _should_fortify(self, state: "GameState", unit: Unit) -> bool:
        """防御态势：站在己方目标点上、工事未满、附近无敌情时构筑工事。"""
        tile = state.map.at(unit.x, unit.y)
        if tile is None or tile.terrain.value == "water":
            return False
        engineer = unit.template.fort_power >= 2
        if not engineer and not (tile.objective or tile.facility.value in ("depot", "hq", "sam_site")):
            return False
        if engineer and tile.fort <= 0 and not tile.objective:
            # 工兵只在能形成阻击线的地形上构筑（丘陵/城区/渡口附近）
            if tile.terrain.value not in ("hills", "urban", "forest", "mountain"):
                return False
        if tile.fort >= 3 and tile.fort_owner is self.side:
            return False
        if unit.mp < 1.0:
            return False
        for e in state.enemies_of(self.side):
            if state.known_level(self.side, e) is Detection.NONE:
                continue
            if e.domain in (Domain.AIR, Domain.SPACE):
                continue
            if rules.dist((unit.x, unit.y), (e.x, e.y)) <= 5.0:
                return False
        return True

    def _threat_penalty(self, state: "GameState", unit: Unit, pos: tuple[int, int]) -> float:
        hp = max(20.0, unit.template.hp)
        pen = 0.0
        for e in state.enemies_of(self.side):
            if state.known_level(self.side, e) is Detection.NONE:
                continue
            if e.domain is Domain.SPACE:
                continue
            d = rules.dist(pos, (e.x, e.y))
            for w in e.usable_weapons():
                if unit.domain not in w.targets:
                    continue
                if d <= w.range_max:
                    pen += w.pk * w.damage / hp
        return min(2.6, pen * 1.4)

    def _best_move(self, state: "GameState", unit: Unit):
        reach = rules.flood_reachable(state, unit)
        if len(reach) <= 1:
            return None
        here = (unit.x, unit.y)
        ordered = getattr(unit, "order", None)
        # 交叉火力阵位：己方对附近敌目标已经占了哪几个方位（每回合一次）
        xmap = tactics.crossfire_map(state, self.side, unit) if self.deep else {}

        # 撤退：打不下去时脱离接触。
        # 「活着回去」比「拼掉」更有价值——尤其是有占领能力的单位，
        # 打残了带不回下一关，等于白送。但踩在己方要点上时守土优先，
        # 这个取舍由战术库按局部兵力对比给出。
        if self.deep:
            decision = tactics.retreat_assessment(state, self.side, unit)
        else:
            decision = tactics.RetreatDecision(
                tactics.should_retreat(state, self.side, unit), 2)
        if decision.retreat:
            spot = self._retreat_spot(state, unit)
            if spot is not None:
                path = rules.find_path(state, unit, spot)
                if path:
                    return path

        # 固守指令：不脱离当前阵位，只在原地附近择优（不前出）
        if ordered is not None and ordered.kind == "hold":
            base = self._score_tile(state, unit, here, 0.0, xmap)
            best_pos, best_score = None, base + 0.4
            for pos, cost in reach.items():
                if pos == here or cost <= 1e-6 or cost > 2.5:
                    continue
                sc = self._score_tile(state, unit, pos, cost, xmap)
                if sc > best_score:
                    best_score, best_pos = sc, pos
            if best_pos is not None:
                return rules.find_path(state, unit, best_pos)
            return None

        # 强攻指令：以指定方向为目标点推进
        goal = None
        if ordered is not None and ordered.kind == "assault":
            goal = (ordered.x, ordered.y)
        if goal is None:
            goal = self._assign_for(state, unit)

        # 主策略：沿目标距离场下降，保证持续推进
        if goal is not None and goal != here:
            # ignore_units=True：梯度用纯地形距离场（可缓存），
            # 友军阻挡由 find_path 精搜时处理——否则距离场每回合都在变，
            # 128×128 想定里这会吃掉 87% 的 AI 回合时间。
            field = rules.field_to_goal(state, unit, goal, ignore_units=True)
            if here in field:
                best_pos, best_val = None, None
                for pos, cost in reach.items():
                    if pos == here:
                        continue
                    fc = field.get(pos)
                    if fc is None or fc >= field[here] - 0.15:
                        continue
                    # 无占领能力的单位不要停在争夺点上，避免堵住己方夺控
                    t = state.map.at(*pos)
                    if t is not None and t.objective and not unit.template.can_capture \
                            and t.owner is not self.side:
                        continue
                    pen = 0.0 if unit.uid in self.returning else self._threat_penalty(state, unit, pos)
                    val = fc + pen * 1.2 + cost * 0.05
                    if xmap:
                        # 交叉火力阵位：能补上第三个方位的落脚点更值钱
                        val -= 1.6 * tactics.crossfire_opportunity(
                            state, self.side, unit, pos, xmap)
                    if best_val is None or val < best_val:
                        best_val, best_pos = val, pos
                if best_pos is not None:
                    return rules.find_path(state, unit, best_pos)

        # 兜底：按战术评分就地择优（已在目标点附近时用于寻找掩护）
        base = self._score_tile(state, unit, here, 0.0, xmap)
        best_pos, best_score = None, base + 0.4
        for pos, cost in reach.items():
            if pos == here or cost <= 1e-6:
                continue
            s = self._score_tile(state, unit, pos, cost, xmap)
            if s > best_score:
                best_score, best_pos = s, pos
        if best_pos is None:
            return None
        return rules.find_path(state, unit, best_pos)

    def _retreat_spot(self, state: "GameState", unit: Unit) -> tuple[int, int] | None:
        """撤退目的地：己方控制区内、离敌人最远的一个可达点。

        优先己方目标点（那里通常有掩护与补给），否则退向己方基地方向。
        """
        reach = rules.flood_reachable(state, unit)
        if len(reach) <= 1:
            return None
        own_pts = [(t.x, t.y) for t in state.objective_tiles if t.owner is self.side]
        foes = [e for e in state.enemies_of(self.side)
                if e.alive and state.known_level(self.side, e) is not Detection.NONE]
        best: tuple[int, int] | None = None
        best_score = -1e9
        for pos, cost in reach.items():
            if pos == (unit.x, unit.y):
                continue
            d = min((rules.dist(pos, (e.x, e.y)) for e in foes), default=99.0)
            # 己方目标点额外加权
            s = d - cost * 0.5
            if pos in own_pts:
                s += 6.0
            # 别退到敌方控制区去
            t = state.map.at(*pos)
            if t is not None and t.owner is not None and t.owner is not self.side:
                s -= 8.0
            if s > best_score:
                best_score, best = s, pos
        return best


    def _nearest_base(self, state: "GameState", unit: Unit) -> tuple[int, int] | None:
        """最近的己方可用机场（或航母）。"""
        best: tuple[int, int] | None = None
        bd = 1e9
        for t in state.map.iter_tiles():
            if t.facility is Facility.AIRBASE and t.owner is unit.side and t.operational:
                d = rules.dist((unit.x, unit.y), (t.x, t.y))
                if d < bd:
                    bd, best = d, (t.x, t.y)
        if best is not None:
            return best          # 固定机场优先：航母在机动，追着跑会耗尽燃油
        for u in state.units:
            if u.alive and u.side is unit.side and u.template.unit_class is UnitClass.CARRIER:
                d = rules.dist((unit.x, unit.y), (u.x, u.y))
                if d < bd:
                    bd, best = d, (u.x, u.y)
        return best

    FUEL_DRAIN = 0.15

    def _needs_return(self, state: "GameState", unit: Unit) -> bool:
        """按剩余航程动态判断是否需要返场（避免"油量够飞回但路径更远"导致的坠毁）。"""
        if unit.domain is not Domain.AIR:
            return False
        base = self._nearest_base(state, unit)
        if base is None:
            return False
        # 半箱以下一律返场，避免在临界值附近来回摆动
        if unit.fuel < 0.45:
            return True
        # 空中移动按 8 邻域计价，对角为 1.414 倍，故用 1.42 折算实际机动消耗
        cost = rules.dist((unit.x, unit.y), base) * 1.42
        turns_needed = cost / max(1.0, unit.template.mp)
        reserve = turns_needed * self.FUEL_DRAIN + 0.30
        return unit.fuel < min(0.94, reserve)

    def _assign_for(self, state: "GameState", unit: Unit) -> tuple[int, int] | None:
        """为单位分配一个固定目标点，避免逐回合摇摆。"""
        if unit.uid in self.assign:
            cur = self.assign[unit.uid]
            if not self.deep or unit.uid in self._switch_checked:
                return cur
            self._switch_checked.add(unit.uid)
            if self._should_switch(state, unit, cur):
                # 目标作废或出现明显更优的选择：把这支部队从旧目标上摘下来，
                # 让它重新走一遍分配流程。摘掉之前别人的 `defenders` 计数
                # 还要靠 `self.assign` 算，所以必须先 pop 再重算。
                self.assign.pop(unit.uid, None)
            else:
                return cur

        # 航空兵按剩余航程判断是否返场
        if self._needs_return(state, unit):
            base = self._nearest_base(state, unit)
            if base is not None:
                self.assign[unit.uid] = base
                self.returning.add(unit.uid)
                return base

        # 空中平台以「猎歼」为主：优先扑向已识别的敌方高价值目标
        if unit.domain is Domain.AIR:
            best: tuple[int, int] | None = None
            best_score = 0.0
            for e in state.enemies_of(self.side):
                if state.known_level(self.side, e) is Detection.NONE:
                    continue
                if e.domain is Domain.SPACE:
                    continue      # 轨道目标不参与地面/空中机动追击
                d = rules.dist((unit.x, unit.y), (e.x, e.y))
                sc = (1.0 + e.template.cost / 300.0) / (1.0 + d / 10.0)
                if sc > best_score:
                    best_score, best = sc, (e.x, e.y)
            if best is not None:
                self.assign[unit.uid] = best
                return best

        # 防空单位守土：优先守住己方目标点，不轻易前出
        if unit.template.interceptors > 0:
            own = [t for t in state.objective_tiles if t.owner is self.side]
            if own:
                t = min(own, key=lambda o: rules.dist((unit.x, unit.y), (o.x, o.y)))
                self.assign[unit.uid] = (t.x, t.y)
                return self.assign[unit.uid]

        # 预警/电子战等无武装传感器平台：前出至能覆盖敌方活动区的阵位
        if not unit.usable_weapons() and unit.best_sensor_range() >= 8:
            foes = state.enemies_of(self.side)
            cand = [t for t in state.objective_tiles if t.owner is not self.side]
            if cand and foes:
                tgt = min(cand, key=lambda t: min(
                    rules.dist((t.x, t.y), (e.x, e.y)) for e in foes))
                dx, dy = tgt.x - unit.x, tgt.y - unit.y
                n = math.hypot(dx, dy) or 1.0
                reach = min(n, unit.best_sensor_range() * 0.75)
                gx = int(round(unit.x + dx / n * reach))
                gy = int(round(unit.y + dy / n * reach))
                gx = max(0, min(state.map.width - 1, gx))
                gy = max(0, min(state.map.height - 1, gy))
                self.assign[unit.uid] = (gx, gy)
                return self.assign[unit.uid]

        # 己方目标点受到威胁时优先回防（尤其是可占领的机动部队）
        # 但每个目标点最多保留 2 个防守单位——否则全队都会被钉在阵地上，
        # 无人去争夺中立要点，战斗会退化成静态对峙。
        if unit.template.can_capture:
            threatened = []
            for t in state.objective_tiles:
                if t.owner is not self.side:
                    continue
                if self._defenders_assigned(state, t) >= 2:
                    continue          # 已有足够兵力防守
                for e in state.enemies_of(self.side):
                    if state.known_level(self.side, e) is Detection.NONE:
                        continue
                    if e.domain in (Domain.AIR, Domain.SPACE):
                        continue
                    if rules.dist((t.x, t.y), (e.x, e.y)) <= 6.0:
                        threatened.append(t)
                        break
            if threatened:
                t = min(threatened, key=lambda o: rules.dist((unit.x, unit.y), (o.x, o.y)))
                self.assign[unit.uid] = (t.x, t.y)
                return self.assign[unit.uid]

        # ---- 目标争夺：用战术库按「路径成本」而非直线距离评估 ----
        # v2.1 之前这里用直线距离打分，结果跨海远征看起来很近，
        # AI 便把仅有的两栖力量派去守自己的点，把中立点拱手让人
        # （128×128 想定实测：8 个中立点一个都拿不到）。


        defenders: dict[tuple[int, int], int] = {}
        for uid, g in self.assign.items():
            if g is not None:
                defenders[g] = defenders.get(g, 0) + 1
        plans = tactics.evaluate_objectives(state, self.side, unit, defenders)
        if plans and plans[0].score >= tactics.FEINT_WEAK_PLAN:
            best = plans[0]
            self.assign[unit.uid] = (best.tile.x, best.tile.y)
            return self.assign[unit.uid]

        # ---- 佯攻：**只在没别的事可做的时候**才分兵 ----
        # 这一步刻意放在争点之后。早先放在之前，结果是「把一支本来要去
        # 主攻方向的部队抽去做姿态」——实测在 40×40 想定里净亏 10 个胜负点。
        # 现在佯攻是「闲兵的差事」：它本来只会去一个很远又不值钱的点
        # （或者干脆原地发呆），那还不如去敌人必救的方向上晃一圈。
        if self.deep:
            spot = self._feint_for(state, unit)
            if spot is not None:
                self.assign[unit.uid] = spot
                self.assign_turn.setdefault(unit.uid, state.turn_count)
                return spot
        if plans:
            best = plans[0]
            self.assign[unit.uid] = (best.tile.x, best.tile.y)
            return self.assign[unit.uid]
        return None

    # ------------------------------------------------------------------ #
    # 多目标优先级转移
    # ------------------------------------------------------------------ #
    def _defender_map(self) -> dict[tuple[int, int], int]:
        """当前已分配给各目标点的兵力数。

        与 `_defenders_assigned` 不同：这里只数「本回合已经定了去向」的单位，
        不做 uid → 单位的反查，用在每回合都要跑好几次的路径上。
        """
        out: dict[tuple[int, int], int] = {}
        for g in self.assign.values():
            if g is not None:
                out[g] = out.get(g, 0) + 1
        return out

    def _should_switch(self, state: "GameState", unit: Unit,
                       cur: tuple[int, int] | None) -> bool:
        """当前目标还要不要继续去。

        只在**每回合第一次**问这个问题（`_switch_checked` 守住），
        否则 `_score_tile` 每评估一个格就会连带问一次。
        """
        if cur is None:
            return False
        # 回防：自家要地被打上门了，决心再坚定也得回来。这条不受冷却限制——
        # 等三个回合再回防，回来时点上已经插着别人的旗了。
        if self._home_threatened(state, unit, cur):
            self.switch_note[unit.uid] = "回防要地"
            return True
        turn = state.turn_count
        if turn - self.assign_turn.get(unit.uid, -99) < tactics.REASSIGN_COOLDOWN:
            return False
        dec = tactics.reassess_objective(state, self.side, unit, cur, None,
                                         self._defender_map())
        self.assign_turn[unit.uid] = turn
        if dec.switch:
            self.switch_note[unit.uid] = dec.reason
        return dec.switch

    def _home_threatened(self, state: "GameState", unit: Unit,
                         cur: tuple[int, int]) -> bool:
        """己方要点是否正在挨打，而我却已经走远了。"""
        if not unit.template.can_capture:
            return False
        cur_tile = state.map.at(cur[0], cur[1])
        if cur_tile is not None and cur_tile.objective and cur_tile.owner is self.side:
            return False        # 我本来就在守一个点，别再拆东墙补西墙
        for t in state.objective_tiles:
            if t.owner is not self.side:
                continue
            if self._defenders_assigned(state, t) >= 2:
                continue
            for e in state.enemies_of(self.side):
                if not e.alive or e.domain in (Domain.AIR, Domain.SPACE):
                    continue
                if state.known_level(self.side, e) is Detection.NONE:
                    continue
                if rules.dist((t.x, t.y), (e.x, e.y)) <= 6.0:
                    return True
        return False

    # ------------------------------------------------------------------ #
    # 佯攻
    # ------------------------------------------------------------------ #
    def _feint_plan(self, state: "GameState") -> tactics.FeintPlan:
        """本回合的佯攻编成。

        要等**几支部队已经排开**再决定：分兵的前提是「主攻有多少人」，
        而这件事在回合开头还是一片空白——那会儿算出来的 `support` 恒为 0，
        判断一定是否决。所以前几个单位先不给计划（也不缓存），
        等 `assign` 有了内容再算一次，算完就定下来。
        """
        if self.feint is not None:
            return self.feint
        if len(self.assign) < 3:
            return tactics.FeintPlan()
        counts = self._defender_map()
        # 主攻方向 = **进攻性**目标里争的人最多的那个。
        # 不能只看「被分配得最多」：己方要点因为防守分支往往人最多，
        # 把它当主攻方向会让「佯攻」跑去打自己的家门口（实测两方都在
        # 满地图对穿，谁也没吸引到谁）。
        main: tuple[int, int] | None = None
        support = 0
        for g in sorted(counts):
            tile = state.map.at(g[0], g[1])
            if tile is None or not tile.objective or tile.owner is self.side:
                continue        # 守家、以及去追敌人（不是目标点）都不算主攻方向
            if counts[g] > support:
                main, support = g, counts[g]
        if main is None:
            cand = sorted((t for t in state.objective_tiles if t.owner is not self.side),
                          key=lambda t: (-t.victory_points, t.y, t.x))
            if cand:
                main = (cand[0].x, cand[0].y)
                support = counts.get(main, 0)
        self.feint = tactics.plan_feint(state, self.side, main, support)
        return self.feint

    def _feint_for(self, state: "GameState", unit: Unit) -> tuple[int, int] | None:
        """这支部队是不是本回合的佯动部队；是就给出佯攻点。"""
        plan = self._feint_plan(state)
        if not plan.active or plan.unit_uid != unit.uid:
            return None
        # 佯动部队被打残 → 收回来。佯攻的价值是「看起来像」，不是「拼光」
        if unit.hp_ratio < 0.55:
            return None
        t = state.map.at(plan.tile[0], plan.tile[1])
        if t is not None and t.owner is self.side:
            return None          # 佯攻点已经拿到手，佯动就变成真的了
        return plan.tile

    def _objective_threatened(self, state: "GameState", t) -> bool:
        """该目标点是否正受威胁（附近有已知敌军）。

        受威胁的点要留冗余守军——一个守军顶不住一次进攻。
        """
        for e in state.enemies_of(self.side):
            if not e.alive:
                continue
            if state.known_level(self.side, e) is Detection.NONE:
                continue
            if e.domain in (Domain.AIR, Domain.SPACE):
                continue
            if rules.dist((t.x, t.y), (e.x, e.y)) <= 8.0:
                return True
        return False


    def _defenders_assigned(self, state: "GameState", t) -> int:
        """统计已被派往防守该目标点的己方单位数量。"""
        n = 0
        for uid, goal in self.assign.items():
            if goal == (t.x, t.y):
                u = state.unit_by_uid(uid)
                if u is not None and u.alive and u.side is self.side:
                    n += 1
        return n

    def _score_tile(self, state: "GameState", unit: Unit, pos: tuple[int, int],
                    cost: float,
                    xmap: dict[int, set[int]] | None = None) -> float:
        score = -cost * 0.12
        cur = (unit.x, unit.y)
        md = state.map
        tile = md.at(*pos)
        if tile is None:
            return -999

        # 1) 向分配目标推进，并在目标点上保持
        goal = self._assign_for(state, unit)
        if goal is not None:
            gain = rules.dist(cur, goal) - rules.dist(pos, goal)
            score += gain * 3.2
            if pos == goal and unit.template.can_capture and tile.owner is not self.side:
                score += 4.5

        # 2) 期望伤害形式的威胁评估
        hp = max(20.0, unit.template.hp)
        threat = 0.0
        own_range = max((w.range_max for w in unit.usable_weapons()), default=0.0)
        enemy_close = 0.0
        for enemy in state.enemies_of(self.side):
            if state.known_level(self.side, enemy) is Detection.NONE:
                continue
            d = rules.dist(pos, (enemy.x, enemy.y))
            enemy_close = max(enemy_close, 1.0 / (1.0 + d / 10.0))
            for w in enemy.usable_weapons():
                if unit.domain not in w.targets:
                    continue
                if d <= w.range_max:
                    threat += w.pk * (1.0 - w.evasion) * w.damage / hp
            if own_range and d <= own_range:
                score += 1.2
        score -= min(2.8, threat * 1.5)

        # 3) 空中平台主动寻敌
        if unit.domain is Domain.AIR:
            score += enemy_close * 2.0
        elif enemy_close > 0 and own_range and own_range < 3:
            score += enemy_close * 0.6

        # 4) 地形掩护与工事
        if tile is not None:
            if unit.domain is Domain.LAND:
                score += tile.terrain.armor_bonus * 4.0
                if tile.terrain.value == "water":
                    score -= 50
                # 己方工事加分，敌方工事扣分（雷场）
                if tile.fort > 0:
                    if tile.fort_owner is self.side:
                        score += 0.8 * tile.fort
                    elif tile.fort_owner is not Side.NEUTRAL:
                        score -= 1.2 * tile.fort
            if tile.elevation > 300 and unit.template.sensors:
                score += 0.5
            if tile.objective and not unit.template.can_capture and tile.owner is not self.side:
                score -= 6.0
            # 5) 交叉火力阵位：能补上第三个直瞄方位的落脚点更值钱
            if xmap:
                score += 1.6 * tactics.crossfire_opportunity(
                    state, self.side, unit, pos, xmap)
        return score


def plan_turn(state: "GameState", side: Side) -> list[AIAction]:
    """一次性生成整回合计划（保留给无动画的批量推演使用）。"""
    planner = AIPlanner(state, side)
    out: list[AIAction] = []
    guard = 0
    while planner.pending() and guard < 500:
        guard += 1
        act = planner.step(state)
        if act is None:
            break
        out.append(act)
    return out


__all__ = ["AIAction", "AIPlanner", "plan_turn"]

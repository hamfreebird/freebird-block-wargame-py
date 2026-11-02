"""对手 AI：基于威胁评估与目标价值启发式的作战规划。

设计原则：
* AI 与玩家受同一套探测规则约束（fog of war 对等）；
* 优先「先敌发现、防区外打击」，避免把高价值平台送入敌方防空包线；
* 每一步都基于最新状态重新规划，因此 AI 会对玩家的机动与开火作出反应。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import rules
from .enums import Detection, Domain, Facility, SensorKind, Side, UnitClass, WeaponKind
from .env import environment_for
from .model import Unit

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


class AIPlanner:
    def __init__(self, state: "GameState", side: Side):
        self.side = side
        order = sorted(
            [u for u in state.units_of(side)],
            key=lambda u: (-u.template.cost, u.domain is Domain.SPACE),
        )
        self.queue: list[int] = [u.uid for u in order]
        self.ew_done: set[int] = set()
        self.move_done: set[int] = set()
        self.actions_taken: dict[int, int] = {}
        self.assign: dict[int, tuple[int, int] | None] = {}
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
                return AIAction(
                    "fire", unit.uid, target_uid=target.uid, weapon_id=weapon.id,
                    note=f"{unit.name} 向 {target.name} 发射 {weapon.name}",
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
                            if random.random() < 0.75:
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
            if random.random() < 0.35:
                return AIAction("radar", unit.uid,
                                note=f"{unit.name} 天气良好，关闭雷达降低暴露")
        return None

    # ------------------------------------------------------------------ #
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
        for enemy in state.enemies_of(self.side):
            if not enemy.alive:
                continue
            if state.known_level(self.side, enemy).level < Detection.TRACKED.level:
                continue
            if enemy.domain is Domain.SPACE and not any(w.asat for w in unit.usable_weapons()):
                continue
            for w in unit.usable_weapons():
                p = rules.shot_profile(state, unit, enemy, w)
                if not p.ok:
                    continue
                val = p.pk * (1.0 - p.intercept_pk) * p.damage
                val += enemy.template.cost / 90.0
                val += (1.0 - enemy.hp_ratio) * 2.5
                # 集中火力：指定目标获得显著加成
                if focus is not None and enemy is focus:
                    val *= 2.2
                if val > best_val:
                    best_val, best = val, (enemy, w, val)
        return best

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

        # 固守指令：不脱离当前阵位，只在原地附近择优（不前出）
        if ordered is not None and ordered.kind == "hold":
            base = self._score_tile(state, unit, here, 0.0)
            best_pos, best_score = None, base + 0.4
            for pos, cost in reach.items():
                if pos == here or cost <= 1e-6 or cost > 2.5:
                    continue
                sc = self._score_tile(state, unit, pos, cost)
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
            field = rules.field_to_goal(state, unit, goal)
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
                    if best_val is None or val < best_val:
                        best_val, best_pos = val, pos
                if best_pos is not None:
                    return rules.find_path(state, unit, best_pos)

        # 兜底：按战术评分就地择优（已在目标点附近时用于寻找掩护）
        base = self._score_tile(state, unit, here, 0.0)
        best_pos, best_score = None, base + 0.4
        for pos, cost in reach.items():
            if pos == here or cost <= 1e-6:
                continue
            s = self._score_tile(state, unit, pos, cost)
            if s > best_score:
                best_score, best_pos = s, pos
        if best_pos is None:
            return None
        return rules.find_path(state, unit, best_pos)

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
            return self.assign[unit.uid]

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

        best: tuple[int, int] | None = None
        best_score = -1.0

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

        for t in state.objective_tiles:
            # 按作战域筛选可达目标，避免单位永远朝着到不了的点位机动
            if unit.domain is Domain.LAND and not unit.template.amphibious:
                if not state.map.same_landmass((unit.x, unit.y), (t.x, t.y)):
                    continue
            if unit.domain in (Domain.SEA, Domain.SUB) and not t.passable_sea:
                continue
            if unit.template.can_capture:
                if t.owner is self.side:
                    weight = 0.35 if self._defenders_assigned(state, t) < 2 else 0.12
                else:
                    weight = 1.0
            else:
                weight = 0.55
            d = rules.dist((unit.x, unit.y), (t.x, t.y))
            sc = weight * (1.0 + t.victory_points * 0.2) / (1.0 + d / 8.0)
            if sc > best_score:
                best_score, best = sc, (t.x, t.y)
        self.assign[unit.uid] = best
        return best

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
                    cost: float) -> float:
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

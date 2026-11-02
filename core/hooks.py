"""规则钩子系统：让想定作者用 JSON 声明自定义规则。

**为什么需要**

现有事件（core/events.py）是「定时触发 + 直接效果」：第 5 回合对某处空袭。
它能做剧本，但表达不了**条件规则**——「每当某单位阵亡时，在此处增援」、
「若某方控制三个以上目标点，则其空军获得制空权」。

模组接口（core/mods.py）让程序员用 Python 写规则；本模块让**不写代码的人**
也能写——用 JSON 声明「触发器 + 效果」。

结构
----
一条规则由两部分组成：

* **触发器（when）**——条件表达式。基础条件用 `{"type": "...", ...}` 声明，
  可用 `all` / `any` / `not` 组合；
* **效果（then）**——要执行的动作列表。

示例::

    "rules": [
      {
        "id": "reinforce_on_loss",
        "when": {"type": "unit_lost", "side": "BLUE",
                 "template": "m1a2"},
        "then": [
          {"type": "reinforce", "side": "BLUE", "template": "m1a2",
           "x": 12, "y": 44, "count": 1},
          {"type": "message", "text": "预备队填补缺口"}
        ]
      }
    ]

触发时机
--------
规则在**回合结算**时统一求值（引擎调用 :func:`evaluate_rules`），
这让规则可以观察战局结果，而不必挂进每一条规则路径。

设计取舍
--------
* 规则**只读**战局状态，副作用集中在效果里——便于调试与回放；
* 单条规则出错不影响其它规则（与现有事件一致）；
* 条件求值顺序固定，因此**可复现**——回放与原局结论一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import Detection, Domain, Side


class RuleError(Exception):
    """规则定义或求值出错。"""


# --------------------------------------------------------------------------- #
# 条件
# --------------------------------------------------------------------------- #
CONDITION_TYPES = (
    "always",
    "turn_at_least", "turn_at_most", "turn_is",
    "side_controls_min",       # 某方控制的目标点数量
    "side_lost_min",           # 某方损失的单位数
    "unit_near_objective",     # 某方有单位在某目标点附近
    "enemy_near_objective",   # 敌方有单位在某目标点附近
    "weather_is",
    "time_of_day_is",
    "victory_points_lead",    # 某方胜负点领先多少
    "all", "any", "not",
)


@dataclass
class Condition:
    """一条触发条件。"""

    type: str = "always"
    side: str = ""
    template: str = ""
    count: int = 0
    value: float = 0.0
    turn: int = 0
    x: int = 0
    y: int = 0
    radius: float = 6.0
    text: str = ""
    children: list["Condition"] = field(default_factory=list)

    @staticmethod
    def parse(raw: Any, where: str = "") -> "Condition":
        if not isinstance(raw, dict):
            raise RuleError(f"{where}条件必须是对象")
        ctype = str(raw.get("type", "always"))
        if ctype not in CONDITION_TYPES:
            raise RuleError(f"{where}未知条件类型 '{ctype}'，"
                            f"可选值：{'、'.join(CONDITION_TYPES)}")
        kids = [Condition.parse(k, f"{where}{ctype}.")
                for k in raw.get("conditions", [])]
        return Condition(
            type=ctype,
            side=str(raw.get("side", "")).upper(),
            template=str(raw.get("template", "")),
            count=int(raw.get("count", 0) or 0),
            value=float(raw.get("value", 0) or 0),
            turn=int(raw.get("turn", 0) or 0),
            x=int(raw.get("x", 0) or 0),
            y=int(raw.get("y", 0) or 0),
            radius=float(raw.get("radius", 6.0) or 0),
            text=str(raw.get("text", "")),
            children=kids,
        )

    def evaluate(self, state) -> bool:
        """求值。条件不满足时返回 False。"""
        ctype = self.type
        if ctype == "always":
            return True
        if ctype == "all":
            return all(c.evaluate(state) for c in self.children)
        if ctype == "any":
            return any(c.evaluate(state) for c in self.children)
        if ctype == "not":
            return not all(c.evaluate(state) for c in self.children)
        if ctype == "turn_at_least":
            return state.turn_count >= self.turn
        if ctype == "turn_at_most":
            return state.turn_count <= self.turn
        if ctype == "turn_is":
            return state.turn_count == self.turn
        if ctype == "weather_is":
            from .env import environment_for
            return environment_for(state).weather.value == self.text
        if ctype == "time_of_day_is":
            from .env import environment_for
            return environment_for(state).time_of_day.value == self.text
        if ctype == "side_controls_min":
            s = _side(self.side)
            if s is None:
                return False
            n = sum(1 for t in state.objective_tiles if t.owner is s)
            return n >= self.count
        if ctype == "side_lost_min":
            s = _side(self.side)
            if s is None:
                return False
            lost = sum(1 for u in state.units
                       if u.side is s and not u.alive)
            return lost >= self.count
        if ctype == "victory_points_lead":
            s = _side(self.side)
            if s is None:
                return False
            other = s.foe
            return state.vp[s] - state.vp[other] >= self.value
        if ctype == "unit_near_objective":
            return self._near(state, self.side, enemy=False)
        if ctype == "enemy_near_objective":
            return self._near(state, self.side, enemy=True)
        return False

    def _near(self, state, side_str: str, enemy: bool) -> bool:
        """某方（或其敌方）是否有单位在某目标点附近。"""
        s = _side(side_str)
        if s is None:
            return False
        targets = [t for t in state.objective_tiles
                   if (t.owner is not s) == enemy or not enemy]
        if not targets:
            return False
        near = False
        for u in state.units:
            if not u.alive:
                continue
            is_enemy = u.side is not s
            if is_enemy != enemy:
                continue
            if self.template and u.template.id != self.template:
                continue
            for t in targets:
                if abs(t.x - u.x) <= self.radius and abs(t.y - u.y) <= self.radius:
                    near = True
                    break
            if near:
                break
        return near


def _side(name: str) -> Side | None:
    if name == "BLUE":
        return Side.BLUE
    if name == "RED":
        return Side.RED
    return None


# --------------------------------------------------------------------------- #
# 效果
# --------------------------------------------------------------------------- #
EFFECT_TYPES = (
    "message",
    "reinforce",
    "airstrike",
    "weather",
    "reveal",
    "objective",
    "damage",
    "heal",
    "set_flag",
)


@dataclass
class Effect:
    """一条效果。"""

    type: str = "message"
    side: str = ""
    template: str = ""
    count: int = 1
    x: int = 0
    y: int = 0
    radius: float = 0.0
    damage: float = 0.0
    weapon: str = ""
    weather: str = ""
    name: str = ""
    owner: str = ""
    value: float = 0.0
    text: str = ""

    @staticmethod
    def parse(raw: Any, where: str = "") -> "Effect":
        if not isinstance(raw, dict):
            raise RuleError(f"{where}效果必须是对象")
        etype = str(raw.get("type", "message"))
        if etype not in EFFECT_TYPES:
            raise RuleError(f"{where}未知效果类型 '{etype}'，"
                            f"可选值：{'、'.join(EFFECT_TYPES)}")
        return Effect(
            type=etype,
            side=str(raw.get("side", "")).upper(),
            template=str(raw.get("template", "")),
            count=int(raw.get("count", 1) or 1),
            x=int(raw.get("x", 0) or 0),
            y=int(raw.get("y", 0) or 0),
            radius=float(raw.get("radius", 0.0) or 0),
            damage=float(raw.get("damage", 0.0) or 0),
            weapon=str(raw.get("weapon", "")),
            weather=str(raw.get("weather", "")),
            name=str(raw.get("name", "")),
            owner=str(raw.get("owner", "")).upper(),
            value=float(raw.get("value", 0.0) or 0),
            text=str(raw.get("text", "")) or str(raw.get("message", "")),
        )


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    """一条规则：条件满足则执行效果。"""

    id: str = ""
    name: str = ""
    when: Condition = field(default_factory=Condition)
    then: list[Effect] = field(default_factory=list)
    once: bool = True           # 只触发一次
    fired: bool = False
    enabled: bool = True

    @staticmethod
    def parse(raw: Any, where: str = "") -> "Rule":
        if not isinstance(raw, dict):
            raise RuleError(f"{where}规则必须是对象")
        when_raw = raw.get("when", {"type": "always"})
        when = Condition.parse(when_raw, f"{where}when.")
        effects_raw = raw.get("then", [])
        if not isinstance(effects_raw, list):
            raise RuleError(f"{where}then 必须是数组")
        effects = [Effect.parse(e, f"{where}then[{i}].")
                   for i, e in enumerate(effects_raw)]
        return Rule(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            when=when,
            then=effects,
            once=bool(raw.get("once", True)),
        )

    @property
    def label(self) -> str:
        return self.name or self.id or "规则"


def parse_rules(raw: Any) -> list[Rule]:
    """从想定 JSON 解析规则列表。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuleError("rules 必须是数组")
    out: list[Rule] = []
    for i, item in enumerate(raw):
        try:
            out.append(Rule.parse(item, f"rules[{i}]."))
        except RuleError as exc:
            raise RuleError(str(exc)) from None
    return out


# --------------------------------------------------------------------------- #
# 求值
# --------------------------------------------------------------------------- #
def evaluate_rules(state, rules: list[Rule]) -> list[str]:
    """结算当前回合的规则，返回播报文本列表。

    与事件一样：**单条规则出错不影响其它规则**。
    求值顺序固定，因此可复现。
    """
    msgs: list[str] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.once and rule.fired:
            continue
        try:
            ok = rule.when.evaluate(state)
        except Exception as exc:
            state.log_message(None, f"规则「{rule.label}」求值失败：{exc}", "system")
            continue
        if not ok:
            continue
        rule.fired = True
        for eff in rule.then:
            try:
                text = _apply_effect(state, eff)
            except Exception as exc:
                text = f"效果「{eff.type}」执行失败：{exc}"
            if text:
                msgs.append(text)
                state.log_message(_side(eff.side), text, "system")
    return msgs


def _apply_effect(state, eff: Effect) -> str:
    """执行一条效果，返回播报文本（无文本则返回空串）。"""
    if eff.type == "message":
        return eff.text or "规则触发"

    if eff.type == "reinforce":
        return _do_reinforce(state, eff)

    if eff.type == "airstrike":
        return _do_area_damage(state, eff, "空袭")

    if eff.type == "damage":
        return _do_area_damage(state, eff, "伤害")

    if eff.type == "heal":
        return _do_area_heal(state, eff)

    if eff.type == "weather":
        return _do_weather(state, eff)

    if eff.type == "reveal":
        for u in state.units:
            if u.alive and abs(u.x - eff.x) <= eff.radius \
                    and abs(u.y - eff.y) <= eff.radius:
                u.revealed_until = state.turn_count + 1
        return f"规则暴露 ({eff.x},{eff.y}) 半径 {eff.radius:.0f}"

    if eff.type == "objective":
        t = state.map.at(eff.x, eff.y)
        if t is None:
            return ""
        t.objective = True
        t.objective_name = eff.name or t.objective_name or "新增目标点"
        t.victory_points = int(eff.value) if eff.value else t.victory_points
        t.owner = _side(eff.owner) or t.owner
        return f"规则设定目标点 {t.objective_name} ({eff.x},{eff.y})"

    if eff.type == "set_flag":
        state.rule_flags[eff.text] = eff.value
        return f"规则标记 {eff.text} = {eff.value}"

    return ""



# --------------------------------------------------------------------------- #
# 效果实现
# --------------------------------------------------------------------------- #
def _do_reinforce(state, eff: Effect) -> str:
    """增援：在指定位置部署若干单位（落点冲突时自动外挪）。"""
    from .events import _free_spot
    from .model import Unit

    side = _side(eff.side) or state.player_side
    tpl = state.scenario.armory.templates.get(eff.template)
    if tpl is None:
        return f"规则增援失败：未知兵种 '{eff.template}'"
    if not state.map.in_bounds(eff.x, eff.y):
        return f"规则增援失败：坐标 ({eff.x},{eff.y}) 越界"
    placed: list[str] = []
    for _ in range(max(1, eff.count)):
        spot = _free_spot(state, side, eff.x, eff.y, tpl.domain)
        if spot is None:
            break
        if hasattr(state, "alloc_uid"):
            u = Unit(template=tpl, side=side, x=spot[0], y=spot[1],
                     uid=state.alloc_uid())
        else:
            u = Unit(template=tpl, side=side, x=spot[0], y=spot[1])
        state.units.append(u)
        placed.append(u.template.short)
    if not placed:
        return ""
    if hasattr(state, "invalidate_pos_index"):
        state.invalidate_pos_index()
    return f"规则增援 {len(placed)}×{eff.template} 于 ({eff.x},{eff.y})"


def _do_area_damage(state, eff: Effect, label: str) -> str:
    """区域伤害：指定范围内的敌方单位受损。"""
    side = _side(eff.side) or state.player_side.foe
    hit = 0
    for u in list(state.units):
        if not u.alive or u.side is side:
            continue
        if abs(u.x - eff.x) <= eff.radius and abs(u.y - eff.y) <= eff.radius:
            u.hp = max(0.0, u.hp - eff.damage)
            if u.hp <= 0.0:
                u.destroyed = True
            hit += 1
    if hasattr(state, "invalidate_pos_index"):
        state.invalidate_pos_index()
    return f"规则{label} ({eff.x},{eff.y}) 半径 {eff.radius:.0f}，命中 {hit} 个单位"


def _do_area_heal(state, eff: Effect) -> str:
    """区域修复：指定范围内的己方单位回血。"""
    side = _side(eff.side) or state.player_side
    hit = 0
    for u in list(state.units):
        if not u.alive or u.side is not side:
            continue
        if abs(u.x - eff.x) <= eff.radius and abs(u.y - eff.y) <= eff.radius:
            u.hp = min(u.template.hp, u.hp + eff.damage)
            hit += 1
    return f"规则修复 ({eff.x},{eff.y}) 半径 {eff.radius:.0f}，修复 {hit} 个单位"


def _do_weather(state, eff: Effect) -> str:
    """改写天候。"""
    from .env import Weather
    try:
        w = Weather(eff.weather)
    except ValueError:
        return f"规则改写天候失败：未知天候 '{eff.weather}'"
    state.scenario_weather = w
    return f"规则改写天候为 {w.label}"



__all__ = [
    "RuleError", "Condition", "Effect", "Rule",
    "CONDITION_TYPES", "EFFECT_TYPES",
    "parse_rules", "evaluate_rules",
]

"""想定脚本：按回合触发的事件。

社区作者不应该为了加一个"第 5 回合空袭"就去改 Python 代码——
在想定 JSON 里写一条 `events` 就能做到：

```jsonc
"events": [
  {
    "turn": 5,
    "type": "airstrike",
    "side": "RED",
    "x": 12, "y": 8,
    "weapon": "kh101",
    "radius": 2,
    "damage": 45,
    "message": "红方航空兵对训练场实施空袭"
  },
  {
    "turn": 8,
    "type": "reinforce",
    "side": "BLUE",
    "unit": "m1a2", "x": 5, "y": 20, "count": 2
  },
  {
    "turn": 3,
    "type": "weather",
    "weather": "fog"
  },
  {
    "turn": 10,
    "type": "objective",
    "x": 18, "y": 18, "name": "补给点", "owner": "BLUE", "value": 5
  }
]
```

事件类型一览：

| type | 效果 |
|---|---|
| `airstrike` | 空袭：对指定区域造成伤害（可选武器、范围、伤害值） |
| `reinforce` | 增援：在指定位置部署若干单位 |
| `weather` | 改变天候（clear / rain / fog / snow） |
| `objective` | 新增或变更目标点 |
| `reveal` | 强制暴露：让某方立即获得对某区域的识别 |
| `message` | 播报一条战报信息（用于剧情与引导） |

设计要点：
* 事件**只在玩家行动开始前结算**，保证玩家有反应时间而不是被偷袭；
* 已触发的事件不会重复执行（除非显式写 `repeat`）；
* 事件解析容错：字段缺失时给出可读的错误，而不是让整局崩溃。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .enums import Domain, Facility, Side, Terrain, Weather

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState
    from .model import Unit


EVENT_TYPES = ("airstrike", "reinforce", "weather", "objective", "reveal", "message")


class EventError(Exception):
    """事件定义错误。"""


@dataclass
class ScenarioEvent:
    """一条想定事件。"""

    turn: int = 1
    type: str = "message"
    side: str = ""                # 归属方（BLUE / RED）
    x: int = 0
    y: int = 0
    radius: float = 0.0
    weapon: str = ""              # 使用的武器 id（空则用默认伤害）
    damage: float = 0.0
    unit: str = ""                # 增援的兵种 id
    count: int = 1
    weather: str = ""             # 天候
    name: str = ""                # 目标点名称
    owner: str = ""               # 目标点归属
    value: int = 0                # 目标点胜负点
    kind: str = "capture"         # 目标点类型
    message: str = ""
    repeat: bool = False          # 是否每回合重复触发
    fired: bool = False           # 内部状态：是否已触发

    @property
    def label(self) -> str:
        """事件的可读描述（用于 UI 展示与日志）。"""
        if self.type == "airstrike":
            return f"空袭 ({self.x},{self.y}) 半径 {self.radius:.0f} 格"
        if self.type == "reinforce":
            return f"增援 {self.count}×{self.unit} 于 ({self.x},{self.y})"
        if self.type == "weather":
            return f"天候转为{self.weather}"
        if self.type == "objective":
            return f"目标点 {self.name or '新增'} ({self.x},{self.y})"
        if self.type == "reveal":
            return f"暴露 ({self.x},{self.y})"
        return self.message or self.type

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn, "type": self.type, "side": self.side,
            "x": self.x, "y": self.y, "radius": self.radius,
            "weapon": self.weapon, "damage": self.damage,
            "unit": self.unit, "count": self.count, "weather": self.weather,
            "name": self.name, "owner": self.owner, "value": self.value,
            "kind": self.kind, "message": self.message, "repeat": self.repeat,
        }


def parse_events(raw: Any) -> list[ScenarioEvent]:
    """从想定 JSON 解析事件列表；格式错误时抛出带定位信息的异常。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EventError("events 必须是数组")
    out: list[ScenarioEvent] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise EventError(f"events[{i}] 必须是对象")
        etype = str(item.get("type", "message"))
        if etype not in EVENT_TYPES:
            raise EventError(
                f"events[{i}]: 未知事件类型 '{etype}'，"
                f"可选值: {'、'.join(EVENT_TYPES)}")
        turn = int(item.get("turn", 1))
        if turn < 1:
            raise EventError(f"events[{i}]: turn 必须 ≥ 1")
        out.append(ScenarioEvent(
            turn=turn,
            type=etype,
            side=str(item.get("side", "")).upper(),
            x=int(item.get("x", 0)),
            y=int(item.get("y", 0)),
            radius=float(item.get("radius", 0.0)),
            weapon=str(item.get("weapon", "")),
            damage=float(item.get("damage", 0.0)),
            unit=str(item.get("unit", "")),
            count=max(1, int(item.get("count", 1))),
            weather=str(item.get("weather", "")),
            name=str(item.get("name", "")),
            owner=str(item.get("owner", "")).upper(),
            value=int(item.get("value", 0)),
            kind=str(item.get("kind", "capture")),
            message=str(item.get("message", "")),
            repeat=bool(item.get("repeat", False)),
        ))
    out.sort(key=lambda e: e.turn)
    return out


# --------------------------------------------------------------------------- #
# 结算
# --------------------------------------------------------------------------- #
def due_events(events: list[ScenarioEvent], turn: int) -> list[ScenarioEvent]:
    """取当前回合应触发的事件。"""
    return [e for e in events if e.turn == turn and (e.repeat or not e.fired)]


def apply_events(state: "GameState", events: list[ScenarioEvent]) -> list[str]:
    """结算当前回合的事件，返回播报文本列表。"""
    msgs: list[str] = []
    for ev in due_events(events, state.turn_count):
        try:
            text = _apply_one(state, ev)
        except Exception as exc:      # 事件出错不应中断整局
            text = f"事件「{ev.label}」执行失败：{exc}"
        ev.fired = True
        if text:
            msgs.append(text)
            state.log_message(_side_of(ev), text, "system")
    return msgs


def _side_of(ev: ScenarioEvent) -> Side | None:
    if ev.side == "BLUE":
        return Side.BLUE
    if ev.side == "RED":
        return Side.RED
    return None


def _apply_one(state: "GameState", ev: ScenarioEvent) -> str:
    if ev.type == "message":
        return ev.message or "想定事件"

    if ev.type == "airstrike":
        return _do_airstrike(state, ev)
    if ev.type == "reinforce":
        return _do_reinforce(state, ev)
    if ev.type == "weather":
        return _do_weather(state, ev)
    if ev.type == "objective":
        return _do_objective(state, ev)
    if ev.type == "reveal":
        return _do_reveal(state, ev)
    return ""


def _do_airstrike(state: "GameState", ev: ScenarioEvent) -> str:
    """空袭：对区域内单位造成伤害。"""
    side = _side_of(ev)
    attacker_side = side or state.player_side.foe
    # 找一件可用的空袭武器：优先指定，否则按兵种库挑一件对地火力
    weapon = None
    if ev.weapon:
        for u in state.units_of(attacker_side):
            weapon = u.weapon_by_id(ev.weapon)
            if weapon:
                break
    if weapon is None:
        for u in state.units_of(attacker_side):
            for w in u.template.weapons:
                if w.kind.value in ("bomb", "land_attack", "asm", "atgm"):
                    weapon = w
                    break
            if weapon:
                break
    damage = ev.damage if ev.damage > 0 else (weapon.damage if weapon else 30.0)

    hits: list[str] = []
    r = max(0.0, ev.radius)
    for u in list(state.units):
        if not u.alive:
            continue
        if u.side is attacker_side:
            continue
        d = rules_dist((ev.x, ev.y), (u.x, u.y))
        if d > r + 0.5:
            continue
        # 距离越远伤害越低
        falloff = 1.0 if r <= 0 else max(0.3, 1.0 - (d / (r + 1.0)) * 0.6)
        dmg = damage * falloff
        u.apply_damage(dmg)
        hits.append(f"{u.name}(-{dmg:.0f})")
        if u.destroyed:
            state.casualties[u.side] += 1
            state.add_fx("explosion", (u.x, u.y), None, ttl=1.2, color=(255, 110, 70))
    if not hits:
        return f"{ev.message or '空袭'}：未造成伤亡"
    label = ev.message or f"空袭命中 {len(hits)} 个目标"
    return f"{label}：{'、'.join(hits[:4])}"


def _do_reinforce(state: "GameState", ev: ScenarioEvent) -> str:
    """增援：在指定位置部署若干单位。"""
    from .model import Unit
    side = _side_of(ev)
    if side is None:
        return "增援事件缺少 side"
    tpl = state.scenario.armory.templates.get(ev.unit)
    if tpl is None:
        return f"增援失败：未知兵种 '{ev.unit}'"
    if not state.map.in_bounds(ev.x, ev.y):
        return f"增援失败：坐标 ({ev.x},{ev.y}) 越界"
    placed: list[str] = []
    for _ in range(ev.count):
        if hasattr(state, "alloc_uid"):
            u = Unit(template=tpl, side=side, x=ev.x, y=ev.y,
                     uid=state.alloc_uid())
        else:
            u = Unit(template=tpl, side=side, x=ev.x, y=ev.y)
        # 尝试找一个不冲突的落点
        spot = _free_spot(state, side, ev.x, ev.y, tpl.domain)
        if spot is None:
            break
        u.x, u.y = spot
        if hasattr(state, "invalidate_pos_index"):
            state.invalidate_pos_index()
        state.units.append(u)
        placed.append(u.name)
    if not placed:
        return f"增援失败：({ev.x},{ev.y}) 附近没有可用落点"
    return f"增援抵达：{'、'.join(placed)}"


def _free_spot(state: "GameState", side: Side, x: int, y: int,
               domain: Domain) -> tuple[int, int] | None:
    """在指定点附近找一个可部署的落点。"""
    tile = state.map.at(x, y)
    if tile is None:
        return None
    occupied = state.unit_at(x, y)
    if occupied is None and not (domain in (Domain.LAND, Domain.INFRA)
                                 and tile.terrain is Terrain.WATER):
        return (x, y)
    for r in range(1, 5):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                nx, ny = x + dx, y + dy
                if not state.map.in_bounds(nx, ny):
                    continue
                if state.unit_at(nx, ny) is not None:
                    continue
                nt = state.map.at(nx, ny)
                if nt is None:
                    continue
                if domain in (Domain.LAND, Domain.INFRA) and nt.terrain is Terrain.WATER:
                    continue
                if domain in (Domain.SEA, Domain.SUB) and not nt.passable_sea:
                    continue
                return (nx, ny)
    return None


def _do_weather(state: "GameState", ev: ScenarioEvent) -> str:
    """改变天候。"""
    name = ev.weather.lower()
    if name not in ("clear", "rain", "fog", "snow"):
        return f"天候事件失败：未知天候 '{ev.weather}'"
    track = getattr(state, "env_track", None)
    if track is None:
        return "天候事件失败：当前对局没有环境系统"
    track.mode = "fixed"
    track.fixed = Weather(name)
    state._last_env = None       # 强制下一回合播报变化
    return f"天候转为{Weather(name).label}"


def _do_objective(state: "GameState", ev: ScenarioEvent) -> str:
    """新增或变更目标点。"""
    from .model import Objective
    if not state.map.in_bounds(ev.x, ev.y):
        return f"目标点事件失败：坐标 ({ev.x},{ev.y}) 越界"
    tile = state.map.at(ev.x, ev.y)
    if tile is None:
        return "目标点事件失败：无效地块"
    owner = Side.NEUTRAL
    if ev.owner in ("BLUE", "RED"):
        owner = Side(ev.owner)
    tile.objective = True
    tile.objective_name = ev.name or tile.objective_name or "新增目标点"
    tile.victory_points = ev.value if ev.value > 0 else tile.victory_points or 3
    tile.owner = owner
    # 新增目标点会改变渲染层的地块索引，通知其重建
    state.map_epoch += 1
    # 同步 objectives 列表
    for o in state.map.objectives:
        if o.x == ev.x and o.y == ev.y:
            o.name = tile.objective_name
            o.value = tile.victory_points
            o.holder = owner
            o.owner = owner
            break
    else:
        state.map.objectives.append(Objective(
            x=ev.x, y=ev.y, name=tile.objective_name,
            kind=_objective_kind(ev.kind), side=owner, value=tile.victory_points,
            holder=owner, owner=owner))
    return f"目标点「{tile.objective_name}」出现在 ({ev.x},{ev.y})"


def _objective_kind(name: str):
    from .enums import ObjectiveKind
    try:
        return ObjectiveKind(name)
    except ValueError:
        return ObjectiveKind.CAPTURE


def _do_reveal(state: "GameState", ev: ScenarioEvent) -> str:
    """强制暴露：让某方立刻识别该区域的目标。"""
    from .enums import Detection
    side = _side_of(ev)
    if side is None:
        return "暴露事件缺少 side"
    revealed = 0
    for u in state.units:
        if not u.alive or u.side is side:
            continue
        d = rules_dist((ev.x, ev.y), (u.x, u.y))
        if d <= max(1.0, ev.radius):
            u.revealed_until = state.turn_count + 1
            revealed += 1
    state.refresh_detection()
    if revealed == 0:
        return "暴露事件：该区域没有目标"
    return f"情报更新：({ev.x},{ev.y}) 附近 {revealed} 个目标暴露"


def rules_dist(a: tuple[int, int], b: tuple[int, int]) -> float:
    """两点距离（避免在此模块直接 import rules 造成循环依赖）。"""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


__all__ = [
    "EVENT_TYPES", "EventError", "ScenarioEvent",
    "parse_events", "due_events", "apply_events",
]

"""想定校验：数据之外的质量检查。

`main.py --check` 只验证"能不能加载"，但加载成功不代表想定**好玩**或**公平**。
本模块补充三类检查：

1. **地形连通性**——目标点是否与双方出生点连通？部队会不会一开始就困在孤岛？
2. **目标点可达性**——目标点的地形是否适合该兵种占领？部署点是否合法？
3. **兵力配比**——双方兵力是否失衡？是否存在"必胜"配置？

所有问题都**定位到具体格**（如 `(12,18)`），方便作者直接改。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .enums import Domain, Facility, Side, Terrain, UnitClass
from .loader import SUB_CAPACITY
from .rules import AIRBASE_CAPACITY


@dataclass
class Issue:
    """一条校验问题。"""

    level: str = "warn"        # error / warn / info
    code: str = ""             # 机器可读代码
    message: str = ""          # 人类可读描述
    x: int = -1                # 定位坐标（-1 表示不针对具体格）
    y: int = -1

    @property
    def where(self) -> str:
        if self.x >= 0 and self.y >= 0:
            return f"({self.x},{self.y})"
        return ""

    def __str__(self) -> str:
        loc = f" @{self.where}" if self.where else ""
        return f"[{self.level}] {self.code}{loc} {self.message}"


@dataclass
class ValidationReport:
    """校验结果。"""

    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "info"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, level: str, code: str, message: str, x: int = -1, y: int = -1) -> None:
        self.issues.append(Issue(level=level, code=code, message=message, x=x, y=y))


# --------------------------------------------------------------------------- #
# 地形连通性
# --------------------------------------------------------------------------- #
def _land_neighbors(md, x: int, y: int):
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                   (-1, -1), (1, -1), (-1, 1), (1, 1)):
        nx, ny = x + dx, y + dy
        if md.in_bounds(nx, ny):
            yield nx, ny


def _flood_land(md, start: tuple[int, int],
                amphibious: bool = False) -> set[tuple[int, int]]:
    """从起点洪泛可通行陆地，返回可达格集合。"""
    seen: set[tuple[int, int]] = set()
    if not md.in_bounds(*start):
        return seen
    q = deque([start])
    seen.add(start)
    while q:
        x, y = q.popleft()
        for nx, ny in _land_neighbors(md, x, y):
            if (nx, ny) in seen:
                continue
            t = md.at(nx, ny)
            if t is None:
                continue
            if t.terrain is Terrain.WATER:
                # 两栖单位可以渡过水域
                if not amphibious:
                    continue
            seen.add((nx, ny))
            q.append((nx, ny))
    return seen


def _flood_sea(md, start: tuple[int, int]) -> set[tuple[int, int]]:
    """从起点洪泛可通行水域。"""
    seen: set[tuple[int, int]] = set()
    if not md.in_bounds(*start):
        return seen
    q = deque([start])
    seen.add(start)
    while q:
        x, y = q.popleft()
        for nx, ny in _land_neighbors(md, x, y):
            if (nx, ny) in seen:
                continue
            t = md.at(nx, ny)
            if t is None or not t.passable_sea:
                continue
            seen.add((nx, ny))
            q.append((nx, ny))
    return seen


# --------------------------------------------------------------------------- #
# 主校验
# --------------------------------------------------------------------------- #
def validate_scenario(source) -> ValidationReport:
    """对想定做完整校验，返回报告。

    `source` 可以是 `Scenario`（静态想定文件）或 `GameState`（对局中的状态，
    此时自动取其底层想定）。这样同一套校验既可用于 `--check`，
    也可用于校验玩家在编辑器里改出的想定。
    """
    scenario = getattr(source, "scenario", source) or source
    rep = ValidationReport()
    _check_units(rep, scenario)
    _check_objectives(rep, scenario)
    _check_connectivity(rep, scenario)
    _check_balance(rep, scenario)
    _check_events(rep, scenario)
    _check_meta(rep, scenario)
    return rep


def _check_units(rep: ValidationReport, scenario) -> None:
    """单位部署合法性。"""
    md = scenario.map
    occupied: dict[tuple[int, int], list] = {}
    for u in scenario.units:
        pos = (u.x, u.y)
        occupied.setdefault(pos, []).append(u)
        t = md.at(u.x, u.y)
        if t is None:
            rep.add("error", "unit-offmap", f"单位 {u.name} 位置越界", u.x, u.y)
            continue
        # 陆上单位不应在海里
        if u.domain in (Domain.LAND, Domain.INFRA) and t.terrain is Terrain.WATER:
            if not u.template.amphibious:
                rep.add("error", "unit-in-water",
                        f"{u.name} 是陆上单位却部署在海域", u.x, u.y)
        if u.domain in (Domain.SEA, Domain.SUB) and not t.passable_sea:
            rep.add("error", "unit-on-land",
                    f"{u.name} 是水面/水下单位却部署在陆地（{t.terrain.label}）", u.x, u.y)
        # 航空兵必须有母基地
        if u.domain is Domain.AIR:
            has_base = any(
                f.facility is Facility.AIRBASE and f.owner is u.side and f.operational
                for f in md.iter_tiles())
            if not has_base and not any(
                    h.template.unit_class is UnitClass.CARRIER and h.side is u.side
                    for h in scenario.units):
                rep.add("warn", "air-no-base",
                        f"{u.name} 是航空兵，但本方没有可用机场或航母——将无法返场加油",
                        u.x, u.y)

    # 同格冲突
    for pos, us in occupied.items():
        if len(us) <= 1:
            continue
        sides = {u.side for u in us}
        if len(sides) > 1:
            names = "、".join(u.name for u in us)
            rep.add("error", "unit-overlap", f"同格部署了敌对单位：{names}", pos[0], pos[1])
            continue
        # 同阵营多单位：按域分别限制
        #   地面 ≤ 1、水面 ≤ 1、水下 ≤ SUB_CAPACITY、空中 ≤ AIRBASE_CAPACITY
        # 空中单位可与地面部队同格（空中支援地面）；
        # 水下单位（潜艇）可与水面舰同格——它在水面之下。
        ground = [u for u in us if u.domain in (Domain.LAND, Domain.INFRA)]
        sea = [u for u in us if u.domain is Domain.SEA]
        sub = [u for u in us if u.domain is Domain.SUB]
        air = [u for u in us if u.domain is Domain.AIR]
        if len(ground) > 1:
            names = "、".join(u.name for u in ground)
            rep.add("error", "unit-stack",
                    f"同格部署了 {len(ground)} 支同阵营地面部队：{names}", pos[0], pos[1])
        if len(sea) > 1:
            names = "、".join(u.name for u in sea)
            rep.add("error", "unit-stack",
                    f"同格部署了 {len(sea)} 艘同阵营水面舰：{names}", pos[0], pos[1])
        if len(sub) > SUB_CAPACITY:
            rep.add("warn", "unit-stack",
                    f"同格潜艇 {len(sub)} 艘，超过容量 {SUB_CAPACITY}", pos[0], pos[1])
        if len(air) > AIRBASE_CAPACITY:
            rep.add("warn", "unit-stack",
                    f"同格空中单位 {len(air)} 架，超过容量 {AIRBASE_CAPACITY}",
                    pos[0], pos[1])

    # 空阵营
    for side in (Side.BLUE, Side.RED):
        n = len([u for u in scenario.units if u.side is side])
        if n == 0:
            rep.add("error", "side-empty", f"{side.label}没有任何单位")


def _check_objectives(rep: ValidationReport, scenario) -> None:
    """目标点合理性与可达性。"""
    md = scenario.map
    for o in md.objectives:
        t = md.at(o.x, o.y)
        if t is None:
            continue
        if t.terrain is Terrain.WATER:
            rep.add("warn", "objective-water",
                    f"目标点「{o.name}」在海域，需要两栖单位才能占领", o.x, o.y)
        if o.value <= 0:
            rep.add("warn", "objective-zero",
                    f"目标点「{o.name}」的胜负点为 {o.value}", o.x, o.y)
        if o.kind.value == "capture":
            # 占领类目标点附近应有可占领单位或至少一方有
            cap = [u for u in scenario.units if u.template.can_capture]
            if not cap:
                rep.add("warn", "objective-no-capturer",
                        f"目标点「{o.name}」为占领类，但本局没有任何可占领单位", o.x, o.y)


def _check_connectivity(rep: ValidationReport, scenario) -> None:
    """地形连通性：目标点是否与双方阵营连通。"""
    md = scenario.map
    blues = [(u.x, u.y) for u in scenario.units if u.side is Side.BLUE]
    reds = [(u.x, u.y) for u in scenario.units if u.side is Side.RED]
    if not blues or not reds:
        return

    # 陆上连通域：以蓝方陆地单位为源
    amphibious = any(u.template.amphibious for u in scenario.units
                     if u.side is Side.BLUE)
    blue_land = [p for p in blues
                 if md.at(*p) and md.at(*p).terrain is not Terrain.WATER]
    red_land = [p for p in reds
                if md.at(*p) and md.at(*p).terrain is not Terrain.WATER]
    blue_reach: set[tuple[int, int]] = set()
    if blue_land:
        # 从所有蓝方陆上单位洪泛（取并集）
        for p in blue_land:
            blue_reach |= _flood_land(md, p, amphibious)
    else:
        blue_reach = set(blues)

    red_reach: set[tuple[int, int]] = set()
    red_amph = any(u.template.amphibious for u in scenario.units if u.side is Side.RED)
    if red_land:
        for p in red_land:
            red_reach |= _flood_land(md, p, red_amph)
    else:
        red_reach = set(reds)

    # 目标点可达性
    for o in md.objectives:
        pos = (o.x, o.y)
        t = md.at(o.x, o.y)
        if t is None or t.terrain is Terrain.WATER:
            continue
        b_ok = pos in blue_reach
        r_ok = pos in red_reach
        if not b_ok and not r_ok:
            rep.add("error", "objective-unreachable",
                    f"目标点「{o.name}」与双方兵力都不连通——没有任何一方能到达", o.x, o.y)
        elif not b_ok:
            rep.add("warn", "objective-blue-blocked",
                    f"目标点「{o.name}」蓝方无法到达（地形被切断）", o.x, o.y)
        elif not r_ok:
            rep.add("warn", "objective-red-blocked",
                    f"目标点「{o.name}」红方无法到达（地形被切断）", o.x, o.y)

    # 双方是否完全隔绝
    if blue_reach and red_reach and not (blue_reach & red_reach):
        rep.add("warn", "no-contact",
                "双方兵力之间没有连通的陆地——将无法发生任何地面交战")


def _check_balance(rep: ValidationReport, scenario) -> None:
    """兵力配比。"""
    armory = scenario.armory
    blue = [u for u in scenario.units if u.side is Side.BLUE]
    red = [u for u in scenario.units if u.side is Side.RED]
    if not blue or not red:
        return
    blue_cost = sum(u.template.cost for u in blue)
    red_cost = sum(u.template.cost for u in red)
    total = blue_cost + red_cost
    if total > 0:
        ratio = blue_cost / total
        if ratio > 0.72:
            rep.add("warn", "balance-blue",
                    f"蓝方造价占比 {ratio * 100:.0f}%，兵力明显偏重")
        elif ratio < 0.28:
            rep.add("warn", "balance-red",
                    f"红方造价占比 {(1 - ratio) * 100:.0f}%，兵力明显偏重")
        elif abs(ratio - 0.5) < 0.02:
            rep.add("info", "balance-even", "双方造价几乎相等——注意别让一方靠装备质量取胜")

    # 双方兵种构成过于雷同
    blue_kinds = {u.template.id for u in blue}
    red_kinds = {u.template.id for u in red}
    if blue_kinds and red_kinds and blue_kinds == red_kinds:
        rep.add("info", "same-arms",
                "双方使用完全相同的兵种——战术变化将只能来自操作而非编制")


def _check_events(rep: ValidationReport, scenario) -> None:
    """想定脚本事件。"""
    from .events import EVENT_TYPES, EventError, parse_events
    raw = scenario.raw.get("events")
    # 加载器已记录过解析错误
    recorded = scenario.raw.get("_events_error")
    if recorded:
        rep.add("error", "event-parse", f"想定事件解析失败：{recorded}")
        return
    if raw is None:
        return
    try:
        events = parse_events(raw)
    except EventError as exc:
        rep.add("error", "event-parse", f"想定事件解析失败：{exc}")
        return
    md = scenario.map
    armory = scenario.armory
    for i, ev in enumerate(events):
        if ev.turn > scenario.turn_limit:
            rep.add("warn", "event-late",
                    f"事件 {i + 1}（{ev.label}）的回合 {ev.turn} 超出想定回合上限 "
                    f"{scenario.turn_limit}，永远不会触发")
        if ev.type == "reinforce":
            if ev.unit not in armory.templates:
                rep.add("error", "event-unit",
                        f"事件 {i + 1}：未知兵种 '{ev.unit}'")
            if not md.in_bounds(ev.x, ev.y):
                rep.add("error", "event-offmap",
                        f"事件 {i + 1}：增援坐标 ({ev.x},{ev.y}) 越界", ev.x, ev.y)
        elif ev.type in ("airstrike", "objective", "reveal"):
            if not md.in_bounds(ev.x, ev.y):
                rep.add("error", "event-offmap",
                        f"事件 {i + 1}：坐标 ({ev.x},{ev.y}) 越界", ev.x, ev.y)
        elif ev.type == "weather":
            if ev.weather and ev.weather not in ("clear", "rain", "fog", "snow"):
                rep.add("error", "event-weather",
                        f"事件 {i + 1}：未知天候 '{ev.weather}'")
        if ev.type not in EVENT_TYPES:
            rep.add("error", "event-type", f"事件 {i + 1}：未知类型 '{ev.type}'")


def _check_meta(rep: ValidationReport, scenario) -> None:
    """元数据完整性——社区想定应当标明作者与难度。"""
    meta = getattr(scenario, "meta", None)
    if meta is None:
        return
    if not meta.author:
        rep.add("info", "meta-author", "想定未标注作者")
    if not meta.difficulty:
        rep.add("info", "meta-difficulty", "想定未标注难度标签")
    if meta.recommended_units:
        for uid in meta.recommended_units:
            if uid not in scenario.armory.templates:
                rep.add("warn", "meta-unit",
                        f"元数据推荐的兵种 '{uid}' 在武器库中不存在")


__all__ = ["Issue", "ValidationReport", "validate_scenario"]

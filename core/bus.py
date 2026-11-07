"""事件总线：把「刚刚发生了什么」广播出去，取代订阅方轮询状态。

**为什么需要**：v3.0 的模组钩子只有五个事件，且大多挂在「开火」这一条链上。
「某单位阵亡」在别的路径上根本不广播——燃油耗尽坠毁、想定脚本的区域伤害、
设施打击引发的连锁，这些死掉的单位一声不响。模组只能每回合遍历单位列表去猜，
而轮询有两个绕不过去的毛病：

1. **分不清「本来就死了」和「刚刚死了」**——这恰恰是「阵亡时触发某事」的
   全部意义；
2. **会漏**——一回合内死了又清掉的诱饵、结算完就移出列表的单位，轮询根本
   看不见。

**做法**：引擎在**状态真的改变**的那一行投递事件，总线负责分发：

* 记进有界历史（默认 96 条），界面与调试可以回看「这回合都发生了什么」；
* 转发给模组钩子（保持 ``(state, *args)`` 的位置参数签名不变）；
* 转发给状态上的订阅者（界面 / 教学 / 观战），回调签名 ``(state, event)``。

**只有引擎能投递**。订阅者拿到的是既成事实，改不了任何东西——要「改」的场合
（命中率、伤害）继续走 ``core/mods.py`` 的 ``modify_*`` 钩子。这两类接口刻意
分开：一类改结果，一类看结果，混在一起会让「为什么这发没打中」变得无法追。
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# 事件目录
# --------------------------------------------------------------------------- #
#: 事件名 → 必须携带的字段（顺序即模组钩子的位置参数顺序）。
#:
#: 新增事件时三处一起改：这里、``core/mods.py`` 的 ``HOOK_SIGNATURES``、
#: ``SDK.md`` 的事件表。``tools/test_bus.py`` 会交叉核对这三处是否一致。
CATALOGUE: dict[str, tuple[str, ...]] = {
    "turn_start": ("turn",),
    "turn_end": ("turn",),
    "unit_destroyed": ("unit", "cause", "by"),
    "unit_damaged": ("unit", "damage", "by"),
    "facility_struck": ("tile", "damage", "by"),
    "facility_captured": ("side", "x", "y", "by"),
    "objective_captured": ("side", "x", "y", "by"),
}

#: 传给模组钩子的位置参数（不含前置的 ``state``）。
#: 与 :data:`CATALOGUE` 的字段顺序一致，只是把「仅供本地订阅者看」的字段筛掉。
#: 例如 ``unit_destroyed`` 带 ``cause`` 给界面解释原因，但模组钩子的签名
#: 是 ``(state, unit)``——v3.0 定下的契约不能因为加了字段就破。
MOD_ARGS: dict[str, tuple[str, ...]] = {
    "turn_start": ("turn",),
    "turn_end": ("turn",),
    "unit_destroyed": ("unit",),
    "unit_damaged": ("unit", "damage", "by"),
    "facility_struck": ("tile", "damage", "by"),
    "facility_captured": ("side", "x", "y", "by"),
    "objective_captured": ("side", "x", "y", "by"),
}


@dataclass(frozen=True)
class GameEvent:
    """一条已经发生的事实。"""

    name: str
    turn: int
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __str__(self) -> str:
        bits = " ".join(f"{k}={_brief(v)}" for k, v in self.data.items())
        return f"[T{self.turn}] {self.name} {bits}"


def _brief(value: Any, limit: int = 24) -> str:
    """事件日志里别把整个单位对象打印出来。"""
    text = getattr(value, "name", None) or repr(value)
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- #
# 总线
# --------------------------------------------------------------------------- #
class EventBus:
    """一个对局的事件分发器。

    **错误隔离**：单个订阅者抛异常不能中断引擎——那意味着一个写坏的模组
    可以让整局崩掉。异常收进 :attr:`errors`，引擎照常往下走。
    """

    def __init__(self, mods: Any = None, history: int = 96) -> None:
        self.mods = mods
        self.history: deque[GameEvent] = deque(maxlen=history)
        self.counts: Counter[str] = Counter()
        self.errors: list[str] = []
        self._subs: dict[str, list[Callable[[Any, GameEvent], None]]] = {}

    # -- 订阅 -------------------------------------------------------------
    def subscribe(self, event: str, fn: Callable[[Any, GameEvent], None]) -> None:
        """订阅某事件；``event="*"`` 订阅全部。"""
        self._subs.setdefault(event, []).append(fn)

    def unsubscribe(self, event: str, fn: Callable[[Any, GameEvent], None]) -> bool:
        lst = self._subs.get(event)
        if not lst or fn not in lst:
            return False
        lst.remove(fn)
        return True

    def subscriber_count(self, event: str | None = None) -> int:
        if event is None:
            return sum(len(v) for v in self._subs.values())
        return len(self._subs.get(event, ()))

    # -- 投递 -------------------------------------------------------------
    def emit(self, event: str, state: Any, **fields: Any) -> GameEvent:
        """投递一个事件。

        事件名或必填字段对不上时**直接抛 KeyError**：投递方只有引擎自己，
        拼错一个字段名应当立刻被发现，而不是变成一条永远没人收到的空广播。
        """
        required = CATALOGUE.get(event)
        if required is None:
            raise KeyError(f"未登记的事件：{event}"
                           f"（可用：{', '.join(sorted(CATALOGUE))}）")
        missing = [f for f in required if f not in fields]
        if missing:
            raise KeyError(f"事件 {event} 缺少字段：{', '.join(missing)}")

        ev = GameEvent(event, int(getattr(state, "turn_count", 0)), dict(fields))
        self.history.append(ev)
        self.counts[event] += 1

        # 1) 模组钩子：位置参数与 v3.0 的签名保持兼容
        if self.mods is not None:
            args = [fields[f] for f in MOD_ARGS.get(event, ())]
            try:
                self.mods.fire(event, state, *args)
            except Exception as exc:            # noqa: BLE001 - 兜底，绝不中断引擎
                self._record_error(f"{event} 模组分发异常：{type(exc).__name__}: {exc}")

        # 2) 本地订阅者：通配符先跑，再跑精确订阅
        for fn in (*self._subs.get("*", ()), *self._subs.get(event, ())):
            try:
                fn(state, ev)
            except Exception as exc:            # noqa: BLE001
                self._record_error(f"{event} 订阅者异常：{type(exc).__name__}: {exc}")
        return ev

    # -- 查询 -------------------------------------------------------------
    def recent(self, event: str | None = None, limit: int = 8) -> list[GameEvent]:
        """最近的若干条事件（可按名字过滤）。"""
        items = [e for e in self.history if event is None or e.name == event]
        return items[-limit:]

    def clear_history(self) -> None:
        self.history.clear()

    def _record_error(self, text: str) -> None:
        # 上限存在的意义：一个每帧都炸的订阅者不该把内存吃光
        if len(self.errors) < 64:
            self.errors.append(text)


__all__ = ["CATALOGUE", "MOD_ARGS", "GameEvent", "EventBus"]

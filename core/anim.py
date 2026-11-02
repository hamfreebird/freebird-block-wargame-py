"""战斗动画：逐帧表现的动画事件队列。

设计要点
--------
* **core 不依赖 pygame**：本模块只描述「什么动画、在哪里、持续多久」，
  具体绘制交给 `ui/mapview.py`。
* **动画与逻辑解耦**：引擎在结算战斗时投递给动画事件，渲染层按帧推进。
  即使动画被关闭或渲染跟不上，游戏逻辑完全不受影响。
* **可复现**：动画只影响观感，不参与任何命中/拦截判定，因此不影响
  平衡性与回放一致性。

动画类型
--------
| kind | 表现 |
|---|---|
| `trail` | 弹道拖尾：沿路径推进的彗尾，尾端逐渐淡出 |
| `burst` | 爆炸：扩散的冲击环 + 火花射线 + 闪光核心 |
| `intercept` | 拦截：环形涟漪 + 六向射线（表示拦截成功） |
| `smoke` | 烟幕：缓慢膨胀并降低不透明度的烟团 |
| `flash` | 命中闪光：目标位置的短促高亮 |
| `ping` | 探测脉冲：向外扩散的圆环 |
| `move` | 机动轨迹：单位移动留下的虚线 |
| `label` | 飘字：命中/拦截的浮动文字 |
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Anim:
    """一个动画事件。坐标为地图格坐标。"""

    kind: str
    a: tuple[float, float]
    b: tuple[float, float] | None = None
    ttl: float = 0.6          # 剩余时长
    life: float = 0.6         # 初始时长（用于计算进度）
    color: tuple[int, int, int] = (255, 200, 120)
    text: str = ""
    size: float = 1.0         # 尺寸倍率
    seed: float = 0.0         # 随机种子（让同型动画形状略有差异）


@dataclass
class AnimTrack:
    """动画队列管理器：持有事件、逐帧推进、剔除过期项。"""

    events: list[Anim] = field(default_factory=list)
    enabled: bool = True
    speed: float = 1.0        # 播放速度倍率（0 = 冻结，>1 = 快进）
    _seq: int = 0

    # -- 投递 -------------------------------------------------------------
    def add(self, kind: str, a, b=None, ttl: float = 0.6,
            color: tuple[int, int, int] = (255, 200, 120),
            text: str = "", size: float = 1.0) -> None:
        """投递一个动画事件。关闭动画时直接忽略。"""
        if not self.enabled:
            return
        self._seq += 1
        self.events.append(Anim(
            kind=kind, a=(float(a[0]), float(a[1])),
            b=None if b is None else (float(b[0]), float(b[1])),
            ttl=ttl, life=ttl, color=color, text=text, size=size,
            seed=(self._seq * 0.6180339887) % 1.0,
        ))

    # -- 推进 -------------------------------------------------------------
    def tick(self, dt: float) -> None:
        """推进动画。dt 为秒。"""
        if not self.events or self.speed <= 0.0:
            return
        step = dt * self.speed
        alive: list[Anim] = []
        for e in self.events:
            e.ttl -= step
            if e.ttl > 0.0:
                alive.append(e)
        self.events = alive

    def clear(self) -> None:
        self.events.clear()

    # -- 查询 -------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.events)

    def busy(self) -> bool:
        """是否还有动画在播放（用于战斗节奏控制）。"""
        return bool(self.events)


def trail_points(a: tuple[float, float], b: tuple[float, float],
                 progress: float, length: int = 8) -> list[tuple[float, float]]:
    """计算弹道拖尾的采样点。

    `progress` 为导弹沿弹道推进的比例（0 = 发射点，1 = 目标点）。
    返回从「最早」到「最新」的若干采样点，调用方按序号做透明度衰减。
    """
    if length < 2:
        return [b]
    pts: list[tuple[float, float]] = []
    head = progress
    for i in range(length):
        t = head - (length - 1 - i) * 0.035
        if t < 0.0:
            t = 0.0
        pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return pts


def burst_radius(progress: float, size: float = 1.0) -> float:
    """爆炸冲击环半径（格）。"""
    return (0.15 + progress * 0.85) * size


def burst_sparks(seed: float, count: int = 8) -> list[tuple[float, float]]:
    """爆炸火花方向（弧度）。用 seed 让不同爆炸形状略有差异。"""
    out: list[tuple[float, float]] = []
    base = seed * math.tau
    for i in range(count):
        ang = base + math.tau * i / count + math.sin(seed * 12.0 + i) * 0.18
        out.append((math.cos(ang), math.sin(ang)))
    return out


def smoke_offset(seed: float, index: int) -> tuple[float, float]:
    """烟团内部偏移（格），让烟幕看起来蓬松而非同心圆。"""
    ang = seed * math.tau + index * 2.39996      # 黄金角散布
    rad = 0.18 + 0.22 * ((seed * 7.0 + index * 0.37) % 1.0)
    return (math.cos(ang) * rad, math.sin(ang) * rad)


__all__ = ["Anim", "AnimTrack", "trail_points", "burst_radius", "burst_sparks",
           "smoke_offset"]

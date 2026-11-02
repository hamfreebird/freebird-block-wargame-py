"""对局级可复现随机流。

原先所有随机判定都走全局 `random`，同一个种子跑两遍结果不同——
这让「回放任意一局」无从谈起。这里把随机数收敛到**对局实例**上：

* 对局开始时派生一个种子，规则层与 AI 的每一次随机判定都从
  :class:`GameRng` 取数；
* 同一个种子 + 同一串命令，重跑的结果逐项一致；
* :meth:`GameRng.calls` 记录已消耗的随机数次数，回放校验时用来确认
  两条对局走过了完全相同的判定路径。

用法::

    rng = GameRng(20261102)
    if rules.roll(0.7, rng):
        dmg = rules.jitter(prof.damage, rng=rng)

``rng`` 为 ``None`` 时退回全局 ``random``——保持既有调用点可用，
但**回放路径上的每一处都必须显式传入**。
"""
from __future__ import annotations

import random
from typing import Any, Sequence, TypeVar

T = TypeVar("T")


def new_seed() -> int:
    """生成一个新的对局种子。

    用系统熵源而非全局 ``random``——种子的质量取决于熵，而不是
    上一局玩到了第几手。
    """
    return random.SystemRandom().randrange(1, 1 << 31)


class GameRng:
    """对局随机流。

    包装 :class:`random.Random`，额外记录已消耗的随机数次数。
    """

    __slots__ = ("seed", "_rng", "calls")

    def __init__(self, seed: int | None = None):
        self.seed = int(seed) if seed is not None else new_seed()
        self._rng = random.Random(self.seed)
        self.calls = 0

    # -- 基础分布 ---------------------------------------------------------
    def random(self) -> float:
        """``[0, 1)`` 均匀分布。"""
        self.calls += 1
        return self._rng.random()

    def uniform(self, a: float, b: float) -> float:
        self.calls += 1
        return self._rng.uniform(a, b)

    def randint(self, a: int, b: int) -> int:
        self.calls += 1
        return self._rng.randint(a, b)

    def choice(self, seq: Sequence[T]) -> T:
        self.calls += 1
        return self._rng.choice(seq)

    def shuffle(self, x: list[Any]) -> None:
        self.calls += 1
        self._rng.shuffle(x)

    # -- 状态序列化 -------------------------------------------------------
    def getstate(self) -> dict[str, Any]:
        """完整状态（可 JSON 序列化）。

        ``random`` 的内部状态必须完整保存——只记种子无法还原
        「已经消耗了 N 次随机数」之后的序列，因为不同分布消耗的
        随机数个数不同。
        """
        version, internal, gauss_next = self._rng.getstate()
        return {
            "seed": self.seed,
            "calls": self.calls,
            "version": version,
            "internal": list(internal),
            "gauss_next": gauss_next,
        }

    def setstate(self, data: dict[str, Any]) -> None:
        """从 :meth:`getstate` 的结果恢复。"""
        self.seed = int(data.get("seed", 0))
        self.calls = int(data.get("calls", 0))
        version = int(data.get("version", 3))
        internal = tuple(int(v) for v in data.get("internal", ()))
        gauss_next = data.get("gauss_next")
        self._rng = random.Random(self.seed)
        self._rng.setstate((version, internal, gauss_next))

    def clone(self) -> "GameRng":
        """复制一份独立流（分支试验用，不影响原流）。"""
        other = GameRng(self.seed)
        other.calls = self.calls
        other._rng.setstate(self._rng.getstate())
        return other

    def __repr__(self) -> str:   # pragma: no cover - 调试辅助
        return f"GameRng(seed={self.seed}, calls={self.calls})"


__all__ = ["GameRng", "new_seed"]

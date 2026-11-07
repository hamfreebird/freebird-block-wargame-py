"""概率判定与伤害浮动。

单独成模块是为了让**最底层的拦截解算**也能用：这两个函数原本在
``rules`` 里，而 ``adn``（防空解算）要用它们——若留在门面里就会形成
``adn`` → ``rules`` → ``adn`` 的循环。

**关于 ``rng=None`` 的兜底**（v3.3.1 收尾的技术债）
--------------------------------------------------
v2.0 引入 ``GameRng`` 时留了「不传 rng 就退回全局 ``random``」的分支，
方便尚未接入对局流的旧代码。问题在于这条路径**不受对局种子控制**，
而且是静默的——谁忘了传 rng，只有等到「同种子两次跑出不同结果」
才会暴露。

现在兜底仍然保留（第三方模组可能还这么调），但代价变得可观测：

* 每次走兜底都会给 :data:`FALLBACK_CALLS` 计数；
* :func:`set_strict` 打开后直接抛 ``RuntimeError``，测试用它把「忘记传」
  变成一次立刻失败的断言；
* ``tools/diag.py`` 的不变量 L26 要求一局推演结束后计数仍为 0——
  引擎自己绝不能走这条路。
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .rng import GameRng

#: 走全局 ``random`` 兜底的次数。引擎侧一局结束必须仍为 0。
FALLBACK_CALLS = 0
_STRICT = False


def set_strict(on: bool = True) -> None:
    """打开/关闭严格模式：严格模式下「不传 rng」直接抛错。

    默认关闭——模组作者可能还在用旧写法，一开就炸不是补丁版该做的事。
    自检脚本会打开它。
    """
    global _STRICT
    _STRICT = on


def is_strict() -> bool:
    """当前是否处于严格模式（自检脚本用它确认开关生效）。"""
    return _STRICT


def reset_counters() -> None:
    """把兜底计数清零（自检脚本在每局开始前调用）。"""
    global FALLBACK_CALLS
    FALLBACK_CALLS = 0


def _fallback(api: str) -> None:
    """走兜底前的守门人：计数，必要时直接拒绝。"""
    global FALLBACK_CALLS
    if _STRICT:
        raise RuntimeError(
            f"{api}() 未传 rng：这条路径不受对局种子控制，结果无法复现。"
            f"调用方应传 state.rng 或 GameRng(...)。")
    FALLBACK_CALLS += 1


def roll(p: float, rng: "GameRng | None" = None) -> bool:
    """概率判定。

    ``rng`` 为对局随机流时结果可复现；为 ``None`` 时退回全局 ``random``
    并记一次兜底（见模块说明）。
    """
    if rng is None:
        _fallback("roll")
        return random.random() < p
    return rng.random() < p


def jitter(amount: float, spread: float = 0.15,
           rng: "GameRng | None" = None) -> float:
    """伤害浮动。

    ``rng`` 语义同 :func:`roll`。
    """
    if rng is None:
        _fallback("jitter")
        return amount * random.uniform(1.0 - spread, 1.0 + spread)
    return amount * rng.uniform(1.0 - spread, 1.0 + spread)


__all__ = ["roll", "jitter", "FALLBACK_CALLS", "set_strict", "is_strict",
           "reset_counters"]

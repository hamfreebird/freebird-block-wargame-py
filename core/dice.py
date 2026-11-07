"""概率判定与伤害浮动。

单独成模块是为了让**最底层的拦截解算**也能用：这两个函数原本在
``rules`` 里，而 ``adn``（防空解算）要用它们——若留在门面里就会形成
``adn`` → ``rules`` → ``adn`` 的循环。
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .rng import GameRng

def roll(p: float, rng: "GameRng | None" = None) -> bool:
    """概率判定。

    ``rng`` 为对局随机流时结果可复现；为 ``None`` 时退回全局
    ``random``（仅供尚未接入对局流的旧代码路径使用）。
    """
    if rng is None:
        return random.random() < p
    return rng.random() < p


def jitter(amount: float, spread: float = 0.15,
           rng: "GameRng | None" = None) -> float:
    """伤害浮动。

    ``rng`` 语义同 :func:`roll`。
    """
    if rng is None:
        return amount * random.uniform(1.0 - spread, 1.0 + spread)
    return amount * rng.uniform(1.0 - spread, 1.0 + spread)

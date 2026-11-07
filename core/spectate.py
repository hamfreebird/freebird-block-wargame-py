"""AI 对战观战：两个 AI 打完整一局，玩家只看。

**为什么不做成「AI 接管玩家回合」**：那需要给引擎加一套「谁是 AI」的
双份状态，而引擎里大量逻辑按 `player_side / ai_side` 二分（相位、探测、
行动权限）。与其在引擎里开第三条路，不如在**外层**驱动：

* 玩家方相位 → 用 `AIPlanner(player_side)` 出动作，走 `apply_ai_action`
  这同一个入口执行；
* AI 方相位 → 用引擎原有的 `begin_ai_phase` / `ai_step` / `finish_ai_turn`。

`apply_ai_action` 只认 `unit_uid`，不检查阵营，所以玩家方的 AI 动作
能复用同一条执行路径——不存在「观战版规则和实战版略有不同」的偏差。

**可观战 = 可复现**：整局只由「想定 + 种子」决定（AI 的随机判定全部
取自 `state.rng`），因此观战回放只需要存种子，不必存动作流。
"""
from __future__ import annotations

from typing import Any

from .ai import AIPlanner
from .engine import GameState
from .enums import Side


class SpectatorMatch:
    """一场 AI 对 AI 的推演，按「一个动作」为粒度推进。"""

    #: 单次 step 内最多跨几次相位切换（防御性上限，正常只需 1~2 次）
    _GUARD = 8

    def __init__(self, scenario, seed: int | None = None,
                 player_side: Side = Side.BLUE, deep: bool = True):
        self.state = GameState(scenario, player_side, seed=seed)
        # 观战：界面不再按某一方的探测结果裁剪视野（不影响任何规则判定）
        self.state.spectator = True
        self.player_side = player_side
        self.deep = deep
        self._planner = AIPlanner(self.state, player_side, deep=deep)
        self.actions = 0
        self.finished = False
        #: 最近一个动作（供界面显示「正在演什么」与镜头跟随）
        self.last: dict[str, Any] = {}
        #: 各阵营行动计数——用于战后简报「谁更主动」
        self.action_count: dict[str, int] = {Side.BLUE.value: 0, Side.RED.value: 0}

    # ------------------------------------------------------------------ #
    # 推进
    # ------------------------------------------------------------------ #
    def step(self) -> dict[str, Any] | None:
        """推进到「下一个动作已执行」为止；返回 None 表示整局结束。

        相位切换（玩家方用完 → AI 方 → 下一回合）在内部消化，
        因此调用方只需按固定节奏反复调用——不必知道回合是怎么翻的。
        """
        st = self.state
        if self.finished:
            return None
        guard = 0
        while guard < self._GUARD:
            guard += 1
            if st.over:
                break
            if st.phase == "player":
                act = self._planner.step(st)
                if act is not None:
                    return self._apply(act)
                # 玩家方这一相位打完了：结算导弹、进入 AI 相位
                st.end_player_turn()
                if st.over:
                    break
                st.begin_ai_phase()
                self._planner = AIPlanner(st, st.ai_side, deep=self.deep)
            else:
                act = self._planner.step(st)
                if act is not None:
                    return self._apply(act)
                st.finish_ai_turn()
                if st.over:
                    break
                self._planner = AIPlanner(st, st.player_side, deep=self.deep)
        self.finished = st.over
        return None

    def _apply(self, act) -> dict[str, Any]:
        st = self.state
        unit = st.unit_by_uid(act.unit_uid)
        ok = st.apply_ai_action(act)
        self.actions += 1
        side = unit.side if unit is not None else None
        if side is not None:
            self.action_count[side.value] = self.action_count.get(side.value, 0) + 1
        info = {
            "kind": act.kind,
            "ok": bool(ok),
            "uid": act.unit_uid,
            "unit": unit.name if unit is not None else "",
            "side": side.value if side is not None else "",
            "side_label": side.label if side is not None else "",
            "text": act.note or act.kind,
            "grid": self._grid_of(act, unit),
        }
        self.last = info
        self.finished = st.over
        return info

    @staticmethod
    def _grid_of(act, unit) -> tuple[int, int] | None:
        """动作发生的格子——镜头跟随用。"""
        if act.path:
            return tuple(act.path[-1])
        if act.tile is not None:
            return tuple(act.tile)
        if act.target_uid is not None and unit is not None:
            return (unit.x, unit.y)
        if unit is not None:
            return (unit.x, unit.y)
        return None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def turn(self) -> int:
        return int(self.state.turn_count)

    @property
    def progress(self) -> float:
        limit = max(1, int(self.state.turn_limit or 1))
        return min(1.0, self.turn / limit)

    def summary(self) -> dict[str, Any]:
        """终局摘要（用于观战结束后的提示行）。"""
        st = self.state
        return {
            "turn": st.turn_count,
            "winner": st.winner.value if st.winner else "",
            "reason": st.win_reason,
            "vp_blue": st.vp[Side.BLUE],
            "vp_red": st.vp[Side.RED],
            "actions": self.actions,
            "blue_actions": self.action_count.get(Side.BLUE.value, 0),
            "red_actions": self.action_count.get(Side.RED.value, 0),
        }

    def run_to_end(self, max_steps: int = 60000) -> Any:
        """无界面跑完整局（`--sim` 与自检用）。返回结束时的对局状态。"""
        n = 0
        while n < max_steps:
            n += 1
            if self.step() is None:
                break
        return self.state


__all__ = ["SpectatorMatch"]

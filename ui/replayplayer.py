"""回放播放器。

把一份回放重新演一遍：

* **普通对局**（`.kind == "match"`）——按命令流逐条重演；
* **观战回放**（`.kind == "ai_vs_ai"`）——没有命令流，只有种子，
  用 :class:`core.spectate.SpectatorMatch` 把两个 AI 重新跑一遍。

两种回放共用同一个播放器与同一套渲染（App 把 ``player.state`` 挂到
``self.state`` 上，界面代码完全不用改），区别只在「怎么推进」。
"""
from __future__ import annotations

import os
from typing import Any

from core.commands import CommandLog
from core.engine import GameState
from core.enums import Side
from core.replay import KIND_AI_VS_AI, ReplayFile, apply_command
from core.spectate import SpectatorMatch


class ReplayPlayer:
    """按固定节奏重放命令流 / AI 对局。"""

    #: 每条命令之间的间隔（秒）
    STEP_INTERVAL = 0.75

    def __init__(self, scenario, replay: ReplayFile):
        self.scenario = scenario
        self.replay = replay
        self.match: SpectatorMatch | None = None
        self.index = 0
        self.acc = 0.0
        self.speed = 1.0
        self.paused = False
        self.finished = False
        #: 本次播放实际执行的命令数（用于核对）
        self.executed = 0
        self.failed = 0
        if replay.kind == KIND_AI_VS_AI:
            # 观战回放：整局由「想定 + 种子」唯一决定，所以什么都不用存
            self.commands = CommandLog()
            self.match = SpectatorMatch(scenario, seed=replay.seed,
                                        player_side=Side(replay.player_side))
            self.state = self.match.state
            self._last: dict[str, Any] = {}
        else:
            self.commands = CommandLog.from_list(replay.commands)
            self.state = GameState(scenario, Side(replay.player_side),
                                   seed=replay.seed)
            self._last = {}

    # -- 推进 -------------------------------------------------------------
    def update(self, dt: float) -> None:
        if self.finished or self.paused:
            return
        self.acc += dt * max(0.1, self.speed)
        while self.acc >= self.STEP_INTERVAL:
            self.acc -= self.STEP_INTERVAL
            if not self.step():
                break

    def step(self) -> bool:
        """执行下一步；返回 False 表示已播完。"""
        if self.match is not None:
            info = self.match.step()
            if info is None:
                self.finished = True
                return False
            self.executed += 1
            self._last = info
            return True
        if self.index >= len(self.commands):
            self.finished = True
            return False
        cmd = self.commands[self.index]
        self.index += 1
        self.executed += 1
        try:
            if not apply_command(self.state, cmd):
                self.failed += 1
        except Exception:
            # 单条命令失败不该中断整场回放
            self.failed += 1
        if self.index >= len(self.commands):
            self.finished = True
        return True

    def skip_to_end(self) -> None:
        """直接跳到结尾（快进）。"""
        guard = 0
        while not self.finished and guard < 60000:
            self.step()
            guard += 1

    # -- 状态 -------------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.commands)

    @property
    def progress(self) -> float:
        if self.match is not None:
            return self.match.progress
        if not self.total:
            return 1.0
        return min(1.0, self.index / self.total)

    def status_line(self) -> str:
        """进度文本（左侧）。"""
        if self.match is not None:
            st = self.state
            # 终局那次推进会让回合数比上限多 1，显示上截住，避免「第 19/18 回合」
            turn = min(st.turn_count, max(1, int(st.turn_limit or 1)))
            return (f"第 {turn}/{st.turn_limit} 回合"
                    f" · 已演 {self.executed} 个动作")
        return f"{self.index}/{self.total}"

    def current_label(self) -> str:
        """当前正在演什么——用于在界面上说明。"""
        if self.match is not None:
            info = self._last
            if not info:
                return "准备就绪"
            side = info.get("side_label") or ""
            return f"{side}{info.get('unit', '')}· {info.get('text', '')}"[:64]
        if self.index == 0 or self.index > len(self.commands):
            return "准备就绪"
        cmd = self.commands[self.index - 1]
        a = cmd.args
        kind = cmd.kind
        if kind == "move":
            return f"机动 → ({a.get('x')}, {a.get('y')})"
        if kind == "fire":
            return f"开火 → 单位 {a.get('target')}"
        if kind == "fire_facility":
            return f"打击设施 ({a.get('x')}, {a.get('y')})"
        if kind == "end_turn":
            return "结束回合"
        if kind == "radar":
            return "雷达开关"
        if kind == "jam":
            return "干扰开关"
        if kind == "datalink":
            return "数据链开关"
        if kind == "fortify":
            return "构筑工事"
        if kind == "decoy":
            return f"投放诱饵（{a.get('kind')}）"
        if kind == "smoke":
            return "施放烟幕"
        if kind == "profile":
            return f"任务剖面 → {a.get('mission')}"
        if kind == "prepare":
            return f"登记射界 ({a.get('x')}, {a.get('y')})"
        if kind == "order":
            return "下达指令"
        if kind == "hold":
            return "待机"
        return kind


def list_replays(directory: str) -> list[dict[str, Any]]:
    """扫描回放目录。

    损坏或读不了的文件也要列出来（标记为不可读），不静默吞掉——
    玩家需要知道自己的回放文件出了什么问题。
    """
    out: list[dict[str, Any]] = []
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".bwrep"):
            continue
        path = os.path.join(directory, name)
        item: dict[str, Any] = {
            "name": name,
            "path": path,
            "scenario": "",
            "seed": 0,
            "commands": 0,
            "note": "",
            "kind": "match",
            "game_version": "",
            "mtime": 0,
            "readable": False,
            "size_kb": 0,
        }
        try:
            item["mtime"] = os.path.getmtime(path)
            item["size_kb"] = os.path.getsize(path) // 1024
        except OSError:
            pass
        try:
            rf = ReplayFile.load(path)
            item["scenario"] = rf.scenario
            item["seed"] = rf.seed
            item["commands"] = len(rf.commands)
            item["note"] = rf.note
            item["kind"] = rf.kind
            item["game_version"] = rf.game_version
            item["readable"] = True
        except Exception:
            pass
        out.append(item)
    out.sort(key=lambda it: it["mtime"], reverse=True)
    return out


__all__ = ["ReplayPlayer", "list_replays"]

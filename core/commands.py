"""对局命令流。

玩家的每一个操作都被记成一条**语义化**命令——记「把 3 号单位移到
(10,20)」而不是「鼠标点在 (412, 233)」。回放不依赖窗口大小、分辨率
或任何 UI 细节，只依赖：想定 + 对局种子 + 命令序列。

命令的 ``args`` 里一律存**标识符**（单位 uid、武器 id、格坐标），
不存对象引用——这样命令流可以直接 JSON 化存盘。

配套的执行器在 :mod:`core.replay`；UI 层与回放器共用同一套
:func:`core.replay.apply_command`，保证「玩家点的」与「回放跑的」
走的是同一条代码路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# 命令类型
# --------------------------------------------------------------------------- #
CMD_SELECT = "select"                # 选中单位（回放时可忽略）
CMD_MOVE = "move"                    # 机动到目标格
CMD_FIRE = "fire"                    # 对单位开火
CMD_FIRE_FACILITY = "fire_facility"  # 打击设施
CMD_END_TURN = "end_turn"            # 结束回合
CMD_RADAR = "radar"                  # 雷达开关
CMD_JAM = "jam"                      # 干扰开关
CMD_FORTIFY = "fortify"              # 构筑工事
CMD_DECOY = "decoy"                  # 投放诱饵
CMD_SMOKE = "smoke"                  # 发射烟幕
CMD_HOLD = "hold"                    # 待机
CMD_PROFILE = "profile"              # 切换任务剖面
CMD_PREPARE = "prepare"              # 火力准备登记
CMD_DATALINK = "datalink"            # 数据链开关
CMD_ORDER = "order"                  # 指挥：下达指令

ALL_KINDS = (
    CMD_SELECT, CMD_MOVE, CMD_FIRE, CMD_FIRE_FACILITY, CMD_END_TURN,
    CMD_RADAR, CMD_JAM, CMD_FORTIFY, CMD_DECOY, CMD_SMOKE, CMD_HOLD,
    CMD_PROFILE, CMD_PREPARE, CMD_DATALINK, CMD_ORDER,
)


@dataclass(frozen=True)
class Command:
    """一条玩家操作。"""

    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "args": dict(self.args), "turn": self.turn}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Command":
        return Command(
            kind=str(data.get("kind", "")),
            args=dict(data.get("args") or {}),
            turn=int(data.get("turn", 0)),
        )

    def __repr__(self) -> str:   # pragma: no cover - 调试辅助
        return f"Command({self.kind}, {self.args}, turn={self.turn})"


class CommandLog:
    """命令序列。

    对局开始时新建，玩家每做一步就追加一条。存盘时整体写出，
    回放时整体读入。
    """

    def __init__(self, commands: Iterable[Command] | None = None):
        self.commands: list[Command] = list(commands or ())

    # -- 记录 -------------------------------------------------------------
    def record(self, cmd_kind: str, turn: int = 0, **args: Any) -> Command:
        """追加一条命令。

        第一个参数刻意不叫 ``kind``——``args`` 里可能也有叫 ``kind``
        的键（比如诱饵类型 radar/ir/visual），重名会撞车。
        """
        cmd = Command(cmd_kind, args, turn)
        self.commands.append(cmd)
        return cmd

    def clear(self) -> None:
        self.commands.clear()

    # -- 序列化 -----------------------------------------------------------
    def to_list(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.commands]

    @staticmethod
    def from_list(data: Iterable[dict[str, Any]]) -> "CommandLog":
        return CommandLog(Command.from_dict(d) for d in data)

    # -- 查询 -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.commands)

    def __iter__(self):
        return iter(self.commands)

    def __getitem__(self, index):
        return self.commands[index]

    def of_kind(self, kind: str) -> list[Command]:
        return [c for c in self.commands if c.kind == kind]

    def summary(self) -> dict[str, int]:
        """按类型统计——回放结束后用来核对「命令是否都执行了」。"""
        out: dict[str, int] = {}
        for c in self.commands:
            out[c.kind] = out.get(c.kind, 0) + 1
        return out


__all__ = [
    "Command", "CommandLog", "ALL_KINDS",
    "CMD_SELECT", "CMD_MOVE", "CMD_FIRE", "CMD_FIRE_FACILITY", "CMD_END_TURN",
    "CMD_RADAR", "CMD_JAM", "CMD_FORTIFY", "CMD_DECOY", "CMD_SMOKE",
    "CMD_HOLD", "CMD_PROFILE", "CMD_PREPARE", "CMD_DATALINK", "CMD_ORDER",
]

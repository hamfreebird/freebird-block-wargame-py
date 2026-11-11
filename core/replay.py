"""对局回放。

回放 = 想定 + 对局种子 + 命令流。种子让随机判定可复现，命令流让
玩家的每一步可重演。两者合起来，任何一局都能被完整重演。

**执行路径与玩家完全一致**：:func:`apply_command` 是 UI 层与回放器
共用的唯一入口——玩家点「开火」和回放跑到「开火」，走的是同一行
代码，不存在「回放版本的行为略有不同」这种偏差。

回放文件是纯 JSON（想定路径 + 种子 + 命令列表），可以直接分享、
存档、或嵌进攻报。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .commands import (
    CMD_DATALINK, CMD_DECOY, CMD_END_TURN, CMD_FIRE, CMD_FIRE_FACILITY,
    CMD_FORTIFY, CMD_HOLD, CMD_JAM, CMD_MOVE, CMD_ORDER, CMD_PREPARE,
    CMD_PROFILE, CMD_RADAR, CMD_SELECT, CMD_SMOKE, Command, CommandLog,
)
from .enums import Side
from .rules import find_path
from .version import GAME_VERSION, compat_note

#: v3：结束回合命令里可带 ``directive``（大模型指挥官给的作战决心）。
#: 为什么必须升版本：旧程序读到 v3 文件会**静默换一套 AI 行为**重演
#: （它不认识 directive，退回启发式），玩家看到的回放与当时的对局不同。
#: 宁可让它明确拒绝，也不要给一份「看起来能放、其实不一样」的回放。
REPLAY_VERSION = 3

#: 玩家/AI 混编的普通对局（命令流）
KIND_MATCH = "match"
#: AI 对 AI 的观战（只存种子——整局由「想定 + 种子」唯一决定）
KIND_AI_VS_AI = "ai_vs_ai"


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #
def _unit_by_uid(state, uid: int):
    for u in state.units:
        if u.uid == uid:
            return u
    return None


def apply_command(state, cmd: Command) -> bool:
    """把一条命令应用到对局状态。

    返回命令是否**成功执行**。失败不抛异常——回放要能容忍
    「命令在当前状态下不可执行」（比如目标已被消灭），
    否则一局回放会因为一条失效命令整个崩掉。
    """
    kind = cmd.kind
    a = cmd.args

    if kind == CMD_SELECT:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        state.select(u)
        return True

    if kind == CMD_MOVE:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None or not u.alive:
            return False
        gx, gy = int(a.get("x", 0)), int(a.get("y", 0))
        path = find_path(state, u, (gx, gy))
        if not path:
            return False
        return bool(state.move_unit(u, path))

    if kind == CMD_FIRE:
        attacker = _unit_by_uid(state, int(a.get("attacker", 0)))
        target = _unit_by_uid(state, int(a.get("target", 0)))
        if attacker is None or target is None:
            return False
        w = attacker.weapon_by_id(str(a.get("weapon", "")))
        if w is None:
            return False
        salvo = a.get("salvo")
        res = state.fire(attacker, target, w, int(salvo) if salvo else None)
        return bool(res.ok)

    if kind == CMD_FIRE_FACILITY:
        attacker = _unit_by_uid(state, int(a.get("attacker", 0)))
        if attacker is None:
            return False
        w = attacker.weapon_by_id(str(a.get("weapon", "")))
        if w is None:
            return False
        pos = (int(a.get("x", 0)), int(a.get("y", 0)))
        res = state.strike_facility(attacker, pos, w)
        return bool(res.ok)

    if kind == CMD_END_TURN:
        # 大模型给的决心随命令一起回放：AI 的决策是「外部输入」，
        # 不记下来就没法复现那一回合。（旧回放没有这个字段，退回启发式）
        raw = a.get("directive")
        if isinstance(raw, dict):
            from .llm_commander import Directive
            state.pending_directive = Directive.from_dict(raw)
        run_ai_turn(state)
        return True

    if kind == CMD_RADAR:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        state.toggle_radar(u)
        return True

    if kind == CMD_JAM:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        state.toggle_jam(u)
        return True

    if kind == CMD_FORTIFY:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        return bool(state.build_fort(u))

    if kind == CMD_DECOY:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        pos = (int(a.get("x", 0)), int(a.get("y", 0)))
        return bool(state.deploy_decoy(u, pos, str(a.get("kind", "radar"))))

    if kind == CMD_SMOKE:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        w = u.weapon_by_id(str(a.get("weapon", "")))
        if w is None:
            return False
        pos = (int(a.get("x", 0)), int(a.get("y", 0)))
        return bool(state.fire_smoke(u, pos, w))

    if kind == CMD_HOLD:
        # 待机：不改变任何状态，只消耗单位的本回合行动
        return True

    if kind == CMD_PROFILE:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        mission = str(a.get("mission", ""))
        if not mission:
            return False
        return bool(state.set_mission(u, mission))

    if kind == CMD_DATALINK:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        state.toggle_datalink(u)
        return True

    if kind == CMD_ORDER:
        commander = _unit_by_uid(state, int(a.get("commander", 0)))
        if commander is None:
            return False
        target = None
        tgt_uid = int(a.get("target", 0))
        if tgt_uid:
            target = _unit_by_uid(state, tgt_uid)
            if target is None:
                return False
        ok, _reason = state.issue_order(commander, str(a.get("order", "")), target)
        return bool(ok)

    if kind == CMD_PREPARE:
        u = _unit_by_uid(state, int(a.get("uid", 0)))
        if u is None:
            return False
        pos = (int(a.get("x", 0)), int(a.get("y", 0)))
        ok, _msg = state.register_fire_mission(u, pos)
        return bool(ok)

    return False


def run_ai_turn(state) -> None:
    """完整跑完 AI 回合。

    UI 里 AI 动作是逐帧推进的（一次一个动作），这里一次跑完——
    动作顺序与 UI 完全一致（AIPlanner 的队列顺序），
    因此随机数消耗序列也一致。
    """
    if state.phase != "player" or state.over:
        return
    state.end_player_turn()
    if state.over:
        return
    state.begin_ai_phase()
    guard = 0
    while state.ai_step() is not None:
        guard += 1
        if guard > 4000:      # 防御性上限：AI 不应死循环
            break
    state.finish_ai_turn()


# --------------------------------------------------------------------------- #
# 回放文件
# --------------------------------------------------------------------------- #
class ReplayError(Exception):
    """回放读写 / 还原失败。"""


@dataclass
class ReplayFile:
    """一份可分享的回放。

    **v3.2 起回放自带想定**：只记想定文件名的回放，换台机器就打不开——
    对方没有那张图。导出时把想定 JSON 一并内嵌（并记下 sha256），
    接收方即使没有这张图也能原样重演。
    """

    scenario: str                       # 想定文件名（如 strait_shield.json）
    seed: int
    player_side: str = "BLUE"
    commands: list[dict[str, Any]] = field(default_factory=list)
    version: int = REPLAY_VERSION
    note: str = ""
    #: 回放类型：match（命令流）/ ai_vs_ai（观战，只有种子）
    kind: str = KIND_MATCH
    #: 制作该回放的程序版本
    game_version: str = ""
    #: 想定指纹——判断本地同名想定是否就是当初那一份
    scenario_sha256: str = ""
    #: 内嵌想定（分享时为字典；旧文件 / 未内嵌为 None）
    scenario_data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "scenario": self.scenario,
            "scenario_sha256": self.scenario_sha256,
            "game_version": self.game_version,
            "seed": self.seed,
            "player_side": self.player_side,
            "note": self.note,
            "commands": list(self.commands),
            "scenario_data": self.scenario_data,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ReplayFile":
        if not isinstance(data, dict):
            raise ReplayError("回放文件格式错误：顶层应为对象")
        embedded = data.get("scenario_data")
        if embedded is not None and not isinstance(embedded, dict):
            embedded = None
        return ReplayFile(
            scenario=str(data.get("scenario", "")),
            seed=int(data.get("seed", 0)),
            player_side=str(data.get("player_side", "BLUE")),
            commands=list(data.get("commands") or []),
            version=int(data.get("version", 1)),
            note=str(data.get("note", "")),
            kind=str(data.get("kind", KIND_MATCH)),
            game_version=str(data.get("game_version", "")),
            scenario_sha256=str(data.get("scenario_sha256", "")),
            scenario_data=embedded,
        )

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def save_shared(self, path: str) -> None:
        """导出为**可直接分享**的文件（缺想定时自动内嵌）。

        与 :meth:`save` 的差别只是「想定在不在包里」。分成两个方法，
        是为了让「本地存档」与「发给别人」这两种意图在代码里也分得清。
        """
        if self.scenario_data is None:
            raise ReplayError(
                "该回放没有内嵌想定数据，无法独立分享；"
                "请从对局中重新导出（导出时会自动内嵌）")
        self.save(path)

    @staticmethod
    def load(path: str) -> "ReplayFile":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayError(f"读取回放失败：{exc}") from None
        rf = ReplayFile.from_dict(data)
        if rf.version > REPLAY_VERSION:
            raise ReplayError(
                f"回放格式版本 {rf.version} 高于当前程序支持的 {REPLAY_VERSION}，"
                f"请更新游戏")
        return rf

    # -- 内嵌想定 -------------------------------------------------------
    def embed_scenario(self, path: str) -> None:
        """把想定文件读进回放（含指纹），失败时保持原样。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.scenario_data = raw
        self.scenario_sha256 = _sha256_json(raw)

    def materialize_scenario(self, search_dirs, writable_dir: str
                             ) -> tuple[str, str]:
        """找到（或还原）本回放所需的想定，返回 ``(路径, 提示)``。

        优先级：**内容一致**的本地文件 → 内嵌想定 → 同名的本地文件（带告警）。
        只按文件名匹配是危险的：同名不同版的想定会让回放演出另一局。
        """
        from .pkg import sha256_file

        name = os.path.basename(self.scenario or "")
        if not name:
            raise ReplayError("回放没有记录想定名称")

        found: str | None = None
        for d in list(search_dirs) + [writable_dir]:
            if not d:
                continue
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                found = cand
                break

        if found is not None:
            if not self.scenario_sha256:
                return found, ""
            try:
                same = sha256_file(found) == self.scenario_sha256
            except OSError:
                same = False
            if same:
                return found, ""
            if self.scenario_data is not None:
                path = self._restore_scenario(writable_dir, name)
                return path, "本地同名想定与录制时不一致，已改用回放内嵌的想定重演"
            return found, "本地同名想定与录制时不一致，观演结果可能与当初不同"

        if self.scenario_data is not None:
            path = self._restore_scenario(writable_dir, name)
            return path, "本机没有该想定，已从回放内嵌数据还原"
        raise ReplayError(f"本机没有回放所需的想定「{name}」，且该回放未内嵌想定数据")

    def _restore_scenario(self, writable_dir: str, name: str) -> str:
        """把内嵌想定写盘。文件名带上指纹前缀，避免覆盖玩家自己的同名想定。"""
        os.makedirs(writable_dir, exist_ok=True)
        stem, ext = os.path.splitext(name)
        tag = (self.scenario_sha256 or "replay")[:8]
        path = os.path.join(writable_dir, f"{stem}.{tag}{ext or '.json'}")
        if not os.path.isfile(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.scenario_data, f, ensure_ascii=False, indent=1)
        return path

    # -- 构造 -----------------------------------------------------------
    @staticmethod
    def from_game(state, scenario_name: str, log: CommandLog,
                  note: str = "", scenario_path: str = "") -> "ReplayFile":
        """从进行中的对局导出回放（默认内嵌想定，可直接分享）。"""
        rf = ReplayFile(
            scenario=scenario_name,
            seed=int(getattr(state, "seed", 0)),
            player_side=state.player_side.value,
            commands=log.to_list(),
            note=note,
            game_version=GAME_VERSION,
        )
        if scenario_path:
            rf.embed_scenario(scenario_path)
        return rf

    @staticmethod
    def from_spectate(state, scenario_name: str,
                      scenario_path: str = "", note: str = "") -> "ReplayFile":
        """观战回放：不存动作流，只存种子——AI 对局由种子唯一决定。"""
        rf = ReplayFile(
            scenario=scenario_name,
            seed=int(getattr(state, "seed", 0)),
            player_side=state.player_side.value,
            commands=[],
            note=note,
            kind=KIND_AI_VS_AI,
            game_version=GAME_VERSION,
        )
        rf.embed_scenario(scenario_path)
        return rf

    def compat_note(self) -> str:
        """版本差异提示（同版本 / 未记录时为空串）。"""
        return compat_note(self.game_version)


def _sha256_json(raw: dict) -> str:
    """对想定内容取指纹。

    按**规范化后的 JSON** 计算：同一份想定经编辑器另存后键序 / 缩进会变，
    直接对文件字节取哈希会把「同一张图」判成两份。
    """
    import hashlib
    blob = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# 回放器
# --------------------------------------------------------------------------- #
class Replayer:
    """按命令流重演一局。"""

    def __init__(self, scenario, seed: int, commands: Iterable[Command] | CommandLog,
                 player_side: Side = Side.BLUE):
        from .engine import GameState
        self._state_cls = GameState
        self.scenario = scenario
        self.seed = int(seed)
        self.commands = (commands if isinstance(commands, CommandLog)
                         else CommandLog(commands))
        self.player_side = player_side

    def run(self, max_turns: int = 200,
            on_command: Callable[[Command, bool], None] | None = None) -> Any:
        """重演整局，返回结束时的对局状态。"""
        state = self._state_cls(self.scenario, self.player_side, seed=self.seed)
        for cmd in self.commands:
            if state.over:
                break
            if state.turn_count > max_turns:
                break
            ok = apply_command(state, cmd)
            if on_command is not None:
                on_command(cmd, ok)
        return state


# --------------------------------------------------------------------------- #
# 结果比对
# --------------------------------------------------------------------------- #
def snapshot(state) -> dict[str, Any]:
    """对局状态快照——用于比对「回放结果」与「原局结果」。

    只取**决定性的**字段：胜负点、单位位置与耐久、弹药、日志。
    动画与音效队列是表现层，不参与比对。
    """
    return {
        "turn_count": state.turn_count,
        "vp": {s.value: v for s, v in state.vp.items()},
        "over": state.over,
        "winner": state.winner.value if state.winner else None,
        "units": [
            {
                "uid": u.uid,
                "name": u.name,
                "side": u.side.value,
                "x": u.x,
                "y": u.y,
                "hp": round(float(u.hp), 4),
                "alive": u.alive,
                "ammo": dict(u.ammo),
                "mp": round(float(u.mp), 4),
                "radar_on": u.radar_on,
                "jam_on": u.jam_on,
                "mission": u.mission,
            }
            for u in state.units
        ],
        "casualties": {s.value: v for s, v in state.casualties.items()},
        # 对局日志：教学提示（kind=tutorial）不参与比对——回放不跑教学系统，
        # 那类日志本就不该出现在回放里。
        "log": [e.text for e in state.log if e.kind != "tutorial"],
        "tutorial_log": [e.text for e in state.log if e.kind == "tutorial"],
    }


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """比对两份快照，返回差异描述列表（空 = 完全一致）。"""
    diffs: list[str] = []
    for key in ("turn_count", "vp", "over", "winner", "casualties"):
        if a.get(key) != b.get(key):
            diffs.append(f"{key}: {a.get(key)} ≠ {b.get(key)}")
    ua = {u["uid"]: u for u in a.get("units", [])}
    ub = {u["uid"]: u for u in b.get("units", [])}
    if set(ua) != set(ub):
        diffs.append(f"单位集合不同: {sorted(set(ua) ^ set(ub))}")
    else:
        for uid in sorted(ua):
            x, y = ua[uid], ub[uid]
            for f in ("x", "y", "hp", "alive", "ammo", "mp",
                      "radar_on", "jam_on", "mission"):
                if x.get(f) != y.get(f):
                    diffs.append(
                        f"单位 {uid}（{x.get('name')}）的 {f}: "
                        f"{x.get(f)} ≠ {y.get(f)}")
    la, lb = a.get("log", []), b.get("log", [])
    if la != lb:
        n = min(len(la), len(lb))
        first = next((i for i in range(n) if la[i] != lb[i]), n)
        diffs.append(f"日志不同（{len(la)} vs {len(lb)} 条，首个差异在第 {first} 条）")
        if first < n:
            diffs.append(f"  原局: {la[first]}")
            diffs.append(f"  回放: {lb[first]}")
    return diffs


__all__ = [
    "ReplayFile", "Replayer", "ReplayError", "REPLAY_VERSION",
    "KIND_MATCH", "KIND_AI_VS_AI",
    "apply_command", "run_ai_turn", "snapshot", "compare",
]

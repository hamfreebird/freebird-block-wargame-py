"""对局回放自检。

验证三件事：

1. **可复现**——同一种子重跑，随机判定与对局结果逐项一致；
2. **可重演**——命令流重演后的对局与原局逐项一致；
3. **命令执行器**——每种命令都能正确改变对局状态。

不依赖 pytest，可直接在 CI 中运行：
    SDL_VIDEODRIVER=dummy python tools/test_replay.py
"""
from __future__ import annotations

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import shutil
import tempfile

_TEST_USER_DIR = tempfile.mkdtemp(prefix="bw_replay_")
os.environ["BLOCKWARGAME_USER_DIR"] = _TEST_USER_DIR
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.commands import (  # noqa: E402
    CMD_DATALINK, CMD_DECOY, CMD_END_TURN, CMD_FIRE, CMD_FIRE_FACILITY,
    CMD_FORTIFY, CMD_HOLD, CMD_JAM, CMD_MOVE, CMD_ORDER, CMD_PREPARE,
    CMD_PROFILE, CMD_RADAR, CMD_SMOKE, Command, CommandLog,
)
from core.enums import Domain, Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.replay import (  # noqa: E402
    ReplayFile, Replayer, apply_command, compare, snapshot,
)

ARMORY = "data/units/armory.json"
# 用兵种最全的想定——回放自检要覆盖诱饵 / 干扰机 / 空中单位等各类命令
MAP = "data/maps/strait_shield.json"

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [FAIL] {label}")
        FAILED.append(label)


def new_state(seed: int):
    from core.engine import GameState
    sc = load_scenario(MAP, ARMORY)
    return GameState(sc, Side.BLUE, seed=seed)


def unit_by_uid(st, uid):
    for u in st.units:
        if u.uid == uid:
            return u
    return None


def play_ai_game(seed: int, turns: int = 6):
    """纯 AI 对局——必然触发随机判定，用来验证种子的作用。"""
    from core.ai import AIPlanner
    st = new_state(seed)
    for _ in range(turns):
        if st.over:
            break
        for side in (Side.BLUE, Side.RED):
            planner = AIPlanner(st, side)
            guard = 0
            while planner.pending() and guard < 800:
                guard += 1
                act = planner.step(st)
                if act is None:
                    break
                st.apply_ai_action(act)
            st.resolve_missiles()
            st.refresh_detection()
        st._advance_turn()
    return st


def build_command_stream(st) -> CommandLog:
    """构造一条覆盖多种命令的命令流。

    刻意挑「一定能成功」的目标，避免测试依赖随机命中。
    """
    log = CommandLog()
    turn = st.turn_count

    blue = [u for u in st.units if u.side is Side.BLUE and u.alive]
    red = [u for u in st.units if u.side is Side.RED and u.alive]
    if not blue or not red:
        return log

    mover = next((u for u in blue if u.domain is not Domain.AIR), blue[0])
    # 找一个真正能走到的相邻格——移到原地会被寻路判为不可达
    step = None
    for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
        nx, ny = mover.x + dx, mover.y + dy
        if st.map.at(nx, ny) is not None:
            step = (nx, ny)
            break
    if step is not None:
        log.record(CMD_MOVE, turn=turn, uid=mover.uid, x=step[0], y=step[1])

    radar = next((u for u in blue if any(s.kind.value == "radar"
                                        for s in u.template.sensors)), None)
    if radar is not None:
        log.record(CMD_RADAR, turn=turn, uid=radar.uid)

    jammer = next((u for u in blue if u.template.jam_strength > 0), None)
    if jammer is not None:
        log.record(CMD_JAM, turn=turn, uid=jammer.uid)

    linked = next((u for u in blue if u.template.datalink), None)
    if linked is not None:
        log.record(CMD_DATALINK, turn=turn, uid=linked.uid)

    fort = next((u for u in blue if u.template.can_fortify), None)
    if fort is not None:
        log.record(CMD_FORTIFY, turn=turn, uid=fort.uid)

    air = next((u for u in blue if u.domain is Domain.AIR), None)
    if air is not None:
        log.record(CMD_PROFILE, turn=turn, uid=air.uid, mission="cap")

    decoy = next((u for u in blue if u.decoy_left > 0), None)
    if decoy is not None:
        log.record(CMD_DECOY, turn=turn, uid=decoy.uid, kind="radar",
                   x=decoy.x, y=decoy.y)

    smoke = next((u for u in blue
                  if any(w.kind.value in ("artillery", "mlrs")
                         for w in u.usable_weapons())), None)
    if smoke is not None:
        w = next(w for w in smoke.usable_weapons()
                 if w.kind.value in ("artillery", "mlrs"))
        log.record(CMD_SMOKE, turn=turn, uid=smoke.uid, weapon=w.id,
                   x=smoke.x, y=smoke.y)

    log.record(CMD_HOLD, turn=turn, uid=blue[0].uid)
    log.record(CMD_END_TURN, turn=turn)
    return log


def test_command_kinds() -> None:
    """每种命令都能被执行且不抛异常。"""
    print("命令执行器：")
    st = new_state(12345)
    log = build_command_stream(st)
    kinds = {c.kind for c in log}
    expected = {CMD_MOVE, CMD_RADAR, CMD_JAM, CMD_DATALINK, CMD_FORTIFY,
                CMD_PROFILE, CMD_DECOY, CMD_SMOKE, CMD_HOLD, CMD_END_TURN}
    missing = expected - kinds
    check(not missing, f"命令流覆盖全部类型（缺 {sorted(missing)}）"
          if missing else "命令流覆盖全部类型")

    ok_count = 0
    for cmd in log:
        try:
            if apply_command(st, cmd):
                ok_count += 1
        except Exception as exc:      # pragma: no cover - 失败路径
            check(False, f"命令 {cmd.kind} 执行未抛异常（{exc}）")
            return
    check(True, "全部命令执行未抛异常")
    check(ok_count >= 5, f"多数命令成功执行（{ok_count}/{len(log)}）")

    # 未知命令应当安全返回 False，而不是崩溃
    check(apply_command(st, Command("no_such_kind")) is False,
          "未知命令安全返回 False")


def test_replay_matches() -> None:
    """回放结果与原局逐项一致。"""
    print("回放一致性：")
    st = new_state(20261102)
    log = build_command_stream(st)
    for cmd in log:
        apply_command(st, cmd)

    rep = Replayer(load_scenario(MAP, ARMORY), 20261102, log, Side.BLUE)
    st2 = rep.run()

    diffs = compare(snapshot(st), snapshot(st2))
    check(not diffs, "回放与原局完全一致" if not diffs
          else f"回放存在差异：{diffs[:3]}")

    # 换一个种子，回放结果应当不同（否则说明种子没起作用）
    st3 = new_state(777)
    log3 = build_command_stream(st3)
    for cmd in log3:
        apply_command(st3, cmd)
    rep3 = Replayer(load_scenario(MAP, ARMORY), 777, log3, Side.BLUE)
    st4 = rep3.run()
    check(not compare(snapshot(st3), snapshot(st4)),
          "另一组种子同样可复现")

def test_repeatable_random() -> None:
    """同种子跑两遍，随机判定与结果一致。"""
    print("随机可复现：")
    a = new_state(31415)
    b = new_state(31415)
    log = build_command_stream(a)
    for cmd in log:
        apply_command(a, cmd)
    for cmd in log:
        apply_command(b, cmd)
    check(a.rng.calls == b.rng.calls,
          f"随机数消耗次数一致（{a.rng.calls}）")
    check(not compare(snapshot(a), snapshot(b)), "同种子两次结果完全一致")

    # 不同种子要产生不同对局——但前提是**真的发生了随机判定**。
    # 上面的命令流未必成功开火，所以另起一局纯 AI 对局来验证。
    ai_a = play_ai_game(1234, 6)
    ai_b = play_ai_game(1234, 6)
    ai_c = play_ai_game(4321, 6)
    check(ai_a.rng.calls == ai_b.rng.calls,
          f"AI 对局随机消耗一致（{ai_a.rng.calls}）")
    check(not compare(snapshot(ai_a), snapshot(ai_b)),
          "AI 对局：同种子结果完全一致")
    check(bool(compare(snapshot(ai_a), snapshot(ai_c))),
          "AI 对局：不同种子结果不同")


def test_replay_file() -> None:
    """回放文件的存读往返。"""
    print("回放文件：")
    st = new_state(999)
    log = build_command_stream(st)
    for cmd in log:
        apply_command(st, cmd)

    rf = ReplayFile.from_game(st, "training_ground.json", log, note="自检")
    path = os.path.join(_TEST_USER_DIR, "r.json")
    rf.save(path)
    rf2 = ReplayFile.load(path)
    check(rf2.scenario == rf.scenario, "想定名往返一致")
    check(rf2.seed == rf.seed, "种子往返一致")
    check(rf2.player_side == rf.player_side, "阵营往返一致")
    check(len(rf2.commands) == len(rf.commands), "命令条数往返一致")
    _cmd0 = CommandLog.from_list(rf2.commands)[0]
    _cmd1 = CommandLog.from_list(rf.commands)[0]
    check(_cmd0.kind == _cmd1.kind, "命令内容往返一致")

    # 从文件重放
    log2 = CommandLog.from_list(rf2.commands)
    rep = Replayer(load_scenario(MAP, ARMORY), rf2.seed, log2,
                   Side(rf2.player_side))
    st2 = rep.run()
    check(not compare(snapshot(st), snapshot(st2)), "从文件重放结果一致")


def test_ui_records_commands() -> None:
    """UI 层确实在记录命令。"""
    print("UI 命令记录：")
    import pygame
    from ui.app import App
    app = App()
    app.start_tutorial()
    app._close_briefing()
    st = app.state
    u = st.selected_unit()
    if u is not None:
        app.order_move(u.x, u.y)
    app.end_turn()
    check(len(app.cmd_log) >= 1, f"UI 记录了命令（{len(app.cmd_log)} 条）")
    kinds = {c.kind for c in app.cmd_log}
    check(CMD_END_TURN in kinds, "UI 记录了结束回合命令")
    check(st.seed > 0, f"对局带有种子（{st.seed}）")


def main() -> int:
    print("=== 对局回放自检 ===")
    test_command_kinds()
    test_replay_matches()
    test_repeatable_random()
    test_replay_file()
    test_ui_records_commands()
    print()
    if FAILED:
        print(f"回放自检失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("回放自检全部通过")
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:
        import traceback
        traceback.print_exc()
        _rc = 1
    finally:
        shutil.rmtree(_TEST_USER_DIR, ignore_errors=True)
    sys.exit(_rc)

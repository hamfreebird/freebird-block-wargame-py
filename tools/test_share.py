"""分享与观战自检（v3.2）。

覆盖三块：

1. **想定分享格式**（`.bwscn` 格式版本 2）——版本标记、逐文件校验和、
   损坏包被拦在落盘之前、旧版包仍可读；
2. **回放分享**（`.bwrep` 格式版本 2）——内嵌想定后换机器也能重演，
   同名但内容不同的想定不会被当成同一张图；
3. **AI 对战观战**——整局能跑完、同种子逐项一致、观战回放能重演出同一局。

不依赖 pytest：
    python tools/test_share.py
"""
from __future__ import annotations

import json
import os
import sys
import zipfile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="bw_share_")
os.environ["BLOCKWARGAME_USER_DIR"] = _TMP
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pkg as _pkg  # noqa: E402
from core import version as _ver  # noqa: E402
from core.enums import Detection, Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.replay import (  # noqa: E402
    KIND_AI_VS_AI, REPLAY_VERSION, ReplayError, ReplayFile, compare, snapshot,
)
from core.spectate import SpectatorMatch  # noqa: E402

ARMORY = "data/units/armory.json"
MAP = "data/maps/strait_shield.json"
MAP2 = "data/maps/steppe_thunder.json"

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [FAIL] {label}")
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# 1. 版本号与兼容性比较
# --------------------------------------------------------------------------- #
def test_version() -> None:
    section("版本号比较")
    check(_ver.parse("3.2") == (3, 2), "两位版本号解析为 (3,2)")
    check(_ver.normalize("3.2") == "3.2.0", "缺位补 0：3.2 → 3.2.0")
    check(_ver.compare("3.2", "3.2.0") == 0, "3.2 与 3.2.0 视为同一版本")
    check(_ver.compare("3.1.9", "3.2.0") == -1, "3.1.9 低于 3.2.0")
    check(_ver.compare("3.10.0", "3.9.0") == 1, "3.10.0 高于 3.9.0（按数字比，不按字典序）")
    check(_ver.parse("dev") == (), "无法解析时返回空元组，不抛异常")
    check(_ver.compat_note(_ver.GAME_VERSION) == "", "同版本无兼容提示")
    check("更新的版本" in _ver.compat_note("99.0.0"), "更新版本 → 提示「文件太新」")
    check("更早的版本" in _ver.compat_note("1.0.0"), "更旧版本 → 提示「文件偏旧」")


# --------------------------------------------------------------------------- #
# 2. 想定分享格式
# --------------------------------------------------------------------------- #
def test_package() -> None:
    section("想定包（.bwscn v2）")
    out = os.path.join(_TMP, "pkg", "sample.bwscn")
    _pkg.export_package([MAP], ARMORY, out, name="测试包", author="tester",
                        include_armory=False)
    check(os.path.isfile(out), "导出成功")

    man = _pkg.inspect_package(out)
    check(man.version == _pkg.PACKAGE_VERSION == 2, f"清单格式版本为 2（实际 {man.version}）")
    check(man.game_version == _ver.GAME_VERSION,
          f"清单记录了游戏版本（{man.game_version}）")
    check(man.compat_note() == "", "同版本导出 → 无兼容提示")
    check(len(man.files) == 1 and man.files[0]["role"] == "map",
          "清单登记了包内文件与角色")
    digest = man.checksum_of(f"maps/{os.path.basename(MAP)}")
    check(len(digest) == 64, "记录了 64 位 sha256")
    check(man.checksum_of("maps/none.json") == "", "未登记的文件查询返回空串")

    # 正常导入
    target = os.path.join(_TMP, "maps_ok")
    res = _pkg.import_package(out, target, ARMORY, merge_armory=False)
    check(res.added_maps == [os.path.basename(MAP)], "导入后地图落地")
    check(os.path.isfile(os.path.join(target, os.path.basename(MAP))), "文件确实写到了目标目录")
    check(not res.notes, f"同版本导入无额外提示（实际 {res.notes}）")

    # 再导入一次 → 提示覆盖
    res2 = _pkg.import_package(out, target, ARMORY, merge_armory=False)
    check(any("覆盖" in n for n in res2.notes), "重复导入会提示覆盖同名想定")

    # 内容被篡改 → 校验和拦截，且不落盘
    bad = os.path.join(_TMP, "pkg", "tampered.bwscn")
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            if item.startswith("maps/"):
                data = data.replace(b"strait_shield", b"tampered_shield")
            dst.writestr(item, data)
    target2 = os.path.join(_TMP, "maps_bad")
    try:
        _pkg.import_package(bad, target2, ARMORY, merge_armory=False)
        check(False, "篡改过的包应被拒绝")
    except _pkg.PackageError as exc:
        check("校验和" in str(exc), f"篡改包被校验和拦下（{str(exc)[:24]}…）")
    check(not os.path.isdir(target2) or not os.listdir(target2),
          "被拒绝的包没有留下任何文件")

    # 旧格式包（版本 1，无 files / game_version）仍可导入
    legacy = os.path.join(_TMP, "pkg", "legacy.bwscn")
    with open(MAP, "rb") as f:
        raw = f.read()
    with zipfile.ZipFile(legacy, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"maps/{os.path.basename(MAP)}", raw)
        z.writestr("package.json", json.dumps({
            "format": "bwscn", "version": 1, "name": "旧包",
            "maps": [os.path.basename(MAP)],
        }, ensure_ascii=False))
    man_old = _pkg.inspect_package(legacy)
    check(man_old.version == 1 and not man_old.files, "旧包清单被正确解析")
    check("v3.2 之前" in man_old.compat_note(), "旧包给出「未记录版本」提示")
    target3 = os.path.join(_TMP, "maps_legacy")
    res3 = _pkg.import_package(legacy, target3, ARMORY, merge_armory=False)
    check(res3.added_maps == [os.path.basename(MAP)], "旧包仍能导入（向后兼容）")
    check(any("v3.2 之前" in n for n in res3.notes), "旧包导入时带出兼容提示")

    # 结构不对的想定（缺 deployments）→ 落盘前拒绝
    junk = os.path.join(_TMP, "pkg", "junk.bwscn")
    with zipfile.ZipFile(junk, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("maps/junk.json", json.dumps({"size": [8, 6], "name": "junk"}))
        z.writestr("package.json", json.dumps({
            "format": "bwscn", "version": 2, "name": "坏包", "maps": ["junk.json"],
            "files": [{"name": "maps/junk.json", "role": "map",
                       "sha256": _pkg.sha256_bytes(
                           json.dumps({"size": [8, 6], "name": "junk"}).encode()),
                       "size": 0}],
        }, ensure_ascii=False))
    target4 = os.path.join(_TMP, "maps_junk")
    try:
        _pkg.import_package(junk, target4, ARMORY, merge_armory=False)
        check(False, "缺 deployments 的想定应被拒绝")
    except _pkg.PackageError as exc:
        check("deployments" in str(exc), "缺 deployments 的想定被拦截")
    check(not os.path.isdir(target4) or not os.listdir(target4),
          "结构非法的想定没有落盘")

    # 格式版本高于程序支持 → 明确拒绝
    future = os.path.join(_TMP, "pkg", "future.bwscn")
    with zipfile.ZipFile(future, "w") as z:
        z.writestr("package.json", json.dumps({
            "format": "bwscn", "version": 99, "name": "未来包", "maps": [],
        }, ensure_ascii=False))
    try:
        _pkg.inspect_package(future)
        check(False, "更高格式版本应被拒绝")
    except _pkg.PackageError as exc:
        check("高于当前程序支持" in str(exc), "更高格式版本给出明确原因")


# --------------------------------------------------------------------------- #
# 3. 回放分享
# --------------------------------------------------------------------------- #
def _spectate_match(seed: int = 424242) -> SpectatorMatch:
    sc = load_scenario(MAP, ARMORY)
    m = SpectatorMatch(sc, seed=seed)
    m.run_to_end()
    return m


def test_replay_share() -> None:
    section("回放分享（.bwrep v2）")
    check(REPLAY_VERSION == 2, f"回放格式版本为 2（实际 {REPLAY_VERSION}）")

    m = _spectate_match()
    rf = ReplayFile.from_spectate(m.state, os.path.basename(MAP),
                                  scenario_path=MAP, note="自检")
    check(rf.kind == KIND_AI_VS_AI, "观战回放类型正确")
    check(rf.scenario_data is not None, "观战回放内嵌了想定")
    check(len(rf.scenario_sha256) == 64, "记录了想定指纹")
    check(rf.game_version == _ver.GAME_VERSION, "记录了游戏版本")
    check(rf.commands == [], "观战回放不存命令流（只存种子）")

    path = os.path.join(_TMP, "rep", "ai.bwrep")
    rf.save(path)
    back = ReplayFile.load(path)
    check(back.seed == rf.seed and back.kind == rf.kind, "种子与类型往返一致")
    check(back.scenario_data is not None and back.scenario_sha256 == rf.scenario_sha256,
          "内嵌想定与指纹往返一致")
    check(back.compat_note() == "", "同版本回放无兼容提示")

    # 换台机器：本地没有同名想定 → 从内嵌数据还原
    empty_dir = os.path.join(_TMP, "no_maps")
    restore_dir = os.path.join(_TMP, "restored")
    os.makedirs(empty_dir, exist_ok=True)
    target, note = back.materialize_scenario([empty_dir], restore_dir)
    check(os.path.isfile(target), "缺想定时从内嵌数据还原出文件")
    check("内嵌" in note, f"还原时给出说明（{note}）")
    sc2 = load_scenario(target, ARMORY)
    check(sc2.map.width > 0, "还原出的想定能被正常加载")

    # 同名但内容不同 → 必须用内嵌那份，并说明原因
    other_dir = os.path.join(_TMP, "other_maps")
    os.makedirs(other_dir, exist_ok=True)
    name = os.path.basename(MAP)
    shutil.copyfile(MAP2, os.path.join(other_dir, name))     # 同名、不同图
    target3, note3 = back.materialize_scenario([other_dir], os.path.join(_TMP, "restored2"))
    check(target3 != os.path.join(other_dir, name),
          "同名不同内容的想定不会被当成同一张图")
    check("不一致" in note3, f"内容不一致时给出告警（{note3}）")

    # 未内嵌 + 缺想定 → 明确报错，而不是演一局错的
    lean = ReplayFile(scenario="nope.json", seed=1)
    try:
        lean.materialize_scenario([empty_dir], restore_dir)
        check(False, "既无内嵌想定又缺文件时应报错")
    except ReplayError as exc:
        check("未内嵌" in str(exc), "缺想定且未内嵌时给出明确原因")

    # 普通对局回放的版本标记
    from core.commands import CMD_END_TURN, CMD_HOLD, Command, CommandLog
    sc = load_scenario(MAP, ARMORY)
    from core.engine import GameState
    st = GameState(sc, Side.BLUE, seed=8888)
    log = CommandLog([Command(CMD_HOLD, {"uid": 1}), Command(CMD_END_TURN, {})])
    for cmd in log:
        from core.replay import apply_command
        apply_command(st, cmd)
    rf2 = ReplayFile.from_game(st, name, log, note="对局", scenario_path=MAP)
    check(rf2.kind == "match" and len(rf2.commands) == 2, "普通回放存命令流")
    check(rf2.scenario_data is not None, "普通回放也内嵌想定（可直接分享）")
    p2 = os.path.join(_TMP, "rep", "match.bwrep")
    rf2.save(p2)
    check(len(ReplayFile.load(p2).commands) == 2, "命令流往返一致")

    # 旧版回放（版本 1，无 kind / 内嵌）仍能读
    old = os.path.join(_TMP, "rep", "old.bwrep")
    with open(old, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "scenario": name, "seed": 5,
                   "player_side": "BLUE", "commands": []}, f)
    o = ReplayFile.load(old)
    check(o.version == 1 and o.kind == "match" and o.scenario_data is None,
          "旧版回放按默认值解析")
    check(o.compat_note() == "", "旧版回放没有版本标记时不乱报兼容问题")

    # 版本号高于程序支持 → 拒绝
    future = os.path.join(_TMP, "rep", "future.bwrep")
    with open(future, "w", encoding="utf-8") as f:
        json.dump({"version": 99, "scenario": name, "seed": 1, "commands": []}, f)
    try:
        ReplayFile.load(future)
        check(False, "更高回放版本应被拒绝")
    except ReplayError as exc:
        check("高于当前程序支持" in str(exc), "更高回放版本给出明确原因")


# --------------------------------------------------------------------------- #
# 4. AI 对战观战
# --------------------------------------------------------------------------- #
def test_spectate() -> None:
    section("AI 对战观战")
    sc = load_scenario(MAP, ARMORY)
    m = SpectatorMatch(sc, seed=20270301)
    n = 0
    while m.step() is not None:
        n += 1
        if n > 20000:
            break
    st = m.state
    check(n > 20, f"整局能跑完（{n} 步 / {m.actions} 个动作）")
    check(st.over, "对局以终局状态结束")
    check(m.finished, "推进器标记为已结束")
    s = m.summary()
    check(s["blue_actions"] > 0 and s["red_actions"] > 0,
          f"两边都真的动过（蓝 {s['blue_actions']} / 红 {s['red_actions']}）")
    check(m.step() is None, "结束后继续推进会安全返回 None")

    # 同种子同进程跑两遍必须逐项一致（地图必须每局复制，不能沿用想定里那份）
    m2 = SpectatorMatch(sc, seed=20270301)
    m2.run_to_end()
    diffs = compare(snapshot(st), snapshot(m2.state))
    check(not diffs, f"同种子重跑逐项一致（差异 {len(diffs)} 条）")
    if diffs:
        for d in diffs[:3]:
            print("      ", d)

    # 不同种子必须跑出不同的局——否则「种子」是摆设
    m3 = SpectatorMatch(sc, seed=20270302)
    m3.run_to_end()
    check(compare(snapshot(st), snapshot(m3.state)) != [], "不同种子得到不同的对局")

    # 观战全图可见，但不改变规则判定
    check(st.spectator, "对局标记为观战")
    enemy = next(u for u in st.units if u.side is not st.player_side)
    check(st.view_level(enemy) is Detection.TRACKED, "观战模式下敌方单位在界面上可见")
    m4 = SpectatorMatch(load_scenario(MAP, ARMORY), seed=99)
    check(m4.state.spectator, "构造出的对局默认处于观战态")
    hidden = m4.state
    hidden.spectator = False
    # 非观战时视图等级必须**等于**该方的探测结果——观战标记不能顺手改规则
    for u in hidden.units:
        if u.side is hidden.player_side:
            continue
        check(hidden.view_level(u) is hidden.known_level(hidden.player_side, u),
              "非观战时视图等级 = 该方探测结果")
        break
    blind = [u for u in hidden.units
             if u.side is not hidden.player_side
             and hidden.known_level(hidden.player_side, u) is Detection.NONE]
    if blind:
        check(hidden.view_level(blind[0]) is Detection.NONE,
              "非观战时未探测到的单位不可见")
    else:
        print("  [跳过] 该想定开局即全图可见，无可验证的未探测单位")

    # 观战回放：只存种子，重演必须得到同一局
    rf = ReplayFile.from_spectate(st, os.path.basename(MAP), scenario_path=MAP)
    path = os.path.join(_TMP, "rep", "spec.bwrep")
    rf.save(path)
    from ui.replayplayer import ReplayPlayer
    player = ReplayPlayer(load_scenario(MAP, ARMORY), ReplayFile.load(path))
    player.skip_to_end()
    diffs2 = compare(snapshot(st), snapshot(player.state))
    check(not diffs2, f"观战回放重演出同一局（差异 {len(diffs2)} 条）")
    if diffs2:
        for d in diffs2[:3]:
            print("      ", d)
    check(player.finished, "回放播放器走到终局")
    check(player.executed > 20, f"回放实际推进了 {player.executed} 个动作")
    check("回合" in player.status_line(), f"进度文本按回合显示（{player.status_line()}）")


def main() -> int:
    print(f"工作目录：{_TMP}")
    test_version()
    test_package()
    test_replay_share()
    test_spectate()
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FAILED:
        print(f"✗ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

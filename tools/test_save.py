"""v3.0 专项自检：存档层的两条硬约束。

项目把「可复现」当作核心承诺，而存档读档是它最容易悄悄破功的边界：

1. **随机流必须整份往返**。只存种子不够——存档前已经消耗掉的随机数
   没法只靠种子还原。这里用 ``GameRng.clone()`` 取一份存档瞬间的基准流，
   逐项比对读档后的序列，而不是只看种子相同。
2. **坏档只能让「读档」失败**。``main.py`` 顶层只有一个 ``except Exception``，
   任何 ``ValueError`` / ``KeyError`` 冒泡上去都会直接结束进程。本脚本用
   各种残缺 JSON 逐一验证它们都被翻译成 ``SaveError``。

用法：python tools/test_save.py
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows CI 的 stdout 默认 cp1252，中文断言信息会直接把脚本打崩
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from core.engine import GameState  # noqa: E402
from core.enums import Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.save import (  # noqa: E402
    SAVE_VERSION, SaveError, load_game, save_game,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_DIR = os.path.join(ROOT, "data", "maps")
ARMORY = os.path.join(ROOT, "data", "units", "armory.json")

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def _first_scenario():
    """取第一个能加载的想定（地图目录里的文件顺序不保证都可用）。"""
    for path in sorted(glob.glob(os.path.join(MAP_DIR, "*.json"))):
        try:
            return os.path.abspath(path), load_scenario(os.path.abspath(path), ARMORY)
        except Exception:
            continue
    return "", None


def _base_payload(scenario_path: str) -> dict:
    return {
        "version": SAVE_VERSION,
        "scenario": scenario_path,
        "armory": ARMORY,
        "player_side": "BLUE",
        "units": [],
        "tiles": [],
    }


def _write(tmpdir: str, name: str, payload) -> str:
    path = os.path.join(tmpdir, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False)
    return path


def _expect_save_error(path: str, label: str) -> None:
    try:
        load_game(path, ARMORY)
    except SaveError:
        check(True, f"坏档拒绝并返回 SaveError：{label}")
    except Exception as exc:      # noqa: BLE001 - 这里就是要抓住任何漏网异常
        check(False, f"坏档泄漏了 {type(exc).__name__}（应为 SaveError）：{label} → {exc}")
    else:
        check(False, f"坏档被静默接受：{label}")


def main() -> int:
    scenario_path, scenario = _first_scenario()
    if scenario is None:
        print("✗ 找不到可用的想定文件，无法自检")
        return 1
    template_id = next(iter(scenario.armory.templates))
    tmpdir = tempfile.mkdtemp(prefix="bw_save_")

    # ------------------------------------------------------------------ #
    print("[1] 随机流整份往返")
    seed = 20261105
    st = GameState(scenario, Side.BLUE, seed=seed)
    st.scenario_path = scenario_path
    st.armory_path = ARMORY
    for _ in range(37):                 # 模拟打到一半：已经消耗了一批随机数
        st.rng.random()
    calls_at_save = st.rng.calls

    save_path = os.path.join(tmpdir, "roundtrip.json")
    save_game(st, save_path)
    with open(save_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    check("rng" in raw, "存档包含 rng 随机流字段")
    check(raw.get("version") == SAVE_VERSION,
          f"存档版本为 SAVE_VERSION（{SAVE_VERSION}）")
    check(isinstance(raw.get("rng"), dict) and "internal" in raw["rng"],
          "rng 字段含完整的 random 内部状态（不只是种子）")

    # 基准流：从存档那一刻克隆，之后不再受任何操作影响
    ref = st.rng.clone()
    expect = [ref.random() for _ in range(5)]

    st2 = load_game(save_path, ARMORY)
    got = [st2.rng.random() for _ in range(5)]
    check(expect == got, "读档后 5 次取数与存档瞬间的序列逐项一致")
    check(st2.seed == seed, f"读档恢复种子（{st2.seed}）")
    check(st2.rng.calls == calls_at_save + 5,
          f"读档恢复已消耗次数（{calls_at_save} → {st2.rng.calls}，含本次 5 次）")
    if expect != got:
        print(f"      期望 {[round(v, 6) for v in expect]}")
        print(f"      实得 {[round(v, 6) for v in got]}")

    # ------------------------------------------------------------------ #
    print("[2] 向后兼容：v5 旧档（无 rng）仍可读")
    old = dict(raw)
    old.pop("rng", None)
    old["version"] = 5
    old_path = _write(tmpdir, "legacy_v5", old)
    try:
        st_old = load_game(old_path, ARMORY)
        check(True, "缺少 rng 的旧档不报错")
        check(isinstance(st_old.seed, int) and st_old.seed > 0,
              "旧档读入后仍有可用的种子")
        check(st_old.rng.calls == 0, "旧档的随机流从零开始（无状态可恢复）")
    except Exception as exc:          # noqa: BLE001
        check(False, f"旧档读取失败：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    print("[3] 状态完整性：存档往返不丢关键字段")
    check(len(st2.units) == len(st.units),
          f"单位数量一致（{len(st.units)}）")
    check(st2.turn_count == st.turn_count, f"回合数一致（{st2.turn_count}）")
    check(st2.player_side is st.player_side, "玩家阵营一致")
    check(st2.map.name == st.map.name, "地图一致")

    # ------------------------------------------------------------------ #
    print("[4] 坏档一律翻译成 SaveError（不得冒泡到主循环）")
    base = _base_payload(scenario_path)

    def variant(name: str, patch: dict | None = None) -> str:
        payload = dict(base)
        if patch:
            payload.update(patch)
        return _write(tmpdir, name, payload)

    _expect_save_error(variant("bad_side", {"player_side": "PURPLE"}), "非法阵营")
    _expect_save_error(variant("bad_version", {"version": "abc"}), "版本号不是数字")
    _expect_save_error(variant("null_version", {"version": None}), "版本号为 null")
    _expect_save_error(variant("future", {"version": SAVE_VERSION + 1}), "版本高于当前程序")
    _expect_save_error(
        variant("unit_no_x", {"units": [{"template": template_id, "side": "BLUE", "y": 1}]}),
        "单位缺少 x 坐标")
    _expect_save_error(
        variant("unit_bad_x", {"units": [{"template": template_id, "side": "BLUE",
                                          "x": "a", "y": 1}]}),
        "单位坐标不是数字")
    _expect_save_error(
        variant("unit_bad_alt", {"units": [{"template": template_id, "side": "BLUE",
                                            "x": 1, "y": 1, "altitude": "orbit"}]}),
        "非法高度枚举")
    _expect_save_error(
        variant("tile_no_y", {"tiles": [{"x": 0}]}), "地块缺少 y 坐标")
    _expect_save_error(
        variant("bad_vp", {"vp": [1, 2]}), "vp 不是对象")
    _expect_save_error(
        variant("bad_rng", {"rng": {"seed": 1, "calls": 0, "version": 3,
                                    "internal": [1, 2], "gauss_next": None}}),
        "rng 内部状态尺寸不对")
    _expect_save_error(_write(tmpdir, "top_array", "[1,2,3]"), "顶层是数组")
    _expect_save_error(_write(tmpdir, "broken_json", "{broken"), "不是合法 JSON")
    _expect_save_error(os.path.join(tmpdir, "no_such.json"), "文件不存在")

    # ------------------------------------------------------------------ #
    print("[5] 无法存档时给出 SaveError")
    naked = GameState(scenario, Side.BLUE, seed=1)   # 故意不设 scenario_path
    try:
        save_game(naked, os.path.join(tmpdir, "naked.json"))
    except SaveError:
        check(True, "缺少想定来源时存档返回 SaveError")
    except Exception as exc:          # noqa: BLE001
        check(False, f"存档失败泄漏了 {type(exc).__name__}: {exc}")
    else:
        check(False, "缺少想定来源时竟然存档成功")

    print()
    shutil.rmtree(tmpdir, ignore_errors=True)
    if FAILS:
        print(f"✗ 存档自检失败 {len(FAILS)} 项：")
        for msg in FAILS:
            print(f"    - {msg}")
        return 1
    print("✓ 存档自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

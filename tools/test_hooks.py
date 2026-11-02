"""v2.2 专项自检：规则钩子系统。

覆盖：
* 条件解析与校验（未知类型要报错并带定位信息）；
* 条件求值（基础条件 + all / any / not 组合）；
* 效果执行（增援 / 空袭 / 修复 / 消息 / 标记）；
* 只触发一次（once）与每回合触发；
* 单条规则出错不影响其它规则；
* 想定加载时规则能被正确读入。

用法：python tools/test_hooks.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("BLOCKWARGAME_USER_DIR", tempfile.mkdtemp())

import pygame  # noqa: E402

from core import hooks  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.paths import data_root  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def _scenario_with_rules(rules: list[dict]) -> GameState:
    """造一个带规则的临时想定并载入。"""
    src = os.path.join(data_root(), "data", "maps", "training_ground.json")
    with open(src, "r", encoding="utf-8") as f:
        doc = json.load(f)
    doc["rules"] = rules
    tp = tempfile.mktemp(suffix=".json")
    with open(tp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    armory = os.path.join(data_root(), "data", "units", "armory.json")
    return GameState(load_scenario(tp, armory), Side.BLUE)


def main() -> int:
    pygame.init()
    pygame.display.set_mode((320, 240))
    print("=== 规则钩子自检 ===")

    # ------------------------------------------------------------------ #
    print("--- 解析与校验 ---")
    check(hooks.parse_rules(None) == [], "None 解析为空列表")
    try:
        hooks.parse_rules([{"when": {"type": "不存在的条件"}, "then": []}])
        check(False, "未知条件类型应当报错")
    except hooks.RuleError as exc:
        check("不存在的条件" in str(exc), "未知条件类型报错并指出是哪个")
    try:
        hooks.parse_rules([{"when": {"type": "always"},
                           "then": [{"type": "不存在的效果"}]}])
        check(False, "未知效果类型应当报错")
    except hooks.RuleError as exc:
        check("不存在的效果" in str(exc), "未知效果类型报错并指出是哪个")
    try:
        hooks.parse_rules([{"when": {"type": "always"}, "then": "不是数组"}])
        check(False, "then 非数组应当报错")
    except hooks.RuleError:
        check(True, "then 非数组报错")

    # ------------------------------------------------------------------ #
    print("--- 条件求值 ---")
    st = _scenario_with_rules([])
    c = hooks.Condition.parse({"type": "always"})
    check(c.evaluate(st), "always 恒为真")
    c = hooks.Condition.parse({"type": "turn_at_least", "turn": 1})
    check(c.evaluate(st), "turn_at_least(1) 在第 1 回合为真")
    c = hooks.Condition.parse({"type": "turn_at_most", "turn": 0})
    check(not c.evaluate(st), "turn_at_most(0) 在第 1 回合为假")
    c = hooks.Condition.parse({"type": "side_controls_min", "side": "BLUE",
                               "count": 99})
    check(not c.evaluate(st), "控制点不足时条件为假")
    c = hooks.Condition.parse({"type": "side_controls_min", "side": "BLUE",
                               "count": 0})
    check(c.evaluate(st), "控制点足够时条件为真")

    # 组合条件
    always = {"type": "always"}
    never = {"type": "not", "conditions": [always]}
    c = hooks.Condition.parse({"type": "all", "conditions": [always, never]})
    check(not c.evaluate(st), "all：有假则假")
    c = hooks.Condition.parse({"type": "any", "conditions": [always, never]})
    check(c.evaluate(st), "any：有真则真")
    c = hooks.Condition.parse({"type": "not", "conditions": [never]})
    check(c.evaluate(st), "not 取反正确")

    # ------------------------------------------------------------------ #
    print("--- 规则执行 ---")
    st = _scenario_with_rules([
        {"id": "add_troops", "name": "增援",
         "when": {"type": "turn_at_least", "turn": 2},
         "then": [{"type": "reinforce", "side": "BLUE", "template": "m1a2",
                   "x": 5, "y": 10, "count": 2}]},
        {"id": "mark", "when": {"type": "turn_at_least", "turn": 3},
         "then": [{"type": "set_flag", "text": "crit", "value": 1},
                  {"type": "message", "text": "标记完成"}]},
    ])
    check(len(st.rules) == 2, "想定里的规则被正确载入")

    n0 = len([u for u in st.units if u.side is Side.BLUE and u.alive])
    st.turn_count = 1
    st._run_rules()
    n1 = len([u for u in st.units if u.side is Side.BLUE and u.alive])
    check(n1 == n0, "条件不满足时不执行效果")

    st.turn_count = 2
    st._run_rules()
    n2 = len([u for u in st.units if u.side is Side.BLUE and u.alive])
    check(n2 == n0 + 2, f"条件满足时增援生效（{n0} → {n2}）")

    st.turn_count = 3
    st._run_rules()
    check(st.rule_flags.get("crit") == 1.0, "set_flag 效果生效")
    check(any("标记完成" in m.text for m in st.log), "message 效果产生播报")

    # once：默认只触发一次
    st.turn_count = 4
    n_before = len([u for u in st.units if u.side is Side.BLUE and u.alive])
    st._run_rules()
    n_after = len([u for u in st.units if u.side is Side.BLUE and u.alive])
    check(n_after == n_before, "once 规则不会重复触发")

    # ------------------------------------------------------------------ #
    print("--- 容错 ---")
    st2 = _scenario_with_rules([
        {"id": "bad", "when": {"type": "always"},
         "then": [{"type": "reinforce", "side": "BLUE",
                   "template": "不存在的兵种", "x": 1, "y": 1}]},
        {"id": "good", "when": {"type": "always"},
         "then": [{"type": "message", "text": "后续规则仍应执行"}]},
    ])
    st2._run_rules()
    check(any("后续规则仍应执行" in m.text for m in st2.log),
          "单条规则出错不影响其它规则")
    check(any("失败" in m.text for m in st2.log),
          "出错的规则有明确的失败播报")

    # 坏规则不影响想定加载
    st3 = _scenario_with_rules([{"when": {"type": "永远不存在的条件"},
                                  "then": []}])
    check(len(st3.rules) == 0, "含坏规则的想定仍能加载（规则被跳过）")

    # ------------------------------------------------------------------ #
    print("--- 效果类型覆盖 ---")
    st4 = _scenario_with_rules([
        {"id": "fx", "when": {"type": "always"},
         "then": [
             {"type": "message", "text": "消息"},
             {"type": "airstrike", "side": "RED", "x": 10, "y": 10,
              "radius": 3, "damage": 5},
             {"type": "heal", "side": "BLUE", "x": 5, "y": 10,
              "radius": 3, "damage": 10},
             {"type": "reveal", "x": 10, "y": 10, "radius": 3},
             {"type": "objective", "x": 12, "y": 12, "name": "规则点",
              "owner": "BLUE", "value": 3},
         ]},
    ])
    st4._run_rules()
    texts = [m.text for m in st4.log]
    check(any("规则空袭" in t for t in texts), "airstrike 效果有播报")
    check(any("规则修复" in t for t in texts), "heal 效果有播报")
    check(any("规则暴露" in t for t in texts), "reveal 效果有播报")
    check(any("规则设定目标点" in t for t in texts), "objective 效果有播报")
    t = st4.map.at(12, 12)
    check(t is not None and t.objective and t.objective_name == "规则点",
          "objective 效果真的设置了目标点")

    pygame.quit()
    if FAILS:
        print(f"\n规则钩子自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\n规则钩子自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""大模型指挥官自检（v3.4）。

**不需要模型、不需要推理进程**：这里验证的是「接线」是否正确，
不是「1.7B 聪不聪明」。模型本身的质量只能靠人看战报，代码能保证的是：

1. **中性决心 == 没有决心**——不开这个功能时，AI 的每一步与引入之前
   逐条一致。这是最重要的一条：新机制不能悄悄改变既有平衡。
2. **模型输出再脏也进不了规则层**——非法姿态、不存在的目标点编号、
   重复的兵种、非布尔的 feint，全部在本地校验里被修正或丢弃。
3. **战争迷雾不泄漏**——简报里只出现本方已识别的敌军。
4. **推理进程起不来就降级**——超时、启动失败都退回规则 AI，
   且启动只尝试一次（不能每回合都去戳一个不存在的 exe）。
5. **回放带上决心**——决心是外部输入，不记进命令流就没法复演那一回合。

用法：python tools/test_llm.py
"""
from __future__ import annotations

import os
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("BLOCKWARGAME_NO_AUDIO", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import llm_client as llm_mod  # noqa: E402
from core.commands import CMD_END_TURN, Command, CommandLog  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Detection, Side  # noqa: E402
from core.llm_commander import (  # noqa: E402
    Directive, LLMCommander, _schema_for, build_prompt, objective_map, parse_directive,
)
from core.loader import load_scenario  # noqa: E402
from core.replay import apply_command  # noqa: E402

ARMORY = os.path.join(ROOT, "data", "units", "armory.json")
MAP = os.path.join(ROOT, "data", "maps", "training_ground.json")
#: 有多个目标点的小图——训练场只有一个目标点，测不了「主攻方向选择」
MAP_OBJ = os.path.join(ROOT, "data", "maps", "steppe_thunder.json")

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def fresh(seed: int = 21, turns: int = 1, path: str = MAP) -> GameState:
    sc = load_scenario(path, ARMORY)
    st = GameState(sc, Side.BLUE, seed=seed)
    st.scenario_path = path
    st.armory_path = ARMORY
    st.turn_count = turns
    st.refresh_detection()
    return st


def run_ai(st: GameState, directive: Directive | None, turns: int = 2) -> list[tuple]:
    """跑若干回合的 AI 相位，返回每一回合的动作签名。"""
    sig: list[tuple] = []
    for _ in range(turns):
        st.phase = "player"
        st.end_player_turn()
        st.begin_ai_phase(directive=directive)
        planner = st._ai_planner
        guard = 0
        while True:
            guard += 1
            act = planner.step(st)
            if act is None or guard > 2000:
                break
            st.apply_ai_action(act)
            sig.append((act.kind, act.unit_uid, act.target_uid,
                        tuple(act.path[-1]) if act.path else None))
        st.resolve_missiles()
        st.refresh_detection()
        st._advance_turn()
    return sig


# --------------------------------------------------------------------------- #
class FakeClient:
    """替身：接口与 ``LLMClient`` 一致，行为可控。"""

    def __init__(self, payload: dict | None = None, delay: float = 0.0,
                 fail_boot: bool = False, fail_call: bool = False):
        self.payload = payload or {"stance": "assault", "feint": False}
        self.delay = delay
        self.fail_boot = fail_boot
        self.fail_call = fail_call
        self.status = "off"
        self.ready = False
        self.last_error = ""
        self.start_calls = 0
        self.calls = 0

    def probe(self) -> bool:
        return True

    def start(self) -> bool:
        self.start_calls += 1
        if self.fail_boot:
            self.last_error = "假启动失败"
            self.status = "error"
            return False
        self.status = "ready"
        self.ready = True
        return True

    def describe(self) -> str:
        return "fake-1.7b"

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_call:
            raise llm_mod.LLMError("假请求失败")
        return dict(self.payload)

    def stop(self) -> None:
        self.status = "off"
        self.ready = False


def test_neutral_is_noop() -> None:
    print("\n--- 中性决心不改变行为 ---")
    base = run_ai(fresh(), None)
    neu = run_ai(fresh(), Directive())
    check(len(base) > 10, f"基线跑出了动作（{len(base)} 条）")
    check(base == neu, "Directive() 与不传决心的动作序列逐条一致")


def test_parse_validation() -> None:
    print("\n--- 输出校验 ---")
    ctx = {"ids": {"O1": (3, 4), "O2": (9, 9)}, "names": {"O1": "甲", "O2": "乙"},
           "main_allowed": ["O1", "O2"], "focus_allowed": ["sam", "mbt"]}
    d = parse_directive({"stance": "ASSAULT", "main_effort": "o1",
                         "focus": ["sam", "sam", "bogus"], "feint": "yes"}, ctx)
    check(d.stance == "assault", "姿态大小写被归一")
    check(d.main_effort == (3, 4), "目标点编号被解析成坐标")
    check(d.focus == ("sam",), "重复与非法兵种被剔除")
    check(d.feint is True, "非布尔 feint 退回 true")

    d2 = parse_directive({"stance": "berserk", "main_effort": "O9",
                          "focus": "mbt"}, ctx)
    check(d2.stance == "balanced", "非法姿态退回 balanced")
    check(d2.main_effort is None, "不存在的目标点被丢弃")
    check(d2.focus == ("mbt",), "单个字符串形式的 focus 也能接受")
    check("balanced" in d2.note and "O9" in d2.note, "修正原因被记进 note")

    d3 = parse_directive({}, ctx)
    check(d3.stance == "balanced" and d3.main_effort is None and d3.source == "llm",
          "空输出得到中性决心，不抛异常")


def test_dict_roundtrip() -> None:
    print("\n--- 决心序列化 ---")
    d = Directive(stance="hold", main_effort=(7, 8), main_name="甲",
                  focus=("sam", "hq"), feint=False, source="llm",
                  model="fake-1.7b", turn=5)
    back = Directive.from_dict(d.to_dict())
    check(back == d, "to_dict / from_dict 往返一致")
    for junk in (None, {}, {"stance": 3, "main": "x", "focus": 5},
                 {"main": [1], "focus": ["nope"]}):
        try:
            Directive.from_dict(junk)
            ok = True
        except Exception:
            ok = False
        check(ok, f"脏数据不抛异常：{junk!r}")


def test_prompt_hides_unknown() -> None:
    print("\n--- 简报不泄漏未探测敌情 ---")
    st = fresh()
    # 把所有敌军标为未发现：简报里不该出现任何敌情
    for u in st.enemies_of(Side.RED):
        st.detection[Side.RED][u.uid] = Detection.NONE
    _sys, user, ctx = build_prompt(st, Side.RED)
    check("Enemy detected: none" in user, "未探测到敌军时简报写明 none")
    enemy_line = next((ln for ln in user.splitlines() if ln.startswith("Enemy detected:")), "")
    for cls in {u.template.unit_class.value for u in st.enemies_of(Side.RED)}:
        check(cls not in enemy_line, f"未探测兵种 {cls} 不出现在敌情行")
    check(all(oid in user for oid in ctx["ids"]), "目标点全部列出（位置是公开信息）")

    # 识别一个敌军后，它应当出现
    foe = st.enemies_of(Side.RED)[0]
    st.detection[Side.RED][foe.uid] = Detection.TRACKED
    _s2, user2, ctx2 = build_prompt(st, Side.RED)
    check(foe.template.unit_class.value in user2, "已识别兵种出现在简报里")
    check(foe.template.unit_class.value in ctx2["focus_allowed"],
          "已识别兵种进入可选 focus 列表")


def test_schema_scope() -> None:
    print("\n--- Schema 只放开合法取值 ---")
    st = fresh()
    _s, _u, ctx = build_prompt(st, Side.RED)
    schema = _schema_for(ctx)
    ids = set(schema["properties"]["main_effort"]["enum"])
    check(ids <= set(ctx["ids"]), "主攻方向枚举是目标点编号的子集")
    own_only = [oid for oid, t in objective_map(st)
                if t.owner is Side.RED and oid not in ctx["main_allowed"]]
    if own_only:
        check(own_only[0] not in ids, f"己方后方据点 {own_only[0]} 不在主攻枚举里")
    else:
        check(True, "本图上没有「己方且无敌情」的据点可排除（跳过）")


def test_commander_flow() -> None:
    print("\n--- 后台取决心 ---")
    st = fresh(turns=3)
    _s, _u, ctx = build_prompt(st, Side.RED)
    oid = ctx["main_allowed"][0]
    payload = {"stance": "assault", "main_effort": oid, "feint": False}
    if ctx["focus_allowed"]:
        payload["focus"] = [ctx["focus_allowed"][0]]
    client = FakeClient(payload)
    cmd = LLMCommander(client)
    check(cmd.available, "可用性探测通过")
    check(cmd.prefetch(st, Side.RED), "prefetch 起了线程")
    for _ in range(100):
        d = cmd.take(st, Side.RED, timeout=0.05)
        if d.source == "llm":
            break
        time.sleep(0.02)
    check(d.source == "llm", "取到模型给的决心")
    check(d.stance == "assault" and d.main_effort == ctx["ids"][oid],
          "决心内容与模型输出一致")
    check(d.model == "fake-1.7b", "记录了模型标识")
    check(client.start_calls == 1, "只为一次决策启动了一次服务")

    # 同一回合再取一次：结果已被消费，应降级
    again = cmd.take(st, Side.RED, timeout=0.05)
    check(again.source == "fallback", "结果只消费一次")

    # 结果过期：换回合后不复用
    st.turn_count += 1
    stale = cmd.take(st, Side.RED, timeout=0.05)
    check(stale.source == "fallback", "上一回合的决心不会被误用")


def test_fallback_paths() -> None:
    print("\n--- 降级 ---")
    st = fresh(turns=2)

    slow = LLMCommander(FakeClient(delay=0.4))
    slow.prefetch(st, Side.RED)
    d = slow.take(st, Side.RED, timeout=0.1)
    check(d.source == "fallback", "超时返回中性决心")
    check(d.active is False, "中性决心不带任何非中性权重")

    bad = LLMCommander(FakeClient(fail_boot=True))
    bad.prefetch(st, Side.RED)
    time.sleep(0.05)
    check(bad._boot_failed, "启动失败被记下")
    before = bad.client.start_calls
    bad.prefetch(st, Side.RED)
    time.sleep(0.05)
    check(bad.client.start_calls == before, "启动失败后不再反复重试")
    check(bad.last_error, "失败原因可读")

    err = LLMCommander(FakeClient(fail_call=True))
    err.prefetch(st, Side.RED)
    time.sleep(0.1)
    check(err.take(st, Side.RED, timeout=0.3).source == "fallback",
          "请求异常时降级而不是崩")


def test_weights_affect_ai() -> None:
    print("\n--- 决心确实影响 AI ---")
    base = fresh(path=MAP_OBJ)
    sig_base = run_ai(base, None, turns=2)
    ids = objective_map(base)
    far = next(((oid, t) for oid, t in ids if t.owner is not Side.RED), None)
    check(far is not None, "找到一个不属于自己的目标点")
    if far is None:
        return
    oid, tile = far
    st = fresh(path=MAP_OBJ)
    sig_llm = run_ai(st, Directive(stance="assault", main_effort=(tile.x, tile.y),
                                   main_name=tile.objective_name or oid,
                                   source="llm"), turns=2)
    check(sig_base != sig_llm, "给了决心之后动作序列发生变化")
    check(bool(st.ai_last_directive.get(Side.RED)), "决心被记进对局状态（战报可见）")
    check(st.ai_last_directive[Side.RED].source == "llm", "来源标记为 llm")


def test_replay_carries_directive() -> None:
    print("\n--- 回放携带决心 ---")
    st = fresh(turns=4)
    d = Directive(stance="screen", main_effort=None, focus=(), feint=True,
                  source="llm", model="fake-1.7b", turn=4)
    log = CommandLog()
    log.record(CMD_END_TURN, turn=4, directive=d.to_dict())
    payload = log.to_dict() if hasattr(log, "to_dict") else None
    if payload is not None:
        round_trip = CommandLog.from_dict(payload) if hasattr(CommandLog, "from_dict") else None
        if round_trip is not None:
            check(round_trip.commands[0].args["directive"]["stance"] == "screen",
                  "命令流 JSON 往返保留决心")

    st2 = fresh(turns=4)
    apply_command(st2, Command(CMD_END_TURN, {"directive": d.to_dict()}, turn=4))
    check(st2._ai_planner is not None, "回放执行了 AI 回合")
    check(getattr(st2._ai_planner, "dir", None) == d,
          "回放里的 AI 用的是记录下来的决心")


def test_discovery() -> None:
    print("\n--- 资源探测 ---")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fake_model = os.path.join(tmp, "lfm3-test.gguf")
        open(fake_model, "wb").write(b"GGUF")
        found = llm_mod.discover_model(tmp)
        check(found == fake_model, "按目录找到 .gguf 模型")

        os.makedirs(os.path.join(tmp, "runtime", "llama"))
        exe = os.path.join(tmp, "runtime", "llama",
                           "llama-server.exe" if os.name == "nt" else "llama-server")
        open(exe, "wb").write(b"x")
        check(llm_mod.discover_server(tmp) == exe, "按目录找到推理运行时")

        cfg = llm_mod.LLMConfig(model_path=tmp, server_path=tmp)
        client = llm_mod.LLMClient(cfg)
        check(client.probe(), "probe 认这两样东西")
        check(not client.ready, "未启动时 ready 为假")


def main() -> int:
    test_neutral_is_noop()
    test_parse_validation()
    test_dict_roundtrip()
    test_prompt_hides_unknown()
    test_schema_scope()
    test_commander_flow()
    test_fallback_paths()
    test_weights_affect_ai()
    test_replay_carries_directive()
    test_discovery()

    print("\n" + "=" * 46)
    if FAILS:
        print(f"✗ {len(FAILS)} 条断言未通过：")
        for m in FAILS:
            print("   -", m)
        return 1
    print("✓ 大模型指挥官自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

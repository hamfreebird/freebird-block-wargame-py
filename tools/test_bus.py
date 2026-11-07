"""事件总线自检（v3.3 11.4）。

**要守住的性质**：

1. **目录三处一致**——`core/bus.py` 的 `CATALOGUE`、`core/mods.py` 的
   `HOOK_SIGNATURES`、`SDK.md` 的事件表，说的是同一批事件、同样的参数顺序。
   三者错位的后果是「模组按文档写的回调，运行时报参数个数不对」。
2. **每条阵亡路径都广播**——直射、触雷、燃油坠毁、诱饵到期、想定脚本的
   区域伤害。这是 11.4 要解决的核心问题：以前只有直射那一条发事件。
3. **订阅者写错不能带崩对局**——一个抛异常的模组不该让引擎停在半路。
4. **伤亡只记一次**——收拢到 `kill_unit` 之后，不能出现两处各加一遍。

用法：python tools/test_bus.py
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

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("BLOCKWARGAME_NO_AUDIO", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import bus as bus_mod  # noqa: E402
from core import hooks as hooks_mod  # noqa: E402
from core import mods as mods_mod  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Detection, Facility, Side, Terrain  # noqa: E402
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Unit  # noqa: E402

ARMORY = os.path.join(ROOT, "data", "units", "armory.json")
MAP = os.path.join(ROOT, "data", "maps", "training_ground.json")
ARM = load_armory(ARMORY)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def fresh(seed: int = 11) -> GameState:
    """一局干净的对局（训练场 20×14，地形平坦）。"""
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=seed)
    st.scenario_path = MAP
    st.armory_path = ARMORY
    # 想定自带的目标点 / 工事 / 设施会干扰占领与油料断言，清掉
    for t in st.map.iter_tiles():
        t.facility = Facility.NONE
        t.owner = Side.NEUTRAL
        t.fort = 0
        t.fort_owner = None
        t.smoke = 0
        t.objective = False
    st.bus.clear_history()
    return st


def unit_of(st: GameState, tid: str, side: Side, x: int, y: int):
    """取一个该型单位放到指定位置；想定里没有就现造一个。

    训练场的编成是给教学用的，只覆盖少数兵种；测试要验证的是「这类装备
    走这条路会不会广播事件」，所以缺什么就直接从武器库造——但**优先复用
    场上已有的同型单位**，这样弹药与模板属性都是真实来源。
    """
    for u in st.units:
        if u.template.id == tid and u.side is side and u.alive:
            u.x, u.y = x, y
            u.hp = u.template.hp
            st.invalidate_pos_index()
            return u
    u = Unit(template=ARM.templates[tid], side=side, x=x, y=y,
             uid=max([q.uid for q in st.units] + [0]) + 1)
    st.units.append(u)
    st.invalidate_pos_index()
    return u


def clear_tile(st: GameState, x: int, y: int) -> None:
    """把一格清成空地：地形改为平原、清掉占位单位。

    想定里的 (6,5) 可能是水或山，也可能已经站了人——不先清干净，
    「能不能走过去」的断言测的就不是我们要测的那件事了。
    """
    t = st.map.at(x, y)
    if t is None:
        raise AssertionError(f"地图上没有 ({x},{y})")
    t.terrain = Terrain.PLAIN
    st.units = [u for u in st.units if (u.x, u.y) != (x, y)]
    st.invalidate_pos_index()


# --------------------------------------------------------------------------- #
# 1. 目录一致性
# --------------------------------------------------------------------------- #
def test_catalogue() -> None:
    print("\n[1] 事件目录三处一致")
    cat = bus_mod.CATALOGUE
    check(cat, f"事件目录非空（{len(cat)} 个事件）")

    orphan = [e for e in cat if e not in mods_mod.HOOK_EVENTS]
    check(not orphan, f"通知型事件都登记进 HOOK_EVENTS（缺 {orphan}）")

    bad_args = [e for e, fields in bus_mod.MOD_ARGS.items()
                if e not in cat or any(f not in cat[e] for f in fields)]
    check(not bad_args, f"MOD_ARGS 只引用目录里已有的字段（问题 {bad_args}）")

    # 签名参数个数 = state + MOD_ARGS 的字段个数
    mismatch = []
    for event, fields in bus_mod.MOD_ARGS.items():
        sig = mods_mod.HOOK_SIGNATURES.get(event)
        if sig is None:
            mismatch.append(f"{event}（无签名）")
            continue
        expect = 1 + len(fields)
        got = sig.count(",") + 1 if sig.strip() != "()" else 0
        if got != expect:
            mismatch.append(f"{event}：{sig} vs {expect} 个参数")
    check(not mismatch, f"钩子签名与 MOD_ARGS 对齐（不一致 {mismatch}）")

    missing_sig = [e for e in mods_mod.HOOK_EVENTS
                   if e not in mods_mod.HOOK_SIGNATURES]
    check(not missing_sig, f"每个钩子都有签名（缺 {missing_sig}）")

    # 文档里的钩子表必须覆盖全部事件名
    with open(os.path.join(ROOT, "SDK.md"), encoding="utf-8") as f:
        doc = f.read()
    undocumented = [e for e in mods_mod.HOOK_EVENTS if f"`{e}`" not in doc]
    check(not undocumented, f"SDK.md 写了每个钩子（缺 {undocumented}）")


# --------------------------------------------------------------------------- #
# 2. 订阅与分发
# --------------------------------------------------------------------------- #
def test_dispatch() -> None:
    print("\n[2] 订阅与分发")

    class _State:
        turn_count = 3

    st = _State()
    bus = bus_mod.EventBus()

    seen: list[str] = []
    wildcard: list[str] = []
    bus.subscribe("turn_start", lambda s, e: seen.append(e.name))
    bus.subscribe("*", lambda s, e: wildcard.append(e.name))

    bus.emit("turn_start", st, turn=3)
    check(seen == ["turn_start"], "精确订阅收到事件")
    check(wildcard == ["turn_start"], "通配符订阅也收到")
    check(bus.subscriber_count() == 2, f"订阅计数（{bus.subscriber_count()}）")

    fn = lambda s, e: None       # noqa: E731
    bus.subscribe("turn_end", fn)
    check(bus.unsubscribe("turn_end", fn) is True, "退订成功")
    check(bus.unsubscribe("turn_end", fn) is False, "重复退订返回 False")

    bus.emit("turn_end", st, turn=3)
    check(len(seen) == 1, "只订阅 turn_start 的看不到 turn_end")
    check(len(wildcard) == 2, "通配符按顺序收到两条")


# --------------------------------------------------------------------------- #
# 3. 错误隔离与历史
# --------------------------------------------------------------------------- #
def test_isolation_and_history() -> None:
    print("\n[3] 错误隔离与历史")

    class _State:
        turn_count = 1

    bus = bus_mod.EventBus(history=4)
    good: list[str] = []

    def bad(s, e):
        raise RuntimeError("模组写坏了")

    bus.subscribe("turn_start", bad)
    bus.subscribe("turn_start", lambda s, e: good.append(e.name))

    ev = bus.emit("turn_start", _State(), turn=1)
    check(good == ["turn_start"], "抛异常的订阅者不影响后面的订阅者")
    check(len(bus.errors) == 1 and "RuntimeError" in bus.errors[0],
          f"异常被收进 errors（{bus.errors[0] if bus.errors else '无'}）")
    check(ev.turn == 1 and ev["turn"] == 1, "事件带回合号且可按字段取值")

    for i in range(10):
        bus.emit("turn_start", _State(), turn=i)
    check(len(bus.history) == 4, f"历史按上限截断（{len(bus.history)}/4）")
    check(bus.counts["turn_start"] == 11,
          f"计数不受历史截断影响（{bus.counts['turn_start']}）")
    check(bus.recent(limit=2)[-1].data["turn"] == 9, "recent() 取最近几条")
    check(len(bus.recent("turn_end")) == 0, "recent() 可按事件名过滤")

    # 错误列表也有上限：一个每帧都炸的订阅者不能把内存吃光
    bus2 = bus_mod.EventBus()
    bus2.subscribe("turn_start", bad)
    for _ in range(200):
        bus2.emit("turn_start", _State(), turn=1)
    check(len(bus2.errors) == 64, f"错误列表有上限（{len(bus2.errors)}）")


# --------------------------------------------------------------------------- #
# 4. 载荷校验
# --------------------------------------------------------------------------- #
def test_payload_validation() -> None:
    print("\n[4] 载荷校验")

    class _State:
        turn_count = 1

    bus = bus_mod.EventBus()
    try:
        bus.emit("no_such_event", _State())
    except KeyError as exc:
        check("未登记的事件" in str(exc), f"未知事件直接报错（{exc}）")
    else:
        check(False, "未知事件被静默接受")

    try:
        bus.emit("unit_destroyed", _State(), unit=object())
    except KeyError as exc:
        check("缺少字段" in str(exc), f"缺字段直接报错（{exc}）")
    else:
        check(False, "缺字段的事件被静默接受")


# --------------------------------------------------------------------------- #
# 5. 五条阵亡路径都广播
# --------------------------------------------------------------------------- #
def test_kill_paths() -> None:
    print("\n[5] 五条阵亡路径都广播 unit_destroyed")

    # 5.1 直射
    st = fresh()
    atk = unit_of(st, "m1a2", Side.BLUE, 5, 5)
    tgt = unit_of(st, "usmc", Side.RED, 6, 5)
    gun = next(w for w in atk.template.weapons if w.kind.value == "gun")
    tgt.hp = 1.0
    st.detection[Side.BLUE][tgt.uid] = Detection.TRACKED
    cas_before = st.casualties[Side.RED]
    res = st.fire(atk, tgt, gun)
    kills = st.bus.recent("unit_destroyed")
    check(res.destroyed, "直射把残血目标打掉")
    check(len(kills) == 1 and kills[0]["cause"] == "direct_fire",
          f"直射阵亡广播了 cause（{kills[0]['cause'] if kills else '无'}）")
    check(kills and kills[0]["by"] is atk, "事件带「谁打的」")
    check(st.casualties[Side.RED] == cas_before + 1,
          f"伤亡 +1（{cas_before} → {st.casualties[Side.RED]}）")

    # 5.2 触雷
    st = fresh()
    clear_tile(st, 5, 5)
    clear_tile(st, 6, 5)
    mover = unit_of(st, "m1a2", Side.BLUE, 5, 5)
    mover.hp = 1.0
    mover.mp = 10.0          # 进敌方工事带要 4 点机动，坦克本回合只有 3.2
    mine_tile = st.map.at(6, 5)
    mine_tile.fort = 6
    mine_tile.fort_owner = Side.RED
    cas_before = st.casualties[Side.BLUE]
    moved = st.move_unit(mover, [(5, 5), (6, 5)])
    kills = st.bus.recent("unit_destroyed")
    check(moved and not mover.alive, f"触雷把残血单位打掉（moved={moved}）")
    check(len(kills) == 1 and kills[0]["cause"] == "mines",
          f"触雷阵亡广播了 cause（{kills[0]['cause'] if kills else '无'}）")
    check(st.casualties[Side.BLUE] == cas_before + 1, "触雷伤亡 +1")

    # 5.3 燃油耗尽
    st = fresh()
    clear_tile(st, 9, 9)
    jet = unit_of(st, "j20", Side.BLUE, 9, 9)
    jet.fuel = 0.0
    cas_before = st.casualties[jet.side]
    st._tick_logistics()
    kills = st.bus.recent("unit_destroyed")
    check(not jet.alive, "燃油耗尽坠毁")
    check(any(k["cause"] == "fuel" for k in kills),
          f"坠毁广播了 cause=fuel（{[k['cause'] for k in kills]}）")
    check(st.casualties[jet.side] == cas_before + 1, "坠毁伤亡 +1")

    # 5.4 诱饵到期
    st = fresh()
    dummy = unit_of(st, "m1a2", Side.RED, 10, 10)
    dummy.is_decoy = True
    dummy.decoy_kind = "radar"
    dummy.decoy_ttl = 1
    cas_before = st.casualties[Side.RED]
    st._tick_decoys()
    kills = st.bus.recent("unit_destroyed")
    check(any(k["cause"] == "decoy_expired" for k in kills),
          f"诱饵失效广播了 cause（{[k['cause'] for k in kills]}）")
    check(st.casualties[Side.RED] == cas_before, "诱饵不计伤亡")

    # 5.5 想定脚本的区域伤害
    st = fresh()
    victim = unit_of(st, "usmc", Side.RED, 8, 8)
    victim.hp = 5.0
    cas_before = st.casualties[Side.RED]
    eff = hooks_mod.Effect(type="area_damage", side="BLUE", x=8, y=8,
                           radius=1.0, damage=50.0)
    hooks_mod._do_area_damage(st, eff, "区域伤害")
    kills = st.bus.recent("unit_destroyed")
    check(not victim.alive, "区域伤害打死了单位")
    check(any(k["cause"] == "scenario_event" for k in kills),
          f"区域伤害广播了 cause（{[k['cause'] for k in kills]}）")
    check(st.casualties[Side.RED] == cas_before + 1,
          f"区域伤害计入伤亡（{cas_before} → {st.casualties[Side.RED]}）")


# --------------------------------------------------------------------------- #
# 6. 回合 / 占领 / 设施打击
# --------------------------------------------------------------------------- #
def test_other_events() -> None:
    print("\n[6] 回合交接 / 占领 / 设施打击")

    st = fresh()
    names: list[str] = []
    st.bus.subscribe("*", lambda s, e: names.append(e.name))
    st._advance_turn()
    check("turn_end" in names and "turn_start" in names,
          f"回合交接广播了两端（{names[:2]}）")
    check(names.index("turn_end") < names.index("turn_start"),
          "先结束再开始")

    # 占领目标点：`_handle_capture` 判的是单位**脚下**那一格
    st = fresh()
    clear_tile(st, 5, 4)
    walker = unit_of(st, "m1a2", Side.BLUE, 5, 4)
    tile = st.map.at(5, 4)
    tile.objective = True
    tile.objective_name = "测试高地"
    tile.owner = Side.RED
    st._handle_capture(walker)
    caps = st.bus.recent("objective_captured")
    check(len(caps) == 1 and caps[0]["side"] is Side.BLUE,
          "占领目标点广播了 objective_captured")
    check(caps and (caps[0]["x"], caps[0]["y"]) == (5, 4), "事件带坐标")
    check(caps and caps[0]["by"] is walker, "事件带占领单位")

    # 夺取设施
    st = fresh()
    clear_tile(st, 5, 4)
    taker = unit_of(st, "m1a2", Side.BLUE, 5, 4)
    ft = st.map.at(5, 4)
    ft.facility = Facility.DEPOT
    ft.owner = Side.RED
    ft.facility_hp = ft.facility_max_hp
    st._handle_capture(taker)
    caps = st.bus.recent("facility_captured")
    check(len(caps) == 1 and caps[0]["side"] is Side.BLUE,
          "夺取设施广播了 facility_captured")

    # 设施打击
    st = fresh()
    clear_tile(st, 4, 4)
    clear_tile(st, 6, 4)
    jet = unit_of(st, "f15e", Side.BLUE, 4, 4)
    bomb = max((w for w in jet.template.weapons if w.vs_facility),
               key=lambda w: w.range_max)
    st_t = st.map.at(6, 4)
    st_t.facility = Facility.AIRBASE
    st_t.owner = Side.RED
    st_t.facility_hp = st_t.facility_max_hp
    st.phase = "player"
    res = st.strike_facility(jet, (6, 4), bomb, 1)
    struck = st.bus.recent("facility_struck")
    check(res.ok, f"设施打击成功（{res.message}）")
    check(len(struck) == 1 and struck[0]["tile"] is st_t,
          "打击设施广播了 facility_struck")


# --------------------------------------------------------------------------- #
# 7. 模组钩子兼容
# --------------------------------------------------------------------------- #
def test_mod_hooks() -> None:
    print("\n[7] 模组通过同一条总线收事件")

    class _API:
        def __init__(self):
            self.hooks: dict[str, list] = {}
            self.ok = True

        def on(self, event, fn):
            self.hooks.setdefault(event, []).append(fn)

    class _Reg:
        def __init__(self):
            self.api = _API()
            self.errors: list[str] = []

        def hooks_for(self, event):
            return list(self.api.hooks.get(event, ()))

        def fire(self, event, *args):
            out = None
            for fn in self.hooks_for(event):
                got = fn(*args)
                if got is not None:
                    out = got
            return out

    reg = _Reg()
    got: list[tuple] = []
    reg.api.on("unit_destroyed", lambda state, unit: got.append(("destroyed", unit.name)))
    reg.api.on("unit_damaged", lambda state, unit, damage, by: got.append(("damaged", damage)))
    reg.api.on("turn_start", lambda state, turn: got.append(("turn", turn)))

    st = fresh()
    st.bus.mods = reg
    dead = unit_of(st, "usmc", Side.RED, 8, 8)
    dead.hp = 0.0
    dead.destroyed = True
    st.kill_unit(dead, "test")
    check(got and got[0][0] == "destroyed",
          f"模组收到 unit_destroyed，签名为 (state, unit)（{got[:1]}）")

    st.bus.emit("unit_damaged", st, unit=dead, damage=12.5, by=None)
    check(("damaged", 12.5) in got, "模组收到 unit_damaged，含伤害值")

    st.bus.emit("turn_start", st, turn=7)
    check(("turn", 7) in got, "模组收到 turn_start，含回合号")

    # 1.0.0 的模组不认识新事件，也不能因此报错
    check(mods_mod.API_VERSION.startswith("1."),
          f"API 版本保持 1.x 兼容（{mods_mod.API_VERSION}）")


def main() -> int:
    print("事件总线自检（v3.3）")
    test_catalogue()
    test_dispatch()
    test_isolation_and_history()
    test_payload_validation()
    test_kill_paths()
    test_other_events()
    test_mod_hooks()

    print("\n" + "=" * 46)
    if FAILS:
        print(f"✗ {len(FAILS)} 条断言未通过：")
        for m in FAILS:
            print("   -", m)
        return 1
    print("✓ 事件总线自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""消耗与补给链（v3.1 9.3）的针对性测试。

这一项要证明的不是「补给线存在」（v1.2 就有），而是**断补给会真的打不出火力**：

1. 油、弹、备件都是实数库存，只出不进会真的见底；
2. 断一天（紧张）齐射降到单发，断三天（枯竭）命中率与拦截通道一起掉；
3. 维修要消耗备件，没有备件就修不动；
4. 恢复补给能把状态清零，不是永久惩罚；
5. 存档往返后库存与断粮计数不丢。

用法::

    python tools/test_logistics.py
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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core import rules  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Detection, Domain, Side  # noqa: E402
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Unit  # noqa: E402
from core.rng import GameRng  # noqa: E402
from core.save import load_game, save_game  # noqa: E402

ARMORY = "data/units/armory.json"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


class _Anim:
    def add(self, *a, **k) -> None:
        pass


class _Mods:
    def fire(self, *a, **k):
        return None


T: dict = {}


def u(tid: str, side: Side = Side.BLUE) -> Unit:
    return Unit(template=T[tid], side=side, x=5, y=5)


def bare_state(sc) -> GameState:
    """只带补给结算所需字段的桩。"""
    st = GameState.__new__(GameState)
    st.units = []
    st.map = sc.map
    st.player_side = Side.BLUE
    st.detection = {}
    st._pos_index = None
    st.turn_count = 1
    st.log = []
    st.fx = []
    st.missiles = []
    st.rng = GameRng(3)
    st.anim = _Anim()
    st.mods = _Mods()
    st.supply_links = {}
    st.casualties = {Side.BLUE: 0, Side.RED: 0}
    st.sfx_events = []
    # 探测重算与本测试无关，固定掉以免依赖地图与干扰场
    st.refresh_detection = lambda: None
    st.stats = {Side.BLUE: {"shots": 0, "hits": 0, "damage": 0, "intercepted": 0},
                Side.RED: {"shots": 0, "hits": 0, "damage": 0, "intercepted": 0}}
    return st


class _Linked:
    """把 supply_source_for 固定成「有补给」，用来测恢复分支。"""

    def __enter__(self):
        self.saved = rules.supply_source_for
        rules.supply_source_for = lambda st, unit: object()
        return self

    def __exit__(self, *exc):
        rules.supply_source_for = self.saved
        return False


# --------------------------------------------------------------------------- #
# 1. 状态分档
# --------------------------------------------------------------------------- #
def test_states() -> None:
    print("\n[1] 补给状态分档")
    x = u("m1a2")
    check(x.supply_state == 0, "初始补给充裕")
    x.supply_dry = 1
    check(x.supply_state == 1, "断一天 → 紧张")
    x.supply_dry = 2
    check(x.supply_state == 1, "断两天仍是紧张")
    x.supply_dry = 3
    check(x.supply_state == 2, "断三天 → 枯竭")
    x.supply_dry = 9
    check(x.supply_state == 2, "断更久仍是枯竭（不会溢出成新档）")


# --------------------------------------------------------------------------- #
# 2. 火力下降
# --------------------------------------------------------------------------- #
def test_firepower() -> None:
    print("\n[2] 断补给后的火力")
    atk = u("m1a2")
    w = next(x for x in atk.template.weapons if x.id == "m256_120")
    check(rules.salvo_limit(atk, w) == w.max_salvo,
          f"充裕时齐射上限 {rules.salvo_limit(atk, w)} 发")

    atk.supply_dry = 1
    check(rules.salvo_limit(atk, w) == 1, "紧张时只能单发点射")

    atk.supply_dry = 3
    st = GameState.__new__(GameState)
    st.units = [atk, u("usmc", Side.RED)]
    st.map = load_scenario("data/maps/training_ground.json", ARMORY).map
    st.player_side = Side.BLUE
    st.detection = {Side.BLUE: {st.units[1].uid: Detection.TRACKED}}
    st._pos_index = None
    st.turn_count = 1
    st.log = []
    st.fx = []
    st.missiles = []
    st.rng = GameRng(5)
    st.anim = _Anim()
    st.mods = _Mods()
    atk.x, atk.y = 5, 5
    st.units[1].x, st.units[1].y = 6, 5

    saved = (rules.own_track, rules.jam_strength_at, rules.has_los, rules.gps_integrity)
    rules.own_track = lambda *a, **k: True
    rules.jam_strength_at = lambda *a, **k: 0.0
    rules.has_los = lambda *a, **k: True
    rules.gps_integrity = lambda *a, **k: 1.0
    try:
        prof = rules.shot_profile(st, atk, st.units[1], w)
    finally:
        (rules.own_track, rules.jam_strength_at,
         rules.has_los, rules.gps_integrity) = saved

    check(prof.ok, "枯竭状态下剖面仍成立（不是直接禁用）")
    check(prof.factors.get("补给枯竭") == 0.88,
          f"枯竭时命中率因子 0.88（实际 {prof.factors.get('补给枯竭')}）")

    # 防空通道：枯竭减半
    sam = u("patriot")
    full = rules._channels_of(sam)
    sam.supply_dry = 3
    check(rules._channels_of(sam) == full // 2,
          f"防空通道 {full} → 枯竭后 {rules._channels_of(sam)}")


# --------------------------------------------------------------------------- #
# 3. 结算：断粮计数、备件、油料
# --------------------------------------------------------------------------- #
def test_resupply(sc) -> None:
    print("\n[3] 补给线结算")
    st = bare_state(sc)
    grunt = u("usmc")
    grunt.hp = grunt.template.hp * 0.4
    grunt.spares = 0.0
    grunt.fuel = 0.20
    grunt.ammo = {w.id: 0 for w in grunt.template.weapons}
    st.units = [grunt]

    # 断补给：油弹备件全部只出不进
    for _ in range(3):
        st._resupply_by_line(grunt)
    check(grunt.supply_dry == 3, f"连续三回合断补给，计数 {grunt.supply_dry}")
    check(grunt.fuel == 0.20, "断补给期间油料不回升")
    check(grunt.spares == 0.0, "断补给期间备件不补充")
    check(grunt.hp == grunt.template.hp * 0.4, "没有备件就修不动")
    cut_logs = [e for e in st.log if "补给线中断" in e.text]
    check(len(cut_logs) == 1, f"断粮提示只记一次（实际 {len(cut_logs)}）")
    dry_logs = [e for e in st.log if "补给枯竭" in e.text]
    check(len(dry_logs) == 1, "枯竭时补一条提示")

    # 恢复补给
    with _Linked():
        st._resupply_by_line(grunt)
    check(grunt.supply_dry == 0, "恢复补给后断粮计数清零")
    check(grunt.fuel > 0.20, f"油料回升到 {grunt.fuel:.2f}")
    check(grunt.spares > 0, f"备件补到 {grunt.spares:.0f}")
    check(grunt.hp > grunt.template.hp * 0.4, "有备件了才修得动")
    check(any("恢复补给" in e.text for e in st.log), "恢复补给有战报")

    # 备件与维修是实打实的一比一
    hurt = u("m1a2")
    hurt.hp = hurt.template.hp * 0.5
    hurt.spares = 5.0
    before_hp, before_sp = hurt.hp, hurt.spares
    st.units = [hurt]
    with _Linked():
        st._resupply_by_line(hurt)
    gained = hurt.hp - before_hp
    # 这一回合同时补了一批备件，先算补给后的库存，再看维修扣了多少
    topped = min(hurt.template.hp * 0.5, before_sp + st.SPARES_RESUPPLY)
    spent = topped - hurt.spares
    check(abs(gained - spent) < 1e-6,
          f"修 {gained:.1f} 点血消耗 {spent:.1f} 点备件（1:1）")
    check(gained <= before_sp + st.SPARES_RESUPPLY + 1e-6,
          "维修量不超过可用备件")


# --------------------------------------------------------------------------- #
# 4. 油料：机动消耗与断油减速
# --------------------------------------------------------------------------- #
def test_fuel(sc) -> None:
    print("\n[4] 油料")
    st = bare_state(sc)
    tank = u("m1a2")
    tank.x, tank.y = 5, 5
    st.units = [tank]
    # 找一条两格的平地路径
    path = [(5, 5), (6, 5)]
    before = tank.fuel
    tank.mp = 9.9
    ok = st.move_unit(tank, path)
    check(ok, "移动成功")
    check(tank.fuel < before, f"机动烧油 {before:.3f} → {tank.fuel:.3f}")
    check(abs((before - tank.fuel) - st.FUEL_PER_TILE[Domain.LAND]) < 1e-6,
          "油耗与格数成正比")

    # 油尽：下回合机动点减半
    tank.fuel = 0.0
    tank.reset_turn(2)
    check(abs(tank.mp - tank.template.mp * tank.mp_factor * 0.5) < 1e-6,
          f"油尽后机动点减半（{tank.mp:.1f} / {tank.template.mp:.1f}）")

    tank.fuel = 1.0
    tank.reset_turn(3)
    check(abs(tank.mp - tank.template.mp * tank.mp_factor) < 1e-6, "油满时机动点正常")


# --------------------------------------------------------------------------- #
# 5. 存档往返
# --------------------------------------------------------------------------- #
def test_save(sc, tmp_dir: str) -> None:
    print("\n[5] 存档往返")
    st = GameState(sc, player_side=Side.BLUE, seed=11)
    st.scenario_path = "data/maps/training_ground.json"
    a = st.units[0]
    a.spares = 17.5
    a.supply_dry = 2
    a.fuel = 0.42

    path = os.path.join(tmp_dir, "logi.json")
    try:
        save_game(st, path)
        back = load_game(path, ARMORY)
    except Exception as exc:                       # noqa: BLE001
        check(False, f"存档往返失败：{exc}")
        return
    b = next((x for x in back.units if x.uid == a.uid), None)
    if b is None:
        check(False, "读档后找不到同一个单位")
        return
    check(abs(b.spares - 17.5) < 1e-6, f"备件往返 {b.spares}")
    check(b.supply_dry == 2, f"断粮计数往返 {b.supply_dry}")
    check(abs(b.fuel - 0.42) < 1e-3, f"油料往返 {b.fuel}")


def main() -> int:
    global T
    pygame.init()
    T = load_armory(ARMORY).templates
    sc = load_scenario("data/maps/training_ground.json", ARMORY)

    print("v3.1 消耗与补给链测试")
    print("=" * 46)
    test_states()
    test_firepower()
    test_resupply(sc)
    test_fuel(sc)
    tmp = os.path.join(os.environ.get("TEMP", "."), "wb_logi_save")
    os.makedirs(tmp, exist_ok=True)
    test_save(sc, tmp)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for m in FAILED:
            print("  -", m)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

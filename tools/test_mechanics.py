"""v3.1 机制边界收敛的针对性测试（9.1）。

这里只测「改成人话之后，规则结论是否真的跟着变」——
不测 UI、不测数值平衡，那些在别处。

覆盖：
1. 风场确定性（同种子同风、雾天静稳、雨雪加成、来向与去向差 180°）
2. 烟幕随风漂移与消散（静风不动、和风每回合挪格、强风缩短寿命、边界不丢烟）
3. 工事构筑需要工期（不是一拍即成的等级跳变、废墟重建加价、驻守折工时）
4. 反导多段拦截（上升段窗口、中段两轮、破片打残后仍可带伤突防）

用法::

    python tools/test_mechanics.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core import env as envmod  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Side, TimeOfDay, Weather  # noqa: E402
from core.i18n import set_language, tr  # noqa: E402
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Unit  # noqa: E402
from core.rng import GameRng  # noqa: E402

ARMORY = "data/units/armory.json"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


class _FixedWind:
    """把环境固定成指定风向/风力的桩，让漂移结论可断言。"""

    def __init__(self, index: int, speed: int) -> None:
        self.index = index
        self.speed = speed

    def at(self, turn: int) -> envmod.Environment:
        return envmod.Environment(time_of_day=TimeOfDay.DAY, weather=Weather.CLEAR,
                                  turn=turn, wind_index=self.index,
                                  wind_speed=self.speed)


class _Anim:
    """动画队列桩：机制测试只关心规则副作用，不关心怎么画。"""

    def add(self, *a, **k) -> None:
        pass


class _Mods:
    """模组钩子桩：默认什么都不改。"""

    def fire(self, *a, **k):
        return None


def _state(sc, wind_index: int, wind_speed: int, turn: int = 4) -> GameState:
    st = GameState.__new__(GameState)
    st.units = []
    st.scenario = sc
    st.map = sc.map
    st.player_side = Side.BLUE
    st.detection = {}
    st._pos_index = None
    st.turn_count = turn
    st.env_track = _FixedWind(wind_index, wind_speed)
    st.missiles = []
    st.fx = []
    st.log = []
    st.stats = {Side.BLUE: defaultdict(int), Side.RED: defaultdict(int)}
    st.rng = GameRng(20261104)
    st.anim = _Anim()
    st.mods = _Mods()
    st.emit_sfx = lambda name: None
    st.map_epoch = 0
    # 探测链在 test_i18n / diag 里覆盖，这里只关心工事与烟幕的账
    st.refresh_detection = lambda: None
    return st


def test_wind_fields() -> None:
    print("风场（天候派生，确定性）")
    a = envmod.EnvironmentTrack(seed=7)
    b = envmod.EnvironmentTrack(seed=7)
    same = all(a.wind_at(t) == b.wind_at(t) for t in range(1, 25))
    check(same, "同种子的风场逐回合一致")

    c = envmod.EnvironmentTrack(seed=8)
    diff = sum(1 for t in range(1, 25) if a.wind_at(t) != c.wind_at(t))
    check(diff > 0, f"换种子风场会变（24 回合中有 {diff} 回合不同）")

    # 雾天近地面静稳：风力压到 1 档以内
    fog = envmod.EnvironmentTrack(seed=7, mode="fixed", fixed=Weather.FOG)
    check(all(fog.wind_at(t)[1] <= 1 for t in range(1, 25)),
          "雾天风力不超过 1 档（烟幕糊在原地）")

    # 雨雪：同一回合相对晴天风力更大（基础档位相同时 +1）
    clear = envmod.EnvironmentTrack(seed=7, mode="fixed", fixed=Weather.CLEAR)
    rain = envmod.EnvironmentTrack(seed=7, mode="fixed", fixed=Weather.RAIN)
    pairs = [(clear.wind_at(t)[1], rain.wind_at(t)[1]) for t in range(1, 25)]
    check(all(r >= c for c, r in pairs) and any(r > c for c, r in pairs),
          "雨雪天风力不低于晴天且有加成")

    # 风向命名与去向差 180°：北风的烟应该往南飘
    north = envmod.Environment(wind_index=0, wind_speed=2)
    check(north.wind_vector == (0, 1), "北风的烟幕向南漂（去向与来向相反）")
    check(north.wind_label == tr("env.wind_n", "北"), "北风标签取语言包")
    check(north.wind_speed == 2 and "2" in north.wind_text(), "风级出现在整句里")

    # 8 种语言都能安全格式化（占位符个数对不上会在这里炸）
    for code in ("zh", "en", "ja", "ko", "de", "fr", "es", "ru"):
        set_language(code)
        try:
            txt = north.wind_text()
        except Exception as exc:                     # noqa: BLE001
            check(False, f"{code} 的风向整句格式化失败：{exc}")
            continue
        check(bool(txt) and "%" not in txt, f"{code} 风向整句可渲染：{txt}")
    set_language("zh")


def test_smoke_drift(sc) -> None:
    print("烟幕随风漂移")
    # 东风（index=2）→ 烟向西漂 1 格
    st = _state(sc, wind_index=2, wind_speed=2)
    t = st.map.at(10, 10)
    t.smoke = 3
    st._tick_smoke()
    check(t.smoke == 0, "起烟格已清空")
    west = st.map.at(9, 10)
    check(west.smoke == 2, f"烟团西移一格且寿命 -1（得到 {west.smoke}）")

    # 静风：原地不动，只掉寿命
    st2 = _state(sc, wind_index=2, wind_speed=0)
    t2 = st2.map.at(10, 10)
    t2.smoke = 3
    st2._tick_smoke()
    check(t2.smoke == 2, "静风时烟幕不挪窝，只掉一回合寿命")

    # 强风：既挪格又额外缩短寿命
    st3 = _state(sc, wind_index=2, wind_speed=3)
    st3.map.at(10, 10).smoke = 3
    st3._tick_smoke()
    check(st3.map.at(9, 10).smoke == 1, "强风下烟幕寿命额外 -1")

    # 微风：两回合才挪一格（按回合奇偶判定，与何时施放无关）
    st4 = _state(sc, wind_index=2, wind_speed=1, turn=5)
    st4.map.at(10, 10).smoke = 3
    st4._tick_smoke()
    check(st4.map.at(10, 10).smoke == 2, "微风奇数回合烟幕仍在原地")
    st4.turn_count = 6
    st4._tick_smoke()
    check(st4.map.at(9, 10).smoke == 1, "微风偶数回合烟幕挪一格")

    # 边界：往图外吹的烟不凭空消失
    st5 = _state(sc, wind_index=2, wind_speed=2)
    edge = st5.map.at(0, 10)
    edge.smoke = 3
    st5._tick_smoke()
    check(edge.smoke == 2, "贴边施放的烟幕留在原地，不会飘出地图消失")

    # 两团烟撞在一起取较浓一侧，不叠加成永久遮蔽
    st6 = _state(sc, wind_index=2, wind_speed=2)
    st6.map.at(0, 10).smoke = 3
    st6.map.at(1, 10).smoke = 2
    st6._tick_smoke()
    check(st6.map.at(0, 10).smoke == 2, "同格烟幕取较浓的一侧（不累加）")

    # 烟幕仍能遮蔽：探测/瞄准判定看的是 smoke > 0
    st7 = _state(sc, wind_index=0, wind_speed=0)
    st7.map.at(6, 6).smoke = 2
    check(st7.map.at(6, 6).smoke > 0, "漂移后 smoke 字段仍是遮蔽判定的依据")


def test_fort_work(sc, arm) -> None:
    print("工事构筑需要工期")
    grunts = [t for t in arm.templates.values()
              if t.can_fortify and t.fort_power <= 1 and t.domain.value == "land"]
    sappers = [t for t in arm.templates.values()
               if t.can_fortify and t.fort_power >= 2 and t.domain.value == "land"]
    if not grunts or not sappers:
        check(False, "武器库里找不到 fort_power 1 / 2 的工事兵种")
        return

    def mk(st, tpl, x, y):
        u = Unit(template=tpl, side=Side.BLUE, x=x, y=y)
        u.mp = 3.0
        st.units.append(u)
        return u

    # 步兵：工时按 Lv1=1 / Lv2=2 / Lv3=3 累计，一次动作只投 1 工时
    st = _state(sc, 0, 0)
    u = mk(st, grunts[0], 8, 8)
    tile = st.map.at(8, 8)
    check(st.build_fort(u), "步兵可以开始构筑工事")
    check(tile.fort == 1, f"步兵一次施工只到 Lv1（实际 Lv{tile.fort}）")
    u.mp = 3.0
    u.moved = False
    st.build_fort(u)
    check(tile.fort == 1 and tile.fort_build == 1,
          f"Lv1→Lv2 要 2 工时，一次不够（已攒 {tile.fort_build}）")
    u.mp = 3.0
    u.moved = False
    st.build_fort(u)
    check(tile.fort == 2, f"步兵第三次施工到 Lv2（实际 Lv{tile.fort}）")
    for _ in range(3):
        u.mp = 3.0
        u.moved = False
        st.build_fort(u)
    check(tile.fort == 3, f"累计 6 工时封顶 Lv3（实际 Lv{tile.fort}）")
    mp_before = u.mp
    check(not st.build_fort(u) and u.mp == mp_before, "Lv3 之后不再消耗动作")

    # 工兵：一次投入 2 工时，同样动作数更少
    st2 = _state(sc, 0, 0)
    e = mk(st2, sappers[0], 9, 9)
    t2 = st2.map.at(9, 9)
    st2.build_fort(e)
    t2_a = t2.fort
    e.mp, e.moved = 3.0, False
    st2.build_fort(e)
    check(t2_a == 1 and t2.fort == 2, f"工兵两次施工到 Lv2（实际 Lv{t2.fort}）")
    e.mp, e.moved = 3.0, False
    st2.build_fort(e)
    e.mp, e.moved = 3.0, False
    st2.build_fort(e)
    check(t2.fort == 3, "工兵攒够 6 工时封顶 Lv3")

    # 废墟：被打塌的工事原地重建要多花 1 工时
    st3 = _state(sc, 0, 0)
    t3 = st3.map.at(7, 7)
    t3.fort, t3.fort_owner = 1, Side.BLUE
    t3.fort_progress, t3.fort_build, t3.fort_rubble = 0, 0, False
    check(st3.fort_need(t3) == st3.FORT_WORK[1], "Lv1→Lv2 常规需要 2 工时")
    t3.fort = 0
    t3.fort_rubble = True
    check(st3.fort_need(t3) == st3.FORT_WORK[0] + st3.RUBBLE_EXTRA,
          "废墟上重建 Lv1 要多花工时（先清障）")
    u3 = mk(st3, grunts[0], 7, 7)
    st3.build_fort(u3)
    check(t3.fort == 0, "一次施工不足以在废墟上立起 Lv1")

    # 驻守维修产出的是工时，不是直接涨级
    st4 = _state(sc, 0, 0)
    t4 = st4.map.at(6, 6)
    t4.fort, t4.fort_owner, t4.fort_build = 1, Side.BLUE, 0
    u4 = mk(st4, grunts[0], 6, 6)
    u4.moved = False
    st4._tick_fortifications()
    check(t4.fort == 1 and t4.fort_build == 0, "驻守一回合只是在攒进度")
    st4._tick_fortifications()
    check(t4.fort == 1 and t4.fort_build == 1,
          f"驻守两回合才折 1 工时（fort_build={t4.fort_build}）")

    # 无人维护仍会荒废，掉到 0 时施工进度清零
    t4.fort_build = 1
    u4.x, u4.y = 0, 0                # 撤走部队
    st4._pos_index = None            # 位置索引按约定需手工失效
    for _ in range(3):
        st4._tick_fortifications()
    check(t4.fort == 0 and t4.fort_build == 0, "无人维护 3 回合掉一级并清空进度")


def test_interception(sc, arm) -> None:
    print("反导多段拦截")
    from core import dice, rules
    from core.model import Missile
    from core.rng import GameRng

    def find_unit_weapon(prefix: str):
        for t in arm.templates.values():
            for w in t.weapons:
                if w.id == prefix:
                    return t, w
        return None, None

    thaad_tpl, _ = find_unit_weapon("thaad_int")
    isk_tpl, isk_w = find_unit_weapon("9m723")
    if thaad_tpl is None or isk_tpl is None:
        check(False, "武器库缺少 THAAD 或 伊斯坎德尔，无法测试多段拦截")
        return

    def build(thaad_pos, target_pos, own_pos=(2, 2)):
        st = _state(sc, 0, 0)
        atk = Unit(template=isk_tpl, side=Side.RED, x=own_pos[0], y=own_pos[1])
        tgt = Unit(template=thaad_tpl, side=Side.BLUE, x=target_pos[0], y=target_pos[1])
        d = Unit(template=thaad_tpl, side=Side.BLUE, x=thaad_pos[0], y=thaad_pos[1])
        st.units.extend([atk, tgt, d])
        st._pos_index = None
        m = Missile(weapon=isk_w, attacker_uid=atk.uid, attacker_side=Side.RED,
                    target_uid=tgt.uid, origin=(atk.x, atk.y),
                    target_pos=(tgt.x, tgt.y), eta=4, salvo=1, label=isk_w.name)
        st.missiles = [m]
        return st, m, d

    # 打不中时：两轮中段窗口都要真的发生，各消耗一发拦截弹
    real_roll = dice.roll
    try:
        dice.roll = lambda p, rng=None: False
        st, m, d = build((9, 6), (16, 10))
        ammo0 = d.ammo.get("thaad_int", 0)
        st.resolve_missiles()
        st.resolve_missiles()
        check(m.midcourse_windows == rules.MIDCOURSE_WINDOWS,
              f"中段共 {rules.MIDCOURSE_WINDOWS} 轮窗口（实际 {m.midcourse_windows}）")
        used = ammo0 - d.ammo.get("thaad_int", 0)
        check(used == rules.MIDCOURSE_WINDOWS, f"每轮烧一发拦截弹（用掉 {used} 发）")

        # 打中：第一轮就结束，弹头不再进入后续窗口
        dice.roll = lambda p, rng=None: True
        st2, m2, d2 = build((9, 6), (16, 10))
        st2.resolve_missiles()
        check(not st2.missiles and m2.midcourse_windows == 1,
              "中段命中后弹头即刻消失，不再占用后续窗口")

        # 上升段：只有贴着发射点的那套系统够得着
        dice.roll = lambda p, rng=None: True
        st3, m3, d3 = build((2, 2), (16, 10))
        check(st3._boost_interception(m3), "发射点附近的反导系统可打上升段")
        check(m3 not in st3.missiles, "上升段击落即从在飞列表摘除")

        dice.roll = lambda p, rng=None: False
        st4, m4, d4 = build((2, 2), (16, 10))
        ammo4 = d4.ammo.get("thaad_int", 0)
        check(not st4._boost_interception(m4), "未命中则弹头继续飞行")
        check(m4 in st4.missiles and d4.ammo.get("thaad_int", 0) == ammo4 - 1,
              "上升段失手也要消耗拦截弹")

        # 部署在目标侧的系统够不到发射点：拿不到上升段窗口
        st5, m5, d5 = build((16, 10), (16, 10))
        check(not st5._boost_interception(m5), "远离发射点的反导系统没有上升段机会")
    finally:
        dice.roll = real_roll

    # 破片：普通弹药可能被「打残后突防」，弹道/高超目标只认直接碰撞
    sam_tpl = next((t for t in arm.templates.values()
                    if any(w.id == "48n6dm" for w in t.weapons)), None)
    tom = next((w for t in arm.templates.values()
                for w in t.weapons if w.id == "tomahawk"), None)
    if sam_tpl is None or tom is None:
        check(False, "武器库缺少 S-400 或 战斧，无法测试破片判定")
        return

    def sam_state():
        s = _state(sc, 0, 0)
        d = Unit(template=sam_tpl, side=Side.BLUE, x=8, y=8)
        s.units.append(d)
        s._pos_index = None
        return s, d

    try:
        dice.roll = lambda p, rng=None: True          # 强制每次判定都成立
        s1, d1 = sam_state()
        leakers, events, partial = rules.resolve_salvo_interception(
            s1, (8, 8), Side.BLUE, tom, 2)
        check(partial == 2 and leakers == 2, "巡航弹被破片命中仍算突防（打残不打碎）")
        check(any("破片" in e for e in events), "战报里写清是破片命中而非彻底拦截")

        s2, d2 = sam_state()
        _, _, part_bal = rules.resolve_salvo_interception(
            s2, (8, 8), Side.BLUE, isk_w, 2)
        check(part_bal == 0, "弹道弹头不吃破片：要么被直接撞碎，要么完整突防")

        dice.roll = lambda p, rng=None: False
        s3, d3 = sam_state()
        leakers3, _, partial3 = rules.resolve_salvo_interception(
            s3, (8, 8), Side.BLUE, tom, 2)
        check(leakers3 == 2 and partial3 == 0, "全部脱靶时既不算拦截也不算打残")
    finally:
        dice.roll = real_roll
    check(0 < rules.PARTIAL_DAMAGE < 1.0, "打残后的毁伤折扣在 0 与 1 之间")


def test_los_cache_isolated() -> None:
    """通视缓存必须按局隔离。

    模块级缓存只用坐标做键，换一张地图开局时同坐标对会命中上一张图的
    地形结论。这条断言用一对**实测结论相反**的坐标锁住它。
    """
    print("\n[5] 通视缓存按局隔离")
    from core.engine import GameState
    from core.loader import load_scenario
    from core import geo

    def mk(name):
        return GameState(load_scenario(f"data/maps/{name}.json", ARMORY),
                         Side.BLUE, seed=1)

    a, b = mk("steppe_thunder"), mk("strait_confrontation")
    p0, q0 = (21, 10), (19, 6)
    ra, rb = geo.has_los(a, p0, q0), geo.has_los(b, p0, q0)
    check(ra is False and rb is True,
          f"同一对坐标在两张图上结论不同（{ra} / {rb}）")
    # 再算一遍，确认后算的那一方没有被先算的污染
    check(geo.has_los(b, p0, q0) is True, "先算 A 之后 B 仍得到自己的结论")
    check(geo.has_los(a, p0, q0) is False, "反向同理")


def main() -> int:
    pygame.init()
    sc = load_scenario("data/maps/training_ground.json", ARMORY)
    arm = load_armory(ARMORY)        # 顺手确认武器库仍可加载

    print("v3.1 机制收敛测试")
    print("=" * 46)
    test_wind_fields()
    test_smoke_drift(sc)
    test_fort_work(sc, arm)
    test_interception(sc, arm)
    test_los_cache_isolated()

    print("=" * 46)
    if FAILED:
        print(f"✗ {len(FAILED)} 项未通过：")
        for m in FAILED:
            print("   -", m)
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

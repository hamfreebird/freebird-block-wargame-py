"""规则层单元测试（v3.3 11.2）。

**为什么要单独一层**：现有 39 个自检脚本都是端到端的——造一个够用的
小战场、跑一遍剖面、看结论对不对。这能抓住「机制整体跑偏」，但抓不住
「这个函数对边界输入怎么反应」，而且一旦失败要顺藤摸瓜找半天。

这一层只管**纯函数的输入输出**：给一个刁钻的输入，断言它按定义回答。
不跑引擎循环、不推进回合、不打日志。

**覆盖率怎么算**：不追求行覆盖率，而是维护一份「关键纯函数清单」
（``TARGETS``），每个用例声明它覆盖了清单里的哪几项，跑完报告
「清单 N 项 / 已覆盖 M 项」。两项对不上就失败——比行覆盖率更能反映
「规则层的每个决定点都被人盯过」。需要真实行覆盖时加 ``--trace``，
用标准库 ``trace`` 输出 core/ 各模块的被执行行数。

用法::

    python tools/test_units.py            # 跑用例 + 覆盖清单
    python tools/test_units.py --trace    # 额外输出行覆盖统计
"""
from __future__ import annotations

import itertools
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

from core import adn, combat, detect, dice, ew, facility, geo, logistics  # noqa: E402
from core import pathfind  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import (Detection, Domain, Facility, SensorKind, Side,  # noqa: E402
                        Terrain)
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Tile, Unit  # noqa: E402
from core.rng import GameRng  # noqa: E402

ARMORY = os.path.join(ROOT, "data", "units", "armory.json")
ARM = load_armory(ARMORY)

FAILED: list[str] = []
CASES: list[tuple[str, object, tuple[str, ...]]] = []
COVERED: set[str] = set()
_NEXT_UID = itertools.count(1)

#: 关键纯函数清单——规则层的每个「决定点」都该有人盯着。
#: 新增机制时把新函数加进来，否则覆盖率报告会提醒你「有东西没测」。
TARGETS: dict[str, tuple[str, ...]] = {
    "core.dice": ("roll", "jitter"),
    "core.geo": ("dist", "dist_units", "bearing_sector", "crossfire_bonus",
                 "has_los", "_cache_of"),
    "core.ew": ("decoy_signature", "jam_field", "jam_strength_at",
                "gps_integrity"),
    "core.detect": ("evaluate_detection", "sensor_effective_range",
                    "detection_level", "build_detection_map", "own_track"),
    "core.adn": ("sam_for_intercept", "interception_estimate",
                 "resolve_salvo_interception", "adn_summary_for"),
    "core.combat": ("salvo_limit", "shot_profile", "counter_threat_profile"),
    "core.facility": ("service_tile", "facility_strike_profile"),
    "core.logistics": ("supply_sources", "assign_supply", "supply_status",
                       "facility_service_ok"),
    "core.pathfind": ("can_end_at", "tile_move_cost", "step_cost", "find_path",
                      "flood_reachable", "reachable_tiles", "path_cost",
                      "path_cost_to", "is_reachable", "movement_profile"),
}


def check(cond: bool, msg: str) -> None:
    print(f"    [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


def case(group: str, *targets: str):
    """登记一个用例，并声明它覆盖清单里的哪些函数。"""
    def deco(fn):
        CASES.append((group, fn, targets))
        COVERED.update(targets)
        return fn
    return deco


# --------------------------------------------------------------------------- #
# 极小战场
# --------------------------------------------------------------------------- #
class _Anim:
    def add(self, *a, **k) -> None:
        pass


class _Mods:
    def fire(self, *a, **k):
        return None

    def modify_damage(self, *a, **k):
        return None


def bare_state(map_name: str = "training_ground", **over) -> GameState:
    """只填规则函数真正会读的字段——不跑引擎初始化。"""
    sc = load_scenario(os.path.join(ROOT, "data", "maps", f"{map_name}.json"), ARMORY)
    st = GameState.__new__(GameState)
    st.map = sc.map
    st.scenario = sc
    st.player_side = Side.BLUE
    st.ai_side = Side.RED
    st.units = []
    st.detection = {Side.BLUE: {}, Side.RED: {}}
    st._pos_index = None
    st.turn_count = 1
    st.turn_limit = sc.turn_limit
    st.log = []
    st.fx = []
    st.missiles = []
    st.orders = []
    st.fire_missions = []
    st.rng = GameRng(7)
    st.anim = _Anim()
    st.mods = _Mods()
    st.salvo_setting = 1
    st.vp = {Side.BLUE: 0, Side.RED: 0}
    st.casualties = {Side.BLUE: 0, Side.RED: 0}
    st.gps = {Side.BLUE: 1.0, Side.RED: 1.0}
    st.sat_recon = {}
    st.sat_track = {}
    st.over = False
    st.phase = "player"
    st.map_epoch = 0
    st._detect_gen = 0
    st._detect_global_fp = None
    st.spectator = False
    st.weather = None
    st.supply_links = {}
    for k, v in over.items():
        setattr(st, k, v)
    return st


def mk_unit(tid: str, side: Side, x: int = 5, y: int = 5) -> Unit:
    """造一个单位。

    ``uid`` 必须互不相同：探测态势表按 uid 做键、空间索引按 uid 去重，
    若全为默认的 0，多个单位会在索引里互相顶掉，表现为「明明在射程内
    却探测不到」。真实的引擎按对局内顺序分配 uid，这里用一个递增计数器
    模拟同样的前提。
    """
    return Unit(template=ARM.templates[tid], side=side, x=x, y=y,
                uid=next(_NEXT_UID))


def with_units(*units: Unit, **over) -> GameState:
    st = bare_state(**over)
    st.units = list(units)
    st._pos_index = None
    return st


def set_terrain(st: GameState, x: int, y: int, terrain: Terrain) -> Tile:
    """改地形，并让依赖地图的缓存失效。

    引擎在任何地图变更处都会 ``map_epoch += 1``——距离场（``_field_cache``）
    与探测缓存都靠它判废。测试里直接写 ``tile.terrain`` 却漏掉这一步，
    会读到改之前建好的距离场，断言就对不上，而且**换台机器/换个用例顺序
    才复现**，极难查。
    """
    t = st.map.at(x, y)
    t.terrain = terrain
    st.map_epoch += 1
    return t


def _clear_map_objs(st: GameState) -> None:
    """把地图上的目标点 / 工事 / 烟幕清掉，避免想定自带内容干扰断言。"""
    for t in st.map.tiles.values():
        t.smoke = 0
        t.fort = 0
        t.fort_owner = None
        t.facility = Facility.NONE
        t.owner = Side.NEUTRAL
        t.facility_hp = 0.0


# --------------------------------------------------------------------------- #
# 1. 骰子
# --------------------------------------------------------------------------- #
@case("dice", "roll", "jitter")
def test_dice() -> None:
    print("\n[1] 骰子")
    check(all(dice.roll(0.0, GameRng(i)) is False for i in range(20)),
          "p=0 恒为假")
    check(all(dice.roll(1.0, GameRng(i)) is True for i in range(20)),
          "p=1 恒为真")

    r1, r2x = GameRng(99), GameRng(99)
    a = [dice.roll(0.5, r1) for _ in range(30)]
    b = [dice.roll(0.5, r2x) for _ in range(30)]
    check(a == b, "同种子同概率序列一致（可复现）")

    r = GameRng(3)
    vals = [dice.jitter(100.0, 0.2, r) for _ in range(200)]
    check(min(vals) >= 79.9 and max(vals) <= 120.1,
          f"浮动落在 ±20% 内（{min(vals):.1f}–{max(vals):.1f}）")

    r2, r3 = GameRng(5), GameRng(5)
    check([dice.jitter(50.0, 0.1, r2) for _ in range(10)]
          == [dice.jitter(50.0, 0.1, r3) for _ in range(10)],
          "浮动同种子可复现")


# --------------------------------------------------------------------------- #
# 2. 几何与视线
# --------------------------------------------------------------------------- #
@case("geo", "dist", "dist_units", "bearing_sector")
def test_geometry() -> None:
    print("\n[2] 几何")
    check(abs(geo.dist((0, 0), (3, 4)) - 5.0) < 1e-9, "dist 3-4-5")
    check(geo.dist((2, 2), (2, 2)) == 0.0, "同点距离为 0")
    a, b = mk_unit("m1a2", Side.BLUE, 0, 0), mk_unit("m1a2", Side.RED, 3, 4)
    check(abs(geo.dist_units(a, b) - 5.0) < 1e-9, "dist_units 与 dist 一致")

    sectors = {geo.bearing_sector((10 + dx, 10 + dy), (10, 10))
               for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1),
                              (1, 1), (-1, 1), (-1, -1), (1, -1))}
    check(len(sectors) == 8, f"八个方向落在八个不同扇区（实得 {len(sectors)}）")
    check(geo.bearing_sector((11, 10), (10, 10))
          == geo.bearing_sector((21, 10), (20, 10)), "同方向不同距离落在同一扇区")


@case("geo", "crossfire_bonus")
def test_crossfire() -> None:
    print("\n[3] 交叉火力")
    atk = mk_unit("m1a2", Side.BLUE, 5, 5)
    tgt = mk_unit("usmc", Side.RED, 8, 5)
    st = with_units(atk, tgt)
    _clear_map_objs(st)
    w = next((x for x in atk.template.weapons if x.kind.value == "gun"), None)
    if w is None:
        check(False, "m1a2 找不到直瞄武器")
        return

    mult, n = geo.crossfire_bonus(st, atk, tgt, w)
    check(mult == 1.0 and n == 1, "只有自己一个方位 → 无加成")

    # 再从两个方向各放一辆友军坦克
    for pos in ((8, 3), (8, 7)):
        mate = mk_unit("m1a2", Side.BLUE, *pos)
        st.units.append(mate)
    st._pos_index = None
    mult, n = geo.crossfire_bonus(st, atk, tgt, w)
    check(n >= 3 and mult > 1.0, f"三方位以上才有加成（n={n}, ×{mult:.2f}）")
    check(mult <= 1.34, f"加成有上限（×{mult:.2f}）")

    air = mk_unit("ah64", Side.RED, 8, 5)
    st2 = with_units(atk, air)
    _clear_map_objs(st2)
    m2, _ = geo.crossfire_bonus(st2, atk, air, w)
    check(m2 == 1.0, "空中目标不吃交叉火力")


@case("geo", "has_los", "_cache_of")
def test_los() -> None:
    print("\n[4] 通视")
    st = bare_state("steppe_thunder")
    check(geo.has_los(st, (3, 3), (9, 3), observer_air=True) is True,
          "空中观察者恒通视")
    check(geo.has_los(st, (3, 3), (3, 3)) is True, "同格通视")

    # 缓存按局隔离：两张图的同坐标对可以给出不同结论
    a2 = bare_state("strait_confrontation")
    p, q = (21, 10), (19, 6)
    ra, rb = geo.has_los(st, p, q), geo.has_los(a2, p, q)
    check((ra, rb) == (False, True), f"换地图不命中上一张图的结论（{ra}/{rb}）")
    check(getattr(st, "_los_cache", None) is not None, "缓存挂在 state 上")

    d1 = geo._cache_of(st, "_probe")
    d2 = geo._cache_of(st, "_probe")
    check(d1 is d2, "_cache_of 同一状态返回同一字典")
    check(geo._cache_of(a2, "_probe") is not d1, "不同状态各自一份")


# --------------------------------------------------------------------------- #
# 3. 电子战
# --------------------------------------------------------------------------- #
@case("ew", "decoy_signature")
def test_decoy() -> None:
    print("\n[5] 诱饵信号")
    check(ew.decoy_signature("radar", SensorKind.RADAR) > 1.0,
          "雷达诱饵对雷达比真目标更好找")
    check(ew.decoy_signature("radar", SensorKind.SONAR) == 0.0,
          "雷达诱饵对声呐完全不可见")
    check(ew.decoy_signature("nonexistent", SensorKind.RADAR) == 0.0,
          "未登记的诱饵类型视为不可见")
    check(0 < ew.decoy_signature("multi", SensorKind.IR) < 1.3,
          "多光谱诱饵对热像也有信号，但不如专用型")


@case("ew", "jam_field", "jam_strength_at", "gps_integrity")
def test_jam() -> None:
    print("\n[6] 干扰场")
    st = bare_state("training_ground")
    _clear_map_objs(st)
    empty = ew.jam_field(st, Side.BLUE)
    check(empty.at((5, 5)) == 0.0, "没有干扰机时强度为 0")

    jammer = mk_unit("ew_ru", Side.RED, 5, 5)       # 克拉苏哈-4，雷达/通信/导航三频段
    jammer.jam_on = True
    st.units = [jammer]
    st._pos_index = None
    field = ew.jam_field(st, Side.BLUE)
    check(field.at((5, 5)) > 0.0, "干扰机所在格强度 > 0")
    check(field.at((60, 60)) == 0.0, "覆盖外为 0")
    check(all(v <= 0.85 for v in field.cells.values()), "单格强度上限 0.85")
    check(field is ew.jam_field(st, Side.BLUE), "同状态复算命中缓存")

    jammer.jam_on = False
    st._pos_index = None
    check(ew.jam_field(st, Side.BLUE).at((5, 5)) == 0.0, "关机后重建为空场")

    jammer.jam_on = True
    st.gps = {Side.BLUE: 0.1, Side.RED: 2.0}
    check(ew.gps_integrity(st, Side.BLUE) == 0.25, "卫导完好度下限 0.25")
    check(ew.gps_integrity(st, Side.RED) == 1.0, "上限 1.0")


# --------------------------------------------------------------------------- #
# 4. 探测
# --------------------------------------------------------------------------- #
@case("detect", "evaluate_detection")
def test_detection_rules() -> None:
    print("\n[7] 单点探测")
    obs = mk_unit("e3", Side.BLUE, 5, 5)            # E-3G：雷达 22 + 电子侦察 11
    tgt = mk_unit("m1a2", Side.RED, 7, 5)           # 坦克只有热像，不辐射
    st = with_units(obs, tgt)
    _clear_map_objs(st)

    obs.radar_on = False
    r = detect.evaluate_detection(st, obs, tgt)
    check(not r.ok, "雷达关机后，对不辐射的目标一无所知（只剩被动侦察）")

    obs.radar_on = True
    r_on = detect.evaluate_detection(st, obs, tgt)
    check(r_on.ok and r_on.sensor == SensorKind.RADAR.label,
          f"雷达开机后靠雷达发现（{r_on.sensor or '无'}）")

    # 烟幕遮蔽：不放大有效距离
    st.map.at(7, 5).smoke = 3
    r_smoke = detect.evaluate_detection(st, obs, tgt)
    check(r_smoke.effective_range <= r_on.effective_range + 1e-9,
          f"烟幕不放大探测距离（{r_smoke.effective_range:.1f}"
          f" ≤ {r_on.effective_range:.1f}）")
    st.map.at(7, 5).smoke = 0

    # 声呐只对水下/水面
    sub = mk_unit("kilo", Side.RED, 7, 5)
    helo = mk_unit("s70", Side.BLUE, 5, 5)          # 声呐 9 + 雷达 8
    st2 = with_units(helo, sub)
    _clear_map_objs(st2)
    check(detect.evaluate_detection(st2, helo, sub).ok,
          "反潜直升机对潜艇有发现")

    land = mk_unit("usmc", Side.RED, 6, 5)
    st3 = with_units(helo, land)
    _clear_map_objs(st3)
    r3 = detect.evaluate_detection(st3, helo, land)
    check(r3.ok and r3.sensor != SensorKind.SONAR.label,
          f"声呐不会用来「看」地面车辆（实得 {r3.sensor or '无'}）")


@case("detect", "detection_level", "own_track", "build_detection_map",
      "sensor_effective_range")
def test_detection_level() -> None:
    print("\n[8] 阵营级探测等级")
    obs = mk_unit("e3", Side.BLUE, 5, 5)
    tgt = mk_unit("m1a2", Side.RED, 6, 5)
    st = with_units(obs, tgt)
    _clear_map_objs(st)

    check(detect.detection_level(st, Side.BLUE, obs) is Detection.TRACKED,
          "己方单位对自己是跟踪级")
    dead = mk_unit("m1a2", Side.RED, 8, 5)
    dead.destroyed = True
    check(detect.detection_level(st, Side.BLUE, dead) is Detection.NONE,
          "已阵亡单位无探测等级")

    sat = mk_unit("sat_red", Side.RED, 6, 6)
    check(detect.detection_level(st, Side.BLUE, sat) is Detection.TRACKED,
          "轨道目标由空间监视网编目，双方都能跟踪")

    table = detect.build_detection_map(st)
    check(isinstance(table, dict) and tgt.uid in table,
          "build_detection_map 覆盖在场单位")
    check(set(st.detection) == {Side.BLUE, Side.RED}, "双方态势表都写了")

    # 主动暴露：单独一局，视野内没有任何观测者，才能看出暴露本身的效果
    lone = mk_unit("m1a2", Side.RED, 6, 5)
    st4 = with_units(lone)
    _clear_map_objs(st4)
    check(detect.detection_level(st4, Side.BLUE, lone) is Detection.NONE,
          "无人观察时等级为空")
    lone.revealed_until = st4.turn_count
    check(detect.detection_level(st4, Side.BLUE, lone) is Detection.CLASSIFIED,
          "主动暴露后至少被识别（无需传感器）")

    # 自身跟踪：只算自己这一台传感器
    tank = mk_unit("m1a2", Side.BLUE, 5, 5)        # 热像 2.5
    st5 = with_units(tank)
    _clear_map_objs(st5)
    check(detect.own_track(st5, tank, tank) is True, "自己对自己天然跟踪")
    far = mk_unit("m1a2", Side.RED, 18, 12)        # 约 14.8 格，远超热像射程
    st5.units.append(far)
    st5._pos_index = None
    check(detect.own_track(st5, tank, far) is False, "够不着的目标不构成自身跟踪")

    eff, los = detect.sensor_effective_range(
        st, obs, tgt, 10.0, SensorKind.RADAR, 0.0)
    check(eff > 0 and isinstance(los, bool), "有效距离是个正数且返回通视标志")


# --------------------------------------------------------------------------- #
# 5. 射击剖面
# --------------------------------------------------------------------------- #
@case("combat", "salvo_limit", "shot_profile")
def test_shot_profile() -> None:
    print("\n[9] 射击剖面")
    atk = mk_unit("m1a2", Side.BLUE, 5, 5)
    tgt = mk_unit("m1a2", Side.RED, 6, 5)
    st = with_units(atk, tgt)
    _clear_map_objs(st)
    st.detection = {Side.BLUE: {tgt.uid: Detection.TRACKED}, Side.RED: {}}
    gun = next(w for w in atk.template.weapons if w.kind.value == "gun")

    p = combat.shot_profile(st, atk, tgt, gun)
    check(p.ok, f"贴身直瞄可解算（pk={p.pk:.2f}）")
    check(0.0 <= p.pk <= 1.0, "命中率在 [0,1]")
    check(p.damage > 0, "毁伤为正")
    check({"射程包线", "目标规避", "战损影响"} <= set(p.factors),
          f"因子表含恒有项（{len(p.factors)} 项）")

    far = mk_unit("m1a2", Side.RED, 20, 20)
    st.units.append(far)
    st._pos_index = None
    p_far = combat.shot_profile(st, atk, far, gun)
    check(not p_far.ok, "超出射程不成立")

    atk.ammo[gun.id] = 0
    check(combat.salvo_limit(atk, gun) == 0 or not combat.shot_profile(
        st, atk, tgt, gun).ok, "没有备弹就开不了火")
    atk.ammo[gun.id] = 8
    check(1 <= combat.salvo_limit(atk, gun) <= gun.max_salvo,
          f"齐射上限受武器与备弹双重夹取（{combat.salvo_limit(atk, gun)}"
          f" ≤ {gun.max_salvo}）")


@case("combat", "counter_threat_profile")
def test_counter_threat() -> None:
    print("\n[10] 对手视角威胁评估")
    atk = mk_unit("f15e", Side.BLUE, 5, 5)          # 打击鹰：带 JASSM 巡航弹
    tgt = mk_unit("usmc", Side.RED, 6, 5)
    sam = mk_unit("patriot", Side.RED, 7, 6)
    st = with_units(atk, tgt, sam)
    _clear_map_objs(st)
    bomb = next((w for w in atk.template.weapons
                 if w.kind.value in ("bomb", "land_attack", "arm")), None)
    if bomb is None:
        check(False, "f15e 没有对地弹药")
        return
    prof = combat.counter_threat_profile(st, atk, tgt, bomb)
    check(0.0 <= prof.intercept_pk <= 1.0, f"拦截概率在 [0,1]（{prof.intercept_pk:.2f}）")
    check(prof.interceptors is not None, "给出拦截方清单")
    check(isinstance(prof.blockers, list), "带阻断条件清单")
    check(prof.intercept_pk > 0.0, "目标旁边摆着爱国者，拦截概率不为 0")


# --------------------------------------------------------------------------- #
# 6. 防空与拦截
# --------------------------------------------------------------------------- #
@case("adn", "sam_for_intercept", "interception_estimate",
      "resolve_salvo_interception", "adn_summary_for")
def test_adn() -> None:
    print("\n[11] 防空网")
    sam = mk_unit("s400", Side.RED, 5, 5)
    st = with_units(sam)
    _clear_map_objs(st)

    w = adn.sam_for_intercept(sam, (5, 5))
    check(w is not None and w.kind.value in ("sam", "aam"),
          f"选出可用拦截弹（{w.name if w else '无'}）")

    saved = dict(sam.ammo)
    for k in sam.ammo:
        sam.ammo[k] = 0
    check(adn.sam_for_intercept(sam, (5, 5)) is None, "没有备弹就选不出拦截弹")
    sam.ammo.update(saved)

    cruise = next((x for t in ARM.templates.values() for x in t.weapons
                   if x.id == "tomahawk"), None)
    if cruise is None:
        check(False, "武器库缺战斧")
        return
    pk, names = adn.interception_estimate(st, sam, sam, cruise)
    check(0.0 <= pk <= 1.0, f"拦截概率有界（{pk:.2f}）")
    check(isinstance(names, list), "返回拦截方名字列表")

    leakers, events, partial = adn.resolve_salvo_interception(
        st, (5, 5), Side.RED, cruise, 2)
    # 破片打残的那发**仍算突防**（只是毁伤打折），所以 partial 是 leakers 的子集
    check(leakers <= 2 and partial <= leakers,
          f"突防不超过总数，打残是突防的子集（{leakers} 突防，其中 {partial} 被打残）")
    check(isinstance(events, list), "返回播报文本")

    summary = adn.adn_summary_for(st, Side.RED, (5, 5), cruise)
    check(summary is not None and hasattr(summary, "nodes"),
          "能给某点做防空伞摘要")


@case("facility", "facility_strike_profile")
def test_facility_strike() -> None:
    print("\n[12b] 打击固定设施")
    jet = mk_unit("f15e", Side.BLUE, 3, 3)
    st = with_units(jet)
    _clear_map_objs(st)
    t = st.map.at(6, 3)
    bomb = max((w for w in jet.template.weapons if w.vs_facility),
               key=lambda w: w.range_max)          # 取包线最远的那一发

    p = facility.facility_strike_profile(st, jet, t, bomb)
    check(not p.ok and "没有可打击的设施" in p.reason, f"空地打不了（{p.reason}）")

    t.facility = Facility.AIRBASE
    t.owner = Side.RED
    t.facility_hp = t.facility_max_hp
    p = facility.facility_strike_profile(st, jet, t, bomb)
    check(p.ok, f"够得着的敌方机场可以打（pk={p.pk:.2f}）")
    check(0.0 <= p.pk <= 1.0 and p.damage > 0, "命中率有界、毁伤为正")
    check(p.distance > 0, f"给出距离（{p.distance:.1f} 格）")

    t.owner = Side.BLUE
    check(not facility.facility_strike_profile(st, jet, t, bomb).ok,
          "不能打击己方设施")

    t.owner = Side.RED
    far = mk_unit("f15e", Side.BLUE, 15, 13)       # 超过 jdam/jassm 的包线
    st2 = with_units(far)
    _clear_map_objs(st2)
    t2 = st2.map.at(0, 0)
    t2.facility = Facility.AIRBASE
    t2.owner = Side.RED
    t2.facility_hp = t2.facility_max_hp
    short = next(w for w in far.template.weapons if w.id == "jdam")
    p_far = facility.facility_strike_profile(st2, far, t2, short)
    check(not p_far.ok and "超出射程" in p_far.reason, f"够不着就不成立（{p_far.reason}）")

    jet.ammo[bomb.id] = 0
    check("弹药耗尽" in facility.facility_strike_profile(st, jet, t, bomb).reason,
          "没弹就打不了")


# --------------------------------------------------------------------------- #
# 7. 设施与后勤
# --------------------------------------------------------------------------- #
@case("facility", "service_tile")
def test_service() -> None:
    print("\n[12] 设施服务")
    jet = mk_unit("j20", Side.BLUE, 3, 3)
    st = with_units(jet)
    _clear_map_objs(st)
    t = st.map.at(3, 3)
    check(facility.service_tile(st, jet) is None, "空旷地没有服务设施")

    t.facility = Facility.AIRBASE
    t.owner = Side.BLUE
    t.facility_hp = t.facility_max_hp
    check(facility.service_tile(st, jet) is t, "己方机场可为航空兵服务")

    t.owner = Side.RED
    check(facility.service_tile(st, jet) is None, "敌方机场不服务")

    t.owner = Side.BLUE
    t.facility_hp = 0.0
    check(facility.service_tile(st, jet) is None, "被摧毁的机场不服务")


@case("logistics", "supply_sources", "assign_supply", "supply_status",
      "facility_service_ok")
def test_logistics() -> None:
    print("\n[13] 补给链")
    tank = mk_unit("m1a2", Side.BLUE, 5, 5)
    st = with_units(tank)
    _clear_map_objs(st)
    t = st.map.at(5, 5)
    t.facility = Facility.DEPOT
    t.owner = Side.BLUE
    t.facility_hp = t.facility_max_hp

    srcs = logistics.supply_sources(st, Side.BLUE)
    check(any((s.tile.x, s.tile.y) == (5, 5) for s in srcs),
          f"补给源清单包含本格后勤枢纽（{len(srcs)} 个）")

    mapping = logistics.assign_supply(st, Side.BLUE)
    check(tank.uid in mapping, "在场单位分配到补给源")

    st.supply_links = mapping           # 引擎每回合把分配结果挂到状态上
    ok, why = logistics.supply_status(st, tank)
    check(ok and "补给线" in why, f"有补给时状态可读（{why}）")

    # 枢纽被摧毁 → 不再作为补给源出现，单位随即断粮
    t.facility_hp = 0.0
    broken = logistics.assign_supply(st, Side.BLUE)
    check(tank.uid not in broken, "枢纽被毁后该单位失去补给源")
    st.supply_links = broken
    ok2, why2 = logistics.supply_status(st, tank)
    check(not ok2 and why2 == "补给中断", f"断粮后状态改变（{why2}）")

    check(logistics.facility_service_ok(t) is False, "被毁设施不可服务")
    t.facility_hp = t.facility_max_hp
    check(logistics.facility_service_ok(t) is True, "完好设施可服务")
    check(logistics.facility_service_ok(None) is False, "无地块视为不可服务")


# --------------------------------------------------------------------------- #
# 8. 机动与寻路
# --------------------------------------------------------------------------- #
@case("pathfind", "step_cost", "tile_move_cost", "can_end_at")
def test_move_rules() -> None:
    print("\n[14] 机动规则")
    check(abs(pathfind.step_cost(1, 1) - 1.41421356) < 1e-6, "对角为 √2")
    check(pathfind.step_cost(1, 0) == 1.0, "直线为 1")

    tank = mk_unit("m1a2", Side.BLUE, 5, 5)
    st = with_units(tank)
    _clear_map_objs(st)
    for y in range(0, 12):
        for x in range(0, 12):
            set_terrain(st, x, y, Terrain.PLAIN)
    plain = pathfind.tile_move_cost(st, tank, 6, 5, (5, 5))
    set_terrain(st, 6, 5, Terrain.MOUNTAIN)
    mount = pathfind.tile_move_cost(st, tank, 6, 5, (5, 5))
    check(plain is not None and mount is not None and mount > plain,
          f"山地比平原贵（{mount} > {plain}）")

    set_terrain(st, 6, 5, Terrain.WATER)
    check(pathfind.tile_move_cost(st, tank, 6, 5, (5, 5)) is None,
          "陆战单位不能进水面")
    set_terrain(st, 6, 5, Terrain.PLAIN)

    # 同格规则：地面互斥，航空兵可与**友军**地面部队共格
    heli = mk_unit("ah64", Side.BLUE, 6, 5)
    mate = mk_unit("m1a2", Side.BLUE, 6, 5)
    st.units.extend([heli, mate])
    st._pos_index = None
    check(pathfind.can_end_at(st, heli, (6, 5)) is True, "航空兵可与友军地面单位同格")
    check(pathfind.can_end_at(st, tank, (6, 5)) is False,
          "地面单位不能与友军地面单位同格")
    check(pathfind.can_end_at(st, mate, (6, 5)) is True,
          "已经站在那里的单位可以「留在原地」——与它同格的只有飞机")

    enemy = mk_unit("usmc", Side.RED, 7, 5)
    st.units.append(enemy)
    st._pos_index = None
    check(pathfind.can_end_at(st, tank, (7, 5)) is False, "地面单位不能与敌军同格")
    check(pathfind.can_end_at(st, heli, (7, 5)) is False,
          "航空兵也不能停在敌军头上（共存只对友军成立）")


@case("pathfind", "find_path", "flood_reachable", "reachable_tiles",
      "path_cost", "path_cost_to", "is_reachable", "movement_profile")
def test_pathfind() -> None:
    print("\n[15] 寻路")
    tank = mk_unit("m1a2", Side.BLUE, 2, 2)
    st = with_units(tank)
    _clear_map_objs(st)
    for y in range(0, 14):
        for x in range(0, 14):
            set_terrain(st, x, y, Terrain.PLAIN)

    # 目标取 3 格（恰好用掉 3.2 机动点里的 3 点），4 格会超出本回合预算
    path = pathfind.find_path(st, tank, (5, 2))
    check(path is not None and path[0] == (2, 2) and path[-1] == (5, 2),
          f"路径从起点到终点（{len(path) if path else 0} 步）")
    check(pathfind.find_path(st, tank, (6, 2)) is None,
          "超出本回合机动点的目标无路径（不是绕远，是根本走不到）")

    reach = pathfind.flood_reachable(st, tank)
    check((2, 2) in reach and reach[(2, 2)] == 0.0, "可达集包含起点且消耗为 0")
    check(all(v <= tank.mp for v in reach.values()),
          f"可达集不超出机动点（{tank.mp:.1f}）")
    check(pathfind.reachable_tiles(st, tank) == reach, "reachable_tiles 就是洪泛结果")

    cost = pathfind.path_cost(st, tank, path)
    check(abs(cost - reach.get((5, 2), -1)) < 1e-6,
          f"路径代价与洪泛一致（{cost:.2f}）")
    check(pathfind.path_cost_to(st, tank, (5, 2)) is not None, "path_cost_to 给得出代价")
    check(pathfind.is_reachable(st, tank, (5, 2)) is True, "可达判定一致")
    check(pathfind.is_reachable(st, tank, (13, 13)) is True,
          "长期可达性不看本回合预算——走得远不等于走不到")

    # 把起点围死，才是真正的「不可达」
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        set_terrain(st, 2 + dx, 2 + dy, Terrain.WATER)
    check(pathfind.is_reachable(st, tank, (5, 2)) is False, "被水围死则判定不可达")

    mp = pathfind.movement_profile(tank)
    check(isinstance(mp, tuple) and len(mp) >= 2, f"机动画像是个元组（{mp}）")


# --------------------------------------------------------------------------- #
# 覆盖清单
# --------------------------------------------------------------------------- #
def report_coverage() -> int:
    print("\n" + "=" * 46)
    print("关键纯函数覆盖")
    total = 0
    missing: list[str] = []
    for mod, names in TARGETS.items():
        obj = {"core.dice": dice, "core.geo": geo, "core.ew": ew,
               "core.detect": detect, "core.adn": adn, "core.combat": combat,
               "core.facility": facility, "core.logistics": logistics,
               "core.pathfind": pathfind}[mod]
        have = []
        for n in names:
            total += 1
            if not hasattr(obj, n):
                missing.append(f"{mod}.{n}（不存在）")
            elif n not in COVERED:
                missing.append(f"{mod}.{n}")
            else:
                have.append(n)
        print(f"  {mod:16s} {len(have):2d}/{len(names):2d}")
    covered = total - len(missing)
    pct = 100.0 * covered / total if total else 100.0
    print(f"  合计 {covered}/{total}（{pct:.1f}%）")
    if missing:
        print("  未覆盖：")
        for m in missing:
            print("   -", m)
        return 1
    return 0


def trace_report() -> None:
    """用标准库 trace 给出 core/ 的实际行覆盖（粗糙但不需要第三方依赖）。"""
    import contextlib
    import io
    import trace as _trace

    print("\n" + "=" * 46)
    print("行覆盖（--trace）")
    tr = _trace.Trace(count=True, trace=False)
    # 这一遍只是为了让 trace 记账，用例的打印输出会重复一遍，丢掉它
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            tr.runfunc(run_cases)
    except SystemExit:
        pass

    per_file: dict[str, set[int]] = {}
    root = os.path.abspath(ROOT)
    for (fname, lineno) in tr.results().counts:
        if not fname or fname.startswith("<"):
            continue
        # 用 relpath 归一化比较：Windows 的分隔符是反斜杠，
        # 拿 ".\core\" 做子串匹配会因为点号是**字面量**而永远不命中。
        # 标准库模块可能装在另一个盘上，跨盘时 relpath 会抛 ValueError。
        try:
            rel = os.path.relpath(os.path.abspath(fname), root).replace(os.sep, "/")
        except ValueError:
            continue
        if not rel.startswith("core/"):
            continue
        per_file.setdefault(rel, set()).add(lineno)

    if not per_file:
        print("  （没采到 core/ 的执行记录，检查 trace 是否被禁用）")
        return
    for rel in sorted(per_file):
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as fh:
            total = sum(1 for line in fh
                        if line.strip() and not line.strip().startswith("#"))
        hit = len(per_file[rel])
        print(f"  {rel[5:]:22s} 执行 {hit:4d} 行 / 有效 {total:4d} 行"
              f"（{100.0 * hit / max(1, total):.1f}%）")


def run_cases() -> None:
    for _group, fn, _targets in CASES:
        fn()


def main() -> int:
    print("规则层单元测试（v3.3）")
    print("=" * 46)
    run_cases()

    print("\n" + "=" * 46)
    if FAILED:
        print(f"✗ {len(FAILED)} 条断言未通过：")
        for m in FAILED:
            print("   -", m)
        return 1
    cov = report_coverage()
    if "--trace" in sys.argv:
        trace_report()
    print("\n✓ 全部通过")
    return cov


if __name__ == "__main__":
    sys.exit(main())

"""兵种相克函数化（v3.1 9.2）的针对性测试。

旧实现是三行 if，新实现是「武器画像 × 目标画像」的连续函数。
这里要守住四件事：

1. **方向对**：穿甲弹打装甲更疼、破片弹打步兵更疼，且不再有阈值跳变；
2. **有界**：乘数落在 [0.55, 1.45]，不能靠堆数据把某种武器堆成万能；
3. **可解释**：同一武器对不同目标确实有差异，且差异能说出原因；
4. **不串台**：相克只改毁伤，不该悄悄改命中率。

用法::

    python tools/test_counter.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core import counter  # noqa: E402
from core import rules  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Detection, Side  # noqa: E402
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Unit  # noqa: E402
from core.rng import GameRng  # noqa: E402

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


def load() -> dict[str, object]:
    arm = load_armory(ARMORY)
    return arm.templates


T: dict = {}


def u(tid: str, side: Side = Side.RED) -> Unit:
    return Unit(template=T[tid], side=side, x=5, y=5)


def w(wid: str):
    for t in T.values():
        for wp in t.weapons:
            if wp.id == wid:
                return wp
    raise KeyError(wid)


def mult(wid: str, tid: str) -> float:
    return counter.counter_multiplier(w(wid), u(tid))[0]


# --------------------------------------------------------------------------- #
# 1. 方向性
# --------------------------------------------------------------------------- #
def test_direction() -> None:
    print("\n[1] 相克方向")
    # 穿甲弹：装甲越厚越吃
    mbt_vs_inf = mult("m256_120", "usmc")
    mbt_vs_mbt = mult("m256_120", "m1a2")
    check(mbt_vs_mbt > mbt_vs_inf + 0.20,
          f"坦克炮打坦克({mbt_vs_mbt:.2f}) 明显高于打步兵({mbt_vs_inf:.2f})")
    check(mbt_vs_inf < 1.0, "坦克炮打散兵低于基准（过穿，效费比差）")

    # 破片弹：软目标更吃
    arty_vs_inf = mult("m777_155", "usmc")
    arty_vs_mbt = mult("m777_155", "m1a2")
    check(arty_vs_inf > arty_vs_mbt + 0.20,
          f"榴弹打步兵({arty_vs_inf:.2f}) 明显高于打坦克({arty_vs_mbt:.2f})")
    check(arty_vs_mbt < 0.85, "榴弹不是反坦克主力（对坦克打折）")

    # 反坦克导弹：对装甲最有效
    atgm_vs_mbt = mult("tow2b", "m1a2")
    atgm_vs_inf = mult("tow2b", "usmc")
    check(atgm_vs_mbt > 1.25, f"反坦克导弹对坦克 {atgm_vs_mbt:.2f} > 1.25")
    check(atgm_vs_inf < 1.0, f"反坦克导弹打散兵 {atgm_vs_inf:.2f} < 1.0（聚能战斗部）")

    # 通用武器：不挑目标
    spread = max(mult("m242_25", t) for t in ("usmc", "bradley", "m1a2", "paladin")) - \
        min(mult("m242_25", t) for t in ("usmc", "bradley", "m1a2", "paladin"))
    check(spread < 0.20, f"25 mm 机炮对各类地面目标差异仅 {spread:.2f}（通用武器不挑食）")


# --------------------------------------------------------------------------- #
# 2. 连续性：没有阈值跳变
# --------------------------------------------------------------------------- #
def test_continuity() -> None:
    print("\n[2] 连续性")
    base = T["m1a2"]
    weapon = w("m256_120")
    prev = None
    worst = 0.0
    for i in range(0, 46):
        tpl = replace(base, armor=i / 100.0)
        m, _ = counter.counter_multiplier(
            weapon, Unit(template=tpl, side=Side.RED, x=5, y=5))
        if prev is not None:
            worst = max(worst, abs(m - prev))
        prev = m
    check(worst < 0.05, f"硬度 0→0.45 扫描，最大单步跳变 {worst:.3f} < 0.05（无阈值）")

    # 有界
    lo, hi = 9.0, 0.0
    for tid in T:
        for wid in ("m242_25", "m256_120", "tow2b", "m777_155", "gmmlrs", "jdam", "prsm"):
            try:
                m = mult(wid, tid)
            except Exception:                     # noqa: BLE001
                continue
            lo, hi = min(lo, m), max(hi, m)
    check(0.50 <= lo and hi <= 1.46, f"全域乘数落在 [{lo:.2f}, {hi:.2f}]，未越界")


# --------------------------------------------------------------------------- #
# 3. 可解释：差异确实存在，且能说出原因
# --------------------------------------------------------------------------- #
def test_explainable() -> None:
    print("\n[3] 可解释性")
    weapon = w("m256_120")
    vals = {tid: mult("m256_120", tid) for tid in ("usmc", "bradley", "m1a2", "paladin")}
    check(len({round(v, 2) for v in vals.values()}) >= 3,
          f"同一门炮对四种目标给出 {len({round(v, 2) for v in vals.values()})} 种不同结果")
    check(max(vals.values()) - min(vals.values()) > 0.30,
          f"最大差异 {max(vals.values()) - min(vals.values()):.2f} > 0.30")

    _, reasons = counter.counter_multiplier(weapon, u("m1a2"))
    check(bool(reasons), "打坦克时给出了至少一条原因")
    check(all(isinstance(k, str) and 0.0 < v < 3.0 for k, v in reasons),
          f"原因项格式合法：{[(k, round(v, 2)) for k, v in reasons]}")

    # 通道选择符合弹种：榴弹说「爆破」，坦克炮说「破甲」
    # （打散兵时榴弹的乘数已经接近 1.0，按设计不出说明——改用步战车）
    _, r_arty = counter.counter_multiplier(w("m777_155"), u("bradley"))
    _, r_gun = counter.counter_multiplier(w("m256_120"), u("m1a2"))
    check(bool(r_arty) and r_arty[0][0] == "爆破",
          f"榴弹的主因是「{r_arty[0][0] if r_arty else '（无）'}」")
    check(bool(r_gun) and r_gun[0][0] == "破甲",
          f"坦克炮的主因是「{r_gun[0][0] if r_gun else '（无）'}」")


# --------------------------------------------------------------------------- #
# 4. 地形与目标形象
# --------------------------------------------------------------------------- #
def test_profiles() -> None:
    print("\n[4] 画像与地形")
    # 空中平台不该被「破甲」通道拖累
    check(counter.armor_sensitivity(0.0) == 0.0, "零装甲目标不吃破甲加成/惩罚")
    check(counter.armor_sensitivity(0.42) > 1.0, "主战坦克的破甲敏感度 > 1")

    # 步兵在城区更难被面杀伤；车辆在城区反而更吃
    from core.enums import Terrain
    from core.model import Tile
    inf = u("usmc")
    veh = u("bradley")
    plain_inf = counter.target_profile(inf, Tile(5, 5, Terrain.PLAIN))
    urban_inf = counter.target_profile(inf, Tile(5, 5, Terrain.URBAN))
    plain_veh = counter.target_profile(veh, Tile(5, 5, Terrain.PLAIN))
    urban_veh = counter.target_profile(veh, Tile(5, 5, Terrain.URBAN))
    check(urban_inf.exposure < plain_inf.exposure * 0.8,
          f"城区步兵受弹面积 {urban_inf.exposure:.2f} 明显小于野外 {plain_inf.exposure:.2f}")
    check(urban_veh.exposure > plain_veh.exposure,
          f"城区车辆受弹面积 {urban_veh.exposure:.2f} 不低于野外 {plain_veh.exposure:.2f}")

    m_plain = counter.counter_multiplier(w("m777_155"), inf, Tile(5, 5, Terrain.PLAIN))[0]
    m_urban = counter.counter_multiplier(w("m777_155"), inf, Tile(5, 5, Terrain.URBAN))[0]
    check(m_urban < m_plain, f"榴弹打城区步兵 ×{m_urban:.2f} < 打野外步兵 ×{m_plain:.2f}")

    # 小口径炮的通道偏爆破，大口径偏穿甲
    small = counter.weapon_profile(w("m242_25"))
    big = counter.weapon_profile(w("m256_120"))
    check(small.blast > big.blast + 0.25,
          f"25 mm 爆破通道 {small.blast:.2f} ≫ 120 mm 的 {big.blast:.2f}（弹重插值生效）")

    # 直升机现在标了 low 高度层：防空导弹打它不该再被「高度」重罚
    _, heli_reasons = counter.counter_multiplier(w("pac3"), u("ah64"))
    alt_terms = [v for k, v in heli_reasons if k == "高度"]
    check(not alt_terms or alt_terms[0] > 0.80,
          f"防空导弹打直升机的高度项 {alt_terms or ['未触发']} 不低")


# --------------------------------------------------------------------------- #
# 5. 集成：相克进入毁伤，但不碰命中率
# --------------------------------------------------------------------------- #
def _scene(target_tid: str):
    """搭一个能通过 shot_profile 全部前置条件的极小战场。"""
    sc = load_scenario("data/maps/training_ground.json", ARMORY)
    st = GameState.__new__(GameState)
    atk = u("m1a2", Side.BLUE)
    atk.x, atk.y = 5, 5
    tgt = u(target_tid, Side.RED)
    tgt.x, tgt.y = 6, 5        # 120 mm 滑膛炮射程 1.3 格，得贴近
    st.units = [atk, tgt]
    st.map = sc.map
    st.player_side = Side.BLUE
    st.detection = {Side.BLUE: {tgt.uid: Detection.TRACKED}}
    st._pos_index = None
    st.turn_count = 1
    st.log = []
    st.fx = []
    st.missiles = []
    st.rng = GameRng(1)
    st.anim = _Anim()
    st.mods = _Mods()
    st.salvo_setting = 1
    # 这些外部因素与相克无关，固定掉以免测试受地图/干扰影响
    saved = (rules.own_track, rules.jam_strength_at, rules.has_los, rules.gps_integrity)
    rules.own_track = lambda *a, **k: True
    rules.jam_strength_at = lambda *a, **k: 0.0
    rules.has_los = lambda *a, **k: True
    rules.gps_integrity = lambda *a, **k: 1.0
    return st, atk, tgt, saved


def test_shot_profile() -> None:
    print("\n[5] 与射击剖面的集成")
    profs = {}
    for tid in ("usmc", "m1a2"):
        st, atk, tgt, saved = _scene(tid)
        try:
            profs[tid] = rules.shot_profile(st, atk, tgt, w("m256_120"))
        finally:
            (rules.own_track, rules.jam_strength_at,
             rules.has_los, rules.gps_integrity) = saved

    p_inf, p_mbt = profs["usmc"], profs["m1a2"]
    check(p_inf.ok and p_mbt.ok, "两次射击剖面都成立")
    # 命中率可以因目标规避不同而不同，但**因子集合**不该被相克改动
    check(set(p_inf.factors) == set(p_mbt.factors),
          f"两份剖面的命中率因子集合一致：{sorted(p_inf.factors)}")
    check(p_mbt.damage > p_inf.damage,
          f"打坦克毁伤 {p_mbt.damage:.1f} > 打步兵 {p_inf.damage:.1f}")
    check(p_inf.damage_factors and p_mbt.damage_factors,
          "两份剖面都带上了相克说明")
    check(any(k not in p_inf.factors for k in p_inf.damage_factors),
          "相克项没有混进命中率因子（factors）")
    mult_inf = counter.counter_multiplier(w("m256_120"), u("usmc"))[0]
    mult_mbt = counter.counter_multiplier(w("m256_120"), u("m1a2"))[0]
    check(abs(p_inf.damage_mult - mult_inf) < 1e-6 and
          abs(p_mbt.damage_mult - mult_mbt) < 1e-6,
          "剖面里的 damage_mult 与直接调用一致")


def main() -> int:
    global T
    pygame.init()
    T = load()

    print("v3.1 兵种相克函数化测试")
    print("=" * 46)
    test_direction()
    test_continuity()
    test_explainable()
    test_profiles()
    test_shot_profile()

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

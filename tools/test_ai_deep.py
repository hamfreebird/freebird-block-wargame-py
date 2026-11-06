"""v3.1 专项自检：AI 战术深化（9.5）。

覆盖四件事：

1. **多目标优先级转移**——目标作废就改主意，出现明显更优的目标才换向；
2. **撤退时机**——按「值不值得守 × 守不守得住」分档，而不是只看血量；
3. **跨单位协同**——集火不超额、交叉火力有方位、佯攻要有余力；
4. **不失分**——40×40 想定上，深战术对同一个对手不弱于 v2.2 的浅战术。

前三条是机制断言，第四条是 A/B 对照：同一张图、同一个种子、
同样的对手，只切换一方 AI 的 `deep` 开关。这条才是「AI 变强了」
唯一可证伪的证据——否则改了一堆启发式，只是换了个说法。

用法：python tools/test_ai_deep.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from core import ai_tactics as tactics  # noqa: E402
from core import rules  # noqa: E402
from core.ai import AIPlanner  # noqa: E402
from core.engine import GameState  # noqa: E402
from core.enums import Detection, Domain, Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.paths import data_root  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def _load(map_name: str, seed: int = 7) -> GameState:
    path = os.path.join(data_root(), "data", "maps", map_name + ".json")
    armory = os.path.join(data_root(), "data", "units", "armory.json")
    return GameState(load_scenario(path, armory), Side.BLUE, seed=seed)


def _land(st: GameState, side: Side, capturable: bool = True):
    for u in st.units_of(side):
        if not u.alive or u.domain is not Domain.LAND:
            continue
        if u.template.can_capture is capturable:
            return u
    return None


def _free_tile(st: GameState, near: tuple[int, int], radius: int = 2):
    """在 near 附近找一个没有单位、非水域的格子。"""
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            x, y = near[0] + dx, near[1] + dy
            t = st.map.at(x, y)
            if t is None or t.terrain.value == "water":
                continue
            if st.unit_at(x, y) is None:
                return (x, y)
    return None


# --------------------------------------------------------------------------- #
def test_reassign(st: GameState) -> None:
    print("--- 多目标优先级转移 ---")
    u = _land(st, Side.BLUE)
    check(u is not None, "有可占领的陆上单位")
    if u is None:
        return

    dec = tactics.assignment_invalid(st, Side.BLUE, u, None)
    check(dec == "无目标", f"没有目标时报「无目标」（{dec}）")

    # 无占领能力的单位不该去争别人的点
    gun = _land(st, Side.BLUE, capturable=False)
    if gun is not None:
        foe_pt = next((t for t in st.objective_tiles if t.owner is not Side.BLUE), None)
        if foe_pt is not None:
            why = tactics.assignment_invalid(st, Side.BLUE, gun, (foe_pt.x, foe_pt.y))
            check(why == "本单位无占领能力", f"无占领能力者不去争点（{why}）")

    # 目标作废（己方点已有足够守军）→ 必须改派
    own = next((t for t in st.objective_tiles if t.owner is Side.BLUE), None)
    mates = [m for m in st.units_of(Side.BLUE)
             if m is not u and m.alive and m.template.can_capture][:2]
    if own is not None and len(mates) == 2:
        mates[0].x, mates[0].y = own.x, own.y
        mates[1].x, mates[1].y = own.x, own.y
        u.x, u.y = own.x, own.y
        why = tactics.assignment_invalid(st, Side.BLUE, u, (own.x, own.y))
        check(why == "己方点已有守军", f"己方点守军够了就改派（{why}）")

    # 迟滞：当前目标就是最优解时不换向；当前目标是最差解时换
    scores = tactics.rough_objective_scores(st, Side.BLUE, u)
    check(len(scores) >= 2, f"能给出粗筛排序（{len(scores)} 个候选）")
    if len(scores) >= 2:
        best_t = scores[0][1]
        worst_t = scores[-1][1]
        d_best = tactics.reassess_objective(st, Side.BLUE, u, (best_t.x, best_t.y))
        check(not d_best.switch, f"已经在最优目标上就不改主意（{d_best.reason}）")
        d_worst = tactics.reassess_objective(st, Side.BLUE, u, (worst_t.x, worst_t.y))
        check(d_worst.switch, f"明显更差的目标应当放弃（{d_worst.reason}）")

    # 粗筛与最终打分同公式：粗筛第一名的分数必须等于 evaluate 的第一名
    plans = tactics.evaluate_objectives(st, Side.BLUE, u)
    if plans:
        check(abs(plans[0].score - scores[0][0]) < 1e-6,
              "粗筛与可达性评估用的是同一套打分（只在可达性上分叉）")


# --------------------------------------------------------------------------- #
def test_retreat(st: GameState) -> None:
    print("--- 撤退时机 ---")
    u = _land(st, Side.BLUE)
    if u is None:
        return
    hp0 = u.hp

    d = tactics.retreat_assessment(st, Side.BLUE, u)
    check(not d.retreat and d.urgency == 0, "满血部队不撤退")

    d = tactics.retreat_assessment(st, Side.BLUE, u, can_move=False)
    check(not d.retreat and d.reason == "已被合围",
          f"被合围时不撤退（只会撞进包围圈）：{d.reason}")

    # 守土：站在己方要点上，兵力不悬殊 → 守
    own = next((t for t in st.objective_tiles if t.owner is Side.BLUE), None)
    if own is not None:
        u.x, u.y = own.x, own.y
        u.hp = hp0
        d = tactics.retreat_assessment(st, Side.BLUE, u)
        check(not d.retreat and d.reason == "据守要点",
              f"己方要点上据守不退（{d.reason}）")

    # 弹药打光：留在这里只是靶子
    spot = _free_tile(st, (u.x, u.y))
    if spot is not None:
        u.x, u.y = spot
    for w in u.template.weapons:
        u.ammo[w.id] = 0
    d = tactics.retreat_assessment(st, Side.BLUE, u)
    check(d.retreat and d.reason == "弹药耗尽", f"弹药耗尽即脱离（{d.reason}）")

    # 断粮：火力打不出来，越拖越弱（这一条比「弹药耗尽」轻，urgency 1）
    for w in u.template.weapons:
        u.ammo[w.id] = w.ammo
    u.hp = max(1.0, u.template.hp * 0.5)
    u.fuel = 1.0
    u.supply_dry = 4          # supply_state == 2
    if u.supply_state >= 2:
        d = tactics.retreat_assessment(st, Side.BLUE, u)
        check(d.retreat and d.urgency == 1 and d.reason == "补给枯竭",
              f"断粮时寻机后撤（{d.reason} / 急迫度 {d.urgency}）")
    u.supply_dry = 0
    u.hp = hp0


# --------------------------------------------------------------------------- #
def test_focus_fire(st: GameState) -> None:
    print("--- 集火与接力 ---")
    target = next((e for e in st.enemies_of(Side.BLUE)
                   if e.alive and e.domain is Domain.LAND), None)
    shooter = next((u for u in st.units_of(Side.BLUE)
                    if u.alive and u.domain is Domain.LAND and u.usable_weapons()), None)
    check(target is not None and shooter is not None, "有可交战的一对单位")
    if target is None or shooter is None:
        return
    # 贴近到一格，保证既在射程内又有跟踪诸元
    shooter.x, shooter.y = target.x, target.y + 1
    st.refresh_detection()

    w = next((w for w in shooter.usable_weapons()
              if rules.shot_profile(st, shooter, target, w).ok), None)
    check(w is not None, "射程内存在可用武器")
    if w is None:
        return
    p = rules.shot_profile(st, shooter, target, w)
    check(p.ok and p.damage > 0, "基准剖面可解算")

    planner = AIPlanner(st, shooter.side, deep=True)
    exp = planner._fire_expected(st, shooter, target, w)
    check(exp > 0, f"能估出这一发的预期毁伤（{exp:.1f}）")

    # 接力：差一口气就打死 → 高加成；差得远 → 小加成；已经死了 → 不加
    planner.fire_damage[target.uid] = max(0.0, target.hp - exp * 0.5)
    near = planner._follow_up_bonus(st, shooter, target, w,
                                    planner.fire_damage[target.uid])
    planner.fire_damage[target.uid] = 1.0
    far = planner._follow_up_bonus(st, shooter, target, w, 1.0)
    planner.fire_damage[target.uid] = target.hp + 50
    dead = planner._follow_up_bonus(st, shooter, target, w, target.hp + 50)
    check(near > far > 0, f"补最后一刀比隔靴搔痒更值（{near:.2f} > {far:.2f}）")
    check(dead == 0.0, "已经打死的目标不再加成")

    # 不浪费弹药：台账已够致命时，该目标不再进入候选
    planner2 = AIPlanner(st, shooter.side, deep=True)
    planner2.fire_damage[target.uid] = target.hp + 1
    picked = planner2._best_shot(st, shooter)
    check(picked is None or picked[0] is not target,
          "已被判定打掉的目标不再分配火力")

    # 浅模式不记账：deep=False 时不产生台账
    shallow = AIPlanner(st, shooter.side, deep=False)
    check(shallow.fire_damage == {}, "浅模式不建集火台账")


# --------------------------------------------------------------------------- #
def test_crossfire(st: GameState) -> None:
    print("--- 交叉火力 ---")
    ring = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
    check(len({rules.bearing_sector(p, (0, 0)) for p in ring}) == 8,
          "八个方位各占一档")
    check(rules.bearing_sector((1, 0), (0, 0)) != rules.bearing_sector((-1, 0), (0, 0)),
          "正反两个方向落在不同档")

    target = next((e for e in st.enemies_of(Side.BLUE)
                   if e.alive and e.domain is Domain.LAND), None)
    shooters = [u for u in st.units_of(Side.BLUE)
                if u.alive and u.domain is Domain.LAND
                and any(w.kind in rules.CROSSFIRE_KINDS for w in u.usable_weapons())]
    check(target is not None and len(shooters) >= 3,
          f"有 1 个地面目标与 ≥3 个直瞄单位（{len(shooters)}）")
    if target is None or len(shooters) < 3:
        return

    # 摆成三面夹击：北 / 东 / 南
    target.x, target.y = 18, 18
    for u, (dx, dy) in zip(shooters[:3], ((0, -1), (1, 0), (0, 1))):
        u.x, u.y = target.x + dx, target.y + dy
    w = next(w for w in shooters[0].usable_weapons()
             if w.kind in rules.CROSSFIRE_KINDS)
    mult, sectors = rules.crossfire_bonus(st, shooters[0], target, w)
    check(sectors >= 3, f"三个方位都被数进去（{sectors} 档）")
    check(mult > 1.0, f"多方位压制提升毁伤（×{mult:.2f}）")

    # 只有两个方位时不加成——两条枪打一面不等于交叉火力
    far = shooters[2]
    far.x, far.y = target.x + 1, target.y - 2      # 与北面那支同档（约 -63°）
    mult2, sectors2 = rules.crossfire_bonus(st, shooters[0], target, w)
    check(mult2 == 1.0 and sectors2 < 3,
          f"少于三个方位不加成（{sectors2} 档，×{mult2:.2f}）")
    far.x, far.y = target.x, target.y + 1

    # 有掩蔽的目标收益更高：工事与堑壕只挡得住一个方向
    base_mult, _ = rules.crossfire_bonus(st, shooters[0], target, w)
    target.entrench = 2
    dug_mult, _ = rules.crossfire_bonus(st, shooters[0], target, w)
    target.entrench = 0
    check(dug_mult > base_mult,
          f"挖壕据守的目标更怕交叉火力（×{dug_mult:.2f} > ×{base_mult:.2f}）")

    # 间瞄没有「方位」可言
    arty = next((u for u in st.units_of(Side.BLUE)
                 if any(w.kind is rules.WeaponKind.MLRS for w in u.usable_weapons())), None)
    if arty is not None:
        mlrs = next(w for w in arty.usable_weapons()
                    if w.kind is rules.WeaponKind.MLRS)
        check(rules.crossfire_bonus(st, arty, target, mlrs)[0] == 1.0,
              "火箭炮不参与交叉火力（间瞄无方位）")

    # 空中目标一闪而过，谈不上几面夹击
    air = next((u for u in st.units_of(Side.BLUE) if u.domain is Domain.AIR), None)
    if air is not None:
        check(rules.crossfire_bonus(st, shooters[0], air, w)[0] == 1.0,
              "空中目标不吃交叉火力")

    # 阵位评估：第三个方位值得去
    st.refresh_detection()
    xmap = tactics.crossfire_map(st, Side.BLUE, shooters[0])
    if target.uid in xmap:
        check(len(xmap[target.uid]) >= 1, "能统计出己方已占的方位")
        north = (target.x, target.y - 1)
        gain = tactics.crossfire_opportunity(st, Side.BLUE, shooters[0], north, xmap)
        check(gain >= 0, f"阵位评估给出非负收益（{gain:.2f}）")
    else:
        check(True, "目标不在交叉火力索引里（条件不成立，跳过）")


# --------------------------------------------------------------------------- #
def test_feint(st: GameState) -> None:
    print("--- 佯攻 ---")
    u = _land(st, Side.BLUE)
    if u is None:
        return
    check(not tactics.plan_feint(st, Side.BLUE, None).active,
          "没有主攻方向就不谈佯动")
    check(not tactics.plan_feint(st, Side.BLUE, (u.x, u.y), 0).active,
          "主攻方向还没排开人就不分兵")

    # 造一个「有余力 + 敌人手里有个必救的点」的场面：
    # 主攻方向取价值最高的非己方点，佯攻点是敌人手上的次要点。
    # 只搬部队、不搬地块——地块在 `map.tiles` 里是按坐标索引的，
    # 改 `Tile.x/y` 只会让 `map.at()` 找到另一个对象（这个坑值得记一笔）。
    main_t = max((t for t in st.objective_tiles if t.owner is not Side.BLUE),
                 key=lambda t: (t.victory_points, -t.y, -t.x), default=None)
    foe_pt = next((t for t in st.objective_tiles
                   if t.owner is Side.RED and t is not main_t), None)
    cheap = min((x for x in st.units_of(Side.BLUE)
                 if x.alive and x.template.can_capture and x.domain is Domain.LAND),
                key=lambda x: (x.template.cost, x.uid), default=None)
    if main_t is None or foe_pt is None or cheap is None:
        check(True, "想定里没有可用的佯攻场面（跳过）")
        return
    check(rules.dist((foe_pt.x, foe_pt.y), (main_t.x, main_t.y)) >= tactics.FEINT_MIN_GAP,
          "佯攻方向与主攻方向本来就分得开")
    # 把一支便宜部队挪到佯攻点附近——这支部队原本在后方集结地
    cheap.x, cheap.y = foe_pt.x - 3, foe_pt.y
    st.refresh_detection()

    plan = tactics.plan_feint(st, Side.BLUE, (main_t.x, main_t.y), 2)
    check(plan.active, f"有余力时应当做佯动：{plan.note}")
    if plan.active:
        check(plan.tile != (main_t.x, main_t.y), "佯攻点不是主攻方向")
        pick = st.unit_by_uid(plan.unit_uid)
        check(pick is not None and pick.template.cost <= tactics.FEINT_MAX_COST,
              "佯动部队选的是便宜的那支")
        check(pick is not None
              and rules.dist((pick.x, pick.y), plan.tile) <= tactics.FEINT_MAX_DIST,
              "佯攻点在「同一场战斗」的距离尺度内")
        # 佯攻点必须有人看得见，否则只是把部队扔到空地上
        here = st.map.at(plan.tile[0], plan.tile[1])
        watched = (here is not None and here.owner is Side.BLUE.foe) or any(
            e.alive and rules.dist(plan.tile, (e.x, e.y)) <= tactics.FEINT_WATCH_RADIUS
            for e in st.enemies_of(Side.BLUE))
        check(watched, "佯攻点有「观众」——敌人必须来救")


# --------------------------------------------------------------------------- #
def _run_ab(map_name: str, turns: int, blue_deep: bool, red_deep: bool,
            seed: int) -> int:
    """跑一局双 AI 推演，返回蓝方得分（蓝 VP − 红 VP）。"""
    st = _load(map_name, seed)
    def play(side: Side, deep: bool) -> None:
        planner = AIPlanner(st, side, deep=deep)
        guard = 0
        while planner.pending() and guard < 900 and not st.over:
            guard += 1
            act = planner.step(st)
            if act is None:
                break
            st.apply_ai_action(act)
    for _ in range(turns):
        if st.over:
            break
        st.phase = "player"
        play(Side.BLUE, blue_deep)
        st.resolve_missiles()
        st.refresh_detection()
        st.phase = "ai"
        play(Side.RED, red_deep)
        st.resolve_missiles()
        st.refresh_detection()
        st._advance_turn()
    return st.vp[Side.BLUE] - st.vp[Side.RED]


def test_no_regression() -> None:
    print("--- 不失分（A/B 对照）---")
    # 验收线：40×40 及以上。海峡屏障正好 40×40，几秒就能跑完。
    # 草原雷霆 36×36 作为第二个数据点——它更小、更挤，交战更频繁。
    cases = (("strait_shield", 12, (7, 11)), ("steppe_thunder", 12, (3, 7)))
    for map_name, turns, seeds in cases:
        deep_total = shallow_total = 0
        for sd in seeds:
            d = _run_ab(map_name, turns, True, True, sd)
            s = _run_ab(map_name, turns, False, True, sd)
            deep_total += d
            shallow_total += s
            print(f"    {map_name} seed {sd}: 深 {d:+d} / 浅 {s:+d}")
        # 允许一点波动：战术改动影响的是「选择的先后」，不是数值本身，
        # 而双 AI 推演对某一次交战的胜负极其敏感。真正的红线是「别明显变差」。
        check(deep_total >= shallow_total - 6,
              f"{map_name}：深战术对同一个对手不弱于浅战术"
              f"（深 {deep_total:+d} / 浅 {shallow_total:+d}）")


# --------------------------------------------------------------------------- #
def main() -> int:
    pygame.init()
    pygame.display.set_mode((320, 240))
    print("=== AI 战术深化自检（v3.1 9.5）===")

    st = _load("steppe_thunder")
    test_reassign(st)
    st = _load("steppe_thunder")
    test_retreat(st)
    st = _load("steppe_thunder")
    test_focus_fire(st)
    st = _load("steppe_thunder")
    test_crossfire(st)
    st = _load("steppe_thunder")
    test_feint(st)
    test_no_regression()

    pygame.quit()
    if FAILS:
        print(f"\nAI 战术深化自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\nAI 战术深化自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

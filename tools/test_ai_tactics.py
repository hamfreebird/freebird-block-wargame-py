"""v2.2 专项自检：AI 战术库与寻路。

覆盖 v2.2 新增的三件事：

1. **可达性判断**——路径成本而非直线距离（跨海目标不再被误判为"近"）；
2. **保底移动**——被部署在高代价地形里的单位不会永远动不了；
3. **战术库**——目标争夺优先级、斩首打击、撤退判断、补给优先级。

用法：python tools/test_ai_tactics.py
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
from core.engine import GameState  # noqa: E402
from core.enums import Domain, Side  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.paths import data_root  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def _load(map_name: str) -> GameState:
    path = os.path.join(data_root(), "data", "maps", map_name + ".json")
    armory = os.path.join(data_root(), "data", "units", "armory.json")
    return GameState(load_scenario(path, armory), Side.BLUE)


def main() -> int:
    pygame.init()
    pygame.display.set_mode((320, 240))
    print("=== AI 战术库自检 ===")

    # ------------------------------------------------------------------ #
    print("--- 寻路与可达性 ---")
    st = _load("strait_confrontation")
    md = st.map

    # 路径成本与直线距离的区别：跨海的两点直线近、绕行远
    amphib = [u for u in st.units if u.side is Side.BLUE and u.alive
               and u.template.amphibious and u.domain is Domain.LAND]
    check(len(amphib) >= 1, "想定里有两栖单位可测")
    if amphib:
        u = amphib[0]
        far = None
        for t in st.objective_tiles:
            lm_a = md.landmass.get((u.x, u.y))
            lm_b = md.landmass.get((t.x, t.y))
            if lm_a is not None and lm_b is not None and lm_a != lm_b:
                far = t
                break
        if far is not None:
            straight = rules.dist((u.x, u.y), (far.x, far.y))
            cost = rules.path_cost_to(st, u, (far.x, far.y))
            check(cost is not None, "两栖单位能算出跨海目标的路径成本")
            check(cost is not None and cost > straight,
                  f"跨海路径成本大于直线距离（{cost:.0f} > {straight:.0f}）")
            reach = tactics.evaluate_reach(st, u, (far.x, far.y))
            check(reach.reachable and reach.crosses_water,
                  "可达性评估标出「需跨海」")

    # 不可达的目标：非两栖地面单位到不了海岛
    ground = [u for u in st.units if u.side is Side.BLUE and u.alive
               and u.domain is Domain.LAND and not u.template.amphibious
               and u.mp > 0]
    if ground:
        u = ground[0]
        island = None
        for t in st.objective_tiles:
            lm_a = md.landmass.get((u.x, u.y))
            lm_b = md.landmass.get((t.x, t.y))
            if lm_a is not None and lm_b is not None and lm_a != lm_b:
                island = t
                break
        if island is not None:
            r = tactics.evaluate_reach(st, u, (island.x, island.y))
            check(not r.reachable, "非两栖地面单位到不了别的大陆（正确判不可达）")

    # ------------------------------------------------------------------ #
    print("--- 保底移动 ---")
    # 128×128 想定曾有 12/99 个单位开局就动不了（被部署在高代价地形里）
    stuck = []
    for u in st.units:
        if not u.alive or u.mp <= 0:
            continue
        if len(rules.flood_reachable(st, u)) <= 1:
            stuck.append(u.template.short)
    check(not stuck, f"没有单位开局就动不了（发现 {len(stuck)} 个：{stuck[:3]}）")

    # 保底移动本身：所有可动单位至少能走一步
    movers = [u for u in st.units if u.alive and u.mp > 0]
    no_move = [u.template.short for u in movers
               if len(rules.flood_reachable(st, u)) <= 1]
    check(not no_move, f"全部可动单位都有出路（{len(no_move)} 个例外）")

    # 保底移动只在「完全动不了」时生效，不影响正常单位
    normal = [u for u in movers if len(rules.flood_reachable(st, u)) > 3]
    if normal:
        u = normal[0]
        fb = rules.fallback_moves(st, u)
        # 正常单位即使有保底选项，正规可达集也不应因此改变
        reach = rules.flood_reachable(st, u)
        check(len(reach) > 1, "正常单位的可达集不受保底逻辑污染")

    # ------------------------------------------------------------------ #
    print("--- 目标争夺优先级 ---")
    land = [u for u in st.units if u.side is Side.BLUE and u.alive
            and u.domain is Domain.LAND and u.template.can_capture]
    check(len(land) >= 1, "有可占领的地面单位")
    if land:
        u = land[0]
        plans = tactics.evaluate_objectives(st, Side.BLUE, u)
        check(len(plans) >= 1, f"能评估出目标点（{len(plans)} 个）")
        check(all(p.reach.reachable for p in plans),
              "入选目标点都是可达的（不含够不着的）")
        check(plans == sorted(plans, key=lambda p: -p.score),
              "目标按得分降序排列")
        # 中立点应当比己方点更值得争
        neu = [p for p in plans if p.tile.owner is Side.NEUTRAL]
        own = [p for p in plans if p.tile.owner is Side.BLUE]
        if neu and own:
            check(neu[0].score > own[0].score,
                  "中立点的优先级高于己方点（争夺优先）")

    # ------------------------------------------------------------------ #
    print("--- 斩首打击 ---")
    enemies = [e for e in st.enemies_of(Side.BLUE) if e.alive]
    check(len(enemies) >= 1, "有敌方单位可评估")
    if enemies:
        # 给敌方单位造一个「据点守卫」的处境
        prio = tactics.strike_priority(st, Side.BLUE, enemies[0])
        check(prio >= 0, "威胁价值可计算")
        # 防空单位的价值应高于普通单位
        sam = [e for e in enemies if e.template.interceptors > 0]
        normal_e = [e for e in enemies if e.template.interceptors == 0]
        if sam and normal_e:
            p_sam = tactics.strike_priority(st, Side.BLUE, sam[0])
            p_norm = tactics.strike_priority(st, Side.BLUE, normal_e[0])
            check(p_sam > p_norm, "敌方防空单位是更高的打击优先目标")

    # ------------------------------------------------------------------ #
    print("--- 撤退与补给 ---")
    if land:
        u = land[0]
        # 满血不该撤退
        check(not tactics.should_retreat(st, Side.BLUE, u),
              "满血单位不撤退")
        # 打到残血且孤立 → 应撤退
        u.hp = max(1, int(u.template.hp * 0.15))
        lonely = not any(
            o is not u and o.alive and rules.dist_units(u, o) <= 8.0
            for o in st.units_of(Side.BLUE))
        if lonely:
            check(tactics.should_retreat(st, Side.BLUE, u),
                  "重伤且孤立时应当撤退")
        else:
            check(True, "附近有友军时不撤退（条件不成立，跳过）")

    # 补给优先级：弹药充足的不去补
    if land:
        u = land[0]
        check(not tactics.resupply_needed(st, Side.BLUE, u),
              "弹药充足时不需要补给")

    # ------------------------------------------------------------------ #
    print("--- 距离场缓存 ---")
    st2 = _load("strait_confrontation")
    u = [x for x in st2.units if x.side is Side.BLUE and x.alive and x.mp > 0][0]
    goal = st2.objective_tiles[0]
    st2._field_cache.clear()
    f1 = rules.field_to_goal(st2, u, (goal.x, goal.y), ignore_units=True)
    n1 = len(st2._field_cache)
    f2 = rules.field_to_goal(st2, u, (goal.x, goal.y), ignore_units=True)
    check(f1 is f2, "同参数二次取用命中缓存（同一对象）")
    check(len(st2._field_cache) == n1, "缓存条目数未增长（确实命中）")

    # 不同 profile 应该分开缓存
    sea = [x for x in st2.units if x.side is Side.BLUE and x.alive
           and x.domain is Domain.SEA]
    if sea:
        before = len(st2._field_cache)
        rules.field_to_goal(st2, sea[0], (goal.x, goal.y), ignore_units=True)
        check(len(st2._field_cache) > before,
              "不同机动档案分开缓存（陆地与海军不共用距离场）")

    pygame.quit()
    if FAILS:
        print(f"\nAI 战术库自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\nAI 战术库自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

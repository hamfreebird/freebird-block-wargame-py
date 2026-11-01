#!/usr/bin/env python3
"""方块兵棋 · Block Wargame

回合制现代联合作战推演。运行：

    python main.py                  # 图形界面
    python main.py --check          # 校验数据文件后退出
    python main.py --sim 6          # 无界面推演 6 回合（双 AI）
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.enums import Side  # noqa: E402
from core.loader import LoadError, list_maps, load_scenario  # noqa: E402

MAP_DIR = os.path.join(ROOT, "data", "maps")
ARMORY = os.path.join(ROOT, "data", "units", "armory.json")


def check_data() -> int:
    """校验全部想定与武器库，输出可读报告。"""
    try:
        paths = list_maps(MAP_DIR)
        if not paths:
            print(f"[错误] {MAP_DIR} 中没有 .json 地图文件")
            return 1
        for p in paths:
            sc = load_scenario(p, ARMORY)
            n_b = len([u for u in sc.units if u.side is Side.BLUE])
            n_r = len([u for u in sc.units if u.side is Side.RED])
            print(f"[OK] {sc.map.name:<22} {sc.map.width}x{sc.map.height} "
                  f"{sc.map.tile_km:.0f}km/格  回合 {sc.turn_limit}  蓝{n_b}/红{n_r}")
        armory = load_scenario(paths[0], ARMORY).armory
        print(f"[OK] 武器库: {len(armory.templates)} 兵种 / "
              f"{len(armory.weapons)} 武器 / {len(armory.sensors)} 传感器")
        warns = audit_armory(armory)
        for w in warns[:12]:
            print(f"  [提示] {w}")
        if len(warns) > 12:
            print(f"  … 另有 {len(warns) - 12} 条提示")
    except LoadError as exc:
        print(f"[错误] {exc}")
        return 1
    return 0


def audit_armory(armory) -> list[str]:
    """武器库静态审查：抓出「永远打不到相邻格」之类的数据陷阱。"""
    from core import rules
    from core.enums import WeaponKind

    out: list[str] = []
    direct = (WeaponKind.GUN, WeaponKind.ATGM, WeaponKind.TORPEDO, WeaponKind.BOMB)
    diag = 1.41421356
    for w in armory.weapons.values():
        if w.kind in direct and w.range_max + rules.RANGE_TOLERANCE < diag:
            out.append(f"武器 {w.id} 射程 {w.range_max:.1f} 格，无法攻击斜向相邻格（建议 ≥1.0）")
        if w.max_salvo > 1 and w.max_salvo > w.ammo:
            out.append(f"武器 {w.id} 齐射上限 {w.max_salvo} 超过备弹 {w.ammo}")
    from core.enums import Domain
    for t in armory.templates.values():
        if t.domain is Domain.SPACE:
            continue          # 卫星是纯侦察资产，无武器属正常
        if not t.weapons and not t.sensors:
            out.append(f"兵种 {t.id} 既无武器也无传感器")
    return out


def run_sim(turns: int, map_index: int = 0) -> int:
    """无界面双 AI 推演，用于平衡性测试与回归验证。"""
    import random
    from core.ai import AIPlanner
    from core.engine import GameState

    random.seed(1234)
    paths = list_maps(MAP_DIR)
    sc = load_scenario(paths[min(map_index, len(paths) - 1)], ARMORY)
    st = GameState(sc, Side.BLUE)

    def play(side: Side) -> None:
        planner = AIPlanner(st, side)
        guard = 0
        while planner.pending() and guard < 800 and not st.over:
            guard += 1
            act = planner.step(st)
            if act is None:
                break
            # 与实机 AI 相位共用同一动作分发，避免推演结果失真
            st.apply_ai_action(act)

    for _ in range(turns):
        if st.over:
            break
        st.phase = "player"
        play(Side.BLUE)
        st.resolve_missiles()
        st.refresh_detection()
        st.phase = "ai"
        play(Side.RED)
        st.resolve_missiles()
        st.refresh_detection()
        st._advance_turn()

    print(f"推演 {st.turn_count - 1} 回合｜{sc.map.name}")
    print(f"  存活：蓝 {len(st.units_of(Side.BLUE))} / 红 {len(st.units_of(Side.RED))}")
    print(f"  胜负点：蓝 {st.vp[Side.BLUE]} / 红 {st.vp[Side.RED]}")
    print(f"  战损：蓝 {st.casualties[Side.BLUE]} / 红 {st.casualties[Side.RED]}")
    print(f"  在飞弹药 {len(st.missiles)}｜日志 {len(st.log)} 条")
    for e in st.log[-8:]:
        print(f"    T{e.turn} {e.text}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="方块兵棋 Block Wargame")
    ap.add_argument("--check", action="store_true", help="校验数据文件")
    ap.add_argument("--sim", type=int, metavar="N", help="无界面推演 N 回合")
    ap.add_argument("--map", type=int, default=0, help="推演使用第 N 张地图（从 0 开始）")
    args = ap.parse_args()

    if args.check:
        return check_data()
    if args.sim:
        return run_sim(args.sim, args.map)

    try:
        import pygame  # noqa: F401
    except ImportError:
        print("缺少 pygame。请先安装：\n    pip install pygame-ce\n"
              "或  pip install pygame")
        return 1

    from ui.app import App
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

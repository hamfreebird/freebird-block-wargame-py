"""同格共存规则的针对性测试。

新规则（v1.5）：一个格子里可以有「至多 1 个地面单位 + 若干空中单位」，
且必须同阵营——空中支援地面是常见战术。

用法::

    python tools/test_stack.py
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

from core.enums import Domain, Side  # noqa: E402
from core.loader import load_armory, load_scenario  # noqa: E402
from core.model import Unit  # noqa: E402
from core import rules  # noqa: E402

ARMORY = "data/units/armory.json"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


def main() -> int:
    pygame.init()
    arm = load_armory(ARMORY)
    sc = load_scenario("data/maps/training_ground.json", ARMORY)

    air_ids = [k for k, t in arm.templates.items() if t.domain is Domain.AIR]
    land_ids = [k for k, t in arm.templates.items() if t.domain is not Domain.AIR]
    if not air_ids or not land_ids:
        print("武器库里缺少空中或地面模板，无法测试")
        return 1

    # 构造一个最小状态：同格的地面单位 + 空中单位
    from core.engine import GameState

    class _FakeMap:
        width = height = 8
        tile_km = 12
        name = "test"
        description = ""
        tiles: dict = {}
        objectives: list = []
        meta: dict = {}

        def in_bounds(self, x, y):
            return 0 <= x < 8 and 0 <= y < 8

        def at(self, x, y):
            return None

        def iter_tiles(self):
            return iter(())

    st = GameState.__new__(GameState)
    st.units = []
    st.scenario = sc
    st.map = sc.map
    st.player_side = Side.BLUE
    st.detection = {}
    st._pos_index = None        # 位置索引（惰性重建）

    def mk(tid, side, x, y):
        u = Unit(template=arm.templates[tid], side=side, x=x, y=y)
        st.units.append(u)
        return u

    print("同格共存规则测试")
    print("=" * 46)

    ground = mk(land_ids[0], Side.BLUE, 4, 4)
    air = mk(air_ids[0], Side.BLUE, 4, 4)
    check(len(st.units_at(4, 4)) == 2, "同格可有 1 地面 + 1 空中")
    check(st.unit_at(4, 4) is ground, "unit_at 优先返回地面单位")
    check(len(st.air_units_at(4, 4)) == 1, "air_units_at 只返回空中单位")

    # 新建的空中单位可否加入该格
    air2 = Unit(template=arm.templates[air_ids[1]], side=Side.BLUE, x=0, y=0)
    st.units.append(air2)
    check(rules.can_end_at(st, air2, (4, 4)), "空中单位可加入已有地面+空中的格子")

    # 地面单位不可与地面同格
    ground2 = Unit(template=arm.templates[land_ids[1]], side=Side.BLUE, x=0, y=0)
    st.units.append(ground2)
    check(not rules.can_end_at(st, ground2, (4, 4)), "地面单位不可与另一支地面单位同格")

    # 敌方不可同格
    foe_ground = Unit(template=arm.templates[land_ids[2]], side=Side.RED, x=0, y=0)
    st.units.append(foe_ground)
    check(not rules.can_end_at(st, foe_ground, (4, 4)), "敌方地面单位不可同格")
    foe_air = Unit(template=arm.templates[air_ids[2]], side=Side.RED, x=0, y=0)
    st.units.append(foe_air)
    check(not rules.can_end_at(st, foe_air, (4, 4)), "敌方空中单位不可同格")

    # 机动点消耗：空中单位可进入地面单位所在格
    cost = rules.tile_move_cost(st, air2, 4, 4, (0, 0))
    check(cost is not None, "空中单位可机动到地面单位所在格")
    # 地面单位不可机动到有地面单位的格
    cost2 = rules.tile_move_cost(st, ground2, 4, 4, (0, 0))
    check(cost2 is None, "地面单位不可机动到有地面单位的格")

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("同格共存规则测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

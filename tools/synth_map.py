"""合成大地图：把中型想定平铺成 size×size，用于性能余量基准（v3.3 11.5）。

**与 `tools/make_big_map.py` 的区别**：那个是**内容生成器**——一次性产出
`strait_confrontation.json`（128×128 真实战场，进仓库）；这个是**运行时合成器**
——不落盘、只给基准用。名字刻意分开，免得有人拿错。

**为什么不直接提交一张 256×256 的地图 JSON**：那要 4MB 左右（65536 个地块），
进仓库既拖慢克隆，又会在每次调参时产生「只改了地形」的巨型 diff。基准需要的
是**尺寸**与**地形多样性**，不是某个具体战场，所以平铺现成想定更划算：
同一张源图铺 2×2，地形种类与真实想定一致，生成耗时几十毫秒。

**平铺的副作用要记住**：四象限的地理是重复的，所以这里**不能**用来做 AI 或
平衡性结论——它的唯一用途是量渲染与探测的开销。这条限制写在这里，是为了
避免以后有人拿它跑 `--sim` 然后对着「镜像战场」的结果困惑。
"""
from __future__ import annotations

import copy
import os

from core.loader import MapData, Scenario, load_scenario
from core.model import Objective, Tile, clone_tile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(size: int = 256, source: str = "strait_confrontation",
          armory: str = "") -> Scenario:
    """平铺源想定，返回一个 size×size 的合成想定。"""
    armory = armory or os.path.join(ROOT, "data", "units", "armory.json")
    src = load_scenario(os.path.join(ROOT, "data", "maps", f"{source}.json"), armory)
    sw, sh = src.map.width, src.map.height
    reps_x = max(1, size // sw)
    reps_y = max(1, size // sh)

    tiles: dict[tuple[int, int], Tile] = {}
    landmass: dict[tuple[int, int], int] = {}
    for y in range(size):
        for x in range(size):
            sx, sy = x % sw, y % sh
            t = clone_tile(src.map.tiles[(sx, sy)], x, y)
            tiles[(x, y)] = t
            lid = src.map.landmass.get((sx, sy))
            if lid is not None:
                # 每个重复块算一块独立陆地：否则「隔海的敌人」会被判成同岛，
                # 与真实战场的拓扑差得太远，量出来的寻路开销也就失去意义
                landmass[(x, y)] = lid + 1000 * ((y // sh) * reps_x + (x // sw))

    objectives: list[Objective] = []
    for qy in range(reps_y):
        for qx in range(reps_x):
            for o in src.map.objectives:
                if o.x + qx * sw < size and o.y + qy * sh < size:
                    objectives.append(Objective(
                        x=o.x + qx * sw, y=o.y + qy * sh, name=o.name,
                        kind=o.kind, side=o.side, value=o.value,
                        holder=o.holder))

    big_map = MapData(
        id=f"{src.map.id}_{size}",
        name=f"{src.map.name}（{size}×{size} 合成）",
        width=size, height=size,
        tile_km=src.map.tile_km,
        tiles=tiles,
        objectives=objectives,
        meta=dict(src.map.meta),
        landmass=landmass,
    )

    units = []
    uid = 0
    for qy in range(reps_y):
        for qx in range(reps_x):
            for u in src.units:
                c = copy.deepcopy(u)
                c.x, c.y = u.x + qx * sw, u.y + qy * sh
                uid += 1
                c.uid = uid
                units.append(c)

    return Scenario(
        map=big_map,
        armory=src.armory,
        units=units,
        turn_limit=src.turn_limit,
        side_blue_objectives=list(src.side_blue_objectives),
        raw=dict(src.raw),
        meta=src.meta,
        events=[],
    )


__all__ = ["build", "ROOT"]

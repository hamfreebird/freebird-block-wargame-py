"""几何、视线与规则层共用常量。

本模块是规则层的最底层：它只被别的规则模块引用，自己不引用任何规则模块。
``_cache_of`` 也放在这里——它是「把缓存挂到对局状态上」的通用约定，
电子战与探测都靠它避免模块级缓存污染下一局。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Iterable

from .enums import Domain, SensorKind, Terrain, WeaponKind
from .model import Unit, Weapon

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

NEIGHBORS_8 = [
    (-1, -1), (0, -1), (1, -1),
    (-1, 0), (1, 0),
    (-1, 1), (0, 1), (1, 1),
]

# 地形对视线遮挡的阈值（米）：来射线高出地形高度不足该值即被遮挡
LOS_BLOCK_THRESHOLD = {
    Terrain.MOUNTAIN: 10,
    Terrain.HILLS: 110,
    Terrain.URBAN: 70,
    Terrain.FOREST: 150,
    Terrain.PLAIN: 320,
    Terrain.DESERT: 320,
    Terrain.MARSH: 300,
    Terrain.COAST: 260,
    Terrain.WATER: 400,
}

AIR_OBSERVER_HEIGHT = 3200.0
AIR_TARGET_HEIGHT = 2200.0

# 反卫星交战的抽象距离：轨道目标不与地面共享平面坐标，
# 只要双方均有反卫星手段即视为可交战，射程按此常数校验。
ORBITAL_ENGAGEMENT_DISTANCE = 5.0

# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def dist(a: tuple[int, int], b: tuple[int, int]) -> float:
    """欧氏距离（格）。"""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist_units(u1: Unit, u2: Unit) -> float:
    return dist((u1.x, u1.y), (u2.x, u2.y))

# --------------------------------------------------------------------------- #
# 交叉火力
# --------------------------------------------------------------------------- #
#: 能形成交叉火力的武器：只有直瞄（弹道可见、要靠炮口指向）才谈得上「方位」。
#: 间瞄与面杀伤从哪个方向来都一样，多几个方位并不增加威胁。
CROSSFIRE_KINDS = frozenset({
    WeaponKind.GUN, WeaponKind.ATGM, WeaponKind.CIWS, WeaponKind.TORPEDO,
})
#: 只有落在地面/水面的目标才「有侧面」可打。空中目标一闪而过，
#: 多方位压制来不及形成，因此不参与交叉火力加成。
CROSSFIRE_DOMAINS = frozenset({Domain.LAND, Domain.INFRA, Domain.SEA})
#: 参战方位按 45° 一档统计。八档里占到三档（约 90° 张角）才算真的「交叉」。
CROSSFIRE_SECTORS = 8
#: 超出这个距离的友军不算同一场交战——他们打的是另一场仗。
CROSSFIRE_RADIUS = 16.0


def bearing_sector(src: tuple[int, int], center: tuple[int, int]) -> int:
    """src 相对 center 的方位扇区（0..7，每档 45°）。"""
    ang = math.atan2(src[1] - center[1], src[0] - center[0])
    return int((ang + math.pi) / (2 * math.pi) * CROSSFIRE_SECTORS) % CROSSFIRE_SECTORS


def _can_crossfire(unit: Unit, target: Unit, d: float) -> bool:
    """该单位此刻能否用直瞄火力够到目标（只看射程与目标类型，不问通视）。"""
    for w in unit.usable_weapons():
        if w.kind not in CROSSFIRE_KINDS:
            continue
        if target.domain not in w.targets:
            continue
        if d <= w.range_max + RANGE_TOLERANCE and unit.ammo.get(w.id, 0) > 0:
            return True
    return False


def crossfire_bonus(state: "GameState", attacker: Unit, target: Unit,
                    weapon: Weapon) -> tuple[float, int]:
    """交叉火力对毁伤的加成：``(倍率, 参与方位数)``。

    为什么是毁伤而不是命中率：正面挨一发和侧面挨一发，命中概率是同一个问题，
    差别在于**目标把哪一面朝向威胁**。堑壕、掩体、装甲都是按一个方向加固的，
    换个方位打过去，那份加固就用不上了——所以加成只对有掩蔽的目标明显。

    这也解释了为什么它值钱：它是「工事 + 城区死守」少数几个正经解法之一。
    """
    if weapon.kind not in CROSSFIRE_KINDS or target.domain not in CROSSFIRE_DOMAINS:
        return 1.0, 1
    tpos = (target.x, target.y)
    sectors = {bearing_sector((attacker.x, attacker.y), tpos)}
    for u in state.units:
        if u is attacker or not u.alive or u.side is not attacker.side:
            continue
        d = dist((u.x, u.y), tpos)
        if d > CROSSFIRE_RADIUS:
            continue
        if _can_crossfire(u, target, d):
            sectors.add(bearing_sector((u.x, u.y), tpos))
    n = len(sectors)
    if n < 3:
        return 1.0, n
    # 有掩蔽的目标（工事 / 挖壕 / 城区丘陵）交叉火力收益翻倍
    tile = state.map.at(target.x, target.y)
    covered = target.entrench > 0
    if tile is not None:
        covered = covered or tile.fort_cover_for(target.side) > 0.02 \
            or tile.terrain.armor_bonus > 0.05
    step = 0.11 if covered else 0.07
    extra = min(3, n - 2)
    return 1.0 + step * extra, n

# 近距离（格）：进入该距离内，光学/热像即可完成识别与跟踪。
# 否则「工事隐蔽 + 需跟踪才能开火」会让据守步兵在贴脸距离上完全无法被直瞄火力打击。
CLOSE_TRACK_RANGE = 1.6

# 射程判定容差（格）。机动作战是 8 方向，对角相邻的欧氏距离为 1.414，
# 若不给容差，射程 1.0–1.4 的直瞄武器将永远打不到斜对角的敌人——
# 近战单位会陷入「贴脸却无法开火」的死角。容差取半格，只影响相邻圈。
RANGE_TOLERANCE = 0.45

def _bresenham(x0: int, y0: int, x1: int, y1: int) -> Iterable[tuple[int, int]]:
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    x, y = x0, y0
    while True:
        yield x, y
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy


def has_los(state: "GameState", a: tuple[int, int], b: tuple[int, int],
            observer_air: bool = False, target_air: bool = False) -> bool:
    """判断 a 与 b 之间是否存在通视。

    采用「高度剖面」模型：沿线插值出视线高度，与途经地块海拔比较。
    空中平台默认获得高度优势（可越过地形遮蔽）。

    通视只取决于**地形海拔**——地图在一局内不会变（烟幕影响的是探测
    距离而非视线），因此结果可以缓存。这是探测解算里第二热的地方。
    """
    key = (a, b, observer_air, target_air)
    # 缓存挂在**对局**上，不是模块级：模块级缓存只用坐标做键，换一张地图
    # 开局时同坐标对会命中上一张图的地形结论（实测 (21,10)→(19,6) 在
    # 草原雷霆不可通视、在海峡对决可通视，先算前者就会把后者判错）。
    cache = _cache_of(state, "_los_cache")
    cached = cache.get(key)
    if cached is not None:
        return cached
    result = _compute_los(state, a, b, observer_air, target_air)
    if len(cache) > 20000:
        cache.clear()
    cache[key] = result
    return result


def _compute_los(state: "GameState", a: tuple[int, int], b: tuple[int, int],
                  observer_air: bool, target_air: bool) -> bool:
    md = state.map
    ta = md.at(*a)
    tb = md.at(*b)
    if ta is None or tb is None:
        return False
    if observer_air:
        return True  # 空中平台居高临下，忽略地形遮蔽

    h1 = ta.elevation
    h2 = tb.elevation + (AIR_TARGET_HEIGHT if target_air else 0)
    total = dist(a, b)
    if total < 1e-6:
        return True

    pts = list(_bresenham(a[0], a[1], b[0], b[1]))
    for (x, y) in pts[1:-1]:
        t = md.at(x, y)
        if t is None:
            continue
        f = dist(a, (x, y)) / total
        line_h = h1 + (h2 - h1) * f
        thresh = LOS_BLOCK_THRESHOLD.get(t.terrain, 300)
        if t.elevation - line_h > thresh:
            return False
    return True

def _cache_of(state, attr: str) -> dict:
    """取对局私有的缓存字典（不存在则创建）。

    **缓存必须挂在 state 上，不能放模块级**：模块级缓存若用
    `id(state)` 做键，CPython 回收旧对局后会复用同一地址——
    新对局就会命中**上一局**的距离场。这会让同种子的推演
    得出不同结论，直接破坏「可复现」这条底线（实测平衡结果
    在两次相同测量间从 2:8 变成 1:7）。
    """
    cache = getattr(state, attr, None)
    if cache is None:
        cache = {}
        setattr(state, attr, cache)
    return cache

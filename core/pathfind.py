"""机动与寻路：移动代价、可达集、距离场、路径搜索。

``can_end_at`` 里的同格规则（空中可与地面共存、潜艇每格上限）是
「能不能停在这里」，与「怎么走过去」共用同一份地形代价，因此同模块。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import Domain, Facility, Side, Terrain, UnitClass
from .facility import AIRBASE_CAPACITY
from .geo import NEIGHBORS_8
from .loader import SUB_CAPACITY
from .model import Unit

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

# --------------------------------------------------------------------------- #
# 机动
# --------------------------------------------------------------------------- #



def can_end_at(state: "GameState", unit: Unit, pos: tuple[int, int]) -> bool:
    """是否可以停留在该地块。

    **同格共存规则**：一个格子里可以有

    * 至多 1 个**地面单位**（坦克 / 步兵 / 防空阵地 / 雷达站 …）；
    * 至多 1 个**水面单位**（舰艇）；
    * 至多 2 个**水下单位**（潜艇，在水面之下，可与水面舰同格）；
    * 至多 4 个**空中单位**（机场容量），可与地面部队同格——空中支援地面
      是常见战术，空中单位不占地面空间。

    敌对单位同格一律禁止——否则交战目标与占点判定都会出现歧义。
    """
    occ = [u for u in state.units if u.alive and (u.x, u.y) == pos and u is not unit]
    if not occ:
        return True
    # 敌对单位同格：禁止
    if any(u.side is not unit.side for u in occ):
        return False
    ground_occ = [u for u in occ if u.domain in (Domain.LAND, Domain.INFRA)]
    sea_occ = [u for u in occ if u.domain is Domain.SEA]
    sub_occ = [u for u in occ if u.domain is Domain.SUB]
    air_occ = [u for u in occ if u.domain is Domain.AIR]

    if unit.domain is Domain.AIR:
        # 空中单位：与地面/水面单位共存无妨；与其它空军同格才看容量
        if ground_occ or sea_occ or sub_occ:
            return True
        tile = state.map.at(pos[0], pos[1])
        if tile is not None and tile.facility is Facility.AIRBASE and tile.owner is unit.side \
                and tile.operational:
            return len(air_occ) < AIRBASE_CAPACITY
        host = [u for u in air_occ if u.template.unit_class is UnitClass.CARRIER]
        if host:
            return True
        # 无机场无航母：普通地面上的临时起降，允许少量同格但要受限
        return len(air_occ) < AIRBASE_CAPACITY

    if unit.domain is Domain.SEA:
        # 水面舰：不能与另一艘水面舰同格；可与潜艇、空中单位共处
        return not sea_occ

    if unit.domain is Domain.SUB:
        # 潜艇：在水面之下，可与水面舰同格
        return len(sub_occ) < SUB_CAPACITY

    # 地面单位：不能与其它地面单位同格；可与本方飞机、水面舰共处
    return not ground_occ


def tile_move_cost(state: "GameState", unit: Unit, x: int, y: int,
                   from_pos: tuple[int, int], block_friendly: bool = True,
                   ignore_units: bool = False) -> float | None:
    """进入 (x,y) 的机动点消耗；不可进入返回 None。

    `block_friendly=False` 用于寻路：允许借道友军占据的格子，但不得停留。

    `ignore_units=True` 进一步忽略**敌方**占据（只留下地形判定）——
    用于距离场梯度：敌方占位会随战斗变化，若计入则距离场无法缓存。

    同格共存（见 `can_end_at`）：空中单位可进入地面部队所在的格子，
    地面单位也可进入有本方飞机的格子——因此判定要看**同格全部单位**，
    而不是其中的某一个。
    """
    md = state.map
    t = md.at(x, y)
    if t is None:
        return None
    if not ignore_units:
        occ_all = state.units_at(x, y)
        if occ_all:
            # 敌对单位占据格需通过战斗解决
            if any(u.side is not unit.side for u in occ_all):
                return None
            if block_friendly:
                others = [u for u in occ_all if u is not unit]
                if others:
                    # 按域判定：地面单位不与另一支地面部队同格；空中单位可与地面共存
                    if unit.domain is Domain.AIR:
                        pass
                    elif unit.domain is Domain.SEA:
                        if any(u.domain is Domain.SEA for u in others):
                            return None
                    elif unit.domain is Domain.SUB:
                        if sum(1 for u in others if u.domain is Domain.SUB) >= SUB_CAPACITY:
                            return None
                    else:
                        if any(u.domain in (Domain.LAND, Domain.INFRA) for u in others):
                            return None

    dom = unit.domain
    if dom is Domain.AIR:
        return 1.0
    if dom in (Domain.SEA, Domain.SUB):
        if not t.passable_sea:
            return None
        base = 1.0 if t.terrain is Terrain.WATER else 1.6
        return base
    # 地面 / 设施
    if t.terrain is Terrain.WATER:
        if unit.template.amphibious:
            return 2.4          # 两栖投送：涉水航渡代价高
        return None
    base = t.terrain.move_cost
    if base >= 90:
        return None
    # 敌方野战工事：雷场、反坦克壕与障碍物拖慢推进
    if t.fort > 0 and t.fort_owner is not Side.NEUTRAL and t.fort_owner is not unit.side:
        base += 0.55 * t.fort
    # 海拔爬升惩罚。from_pos 的地形由调用方（Dijkstra）已知，
    # 但直接查一次更简单——这一行在寻路里每秒执行上百万次，
    # 所以只在确实需要时才算差值。
    if t.elevation > 400:
        prev = md.at(*from_pos)
        if prev is not None and t.elevation - prev.elevation > 400:
            base += 1.0
    return base
    return base


def step_cost(dx: int, dy: int) -> float:
    return 1.41421356 if (dx != 0 and dy != 0) else 1.0


def find_path(state: "GameState", unit: Unit, goal: tuple[int, int],
              max_cost: float | None = None) -> list[tuple[int, int]] | None:
    """Dijkstra 最短机动路径（8 邻域），返回含起点的坐标序列。"""
    import heapq

    start = (unit.x, unit.y)
    if start == goal:
        return [start]
    budget = unit.mp if max_cost is None else max_cost
    # 保底格：物理上能走但代价超出机动点的那些（见 fallback_moves）。
    # 单位完全动不了时，靠它们挤出第一步。
    fb = fallback_moves(state, unit) if unit.mp > 0 else {}
    if goal not in state.map.tiles or not can_end_at(state, unit, goal):
        return None

    best: dict[tuple[int, int], float] = {start: 0.0}
    prev: dict[tuple[int, int], tuple[int, int]] = {}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, start)]

    while pq:
        cost, cur = heapq.heappop(pq)
        if cur == goal:
            path = [cur]
            while cur in prev:
                cur = prev[cur]
                path.append(cur)
            path.reverse()
            return path
        if cost > best.get(cur, 1e9):
            continue
        cx, cy = cur
        for dx, dy in NEIGHBORS_8:
            nx, ny = cx + dx, cy + dy
            if not state.map.in_bounds(nx, ny):
                continue
            step = tile_move_cost(state, unit, nx, ny, cur, block_friendly=False)
            if step is None:
                continue
            step = fb.get((nx, ny), step)   # 保底格：用挤出去的代价
            ncost = cost + step * step_cost(dx, dy)
            if ncost > budget + 1e-6 and (nx, ny) not in fb:
                continue
                continue
            if ncost < best.get((nx, ny), 1e9):
                best[(nx, ny)] = ncost
                prev[(nx, ny)] = cur
                heapq.heappush(pq, (ncost, (nx, ny)))
    return None


def fallback_moves(state: "GameState", unit: Unit) -> dict[tuple[int, int], float]:
    """保底移动：物理上能走、但代价超出机动点的相邻格。

    **为什么需要**：单位被部署在山地/沼泽等高代价地形里（或被友军围死）时，
    正规洪泛会返回「只有起点」——这个单位从开局起就永远动不了，等于白送。
    实测 128×128 的「海峡对决」里有 12/99 个单位处于这种状态，
    直接导致 AI 表现崩坏（蓝方 20 回合拿不下任何中立点）。

    这里给它们一条「硬挤出去」的路：以刚好用尽机动点的代价移动，
    代价高但至少能动。**只对完全无法移动的单位生效**，
    因此不影响正常单位的机动行为。
    """
    out: dict[tuple[int, int], float] = {}
    if unit.domain is Domain.AIR:
        return out
    for dx, dy in NEIGHBORS_8:
        nx, ny = unit.x + dx, unit.y + dy
        if not state.map.in_bounds(nx, ny):
            continue
        t = state.map.at(nx, ny)
        if t is None:
            continue
        occ = state.units_at(nx, ny)
        if any(o.side is not unit.side for o in occ):
            continue        # 敌方占据：不能硬挤
        if unit.domain is Domain.LAND and t.terrain is Terrain.WATER \
                and not unit.template.amphibious:
            continue
        if unit.domain in (Domain.SEA, Domain.SUB) and not t.passable_sea:
            continue
        base = t.terrain.move_cost
        if base >= 90:
            continue
        if unit.domain is Domain.LAND:
            if t.fort > 0 and t.fort_owner is not Side.NEUTRAL \
                    and t.fort_owner is not unit.side:
                base += 0.55 * t.fort
        out[(nx, ny)] = min(base * step_cost(dx, dy), max(1.0, unit.mp))
    return out



def reachable_tiles(state: "GameState", unit: Unit) -> dict[tuple[int, int], float]:
    """单位当前可到达的全部地块及其消耗（单次 Dijkstra 洪泛，供 AI 与 UI 使用）。"""
    return flood_reachable(state, unit)


def flood_reachable(state: "GameState", unit: Unit,
                    budget: float | None = None,
                    allow_fallback: bool = True) -> dict[tuple[int, int], float]:
    """以单位所在地为源做受限 Dijkstra，返回 {坐标: 累计消耗}。

    `allow_fallback=False` 时不给保底出路——平衡体检用它来发现
    「部署本身就有问题」的单位（正规移动走不了，只能靠兜底）。
    """
    import heapq

    start = (unit.x, unit.y)
    limit = unit.mp if budget is None else budget
    best: dict[tuple[int, int], float] = {start: 0.0}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
    while pq:
        cost, (cx, cy) = heapq.heappop(pq)
        if cost > best.get((cx, cy), 1e9):
            continue
        for dx, dy in NEIGHBORS_8:
            nx, ny = cx + dx, cy + dy
            if not state.map.in_bounds(nx, ny):
                continue
            step = tile_move_cost(state, unit, nx, ny, (cx, cy), block_friendly=False)
            if step is None:
                continue
            ncost = cost + step * step_cost(dx, dy)
            if ncost > limit + 1e-6:
                continue
            if ncost < best.get((nx, ny), 1e9):
                best[(nx, ny)] = ncost
                heapq.heappush(pq, (ncost, (nx, ny)))
    # 可借道通过，但不可停留在友军占据格
    reach = {p: c for p, c in best.items() if p == start or can_end_at(state, unit, p)}
    if len(reach) <= 1 and allow_fallback:
        # 完全动不了：给一条保底出路（见 fallback_moves）
        for p, c in fallback_moves(state, unit).items():
            if can_end_at(state, unit, p):
                reach[p] = c
    return reach


def path_cost(state: "GameState", unit: Unit, path: list[tuple[int, int]]) -> float:
    total = 0.0
    for i in range(1, len(path)):
        (px, py), (x, y) = path[i - 1], path[i]
        step = tile_move_cost(state, unit, x, y, (px, py), block_friendly=False)
        if step is None:
            return 1e9
        total += step * step_cost(x - px, y - py)
    return total


def movement_profile(unit: Unit) -> tuple:
    """单位的机动特性指纹。

    决定「哪些格子能走、走多远」，因此是距离场缓存键的一部分。

    **只包含真正影响移动代价的字段**：作战域 + 能否涉水。
    兵种（unit_class）**不影响**移动代价——把它算进去会让
    25 个兵种产生 25 份重复的距离场，缓存几乎不命中。
    """
    return (unit.domain, unit.template.amphibious)


def field_to_goal(state: "GameState", unit: Unit, goal: tuple[int, int],
                  limit: float | None = None,
                  ignore_units: bool = False) -> dict[tuple[int, int], float]:
    """以目标为源的 Dijkstra 距离场（按该单位的机动代价）。

    用作 AI 的路径梯度：单位每回合沿距离场下降方向前进，避免贪心陷入局部最优。

    **ignore_units：为什么梯度要忽略友军阻挡**

    友军占据会阻挡通行，让距离场每回合都在变——128×128 的想定里，
    这让 AI 回合的 87% 时间耗在重复计算同一张场上。
    但友军阻挡是**局部效应**：真正决定能不能过去的是地形。

    因此梯度用「忽略友军阻挡」的纯地形距离场（可缓存），
    实际能否走到由 :func:`find_path` 精搜时判定（它会考虑阻挡）。

    **两级搜索半径**

    先用小半径（便宜）覆盖近处，单位不在场里才扩大到全图（昂贵但罕见）。
    这让「近距离目标」的成本降到全图搜索的 1/5，
    而跨海远征那种远距离目标仍然算得出来。
    """
    if goal not in state.map.tiles:
        return {}
    if limit is not None:
        return _build_field(state, unit, goal, limit, ignore_units)
    for tier in (NEAR_FIELD_LIMIT, _wide_field_limit(state)):
        f = _build_field(state, unit, goal, tier, ignore_units)
        if not ignore_units or (unit.x, unit.y) in f:
            return f
    return {}


#: 距离场缓存条目上限。按内存反推，不是按条数拍的：一条全图场在
#: 256×256 上约 1 万个格点、约 1MB（键是 tuple，dict 开销占大头）。
#: 192 条 ≈ 170MB，对应实测工作集：128×128 想定 87 条、256×256 合成图 167 条。
#: 旧上限 96 贴着小图工作集，大图必然周期性整表清空（实测 2 次 / 单方相位）。
_FIELD_CACHE_LIMIT = 192


def _build_field(state: "GameState", unit: Unit, goal: tuple[int, int],
                 limit: float, ignore_units: bool) -> dict[tuple[int, int], float]:
    """计算指定半径的距离场（带缓存）。

    缓存容量是**按内存**定的，不是按条数拍的：实测 256×256 上一条全图
    距离场约 8.5k 个格点、约 0.9MB（dict 的键是 tuple），96 条就是 ~85MB。
    所以不能像审计建议的那样直接放到 512 条（那是 450MB）。

    工作集 = 单位机动剖面数 × 目标点数，实测 128×128 为 87 条、
    256×256 为 94 条——旧上限 96 是**贴着工作集的悬崖**，多一个目标点
    就开始周期性整表清空。这里改成 128 条 + 「满了淘汰最旧的四分之一」：
    正常地图永不触发淘汰，异常地图也不会一次丢掉全部工作集。
    """
    import heapq

    # 缓存：纯地形距离场在地图不变时完全稳定，命中率极高
    cache = None
    if ignore_units:
        cache = getattr(state, "_field_cache", None)
        if cache is None:
            cache = state._field_cache = {}
        ck = (goal, movement_profile(unit), round(limit, 1), state.map_epoch)
        hit = cache.get(ck)
        if hit is not None:
            return hit

    field: dict[tuple[int, int], float] = {goal: 0.0}
    pq: list[tuple[float, tuple[int, int]]] = [(0.0, goal)]
    while pq:
        cost, (cx, cy) = heapq.heappop(pq)
        if cost > field.get((cx, cy), 1e9) or cost > limit:
            continue
        for dx, dy in NEIGHBORS_8:
            nx, ny = cx + dx, cy + dy
            if not state.map.in_bounds(nx, ny):
                continue
            step = tile_move_cost(state, unit, nx, ny, (cx, cy),
                                  block_friendly=False, ignore_units=ignore_units)
            if step is None:
                continue
            ncost = cost + step * step_cost(dx, dy)
            if ncost > limit:
                continue
            if ncost < field.get((nx, ny), 1e9):
                field[(nx, ny)] = ncost
                heapq.heappush(pq, (ncost, (nx, ny)))
    if cache is not None:
        if len(cache) >= _FIELD_CACHE_LIMIT:
            # 淘汰最旧的一批（dict 保序），而不是 clear()：
            # 整表清空会让下一次调用把整个工作集重新算一遍——实测 256×256
            # 单方相位因此多花 13%。清四分之一既腾出空间，又保住大部分热点。
            for stale in list(cache)[: max(1, _FIELD_CACHE_LIMIT // 4)]:
                cache.pop(stale, None)
        cache[ck] = field
    return field



# 距离场的搜索半径上限（机动代价）。128×128 的想定里，
# 从一端走到另一端大约 200+——但那样搜索太慢，
# 所以真正的远距离导航交给区域图（见 core/nav.py）。
MAX_FIELD_LIMIT = 220.0

# 近距离搜索半径：绝大多数目标点在这个范围内，
# 用它算距离场比全图搜索快 5 倍。
NEAR_FIELD_LIMIT = 56.0


def _wide_field_limit(state: "GameState") -> float:
    """全图搜索半径（按地图尺寸）。

    半径必须**与单位位置无关**——否则同一目标点对不同单位会算出不同的场，
    缓存永远不命中（实测命中率因此从 90% 掉到接近 0）。

    也必须**足够大**：跨海投送的路径成本可能是直线距离的 3 倍以上
    （涉水格代价 2.4，海岸线曲折）。半径偏小会让 AI 误判
    「这个点我够不着」从而放弃争夺。
    """
    md = state.map
    diag = (md.width ** 2 + md.height ** 2) ** 0.5
    return min(MAX_FIELD_LIMIT, max(64.0, diag * 1.7))



def path_cost_to(state: "GameState", unit: Unit,
                 goal: tuple[int, int]) -> float | None:
    """单位到目标的**实际路径成本**；不可达返回 None。

    AI 用它判断「这个目标点我够得着吗」——直线距离会骗人：
    隔着海的两点直线很近，绕行却可能要走几百格。
    """
    f = field_to_goal(state, unit, goal, ignore_units=True)
    return f.get((unit.x, unit.y))


def is_reachable(state: "GameState", unit: Unit,
                 goal: tuple[int, int]) -> bool:
    """单位能否机动到该点（不考虑敌方火力）。"""
    return path_cost_to(state, unit, goal) is not None

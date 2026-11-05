"""规则运算：距离、视线、探测链、电子战、命中与拦截解算。

本模块只做纯计算，不修改游戏状态，便于单测与 UI 复用（例如预测命中率）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from . import counter as counter_rules
from .enums import Detection, Domain, Facility, SensorKind, Side, Terrain, UnitClass, WeaponKind
from .rng import GameRng
from .loader import SUB_CAPACITY
from .model import Tile, Unit, Weapon
from .env import environment_for

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
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


# 诱饵信号特征：不同诱饵只在对应频段上"像真的"。
# 数值是探测距离的倍率（>1 表示比真实目标更好找），缺失视为完全不可见。
DECOY_SIGNATURE: dict[str, dict[SensorKind, float]] = {
    "radar":  {SensorKind.RADAR: 1.30, SensorKind.ESM: 1.45},          # 角反射器/干扰机：只骗雷达
    "ir":     {SensorKind.IR: 1.25, SensorKind.OPTICAL: 0.55},         # 热源/红外诱饵：只骗热像
    "visual": {SensorKind.OPTICAL: 1.20},                              # 充气假目标：只骗光学
    "multi":  {SensorKind.RADAR: 1.10, SensorKind.ESM: 1.20,
               SensorKind.IR: 1.10, SensorKind.OPTICAL: 0.80},         # 多光谱诱饵：代价高
}

DECOY_LABELS = {"radar": "雷达诱饵", "ir": "红外诱饵", "visual": "目视假目标",
                "multi": "多光谱诱饵", "thermal": "红外诱饵"}


def decoy_signature(kind: str, sensor: SensorKind) -> float:
    """诱饵在指定传感器频段上的信号强度倍率；0 表示不可见。"""
    return DECOY_SIGNATURE.get(kind, {}).get(sensor, 0.0)


# 近距离（格）：进入该距离内，光学/热像即可完成识别与跟踪。
# 否则「工事隐蔽 + 需跟踪才能开火」会让据守步兵在贴脸距离上完全无法被直瞄火力打击。
CLOSE_TRACK_RANGE = 1.6

# 射程判定容差（格）。机动作战是 8 方向，对角相邻的欧氏距离为 1.414，
# 若不给容差，射程 1.0–1.4 的直瞄武器将永远打不到斜对角的敌人——
# 近战单位会陷入「贴脸却无法开火」的死角。容差取半格，只影响相邻圈。
RANGE_TOLERANCE = 0.45

# 破片战斗部的「打残」概率：防空导弹靠近炸引信与破片杀伤，
# 命中不等于把弹体打碎——弹头可能带着残余能量继续飞。
# 这发仍算突防，但毁伤按 PARTIAL_DAMAGE 打折。
FRAG_PARTIAL_CHANCE = 0.25
PARTIAL_DAMAGE = 0.45

# 弹道导弹的拦截窗口数（中段）。一次窗口只有一次交战机会时，
# 反导系统的备弹再充足也打不出「多轮拦截」的层次；两轮之后弹头进入末段。
MIDCOURSE_WINDOWS = 2

# 上升段拦截的难度系数：弹头还在助推、速度尚未拉满，但拦截窗口只有几十秒，
# 且只有部署到发射点附近的反导系统才够得着。
BOOST_PK_SCALE = 0.35


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
    cached = _los_cache.get(key)
    if cached is not None:
        return cached
    result = _compute_los(state, a, b, observer_air, target_air)
    if len(_los_cache) > 20000:
        _los_cache.clear()
    _los_cache[key] = result
    return result


# 通视缓存：地图地形在一局内不变
_los_cache: dict[tuple, bool] = {}


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


# --------------------------------------------------------------------------- #
# 电子战
# --------------------------------------------------------------------------- #
def jam_strength_at(state: "GameState", pos: tuple[int, int], victim_side,
                    band: str = "radar") -> float:
    """返回某位置上、针对 victim_side 的指定频段干扰强度（0..1）。

    频段化之后，电子干扰不再是「一刀切」的单一乘数：

    * `radar` 频段 —— 压制雷达探测与雷达/半主动制导弹药；
    * `comm` 频段 —— 切断数据链与协同交战（信火一体失效）；
    * `nav` 频段 —— 压制卫星导航，使精确制导弹药退化到惯导精度。

    干扰机只能干扰它**实际具备**的频段，因此针对性关闭某类装备
    （关雷达 / 断数据链 / 转惯导）才是有效的反制。
    """
    return jam_field(state, victim_side, band).at(pos)


# --------------------------------------------------------------------------- #
# 干扰场缓存
# --------------------------------------------------------------------------- #
# 每次探测判定都要问「这个位置的雷达干扰有多强」，而每次都遍历全部
# 单位找干扰机是纯浪费——干扰机的位置与开关在一次解算内是不变的。
# 缓存整张干扰场，把 O(N) 降到 O(1) 查询。

@dataclass
class JamField:
    """某方在某频段上受到的干扰场。

    用「按格累加」的方式构建：遍历所有开机的敌方干扰机，
    把它对覆盖范围内每一格的贡献累加进一张稀疏字典。
    """

    cells: dict[tuple[int, int], float] = field(default_factory=dict)
    max_radius: float = 0.0

    def at(self, pos: tuple[int, int]) -> float:
        return self.cells.get(pos, 0.0)




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



def jam_field(state: "GameState", victim_side, band: str = "radar") -> JamField:
    """取（并缓存）针对 victim_side 的指定频段干扰场。

    缓存键包含所有干扰机的状态指纹——任一干扰机移动或开关都会重建。
    """
    jammers = []
    for u in state.units:
        if not u.alive or u.side is victim_side:
            continue
        tpl = u.template
        if tpl.jam_strength <= 0 or not u.jam_on:
            continue
        if band not in tpl.jam_bands:
            continue
        if tpl.jam_radius <= 0:
            continue
        jammers.append(u)
    if not jammers:
        return JamField()

    fp = tuple((u.uid, u.x, u.y, u.jam_on) for u in jammers)
    key = (id(state), victim_side, band, fp)
    cached = _cache_of(state, "_jam_cache").get(key)
    if cached is not None:
        return cached

    cells: dict[tuple[int, int], float] = {}
    max_r = 0.0
    for u in jammers:
        tpl = u.template
        r = tpl.jam_radius
        max_r = max(max_r, r)
        weight = tpl.jam_band_weights.get(band, 1.0)
        strength = tpl.jam_strength * weight
        x0, x1 = int(u.x - r), int(u.x + r) + 1
        y0, y1 = int(u.y - r), int(u.y + r) + 1
        for cy in range(y0, y1 + 1):
            for cx in range(x0, x1 + 1):
                d = dist((cx, cy), (u.x, u.y))
                if d > r:
                    continue
                falloff = 1.0 - (d / r) ** 2 * 0.55   # 边缘衰减
                cells[(cx, cy)] = cells.get((cx, cy), 0.0) + strength * falloff
    # 上限裁剪：与原实现一致
    field_obj = JamField(cells={k: min(0.85, v) for k, v in cells.items()},
                         max_radius=max_r)
    cache = _cache_of(state, "_jam_cache")
    if len(cache) > 12:
        cache.clear()
    cache[key] = field_obj
    return field_obj


def gps_integrity(state: "GameState", side) -> float:
    """卫星导航完好度：受敌方干扰与反卫星作战影响。"""
    return max(0.25, min(1.0, state.gps[side]))


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
@dataclass
class DetectionResult:
    level: Detection = Detection.NONE
    sensor: str = ""
    effective_range: float = 0.0
    distance: float = 0.0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.level is not Detection.NONE


def sensor_effective_range(state: "GameState", observer: Unit, target: Unit,
                           sensor_range: float, kind: SensorKind,
                           counter_stealth: float = 0.0) -> tuple[float, bool]:
    """计算某传感器对目标的实际探测距离，返回 (距离, 是否满足通视)。"""
    md = state.map
    obs_tile = md.at(observer.x, observer.y)
    tgt_tile = md.at(target.x, target.y)
    if obs_tile is None or tgt_tile is None:
        return 0.0, False

    r = sensor_range

    # 0) 诱饵：只在匹配的频段上产生回波/热信号
    decoy = target.is_decoy
    if decoy:
        sig = decoy_signature(target.decoy_kind, kind)
        if sig <= 0:
            return 0.0, False
        r *= sig

    # 1) 观测方高度优势（雷达波视距）
    if kind is SensorKind.RADAR:
        if observer.domain is Domain.AIR and observer.mission == "cap":
            r *= 1.15      # 巡逻剖面下雷达保持对空搜索
        if observer.domain is Domain.AIR:
            r *= 1.20
        else:
            r *= 1.0 + min(0.30, obs_tile.elevation / 6000.0)
        # 目标高度层
        r *= target.altitude.radar_factor

    # 2) 隐身（米波/超视距雷达可部分抵消隐身设计）；假目标不享受隐身外形
    if kind in (SensorKind.RADAR, SensorKind.IR) and not decoy:
        eff_stealth = max(0.0, target.template.stealth * (1.0 - counter_stealth))
        r *= max(0.08, 1.0 - eff_stealth)

    # 3) 地形遮蔽（地面目标）+ 工事伪装
    if target.domain in (Domain.LAND, Domain.INFRA, Domain.SEA, Domain.SUB):
        r *= tgt_tile.terrain.cover
    if target.domain is Domain.LAND and target.entrench > 0:
        r *= max(0.55, 1.0 - 0.14 * target.entrench)
    # 野战工事：掩体与伪装网
    if tgt_tile.fort > 0 and tgt_tile.fort_owner is target.side:
        r *= max(0.62, 1.0 - 0.07 * tgt_tile.fort)
    if tgt_tile.smoke > 0:
        r *= 0.35

    # 3.5) 被压制：雷达站被打击后效能大幅下降（反辐射/压制打击的战果）
    if observer.suppressed > 0 and kind is SensorKind.RADAR:
        r *= 0.5

    # 4) 电子干扰（仅影响主动/电磁类传感器，按频段分别结算）
    if kind in (SensorKind.RADAR, SensorKind.ESM):
        r *= 1.0 - jam_strength_at(state, (observer.x, observer.y), observer.side,
                                   "radar") * 0.85

    # 4.5) 战场环境：时段与天候改变光电/红外/雷达的探测效能
    env = environment_for(state)
    r *= env.factor(kind)

    # 5) 通视
    los = has_los(
        state, (observer.x, observer.y), (target.x, target.y),
        observer_air=observer.domain is Domain.AIR,
        target_air=target.domain in (Domain.AIR, Domain.SPACE),
    )
    if kind is SensorKind.RADAR and target.domain is Domain.AIR:
        los = True  # 对空探测不受地面起伏影响
    return max(0.0, r), los


def evaluate_detection(state: "GameState", observer: Unit, target: Unit) -> DetectionResult:
    """单观测者对单目标的探测判定，取所有传感器中最高等级。"""
    best = DetectionResult()
    d = dist_units(observer, target)

    for s in observer.template.sensors:
        kind = s.kind

        # 主动雷达需开机
        if kind is SensorKind.RADAR and not observer.radar_on:
            continue
        # 电子侦察只发现辐射源
        if kind is SensorKind.ESM and not target.is_emitting():
            continue
        # 声呐只对水下/水面
        if kind is SensorKind.SONAR and target.domain not in (Domain.SUB, Domain.SEA):
            continue
        if kind is SensorKind.SONAR and target.domain is Domain.SEA and d > s.range_max * 0.4:
            continue

        eff, los = sensor_effective_range(state, observer, target, s.range_max, kind,
                                          s.counter_stealth)
        if kind is SensorKind.ESM:
            eff *= 1.35   # 被动侦察的距离优势
        if eff <= 0 or not los or d > eff:
            continue

        ratio = d / eff
        # strength 越高，能在越远距离上达到「跟踪」
        track_ratio = 0.35 + 0.30 * s.strength
        class_ratio = 0.70 + 0.22 * s.strength
        if ratio <= track_ratio:
            lvl = Detection.TRACKED
        elif ratio <= class_ratio:
            lvl = Detection.CLASSIFIED
        else:
            lvl = Detection.CONTACT
        # 近距离目视/热像识别：贴脸距离上不再受工事伪装影响
        if d <= CLOSE_TRACK_RANGE and lvl.level < Detection.TRACKED.level:
            lvl = Detection.TRACKED

        if lvl.level > best.level.level:
            best = DetectionResult(
                level=lvl,
                sensor=f"{kind.label}",
                effective_range=eff,
                distance=d,
                reason=f"{kind.label}探测 {d:.1f}/{eff:.1f} 格",
            )
    return best


def detection_level(state: "GameState", viewer_side, target: Unit) -> Detection:
    """指定阵营对目标单位的探测等级（聚合全部己方传感器与情报来源）。"""
    return _detection_level(state, viewer_side, target, None)


def _detection_level(state: "GameState", viewer_side, target: Unit,
                     index: "DetectionIndex | None") -> Detection:
    """探测等级解算（可复用外部构建的空间索引）。

    **增量**：单个「观测者 × 目标」的判定结果被缓存。缓存键包含**双方
    所有影响判定的状态指纹**——只要任何一项变了，键就变了，旧结论自动
    作废。这样既不需要在各处维护失效钩子（漏一个就会给出错误结果），
    又能让「只挪动了一个单位」的操作只重算真正受影响的那几对。
    """
    if target.side is viewer_side:
        return Detection.TRACKED
    if not target.alive:
        return Detection.NONE
    # 轨道目标由空间监视网编目：双方都能掌握其轨道，可直接作为反卫星交战目标
    if target.domain is Domain.SPACE:
        return Detection.TRACKED

    # 主动暴露（开火 / 雷达开机 / 干扰机开机）→ 至少识别
    if target.revealed_until >= state.turn_count:
        base = Detection.CLASSIFIED
    else:
        base = Detection.NONE

    # 天基侦察过顶：星下点附近可达跟踪级，覆盖区其余为识别级
    if target.uid in state.sat_recon.get(viewer_side, ()):
        if base.level < Detection.CLASSIFIED.level:
            base = Detection.CLASSIFIED
    if target.uid in getattr(state, "sat_track", {}):
        base = Detection.TRACKED

    cache = getattr(state, "_detect_cache", None)
    env_key = _env_key(state)

    best = base
    for obs in _candidate_observers(state, viewer_side, target, index):
        # 距离剪枝：超出触及距离的观测者不可能探测到目标。
        # 与原先的剪枝阈值一致，判定结论不变。
        if obs.best_sensor_range() * 1.6 + 0.5 < dist_units(obs, target):
            continue
        lvl = None
        if cache is not None:
            ck = (viewer_side.value, obs.uid, obs.detect_gen,
                  target.uid, target.detect_gen, env_key, state._detect_gen)
            lvl = cache.get(ck)
        if lvl is None:
            r = evaluate_detection(state, obs, target)
            lvl = r.level
            if cache is not None:
                cache[ck] = lvl
        if lvl.level > best.level:
            best = lvl
            if best is Detection.TRACKED:
                break
    return best


def _env_key(state: "GameState") -> tuple:
    """环境指纹：时段与天候一变，所有探测距离都要重算。"""
    env = environment_for(state)
    return (env.time_of_day, env.weather)


# --------------------------------------------------------------------------- #
# 探测加速：分层空间索引
# --------------------------------------------------------------------------- #
# 传感器射程有上限（当前数据里最大 22 格），因此「谁能看见谁」是
# **强局部性**的：远处单位之间根本不可能互相探测。原先的做法是对每个
# 目标遍历全部单位，复杂度 O(目标数 × 单位数)——在 256×256 的大型想定里
# 会变成上万次无效的距离比较。
#
# 做法：把观测者按**射程档位**分层，每层按网格分桶。目标按各层的
# 触及距离查询邻格，取并集。射程短的观测者（占大多数）不会因为少数
# 远程单位而被迫被查询。
#
# 正确性：返回的候选集合是**保守超集**——可能包含最终探测不上的单位，
# 但绝不会漏掉能探测上的单位，判定结论与全量遍历完全一致。

# 单位数少于该阈值时直接全扫——建索引的固定开销反而更高
_DIRECT_SCAN_LIMIT = 24


class DetectionIndex:
    """某一方观测者的空间索引。

    **单网格**存所有观测者；查询时取邻格内的候选，再用距离精确剪枝。
    网格边长按该方**最大观测半径**选取，使邻格查询的格数保持常数级：
    边长取 reach/2 时，触及距离正好覆盖 5×5 格的邻域。

    相比「按射程分档分层」，单网格的查询格数更少（不会因多档叠加而膨胀），
    代价是近处会多出一些够不着的候选——但那只是一次廉价的距离比较，
    比多扫几十个空格子划算得多。
    """

    __slots__ = ("cells", "cell", "reach")

    def __init__(self, cells: dict[int, list[Unit]], cell: int, reach: float):
        self.cells = cells
        self.cell = cell
        self.reach = reach

    def candidates(self, tx: int, ty: int) -> list[Unit]:
        """取可能观测到 (tx, ty) 的候选观测者（保守超集）。"""
        out: list[Unit] = []
        seen: set[int] = set()
        cell = self.cell
        reach = self.reach
        span = int(reach // cell) + 1
        cx, cy = tx // cell, ty // cell
        for by in range(cy - span, cy + span + 1):
            row = by << 20
            for bx in range(cx - span, cx + span + 1):
                bucket = self.cells.get(row + bx)
                if not bucket:
                    continue
                for u in bucket:
                    if u.uid in seen:
                        continue
                    seen.add(u.uid)
                    out.append(u)
        return out


def _build_detection_index(state: "GameState", viewer_side) -> DetectionIndex | None:
    """构建（并缓存）某方的观测者空间索引。

    索引只在观测者集合或其位置变化时重建——用状态指纹判断。
    目标移动不影响索引，所以「目标动、观测者不动」时索引一直复用。
    """
    observers = [u for u in state.units
                 if u.side is viewer_side and u.alive and u.template.sensors]
    if len(observers) <= _DIRECT_SCAN_LIMIT:
        return None

    # 指纹：观测者的 uid、位置、射程——任一变化则索引失效
    fp = tuple((u.uid, u.x, u.y, u.best_sensor_range()) for u in observers)
    key = (id(state), viewer_side, fp)
    cached = _cache_of(state, "_index_cache").get(key)
    if cached is not None:
        return cached

    # 单网格：所有观测者按格子归档。
    # 格子边长按最大触及距离的一半选取——触及距离正好覆盖 5×5 格的
    # 邻域：格子更大则近处候选过多，更小则访问的格子数翻倍。
    max_range = 0.0
    for u in observers:
        max_range = max(max_range, u.best_sensor_range())
    # 有效距离可能比标称射程大（高度增益 + 目标高度层 + 机动），
    # 用 1.6 的安全系数，与原先的距离剪枝保持一致。
    reach = max_range * 1.6 + 0.5
    cell = max(4, int(reach / 2))

    cells: dict[int, list[Unit]] = {}
    for u in observers:
        key = (u.y // cell) * 1048576 + (u.x // cell)
        cells.setdefault(key, []).append(u)

    idx = DetectionIndex(cells, cell, reach)
    cache = _cache_of(state, "_index_cache")
    if len(cache) > 8:
        cache.clear()
    cache[key] = idx
    return idx





# --------------------------------------------------------------------------- #
# 探测增量：状态世代号
# --------------------------------------------------------------------------- #
# 「观测者 × 目标」的判定结果可以缓存，但**只在双方状态都没变时**才有效。
# 状态变了就必须重算——漏改一处就会给出错误结论（那比慢更糟）。
#
# 做法：给每个单位记一个「状态指纹」，解算时算一遍当前指纹，
# 与上次记录的对比：变了就把该单位的世代号 +1。
# 缓存键里带上世代号，状态一变键就变，旧结论自动作废。
#
# 这样**不需要在引擎各处挂失效钩子**——移动、开火、开关雷达、
# 被压制、工事建成……无论从哪条路径改的状态，都会被指纹捕获。

def _unit_fingerprint(u: Unit) -> tuple:
    """单位状态指纹：覆盖所有影响「能否被探测到」的字段。"""
    return (u.x, u.y, u.radar_on, u.jam_on, u.suppressed, u.mission,
            u.altitude, u.alive, u.is_decoy, u.decoy_kind, u.entrench,
            u.revealed_until)


def _sync_generations(state: "GameState") -> None:
    """比对单位状态指纹，给状态已变的单位递增世代号。

    同时维护全局世代号：环境（时段/天候）、干扰机态势与地块遮蔽变化
    会影响全部探测结论，同样需要作废缓存。
    """
    cache = getattr(state, "_detect_fp", None)
    if cache is None:
        cache = {}
        state._detect_fp = cache
    for u in state.units:
        fp = _unit_fingerprint(u)
        old = cache.get(u.uid)
        if old != fp:
            cache[u.uid] = fp
            u.detect_gen += 1

    env = environment_for(state)
    jammers = tuple(sorted((u.uid, u.x, u.y, u.jam_on) for u in state.units
                            if u.alive and u.template.jam_strength > 0 and u.jam_on))
    gfp = (env.time_of_day, env.weather, jammers, state.map_epoch)
    if state._detect_global_fp != gfp:
        state._detect_global_fp = gfp
        state._detect_gen += 1


def _candidate_observers(state: "GameState", viewer_side, target: Unit,
                         index: DetectionIndex | None) -> list[Unit]:
    """找出可能观测到 target 的候选观测者。"""
    if index is None:
        return [u for u in state.units
                if u.side is viewer_side and u.alive and u.template.sensors]
    return index.candidates(target.x, target.y)


def own_track(state: "GameState", observer: Unit, target: Unit) -> bool:
    """该单位能否**依靠自身传感器**达到跟踪级（用于判定信火一体是否依赖外部引导）。"""
    if observer is target:
        return True
    if target.domain is Domain.SPACE:
        return True
    return evaluate_detection(state, observer, target).level is Detection.TRACKED


def build_detection_map(state: "GameState") -> dict[int, Detection]:
    """重算全图探测态势，返回 {unit_uid: 该单位对「当前行动方」的探测等级}。

    同时写入双方各自的态势表，供 AI 与 UI 使用。
    """
    result: dict[int, Detection] = {}
    # 先同步世代号：状态变了的单位会带着新世代号，缓存自然失效
    _sync_generations(state)
    for side in (state.player_side, state.player_side.foe):
        # 索引只建一次，供该方所有目标复用
        index = _build_detection_index(state, side)
        table = {}
        for tgt in state.units:
            if not tgt.alive:
                continue
            table[tgt.uid] = _detection_level(state, side, tgt, index)
        state.detection[side] = table
        if side is state.player_side:
            result = table
    return result


# --------------------------------------------------------------------------- #
# 命中解算
# --------------------------------------------------------------------------- #
@dataclass
class ShotProfile:
    """一次射击的完整概率分解，用于 UI 展示与结算。"""

    ok: bool = False
    reason: str = ""
    pk: float = 0.0
    damage: float = 0.0
    distance: float = 0.0
    in_range: bool = False
    has_los: bool = True
    factors: dict[str, float] = field(default_factory=dict)
    #: 相克对**毁伤**的修正（与 factors 分开：factors 乘的是命中率）
    damage_factors: dict[str, float] = field(default_factory=dict)
    damage_mult: float = 1.0
    salvo: int = 1
    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)
    cue_source: str = ""
    cue_penalty: float = 1.0
    notes: list[str] = field(default_factory=list)


def salvo_limit(unit: Unit, weapon: Weapon) -> int:
    """可选的齐射发数上限。

    补给紧张时只能单发点射——多发齐射要开箱、要装填、要驳运，
    断粮的部队打不出这个节奏。这是「断补给就真的打不出该有火力」的第一层。
    """
    if unit.supply_state >= 1:
        return 1
    return max(1, weapon.max_salvo)


def _fire_mission_bonus(state: "GameState", attacker: Unit, target: Unit) -> tuple[float, float, str]:
    """火力准备加成：(命中倍率, 毁伤倍率, 说明)。

    优先调用引擎上的方法（引擎持有准备状态），以免 rules 反向依赖 engine。
    """
    fn = getattr(state, "fire_mission_bonus", None)
    if fn is None:
        return 1.0, 1.0, ""
    return fn(attacker, target)


def shot_profile(state: "GameState", attacker: Unit, target: Unit, weapon: Weapon,
                 origin: tuple[int, int] | None = None) -> ShotProfile:
    """计算射击可行性与命中概率（不消耗弹药、不随机）。

    `origin` 用于在飞弹药：射程应自发射点而非载机当前位置起算。
    """
    ax, ay = origin if origin else (attacker.x, attacker.y)
    p = ShotProfile(distance=dist((ax, ay), (target.x, target.y)),
                    salvo=min(weapon.salvo, salvo_limit(attacker, weapon)))
    is_space = target.domain is Domain.SPACE
    if is_space:
        p.distance = ORBITAL_ENGAGEMENT_DISTANCE

    if not attacker.alive or not target.alive:
        p.reason = "目标已不存在"
        return p
    if target.side is attacker.side:
        p.reason = "不能攻击己方单位"
        return p
    if attacker.mission == "ferry":
        p.reason = "转场剖面：武器保险关闭，无法射击"
        return p
    if attacker.ammo.get(weapon.id, 0) <= 0:
        p.reason = "弹药耗尽"
        return p
    if target.domain not in weapon.targets:
        p.reason = f"{weapon.name} 无法打击{target.domain.label}目标"
        return p
    if is_space and not weapon.asat:
        p.reason = "该武器不具备反卫星能力"
        return p
    if p.distance < weapon.range_min:
        p.reason = f"低于最小射程 {weapon.range_min:.1f} 格"
        return p
    if p.distance > weapon.range_max + RANGE_TOLERANCE:
        p.reason = f"超出射程 {weapon.range_max:.1f} 格"
        return p
    p.in_range = True

    # 探测要求：必须达到武器所需情报等级
    lvl = state.known_level(attacker.side, target)
    if lvl.level < weapon.min_detection.level:
        p.reason = f"情报不足（需{weapon.min_detection.label}，当前{lvl.label}）"
        return p

    # 信火一体：目标由友邻/预警平台提供跟踪诸元，本平台自身并未锁定
    if not own_track(state, attacker, target):
        if attacker.template.datalink and attacker.datalink_on:
            # 通信频段被压制 → 数据链中断，协同交战退化为陈旧诸元
            comm_jam = jam_strength_at(state, (attacker.x, attacker.y), attacker.side, "comm")
            if comm_jam > 0.15:
                p.cue_source = f"己方传感器网（通信受扰 {comm_jam * 100:.0f}%）"
                p.cue_penalty = max(0.45, 1.0 - comm_jam * 0.65)
            else:
                p.cue_source = "己方传感器网（协同交战）"
        elif attacker.template.datalink:
            # 主动切断数据链：牺牲协同能力换取抗干扰
            p.cue_source = "数据链已切断（抗干扰）"
            p.cue_penalty = 0.78
        else:
            p.cue_source = "友邻目标指示（无数据链）"
            p.cue_penalty = 0.78

    # 反辐射导弹：仅能攻击辐射源
    if weapon.guidance.value == "anti_radiation" and not target.is_emitting():
        p.reason = "目标未辐射（雷达关机），反辐射导弹无法锁定"
        return p

    # 通视（轨道目标不适用地形遮蔽）
    if not is_space and (weapon.needs_los or weapon.kind in (WeaponKind.GUN, WeaponKind.ATGM)):
        p.has_los = has_los(
            state, (ax, ay), (target.x, target.y),
            observer_air=attacker.domain is Domain.AIR,
            target_air=target.domain is Domain.AIR,
        )
        if not p.has_los:
            p.reason = "无通视条件"
            return p

    f: dict[str, float] = {}

    # 射程包线
    edge = (p.distance - weapon.range_min) / max(0.1, weapon.range_max - weapon.range_min)
    f["射程包线"] = 1.0 if edge < 0.75 else max(0.55, 1.0 - (edge - 0.75) * 1.4)

    # 电子战：干扰目标所在空域 / 己方制导链路
    if weapon.guidance.vulnerable_to_ew:
        jam_t = jam_strength_at(state, (target.x, target.y), attacker.side)
        jam_a = jam_strength_at(state, (ax, ay), attacker.side)
        f["电子对抗"] = max(0.25, 1.0 - (jam_t * 0.55 + jam_a * 0.35))
    else:
        f["电子对抗"] = 1.0

    # 卫星导航（精确制导弹药依赖 GPS/北斗）
    if weapon.guidance.value == "gps_ins":
        f["导航精度"] = gps_integrity(state, attacker.side)

    # 目标机动规避
    f["目标规避"] = max(0.30, 1.0 - target.template.evasion * 0.55)

    # 烟幕：遮蔽光电/半主动制导与直瞄火力，面压制与卫星/惯性制导不受影响
    tgt_smoke = state.map.at(target.x, target.y)
    if tgt_smoke is not None and tgt_smoke.smoke > 0 and not is_space:
        if weapon.guidance.value in ("optical", "ir", "radar_semi") or \
                weapon.kind in (WeaponKind.GUN, WeaponKind.ATGM):
            f["烟幕遮蔽"] = 0.55

    # 制空巡逻剖面：保持雷达与导弹战备，空战命中更高
    if attacker.domain is Domain.AIR and attacker.mission == "cap" and weapon.kind is WeaponKind.AAM:
        f["巡逻战备"] = 1.15

    # 压制效果
    if attacker.suppressed > 0:
        f["己方被压制"] = 0.80

    # 战损对射击的影响
    f["战损影响"] = 0.75 + 0.25 * attacker.hp_ratio

    # 补给枯竭：缺备件意味着观瞄失校、发射机构带病工作，散布变大
    if attacker.supply_state >= 2:
        f["补给枯竭"] = 0.88

    if p.cue_penalty < 1.0:
        f["外部引导"] = p.cue_penalty

    # 火力准备：射界内的间瞄射击获得显著加成
    prep_pk, prep_dmg, prep_note = _fire_mission_bonus(state, attacker, target)
    if prep_pk != 1.0:
        f["火力准备"] = prep_pk
        p.notes.append(prep_note)

    pk = weapon.pk
    for v in f.values():
        pk *= v
    p.factors = f
    p.pk = max(0.02, min(0.97, pk))

    # 命中后伤害（穿甲 vs 装甲 / 地形掩护 / 工事 / 兵种相克）
    if is_space:
        p.damage = weapon.damage
    else:
        tgt_tile = state.map.at(target.x, target.y)
        cover = tgt_tile.terrain.armor_bonus if tgt_tile and target.domain in (Domain.LAND, Domain.INFRA) else 0.0
        # 构筑工事：步兵可挖壕据守，显著降低被杀伤
        ent = target.entrench * (0.10 + target.template.entrench_bonus)
        # 野战工事（堑壕/掩体/雷场）：归属方在阵地上额外减伤
        fort = tgt_tile.fort_cover_for(target.side) if tgt_tile else 0.0
        mitigation = 1.0 - min(0.90, target.template.armor * 0.85 + cover + ent + fort)
        dmg = weapon.damage * mitigation
        # 火力准备：射界内预先标定的射击毁伤更高
        if prep_dmg != 1.0:
            dmg *= prep_dmg
        # 兵种相克：目标特性 × 武器特性的连续函数（core/counter.py）。
        # 城区不再单独乘一个固定系数——它作为目标的「掩蔽 / 疏散空间」
        # 进入了目标画像，与武器通道一起决定结果。
        counter, reasons = counter_rules.counter_multiplier(weapon, target, tgt_tile)
        p.damage_mult = counter
        if abs(counter - 1.0) >= 0.005:
            dmg *= counter
            p.damage_factors = dict(reasons)
        p.damage = dmg
    p.ok = True

    # 拦截评估（反弹道/反卫星拦截不适用现役防空网）
    if is_space:
        p.intercept_pk, p.interceptors = 0.0, []
    else:
        p.intercept_pk, p.interceptors = interception_estimate(state, attacker, target, weapon)
    return p


def sam_for_intercept(d: Unit, pos: tuple[int, int]) -> Weapon | None:
    """为单位选择用于拦截的防空弹：射程够、有备弹、非中段专用弹。

    区域防空用防空导弹（SAM）；点防御可用主动雷达空空弹（AAM），
    但效果略低（专用发射架与火控都更弱）。
    """
    best = None
    for w in d.template.weapons:
        if w.kind not in (WeaponKind.SAM, WeaponKind.AAM) or w.midcourse or w.ciws:
            continue
        if d.ammo.get(w.id, 0) <= 0:
            continue
        if dist((d.x, d.y), pos) > w.range_max + RANGE_TOLERANCE:
            continue
        if best is None or w.range_max > best.range_max:
            best = w
    return best


# --------------------------------------------------------------------------- #
# 多点防空协同（IADS）
# --------------------------------------------------------------------------- #
@dataclass
class ADNNode:
    """防空网中的一个节点——通常是一套防空系统。

    节点记录自己的分工、可用火控通道与剩余拦截弹，网据此统一分配任务。
    """

    unit: Unit
    weapon: Weapon | None
    pk: float                  # 对该目标的单发拦截概率
    channels: int              # 剩余火控通道
    ammo: int                  # 剩余拦截弹
    role: str                  # 分工：区域防空 / 要地防空 / 点防御 / 末段硬杀伤
    ciws: bool = False

    @property
    def name(self) -> str:
        return self.unit.name


@dataclass
class AirDefenseNetwork:
    """一体化防空网：跨单位统一分配火控通道。

    与「各打各的」的关键区别：
    * 火控通道由**网**统一记账与分配，而不是每个单位各自为战；
    * 网会按单发拦截概率**择优派工**——多发弹药同时来袭时，
      不会让每个单位都对着第一发猛打，而是分给最合适的节点；
    * 每个节点有明确**分工**（区域 / 要地 / 点防御 / 末段），
      交战面板会把分工显示给玩家。
    """

    side: Side
    nodes: list[ADNNode] = field(default_factory=list)

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(cls, state: "GameState", side: Side, pos: tuple[int, int],
              weapon: Weapon, self_unit: Unit | None = None) -> "AirDefenseNetwork":
        """针对来袭弹药构建防空网。"""
        net = cls(side=side)
        for d in state.units:
            if not d.alive or d.side is not side or d.is_decoy:
                continue
            # -- 区域防空：SAM / AAM --
            if d.template.interceptors > 0 and d.template.intercept_pk > 0:
                sam = sam_for_intercept(d, pos)
                if sam is not None and d.intercepts_used < d.template.interceptors:
                    rng = sam.range_max
                    if dist((d.x, d.y), pos) <= rng + RANGE_TOLERANCE:
                        jam = jam_strength_at(state, (d.x, d.y), d.side, "radar")
                        pk = d.template.intercept_pk * (1.0 - weapon.evasion)
                        pk *= (1.0 - jam * 0.6) * (1.0 - weapon.stealth * 0.7)
                        if sam.kind is WeaponKind.AAM:
                            pk *= 0.85
                        if weapon.ballistic:
                            pk *= 0.30
                        if weapon.strategic:
                            continue
                        own = self_unit is not None and d is self_unit
                        if own:
                            pk *= 0.6
                        if pk > 0.01:
                            net.nodes.append(ADNNode(
                                unit=d, weapon=sam, pk=min(0.95, pk),
                                channels=_channels_of(d),
                                ammo=d.ammo.get(sam.id, 0),
                                role=_adn_role(sam, own),
                            ))
            # -- 近防炮：末段硬杀伤 --
            for w in d.template.weapons:
                if not w.ciws or d.ammo.get(w.id, 0) <= 0:
                    continue
                if dist((d.x, d.y), pos) > w.range_max + RANGE_TOLERANCE:
                    continue
                pk = w.pk * (1.0 - weapon.evasion * 0.55) * (1.0 - weapon.stealth * 0.6)
                if weapon.ballistic:
                    pk *= 0.05
                if weapon.hypersonic:
                    pk *= 0.10
                elif weapon.speed >= 9.0:
                    pk *= 0.55
                if pk > 0.01:
                    net.nodes.append(ADNNode(
                        unit=d, weapon=w, pk=min(0.95, pk),
                        channels=99, ammo=d.ammo.get(w.id, 0),
                        role="末段硬杀伤", ciws=True,
                    ))
        # 择优派工：拦截概率高的节点优先
        net.nodes.sort(key=lambda n: (-n.pk, n.channels))
        return net

    # -- 分配 -------------------------------------------------------------
    def assign(self, state: "GameState") -> tuple[ADNNode | None, float]:
        """为来袭弹药分配最合适的节点。

        返回 (节点, 调整后的拦截概率)。没有可用节点时返回 (None, 0)。
        """
        for n in self.nodes:
            if n.ciws:
                if n.ammo > 0:
                    return n, n.pk
                continue
            if n.channels > 0 and n.ammo > 0:
                return n, n.pk
        return None, 0.0

    @property
    def total_channels(self) -> int:
        return sum(n.channels for n in self.nodes if not n.ciws)

    def summary(self) -> list[tuple[str, str, int]]:
        """网内分工摘要：(节点名, 分工, 剩余通道)。"""
        out: list[tuple[str, str, int]] = []
        for n in self.nodes:
            ch = "—" if n.ciws else str(n.channels)
            out.append((n.name, n.role, n.channels))
            _ = ch
        return out

    def role_counts(self) -> dict[str, int]:
        """各分工的节点数量。"""
        out: dict[str, int] = {}
        for n in self.nodes:
            out[n.role] = out.get(n.role, 0) + 1
        return out


def _channels_of(d: Unit) -> int:
    """该防空节点本回合还能开几条火控通道。

    补给枯竭时减半：通道本身是硬件，但再装填跟不上，
    打完一轮就接不上第二波——比直接禁用更接近真实。
    """
    ch = max(0, d.template.interceptors - d.intercepts_used)
    if d.supply_state >= 2:
        ch //= 2
    return ch


def _adn_role(sam: Weapon, own: bool) -> str:
    """按拦截弹与射程判定节点分工。"""
    if own:
        return "自卫拦截"
    if sam.range_max >= 7.0:
        return "区域防空"
    if sam.range_max >= 3.0:
        return "要地防空"
    return "点防御"


def adn_summary_for(state: "GameState", side: Side, pos: tuple[int, int],
                     weapon: Weapon, self_unit: Unit | None = None) -> AirDefenseNetwork:
    """对外接口：构建指定位置处的防空网。"""
    return AirDefenseNetwork.build(state, side, pos, weapon, self_unit)


def resolve_salvo_interception(state: "GameState", pos: tuple[int, int], side: Side,
                               weapon: Weapon, count: int,
                               self_unit: Unit | None = None) -> tuple[int, list[str], int]:
    """逐发拦截结算（由防空网统一派工）。

    每发来袭弹药交由防空网分配给**最合适的节点**；节点消耗一条火控通道
    与一枚拦截弹。同一节点在通道耗尽或弹尽后退出网，
    下一发弹药改派其他节点——这就是「跨单位分配」而非「各打各的」。

    第三个返回值 ``partial`` 是**被破片击伤、但没被彻底摧毁**的发数。
    防空导弹是破片杀伤，近炸引信常常只是把弹体打坏，弹头带着残余能量
    继续扑过来——这发仍算突防，只是毁伤要打折（由调用方结算）。
    弹道/高超音速目标不适用：那种速度下只有直接碰撞才算命中。
    """
    if not weapon.interceptable or weapon.strategic:
        return count, [], 0
    net = AirDefenseNetwork.build(state, side, pos, weapon, self_unit=self_unit)
    if not net.nodes:
        return count, [], 0

    # 硬目标只能被打碎，不存在「打残了还飞过来」这回事
    frag_ok = not (weapon.ballistic or weapon.hypersonic)
    leakers = 0
    partial = 0
    events: list[str] = []
    for _ in range(max(1, count)):
        outcome = "leak"
        # 网内择优派工：可能有多层拦截机会（远层失败后交给近层）
        for node in list(net.nodes):
            if node.ciws:
                if node.ammo <= 0:
                    continue
                node.ammo -= 1
                node.unit.ammo[node.weapon.id] = max(
                    0, node.unit.ammo.get(node.weapon.id, 0) - 1)
            else:
                if node.channels <= 0 or node.ammo <= 0:
                    continue
                node.channels -= 1
                node.ammo -= 1
                node.unit.intercepts_used += 1
                node.unit.ammo[node.weapon.id] = max(
                    0, node.unit.ammo.get(node.weapon.id, 0) - 1)
            if roll(node.pk, state.rng):
                if frag_ok and roll(FRAG_PARTIAL_CHANCE, state.rng):
                    events.append(f"{node.name}·{node.role} 破片命中 1 发（弹体受损未解体）")
                    outcome = "partial"
                else:
                    events.append(f"{node.name}·{node.role} 拦截 1 发（Pk {node.pk * 100:.0f}%）")
                    outcome = "kill"
                break
        if outcome == "kill":
            continue
        leakers += 1
        if outcome == "partial":
            partial += 1
    return leakers, events, partial


def midcourse_interceptors(state: "GameState", pos: tuple[int, int], side: Side):
    """可在中段拦截弹道导弹的防御方（SM-3 / THAAD / 77N6）。

    返回 [(单位, 拦截弹, 单发拦截概率)]；中段拦截很难，基础概率按武器 Pk 的 55% 计。
    """
    out = []
    for d in state.units:
        if not d.alive or d.side is not side or d.is_decoy:
            continue
        for w in d.template.weapons:
            if not w.midcourse or d.ammo.get(w.id, 0) <= 0:
                continue
            if dist((d.x, d.y), pos) > w.range_max + RANGE_TOLERANCE:
                continue
            pk = min(0.85, w.pk * 0.55)
            out.append((d, w, pk))
            break
    return out


def interception_estimate(state: "GameState", attacker: Unit, target: Unit,
                          weapon: Weapon) -> tuple[float, list[str]]:
    """评估防守方对来袭弹药的拦截概率（由防空网统一派工）。"""
    if weapon.strategic:
        # 洲际弹道导弹：中段有 MIDCOURSE_WINDOWS 轮窗口，每轮按弹道中点估算
        mid = ((attacker.x + target.x) // 2, (attacker.y + target.y) // 2)
        survive = 1.0
        names = []
        for d, w, pk in midcourse_interceptors(state, mid, target.side):
            survive *= (1.0 - pk) ** MIDCOURSE_WINDOWS
            names.append(f"{d.name}·中段 {w.name[:8]} ({pk * 100:.0f}%)")
        return 1.0 - survive, names
    if not weapon.interceptable:
        return 0.0, []

    net = AirDefenseNetwork.build(state, target.side, (target.x, target.y), weapon,
                                  self_unit=target)
    survive = 1.0
    names: list[str] = []
    for n in net.nodes[:5]:
        survive *= (1.0 - n.pk)
        ch = "·" + str(n.channels) if not n.ciws else ""
        names.append(f"{n.name}·{n.role} ({n.pk * 100:.0f}%){ch}")
    return 1.0 - survive, names


# --------------------------------------------------------------------------- #
# 对手视角威胁评估
# --------------------------------------------------------------------------- #
@dataclass
class CounterThreat:
    """「对手视角」评估：这一发子弹为什么打不到 / 打到了会怎样。

    玩家在交战面板中需要回答三类问题：
    1. 子弹会不会被拦下（拦截层次与综合拦截概率）；
    2. 目标是否有反击能力（对方能不能打到我们）；
    3. 这一发到底受什么条件限制（情报、通视、弹种、诱饵）。
    """

    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)
    reply_units: list[str] = field(default_factory=list)   # 可反击的敌方单位
    reply_weapons: list[str] = field(default_factory=list)  # 其可用武器
    reply_pk: float = 0.0                                  # 单发最危险的反击概率
    enemy_fire_control: bool = False                       # 对方是否已跟踪到我们
    enemy_detected_level: Detection = Detection.NONE
    blockers: list[str] = field(default_factory=list)      # 阻止命中的具体条件
    notes: list[str] = field(default_factory=list)         # 补充提示
    adn_roles: list[tuple[str, str, int]] = field(default_factory=list)  # 防空网分工
    adn_channels: int = 0                                   # 网内剩余火控通道总数

    @property
    def blocked(self) -> bool:
        """这一发是否存在被完全拦下的风险。"""
        return self.intercept_pk > 0.01


def counter_threat_profile(state: "GameState", attacker: Unit, target: Unit,
                           weapon: Weapon) -> CounterThreat:
    """从防守方视角评估这次射击，解释「为什么打不到」。

    与 `shot_profile` 互补：后者回答「我能不能打中」，本函数回答
    「对方能做什么」。所有判断均为纯计算，不消耗任何资源。
    """
    out = CounterThreat()
    if attacker is None or target is None or weapon is None:
        return out

    # -- 1) 拦截层次：这一发会被谁拦下 --
    if weapon.interceptable and not weapon.strategic:
        pk, names = interception_estimate(state, attacker, target, weapon)
        out.intercept_pk = pk
        out.interceptors = names
        # 防空网分工：让玩家看清对方防区如何组合
        net = AirDefenseNetwork.build(state, target.side, (target.x, target.y), weapon,
                                      self_unit=target)
        out.adn_roles = [(n.name, n.role, n.channels) for n in net.nodes[:5]]
        out.adn_channels = net.total_channels
    elif weapon.strategic:
        out.notes.append("洲际弹道：战术防空网无能为力，仅中段反导可拦")

    # -- 2) 目标能否反击：对方有哪些单位能打到我们 --
    dist_to_me = dist_units(target, attacker)
    best_reply = 0.0
    for e in state.enemies_of(attacker.side):
        if e is target or not e.alive:
            continue
        usable = [w for w in e.usable_weapons()
                  if attacker.domain in w.targets
                  and dist((e.x, e.y), (attacker.x, attacker.y)) <= w.range_max + RANGE_TOLERANCE]
        if not usable:
            continue
        out.reply_units.append(e.name)
        for w in usable[:2]:
            out.reply_weapons.append(f"{e.name}·{w.name}")
        best_reply = max(best_reply, max(w.pk for w in usable))
    out.reply_pk = best_reply

    # 目标自身的反击能力（自卫火力）
    self_reply = [w for w in target.usable_weapons()
                  if attacker.domain in w.targets
                  and dist_to_me <= w.range_max + RANGE_TOLERANCE]
    if self_reply:
        out.reply_units.append(f"{target.name}（自卫）")
        for w in self_reply[:2]:
            out.reply_weapons.append(f"{target.name}·{w.name}")
        out.reply_pk = max(out.reply_pk, max(w.pk for w in self_reply))

    # -- 3) 对方是否已经掌握我们的火控数据（能否反击取决于是否跟踪到） --
    lvl = detection_level(state, target.side, attacker)
    out.enemy_detected_level = lvl
    out.enemy_fire_control = lvl is Detection.TRACKED

    # -- 4) 阻止命中的具体条件 --
    if target.is_decoy:
        out.blockers.append("目标疑为电子诱饵：打上去只会浪费弹药")
    if weapon.guidance.value == "anti_radiation" and not target.is_emitting():
        out.blockers.append("目标未辐射，反辐射导弹无法锁定")
    if not weapon.interceptable:
        out.blockers.append("该弹种不可拦截：但也意味着精度依赖发射时诸元")
    if weapon.min_detection is Detection.CLASSIFIED and \
            state.known_level(attacker.side, target) is Detection.CLASSIFIED:
        out.blockers.append("仅识别级情报即可开火：命中精度已打折")
    if weapon.speed >= 9.0 and out.intercept_pk > 0.2:
        out.blockers.append("高速弹的拦截窗口很短，但仍有被截杀的可能")
    if out.enemy_fire_control:
        out.blockers.append("对方已跟踪到我方：开火会立刻暴露并招致反击")
    elif out.reply_units:
        out.blockers.append("对方有反击能力，但尚未跟踪到我方")
    return out


# --------------------------------------------------------------------------- #
# 固定设施打击与基地勤务
# --------------------------------------------------------------------------- #
FACILITY_SIZE = {
    Facility.AIRBASE: 1.20,
    Facility.PORT: 1.15,
    Facility.DEPOT: 1.05,
    Facility.CITY: 1.20,
    Facility.RADAR_SITE: 0.95,
    Facility.SAM_SITE: 1.00,
    Facility.HQ: 0.90,
}


@dataclass
class FacilityStrike:
    """对机场/港口等固定设施的火力打击解算。"""

    ok: bool = False
    reason: str = ""
    pk: float = 0.0
    damage: float = 0.0
    distance: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    intercept_pk: float = 0.0
    interceptors: list[str] = field(default_factory=list)


def facility_strike_profile(state: "GameState", attacker: Unit, tile: Tile,
                            weapon: Weapon) -> FacilityStrike:
    """评估对设施实施打击的可行性与效果。"""
    p = FacilityStrike(distance=dist((attacker.x, attacker.y), (tile.x, tile.y)))
    if not attacker.alive:
        p.reason = "攻击单位已不存在"
        return p
    if tile.facility is Facility.NONE:
        p.reason = "该地块没有可打击的设施"
        return p
    if tile.owner is attacker.side:
        p.reason = "不能打击己方设施"
        return p
    if not weapon.vs_facility:
        p.reason = f"{weapon.name} 不具备对固定设施的打击能力"
        return p
    if attacker.ammo.get(weapon.id, 0) <= 0:
        p.reason = "弹药耗尽"
        return p
    if tile.facility_hp <= 0:
        p.reason = "该设施已被摧毁"
        return p
    if p.distance < weapon.range_min:
        p.reason = f"低于最小射程 {weapon.range_min:.1f} 格"
        return p
    if p.distance > weapon.range_max + RANGE_TOLERANCE:
        p.reason = f"超出射程 {weapon.range_max:.1f} 格"
        return p
    if p.distance > 0.05:
        if not has_los(state, (attacker.x, attacker.y), (tile.x, tile.y),
                       observer_air=attacker.domain is Domain.AIR):
            p.reason = "无通视条件，无法对设施实施精确打击"
            return p

    f: dict[str, float] = {}
    edge = (p.distance - weapon.range_min) / max(0.1, weapon.range_max - weapon.range_min)
    f["射程包线"] = 1.0 if edge < 0.75 else max(0.55, 1.0 - (edge - 0.75) * 1.4)
    if weapon.strategic:
        f["电子对抗"] = 1.0        # 惯性/天文制导，不受战场干扰影响
    elif weapon.guidance.vulnerable_to_ew:
        jam_t = jam_strength_at(state, (tile.x, tile.y), attacker.side)
        jam_a = jam_strength_at(state, (attacker.x, attacker.y), attacker.side)
        f["电子对抗"] = max(0.25, 1.0 - (jam_t * 0.55 + jam_a * 0.35))
    else:
        f["电子对抗"] = 1.0
    if weapon.guidance.value == "gps_ins":
        f["导航精度"] = gps_integrity(state, attacker.side)
    f["设施尺寸"] = FACILITY_SIZE.get(tile.facility, 1.0)
    f["战损影响"] = 0.75 + 0.25 * attacker.hp_ratio

    pk = weapon.pk
    for v in f.values():
        pk *= v
    p.factors = f
    p.pk = max(0.03, min(0.97, pk))
    p.damage = weapon.damage * FACILITY_SIZE.get(tile.facility, 1.0)
    p.ok = True

    # 设施本身不还手，但周边防空网与近防炮会拦截
    survive = 1.0
    names: list[str] = []
    if tile.owner in (Side.BLUE, Side.RED):
        net = AirDefenseNetwork.build(state, tile.owner, (tile.x, tile.y), weapon)
        for n in net.nodes[:5]:
            survive *= (1 - n.pk)
            names.append(f"{n.name}·{n.role} ({n.pk * 100:.0f}%)")
    p.intercept_pk = 1 - survive
    p.interceptors = names
    return p


def service_tile(state: "GameState", unit: Unit) -> Tile | None:
    """返回可为该单位提供燃油/弹药/维修补给的地块，无则 None。

    * 航空兵：己方完好机场；或同格的己方航母（浮动机场）
    * 水面舰艇/潜艇：己方完好港口
    * 地面部队：己方完好后勤枢纽/机场/指挥部
    """
    t = state.map.at(unit.x, unit.y)
    if t is None:
        return None
    dom = unit.domain
    if dom is Domain.AIR:
        if t.facility is Facility.AIRBASE and t.owner is unit.side and t.operational:
            return t
        for host in state.units_at(unit.x, unit.y):
            if host is unit or host.side is not unit.side:
                continue
            if host.template.unit_class is UnitClass.CARRIER and host.hp_ratio > 0.3:
                return t
        return None
    if dom in (Domain.SEA, Domain.SUB):
        if t.facility is Facility.PORT and t.owner is unit.side and t.operational:
            return t
        return None
    if dom in (Domain.LAND, Domain.INFRA):
        if t.facility in (Facility.DEPOT, Facility.AIRBASE, Facility.HQ) \
                and t.owner is unit.side and t.operational:
            return t
    return None


# --------------------------------------------------------------------------- #
# 补给线与纵深
# --------------------------------------------------------------------------- #
# 各类补给设施的覆盖半径与承载力（同时能补给多少个单位）。
# 承载力设计原则：一支合成团的常规兵力（约 12–16 个单位）应当能被
# 1~2 个枢纽覆盖，这样「补给线被打断」是**战术事件**（枢纽被打击、
# 部队深入敌后超出半径），而不是开局就必然发生的常态。
SUPPLY_PROFILE: dict[Facility, tuple[float, int]] = {
    Facility.DEPOT: (10.0, 12),     # 后勤枢纽：覆盖最广、承载最强
    Facility.HQ: (6.0, 6),          # 指挥部：就近补给
    Facility.AIRBASE: (6.0, 8),     # 机场：主要保障航空兵
    Facility.PORT: (7.0, 8),        # 港口：保障舰艇
    Facility.RADAR_SITE: (3.0, 2),
    Facility.SAM_SITE: (3.0, 2),
}


@dataclass
class SupplySource:
    """一个补给源：后勤设施及其覆盖能力。"""

    tile: Tile
    radius: float
    capacity: int
    side: Side

    @property
    def x(self) -> int:
        return self.tile.x

    @property
    def y(self) -> int:
        return self.tile.y

    @property
    def name(self) -> str:
        return f"{self.tile.facility.label}({self.x},{self.y})"

    @property
    def operational(self) -> bool:
        return self.tile.operational and self.tile.owner is self.side

    def covers(self, unit: Unit) -> bool:
        return dist((self.x, self.y), (unit.x, unit.y)) <= self.radius


def supply_sources(state: "GameState", side: Side) -> list[SupplySource]:
    """列出该阵营所有可用作补给源的地块（按覆盖半径降序）。"""
    out: list[SupplySource] = []
    for t in state.map.iter_tiles():
        if t.facility not in SUPPLY_PROFILE:
            continue
        if t.owner is not side:
            continue
        radius, cap = SUPPLY_PROFILE[t.facility]
        out.append(SupplySource(tile=t, radius=radius, capacity=cap, side=side))
    out.sort(key=lambda s: (-s.radius, s.x, s.y))
    return out


def assign_supply(state: "GameState", side: Side) -> dict[int, SupplySource]:
    """为该阵营所有单位分配补给源。

    分配规则（贪心，按距离由近及远）：
    * 单位必须在补给源的覆盖半径内；
    * 每个补给源有承载力上限，超出的单位得不到补给——这就是「纵深」：
      深入敌后的部队往往因为补给源被打瘫或承载力耗尽而断粮；
    * 分配结果稳定：同一态势下每次结算得到相同结果。

    卫星（天基）不需要补给，不参与分配。
    """
    sources = [s for s in supply_sources(state, side) if s.operational]
    # 用坐标作占用计数键：id() 在对象被回收后可能被复用，导致计数串台
    used: dict[tuple[int, int], int] = {}
    out: dict[int, SupplySource] = {}
    units = [u for u in state.units_of(side)
             if not u.is_decoy and u.domain is not Domain.SPACE]
    # 按「离最近补给源的距离」排序：孤立单位先占位，避免被近处单位挤掉
    def nearest_dist(u: Unit) -> float:
        ds = [dist((s.x, s.y), (u.x, u.y)) for s in sources]
        return min(ds) if ds else 1e9

    units.sort(key=lambda u: (nearest_dist(u), u.uid))
    for u in units:
        best = None
        best_d = 1e9
        for s in sources:
            key = (s.x, s.y)
            if used.get(key, 0) >= s.capacity:
                continue
            d = dist((s.x, s.y), (u.x, u.y))
            if d > s.radius:
                continue
            if d < best_d:
                best, best_d = s, d
        if best is not None:
            out[u.uid] = best
            key = (best.x, best.y)
            used[key] = used.get(key, 0) + 1
    return out


def supply_source_for(state: "GameState", unit: Unit) -> SupplySource | None:
    """查询单个单位当前挂接的补给源。"""
    links = getattr(state, "supply_links", None)
    if not links:
        return None
    src = links.get(unit.uid)
    return src if src is not None else None


def supply_status(state: "GameState", unit: Unit) -> tuple[bool, str]:
    """单位补给状态：(是否有补给, 说明)。"""
    src = supply_source_for(state, unit)
    if src is None:
        return False, "补给中断"
    return True, f"补给线：{src.name}"


def facility_service_ok(tile: Tile | None) -> bool:
    return tile is not None and tile.operational


# --------------------------------------------------------------------------- #
# 机动
# --------------------------------------------------------------------------- #
AIRBASE_CAPACITY = 4


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


def _build_field(state: "GameState", unit: Unit, goal: tuple[int, int],
                 limit: float, ignore_units: bool) -> dict[tuple[int, int], float]:
    """计算指定半径的距离场（带缓存）。"""
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
        if len(cache) > 96:
            cache.clear()
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


# --------------------------------------------------------------------------- #
# 骰子
# --------------------------------------------------------------------------- #
def roll(p: float, rng: "GameRng | None" = None) -> bool:
    """概率判定。

    ``rng`` 为对局随机流时结果可复现；为 ``None`` 时退回全局
    ``random``（仅供尚未接入对局流的旧代码路径使用）。
    """
    if rng is None:
        return random.random() < p
    return rng.random() < p


def jitter(amount: float, spread: float = 0.15,
           rng: "GameRng | None" = None) -> float:
    """伤害浮动。

    ``rng`` 语义同 :func:`roll`。
    """
    if rng is None:
        return amount * random.uniform(1.0 - spread, 1.0 + spread)
    return amount * rng.uniform(1.0 - spread, 1.0 + spread)


__all__ = [
    "dist", "dist_units", "RANGE_TOLERANCE", "CLOSE_TRACK_RANGE", "has_los",
    "DECOY_SIGNATURE", "DECOY_LABELS", "decoy_signature",
    "sam_for_intercept", "resolve_salvo_interception", "midcourse_interceptors",
    "jam_strength_at", "gps_integrity",
    "ADNNode", "AirDefenseNetwork", "adn_summary_for",
    "DetectionResult", "evaluate_detection", "detection_level", "build_detection_map",
    "ShotProfile", "shot_profile", "interception_estimate",
    "CounterThreat", "counter_threat_profile", "salvo_limit",
    "FacilityStrike", "facility_strike_profile", "service_tile", "facility_service_ok",
    "FACILITY_SIZE", "ORBITAL_ENGAGEMENT_DISTANCE", "AIRBASE_CAPACITY",
    "tile_move_cost", "find_path", "reachable_tiles", "flood_reachable", "path_cost",
    "field_to_goal", "movement_profile", "path_cost_to", "is_reachable",
    "MAX_FIELD_LIMIT", "roll", "jitter",
    "sensor_effective_range", "NEIGHBORS_8",
]

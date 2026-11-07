"""探测链：传感器效能、单点判定、全图态势与增量缓存。

三件事挤在一起，是因为它们共用同一套失效规则：空间索引按观测者建立，
判定结果按「双方状态指纹 + 环境指纹」缓存。分开会让缓存键的定义
散落到两处，迟早不同步。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import Detection, Domain, SensorKind
from .env import environment_for
# 这几个「环境依赖」走模块属性访问（``ew.jam_strength_at`` 而不是
# ``from .ew import jam_strength_at``）：自检脚本需要把它们临时换成
# 确定性桩值，而打补丁只能改**定义处模块**的属性。函数内直接引用
# 的名字是另一份绑定，补丁打不到。
from . import ew, geo
from .geo import CLOSE_TRACK_RANGE, _cache_of, dist_units
from .model import Unit

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState
    from .model import UnitTemplate

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
        sig = ew.decoy_signature(target.decoy_kind, kind)
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
        r *= 1.0 - ew.jam_strength_at(state, (observer.x, observer.y), observer.side,
                                   "radar") * 0.85

    # 4.5) 战场环境：时段与天候改变光电/红外/雷达的探测效能
    env = environment_for(state)
    r *= env.factor(kind)

    # 5) 通视
    los = geo.has_los(
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

def _template_sig(t: "UnitTemplate") -> tuple:
    """模板中会影响探测判定的部分。

    模板在一局内本应不变，但模组会做两件事：**整套替换**（``u.template = x``）
    与**就地改**（换雷达、调隐身）。前者只有 ``id()`` 能抓到，后者只有标量能
    抓到，所以两个都取。传感器的几何参数直接决定探测距离，逐项列出。

    （v3.3.1 前指纹不含模板，于是「模组中途换了雷达」会沿用旧结论。）
    """
    return (id(t), t.stealth, t.unit_class, t.jam_strength, t.jam_bands,
            tuple((s.kind, s.range_max, s.strength, s.counter_stealth)
                  for s in t.sensors))


def _unit_fingerprint(u: Unit) -> tuple:
    """单位状态指纹：覆盖所有影响「能否被探测到」的字段。"""
    return (u.x, u.y, u.radar_on, u.jam_on, u.suppressed, u.mission,
            u.altitude, u.alive, u.is_decoy, u.decoy_kind, u.entrench,
            u.revealed_until, _template_sig(u.template))


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

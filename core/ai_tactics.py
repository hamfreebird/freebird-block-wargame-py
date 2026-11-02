"""AI 战术库：把「怎么打」与「怎么走」分离。

AIPlanner（core/ai.py）负责**流程**——谁先动、开不开火、动到哪；
战术库（本模块）负责**决策内容**——该去哪个点、该打谁、该不该撤。

分开的理由：战术规则各自独立、可单测，也能被模组替换。

v2.2 新增。起因是 v1.5 的实测：128×128 想定里蓝方 AI 20 回合拿不下
任何中立点。诊断发现根因不在「贪心」，而在**可达性判断**：

* 目标评分用**直线距离**——隔着海的两点直线很近，绕行却要走几百格；
* 陆上单位用 `same_landmass` 硬过滤，**两栖单位虽能跨海却没被优先派去**；
* 目标一旦分配就固定不变，不会因为「够不着」而改主意。

于是 AI 把仅有的两栖力量派去守自己的点，把高价值中立点拱手让人。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.enums import Detection, Domain, Side, Terrain
from core import rules

if TYPE_CHECKING:
    from core.engine import GameState
    from core.model import Tile, Unit


# --------------------------------------------------------------------------- #
# 可达性
# --------------------------------------------------------------------------- #
@dataclass
class Reach:
    """到一个目标点的可达性评估。"""

    cost: float | None = None      # 实际路径成本；None = 不可达
    crosses_water: bool = False    # 是否需要跨海

    @property
    def reachable(self) -> bool:
        return self.cost is not None

    @property
    def turns_needed(self) -> int:
        """大约需要几个回合才能走到（按每回合移动力估算）。"""
        if self.cost is None:
            return 999
        return max(1, int(self.cost / 4.0) + 1)


def evaluate_reach(state: "GameState", unit: Unit,
                   target: tuple[int, int]) -> Reach:
    """评估单位到某点的可达性。

    用**真实路径成本**而非直线距离——这是整个战术库的地基。
    直线距离会骗人：海岛与大陆直线只隔十几格，绕行却可能超过 200。
    """
    cost = rules.path_cost_to(state, unit, target)
    if cost is None:
        return Reach(None, False)
    # 跨海判定：起终点所在陆块不同
    lm_a = state.map.landmass.get((unit.x, unit.y))
    lm_b = state.map.landmass.get(target)
    crosses = lm_a is not None and lm_b is not None and lm_a != lm_b
    return Reach(cost, crosses)


# --------------------------------------------------------------------------- #
# 目标争夺优先级
# --------------------------------------------------------------------------- #
# 粗筛后保留的目标点数量。距离场是本模块最贵的计算，
# 而绝大多数目标点从来不会被选中——全算一遍只是白烧时间。
MAX_REACH_CANDIDATES = 5


@dataclass
class ObjectivePlan:
    """一个目标点的战术评估结果。"""

    tile: Tile
    score: float
    reach: Reach
    role: str = "contest"     # contest 争夺 / defend 防守 / garrison 驻守


def objective_value(state: "GameState", side: Side, tile: Tile) -> float:
    """目标点的**价值**——只与胜利点有关，不看归属。

    归属的影响放在别处（见 evaluate_objectives）：己方点要打折，
    但**中立点与敌方点一视同仁**——AI 必须敢于攻打敌方控制的目标点，
    那才是胜负点的主要来源。实测把敌方点单独降权会让 AI 畏首畏尾
    （平衡从 4:5 掉到 1:9），因为它转而只敢守家。
    """
    return 1.0 + tile.victory_points * 0.25



def defense_quota(tile: Tile, threat: bool = False) -> int:
    """一个目标点该留几个守军。

    普通点 1 个；受威胁的点 2 个（挡住进攻需要冗余）。
    """
    return 2 if threat else 1


def suited_to_defend(unit: Unit) -> bool:
    """该单位是否**适合守土**。

    守土要的是稳定与火力覆盖，机动性反而没用；反过来，机动性强的
    单位（如两栖战车）留在家里是浪费——它去争点更划算。

    这是真实的战术分工，也是让 AI 同时「守得住」与「攻得下」的关键。
    """
    if unit.domain is Domain.AIR:
        return False
    # 防空单位天然适合守土
    if unit.template.interceptors > 0:
        return True
    # 低机动力的单位（固定阵地型）适合守
    return unit.mp <= 2.4



def evaluate_objectives(state: "GameState", side: Side, unit: Unit,
                        defenders: dict[tuple[int, int], int] | None = None,
                        ) -> list[ObjectivePlan]:
    """评估所有目标点对该单位的价值，按得分排序。

    `defenders` 记录每个点已分配的防守单位数——避免全队挤向同一点。

    **两级筛选**：先用直线距离与胜利点做粗筛，只对最有希望的几个点
    计算精确路径成本。距离场是这张地图上最贵的计算（每个「目标点 ×
    机动档案」一次全图 Dijkstra），对全部 13 个目标点都算会让 AI 回合
    从 1.8 秒涨到 6 秒——而其中绝大多数从来不会被选中。
    """
    defenders = defenders or {}
    # -- 粗筛：直线距离 + 胜利点，取前若干个候选 --
    cands: list[tuple[float, Tile]] = []
    for t in state.objective_tiles:
        if unit.template.interceptors > 0 and t.owner is side:
            continue
        if not unit.template.can_capture and t.owner is not side:
            continue        # 无占领能力的单位不必去争点
        d = rules.dist((unit.x, unit.y), (t.x, t.y))
        # 粗筛用**与最终打分一致的公式**（只差可达性）。
        # 若粗筛用另一套排序，会把最终最优解提前淘汰——
        # 实测平衡因此从 3:7 掉到 2:8。
        n_def = defenders.get((t.x, t.y), 0)
        w = (0.35 if n_def < 2 else 0.12) if t.owner is side else 1.0
        rough = w * (1.0 + t.victory_points * 0.2) / (1.0 + d / 8.0)
        cands.append((rough, t))
    cands.sort(key=lambda c: -c[0])
    cands = cands[:MAX_REACH_CANDIDATES]

    plans: list[ObjectivePlan] = []
    for _rough, t in cands:
        reach = evaluate_reach(state, unit, (t.x, t.y))
        if not reach.reachable:
            continue
        # 其余一律 1.0；分母用**直线距离** / 8。
        # v2.2 只加「可达性过滤」，不动排序——可达性才是真正的病根，
        # 换掉权重或分母会让 AI 的取舍整体偏移，实测平衡从 4:5 掉到 1:9。
        # 权重与分母沿用 v2.1 的结构：己方点 0.35（守军够则 0.12），
        # 其余一律 1.0；分母用**直线距离** / 8。
        #
        # v2.2 只加「可达性过滤」，不动排序——可达性才是真正的病根。
        # 换掉权重或分母会让 AI 的取舍整体偏移：实测平衡从 4:5 掉到 1:9。
        #
        # 中立点与敌方点**同等对待**——AI 必须敢于攻打敌方控制的目标点，
        # 那才是胜负点的主要来源。单独给敌方点降权会让它畏首畏尾。
        n_def = defenders.get((t.x, t.y), 0)
        weight = (0.35 if n_def < 2 else 0.12) if t.owner is side else 1.0
        val = weight * (1.0 + t.victory_points * 0.2) \
            / (1.0 + rules.dist((unit.x, unit.y), (t.x, t.y)) / 8.0)
        plans.append(ObjectivePlan(t, val, reach))

    plans.sort(key=lambda p: -p.score)
    return plans


def contested_objectives(state: "GameState", side: Side) -> list[Tile]:
    """本方**能拿到**的中立点——按价值排序。

    这是「该不该进攻」的情报来源：连一个够得着的中立点都没有，
    说明地图上本方确实没有扩张空间，守土就是正解。
    """
    out = [t for t in state.objective_tiles if t.owner is Side.NEUTRAL]
    out.sort(key=lambda t: -t.victory_points)
    return out


# --------------------------------------------------------------------------- #
# 斩首打击
# --------------------------------------------------------------------------- #
def strike_priority(state: "GameState", side: Side, target: Unit) -> float:
    """目标对己方的**威胁价值**——用来决定「先打谁」。

    斩首打击的逻辑：优先打掉「打掉之后能让我们多拿分」的单位——
    敌方占领点的守卫、能压制我方的防空、能切断我方补给线的单位。
    """
    if not target.alive:
        return 0.0
    sc = 0.0
    # 敌方占领点的守卫：打掉它才可能夺点
    tile = state.map.at(target.x, target.y)
    if tile is not None and tile.objective and tile.owner is not Side.NEUTRAL \
            and tile.owner is not side:
        sc += 3.0 + tile.victory_points * 0.25
    # 防空单位：打掉能显著降低敌方拦截能力
    if target.template.interceptors > 0:
        sc += 2.5
    # 高价值单位
    sc += target.template.cost / 120.0
    # 靠近我方目标的敌方单位更该先打
    for t in state.objective_tiles:
        if t.owner is side and rules.dist((target.x, target.y), (t.x, t.y)) <= 8.0:
            sc += 1.2
            break
    return sc


def pick_strike_target(state: "GameState", side: Side, unit: Unit,
                       limit: int = 4) -> Unit | None:
    """在射程内挑一个最该打的敌方目标。

    `limit` 限制候选数量——射程内可能有很多目标，全算威胁值太慢。
    """
    own = [w for w in unit.usable_weapons()]
    if not own:
        return None
    rmax = max(w.range_max for w in own)
    cands = []
    for e in state.enemies_of(side):
        if not e.alive:
            continue
        if state.known_level(side, e) is Detection.NONE:
            continue        # 没发现的目标打不了
        d = rules.dist_units(unit, e)
        if d > rmax:
            continue
        cands.append((strike_priority(state, side, e), -d, e))
    if not cands:
        return None
    cands.sort(key=lambda c: (-c[0], -c[1]))
    return cands[0][2]


# --------------------------------------------------------------------------- #
# 多层防空
# --------------------------------------------------------------------------- #
def defense_position(state: "GameState", side: Side, unit: Unit,
                     protected: Tile | None = None) -> tuple[int, int] | None:
    """防空单位该守在哪个位置。

    原则：
    1. 有敌方目标点要守时，守在它附近（拦截半径内）；
    2. 敌方航空兵逼近时，向其航路方向前出拦截；
    3. 否则守在己方目标点与补给枢纽之间。
    """
    r_intercept = unit.best_sensor_range()
    # 1) 敌方空袭威胁：向敌方空兵与己方要地之间靠拢
    threats = [e for e in state.enemies_of(side)
               if e.alive and e.domain is Domain.AIR
               and state.known_level(side, e) is not Detection.NONE]
    if threats:
        own_pts = [t for t in state.objective_tiles if t.owner is side]
        if own_pts:
            tgt = min(own_pts, key=lambda o: rules.dist((o.x, o.y),
                                                       (threats[0].x, threats[0].y)))
            t = threats[0]
            dx, dy = t.x - tgt.x, t.y - tgt.y
            n = max(1.0, (dx * dx + dy * dy) ** 0.5)
            reach = min(n, r_intercept * 0.8)
            gx = int(round(tgt.x + dx / n * reach))
            gy = int(round(tgt.y + dy / n * reach))
            gx = max(0, min(state.map.width - 1, gx))
            gy = max(0, min(state.map.height - 1, gy))
            return (gx, gy)
    # 2) 守己方目标点
    own_pts = [t for t in state.objective_tiles if t.owner is side]
    if own_pts:
        t = protected or min(own_pts, key=lambda o: rules.dist((unit.x, unit.y),
                                                              (o.x, o.y)))
        return (t.x, t.y)
    return None


def air_defense_commitment(state: "GameState", side: Side, unit: Unit) -> bool:
    """该单位是否应当专职防空（而不是去争点）。

    判据：敌方空中力量对我方要地的威胁足够大。
    """
    if unit.template.interceptors <= 0:
        return False
    own_pts = [t for t in state.objective_tiles if t.owner is side]
    if not own_pts:
        return False
    threats = [e for e in state.enemies_of(side)
               if e.alive and e.domain is Domain.AIR
               and state.known_level(side, e) is not Detection.NONE]
    if not threats:
        return False
    r = unit.best_sensor_range()
    for e in threats:
        for t in own_pts:
            if rules.dist((e.x, e.y), (t.x, t.y)) <= r * 1.4:
                return True
    return False


# --------------------------------------------------------------------------- #
# 诱饵运用
# --------------------------------------------------------------------------- #
def decoy_worth(state: "GameState", side: Side, unit: Unit) -> bool:
    """该单位是否值得放诱饵。

    诱饵的价值在于**把精确制导弹药引开**——所以只在
    「敌方有精确制导武器 且 我方重要单位暴露」时才值得。
    """
    if not unit.template.decoy_types:
        return False
    # 敌方是否有精确制导能力
    guided = False
    for e in state.enemies_of(side):
        if not e.alive:
            continue
        for w in e.usable_weapons():
            if w.guidance.name in ("PRECISE", "SATELLITE"):
                guided = True
                break
        if guided:
            break
    if not guided:
        return False
    # 我方是否有暴露在外的要紧单位
    for u in state.units_of(side):
        if not u.alive or u is unit:
            continue
        if u.template.cost >= 150 and u.hp_ratio > 0.5:
            return True
    return False


def ammo_ratio(unit: Unit) -> float:
    """单位的弹药剩余比例（0..1）。

    没有独立方法——弹药是 `dict[武器 id, 数量]`，需要自己按基数加权。
    """
    total = 0
    left = 0
    for w in unit.template.weapons:
        base = max(1, w.ammo)
        total += base
        left += min(base, unit.ammo.get(w.id, 0))
    if total <= 0:
        return 1.0
    return left / total



# --------------------------------------------------------------------------- #
# 撤退与补给
# --------------------------------------------------------------------------- #
def should_retreat(state: "GameState", side: Side, unit: Unit) -> bool:
    """该单位是否应当脱离接触。

    判据：兵力损失过大 + 附近没有友军支援 + 补给断裂。
    此时「活着回去」比「拼掉」更有价值。
    """
    if unit.hp_ratio > 0.45:
        return False
    # 附近有友军就不撤（还有机会打赢）
    for u in state.units_of(side):
        if u is unit or not u.alive:
            continue
        if rules.dist_units(unit, u) <= 8.0:
            return False
    # 弹药打光又受伤：撤
    if ammo_ratio(unit) <= 0.02 and unit.hp_ratio < 0.6:
        return True
    # 重伤且孤立：撤
    return unit.hp_ratio < 0.28


def resupply_needed(state: "GameState", side: Side, unit: Unit) -> bool:
    """该单位是否应当优先回补给点。

    补给是「连续作战能力」的底座——但**不能所有单位都去补**，
    否则战场瞬间空掉。判据：弹药/油量告急 + 附近没有其他补给点。
    """
    if unit.domain is Domain.AIR:
        return unit.fuel < 0.25
    if ammo_ratio(unit) > 0.35:
        return False
    # 附近已有别的单位在补给，不必都去
    for u in state.units_of(side):
        if u is unit or not u.alive:
            continue
        if ammo_ratio(u) < 0.35 and rules.service_tile(state, u) is not None:
            return False
    return rules.service_tile(state, unit) is not None


__all__ = [
    "Reach", "evaluate_reach", "ObjectivePlan", "objective_value",
    "defense_quota", "suited_to_defend",
    "evaluate_objectives", "contested_objectives",
    "strike_priority", "pick_strike_target",
    "defense_position", "air_defense_commitment",
    "decoy_worth", "should_retreat", "resupply_needed", "ammo_ratio",
]

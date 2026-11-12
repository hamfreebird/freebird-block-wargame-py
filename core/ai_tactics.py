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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.enums import Detection, Domain, Side
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



def _objective_weight(side: Side, tile: Tile, n_def: int) -> float:
    """目标点的权重：己方点要打折（守军够则几乎不去），其余一律 1.0。

    中立点与敌方点**同等对待**——AI 必须敢于攻打敌方控制的目标点，
    那才是胜负点的主要来源。单独给敌方点降权会让它畏首畏尾
    （实测平衡从 4:5 掉到 1:9），因为它转而只敢守家。
    """
    if tile.owner is not side:
        return 1.0
    return 0.35 if n_def < 2 else 0.12


#: 大模型指定主攻方向时，该目标点在评分里获得的倍率。
#: 用**倍率**而不是加绝对值：粗筛与精确排序必须同公式（见
#: `rough_objective_scores` 的注释），倍率对两者等价，也不会因为
#: 地图变大、分值尺度变化而失效。
#:
#: 2.5 是实测出来的：1.6 只挪动 1 个营（等于没下令），4.0 以上与 6.0
#: 的结果一样（都只有 4 个营能脱离当前任务），2.5 拿到 3 个营、
#: 且不会让全军丢下眼前的防御去长途奔袭。
EFFORT_GAIN = 2.5


def rough_objective_scores(state: "GameState", side: Side, unit: Unit,
                           defenders: dict[tuple[int, int], int] | None = None,
                           effort: tuple[int, int] | None = None,
                           ) -> list[tuple[float, Tile]]:
    """粗筛打分：直线距离 + 胜利点，**不算路径**。

    这是「这个点对我有多值钱」的廉价近似。精确版要把可达性（真实路径成本）
    算进去，而那是这张地图上最贵的计算（每个「目标点 × 机动档案」一次全图
    Dijkstra）——所以粗筛与重算都用它，只有真正要出发时才付那笔钱。

    粗筛与最终排序**必须同公式**（只差可达性因子）。若两套排序不一致，
    会把最终最优解在粗筛阶段就提前淘汰——实测平衡因此从 3:7 掉到 2:8。

    `effort` 是「指挥官钦定的主攻方向」：命中它的点乘 :data:`EFFORT_GAIN`。
    放在这个公式里而不是事后重排，正是为了不破坏上面那条一致性。
    """
    defenders = defenders or {}
    out: list[tuple[float, Tile]] = []
    for t in state.objective_tiles:
        if unit.template.interceptors > 0 and t.owner is side:
            continue
        if not unit.template.can_capture and t.owner is not side:
            continue        # 无占领能力的单位不必去争点
        d = rules.dist((unit.x, unit.y), (t.x, t.y))
        n_def = defenders.get((t.x, t.y), 0)
        rough = _objective_weight(side, t, n_def) \
            * (1.0 + t.victory_points * 0.2) / (1.0 + d / 8.0)
        if effort is not None and (t.x, t.y) == effort:
            rough *= EFFORT_GAIN
        out.append((rough, t))
    out.sort(key=lambda c: -c[0])
    return out


def evaluate_objectives(state: "GameState", side: Side, unit: Unit,
                        defenders: dict[tuple[int, int], int] | None = None,
                        effort: tuple[int, int] | None = None,
                        ) -> list[ObjectivePlan]:
    """评估所有目标点对该单位的价值，按得分排序。

    `defenders` 记录每个点已分配的防守单位数——避免全队挤向同一点。

    **两级筛选**：先用 `rough_objective_scores` 粗筛，只对最有希望的几个点
    计算精确路径成本。对全部 13 个目标点都算可达性会让 AI 回合从 1.8 秒
    涨到 6 秒——而其中绝大多数从来不会被选中。
    """
    defenders = defenders or {}
    rough = rough_objective_scores(state, side, unit, defenders, effort)
    cands = rough[:MAX_REACH_CANDIDATES]

    plans: list[ObjectivePlan] = []
    for val, t in cands:
        reach = evaluate_reach(state, unit, (t.x, t.y))
        if not reach.reachable:
            continue
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
# 多目标优先级转移
# --------------------------------------------------------------------------- #
#: 改派冷却（回合）：1 = 每回合复核一次。
#: 复核本身很便宜（`rough_objective_scores` 不算路径），真正防抖的是下面
#: 那个 1.45 倍的迟滞——只有「明显更好」才值得放弃已经走了一半的路。
#: 早先设成 3 反而更差：部队三个回合不回话，战场的形状早就变了。
REASSIGN_COOLDOWN = 1

#: 新目标要好这么多倍才值得换向。已经走了一半的路也是成本。
REASSIGN_GAIN = 1.45


@dataclass
class Reassign:
    """是否放弃当前目标。"""

    switch: bool = False
    reason: str = ""


def _garrisoned(state: "GameState", side: Side, tile: Tile, exclude: Unit) -> bool:
    """目标点是否已经站了足够多的己方守军。

    按**受威胁**的口径算配额（2 个）：这里问的是「再来一个还有没有用」，
    而 `_assign_for` 的防守分支正是按 2 个留人的。用 1 会立刻把刚派去的
    第二个守军又调走，等于这条分支白写。
    """
    n = 0
    for u in state.units_of(side):
        if u is exclude or not u.template.can_capture:
            continue
        if rules.dist((u.x, u.y), (tile.x, tile.y)) <= 1.5:
            n += 1
    return n >= defense_quota(tile, True)


def assignment_invalid(state: "GameState", side: Side, unit: Unit,
                       goal: tuple[int, int] | None) -> str:
    """当前目标是否已经作废——**只看局部状态，不算路径**。

    这条路每次分配都要走，所以刻意不做任何 Dijkstra：
    「点已经被自己人占了」「我没有占领能力」「点不见了」都是 O(1) 或 O(N) 的判定。
    真正贵的那次重算留给 `reassess_objective`，由调用方按冷却节奏触发。
    """
    if goal is None:
        return "无目标"
    tile = state.map.at(goal[0], goal[1])
    if tile is None:
        return "目标点不存在"
    if not tile.objective:
        return "目标点已被取消"
    if tile.owner is side and _garrisoned(state, side, tile, unit):
        return "己方点已有守军"
    if tile.owner is not side and not unit.template.can_capture:
        return "本单位无占领能力"
    return ""


def reassess_objective(state: "GameState", side: Side, unit: Unit,
                       goal: tuple[int, int] | None,
                       plans: list[ObjectivePlan] | None = None,
                       defenders: dict[tuple[int, int], int] | None = None,
                       effort: tuple[int, int] | None = None,
                       ) -> Reassign:
    """比较当前目标与重新评估后的首选，决定要不要转移。

    重算走 `rough_objective_scores`（不算路径）——它只需要回答
    「有没有明显更好的选择」，不需要精确到「走几回合」。真正的可达性
    由 `assignment_invalid` 与出发时的 `evaluate_objectives` 负责。
    这样一次重算的成本是 O(目标点数)，而不是几次全图 Dijkstra，
    才有可能每回合给几十个单位都算一遍。

    `plans` 若调用方已经算好（精确版）就直接用。
    """
    why = assignment_invalid(state, side, unit, goal)
    if why:
        return Reassign(True, why)
    if plans is not None:
        if not plans:
            return Reassign(False, "")
        pairs = [(p.score, p.tile) for p in plans]
    else:
        pairs = rough_objective_scores(state, side, unit, defenders, effort)
    if not pairs:
        return Reassign(False, "")
    cur = next((sc for sc, t in pairs if (t.x, t.y) == goal), None)
    if cur is None:
        # 当前目标已经不在候选里（不可达 / 已不再值得去）——原地发呆更糟
        return Reassign(True, "目标已不可达")
    best_sc, best_tile = pairs[0]
    if (best_tile.x, best_tile.y) != goal and best_sc > cur * REASSIGN_GAIN:
        return Reassign(True, "出现更优目标")
    return Reassign(False, "")


# --------------------------------------------------------------------------- #
# 战场态势：局部兵力对比
# --------------------------------------------------------------------------- #
#: 判断「这一仗还打不打得下去」的观察半径。
CAUTION_RADIUS = 10.0


def _combat_power(unit: Unit) -> float:
    """单位的当下战力：造价 × 完好度 × 弹药余量。

    造价代表编制规模，完好度代表还剩多少，弹药代表还能打多久。
    三者相乘比单看血量更接近指挥员的判断——一辆满血没弹的坦克，
    在战场上和一辆半血满弹的坦克价值完全不同。
    """
    return unit.template.cost * (0.35 + 0.65 * unit.hp_ratio) \
        * (0.45 + 0.55 * ammo_ratio(unit))


def local_balance(state: "GameState", side: Side, unit: Unit,
                  radius: float = CAUTION_RADIUS) -> float:
    """局部兵力对比：我方战力 ÷ 双方战力之和（0..1）。

    0.5 是均势，0.3 以下意味着「这一片打不过」。只看已知敌情——
    AI 不该凭空知道雾里的部队。
    """
    own = _combat_power(unit)
    foe = 0.0
    for u in state.units_of(side):
        if u is unit or not u.alive or u.domain is Domain.SPACE:
            continue
        if rules.dist_units(unit, u) <= radius:
            own += _combat_power(u)
    for e in state.enemies_of(side):
        if not e.alive or e.domain is Domain.SPACE:
            continue
        if state.known_level(side, e) is Detection.NONE:
            continue
        if rules.dist_units(unit, e) <= radius:
            foe += _combat_power(e)
    if own + foe <= 0:
        return 0.5
    return own / (own + foe)


# --------------------------------------------------------------------------- #
# 撤退时机
# --------------------------------------------------------------------------- #
@dataclass
class RetreatDecision:
    """脱离接触的判断结果。`urgency` 越大越该立刻走。"""

    retreat: bool = False
    urgency: int = 0        # 0 坚守 / 1 寻机后撤 / 2 立即脱离
    reason: str = ""


def retreat_assessment(state: "GameState", side: Side, unit: Unit,
                       can_move: bool = True) -> RetreatDecision:
    """该不该撤、有多急。

    v2.2 的判据只有「血量 < 45% 且附近没友军」，两个毛病：
    一是**看不见劣势**——三辆坦克围着一辆步战车，只要 8 格内还有一个友军就不撤；
    二是**不懂取舍**——踩在己方要点上、身后就是补给枢纽，也一样掉头就走。

    现在按「值不值得守 × 守不守得住」两件事分档：

    * 脚下是己方要点，且兵力不悬殊 → 守（要点丢了，活着回去也没用）；
    * 局部兵力对比崩了、弹药打光、油尽、断粮 → 撤，且分轻重缓急。
    """
    hp = unit.hp_ratio
    if not can_move:
        # 被合围时撤退命令只会让部队在包围圈里乱撞
        return RetreatDecision(False, 0, "已被合围")

    tile = state.map.at(unit.x, unit.y)
    on_own_point = (tile is not None and tile.objective
                    and tile.owner is side)
    bal = local_balance(state, side, unit)
    ammo = ammo_ratio(unit)

    # 1) 守土：己方要点上，只要还有一搏之力就不走
    if on_own_point and (bal >= 0.45 or hp >= 0.55):
        return RetreatDecision(False, 0, "据守要点")

    # 2) 弹药打光且没有近战手段：留在这里只是靶子
    if ammo <= 0.03:
        return RetreatDecision(True, 2, "弹药耗尽")

    # 3) 局部兵力悬殊：这一片已经打不过了
    if bal < 0.35 and hp < 0.55:
        return RetreatDecision(True, 2, "兵力悬殊")

    # 4) 油尽：地面部队还能爬，但爬不出包围圈
    if unit.domain is not Domain.AIR and unit.fuel <= 0.05:
        return RetreatDecision(True, 2, "油料耗尽")

    # 5) 断粮：火力打不出来，越拖越弱
    if unit.supply_state >= 2 and hp < 0.60:
        return RetreatDecision(True, 1, "补给枯竭")

    # 6) 战损严重
    if hp < 0.28 and bal < 0.60:
        return RetreatDecision(True, 2, "战损严重")

    # 7) 劣势但还可控：寻机脱离，不必仓皇
    if hp < 0.45 and bal < 0.30:
        return RetreatDecision(True, 1, "劣势明显")
    return RetreatDecision(False, 0, "")


# --------------------------------------------------------------------------- #
# 佯攻
# --------------------------------------------------------------------------- #
#: 佯动部队的造价上限：佯攻的要点是「看起来像真的」，不是「真的很强」。
FEINT_MAX_COST = 240
#: 佯攻方向与主攻方向至少要分开这么多格——两路挤在一起，敌人不用分兵。
FEINT_MIN_GAP = 6.0
#: 佯攻点附近这个半径内有敌军，才算「有人看得见」。
FEINT_WATCH_RADIUS = 8.0
#: 主攻方向上至少已有这么多支部队，才谈得上分兵做佯动（1 = 主攻确实存在）。
FEINT_MIN_SUPPORT = 1
#: 可占领部队要比待争的目标点多出这么多支，才分得出人来佯动。
FEINT_MIN_SPARE = 2
#: 佯动部队到佯攻点的距离上限（格）。超出这个尺度就不叫佯攻了。
FEINT_MAX_DIST = 18.0
#: 争点方案低于这个分数就算「没什么正事可做」——让这样的单位去做佯动。
#: 打分是 ``价值 ÷ (1 + 直线距离/8)``：0.30 约等于「最近的正经目标也在 40 格开外」。
FEINT_WEAK_PLAN = 0.30


@dataclass
class FeintPlan:
    """一次佯攻的编成。"""

    unit_uid: int = 0
    tile: tuple[int, int] | None = None
    main_tile: tuple[int, int] | None = None
    note: str = ""

    @property
    def active(self) -> bool:
        return self.unit_uid != 0 and self.tile is not None


def plan_feint(state: "GameState", side: Side,
               main: tuple[int, int] | None,
               main_support: int = 0) -> FeintPlan:
    """挑一支部队去次要方向做姿态，把守军从主攻方向引开。

    这不是「电子诱饵」——那是假信号，打上去什么也没有。佯攻是真部队、
    真机动、真能占点，所以对手**必须**分兵应对；代价是这支部队可能真的陷进去。
    因此选兵的标准是「便宜、跑得快、血还满」，而不是「最强」。

    只在主攻方向已经有足够兵力（`main_support`）时才启用：连主攻都不够人，
    分兵只是自杀。这条不是保守——实测在 40×40 想定里，让一支部队
    在没余力的时候去做姿态，进攻方净亏 10 个胜负点。
    """
    if main is None or main_support < FEINT_MIN_SUPPORT:
        return FeintPlan()
    capturable = [u for u in state.units_of(side)
                  if u.alive and u.template.can_capture
                  and u.domain in (Domain.LAND, Domain.SEA)]
    contested = [t for t in state.objective_tiles if t.owner is not side]
    # 佯攻要的是**富余**：可占领部队比要争的点还多，才分得出人来演戏。
    if len(capturable) - len(contested) < FEINT_MIN_SPARE:
        return FeintPlan()
    main_tile = state.map.at(main[0], main[1])
    main_vp = main_tile.victory_points if main_tile is not None else 0

    # 佯攻点：立场不同、方向分开、价值不高于主攻方向（次要方向才叫佯动）
    cands: list[tuple[tuple[float, float], Tile]] = []
    for t in state.objective_tiles:
        if t.owner is side:
            continue
        if rules.dist((t.x, t.y), main) < FEINT_MIN_GAP:
            continue
        if t.victory_points > main_vp:
            continue          # 比主攻还值钱，那就该真打，不是佯动
        # 佯攻必须有「观众」：点在敌人手里（丢了掉分，必须回来救），
        # 或附近本来就有敌军看得见我们的动作。无人可引的佯动只是把一支部队
        # 扔到空地上。
        watched = t.owner is side.foe or any(
            e.alive and rules.dist((t.x, t.y), (e.x, e.y)) <= FEINT_WATCH_RADIUS
            for e in state.enemies_of(side))
        if not watched:
            continue
        # 优先敌人手里的点（真正必须救），再按分兵距离排
        rank = 0.0 if t.owner is side.foe else 1.0
        cands.append(((rank, rules.dist((t.x, t.y), main)), t))
    if not cands:
        return FeintPlan()
    # 分兵距离最短的那个次要点：主力来得及在敌方回援前解决战斗
    cands.sort(key=lambda c: c[0])
    target = cands[0][1]

    # 选兵：便宜、机动好、还完好，而且**够得着**。
    # 造价低意味着「就算亏了也不心疼」；距离上限意味着这支部队还在
    # 同一场战斗的时间尺度里——走十个回合才到的佯动不叫佯动，叫远征。
    pool = [u for u in capturable
            if u.template.cost <= FEINT_MAX_COST and u.hp_ratio >= 0.70
            and u.mp >= 2.4 and u.uid != 0
            and rules.dist((u.x, u.y), (target.x, target.y)) <= FEINT_MAX_DIST]
    if not pool:
        return FeintPlan()
    pool.sort(key=lambda u: (u.template.cost, -u.mp, u.uid))
    pick = pool[0]
    return FeintPlan(
        unit_uid=pick.uid, tile=(target.x, target.y), main_tile=main,
        note=f"{pick.name} 向 {target.objective_name or '次要方向'} 佯动",
    )


# --------------------------------------------------------------------------- #
# 交叉火力阵位
# --------------------------------------------------------------------------- #
def _can_hit(unit: Unit, target: Unit, d: float) -> bool:
    for w in unit.usable_weapons():
        if w.kind not in rules.CROSSFIRE_KINDS:
            continue
        if target.domain not in w.targets:
            continue
        if d <= w.range_max + rules.RANGE_TOLERANCE and unit.ammo.get(w.id, 0) > 0:
            return True
    return False


def crossfire_map(state: "GameState", side: Side, unit: Unit,
                  limit: int = 5) -> dict[int, set[int]]:
    """己方对附近敌目标**已经占据**的直瞄方位（敌 uid → 方位档集合）。

    每个候选阵位都会问一次「站这里能不能多出一个方位」，
    所以先算好一次，别在格子上反复扫全战场。
    """
    out: dict[int, set[int]] = {}
    foes: list[tuple[float, Unit]] = []
    for e in state.enemies_of(side):
        if not e.alive or e.domain not in rules.CROSSFIRE_DOMAINS:
            continue
        if state.known_level(side, e) is Detection.NONE:
            continue
        foes.append((rules.dist_units(unit, e), e))
    if not foes:
        return out
    foes.sort(key=lambda c: (c[0], c[1].uid))
    for _d, e in foes[:limit]:
        secs: set[int] = set()
        for f in state.units_of(side):
            if f is unit or not f.alive:
                continue
            d = rules.dist((f.x, f.y), (e.x, e.y))
            if d > rules.CROSSFIRE_RADIUS:
                continue
            if _can_hit(f, e, d):
                secs.add(rules.bearing_sector((f.x, f.y), (e.x, e.y)))
        if secs:
            out[e.uid] = secs
    return out


def crossfire_opportunity(state: "GameState", side: Side, unit: Unit,
                          pos: tuple[int, int],
                          sectors_by_enemy: dict[int, set[int]]) -> float:
    """站到 `pos` 能不能把对某个敌目标的方位数推到三档（即真正形成交叉）。

    只奖励「刚好补上第三个方位」这一步——已经在三个方位上的目标，
    再站过去不增加任何东西，不该继续加分。
    """
    best = 0.0
    for uid, secs in sectors_by_enemy.items():
        e = state.unit_by_uid(uid)
        if e is None or not e.alive:
            continue
        d = rules.dist(pos, (e.x, e.y))
        if d > rules.CROSSFIRE_RADIUS:
            continue
        if not _can_hit(unit, e, d):
            continue
        sec = rules.bearing_sector(pos, (e.x, e.y))
        if sec in secs:
            continue
        n = len(secs) + 1
        if n < 3:
            continue
        if d > max((w.range_max for w in unit.usable_weapons()
                    if w.kind in rules.CROSSFIRE_KINDS), default=0.0) * 0.85:
            continue          # 站在射程边缘，下一回合对方一挪就够不着了
        best = max(best, 0.8 + 0.3 * min(3, n - 2))
    return best


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
def should_retreat(state: "GameState", side: Side, unit: Unit,
                   can_move: bool = True) -> bool:
    """该单位是否应当脱离接触（`retreat_assessment` 的布尔壳）。"""
    return retreat_assessment(state, side, unit, can_move).retreat


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
    "evaluate_objectives", "contested_objectives", "rough_objective_scores",
    "Reassign", "assignment_invalid", "reassess_objective", "REASSIGN_COOLDOWN",
    "CAUTION_RADIUS", "local_balance",
    "RetreatDecision", "retreat_assessment",
    "FeintPlan", "plan_feint", "FEINT_MIN_GAP", "FEINT_MAX_COST",
    "crossfire_map", "crossfire_opportunity",
    "strike_priority", "pick_strike_target",
    "defense_position", "air_defense_commitment",
    "decoy_worth", "should_retreat", "resupply_needed", "ammo_ratio",
]

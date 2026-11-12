"""防空网：多点协同、逐发拦截、中段反导。

``sam_for_intercept`` 原本和射击解算放在一起，但它只被防空网使用、
而射击解算又需要 ``interception_estimate``——把它挪到这里，依赖就
变成单向的 ``combat`` → ``adn``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

# 这几个「环境依赖」走模块属性访问（``ew.jam_strength_at`` 而不是
# ``from .ew import jam_strength_at``）：自检脚本需要把它们临时换成
# 确定性桩值，而打补丁只能改**定义处模块**的属性。函数内直接引用
# 的名字是另一份绑定，补丁打不到。
from . import dice, ew
from .enums import Side, WeaponKind
from .geo import RANGE_TOLERANCE, dist
from .model import Unit, Weapon

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

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
                        jam = ew.jam_strength_at(state, (d.x, d.y), d.side, "radar")
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
            if dice.roll(node.pk, state.rng):
                if frag_ok and dice.roll(FRAG_PARTIAL_CHANCE, state.rng):
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

"""大模型指挥官的决策层：把战局压成一段简报，换回一条「作战决心」。

**为什么只让它决定「决心」，不让它决定「动作」**：
1.7B 的模型没法可靠地输出「3 号单位沿 (4,7)-(5,8) 机动到 (6,9)」这类
带约束的动作串——错一格就会撞山、进雷场、或者把弹药打光。
而 ``AIPlanner`` 已经把「怎么走、打谁、打几发」做得比任何小模型都好。

所以分工是：**模型管意图，规则管执行**。模型只选四件事——
主攻方向、作战姿态、优先打击的兵种、要不要佯动；每个字段都是枚举，
越界值一律被本地校验挡掉。规则层再把「意图」折算成权重。

这条边界还有一个副作用是好的：模型输出再离谱，也只会让 AI 打得
「更莽」或「更保守」，不会让它作弊、不会让它看见战争迷雾里的东西，
更不会让它产生非法动作。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import rules
from .enums import Detection, Side
from .i18n import tr
from .llm_client import LLMClient, LLMError

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

#: 允许的姿态。每一项对应一对权重（推进倾向、风险规避）。
#: 数值不是拍脑袋：以 ``balanced`` 为 1.0，``assault`` 把「往前推」的
#: 收益抬 25%、把挨打的惩罚压到 75%，在平衡体检里等价于「更敢换血」。
STANCE_TUNING: dict[str, tuple[float, float]] = {
    "assault": (1.25, 0.75),
    "balanced": (1.00, 1.00),
    "hold": (0.70, 1.30),
    "screen": (0.85, 1.55),
}

STANCE_IDS: tuple[str, ...] = tuple(STANCE_TUNING)

#: 模型能选的「优先打击兵种」。只放陆海空常见类别，避免 prompt 里塞一长串
#: 玩家一辈子见不到的枚举值（卫星、加油机之类）。
FOCUS_CANDIDATES: tuple[str, ...] = (
    "infantry", "mbt", "ifv", "arty", "mlrs", "sam", "aaa", "radar",
    "hq", "engineer", "destroyer", "frigate", "carrier", "sub",
    "fighter", "bomber", "amphib", "heli", "attack_heli", "uav",
)

_JSON_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": list(STANCE_IDS)},
        "feint": {"type": "boolean"},
    },
    "required": ["stance", "feint"],
}


@dataclass(frozen=True)
class Directive:
    """一条作战决心。默认值 = 完全中性的启发式行为（等价于没有指挥官）。"""

    stance: str = "balanced"
    #: 主攻方向（格坐标）。``None`` 表示让规则层自己挑。
    main_effort: tuple[int, int] | None = None
    main_name: str = ""
    #: 优先打击的敌方兵种（``UnitClass`` 的值），最多两个。
    focus: tuple[str, ...] = ()
    feint: bool = True
    #: ``llm`` / ``fallback`` / ``replay``——界面与战报靠它区分「谁下的令」。
    source: str = "fallback"
    model: str = ""
    turn: int = 0
    #: 解析时的补充说明（模型给了非法值被修正时记下来，便于排查）
    note: str = ""

    # -- 权重 -------------------------------------------------------------
    @property
    def advance(self) -> float:
        """推进倾向：乘在「离目标更近」这一项收益上。"""
        return STANCE_TUNING.get(self.stance, (1.0, 1.0))[0]

    @property
    def caution(self) -> float:
        """风险规避：乘在「进入敌方火力包线」这一项惩罚上。"""
        return STANCE_TUNING.get(self.stance, (1.0, 1.0))[1]

    @property
    def active(self) -> bool:
        """是否真的带来了非中性影响——全中性时规则层可以跳过相关计算。"""
        return (self.stance != "balanced" or self.main_effort is not None
                or bool(self.focus) or not self.feint)

    # -- 文案 -------------------------------------------------------------
    def stance_label(self) -> str:
        return tr(f"ui.ai.stance.{self.stance}", self.stance)

    def describe(self) -> str:
        """一句本地化描述，写进战报。"""
        parts = [self.stance_label()]
        if self.main_name:
            parts.append(tr("ui.ai.main_fmt", "主攻 %s") % self.main_name)
        if self.focus:
            parts.append(tr("ui.ai.focus_fmt", "优先打击 %s")
                         % "、".join(_class_label(c) for c in self.focus))
        if not self.feint:
            parts.append(tr("ui.ai.no_feint", "不分兵佯动"))
        return tr("ui.ai.directive_fmt", "指挥官决心：%s") % "，".join(parts)

    # -- 存档 / 回放 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "main": list(self.main_effort) if self.main_effort else None,
            "main_name": self.main_name,
            "focus": list(self.focus),
            "feint": self.feint,
            "source": self.source,
            "model": self.model,
            "turn": self.turn,
        }

    @staticmethod
    def from_dict(data: Any) -> "Directive":
        """从存档/回放里还原。字段脏了不抛——回放要能容忍手改过的文件。"""
        if not isinstance(data, dict):
            return Directive()
        stance = str(data.get("stance", "balanced")).lower()
        if stance not in STANCE_TUNING:
            stance = "balanced"
        main = data.get("main")
        main_xy: tuple[int, int] | None = None
        if isinstance(main, (list, tuple)) and len(main) == 2:
            try:
                main_xy = (int(main[0]), int(main[1]))
            except (TypeError, ValueError):
                main_xy = None
        raw_focus = data.get("focus") or ()
        if isinstance(raw_focus, str):
            raw_focus = (raw_focus,)
        elif not isinstance(raw_focus, (list, tuple)):
            raw_focus = ()          # 手改过的存档里可能是数字/对象，直接丢弃
        focus = tuple(str(c).lower() for c in raw_focus
                      if str(c).lower() in FOCUS_CANDIDATES)[:2]
        feint = data.get("feint", True)
        return Directive(
            stance=stance,
            main_effort=main_xy,
            main_name=str(data.get("main_name", "") or "")[:40],
            focus=focus,
            feint=bool(feint) if isinstance(feint, bool) else True,
            source=str(data.get("source", "replay"))[:16],
            model=str(data.get("model", "") or "")[:48],
            turn=int(data.get("turn", 0) or 0),
        )


def _class_label(value: str) -> str:
    """兵种值 → 本地化名称（复用枚举的翻译表，不另开一份）。"""
    from .enums import UnitClass
    try:
        return UnitClass(value).label
    except ValueError:
        return value


# --------------------------------------------------------------------------- #
# 局势简报
# --------------------------------------------------------------------------- #
def objective_map(state: "GameState") -> list[tuple[str, Any]]:
    """目标点编号：按 (y, x) 排序后给 O1..On。

    排序必须稳定：模型上一回合说「主攻 O5」，这一回合 O5 得还是同一个点，
    否则它会觉得自己被耍了（人也会）。
    """
    tiles = sorted(state.objective_tiles, key=lambda t: (t.y, t.x))
    return [(f"O{i + 1}", t) for i, t in enumerate(tiles)]


def _force_line(units: list) -> str:
    """按兵种聚合兵力，形如 ``3x infantry, 2x sam``。"""
    counts: dict[str, int] = {}
    for u in units:
        counts[u.template.unit_class.value] = counts.get(u.template.unit_class.value, 0) + 1
    if not counts:
        return "none"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{n}x {cls}" for cls, n in ordered)


def build_prompt(state: "GameState", side: Side) -> tuple[str, str, dict]:
    """把战局压成简报。

    返回 ``(system, user, ctx)``。``ctx`` 里带目标点编号表与合法兵种集，
    供随后的输出校验使用。

    **只用本方可见的信息**：敌军按 ``known_level`` 过滤，未发现的部队
    既不进数量、也不进坐标。模型不享有任何规则之外的视野。
    """
    mine = [u for u in state.units_of(side) if u.alive]
    known = [u for u in state.enemies_of(side)
             if u.alive and state.known_level(side, u).level >= Detection.CLASSIFIED.level]
    seen_any = [u for u in state.enemies_of(side)
                if u.alive and state.known_level(side, u) is not Detection.NONE]

    objs = objective_map(state)
    ctx = {
        "ids": {oid: (t.x, t.y) for oid, t in objs},
        "names": {oid: (t.objective_name or oid) for oid, t in objs},
        "classes": sorted({u.template.unit_class.value for u in known}),
    }

    other = Side.BLUE if side is Side.RED else Side.RED
    lines: list[str] = []
    lines.append(f"Turn {state.turn_count} of {state.turn_limit}. "
                 f"Score BLUE {state.vp[Side.BLUE]} : RED {state.vp[Side.RED]}.")
    # 比分差写清楚，模型才会算「要不要冒险」
    diff = state.vp[side] - state.vp[other]
    trend = "ahead" if diff > 0 else ("behind" if diff < 0 else "level")
    lines.append(f"Your side is {side.value} and is {trend} by {abs(diff)} points.")
    lines.append("")
    lines.append("Own force: " + _force_line(mine))
    lines.append("Enemy detected: " + (_force_line(known) if known else "none"))
    if len(seen_any) > len(known):
        lines.append(f"({len(seen_any) - len(known)} more enemy contacts unidentified)")

    # 目标点：编号 / 坐标 / 价值 / 归属 / 我最近单位距离 / 附近已知敌军
    lines.append("")
    lines.append("Objectives: id (x,y) value owner dist_from_your_nearest_unit "
                 "enemy_dist known_enemy_within_6")
    contestable: list[str] = []
    for oid, t in objs:
        owner = t.owner.value if t.owner else "neutral"
        d = min((rules.dist((u.x, u.y), (t.x, t.y)) for u in mine), default=99.0)
        near = sum(1 for e in seen_any
                   if rules.dist((e.x, e.y), (t.x, t.y)) <= 6.0)
        ed = min((rules.dist((e.x, e.y), (t.x, t.y)) for e in seen_any), default=99.0)
        label = t.objective_name or ""
        lines.append(f"{oid} ({t.x},{t.y}) value{t.victory_points} {owner.lower()} "
                     f"dist {d:.0f} enemy {ed:.0f} near {near}"
                     + (f" {label}" if label else ""))
        # 只把「值得投入主力」的点交给模型选：己方后方据点不参与。
        # 否则 1.7B 会挑一个离自己最近、价值最低的自家机场当主攻方向——
        # 从地形图上看它确实「近」，但那不叫主攻，叫原地转圈。
        if t.owner is not side or near > 0:
            contestable.append(oid)
    ctx["main_allowed"] = contestable

    if state.turn_count > 1:
        prev = getattr(state, "ai_last_directive", {}).get(side)
        if prev is not None and prev.main_effort:
            lines.append("")
            lines.append(f"Last turn you chose: {prev.stance} toward "
                         f"{prev.main_name or prev.main_effort}.")

    allowed = [c for c in FOCUS_CANDIDATES if c in ctx["classes"]]
    ctx["focus_allowed"] = allowed

    lines.append("")
    lines.append("Choose this turn's intent:")
    lines.append("- stance: assault (push hard, accept losses) / balanced / "
                 "hold (defend what you own) / screen (keep distance, trade space for time)")
    lines.append("- main_effort: one objective id to mass your forces on")
    if allowed:
        lines.append("- focus: 1-2 enemy classes to prioritise, from " + "/".join(allowed))
    lines.append("- feint: whether to spare one unit for a diversion")
    lines.append("Reply with JSON only.")

    system = (
        "You are the commander of one side in a turn-based operational wargame. "
        "You decide intent only; a staff officer handles movement and fire. "
        "Scoring: each objective you control scores its value every turn; "
        "capturing an enemy objective flips it. Enemy air defences (sam/aaa) "
        "intercept air and artillery strikes. Output JSON only, no explanation."
    )
    return system, "\n".join(lines), ctx


def _schema_for(ctx: dict) -> dict:
    """按本回合的合法集合生成 schema。

    枚举必须由**本地**限定：让模型在任意字符串里挑，再靠事后正则兜底，
    等于把「输出合法性」交给一个 1.7B 的模型去保证。
    """
    import copy
    schema = copy.deepcopy(_JSON_SCHEMA_BASE)
    ids = list(ctx.get("main_allowed") or ctx.get("ids", {}))
    if ids:
        schema["properties"]["main_effort"] = {"type": "string", "enum": ids}
        schema["required"].append("main_effort")
    allowed = ctx.get("focus_allowed") or []
    if allowed:
        schema["properties"]["focus"] = {
            "type": "array",
            "items": {"type": "string", "enum": allowed},
            "minItems": 1, "maxItems": 2,
        }
        schema["required"].append("focus")
    return schema


def parse_directive(obj: dict, ctx: dict, model: str = "", turn: int = 0) -> Directive:
    """把模型输出校验成 ``Directive``。非法值就地修正，绝不抛异常。"""
    notes: list[str] = []
    stance = str(obj.get("stance", "")).strip().lower()
    if stance not in STANCE_TUNING:
        notes.append(f"stance={stance!r} 非法，按 balanced 处理")
        stance = "balanced"

    ids: dict[str, tuple[int, int]] = ctx.get("ids", {})
    raw_main = str(obj.get("main_effort", "")).strip().upper()
    main_xy = ids.get(raw_main)
    if main_xy is None and raw_main:
        notes.append(f"main_effort={raw_main!r} 不在目标点列表，忽略")

    raw_focus = obj.get("focus") or []
    if isinstance(raw_focus, str):
        raw_focus = [raw_focus]
    allowed = set(ctx.get("focus_allowed") or ())
    focus: list[str] = []
    for item in raw_focus:
        cls = str(item).strip().lower()
        if cls in allowed and cls not in focus:
            focus.append(cls)
        elif cls and cls not in allowed:
            notes.append(f"focus={cls!r} 不在可选列表，忽略")
    focus = focus[:2]

    feint = obj.get("feint", True)
    if not isinstance(feint, bool):
        notes.append("feint 不是布尔值，按 true 处理")
        feint = True

    return Directive(
        stance=stance,
        main_effort=main_xy,
        main_name=ctx.get("names", {}).get(raw_main, "") if main_xy else "",
        focus=tuple(focus),
        feint=feint,
        source="llm",
        model=model,
        turn=turn,
        note="; ".join(notes)[:160],
    )


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
class LLMCommander:
    """在后台线程里跑推理，主线程只负责「取结果或降级」。

    线程只碰 ``LLMClient`` 与自己的结果槽；**战局在交给线程之前就已经
    压成字符串了**（见 ``build_prompt``）。这条边界很重要：推理要跑 9 秒，
    期间玩家还在操作，若线程去读 ``state`` 就会读到半改的状态，轻则
    决策漂移，重则在遍历单位时撞上长度变化。
    """

    def __init__(self, client: LLMClient, enabled: bool = True):
        self.client = client
        self.enabled = enabled
        self.last_error = ""
        self.last_ms = 0
        self.calls = 0
        self.hits = 0
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._result: tuple[int, Directive] | None = None
        self._boot_failed = False

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self.enabled and not self._boot_failed and self.client.probe()

    def status_text(self) -> str:
        if not self.enabled:
            return tr("ui.ai.status_off", "规则 AI")
        if self._boot_failed or self.client.status == "error":
            return tr("ui.ai.status_error", "大模型不可用")
        if self.client.ready:
            return tr("ui.ai.status_ready", "大模型就绪")
        return tr("ui.ai.status_booting", "大模型加载中")

    # ------------------------------------------------------------------ #
    def prefetch(self, state: "GameState", side: Side) -> bool:
        """在玩家回合就先算下一步的决心。

        推理要 9 秒左右，等 AI 相位开始再算就是让玩家干等。
        放在玩家思考的时间里跑，绝大多数回合都是「已经算好了」。
        """
        if not self.enabled or self._boot_failed:
            return False
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            turn = state.turn_count
            system, user, ctx = build_prompt(state, side)
            schema = _schema_for(ctx)
            self._worker = threading.Thread(
                target=self._run, args=(turn, system, user, schema, ctx),
                name="llm-commander", daemon=True)
            self._worker.start()
            return True

    def _run(self, turn: int, system: str, user: str, schema: dict, ctx: dict) -> None:
        t0 = time.perf_counter()
        try:
            if not self.client.ready and not self.client.start():
                self._boot_failed = True
                self.last_error = self.client.last_error
                return
            obj = self.client.chat_json(system, user, schema)
            directive = parse_directive(obj, ctx, model=self.client.describe(), turn=turn)
        except LLMError as exc:
            self.last_error = str(exc)
            return
        except Exception as exc:                      # noqa: BLE001 - 线程里不能冒泡
            self.last_error = f"{type(exc).__name__}: {exc}"
            return
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        self.calls += 1
        with self._lock:
            self._result = (turn, directive)

    # ------------------------------------------------------------------ #
    def take(self, state: "GameState", side: Side, timeout: float = 0.8) -> Directive:
        """取本回合的决心；没有就返回中性决心（= 原有启发式行为）。"""
        turn = state.turn_count
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                got = self._result
            if got is not None and got[0] == turn:
                self.hits += 1
                with self._lock:
                    self._result = None
                return got[1]
            worker = self._worker
            alive = worker is not None and worker.is_alive()
            if not alive or time.time() >= deadline:
                break
            time.sleep(0.05)
        return Directive(source="fallback", turn=turn)

    def stop(self) -> None:
        self.client.stop()

    def reset(self) -> None:
        """换局/读档时清掉上一局的残留结果。"""
        with self._lock:
            self._result = None
            self._worker = None


__all__ = [
    "Directive", "FOCUS_CANDIDATES", "LLMCommander", "STANCE_IDS",
    "STANCE_TUNING", "build_prompt", "objective_map", "parse_directive",
]

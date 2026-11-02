"""教学想定与新手引导。

设计目标：让新玩家**在 10 分钟内**理解本作的核心循环
    发现 → 跟踪 → 打击 → 防守 → 结算
而不需要读文档。

实现方式是「事件驱动」的引导机：
* 每个教学步骤声明一个**触发事件**（如 fire / fortify）与可选的**状态条件**；
* 主程序在关键动作发生时调用 `Tutorial.note(event)`；
* 每帧调用 `Tutorial.poll(state)` 处理「需要看状态才能判定」的步骤；
* 命中条件后自动推进到下一步，并把新步骤写进作战日志。

步骤本身不写死在本模块里——教学想定（地图 JSON）通过 `tutorial` 字段
声明自己的步骤脚本，使得社区想定也能定制引导流程。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .enums import Detection, Side

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState


# 引导机可以识别的事件名（主程序通过 Tutorial.note 投递）
EVENTS = (
    "select_unit",     # 玩家选中了己方单位
    "hover_enemy",     # 鼠标悬停在敌方目标上
    "tracked",         # 某个敌方目标被跟踪到「跟踪」级
    "fire",            # 玩家完成了一次开火
    "counter_viewed",  # 玩家查看了交战面板的「对手视角」
    "fortify",         # 玩家构筑了工事
    "turn_end",        # 玩家结束了回合
    "next",            # 玩家点击「下一步」
    # -- v2.0：空海天协同相关 --
    "air_move",        # 玩家机动了空中单位
    "profile",         # 玩家切换了任务剖面
    "radar",           # 玩家开关了雷达
    "jam",             # 玩家开关了干扰机
    "datalink",        # 玩家开关了数据链
    "smoke",           # 玩家施放了烟幕
    "decoy",           # 玩家投放了诱饵
    "prepare",         # 玩家登记了预先射击射界
    "order",           # 玩家下达了指挥指令
)


@dataclass
class TutorialStep:
    """一个教学步骤。

    Attributes:
        id: 步骤标识（用于存档与调试）
        title: 步骤标题
        body: 详细的操作说明
        goal: 一句话目标（显示在进度条上）
        trigger: 触发事件名；为空表示只能用「下一步」推进
        requires: 额外的状态条件（可选），返回 True 才算完成
        focus: 需要高亮的界面区域（map / sidebar / topbar / log / None）
    """

    id: str
    title: str
    body: str
    goal: str = ""
    trigger: str = ""
    requires: Callable[["GameState"], bool] | None = None
    focus: str = ""

    def satisfied(self, state: "GameState") -> bool:
        if self.requires is None:
            return True
        try:
            return bool(self.requires(state))
        except Exception:
            return False


@dataclass
class Tutorial:
    """教学引导机。"""

    steps: list[TutorialStep] = field(default_factory=list)
    index: int = 0
    finished: bool = False
    _pending: set[str] = field(default_factory=set)
    #: 已经发生过的事件（跨步骤保留）。玩家可能提前做了后面才教的动作
    #: ——比如在第 1 步就把无人机推了出去。没有这份记忆的话，
    #: 教学会永远等不到那个事件，把人卡死。
    _seen: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def note(self, event: str) -> None:
        """投递一个事件。若它命中当前步骤的触发条件且条件满足，则推进步骤。

        带 `requires` 的步骤需要读取游戏状态才能判定，此时 `note` 只登记事件，
        实际推进推迟到 `poll(state)`——保证「条件不满足就不算完成」。
        """
        if self.finished or not self.steps:
            return
        self._pending.add(event)
        self._seen.add(event)
        step = self.current
        if step is None:
            return
        if step.trigger == event and step.requires is None:
            self.advance()

    def poll(self, state: "GameState") -> None:
        """轮询状态型步骤：触发事件已发生且状态条件满足时推进。

        事件在 ``_pending``（本步骤内）或 ``_seen``（此前任何时候）里都算
        「已发生」——这样玩家提前做过的事不会让教学卡住。
        """
        if self.finished or not self.steps:
            return
        step = self.current
        if step is None or not step.trigger:
            return
        if step.trigger not in self._pending and step.trigger not in self._seen:
            return
        if step.satisfied(state):
            self.advance()

    def advance(self) -> None:
        """推进到下一步；已是最后一步则结束教学。"""
        if self.finished:
            return
        self.index += 1
        self._pending.clear()
        if self.index >= len(self.steps):
            self.index = len(self.steps)
            self.finished = True

    def skip(self) -> None:
        """跳过整个教学。"""
        self.index = len(self.steps)
        self.finished = True

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def current(self) -> TutorialStep | None:
        if self.finished or self.index >= len(self.steps):
            return None
        return self.steps[self.index]

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def progress_text(self) -> str:
        return f"教学 {min(self.index + 1, self.total)}/{self.total}"

    def describe(self) -> str:
        """当前步骤的一句话描述（用于状态栏与日志）。"""
        step = self.current
        return f"{step.title}——{step.goal}" if step else "教学完成"


# --------------------------------------------------------------------------- #
# 步骤脚本
# --------------------------------------------------------------------------- #
def _any_tracked_target(state: "GameState") -> bool:
    """是否存在被己方「跟踪」级的敌方目标（可装订射击诸元）。"""
    for e in state.enemies_of(state.player_side):
        if e.alive and state.known_level(state.player_side, e) is Detection.TRACKED:
            return True
    return False


def default_steps() -> list[TutorialStep]:
    """标准新手引导脚本（4 步，覆盖探测—打击—防守—工事）。"""
    return [
        TutorialStep(
            id="welcome",
            title="第 1 步 · 认识战场",
            body="欢迎来到方块兵棋。左侧是战场，右侧三个页签分别是单位详情、战役态势与作战日志。\n"
                 "先点击地图上的任意一个蓝色单位（推荐：近程防空连），看看它有什么传感器和武器。",
            goal="点击任意一个己方单位",
            trigger="select_unit",
            focus="sidebar",
        ),
        TutorialStep(
            id="detect",
            title="第 2 步 · 发现与识别",
            body="战场上灰色的问号代表「有接触但未识别」的目标。\n"
                 "把鼠标移到问号上，会弹出目标详情——这就是悬停侦察。\n"
                 "想要看得更清楚，就让侦察单元往前靠：传感器越近，识别等级越高。",
            goal="悬停查看一个敌方目标",
            trigger="hover_enemy",
            focus="map",
        ),
        TutorialStep(
            id="engage",
            title="第 3 步 · 跟踪与开火",
            body="只有被「跟踪」的目标才能装订射击诸元。\n"
                 "把火箭炮连往前推，缩短与敌目标的距离——目标一旦进入射程即可开火。\n"
                 "点击敌方目标，再按回车确认开火。",
            goal="跟踪目标并开火",
            trigger="fire",
            requires=_any_tracked_target,
            focus="map",
        ),
        TutorialStep(
            id="defend",
            title="第 4 步 · 看清对面在干什么",
            body="交战面板下方的「对手视角」会告诉你：\n"
                 "· 敌方有没有火控通道、能不能反击你；\n"
                 "· 谁会拦截你的弹药、拦截概率多大；\n"
                 "· 这一发为什么打不到。\n"
                 "开火前先看这里，能省下整整一排炮弹。",
            goal="查看一次对手视角",
            trigger="counter_viewed",
            focus="sidebar",
        ),
        TutorialStep(
            id="fortify",
            title="第 5 步 · 构筑工事",
            body="步兵驻守不动时可以挖壕据守。\n"
                 "选中轻步兵连 → 点击右侧「筑工事」按钮，工事会降低被命中的概率，"
                 "同时让你更难被发现。\n"
                 "注意：一旦机动，工事就会失效。",
            goal="让步兵构筑一道工事",
            trigger="fortify",
            focus="sidebar",
        ),
        TutorialStep(
            id="finish",
            title="第 6 步 · 结束回合",
            body="按空格结束回合，红方 AI 会开始行动。\n"
                 "留心「作战日志」——每一次探测、拦截与命中都记录在那里。\n"
                 "你已经掌握了本作的核心循环，接下来去试试真正的想定吧。",
            goal="结束第一个回合",
            trigger="turn_end",
            focus="topbar",
        ),
    ]


def steps_from_scenario(raw: dict) -> list[TutorialStep] | None:
    """从想定 JSON 中读取自定义教学脚本。

    想定里可以这样声明（所有字段均可省略）：
        "tutorial": {
          "steps": [
            {"id": "x", "title": "...", "body": "...", "goal": "...", "trigger": "fire"}
          ]
        }
    未声明时返回 None，由调用方回退到默认脚本。
    """
    spec = raw.get("tutorial")
    if not isinstance(spec, dict):
        return None
    items = spec.get("steps")
    if not isinstance(items, list) or not items:
        return None
    out: list[TutorialStep] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", f"第 {i + 1} 步"))
        body = str(item.get("body", ""))
        goal = str(item.get("goal", ""))
        trigger = str(item.get("trigger", ""))
        if trigger and trigger not in EVENTS:
            trigger = ""
        out.append(TutorialStep(
            id=str(item.get("id", f"step{i + 1}")),
            title=title,
            body=body,
            goal=goal,
            trigger=trigger,
            focus=str(item.get("focus", "")),
        ))
    return out or None


# 教学想定的地图 ID：加载该想定时自动启用引导
TUTORIAL_SCENARIO_ID = "training_ground"


def build_tutorial(raw: dict) -> Tutorial | None:
    """为一个想定构造教学引导机。

    优先级：想定内自定义脚本 → 教学想定的标准脚本 → **没有教学**。

    **普通想定必须返回 None**（而不是回退到默认脚本）：

    教学引导机会监听玩家的每一个操作（选中、开火、结束回合……），
    每推进一步就往作战日志里写一条「教学：第 N 步 · …」。
    如果普通想定也挂了引导机，玩家在正规作战中会看到
    与当前战局毫不相干的教学提示——实测「海峡对决」里
    日志中会冒出「教学：第 2 步 · 发现与识别」这种条目。

    教学是**教学想定的事**，不是所有想定的默认行为。
    """
    steps = steps_from_scenario(raw)
    if steps is None and raw.get("id") == TUTORIAL_SCENARIO_ID:
        steps = default_steps()
    if steps is None:
        return None
    return Tutorial(steps=steps)



__all__ = [
    "EVENTS", "TutorialStep", "Tutorial", "default_steps",
    "steps_from_scenario", "build_tutorial", "TUTORIAL_SCENARIO_ID",
]

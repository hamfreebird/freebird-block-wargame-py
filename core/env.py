"""战场环境：时段与天候。

一回合 = 1 小时，因此「时段」可由回合号直接推算：
    回合 1 = 拂晓 → 回合 7 = 正午 → 回合 13 = 黄昏 → 回合 19 = 深夜
天候则按想定种子做确定性伪随机序列——同一想定每局的天候完全一致，
这样「重开一局换个天气重试」不会成为可利用的随机源，读档也能精确复现。

对作战的实际影响集中在**探测矩阵**：
* 夜间光学探测大幅衰减，红外探测反而增强（红外是夜战主力）；
* 雨雾削弱雷达与光电，但几乎不影响红外；
* 因此天候会改变「什么传感器在什么时段最值钱」，
  玩家与 AI 都必须据此调整侦察与打击节奏。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .enums import SensorKind, TimeOfDay, Weather


# --------------------------------------------------------------------------- #
# 时段
# --------------------------------------------------------------------------- #
# 一天的时段分布（24 格）。回合从 06:00（拂晓）起算。
TIME_TABLE: list[TimeOfDay] = (
    [TimeOfDay.NIGHT] * 5          # 00:00–04:00
    + [TimeOfDay.DAWN]             # 05:00
    + [TimeOfDay.DAWN]             # 06:00
    + [TimeOfDay.DAY] * 9          # 07:00–15:00
    + [TimeOfDay.DUSK] * 3         # 16:00–18:00
    + [TimeOfDay.NIGHT] * 6        # 19:00–23:00
)

# 一个昼夜折合多少回合。想定通常 16–18 回合，若严格「1 回合 = 1 小时」，
# 整局只在拂晓到黄昏之间，夜战与恶劣天候机制无从体现。
# 因此压缩为 6 回合 = 1 个昼夜：一个 18 回合的想定可覆盖约 3 个昼夜。
DEFAULT_DAY_LENGTH = 6

# 时段对各类传感器的探测系数
TIME_FACTORS: dict[TimeOfDay, dict[str, float]] = {
    TimeOfDay.DAWN: {"optical": 0.70, "thermal": 1.05, "radar": 1.00},
    TimeOfDay.DAY: {"optical": 1.00, "thermal": 0.92, "radar": 1.00},
    TimeOfDay.DUSK: {"optical": 0.62, "thermal": 1.12, "radar": 0.98},
    TimeOfDay.NIGHT: {"optical": 0.28, "thermal": 1.32, "radar": 0.94},
}

# 天候对各类传感器的探测系数
WEATHER_FACTORS: dict[Weather, dict[str, float]] = {
    Weather.CLEAR: {"optical": 1.00, "thermal": 1.00, "radar": 1.00},
    Weather.RAIN: {"optical": 0.82, "thermal": 0.95, "radar": 0.84},
    Weather.FOG: {"optical": 0.42, "thermal": 0.90, "radar": 0.62},
    Weather.SNOW: {"optical": 0.68, "thermal": 0.86, "radar": 0.78},
}

# 风向按汉语习惯用**来向**命名（西北风 = 从西北来、吹向东南），
# 但烟幕移动用的是**去向**。两者相差 180°，所以这张表存的是
# 「指定来向时，格子坐标该往哪边挪」，方向必须反着写——
# 写成同向会让「顺风放烟」变成往敌人脸上送烟。
# 地图 y 轴向下（北 = y 减小），故北风的去向是 (0, +1)。
WIND_VECTORS: tuple[tuple[int, int], ...] = (
    (0, 1),     # 0 北风 → 向南
    (-1, 1),    # 1 东北风 → 向西南
    (-1, 0),    # 2 东风 → 向西
    (-1, -1),   # 3 东南风 → 向西北
    (0, -1),    # 4 南风 → 向北
    (1, -1),    # 5 西南风 → 向东北
    (1, 0),     # 6 西风 → 向东
    (1, 1),     # 7 西北风 → 向东南
)

#: 风向标签的键后缀（与 WIND_VECTORS 同序）
WIND_CODES: tuple[str, ...] = ("n", "ne", "e", "se", "s", "sw", "w", "nw")

#: 风力档位（0 静风 … 3 强风）：决定烟幕移动与消散速度
WIND_MAX = 3

# 天候变化倾向：雨雾不会连续出现太久
WEATHER_CHAIN: dict[Weather, tuple[Weather, ...]] = {
    Weather.CLEAR: (Weather.CLEAR, Weather.CLEAR, Weather.RAIN, Weather.FOG, Weather.CLEAR),
    Weather.RAIN: (Weather.RAIN, Weather.CLEAR, Weather.CLEAR, Weather.SNOW, Weather.FOG),
    Weather.FOG: (Weather.FOG, Weather.CLEAR, Weather.RAIN, Weather.CLEAR, Weather.SNOW),
    Weather.SNOW: (Weather.SNOW, Weather.CLEAR, Weather.CLEAR, Weather.FOG, Weather.CLEAR),
}


def _sensor_band(kind: SensorKind) -> str:
    """传感器归入哪个天候/时段系数通道。

    只有主动辐射与光电手段受天候影响：
    * 雷达 —— 雨雾衰减严重；
    * 红外 —— 几乎不受天候影响，夜间反而增强；
    * 光学 —— 昼夜与雨雾影响最大；
    * 电子侦察 / 声呐 / 卫星 —— 被动或天基手段，不受天候影响。
    """
    if kind is SensorKind.RADAR:
        return "radar"
    if kind is SensorKind.IR:
        return "thermal"
    if kind is SensorKind.OPTICAL:
        return "optical"
    return "none"      # 被动/天基探测不受天候影响


# --------------------------------------------------------------------------- #
# 环境状态
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Environment:
    """某一回合的战场环境快照。"""

    time_of_day: TimeOfDay = TimeOfDay.DAWN
    weather: Weather = Weather.CLEAR
    turn: int = 1
    day_length: int = DEFAULT_DAY_LENGTH
    wind_index: int = 0
    wind_speed: int = 0

    @property
    def label(self) -> str:
        return f"{self.time_of_day.label}·{self.weather.label}"

    @property
    def clock(self) -> str:
        """战场时钟：按作战日长度折算的小时。"""
        hour = (5 + self.turn * 24 // max(1, self.day_length)) % 24
        return f"{hour:02d}:00"

    def factor(self, kind: SensorKind) -> float:
        """该类传感器在当前环境下的探测距离系数。"""
        band = _sensor_band(kind)
        if band == "none":
            return 1.0        # 被动/天基探测不受天候影响
        t = TIME_FACTORS[self.time_of_day]
        w = WEATHER_FACTORS[self.weather]
        return t[band] * w[band]

    def describe_effects(self) -> str:
        """一行式说明，用于顶栏与战报。

        分隔符与「无影响」也要吃语言包——中文用顿号，日文用中点，
        拉丁语系用逗号，硬编码哪一个都会在别的语言里显得别扭。
        """
        from .i18n import tr
        parts = []
        for kind in (SensorKind.OPTICAL, SensorKind.IR, SensorKind.RADAR):
            f = self.factor(kind)
            if abs(f - 1.0) >= 0.02:
                parts.append(tr("env.effect_fmt", "%s×%.2f") % (kind.label, f))
        return tr("env.join", "、").join(parts) if parts else tr("env.no_effect", "无显著影响")

    # -- 风 ---------------------------------------------------------------
    @property
    def wind_vector(self) -> tuple[int, int]:
        """烟幕随风漂移的格偏移（去向）。"""
        return WIND_VECTORS[self.wind_index % len(WIND_VECTORS)]

    @property
    def wind_label(self) -> str:
        """风向名（来向），如「西北」。"""
        from .i18n import tr
        code = WIND_CODES[self.wind_index % len(WIND_CODES)]
        return tr(f"env.wind_{code}", {"n": "北", "ne": "东北", "e": "东", "se": "东南",
                                       "s": "南", "sw": "西南", "w": "西", "nw": "西北"}[code])

    def wind_text(self) -> str:
        """顶栏与战报用的一句话，如「西北风 2 级」。"""
        from .i18n import tr
        return tr("env.wind_fmt", "%s风 %d 级") % (self.wind_label, self.wind_speed)

    @property
    def smoke_period(self) -> int:
        """烟幕多少回合移动一格：静风不移动，微风两回合，和风以上每回合。"""
        if self.wind_speed <= 0:
            return 0
        return 2 if self.wind_speed == 1 else 1


def _seeded_chain(seed: int, length: int) -> list[Weather]:
    """按种子生成确定性的天候序列。

    用 hashlib 把种子打散成字节流，再逐字节索引天候链——
    保证同一种子必得同一序列，且序列在回合之间连续（不会跳变）。
    """
    out: list[Weather] = []
    cur = Weather.CLEAR
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    for i in range(max(1, length)):
        b = digest[i % len(digest)]
        chain = WEATHER_CHAIN[cur]
        cur = chain[b % len(chain)]
        out.append(cur)
    return out


class EnvironmentTrack:
    """按想定追踪天候演进的容器。

    想定可指定 `weather` 字段：
      * `"clear"` 等固定天候 → 整局不变；
      * `"random"`（默认）→ 按种子演进；
      * `"cycle"` → 晴/雨/雾/雪 固定轮转，便于测试与教学。
    """

    def __init__(self, seed: int = 0, mode: str = "random",
                 fixed: Weather | None = None, day_length: int = DEFAULT_DAY_LENGTH):
        self.seed = seed
        self.mode = mode
        self.fixed = fixed
        self.day_length = max(1, int(day_length))
        self._chain: list[Weather] = []

    @classmethod
    def from_scenario(cls, scenario) -> "EnvironmentTrack":
        raw = scenario.raw if hasattr(scenario, "raw") else {}
        spec = raw.get("weather", "random")
        seed = raw.get("weather_seed", raw.get("id", "blockwargame"))
        if isinstance(seed, str):
            seed = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
        day_length = int(raw.get("day_length", DEFAULT_DAY_LENGTH) or DEFAULT_DAY_LENGTH)
        if spec in ("clear", "rain", "fog", "snow"):
            return cls(seed=int(seed), mode="fixed", fixed=Weather(spec),
                       day_length=day_length)
        if spec == "cycle":
            return cls(seed=int(seed), mode="cycle", day_length=day_length)
        return cls(seed=int(seed), mode="random", day_length=day_length)

    def weather_at(self, turn: int) -> Weather:
        if self.mode == "fixed" and self.fixed is not None:
            return self.fixed
        if self.mode == "cycle":
            order = (Weather.CLEAR, Weather.RAIN, Weather.FOG, Weather.SNOW)
            return order[(max(1, turn) - 1) % len(order)]
        # random：惰性扩展链条
        need = max(1, turn)
        if len(self._chain) < need:
            self._chain = _seeded_chain(self.seed, need + 8)
        return self._chain[max(1, turn) - 1]

    def at(self, turn: int) -> Environment:
        """取第 turn 回合的环境快照。"""
        idx = max(1, turn) - 1
        # 按作战日长度把回合映射到 24 小时的一天
        hours = (5 * self.day_length + idx * 24) // max(1, self.day_length)
        tod = TIME_TABLE[hours % 24]
        wi, ws = self.wind_at(turn)
        return Environment(time_of_day=tod, weather=self.weather_at(turn), turn=turn,
                           day_length=self.day_length, wind_index=wi, wind_speed=ws)

    def wind_at(self, turn: int) -> tuple[int, int]:
        """第 turn 回合的风（来向索引, 风力档位）。

        与天候共用想定种子，但走**另一条哈希流**：风场不该因为
        「某回合下了雨」而整体错位，否则调天候参数会连带改掉全部风向，
        想定作者就说不清自己改的是什么了。天候只作为**修正项**叠加：
        雨雪伴随大风（+1 档），雾天近地面风弱（压到 1 档以内）。
        """
        digest = hashlib.sha256(f"{self.seed}:wind".encode("utf-8")).digest()
        i = (max(1, turn) - 1) * 2
        idx = digest[i % len(digest)] % len(WIND_VECTORS)
        speed = digest[(i + 1) % len(digest)] % 3        # 基础 0–2 档
        w = self.weather_at(turn)
        if w in (Weather.RAIN, Weather.SNOW):
            speed += 1                                   # 雨雪压不住烟，反而吹得散
        elif w is Weather.FOG:
            speed = min(speed, 1)                        # 雾天近地面静稳，烟幕糊在原地
        return idx, max(0, min(WIND_MAX, speed))


def environment_for(state) -> Environment:
    """从游戏状态取当前环境（无 track 时回退到默认）。"""
    track = getattr(state, "env_track", None)
    turn = getattr(state, "turn_count", 1)
    if track is None:
        return Environment(TimeOfDay.DAWN, Weather.CLEAR, turn)
    return track.at(turn)


# --------------------------------------------------------------------------- #
# 视觉表现
# --------------------------------------------------------------------------- #
# 时段色调：(R, G, B, 叠加透明度)
TIME_TINTS: dict[TimeOfDay, tuple[tuple[int, int, int], int]] = {
    TimeOfDay.DAWN: ((70, 60, 90), 46),
    TimeOfDay.DAY: ((255, 250, 235), 0),
    TimeOfDay.DUSK: ((110, 70, 60), 52),
    TimeOfDay.NIGHT: ((10, 16, 38), 108),
}

# 天候色调
#: 天候色调的透明度比 v3.4 之前低：雾不是「贴一层灰」，
#: 它应该降低远处对比度，而不是把整幅画面压进中灰区间。
WEATHER_TINTS: dict[Weather, tuple[tuple[int, int, int], int]] = {
    Weather.CLEAR: ((0, 0, 0), 0),
    Weather.RAIN: ((40, 60, 85), 52),
    Weather.FOG: ((185, 195, 200), 58),
    Weather.SNOW: ((205, 220, 240), 46),
}

#: 时段与天候叠加后的总不透明度上限。
#: 为什么要有：旧实现是两层的 alpha **直接相加**再截到 150，
#: 于是「夜 108 + 雾 74 = 182 → 150」——整幅画面 59% 被同一层灰盖住，
#: 地图动态范围从 250 掉到 104（−58%），地形与棋子一起糊成中灰。
#: 夜雾本来就该看不清，但「看不清」应当是暗处看不清、被照亮的仍看得清。
#: 120 是实测取舍：夜雾动态范围回到 133（≈拂晓+雾的水平），
#: 同时夜间仍明显比拂晓更暗（合成色不同，不是靠 alpha 区分）。
_TINT_CAP = 120


def env_tint(env: Environment) -> tuple[tuple[int, int, int], int]:
    """返回当前环境对应的叠加色调与透明度（渲染层使用）。

    两层叠加的**正确**合成是 ``1-(1-a1)(1-a2)``，不是 ``a1+a2``：
    后者在两层都较浓时会算出「比任何一层都更不透明」的结果，
    等于把两片滤镜当成一片更厚的滤镜。颜色仍按 alpha 加权平均
    （近似「先叠时段、再叠天候」的视觉效果）。
    """
    t_col, t_a = TIME_TINTS[env.time_of_day]
    w_col, w_a = WEATHER_TINTS[env.weather]
    # 合成：以天候为主、时段为辅
    r = (t_col[0] * t_a + w_col[0] * w_a) // max(1, t_a + w_a)
    g = (t_col[1] * t_a + w_col[1] * w_a) // max(1, t_a + w_a)
    b = (t_col[2] * t_a + w_col[2] * w_a) // max(1, t_a + w_a)
    a = 255.0 * (1.0 - (1.0 - t_a / 255.0) * (1.0 - w_a / 255.0))
    return (r, g, b), min(_TINT_CAP, int(round(a)))


__all__ = [
    "Environment", "EnvironmentTrack", "TIME_TABLE", "TIME_FACTORS",
    "WEATHER_FACTORS", "WEATHER_CHAIN", "environment_for", "env_tint",
    "DEFAULT_DAY_LENGTH", "WIND_VECTORS", "WIND_CODES", "WIND_MAX",
]

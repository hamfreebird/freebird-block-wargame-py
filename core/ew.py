"""电子战：干扰场、卫星导航完好度、诱饵信号特征。

干扰场按「按格累加」构建并缓存到对局状态上——每次探测判定都要问
「这个位置的干扰有多强」，遍历全部干扰机找答案属于纯浪费。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .enums import SensorKind
from .geo import _cache_of, dist

if TYPE_CHECKING:  # pragma: no cover
    from .engine import GameState

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

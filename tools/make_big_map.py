"""生成 128×128 大型想定「海峡对决」（架空中美战争）。

设定
----
架空时间线：某年双方在东亚大陆架海峡爆发全面战争。美方（蓝）自西
太平洋展开反攻，中方（红）依托大陆纵深与岸线防御体系据守。

地图结构（自西向东）
--------------------
* **西侧开阔海域**（x 0–28）：蓝方舰母编队与远程打击的前进基地；
* **中央海峡**（x 30–72）：双方必争的喇叭口，中段散布岛链；
* **东侧大陆**（x 74–127）：红方本土纵深——城市带、山地防线、
  沿海机场群与内陆导弹基地。

地形刻意丰富：海域、滩涂、平原、林地、丘陵、山地、城区、荒漠、
沼泽九种地形都有分布，另有山脉走向、河流冲积平原与城市群落。

双方部署
--------
蓝方 26 种兵种 / 40 个单位，红方 30 种兵种 / 44 个单位——
覆盖坦克、步兵、装甲、火炮、火箭炮、防空、电子战、雷达、指挥所、
战斗机、预警机、轰炸机、无人机、直升机、驱逐舰、护卫舰、航母、
潜艇与卫星。

用法::

    python tools/make_big_map.py
"""
from __future__ import annotations

import json
import math
import os
import random
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "maps", "strait_confrontation.json")

W = H = 128
TILE_KM = 20          # 每格 20 km —— 大尺度战场
TURN_LIMIT = 20

# 地形字符
WATER, COAST, PLAIN, FOREST, HILL, MTN, URBAN, DESERT, MARSH = "wcpfhmuds"

# --------------------------------------------------------------------------- #
# 地形生成
# --------------------------------------------------------------------------- #
def _hash2(x: int, y: int, seed: int) -> float:
    """确定性二维哈希 → [0, 1)。"""
    h = (x * 374761393 + y * 668265263 + seed * 1442695040888963407) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    h = h ^ (h >> 16)
    return h / 0xFFFFFFFF


def _value_noise(x: float, y: float, scale: float, seed: int) -> float:
    """双线性插值的值噪声。"""
    fx, fy = x / scale, y / scale
    x0, y0 = int(math.floor(fx)), int(math.floor(fy))
    tx, ty = fx - x0, fy - y0
    tx = tx * tx * (3 - 2 * tx)
    ty = ty * ty * (3 - 2 * ty)
    n00 = _hash2(x0, y0, seed)
    n10 = _hash2(x0 + 1, y0, seed)
    n01 = _hash2(x0, y0 + 1, seed)
    n11 = _hash2(x0 + 1, y0 + 1, seed)
    a = n00 + (n10 - n00) * tx
    b = n01 + (n11 - n01) * tx
    return a + (b - a) * ty


def _fbm(x: float, y: float, seed: int, octaves: int = 4) -> float:
    """分形噪声，多倍频叠加。"""
    total = 0.0
    amp = 1.0
    norm = 0.0
    freq = 1.0
    for i in range(octaves):
        total += _value_noise(x * freq, y * freq, 24.0, seed + i * 17) * amp
        norm += amp
        amp *= 0.5
        freq *= 2.0
    return total / norm


def _coast_line(y: int) -> float:
    """东侧大陆海岸线在给定纬度上的 x 坐标（带起伏）。"""
    base = 74.0
    wobble = math.sin(y * 0.09) * 6.0 + math.sin(y * 0.031 + 1.7) * 9.0
    return base + wobble


def _island(cx: float, cy: float, x: int, y: int, rx: float, ry: float) -> bool:
    dx = (x - cx) / rx
    dy = (y - cy) / ry
    return dx * dx + dy * dy <= 1.0


def _in_strait(x: int, y: int) -> bool:
    """中央海峡：x 在 30–72 之间。"""
    return 30 <= x <= 72


def build_terrain(seed: int = 20270101) -> tuple[list[str], list[str]]:
    """生成地形图与高程图。"""
    grid: list[list[str]] = []
    elev: list[list[int]] = []
    # 中央海峡中的岛链（自北向南，形成断续屏障）
    islands = [
        (38, 22, 5.5, 4.0), (47, 41, 4.0, 3.2), (55, 30, 6.0, 3.6),
        (44, 66, 4.6, 3.4), (61, 55, 5.2, 4.2), (52, 86, 5.0, 3.8),
        (66, 78, 3.8, 3.0), (35, 100, 4.4, 3.6), (58, 108, 5.6, 4.4),
        # 西太平洋的岛链基地群（蓝方前进基地的落脚点）
        (12, 40, 6.5, 5.0), (10, 60, 5.5, 4.5), (9, 82, 6.0, 4.6),
        (15, 24, 4.4, 3.6), (14, 48, 4.0, 3.2),
    ]

    for y in range(H):
        trow = []
        erow = []
        for x in range(W):
            coast = _coast_line(y)
            # 基础高程：大尺度起伏
            e = _fbm(x * 0.9, y * 0.9, seed, 5)
            ridge = _fbm(x * 1.7, y * 1.7, seed + 99, 3)
            # 干旱度（东南内陆）与湿度（河网低洼）
            dry = _fbm(x * 0.55, y * 0.55, seed + 555, 3)
            wet = _fbm(x * 0.7, y * 0.7, seed + 777, 3)
            # 岛链抬升
            island_bonus = 0.0
            for cx, cy, rx, ry in islands:
                d = math.hypot((x - cx) / rx, (y - cy) / ry)
                if d < 1.25:
                    island_bonus = max(island_bonus, (1.25 - d) * 0.9)
            land = e + island_bonus
            if x > coast:
                # 大陆：向东南内陆逐渐升高，形成纵深山脊
                depth = (x - coast) / max(1.0, (W - coast))
                land += depth * 0.42 + ridge * 0.18
            elif _in_strait(x, y):
                # 海峡：只有岛链是陆地
                land = e * 0.35 + island_bonus

            h = int(land * 100)
            erow.append(max(0, min(4, h // 25)))
            # 地形判定（阈值按实际高程分布标定：大陆约 60–115）
            if island_bonus > 0.18:
                trow.append(MTN if h > 78 else (HILL if h > 62 else PLAIN))
            elif x > coast:
                if h > 100:
                    trow.append(MTN)
                elif h > 88:
                    trow.append(HILL)
                elif h > 76:
                    trow.append(FOREST)
                elif h > 66:
                    # 东南内陆干旱区
                    trow.append(DESERT if dry > 0.50 else PLAIN)
                elif h > 60:
                    # 低洼且潮湿 → 沼泽
                    trow.append(MARSH if wet > 0.52 else PLAIN)
                else:
                    trow.append(DESERT if dry > 0.44 else MARSH)
            elif _in_strait(x, y):
                trow.append(COAST if h > 62 else WATER)
            else:
                # 西侧海域：仅近岸一线是滩涂
                trow.append(COAST if h > 58 else WATER)
        grid.append("".join(trow))
        elev.append("".join(str(v) for v in erow))
    return grid, elev


def add_cities(grid: list[str], seed: int = 4242) -> list[tuple[int, int]]:
    """把城市群落刷到指定格上，返回城市坐标。"""
    centers = [(96, 30), (104, 58), (92, 84), (110, 100), (88, 12), (118, 40)]
    cities: list[tuple[int, int]] = []
    for cx, cy in centers:
        r = 4 + int(_hash2(cx, cy, seed) * 3)
        for y in range(max(0, cy - r), min(H, cy + r + 1)):
            for x in range(max(0, cx - r), min(W, cx + r + 1)):
                d = math.hypot(x - cx, y - cy)
                if d <= r and _hash2(x, y, seed) < 0.62:
                    grid[y] = grid[y][:x] + URBAN + grid[y][x + 1:]
                    cities.append((x, y))
    return cities


def _free_spot(grid: list[str], x: int, y: int,
               taken: set[tuple[int, int]], want_land: bool) -> tuple[int, int]:
    """从 (x,y) 螺旋外扩，找一个未被占用的合适格。"""
    for r in range(1, 14):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                nx, ny = x + dx, y + dy
                if not (0 <= ny < H and 0 <= nx < W):
                    continue
                if (nx, ny) in taken:
                    continue
                cell = grid[ny][nx]
                if want_land and cell == WATER:
                    continue
                if not want_land and cell != WATER:
                    continue
                return (nx, ny)
    return (x, y)


def snap_to_water(grid: list[str], x: int, y: int,
                  radius: int = 10) -> tuple[int, int]:
    """把坐标吸附到最近的水域格（舰艇用）。"""
    if 0 <= y < H and 0 <= x < W and grid[y][x] == WATER:
        return (x, y)
    best = (x, y)
    best_d = 10 ** 9
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            nx, ny = x + dx, y + dy
            if not (0 <= ny < H and 0 <= nx < W):
                continue
            if grid[ny][nx] != WATER:
                continue
            d = dx * dx + dy * dy
            if d < best_d:
                best_d = d
                best = (nx, ny)
    return best


def snap_to_land(grid: list[str], x: int, y: int,
                 radius: int = 8) -> tuple[int, int]:
    """把坐标吸附到最近的非水域格。

    设施、工事与地面部署都必须落在陆地上——手写坐标难免有偏差，
    与其在生成后逐个报错，不如生成时就自动对齐。
    """
    if 0 <= y < H and 0 <= x < W and grid[y][x] != WATER:
        return (x, y)
    best = (x, y)
    best_d = 10 ** 9
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            nx, ny = x + dx, y + dy
            if not (0 <= ny < H and 0 <= nx < W):
                continue
            if grid[ny][nx] == WATER:
                continue
            d = dx * dx + dy * dy
            if d < best_d:
                best_d = d
                best = (nx, ny)
    return best


# --------------------------------------------------------------------------- #
# 设施与目标点
# --------------------------------------------------------------------------- #
def build_facilities(grid: list[str]) -> list[dict]:
    """西侧蓝方基地群 + 东侧红方基地群。"""
    facs: list[dict] = []

    def add(x, y, kind, owner, hp):
        x, y = snap_to_land(grid, x, y)
        facs.append({"x": x, "y": y, "facility": kind, "owner": owner, "hp": hp})

    # 蓝方：西太平洋前进基地（菲律宾方向）
    add(10, 40, "port", "BLUE", 140)
    add(12, 44, "airbase", "BLUE", 150)
    add(8, 52, "port", "BLUE", 130)
    add(14, 36, "airbase", "BLUE", 140)
    add(11, 62, "airbase", "BLUE", 130)
    add(9, 30, "radar_site", "BLUE", 110)
    add(6, 74, "port", "BLUE", 120)
    add(13, 88, "airbase", "BLUE", 130)
    # 蓝方在岛链上的前进基地
    add(38, 21, "airbase", "BLUE", 90)
    add(47, 40, "sam_site", "BLUE", 80)
    add(55, 29, "port", "BLUE", 85)
    # 中立岛屿设施
    add(44, 65, "radar_site", "NEUTRAL", 90)
    add(61, 54, "port", "NEUTRAL", 80)

    # 红方：大陆东岸基地群
    add(80, 36, "airbase", "RED", 150)
    add(78, 52, "port", "RED", 140)
    add(84, 20, "airbase", "RED", 140)
    add(82, 66, "airbase", "RED", 140)
    add(88, 96, "port", "RED", 130)
    add(104, 44, "airbase", "RED", 150)
    add(100, 78, "airbase", "RED", 140)
    add(112, 62, "airbase", "RED", 140)
    add(92, 10, "radar_site", "RED", 120)
    add(76, 88, "port", "RED", 120)
    add(120, 88, "airbase", "RED", 140)
    add(90, 60, "sam_site", "RED", 100)
    return facs


def build_objectives(grid: list[str]) -> list[dict]:
    """目标点：岛链是胜负手，大陆要点是纵深。"""
    raw = [
        # 岛链要点：价值高——谁控制岛链谁掌握制海权，是真正的胜负手。
        # 位置集中在蓝方推进轴线（x≈30–58）上，双方都够得着。
        (38, 22, "北屿据点", "capture", "BLUE", 9),
        (46, 33, "中央岛锚地", "capture", "NEUTRAL", 12),
        (40, 60, "西侧前哨", "capture", "NEUTRAL", 10),
        (48, 80, "南方航道", "capture", "NEUTRAL", 10),
        (56, 72, "东南礁群", "capture", "NEUTRAL", 7),
        (56, 44, "西岸门户", "capture", "NEUTRAL", 11),
        # 两岸要点：红方凭地利易守，蓝方要付出代价才能拿下
        (62, 64, "南屿补给点", "capture", "NEUTRAL", 9),
        (70, 68, "红方滩头", "capture", "RED", 8),
        (96, 58, "东岸都会", "capture", "RED", 7),
        (108, 34, "北部重镇", "capture", "NEUTRAL", 6),
        (100, 100, "南方工业城", "capture", "NEUTRAL", 6),
        # 双方后方基地：守住即保底
        (12, 44, "蓝方前进基地", "hold", "BLUE", 5),
        (104, 44, "红方腹地机场", "hold", "RED", 5),
    ]
    out = []
    for x, y, name, kind, side, value in raw:
        x, y = snap_to_land(grid, x, y)
        out.append({"x": x, "y": y, "name": name, "kind": kind, "side": side,
                    "value": value, "owner": side})
    return out


def build_forts(facs: list[dict], grid: list[str]) -> list[dict]:
    """工事：双方基地与要点周围的预设工事。"""
    forts = []
    for f in facs:
        if f["owner"] == "NEUTRAL":
            continue
        lvl = 3 if f["facility"] in ("airbase", "port") else 2
        forts.append({"x": f["x"], "y": f["y"], "level": lvl, "owner": f["owner"]})
    # 前线要点工事
    for x, y, o in ((74, 46, "RED"), (78, 70, "RED"), (55, 30, "NEUTRAL"),
                    (44, 66, "NEUTRAL"), (52, 86, "NEUTRAL")):
        x, y = snap_to_land(grid, x, y)
        forts.append({"x": x, "y": y, "level": 2, "owner": o})
    return forts


# --------------------------------------------------------------------------- #
# 部署
# --------------------------------------------------------------------------- #
_TEMPLATE_DOMAINS: dict[str, str] = {}


def _load_template_domains() -> dict[str, str]:
    """读取兵种模板的域（land / air / sea / sub / infra / space）。"""
    import json as _json
    path = os.path.join(ROOT, "data", "units", "armory.json")
    with open(path, "r", encoding="utf-8") as f:
        doc = _json.load(f)
    return {u["id"]: u["domain"] for u in doc["units"]}


# 部署时需要吸附到陆地的兵种域
_GROUND_DOMAINS = {"land", "infra"}
# 需要吸附到水域的兵种域
_SEA_DOMAINS = {"sea", "sub"}


def build_deployments(grid: list[str]) -> dict[str, list[dict]]:
    """双方部署。

    蓝方 26+ 种兵种 / 40+ 个单位；红方 30+ 种兵种 / 44+ 个单位。
    空中单位与地面单位可同格——这正是 v1.5 新增的战术。
    """
    blue: list[dict] = []
    red: list[dict] = []
    # 兵种域查表（决定是否需要吸附到陆地）
    domains = _TEMPLATE_DOMAINS

    # 已占用的地面格（每格至多一支地面部队）
    taken_ground: set[tuple[int, int]] = set()
    taken_sea: set[tuple[int, int]] = set()
    taken_any: set[tuple[int, int]] = set()

    def add(side: list, tid: str, x: int, y: int, alt: str | None = None) -> None:
        dom = domains.get(tid)
        if dom in _GROUND_DOMAINS:
            x, y = snap_to_land(grid, x, y)
            # 该格已有地面部队 → 找邻近空位
            if (x, y) in taken_ground:
                x, y = _free_spot(grid, x, y, taken_ground, want_land=True)
            taken_ground.add((x, y))
        elif dom in _SEA_DOMAINS:
            x, y = snap_to_water(grid, x, y)
            # 水面舰每格至多一艘；潜艇可与水面舰同格（在水下）
            if dom == "sea":
                if (x, y) in taken_sea:
                    x, y = _free_spot(grid, x, y, taken_sea, want_land=False)
                taken_sea.add((x, y))
        taken_any.add((x, y))
        e = {"template": tid, "x": x, "y": y}
        if alt:
            e["altitude"] = alt
        side.append(e)

    # ---- 蓝方（美）----
    # 太空与预警
    add(blue, "sat_blue", 0, 0)
    add(blue, "e3", 16, 34, "high")
    add(blue, "mq9", 22, 46)
    add(blue, "mq9", 26, 60)
    # 空中力量
    add(blue, "f35", 18, 38, "high")
    add(blue, "f35", 18, 50, "high")
    add(blue, "f16c", 20, 42, "high")
    add(blue, "f16c", 24, 56, "high")
    add(blue, "f18e", 22, 48, "high")
    add(blue, "f15e", 26, 38, "high")
    add(blue, "b52", 30, 34, "high")
    # 直升机（与地面部队同格协同）
    add(blue, "ah64", 16, 40)
    add(blue, "ah64", 20, 52)
    add(blue, "s70", 14, 64)
    add(blue, "ch53", 18, 44)
    add(blue, "c130", 24, 40, "high")
    # 海军
    add(blue, "cvn", 8, 52)
    add(blue, "cvn", 8, 72)
    add(blue, "ddg_burke", 14, 46)
    add(blue, "ddg_burke", 14, 58)
    add(blue, "ddg_burke", 12, 66)
    add(blue, "llsn", 16, 50)
    add(blue, "ssn_va", 4, 44)
    add(blue, "ssn_va", 6, 68)
    # 地面：岛链前进基地
    add(blue, "m1a2", 38, 23)
    add(blue, "bradley", 39, 24)
    add(blue, "usmc", 37, 24)
    add(blue, "patriot", 48, 41)
    add(blue, "nasams", 40, 20)
    add(blue, "paladin", 38, 25)
    add(blue, "eng", 38, 26)
    # 地面：西太平洋基地
    add(blue, "m1a2", 12, 46)
    add(blue, "m1a2", 9, 53)
    add(blue, "bradley", 13, 47)
    add(blue, "himars", 11, 38)
    add(blue, "m142_pz", 15, 37)
    add(blue, "patriot", 8, 43)
    add(blue, "nasams", 16, 40)
    add(blue, "ew_us", 10, 48)
    add(blue, "radar_tpy2", 9, 31)
    add(blue, "usmc", 12, 42)
    add(blue, "hq_brigade", 13, 45)
    add(blue, "hq_battalion", 11, 45)
    add(blue, "ew_us", 14, 61)
    add(blue, "patriot", 12, 63)
    add(blue, "paladin", 13, 87)

    # ---- 红方（中）----
    # 太空与预警
    add(red, "sat_red", 127, 127)
    add(red, "a50", 96, 40, "high")
    add(red, "kj500", 100, 60, "high")
    # 空中力量
    add(red, "j20", 90, 44, "high")
    add(red, "j20", 92, 58, "high")
    add(red, "j16", 88, 50, "high")
    add(red, "j16", 94, 52, "high")
    add(red, "su35", 86, 46, "high")
    add(red, "su34", 98, 66, "high")
    add(red, "mig31k", 92, 36, "high")
    add(red, "tu22m3", 104, 46, "high")
    add(red, "b1b", 108, 62, "high")
    # 海军
    add(red, "type055", 76, 44)
    add(red, "type055", 74, 58)
    add(red, "type052d", 78, 50)
    add(red, "type054a", 76, 66)
    add(red, "slava", 80, 54)
    add(red, "kilo", 72, 48)
    add(red, "kilo", 72, 62)
    # 地面：岸线防御
    add(red, "t90m", 76, 47)
    add(red, "t90m", 77, 48)
    add(red, "bmp3", 78, 47)
    add(red, "naval_inf", 79, 48)
    add(red, "msta", 76, 49)
    add(red, "smerch", 74, 52)
    add(red, "s400", 80, 45)
    add(red, "pantsir", 82, 50)
    add(red, "hq17", 84, 48)
    add(red, "ew_ru", 81, 53)
    add(red, "radar_container", 78, 44)
    add(red, "msta", 84, 52)
    # 地面：纵深
    add(red, "t90m", 96, 57)
    add(red, "bmp3", 97, 58)
    add(red, "naval_inf", 98, 57)
    add(red, "msta", 96, 60)
    add(red, "smerch", 100, 62)
    add(red, "s400", 104, 45)
    add(red, "hq17", 100, 55)
    add(red, "pantsir", 92, 60)
    # 远程火力
    add(red, "df17_bde", 90, 44)
    add(red, "df26_bde", 100, 50)
    add(red, "df41_bde", 110, 40)
    add(red, "iskander", 86, 54)
    add(red, "thaad", 94, 48)
    add(red, "hq_battalion", 88, 52)
    add(red, "hq_brigade", 96, 54)
    # 北部与南部
    add(red, "t90m", 86, 21)
    add(red, "bmp3", 87, 22)
    add(red, "naval_inf", 88, 21)
    add(red, "s400", 83, 21)
    add(red, "msta", 90, 24)
    add(red, "t90m", 100, 99)
    add(red, "bmp3", 101, 100)
    add(red, "naval_inf", 102, 99)
    add(red, "s400", 96, 97)
    add(red, "hq_battalion", 89, 97)
    add(red, "pantsir", 106, 96)
    return {"BLUE": blue, "RED": red}


def build_events() -> list[dict]:
    """想定脚本事件：天候变化与增援。"""
    return [
        {"turn": 6, "type": "weather", "weather": "rain",
         "label": "台风外围雨幕", "description": "暴雨笼罩海峡，双方侦察与机动受限"},
        {"turn": 12, "type": "reinforce", "side": "RED",
         "unit": "t90m", "x": 96, "y": 57,
         "label": "红方增援：坦克连", "description": "红方预备队开赴东岸门户"},
        {"turn": 16, "type": "weather", "weather": "clear",
         "label": "雨过天晴", "description": "能见度恢复，双方重整旗鼓"},
        {"turn": 22, "type": "reinforce", "side": "BLUE",
         "unit": "ddg_burke", "x": 14, "y": 58,
         "label": "蓝方增援：驱逐舰", "description": "蓝方护航编队抵达西侧"},
        {"turn": 28, "type": "reinforce", "side": "RED",
         "unit": "j20", "x": 90, "y": 44,
         "label": "红方增援：战斗机", "description": "红方航空兵进驻前沿"},
    ]


def main() -> int:
    global _TEMPLATE_DOMAINS
    _TEMPLATE_DOMAINS = _load_template_domains()
    grid, elev = build_terrain()
    cities = add_cities(grid)
    facs = build_facilities(grid)
    forts = build_forts(facs, grid)
    objs = build_objectives(grid)
    deps = build_deployments(grid)

    doc = {
        "id": "strait_confrontation",
        "name": "海峡对决",
        "description": "架空时间线：中美在东亚大陆架海峡爆发全面战争。"
                       "美方自西太平洋展开反攻，中方依托大陆纵深与岸线防御体系据守。"
                       "中央岛链是双方必争的胜负手。",
        "size": [W, H],
        "tile_km": TILE_KM,
        "turn_limit": TURN_LIMIT,
        "terrain": grid,
        "elevation": {"rows": elev},
        "facilities": facs,
        "forts": forts,
        "objectives": objs,
        "deployments": deps,
        "events": build_events(),
        "meta": {
            "author": "hamfreebird",
            "summary": "128×128 大型战役想定：蓝方自西太平洋反攻，"
                       "红方依托大陆纵深据守，中央岛链决定制海权归属",
            "difficulty": "epic",
            "tags": ["大型战役", "海空一体", "岛链争夺", "中美对抗", "纵深打击"],
            "recommended_units": ["cvn", "f35", "j20", "df17_bde", "ah64", "type055"],
            "recommended_side": "BLUE",
            "version": "1.5",
        },
    }

    tmp = OUT + ".tmp"
    # newline="\n"：与仓库其它数据文件保持一致（LF），避免 CRLF 噪声
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)

    # 统计
    from collections import Counter
    tc = Counter("".join(grid))
    print(f"已生成 {OUT}")
    print(f"尺寸 {W}×{H}，每格 {TILE_KM} km，回合上限 {TURN_LIMIT}")
    print("地形分布:", " ".join(f"{k}={tc[k]}" for k in
                                 "wcpfhmuds" if tc[k]))
    print(f"城市格 {len(cities)}，设施 {len(facs)}，工事 {len(forts)}，"
          f"目标点 {len(objs)}")
    for side in ("BLUE", "RED"):
        tids = {d["template"] for d in deps[side]}
        print(f"{side}: {len(deps[side])} 个单位 / {len(tids)} 种兵种")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

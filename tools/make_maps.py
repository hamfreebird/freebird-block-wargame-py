"""想定地图生成工具（开发期使用，非游戏运行依赖）。

生成两张手工设计的作战地图并输出为 data/maps/*.json。
玩家可以直接编辑这些 JSON，也可以修改本脚本后重新生成。

用法： python tools/make_maps.py
"""
from __future__ import annotations

import json
import math
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "maps")


# --------------------------------------------------------------------------- #
def smooth_noise(w: int, h: int, seed: int, scale: float = 5.0) -> list[list[float]]:
    rnd = random.Random(seed)
    gw, gh = int(w / scale) + 3, int(h / scale) + 3
    grid = [[rnd.random() for _ in range(gw)] for _ in range(gh)]

    def at(ix: int, iy: int) -> float:
        return grid[max(0, min(gh - 1, iy))][max(0, min(gw - 1, ix))]

    out = []
    for y in range(h):
        row = []
        fy = y / scale
        iy = int(fy)
        ty = fy - iy
        ty = ty * ty * (3 - 2 * ty)
        for x in range(w):
            fx = x / scale
            ix = int(fx)
            tx = fx - ix
            tx = tx * tx * (3 - 2 * tx)
            v00, v10 = at(ix, iy), at(ix + 1, iy)
            v01, v11 = at(ix, iy + 1), at(ix + 1, iy + 1)
            top = v00 + (v10 - v00) * tx
            bot = v01 + (v11 - v01) * tx
            row.append(top + (bot - top) * ty)
        out.append(row)
    return out


def blob(cx: float, cy: float, rx: float, ry: float, x: int, y: int) -> float:
    """椭圆归一化距离：<1 表示在内部。"""
    return math.hypot((x - cx) / rx, (y - cy) / ry)


# --------------------------------------------------------------------------- #
def build_strait(w: int = 40, h: int = 40) -> dict:
    """海峡屏障：东西两大陆被海峡分隔，中部岛链为争夺焦点。"""
    n1 = smooth_noise(w, h, seed=11, scale=6.0)
    n2 = smooth_noise(w, h, seed=27, scale=3.0)
    terrain = [["w"] * w for _ in range(h)]
    elev = [[0] * w for _ in range(h)]

    for y in range(h):
        west = 11.5 + 3.2 * math.sin(y / 5.2) + 1.4 * math.sin(y / 2.1 + 0.7)
        east = 28.5 - 3.0 * math.sin(y / 6.1 + 1.3) - 1.3 * math.cos(y / 1.8)
        for x in range(w):
            n = n1[y][x] * 0.7 + n2[y][x] * 0.3
            if x < west + (n - 0.5) * 2.2:
                terrain[y][x] = "c" if x > west - 1.6 else "p"
            elif x > east + (n - 0.5) * 2.2:
                terrain[y][x] = "c" if x < east + 1.6 else "p"

    # 中部岛链
    islands = [(19.5, 13.0, 2.6, 2.0), (21.0, 20.5, 2.2, 2.6), (18.0, 27.5, 2.8, 1.9),
               (23.5, 33.0, 2.0, 1.6), (17.0, 7.0, 1.8, 1.5)]
    for (cx, cy, rx, ry) in islands:
        for y in range(h):
            for x in range(w):
                if x < 10 or x > 30:
                    continue
                d = blob(cx, cy, rx, ry, x, y)
                if d < 1.0:
                    terrain[y][x] = "p" if d < 0.72 else "c"

    # 西北山地 / 丘陵带
    for y in range(h):
        for x in range(w):
            if terrain[y][x] in ("p", "f", "h", "m"):
                continue
            if terrain[y][x] != "p":
                continue
            d = blob(4.0, 5.0, 6.5, 6.0, x, y)
            if d < 0.8:
                terrain[y][x] = "m" if d < 0.45 else "h"
            elif d < 1.15 and n2[y][x] > 0.55:
                terrain[y][x] = "h"
    # 东部丘陵
    for y in range(h):
        for x in range(w):
            if terrain[y][x] != "p":
                continue
            if x > 33:
                d = blob(37.0, 12.0, 5.0, 7.0, x, y)
                if d < 0.7:
                    terrain[y][x] = "m" if d < 0.35 else "h"
    # 林地
    for y in range(h):
        for x in range(w):
            if terrain[y][x] == "p" and n1[y][x] > 0.62 and n2[y][x] > 0.5:
                terrain[y][x] = "f"
    # 沼泽
    for y in range(h):
        for x in range(w):
            if terrain[y][x] == "p" and 24 < y < 33 and 6 < x < 11 and n2[y][x] > 0.55:
                terrain[y][x] = "s"

    # 城区
    cities = [(8, 19, 2.2), (6, 11, 1.7), (31, 21, 2.2), (34, 29, 1.8), (19, 20, 1.4)]
    for (cx, cy, r) in cities:
        for y in range(max(0, cy - 2), min(h, cy + 3)):
            for x in range(max(0, cx - 2), min(w, cx + 3)):
                if blob(cx, cy, r, r, x, y) < 1.0 and terrain[y][x] in ("p", "c", "f", "h"):
                    terrain[y][x] = "u"

    # 海拔
    base = {"w": 0, "c": 1, "p": 2, "f": 3, "h": 5, "m": 8, "u": 2, "s": 1, "d": 4}
    for y in range(h):
        for x in range(w):
            t = terrain[y][x]
            if t == "w":
                elev[y][x] = 0
                continue
            v = base[t] + int(n2[y][x] * 1.6)
            elev[y][x] = max(0, min(9, v))

    # 中央锚地：开凿为可通航水域，供水面舰艇争夺（陆上单位无法进入）
    terrain[20][21] = "w"
    elev[20][21] = 0

    return {
        "id": "strait_shield",
        "name": "海峡屏障 · 2027",
        "description": "蓝方第 3 舰队依托西岸基地群阻止红方突破海峡；红方以 S-400 与岸基反舰导弹构建拒止区。"
                       "制电磁权与制空权决定海峡控制权。",
        "size": [w, h],
        "tile_km": 12,
        "turn_limit": 18,
        "terrain": ["".join(r) for r in terrain],
        "elevation": {"rows": ["".join(str(v) for v in r) for r in elev]},
        "facilities": [
            {"x": 8, "y": 19, "facility": "port", "owner": "BLUE", "hp": 110},
            {"x": 6, "y": 11, "facility": "airbase", "owner": "BLUE", "hp": 120},
            {"x": 5, "y": 14, "facility": "radar_site", "owner": "BLUE", "hp": 80},
            {"x": 7, "y": 25, "facility": "depot", "owner": "BLUE", "hp": 90},
            {"x": 31, "y": 21, "facility": "port", "owner": "RED", "hp": 110},
            {"x": 33, "y": 12, "facility": "airbase", "owner": "RED", "hp": 120},
            {"x": 35, "y": 14, "facility": "radar_site", "owner": "RED", "hp": 80},
            {"x": 33, "y": 24, "facility": "sam_site", "owner": "RED", "hp": 90},
        ],
        "forts": [
            {"x": 8, "y": 19, "level": 2, "owner": "BLUE"},
            {"x": 6, "y": 11, "level": 2, "owner": "BLUE"},
            {"x": 9, "y": 18, "level": 1, "owner": "BLUE"},
            {"x": 7, "y": 25, "level": 1, "owner": "BLUE"},
            {"x": 31, "y": 21, "level": 2, "owner": "RED"},
            {"x": 34, "y": 29, "level": 2, "owner": "RED"},
            {"x": 32, "y": 22, "level": 1, "owner": "RED"},
            {"x": 33, "y": 23, "level": 1, "owner": "RED"},
        ],
        "objectives": [
            {"x": 8, "y": 19, "name": "西港", "kind": "hold", "side": "BLUE", "value": 3, "owner": "BLUE"},
            {"x": 6, "y": 11, "name": "西岸空军基地", "kind": "hold", "side": "BLUE", "value": 3, "owner": "BLUE"},
            {"x": 31, "y": 21, "name": "东港", "kind": "capture", "side": "RED", "value": 4, "owner": "RED"},
            {"x": 34, "y": 29, "name": "东岸空军基地", "kind": "capture", "side": "RED", "value": 3, "owner": "RED"},
            {"x": 19, "y": 13, "name": "北岛雷达高地", "kind": "capture", "side": "NEUTRAL", "value": 5, "owner": "NEUTRAL"},
            {"x": 21, "y": 20, "name": "中央锚地", "kind": "capture", "side": "NEUTRAL", "value": 5, "owner": "NEUTRAL"},
            {"x": 18, "y": 28, "name": "南岛机场", "kind": "capture", "side": "NEUTRAL", "value": 4, "owner": "NEUTRAL"},
        ],
        "deployments": {
            "BLUE": [
                {"template": "sat_blue", "x": 0, "y": 0},
                {"template": "e3", "x": 4, "y": 4, "altitude": "high"},
                {"template": "f35", "x": 6, "y": 11, "altitude": "med"},
                {"template": "f15e", "x": 5, "y": 9, "altitude": "med"},
                {"template": "b1b", "x": 6, "y": 11, "altitude": "med"},
                {"template": "mq9", "x": 10, "y": 13, "altitude": "low"},
                {"template": "radar_tpy2", "x": 5, "y": 14},
                {"template": "m1a2", "x": 9, "y": 18},
                {"template": "m1a2", "x": 10, "y": 19},
                {"template": "inf_motor", "x": 11, "y": 20},
                {"template": "bradley", "x": 10, "y": 19},
                {"template": "usmc", "x": 10, "y": 17},
                {"template": "inf_light", "x": 11, "y": 18},
                {"template": "paladin", "x": 8, "y": 21},
                {"template": "himars", "x": 7, "y": 23},
                {"template": "patriot", "x": 10, "y": 16},
                {"template": "nasams", "x": 9, "y": 21},
                {"template": "ew_us", "x": 6, "y": 24},
                {"template": "ddg_burke", "x": 14, "y": 20},
                {"template": "cvn", "x": 15, "y": 26},
                {"template": "ssn_va", "x": 18, "y": 33},
            ],
            "RED": [
                {"template": "sat_red", "x": 39, "y": 0},
                {"template": "a50", "x": 36, "y": 6, "altitude": "high"},
                {"template": "su35", "x": 35, "y": 11, "altitude": "med"},
                {"template": "su34", "x": 36, "y": 9, "altitude": "med"},
                {"template": "mig31k", "x": 34, "y": 13, "altitude": "high"},
                {"template": "tu22m3", "x": 37, "y": 8, "altitude": "high"},
                {"template": "radar_container", "x": 35, "y": 14},
                {"template": "s400", "x": 33, "y": 17},
                {"template": "pantsir", "x": 32, "y": 22},
                {"template": "t90m", "x": 30, "y": 20},
                {"template": "bmp3", "x": 31, "y": 23},
                {"template": "naval_inf", "x": 30, "y": 24},
                {"template": "inf_motor", "x": 31, "y": 22},
                {"template": "msta", "x": 33, "y": 19},
                {"template": "smerch", "x": 34, "y": 22},
                {"template": "iskander", "x": 34, "y": 25},
                {"template": "ew_ru", "x": 33, "y": 26},
                {"template": "slava", "x": 26, "y": 18},
                {"template": "kilo", "x": 24, "y": 30},
            ],
        },
    }


def build_steppe(w: int = 36, h: int = 36) -> dict:
    """草原雷霆：纯陆战想定，河流分割战场，检验合成兵种与火力协同。"""
    n1 = smooth_noise(w, h, seed=41, scale=6.5)
    n2 = smooth_noise(w, h, seed=93, scale=3.2)
    terrain = [["p"] * w for _ in range(h)]
    elev = [[0] * w for _ in range(h)]

    for y in range(h):
        for x in range(w):
            terrain[y][x] = "p"

    # 河流（南北向，弯曲）
    for y in range(h):
        rx = 17 + 2.6 * math.sin(y / 4.5) + 1.1 * math.sin(y / 1.7)
        for dx in (-1, 0):
            x = int(rx) + dx
            if 0 <= x < w:
                terrain[y][x] = "w"
        if 0 <= int(rx) + 1 < w:
            terrain[y][int(rx) + 1] = "s"

    # 渡口：在河上开出三处可通行的浅滩，避免战场被河流完全割裂
    for fy in (7, 18, 29):
        rx = int(17 + 2.6 * math.sin(fy / 4.5) + 1.1 * math.sin(fy / 1.7))
        for dx in (-1, 0, 1):
            x = rx + dx
            if 0 <= x < w:
                terrain[fy][x] = "c"

    # 北部丘陵 / 山地
    for y in range(h):
        for x in range(w):
            d = blob(9.0, 5.0, 9.0, 5.5, x, y)
            if d < 0.75:
                terrain[y][x] = "h" if terrain[y][x] == "p" else terrain[y][x]
            if d < 0.32:
                terrain[y][x] = "m"
            d2 = blob(30.0, 30.0, 8.0, 6.5, x, y)
            if d2 < 0.6 and terrain[y][x] == "p":
                terrain[y][x] = "h"
            if d2 < 0.25:
                terrain[y][x] = "m"

    # 林地与荒漠
    for y in range(h):
        for x in range(w):
            if terrain[y][x] != "p":
                continue
            if n1[y][x] > 0.63 and n2[y][x] > 0.45:
                terrain[y][x] = "f"
            elif n1[y][x] < 0.28:
                terrain[y][x] = "d"

    # 城镇
    cities = [(11, 22, 2.2), (25, 13, 2.0), (27, 24, 1.7), (8, 14, 1.5)]
    for (cx, cy, r) in cities:
        for y in range(max(0, cy - 2), min(h, cy + 3)):
            for x in range(max(0, cx - 2), min(w, cx + 3)):
                if blob(cx, cy, r, r, x, y) < 1.0 and terrain[y][x] in ("p", "f", "h", "d", "s"):
                    terrain[y][x] = "u"

    base = {"w": 0, "c": 1, "p": 2, "f": 3, "h": 5, "m": 8, "u": 2, "s": 1, "d": 3}
    for y in range(h):
        for x in range(w):
            t = terrain[y][x]
            if t == "w":
                elev[y][x] = 0
                continue
            elev[y][x] = max(0, min(9, base[t] + int(n2[y][x] * 1.5)))

    return {
        "id": "steppe_thunder",
        "name": "草原雷霆 · 2027",
        "description": "红方装甲集群沿河谷向北突贯，蓝方以远程火力与武装无人机迟滞其进攻。"
                       "开阔地形使侦察与反炮兵成为胜负关键。",
        "size": [w, h],
        "tile_km": 12,
        "turn_limit": 16,
        "terrain": ["".join(r) for r in terrain],
        "elevation": {"rows": ["".join(str(v) for v in r) for r in elev]},
        "facilities": [
            {"x": 11, "y": 22, "facility": "city", "owner": "BLUE", "hp": 100},
            {"x": 8, "y": 14, "facility": "depot", "owner": "BLUE", "hp": 90},
            {"x": 7, "y": 15, "facility": "airbase", "owner": "BLUE", "hp": 120},
            {"x": 25, "y": 13, "facility": "city", "owner": "RED", "hp": 100},
            {"x": 27, "y": 24, "facility": "hq", "owner": "RED", "hp": 100},
            {"x": 30, "y": 11, "facility": "airbase", "owner": "RED", "hp": 120},
        ],
        "forts": [
            {"x": 11, "y": 22, "level": 2, "owner": "BLUE"},
            {"x": 8, "y": 14, "level": 2, "owner": "BLUE"},
            {"x": 17, "y": 18, "level": 1, "owner": "BLUE"},
            {"x": 25, "y": 13, "level": 2, "owner": "RED"},
            {"x": 27, "y": 24, "level": 1, "owner": "RED"},
            {"x": 18, "y": 17, "level": 1, "owner": "RED"},
            {"x": 19, "y": 18, "level": 1, "owner": "RED"},
        ],
        "objectives": [
            {"x": 11, "y": 22, "name": "河谷城", "kind": "hold", "side": "BLUE", "value": 4, "owner": "BLUE"},
            {"x": 25, "y": 13, "name": "北岸枢纽", "kind": "capture", "side": "RED", "value": 4, "owner": "RED"},
            {"x": 18, "y": 18, "name": "河川渡口", "kind": "capture", "side": "NEUTRAL", "value": 6, "owner": "NEUTRAL"},
            {"x": 8, "y": 14, "name": "蓝方后勤枢纽", "kind": "hold", "side": "BLUE", "value": 3, "owner": "BLUE"},
        ],
        "deployments": {
            "BLUE": [
                {"template": "m1a2", "x": 9, "y": 20},
                {"template": "m1a2", "x": 10, "y": 24},
                {"template": "bradley", "x": 12, "y": 22},
                {"template": "inf_light", "x": 11, "y": 21},
                {"template": "usmc", "x": 13, "y": 24},
                {"template": "paladin", "x": 8, "y": 18},
                {"template": "himars", "x": 7, "y": 22},
                {"template": "nasams", "x": 11, "y": 19},
                {"template": "patriot", "x": 10, "y": 17},
                {"template": "thaad", "x": 9, "y": 16},
                {"template": "ew_us", "x": 6, "y": 16},
                {"template": "mq9", "x": 13, "y": 16, "altitude": "low"},
                {"template": "f15e", "x": 7, "y": 15, "altitude": "med"},
                {"template": "f35", "x": 7, "y": 15, "altitude": "med"},
                {"template": "sat_blue", "x": 0, "y": 0},
            ],
            "RED": [
                {"template": "t90m", "x": 25, "y": 18},
                {"template": "t90m", "x": 26, "y": 16},
                {"template": "bmp3", "x": 25, "y": 20},
                {"template": "inf_motor", "x": 24, "y": 19},
                {"template": "naval_inf", "x": 22, "y": 16},
                {"template": "msta", "x": 28, "y": 18},
                {"template": "smerch", "x": 29, "y": 16},
                {"template": "pantsir", "x": 27, "y": 21},
                {"template": "ew_ru", "x": 29, "y": 22},
                {"template": "iskander", "x": 30, "y": 20},
                {"template": "s400", "x": 28, "y": 16},
                {"template": "su34", "x": 30, "y": 11, "altitude": "med"},
                {"template": "sat_red", "x": 35, "y": 0},
            ],
        },
    }


# --------------------------------------------------------------------------- #
def snap_deployments(spec: dict, armory_path: str) -> None:
    """把落在非法地形上的单位吸附到最近可部署地块，并解决同格冲突。"""
    w, h = spec["size"]
    rows = spec["terrain"]
    with open(armory_path, "r", encoding="utf-8") as f:
        tpl = {u["id"]: u for u in json.load(f)["units"]}

    def legal(x: int, y: int, domain: str) -> bool:
        if not (0 <= x < w and 0 <= y < h):
            return False
        c = rows[y][x]
        if domain in ("sea", "sub"):
            return c in "wc"
        if domain == "space":
            return True
        return c != "w"

    occupied: set[tuple[int, int]] = set()
    for side, entries in spec["deployments"].items():
        for e in entries:
            domain = tpl[e["template"]]["domain"]
            x, y = e["x"], e["y"]
            if not legal(x, y, domain) or ((x, y) in occupied and domain != "space"):
                best = None
                for r in range(1, max(w, h)):
                    for dy in range(-r, r + 1):
                        for dx in range(-r, r + 1):
                            nx, ny = x + dx, y + dy
                            if not legal(nx, ny, domain):
                                continue
                            if (nx, ny) in occupied and domain != "space":
                                continue
                            d = dx * dx + dy * dy
                            if best is None or d < best[0]:
                                best = (d, nx, ny)
                    if best:
                        break
                if best is None:
                    raise SystemExit(f"无法为 {side} {e['template']} 找到部署位置")
                e["x"], e["y"] = best[1], best[2]
            occupied.add((e["x"], e["y"]))


def validate(spec: dict, armory_path: str) -> None:
    w, h = spec["size"]
    rows = spec["terrain"]
    assert len(rows) == h and all(len(r) == w for r in rows)
    with open(armory_path, "r", encoding="utf-8") as f:
        armory = json.load(f)
    tpl = {u["id"]: u for u in armory["units"]}

    occupied = set()
    for side, entries in spec["deployments"].items():
        for e in entries:
            t = tpl[e["template"]]
            x, y = e["x"], e["y"]
            c = rows[y][x]
            if t["domain"] in ("land", "infra", "space"):
                if c == "w" and t["domain"] != "space":
                    raise SystemExit(f"部署错误: {side} {t['id']} 位于海域 ({x},{y})")
            if t["domain"] in ("sea", "sub"):
                if c not in "wc":
                    raise SystemExit(f"部署错误: {side} {t['id']} 位于陆上 ({x},{y}) c={c}")
            if (x, y) in occupied and t["domain"] != "space":
                raise SystemExit(f"部署冲突: {side} {t['id']} 重复占用 ({x},{y})")
            occupied.add((x, y))

    for f in spec.get("forts", []):
        assert rows[f["y"]][f["x"]] != "w", f"工事 {f} 位于海域"

    for o in spec["objectives"]:
        # 目标点允许位于海域（海权争夺点），但必须至少能被某一方抵达
        if rows[o["y"]][o["x"]] == "w":
            assert any(t["domain"] in ("sea", "sub") for t in tpl.values()), \
                f"目标点 {o['name']} 位于海域，但双方均无海上力量"


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    armory = os.path.join(ROOT, "data", "units", "armory.json")
    for spec in (build_strait(), build_steppe()):
        snap_deployments(spec, armory)
        validate(spec, armory)
        path = os.path.join(OUT, f"{spec['id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
        print(f"写出 {path}  ({spec['size'][0]}x{spec['size'][1]}, "
              f"{len(spec['deployments']['BLUE'])}v{len(spec['deployments']['RED'])})")


if __name__ == "__main__":
    main()

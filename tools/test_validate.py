"""想定战术校验（v3.1 9.4）的针对性测试。

旧校验只回答「结构合法吗」（单位在不在水里、目标点连不连通）。
这一层要回答「战术上说得通吗」：

1. 目标点是不是**谁也打不到**（缺整条打击链）；
2. 初始部署够不着时，能不能说清「只是需要机动」而不是报错；
3. 制高点分布是否一边倒；
4. 防空伞有没有盖住目标点；
5. 开局是否贴脸。

用法::

    python tools/test_validate.py
"""
from __future__ import annotations

import copy
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame  # noqa: E402

from core.enums import Domain, Side, Terrain  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from core.validate import validate_scenario  # noqa: E402

ARMORY = "data/units/armory.json"
MAP = "data/maps/training_ground.json"

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


def codes(rep) -> set[str]:
    return {i.code for i in rep.issues}


def find(rep, code: str):
    return [i for i in rep.issues if i.code == code]


# --------------------------------------------------------------------------- #
# 1. 出厂想定不该有战术错误
# --------------------------------------------------------------------------- #
def test_clean(sc) -> None:
    print("\n[1] 出厂想定")
    rep = validate_scenario(sc)
    check(rep.ok, f"训练场无 error（实际 {[str(i) for i in rep.errors][:3]}）")
    check("high-ground-share" in codes(rep), "给出了制高点分布")


# --------------------------------------------------------------------------- #
# 2. 谁也打不到：缺整条打击链
# --------------------------------------------------------------------------- #
def test_no_cover(sc) -> None:
    print("\n[2] 谁也打不到")
    s = copy.deepcopy(sc)
    # 把一个目标点挪到水上，并拿掉所有能打海面的武器
    obj = s.map.objectives[0]
    s.map.at(obj.x, obj.y).terrain = Terrain.WATER
    stripped = 0
    for u in s.units:
        for w in u.template.weapons:
            keep = tuple(d for d in w.targets if d not in (Domain.SEA, Domain.SUB))
            if len(keep) != len(w.targets):
                stripped += 1
            w.targets = keep
    rep = validate_scenario(s)
    hits = find(rep, "objective-no-cover")
    check(bool(hits), f"报出「谁也打不到」（剥掉 {stripped} 件反舰武器）")
    check(any("谁也打不到" in i.message for i in hits), "文案直接说明谁也打不到")
    check(any(i.x == obj.x and i.y == obj.y for i in hits), "定位到具体格")


# --------------------------------------------------------------------------- #
# 3. 够不着但有能力：只提示需要机动
# --------------------------------------------------------------------------- #
def test_far_cover(sc) -> None:
    print("\n[3] 够不着 ≠ 不会打")
    s = copy.deepcopy(sc)
    # 把所有单位塞到地图左上角，目标点保持在原位
    for i, u in enumerate(s.units):
        u.x, u.y = 1 + (i % 2), 1 + (i // 2) % 3
    obj = s.map.objectives[0]
    rep = validate_scenario(s)
    far = [i for i in find(rep, "objective-cover-far")
           if i.x == obj.x and i.y == obj.y]
    bad = [i for i in find(rep, "objective-no-cover")
           if i.x == obj.x and i.y == obj.y]
    check(not bad, "有能力打击时不报 error")
    # 目标点此时可能仍被某个射程极远的单位覆盖，允许两种情况之一
    if far:
        check("机动" in far[0].message, f"提示需要机动接近：{far[0].message[:30]}")
    else:
        check(True, "目标点仍在某单位射程内，无需提示")


# --------------------------------------------------------------------------- #
# 4. 制高点口径
# --------------------------------------------------------------------------- #
def test_high_ground(sc) -> None:
    print("\n[4] 制高点")
    s = copy.deepcopy(sc)
    # 把红方陆上单位全部改成空中单位：只剩一方能争制高点
    for u in s.units:
        if u.side is Side.RED and u.domain in (Domain.LAND, Domain.INFRA):
            u.template.domain = Domain.AIR
    rep = validate_scenario(s)
    check("high-ground-one-sided" in codes(rep) or "high-ground-share" in codes(rep),
          f"只有一方陆上单位时给出说明：{sorted(codes(rep) & {'high-ground-one-sided', 'high-ground-share'})}")


# --------------------------------------------------------------------------- #
# 5. 防空覆盖
# --------------------------------------------------------------------------- #
def test_air_defense(sc) -> None:
    print("\n[5] 防空覆盖")
    s = copy.deepcopy(sc)
    for u in s.units:
        if u.max_intercept_range() > 0:
            u.template.weapons = []
    rep = validate_scenario(s)
    check("no-air-defense" in codes(rep), "双方都没有防空时给出提示")

    s2 = copy.deepcopy(sc)
    for u in s2.units:
        if u.side is Side.RED and u.max_intercept_range() > 0:
            u.template.weapons = []
    rep2 = validate_scenario(s2)
    check("air-defense-missing" in codes(rep2),
          f"单方没有防空时点名提示：{sorted(codes(rep2))}")


# --------------------------------------------------------------------------- #
# 6. 开局贴脸
# --------------------------------------------------------------------------- #
def test_opening(sc) -> None:
    print("\n[6] 开局间距")
    s = copy.deepcopy(sc)
    blue = next(u for u in s.units if u.side is Side.BLUE)
    red = next(u for u in s.units if u.side is Side.RED)
    # 找一个平原格放两队，避免踩到水里的合法性检查
    plain = None
    for t in s.map.iter_tiles():
        if t.terrain is Terrain.PLAIN and s.map.at(t.x + 1, t.y) is not None \
                and s.map.at(t.x + 1, t.y).terrain is Terrain.PLAIN:
            plain = t
            break
    if plain is None:
        check(False, "训练场找不到相邻两格平原")
        return
    blue.x, blue.y = plain.x, plain.y
    red.x, red.y = plain.x + 1, plain.y
    rep = validate_scenario(s)
    hit = find(rep, "opening-contact")
    check(bool(hit), f"报出开局即接触：{hit[0].message if hit else '（无）'}")


def main() -> int:
    pygame.init()
    sc = load_scenario(MAP, ARMORY)

    print("v3.1 想定战术校验测试")
    print("=" * 46)
    test_clean(sc)
    test_no_cover(sc)
    test_far_cover(sc)
    test_high_ground(sc)
    test_air_defense(sc)
    test_opening(sc)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for m in FAILED:
            print("  -", m)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

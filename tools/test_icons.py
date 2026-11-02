"""v2.1 专项自检：单位图标包。

覆盖：
* 图标包清单的读取与校验；
* 位图加载、着色与缓存；
* 缺失兵种 / 缺失文件 / 损坏包的降级行为；
* 图标包切换后缓存正确失效。

用法：python tools/test_icons.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402

from core import icons  # noqa: E402
from core.enums import UnitClass  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def main() -> int:
    pygame.init()
    pygame.display.set_mode((320, 240))

    print("=== 图标包自检 ===")

    # -- 清单读取 --
    packs = icons.list_packs()
    check(len(packs) >= 1, f"至少能找到一个图标包（找到 {len(packs)} 个）")
    ids = {p.id for p in packs}
    check("default" in ids, "内置图标包存在")

    default = next((p for p in packs if p.id == "default"), None)
    check(default is not None, "取得内置图标包对象")
    if default is not None:
        check(len(default.icons) == len(list(UnitClass)),
              f"内置包覆盖全部兵种（{len(default.icons)}/{len(list(UnitClass))}）")
        check(default.name != "", "内置包有显示名")
        path = default.icon_path(UnitClass.MBT)
        check(path is not None and os.path.isfile(path), "兵种图标文件存在")

    # -- 位图加载 --
    icons.set_current_pack("default")
    check(icons.current_pack_id() == "default", "已切换到内置图标包")
    bmp = icons.icon_bitmap(UnitClass.MBT, (200, 220, 255), 32)
    check(bmp is not None, "能取到坦克图标位图")
    if bmp is not None:
        check(bmp.get_width() == 32 and bmp.get_height() == 32, "位图尺寸为 32×32")
        # 图标以白色绘制、按目标色着色——检查存在非透明像素
        has_ink = False
        for x in range(0, 32, 2):
            for y in range(0, 32, 2):
                if bmp.get_at((x, y))[3] > 0:
                    has_ink = True
                    break
            if has_ink:
                break
        check(has_ink, "图标含可见像素（不是全透明）")

    # -- 缓存复用 --
    bmp2 = icons.icon_bitmap(UnitClass.MBT, (200, 220, 255), 32)
    check(bmp2 is bmp, "同参数二次取用命中缓存（同一对象）")
    bmp3 = icons.icon_bitmap(UnitClass.MBT, (255, 100, 100), 32)
    check(bmp3 is not bmp, "不同颜色取到不同位图")

    # -- 切回内置矢量 --
    icons.set_current_pack("")
    check(icons.current_pack_id() == "", "已切回内置矢量模式")
    check(icons.icon_bitmap(UnitClass.MBT, (200, 220, 255), 32) is None,
          "矢量模式下不返回位图（由渲染层回退）")

    # -- 损坏的包不影响启动 --
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "broken_pack")
        os.makedirs(bad)
        with open(os.path.join(bad, "pack.json"), "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON ")
        try:
            icons._load_pack_from(bad)
            check(False, "损坏的包应当抛出 IconPackError")
        except icons.IconPackError:
            check(True, "损坏的包抛出 IconPackError（不崩溃）")

        # -- 缺失清单 --
        empty = os.path.join(td, "no_manifest")
        os.makedirs(empty)
        try:
            icons._load_pack_from(empty)
            check(False, "缺少 pack.json 应当抛出 IconPackError")
        except icons.IconPackError:
            check(True, "缺少 pack.json 抛出 IconPackError")

        # -- 未知兵种被忽略 --
        good = os.path.join(td, "weird_pack")
        os.makedirs(good)
        with open(os.path.join(good, "pack.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "weird", "name": "怪包",
                       "icons": {"mbt": "a.png", "not_a_class": "b.png"}}, f)
        p = icons._load_pack_from(good)
        check("not_a_class" not in p.icons, "未知兵种被忽略")
        check("mbt" in p.icons, "已知兵种被保留")
        check(p.icon_path(UnitClass.MBT) is None, "图标文件缺失时返回 None（回退矢量）")

    # -- 不存在的包 --
    check(icons.find_pack("no_such_pack_xyz") is None, "查找不存在的包返回 None")

    icons.set_current_pack("")
    pygame.quit()

    if FAILS:
        print(f"\n图标包自检失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("\n图标包自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

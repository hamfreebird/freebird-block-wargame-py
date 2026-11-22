"""视觉回归自检（v3.5 A 层）。

**要守住的性质**：

1. **地形材质真实存在**——纹理接错、振幅归零、取样越界，都会让地形
   退回纯色块。用"地块区域的亮度标准差"作为客观指标：纯色块的标准差
   接近 0，有材质时至少两位数。这比截图比对稳健——截图会因平台字体、
   GL 驱动差异误报，而色差统计只看我们自己画的像素。
2. **四套主题都能带材质渲染**——主题是运行时切换的，某套主题的
   地形色若与纹理冲突（比如亮色主题下纹理对比被冲淡），必须能发现。
3. **单位立体效果只在够大时启用**——20px 以下加描边会糊成一团，
   这是刻意设的门槛，需要防止后人误删。
4. **纹理缓存只生成一次**——重复生成会让开局卡顿。

用法：python tools/test_visual.py
"""
from __future__ import annotations

import os
import statistics
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("BLOCKWARGAME_NO_AUDIO", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pygame  # noqa: E402

pygame.init()
pygame.display.set_mode((64, 64))

from core.engine import GameState  # noqa: E402
from core.enums import Side, Terrain, UnitClass  # noqa: E402
from core.loader import load_scenario  # noqa: E402
from ui import terrain_tex as TEX  # noqa: E402
from ui import theme as T  # noqa: E402
from ui.mapview import MapView  # noqa: E402

ARMORY = os.path.join(ROOT, "data", "units", "armory.json")
MAP = os.path.join(ROOT, "data", "maps", "strait_confrontation.json")

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [OK] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def _luma_stats(surf: pygame.Surface, rect: pygame.Rect) -> tuple[float, float]:
    """区域内亮度的均值与标准差（抽样，够用且快）。"""
    px = pygame.image.tobytes(surf, "RGB")
    w = surf.get_width()
    vals: list[float] = []
    step = 3
    for y in range(rect.top, rect.bottom, step):
        base = y * w * 3
        for x in range(rect.left, rect.right, step):
            i = base + x * 3
            vals.append(0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2])
    if not vals:
        return 0.0, 0.0
    return statistics.mean(vals), statistics.pstdev(vals)


def test_texture_fields() -> None:
    print("\n--- 纹理生成 ---")
    texs = TEX.textures()
    check(len(texs) == len(list(Terrain)), f"九种地形都有纹理（{len(texs)}）")
    for t, tex in texs.items():
        vals = [pygame.image.tobytes(tex, "RGB")[i] for i in range(0, tex.get_width() * tex.get_height() * 3, 3 * 11)]
        spread = max(vals) - min(vals)
        check(spread >= 20, f"{t.value} 纹理有足够对比度（{spread} 级）")
    light, dark = next(iter(TEX.offsets().values()))
    check(light.get_width() == TEX.TEX_SIZE * 2,
          "偏移层已平铺 2×2（防止取样越界）")


def test_terrain_has_material() -> None:
    print("\n--- 地形材质实际生效 ---")
    surf = pygame.display.set_mode((900, 700))
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=2)
    st.refresh_detection()
    for tid in ("tactical", "light", "deuteranopia", "protanopia"):
        T.set_theme(tid)
        mv = MapView(pygame.Rect(0, 0, 900, 700), st)
        mv.tile = 26
        mv.draw(surf)
        _, sd = _luma_stats(surf, pygame.Rect(60, 60, 300, 300))
        # 纯色块的标准差通常 < 3；带材质应在 6 以上
        check(sd >= 6.0, f"{tid}：地形区域有材质纹理（标准差 {sd:.1f}）")
    T.set_theme("tactical")


def test_unit_icon_stages() -> None:
    print("\n--- 单位图标立体门槛 ---")
    from ui.mapview import draw_vector_class_icon
    col = T.side_color(Side.BLUE, "edge")
    a = pygame.Surface((30, 30), pygame.SRCALPHA)
    draw_vector_class_icon(a, pygame.Rect(0, 0, 30, 30), UnitClass.MBT, col)
    check(a.get_width() == 30, "直接绘制得到 30px 平面图（对照组）")

    mv = MapView(pygame.Rect(0, 0, 10, 10),
                 GameState(load_scenario(MAP, ARMORY), Side.BLUE))
    small = mv._vector_icon_bitmap(UnitClass.MBT, col, 16)
    big = mv._vector_icon_bitmap(UnitClass.MBT, col, 26)
    check(small is not None and small.get_width() == 16,
          "16px 图标不加描边（保持平面，避免糊成一团）")
    check(big is not None and big.get_width() == 30,
          "26px 图标带 2px 描边留白（30 = 26 + 4）")


def test_texture_cache() -> None:
    print("\n--- 纹理缓存 ---")
    TEX._TEX_SIZE_CACHE = None
    TEX._TEXTURES.clear()
    TEX._OFFSETS.clear()
    import time
    t0 = time.perf_counter()
    TEX.offsets()
    first = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    TEX.offsets()
    again = (time.perf_counter() - t0) * 1000
    check(again < 1.0, f"二次取用命中缓存（{again:.2f} ms，首次 {first:.0f} ms）")
    check(first < 600, f"首次生成在可接受范围（{first:.0f} ms < 600）")


def main() -> int:
    print("视觉回归自检（v3.5 A 层）")
    test_texture_fields()
    test_terrain_has_material()
    test_unit_icon_stages()
    test_texture_cache()

    print("\n" + "=" * 46)
    if FAILS:
        print(f"✗ {len(FAILS)} 条断言未通过：")
        for m in FAILS:
            print("   -", m)
        return 1
    print("✓ 视觉回归自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

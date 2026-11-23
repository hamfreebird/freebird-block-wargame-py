"""视觉回归自检（v3.5 A 层）。

**要守住的性质**：

1. **地形明暗仍然分层**——v3.5.1 起地形回到纯色块（贴图观感不达标，
   已按实测反馈回退），但海拔明度与坡向明暗必须仍在，否则地形会退化成
   一张平坦的单色图。用"地块区域的亮度标准差"作客观指标：完全平铺的
   单色标准差接近 0，有起伏时在两位数。
2. **四套主题都能正常渲染**——主题运行时切换，某套主题的令牌若缺失或
   冲突必须能发现。
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
    # 地形已回退为纯色块：只校验九种地形都有配色，不再校验纹理
    colors = T.TERRAIN_COLORS
    check(len(colors) == len(list(Terrain)), f"九种地形都有配色（{len(colors)}）")


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
        # 海拔明度 + 坡向明暗 + 装饰仍在时标准差 > 4；
        # 完全平铺的单色图会接近 0
        check(sd >= 4.0, f"{tid}：地形仍有明暗层次（标准差 {sd:.1f}）")
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


def test_city_glow() -> None:
    """城市光晕必须"低色差、格内均匀"（v3.5.2）。

    v3.5.1 的暖黄光点与底色差近 200 级，实测被判定为"贴了几颗 LED"。
    这里守住两条：峰值增量克制、格内不能退化成圆斑。
    """
    print("\n--- 城市光晕 ---")
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.RED, seed=4)
    mv = MapView(pygame.Rect(0, 0, 400, 300), st)
    mv.tile = 26
    sp = mv._glow_sprite(0)
    ts = 26
    mg = (sp.get_width() - ts) // 2
    check(mg >= 3, f"光晕有边缘外溢（{mg}px）")
    peak = max(sp.get_at((mg + ts // 2, mg + ts // 2))[:3])
    check(peak <= 50, f"峰值增量克制（{peak} 级 ≤ 50，与底色同量级）")
    # 格内四角与中心的差异：均匀（差异小）说明没有圆斑
    cx, cy = mg + ts // 2, mg + ts // 2
    corners = [sp.get_at((mg + 2, mg + 2))[:3],
               sp.get_at((mg + ts - 3, mg + 2))[:3],
               sp.get_at((mg + 2, mg + ts - 3))[:3],
               sp.get_at((mg + ts - 3, mg + ts - 3))[:3]]
    center = sp.get_at((cx, cy))[:3]
    spread = max(max(abs(c[i] - center[i]) for i in range(3)) for c in corners)
    check(spread <= 10, f"格内亮度均匀，无圆斑（四角与中心差 {spread} 级 ≤ 10）")
    # 外圈角落应几乎不叠加（真正的外溢衰减到 0）
    edge = max(sp.get_at((0, 0))[:3])
    check(edge <= 6, f"外圈边缘衰减到接近 0（{edge} 级 ≤ 6）")


def test_weather_scroll() -> None:
    """天气层必须可无缝循环滚动（v3.5.3 A5）。

    做法是 2 倍高 + 下半幅复制上半幅。如果谁把复制那行删了，
    滚动到 y=H 附近会出现图案跳变——肉眼很明显但代码里看不出来，
    所以用像素比对守住。
    """
    print("\n--- 天气滚动层 ---")
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=5)
    mv = MapView(pygame.Rect(0, 0, 320, 240), st)
    mv.tile = 26
    for kind in ("rain", "snow"):
        layer = mv._weather_layer(kind)
        check(layer.get_height() == 480,
              f"{kind} 层高为视口 2 倍（{layer.get_height()} = 240×2）")
        # 上半幅与下半幅逐像素一致 → 滚动循环无缝
        top = pygame.image.tobytes(layer.subsurface(pygame.Rect(0, 0, 320, 240)), "RGB")
        bot = pygame.image.tobytes(layer.subsurface(pygame.Rect(0, 240, 320, 240)), "RGB")
        check(top == bot, f"{kind} 上下两幅完全一致（循环无跳变）")


def test_vignette_scope() -> None:
    """暗角只能盖地图区，且幅度受限（v3.5.3 A6）。"""
    print("\n--- 地图暗角 ---")
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=6)
    mv = MapView(pygame.Rect(0, 0, 400, 300), st)
    surf = pygame.Surface((400, 300))
    mv._vignette_on = True
    mv._draw_vignette(surf)
    layer = next(iter(mv._vignette_cache.values()))
    check(layer.get_size() == (400, 300), "暗角层尺寸等于地图视口（不外溢到 UI）")
    # 中心必须完全透明，否则会压暗战场
    mid = layer.get_at((200, 150))
    check(mid[3] <= 2, f"暗角层中心透明（alpha {mid[3]} ≤ 2）")
    # 最外环的不透明度上限
    edge = layer.get_at((1, 150))
    check(edge[3] <= 50, f"边缘暗度克制（alpha {edge[3]} ≤ 50）")
    mv._vignette_on = False
    surf.fill((0, 0, 0))
    mv._draw_vignette(surf)
    check(surf.get_at((200, 150))[:3] == (0, 0, 0), "关闭后不绘制任何东西")


def test_shake_stateless() -> None:
    """镜头震动必须无状态、有上限、可复原（v3.5.3 A8）。"""
    print("\n--- 镜头震动 ---")
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=7)
    mv = MapView(pygame.Rect(0, 0, 400, 300), st)
    check(mv._shake_offset() == (0, 0), "无爆炸时不震动")
    st.anim.add("burst", (5, 5), ttl=0.4)
    off = mv._shake_offset()
    check(off != (0, 0), f"有爆炸时产生偏移 {off}")
    check(abs(off[0]) <= 3 and abs(off[1]) <= 3, "偏移不超过 3px（否则格线错位像故障）")
    surf = pygame.Surface((400, 300))
    ox0, oy0 = mv.ox, mv.oy
    for _ in range(3):
        mv.draw(surf)
    check(mv.ox == ox0 and mv.oy == oy0, "绘制后镜头复原（异常路径也不漂移）")
    st.anim.events.clear()
    check(mv._shake_offset() == (0, 0), "爆炸过期后自动归零")


def test_chunk_cache() -> None:
    print("\n--- 地形块缓存 ---")
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=3)
    mv = MapView(pygame.Rect(0, 0, 600, 400), st)
    mv.tile = 26
    surf = pygame.Surface((600, 400))
    mv.draw(surf)
    n1 = len(mv._chunks)
    mv.draw(surf)
    check(n1 > 0 and len(mv._chunks) == n1, f"块缓存命中不重建（{n1} 块）")


def main() -> int:
    print("视觉回归自检（v3.5 A 层）")
    test_texture_fields()
    test_terrain_has_material()
    test_unit_icon_stages()
    test_city_glow()
    test_weather_scroll()
    test_vignette_scope()
    test_shake_stateless()
    test_chunk_cache()

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

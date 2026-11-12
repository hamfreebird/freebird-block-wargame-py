"""配色对比度自检：按 WCAG 2.1 算四套主题的关键前景/背景对。

**为什么需要它**：v3.4.0 的审计发现，浅色主题下地图上的目标点名只有
1.12:1、中立设施符号 1.01:1——不是「不好看」，是这两处信息在浅色主题下
等于没画。而布局自检只管重叠越界、锚点自检只管锚点合法性，**没有任何
工具会去算颜色**。这类缺陷只能靠人肉看截图，换个人、换个显示器就漏。

所以把「关键信息必须可读」变成断言：

| 类别 | 阈值 | 依据 |
|---|---|---|
| 地图标签 / 正文 | 4.5:1 | WCAG AA 正文 |
| 地图符号（图形） | 3.0:1 | WCAG AA 非文字对比 |
| 辅助说明（TEXT_FAINT 等） | 只报告 | 它承担「可以不看」的信息 |

用法：
    python tools/check_contrast.py           # 检查，失败返回 1
    python tools/check_contrast.py --report  # 只打印全部数值（含提示项）
"""
from __future__ import annotations

import argparse
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.enums import Detection  # noqa: E402
from core.env import Environment, TimeOfDay, Weather, env_tint  # noqa: E402
from ui import theme as T  # noqa: E402
from ui.inspect import _det_color  # noqa: E402

#: 文字类前景的合格线（WCAG AA 正文）
TEXT_MIN = 4.5
#: 图形/符号类前景的合格线（WCAG AA 非文字）
GRAPHIC_MIN = 3.0

FAILS: list[str] = []
NOTES: list[str] = []


def _lum(c: tuple[int, int, int]) -> float:
    def f(v: int) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def note(msg: str) -> None:
    NOTES.append(msg)


def _worst(fg, bg_items):
    """返回 (最小对比度, 对应背景名)。"""
    return min((ratio(fg, bg), name) for name, bg in bg_items)


def check_theme(tid: str, verbose: bool) -> None:
    T.set_theme(tid)
    th = T.current_theme()
    terrain = [(k.value, v) for k, v in th.terrain.items()]
    panels = [("panel", th.panel), ("panel_sunk", th.panel_sunk)]

    # 1) 地图标签压在地形上——必须读得出来
    r, name = _worst(th.map_label, terrain)
    check(r >= TEXT_MIN, f"{tid} 地图标签 vs 地形最差 {r:.2f}:1（{name}）≥ {TEXT_MIN}")

    # 1b) 叠上最坏环境色调（夜间+雾）之后仍然可读。
    #     只算「纸面色」是不够的：色调层盖在所有东西之上，实测夜雾会
    #     把动态范围砍掉一半。地图标签带一圈 BG_MAP 描边，所以真正
    #     决定可读性的是「前景 vs 描边」，不是「前景 vs 地形」。
    tint, alpha = env_tint(Environment(TimeOfDay.NIGHT, Weather.FOG, 1))
    k = alpha / 255.0
    over = lambda c: T.mix(c, tint, k)          # noqa: E731
    r = ratio(over(th.map_label), over(th.bg_map))
    check(r >= GRAPHIC_MIN, f"{tid} 夜雾下地图标签 vs 描边 {r:.2f}:1 ≥ {GRAPHIC_MIN}")

    # 2) 中立设施符号——图形，3:1 即可
    r, name = _worst(th.map_symbol, terrain)
    check(r >= GRAPHIC_MIN, f"{tid} 设施符号 vs 地形最差 {r:.2f}:1（{name}）≥ {GRAPHIC_MIN}")

    # 3) 侦测「已识别」色在面板上
    #    走 inspect._det_color 而不是直接读 th.detect_classified——要证明
    #    信息卡真的接进了主题，而不是主题里有个没人用的字段。
    det = _det_color(Detection.CLASSIFIED)
    r, name = _worst(det, panels)
    check(r >= TEXT_MIN, f"{tid} 「已识别」色 vs {name} {r:.2f}:1 ≥ {TEXT_MIN}")

    # 4) 正文
    r, name = _worst(th.text, panels)
    check(r >= TEXT_MIN, f"{tid} 正文 vs {name} {r:.2f}:1 ≥ {TEXT_MIN}")

    # 5) 语义色：用于标签 / 强调 / 数值，按正文要求
    for key in ("cyan", "amber", "green", "red"):
        fg = getattr(th, key)
        r, name = _worst(fg, panels)
        check(r >= TEXT_MIN, f"{tid} {key} vs {name} {r:.2f}:1 ≥ {TEXT_MIN}")

    # 6) 辅助说明：只报告
    for key in ("text_dim", "text_faint"):
        fg = getattr(th, key)
        r, name = _worst(fg, panels)
        if verbose:
            print(f"    [info] {tid} {key} vs {name} {r:.2f}:1")
        if r < TEXT_MIN:
            note(f"{tid} {key} vs {name} 只有 {r:.2f}:1（辅助说明，未纳入强制）")

    # 7) 地形之间的可区分度（B3）：只报告最差的一对
    worst = (99.0, "", "")
    for i, (n1, c1) in enumerate(terrain):
        for n2, c2 in terrain[i + 1:]:
            v = ratio(c1, c2)
            if v < worst[0]:
                worst = (v, n1, n2)
    if verbose:
        print(f"    [info] {tid} 地形最接近的一对 {worst[1]} vs {worst[2]} {worst[0]:.2f}:1")


def main() -> int:
    ap = argparse.ArgumentParser(description="配色对比度自检")
    ap.add_argument("--report", action="store_true", help="打印全部数值")
    args = ap.parse_args()

    print("=== 配色对比度自检（WCAG 2.1）===")
    for tid in T.theme_ids():
        print(f"--- {tid} ---")
        check_theme(tid, args.report)
    T.set_theme(T.DEFAULT_THEME)

    if NOTES:
        print(f"\n提示（{len(NOTES)} 条，不影响通过）：")
        for m in NOTES:
            print("   ·", m)
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项不达标：")
        for m in FAILS:
            print("   -", m)
        return 1
    print("✓ 四套主题的关键前景色全部达到对比度要求")
    return 0


if __name__ == "__main__":
    sys.exit(main())

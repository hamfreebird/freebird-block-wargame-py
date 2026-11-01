"""生成应用图标（PNG 多尺寸 + Windows ICO + 矢量 SVG）。

设计语言沿用游戏主题：冷色科技感底、青蓝与赤红两方色块、中央琥珀色打击箭头。

用法：
    python tools/make_icon.py            # 输出到 assets/icon/
"""
from __future__ import annotations

import os
import struct
import sys


# Windows 控制台/管道默认编码（cp1252 等）无法输出中文，
# 统一把标准输出切到 UTF-8，缺失字符以替换符兜底，避免 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "assets", "icon")
BASE = 1024
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

# 主题色（与 ui/theme.py 保持一致）
BG_TOP = (16, 24, 38)
BG_BOTTOM = (6, 9, 15)
BORDER = (31, 46, 66)
LATTICE = (22, 33, 47)
BLUE = (78, 156, 255)
BLUE_DARK = (26, 58, 96)
RED = (255, 106, 106)
RED_DARK = (96, 34, 42)
AMBER = (255, 196, 92)
AMBER_DARK = (120, 80, 24)
WHITE = (244, 250, 255)


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def draw_icon() -> pygame.Surface:
    S = BASE
    surf = pygame.Surface((S, S), pygame.SRCALPHA)

    # ---- 圆角底板 + 垂直渐变 ----
    radius = int(S * 0.22)
    mask = pygame.Surface((S, S), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), pygame.Rect(0, 0, S, S), border_radius=radius)

    grad = pygame.Surface((1, S))
    for y in range(S):
        grad.set_at((0, y), lerp(BG_TOP, BG_BOTTOM, y / (S - 1)))
    grad = pygame.transform.smoothscale(grad, (S, S)).convert_alpha()
    # 先铺满渐变，再用圆角遮罩把四角裁掉（RGBA 取 min）
    surf.blit(grad, (0, 0))
    surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)

    # 底板描边
    edge = pygame.Surface((S, S), pygame.SRCALPHA)
    pygame.draw.rect(edge, (*BORDER, 255), pygame.Rect(3, 3, S - 6, S - 6),
                     width=max(2, S // 256), border_radius=radius - 3)
    surf.blit(edge, (0, 0))

    # ---- 底板内的战场格点（地形格栅）----
    lattice = pygame.Surface((S, S), pygame.SRCALPHA)
    step = S // 10
    dot = max(2, S // 320)
    for gx in range(1, 10):
        for gy in range(1, 10):
            x = gx * step
            y = gy * step
            pygame.draw.circle(lattice, (*LATTICE, 255), (x, y), dot)
    surf.blit(lattice, (0, 0))

    # ---- 蓝方块（左下）----
    def block(cx: int, cy: int, size: int, base: tuple[int, int, int],
              dark: tuple[int, int, int]) -> None:
        r = pygame.Rect(0, 0, size, size)
        r.center = (cx, cy)
        # 外框（发光）
        glow = pygame.Surface((S, S), pygame.SRCALPHA)
        pygame.draw.rect(glow, (*base, 70), r.inflate(int(size * 0.10), int(size * 0.10)),
                         border_radius=int(size * 0.18))
        surf.blit(glow, (0, 0))
        # 主体
        pygame.draw.rect(surf, dark, r, border_radius=int(size * 0.18))
        inner = r.inflate(-int(size * 0.16), -int(size * 0.16))
        pygame.draw.rect(surf, base, inner, border_radius=int(size * 0.14))
        # 顶部细高光
        hl = pygame.Rect(inner.x + int(size * 0.05), inner.y + int(size * 0.05),
                         inner.width - int(size * 0.10), max(2, inner.height // 9))
        pygame.draw.rect(surf, (*WHITE, 46), hl, border_radius=int(size * 0.08))

    bsize = int(S * 0.30)
    block(int(S * 0.27), int(S * 0.66), bsize, BLUE, BLUE_DARK)
    block(int(S * 0.73), int(S * 0.34), bsize, RED, RED_DARK)

    # ---- 中央打击箭头（琥珀）----
    ax, ay = int(S * 0.50), int(S * 0.50)
    arm = int(S * 0.15)
    thick = max(8, int(S * 0.075))
    for sign in (-1, 1):
        pygame.draw.line(surf, AMBER_DARK,
                         (ax - int(S * 0.13), ay + sign * arm),
                         (ax + int(S * 0.07), ay), thick)
        pygame.draw.line(surf, AMBER,
                         (ax - int(S * 0.13), ay + sign * arm),
                         (ax + int(S * 0.07), ay), max(3, thick // 2))
    # 箭头前缘
    pygame.draw.polygon(surf, AMBER_DARK, [
        (ax + int(S * 0.02), ay),
        (ax + int(S * 0.15), ay - int(S * 0.085)),
        (ax + int(S * 0.15), ay + int(S * 0.085)),
    ])
    pygame.draw.polygon(surf, AMBER, [
        (ax + int(S * 0.04), ay),
        (ax + int(S * 0.135), ay - int(S * 0.065)),
        (ax + int(S * 0.135), ay + int(S * 0.065)),
    ])

    # ---- 命中火花 ----
    fx, fy = int(S * 0.80), int(S * 0.22)
    for ang in range(0, 360, 45):
        import math
        rad = math.radians(ang)
        r0 = int(S * 0.030)
        r1 = int(S * 0.062)
        pygame.draw.line(surf, AMBER,
                         (fx + int(math.cos(rad) * r0), fy + int(math.sin(rad) * r0)),
                         (fx + int(math.cos(rad) * r1), fy + int(math.sin(rad) * r1)),
                         max(2, thick // 2))
    pygame.draw.circle(surf, WHITE, (fx, fy), max(3, int(S * 0.018)))

    return surf


def write_ico(pngs: list[tuple[int, bytes]], path: str) -> None:
    """把 PNG 数据打包成多尺寸 ICO（Vista+ 支持 PNG 压缩条目）。"""
    count = len(pngs)
    offset = 6 + 16 * count
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    data = b""
    for size, blob in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
        data += blob
    with open(path, "wb") as f:
        f.write(header + entries + data)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#101826"/><stop offset="1" stop-color="#06090f"/>
    </linearGradient>
  </defs>
  <rect x="3" y="3" width="1018" height="1018" rx="225" ry="225" fill="url(#bg)"
        stroke="#1f2e42" stroke-width="6"/>
  <g fill="#16212f">
    {lattice}
  </g>
  <rect x="205" y="474" width="307" height="307" rx="55" ry="55" fill="#1a3a60" opacity="0.45"/>
  <rect x="216" y="485" width="285" height="285" rx="48" ry="48" fill="#4e9cff"/>
  <rect x="240" y="510" width="237" height="32" rx="16" ry="16" fill="#ffffff" opacity="0.18"/>
  <rect x="512" y="194" width="307" height="307" rx="55" ry="55" fill="#602228" opacity="0.45"/>
  <rect x="523" y="205" width="285" height="285" rx="48" ry="48" fill="#ff6a6a"/>
  <rect x="547" y="230" width="237" height="32" rx="16" ry="16" fill="#ffffff" opacity="0.18"/>
  <g stroke="#ffc45c" stroke-width="74" stroke-linecap="round" fill="none">
    <path d="M389 359 L573 512"/><path d="M389 665 L573 512"/>
  </g>
  <path d="M533 425 L665 512 L533 599 Z" fill="#ffc45c"/>
  <g stroke="#ffc45c" stroke-width="28" stroke-linecap="round">
    <path d="M819 143 L850 174"/><path d="M819 281 L850 250"/>
    <path d="M728 118 L743 157"/><path d="M910 118 L895 157"/>
  </g>
  <circle cx="819" cy="212" r="24" fill="#f4faff"/>
</svg>
"""


def build_svg() -> str:
    dots = []
    step = 1024 // 10
    dot = 4
    for gx in range(1, 10):
        for gy in range(1, 10):
            dots.append(f'<circle cx="{gx * step}" cy="{gy * step}" r="{dot}"/>')
    return SVG.replace("{lattice}", "\n    ".join(dots))


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    pygame.init()
    pygame.display.set_mode((8, 8))

    base = draw_icon()
    ico_entries: list[tuple[int, bytes]] = []
    for size in SIZES:
        img = pygame.transform.smoothscale(base, (size, size))
        path = os.path.join(OUT_DIR, f"icon_{size}.png")
        pygame.image.save(img, path)
        if size in (16, 32, 48, 64, 128, 256):
            with open(path, "rb") as f:
                ico_entries.append((size, f.read()))
        print(f"  {os.path.basename(path)}")

    # 主图标（256，供 README / 窗口图标使用）
    pygame.image.save(pygame.transform.smoothscale(base, (256, 256)),
                      os.path.join(OUT_DIR, "icon.png"))
    write_ico(ico_entries, os.path.join(OUT_DIR, "icon.ico"))
    with open(os.path.join(OUT_DIR, "icon.svg"), "w", encoding="utf-8") as f:
        f.write(build_svg())
    print("  icon.ico")
    print("  icon.svg")
    print(f"输出目录：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

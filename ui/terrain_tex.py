"""程序化地形材质（v3.5 A1）。

**为什么需要**：地形此前是纯色块 + 几条符号线（树点、山三角、建筑方块）。
那是"符号地图"，可辨识但缺少材质感。本模块生成可平铺的灰度噪声纹理，
与主题色相乘后贴到地形块上——同一套纹理适配全部四套主题，
不需要任何美术素材，也不引入 numpy 依赖。

**为什么是灰度而不是彩色**：主题有 4 套（tactical / light / 两套色盲友好），
直接出彩色要为每套各生成一份，换主题时必须重算。灰度层与任意底色合成，
四套主题共用一份纹理，只在启动时生成一次。

**为什么用「加减」而不是 `BLEND_MULT` 相乘**：这是实测踩到的坑。
深色主题的地形底色是 (43,62,49) 这个量级，乘 0.85 只差 6 级——
肉眼完全看不出材质。乘 1.0 与乘 0.85 在屏幕上几乎同色。
改成对底色**加/减**一个偏移量后，同样的 ±20 级在深色底与亮色底上
都清晰可见，且不改变色相。所以纹理由两层构成：
`light`（比中性亮多少）与 `dark`（比中性暗多少），
合成时先用 `BLEND_ADD` 加亮层，再用 `BLEND_SUB` 减暗层。
"""
from __future__ import annotations

import math
import random

import pygame

from core.enums import Terrain

#: 纹理边长。必须 ≥ 最大 tile（84 px，见 MapView.tile 上限），
#: 否则大格时取不到完整子区域。96 生成一次约 100 ms，只在启动时付出。
TEX_SIZE = 96
#: 中性值：纹理取这个灰度时底色不变。
TEX_MID = 128
#: 单边最大明暗偏移（级）。26 是"看得见材质但不喧宾夺主"的量级：
#: 深色底（43,62,49）上加到 69、减到 17，层次清晰；亮色底同样有效。
TEX_AMPLITUDE = 26

_TEXTURES: dict[Terrain, pygame.Surface] = {}


def _value_noise(size: int, cells: int, seed: int) -> list[float]:
    """可平铺的值噪声，返回 size×size 的 [0,1] 浮点表。

    可平铺靠格点索引取模：右边缘的插值会回到左边缘的格点，
    因此把纹理重复平铺时接缝不可见。
    """
    rnd = random.Random(seed)
    # 多一格用于环绕：格点 (i, j) 与 (i+cells, j) 是同一个
    grid = [[rnd.random() for _ in range(cells)] for _ in range(cells)]
    step = size / cells
    out: list[float] = [0.0] * (size * size)
    for y in range(size):
        fy = y / step
        iy = int(fy)
        ty = fy - iy
        ty = ty * ty * (3 - 2 * ty)          # smoothstep，避免线性插值的菱形纹
        y0 = iy % cells
        y1 = (iy + 1) % cells
        row0 = grid[y0]
        row1 = grid[y1]
        base = y * size
        for x in range(size):
            fx = x / step
            ix = int(fx)
            tx = fx - ix
            tx = tx * tx * (3 - 2 * tx)
            x0 = ix % cells
            x1 = (ix + 1) % cells
            a = row0[x0] + (row0[x1] - row0[x0]) * tx
            b = row1[x0] + (row1[x1] - row1[x0]) * tx
            out[base + x] = a + (b - a) * ty
    return out


def _fbm(size: int, cells: int, octaves: int, seed: int) -> list[float]:
    """多八度叠加，得到更自然的细节层次。"""
    acc = [0.0] * (size * size)
    amp = 1.0
    total = 0.0
    c = cells
    for i in range(octaves):
        layer = _value_noise(size, c, seed + i * 7919)
        for k in range(len(acc)):
            acc[k] += layer[k] * amp
        total += amp
        amp *= 0.5
        c = min(size, c * 2)
    inv = 1.0 / max(1e-6, total)
    return [v * inv for v in acc]


def _ridged(size: int, cells: int, octaves: int, seed: int) -> list[float]:
    """脊状噪声：1 - |2n-1| 叠加，产生山脊线。"""
    acc = [0.0] * (size * size)
    amp = 1.0
    total = 0.0
    c = cells
    for i in range(octaves):
        layer = _value_noise(size, c, seed + i * 6151)
        for k in range(len(acc)):
            v = layer[k]
            acc[k] += (1.0 - abs(2.0 * v - 1.0)) * amp
        total += amp
        amp *= 0.55
        c = min(size, c * 2)
    inv = 1.0 / max(1e-6, total)
    return [v * inv for v in acc]


def _hash01(x: int, y: int, seed: int = 0) -> float:
    """确定性 2D 哈希，用于"街灯""碎石"这类离散亮点。"""
    h = (x * 374761393 + y * 668265263 + seed * 2147483647) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFF) / 65535.0


def _shade(v: float, lo: float = 225.0, hi: float = 255.0) -> int:
    return int(max(lo, min(hi, TEX_MID + v * (hi - TEX_MID) / 1.0)))


def _to_surface(field: list[float], lo: float, hi: float) -> pygame.Surface:
    """把 [0,1] 场映射到灰度面。

    **对比度拉伸是必须的**：fBm 叠加后的实际值域通常只有 0.25–0.75，
    直接按 [0,1] 映射会让纹理的灰度差不到 10 级——肉眼完全看不出材质，
    只剩一层几乎均匀的灰。先按实际 min/max 拉满再映射到 [lo,hi]。

    逐像素 `set_at` 很慢（256² 要 20 ms），所以先拼字节串用
    `pygame.image.frombuffer` 一次成型，比逐点写快一个数量级。
    """
    size = int(math.isqrt(len(field)))
    span = hi - lo
    vmin = min(field)
    vmax = max(field)
    inv = 1.0 / max(1e-6, vmax - vmin)
    buf = bytearray(size * size * 3)
    for i, v in enumerate(field):
        n = (v - vmin) * inv
        g = int(lo + (n if n > 0.0 else 0.0) * span)
        if g > 255:
            g = 255
        elif g < 0:
            g = 0
        j = i * 3
        buf[j] = g
        buf[j + 1] = g
        buf[j + 2] = g
    return pygame.image.frombuffer(bytes(buf), (size, size), "RGB").copy()


def make_terrain_texture(terrain: Terrain, size: int = TEX_SIZE) -> pygame.Surface:
    """生成单种地形的灰度纹理（RGB 相同，值域 225–255）。

    每种地形的噪声配方不同——这是"材质感"的来源：
    草地要有柔和的团块，山地要有脊线，城市要有正交街道，
    沙漠要有单向波纹。全用同一种噪声会让九种地形看起来一样。
    """
    if terrain is Terrain.WATER:
        # 多层正弦叠加：横向流动的水面 + 细碎高光
        n = _fbm(size, 4, 2, 1101)
        out = [0.0] * (size * size)
        for y in range(size):
            for x in range(size):
                i = y * size + x
                # 频率要足够高：tile 约 26–34 px，若一个 tile 里只有
                # 一两个波峰，整片水域会显出规则斑块。6/4 个波峰只覆盖
                # 不到半格，实测仍像地砖；提到 14/9 后转为细密水纹。
                wave = (math.sin((x / size) * math.pi * 14 + n[i] * 3.0)
                        + math.sin((y / size) * math.pi * 9 - n[i] * 2.0)) * 0.5
                out[i] = 0.5 + wave * 0.30 + (n[i] - 0.5) * 0.30
        return _to_surface(out, 0, 255)

    if terrain is Terrain.COAST:
        # 沙粒高频 + 潮痕带
        n = _fbm(size, 16, 2, 2202)
        band = _value_noise(size, 3, 2211)
        out = [n[i] * 0.55 + band[i] * 0.45 for i in range(size * size)]
        return _to_surface(out, 0, 255)

    if terrain is Terrain.PLAIN:
        # 柔和草地：低频团块 + 中频细节，振幅克制
        n = _fbm(size, 5, 3, 3303)
        out = [v for v in n]
        return _to_surface(out, 0, 255)

    if terrain is Terrain.FOREST:
        # 团簇感：低频决定明暗块，高频做针叶颗粒
        lo = _fbm(size, 4, 2, 4404)
        hi = _fbm(size, 20, 2, 4411)
        out = [lo[i] * 0.72 + hi[i] * 0.28 for i in range(size * size)]
        return _to_surface(out, 0, 255)

    if terrain is Terrain.HILLS:
        # 缓坡条带：低频噪声压扁在某一轴，形成等高线走向
        n = _fbm(size, 5, 2, 5505)
        out = [0.0] * (size * size)
        for y in range(size):
            for x in range(size):
                i = y * size + x
                out[i] = n[i] * 0.6 + (0.5 + 0.5 * math.sin(
                    (y / size) * math.pi * 5 + n[i] * 4.0)) * 0.4
        return _to_surface(out, 0, 255)

    if terrain is Terrain.MOUNTAIN:
        # 脊状噪声：产生岩脊与沟谷，是山地质感的关键
        n = _ridged(size, 4, 3, 6606)
        grain = _fbm(size, 24, 1, 6611)
        out = [n[i] * 0.82 + grain[i] * 0.18 for i in range(size * size)]
        return _to_surface(out, 0, 255)

    if terrain is Terrain.URBAN:
        # 正交街道网 + 随机亮窗：城市视觉特征是"规则的格网 + 不规则的灯"
        out = [0.42] * (size * size)
        block = max(4, size // 8)
        for y in range(size):
            for x in range(size):
                i = y * size + x
                # 街道：每 block 一条，压暗
                on_street = (x % block == 0) or (y % block == 0)
                if on_street:
                    out[i] = 0.20
                else:
                    # 街区内：楼顶明暗随机
                    bx, by = x // block, y // block
                    out[i] = 0.45 + _hash01(bx, by, 7707) * 0.25
                    # 少量亮窗（约 12%）
                    if _hash01(x, y, 7711) > 0.88:
                        out[i] = 1.0
        return _to_surface(out, 0, 255)

    if terrain is Terrain.DESERT:
        # 单向波纹：沿 x 轴拉伸的正弦 + 高频沙粒
        n = _fbm(size, 6, 2, 8808)
        grain = _fbm(size, 28, 1, 8811)
        out = [0.0] * (size * size)
        for y in range(size):
            for x in range(size):
                i = y * size + x
                ripple = 0.5 + 0.5 * math.sin(
                    (x / size) * math.pi * 7 + n[i] * 5.0)
                out[i] = ripple * 0.55 + n[i] * 0.25 + grain[i] * 0.20
        return _to_surface(out, 0, 255)

    # MARSH：斑驳水洼 + 暗斑
    lo = _fbm(size, 5, 2, 9909)
    hi = _fbm(size, 18, 2, 9911)
    out = [0.0] * (size * size)
    for i in range(size * size):
        v = lo[i] * 0.65 + hi[i] * 0.35
        # 低处压暗，形成水洼
        out[i] = v * (0.75 if v < 0.42 else 1.0)
    return _to_surface(out, 0, 255)


def _split_offset(tex: pygame.Surface, amplitude: int) -> tuple[pygame.Surface, pygame.Surface]:
    """把中性灰纹理拆成「加亮层」与「减暗层」，都是黑底 + 单向偏移。

    `BLEND_ADD` 只能加、`BLEND_SUB` 只能减，所以一次带符号的偏移
    要拆成两张单向量图。合成时顺序无关，两次 blit 各管一个方向。
    """
    w, h = tex.get_size()
    src = pygame.image.tobytes(tex, "RGB")
    light = bytearray(w * h * 3)
    dark = bytearray(w * h * 3)
    for i in range(w * h):
        g = src[i * 3]
        j = i * 3
        if g > TEX_MID:
            v = int((g - TEX_MID) * amplitude / max(1, 255 - TEX_MID))
            if v > 255:
                v = 255
            light[j] = light[j + 1] = light[j + 2] = v
        elif g < TEX_MID:
            v = int((TEX_MID - g) * amplitude / max(1, TEX_MID))
            if v > 255:
                v = 255
            dark[j] = dark[j + 1] = dark[j + 2] = v
    return (pygame.image.frombuffer(bytes(light), (w, h), "RGB").copy(),
            pygame.image.frombuffer(bytes(dark), (w, h), "RGB").copy())


_OFFSETS: dict[Terrain, tuple[pygame.Surface, pygame.Surface]] = {}


def _tile2x(tex: pygame.Surface) -> pygame.Surface:
    """把纹理平铺成 2×2。

    **为什么必须平铺**：取样时源区域是 `Rect(ox, oy, ts, ts)`，而
    ox 最大到 `TEX_SIZE - 1`。若 ox + ts 超过纹理边长，pygame 会把源
    区域裁到边界内——那一格就只有一部分带纹理、其余保持纯底色。
    实测 tile=34 时有 33/96 的取样位置越界，整片水域因此呈现规则的
    点阵（比不加纹理更难看）。平铺到 2 倍边长后，ox < 边长 且 ts ≤ 84
    时必有完整区域可取。
    """
    w, h = tex.get_size()
    out = pygame.Surface((w * 2, h * 2))
    for i in range(2):
        for j in range(2):
            out.blit(tex, (i * w, j * h))
    return out


def offsets(size: int = TEX_SIZE) -> dict[Terrain, tuple[pygame.Surface, pygame.Surface]]:
    """全部地形的 (加亮层, 减暗层)，按尺寸缓存。

    缓存键只有尺寸——偏移层是相对中性的明暗量，与主题无关，
    换主题不需要重算；主题只影响合成时的底色。
    """
    global _TEX_SIZE_CACHE
    if _TEX_SIZE_CACHE != size:
        _TEX_SIZE_CACHE = size
        _TEXTURES.clear()
        _OFFSETS.clear()
    if not _OFFSETS:
        for t in Terrain:
            tex = make_terrain_texture(t, size)
            _TEXTURES[t] = tex
            light, dark = _split_offset(tex, TEX_AMPLITUDE)
            # 平铺成 2×2，避免取样越界（见 _tile2x 说明）
            _OFFSETS[t] = (_tile2x(light), _tile2x(dark))
    return _OFFSETS


def textures(size: int = TEX_SIZE) -> dict[Terrain, pygame.Surface]:
    """全部地形的中性灰纹理（调试与工具用；游戏内走 offsets）。"""
    offsets(size)
    return _TEXTURES


_TEX_SIZE_CACHE: int | None = None


__all__ = [
    "TEX_SIZE", "TEX_MID", "TEX_AMPLITUDE",
    "make_terrain_texture", "offsets", "textures",
]

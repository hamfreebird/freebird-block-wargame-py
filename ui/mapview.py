"""地图渲染与坐标变换。

负责：地形预渲染（缓存）、单位棋子与兵种图标、目标点、射程包线、
探测覆盖、导弹在飞轨迹、战斗特效、缩略图。
"""
from __future__ import annotations

import math

import pygame

from core.enums import Detection, Domain, Facility, Side, Terrain, UnitClass
from core.env import env_tint, environment_for
from core.model import Tile, Unit
from core import anim as anim_mod
from core.icons import icon_bitmap

from . import theme as T

TILE_STEPS = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 32, 36, 40, 46, 52, 60]

# 地形分块渲染：每块边长（格）。
# 整图预渲染在大地图上不可行——256×256 放大到 tile=60 时需要一张
# 15360×15360 的 Surface（约 900MB）。改为按块按需构建，只保留视口附近的块。
CHUNK_TILES = 16
# 缓存预算按**字节**而非块数计：一块的地图像素量随 tile 变化极大
# （tile=10 时 102KB，tile=60 时 3.7MB），按块数封顶要么浪费内存、
# 要么在大 tile 下反复重建（LRU 抖动会让帧率崩掉）。
CHUNK_CACHE_BYTES = 64 * 1024 * 1024


class MapView:
    def __init__(self, rect: pygame.Rect, state):
        self.rect = pygame.Rect(rect)
        self.state = state
        self.tile = 22
        self.ox = 0.0
        self.oy = 0.0
        self._chunks: dict[tuple[int, int], pygame.Surface] = {}
        self._chunk_lru: list[tuple[int, int]] = []
        self._chunk_bytes = 0
        self._chunk_epoch: tuple | None = None
        self._token_cache: dict[tuple, pygame.Surface] = {}
        self._token_epoch_cache: tuple | None = None
        self._vector_icons: dict[tuple, pygame.Surface] = {}
        self._weather_cache: dict[tuple, pygame.Surface] = {}
        self._tint_cache: dict[tuple, pygame.Surface] = {}
        self._fx_layer: pygame.Surface | None = None
        self._anim_layer: pygame.Surface | None = None
        self._ring_cache: dict[tuple, pygame.Surface] = {}
        self.overlay = "none"          # none / sensors / threat
        self.show_grid = True
        self.fit()

    # ------------------------------------------------------------------ #
    # 坐标
    # ------------------------------------------------------------------ #
    def fit(self) -> None:
        md = self.state.map
        sx = (self.rect.width - 8) / md.width
        sy = (self.rect.height - 8) / md.height
        target = min(sx, sy)
        best = TILE_STEPS[0]
        for s in TILE_STEPS:
            if s <= target:
                best = s
        self.tile = best
        self._center()

    def _center(self) -> None:
        md = self.state.map
        self.ox = (self.rect.width - md.width * self.tile) / 2
        self.oy = (self.rect.height - md.height * self.tile) / 2
        self._clamp()

    def _clamp(self) -> None:
        md = self.state.map
        mw, mh = md.width * self.tile, md.height * self.tile
        if mw <= self.rect.width:
            self.ox = (self.rect.width - mw) / 2
        else:
            self.ox = min(0.0, max(self.rect.width - mw, self.ox))
        if mh <= self.rect.height:
            self.oy = (self.rect.height - mh) / 2
        else:
            self.oy = min(0.0, max(self.rect.height - mh, self.oy))

    def pan(self, dx: float, dy: float) -> None:
        self.ox += dx
        self.oy += dy
        self._clamp()

    def zoom_at(self, pos: tuple[int, int], step: int) -> None:
        old = self.tile
        try:
            i = TILE_STEPS.index(old)
        except ValueError:
            i = min(range(len(TILE_STEPS)), key=lambda k: abs(TILE_STEPS[k] - old))
        i = max(0, min(len(TILE_STEPS) - 1, i + step))
        new = TILE_STEPS[i]
        if new == old:
            return
        # 保持光标下的地图点不变
        gx = (pos[0] - self.rect.x - self.ox) / old
        gy = (pos[1] - self.rect.y - self.oy) / old
        self.tile = new
        self.ox = pos[0] - self.rect.x - gx * new
        self.oy = pos[1] - self.rect.y - gy * new
        self._clamp()

    def grid_to_rect(self, x: int, y: int) -> pygame.Rect:
        return pygame.Rect(int(self.rect.x + self.ox + x * self.tile),
                           int(self.rect.y + self.oy + y * self.tile),
                           self.tile, self.tile)

    def grid_center(self, x: float, y: float) -> tuple[int, int]:
        return (int(self.rect.x + self.ox + (x + 0.5) * self.tile),
                int(self.rect.y + self.oy + (y + 0.5) * self.tile))

    def screen_to_grid(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        gx = math.floor((pos[0] - self.rect.x - self.ox) / self.tile)
        gy = math.floor((pos[1] - self.rect.y - self.oy) / self.tile)
        if self.state.map.in_bounds(gx, gy):
            return (gx, gy)
        return None

    def contains(self, pos: tuple[int, int]) -> bool:
        return self.rect.collidepoint(pos)

    # ------------------------------------------------------------------ #
    # 地形层（分块）
    # ------------------------------------------------------------------ #
    def _terrain_epoch(self) -> tuple:
        """地形缓存的失效指纹。

        包含主题 id——换肤后地形颜色必须重画，否则会残留旧配色。
        """
        md = self.state.map
        return (self.tile, self.show_grid, md.width, md.height, id(md),
                T.current_theme().id)

    def _chunk_bounds(self, cx: int, cy: int) -> tuple[int, int, int, int]:
        """块的格子范围 (x0, y0, x1, y1)，右开区间。"""
        md = self.state.map
        x0 = cx * CHUNK_TILES
        y0 = cy * CHUNK_TILES
        return x0, y0, min(md.width, x0 + CHUNK_TILES), min(md.height, y0 + CHUNK_TILES)

    def _build_chunk(self, cx: int, cy: int) -> pygame.Surface:
        """构建单块地形。

        海岸线与等高线在块边界处会被截断——这是可接受的：块之间存在
        1 格的取整误差在视觉上不可见，而换取的是内存从 O(全图) 降到
        O(视口)。**但网格线必须在块内完整画出**，否则块之间会出现缝隙。
        """
        md = self.state.map
        ts = self.tile
        x0, y0, x1, y1 = self._chunk_bounds(cx, cy)
        w = max(1, x1 - x0)
        h = max(1, y1 - y0)
        surf = pygame.Surface((w * ts, h * ts))
        surf.fill(T.BG_MAP)

        for ty in range(y0, y1):
            for tx in range(x0, x1):
                t = md.at(tx, ty)
                if t is None:
                    continue
                lx = (tx - x0) * ts
                ly = (ty - y0) * ts
                base = T.TERRAIN_COLORS.get(t.terrain, T.TERRAIN_COLORS[Terrain.PLAIN])
                col = T.elevation_shade(base, t.elevation)
                # 山体阴影：按西北方向高差做明暗，形成柔和的地形起伏
                if t.terrain is not Terrain.WATER:
                    # 中心差分求坡度，避免单侧差分产生条带状伪影
                    l_ = md.at(tx - 1, ty)
                    r_ = md.at(tx + 1, ty)
                    u_ = md.at(tx, ty - 1)
                    d_ = md.at(tx, ty + 1)
                    gx = ((r_.elevation if r_ else t.elevation)
                          - (l_.elevation if l_ else t.elevation)) / 2.0
                    gy = ((d_.elevation if d_ else t.elevation)
                          - (u_.elevation if u_ else t.elevation)) / 2.0
                    dh = gx * 0.7 + gy * 0.7
                    f = 1.0 + max(-0.13, min(0.13, dh / 2600.0))
                    col = (max(0, min(255, int(col[0] * f))),
                           max(0, min(255, int(col[1] * f))),
                           max(0, min(255, int(col[2] * f))))
                r = pygame.Rect(lx, ly, ts, ts)
                pygame.draw.rect(surf, col, r)
                self._decorate(surf, t, r, col)

        # 海岸线：陆地与水域相邻处提亮一道细边
        if ts >= 10:
            shore = (150, 196, 214)
            for ty in range(y0, y1):
                for tx in range(x0, x1):
                    t = md.at(tx, ty)
                    if t is None or t.terrain is Terrain.WATER:
                        continue
                    lx = (tx - x0) * ts
                    ly = (ty - y0) * ts
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        n = md.at(tx + dx, ty + dy)
                        if n is None or n.terrain is not Terrain.WATER:
                            continue
                        if dx == 1:
                            pygame.draw.line(surf, shore, (lx + ts - 1, ly), (lx + ts - 1, ly + ts))
                        elif dx == -1:
                            pygame.draw.line(surf, shore, (lx, ly), (lx, ly + ts))
                        elif dy == 1:
                            pygame.draw.line(surf, shore, (lx, ly + ts - 1), (lx + ts, ly + ts - 1))
                        else:
                            pygame.draw.line(surf, shore, (lx, ly), (lx + ts, ly))

        # 等高线（每 600 米一级，仅在与邻格分级不同处描一道暗线）
        if ts >= 13:
            for ty in range(y0, y1):
                for tx in range(x0, x1):
                    t = md.at(tx, ty)
                    if t is None or t.terrain is Terrain.WATER:
                        continue
                    band = t.elevation // 600
                    lx = (tx - x0) * ts
                    ly = (ty - y0) * ts
                    n = md.at(tx + 1, ty)
                    if n is not None and n.terrain is not Terrain.WATER and n.elevation // 600 != band:
                        pygame.draw.line(surf, (12, 18, 24), (lx + ts - 1, ly),
                                         (lx + ts - 1, ly + ts))
                    n = md.at(tx, ty + 1)
                    if n is not None and n.terrain is not Terrain.WATER and n.elevation // 600 != band:
                        pygame.draw.line(surf, (12, 18, 24), (lx, ly + ts - 1),
                                         (lx + ts, ly + ts - 1))

        # 网格：低对比度分隔线，避免干扰地形。
        # 块内自绘完整网格（含块边界），这样相邻块的线正好接上，不留缝。
        if self.show_grid and ts >= 12:
            for gx in range(x0, x1 + 1):
                px = (gx - x0) * ts
                pygame.draw.line(surf, T.GRID, (px, 0), (px, h * ts), 1)
            for gy in range(y0, y1 + 1):
                py = (gy - y0) * ts
                pygame.draw.line(surf, T.GRID, (0, py), (w * ts, py), 1)

        return surf

    def _get_chunk(self, cx: int, cy: int) -> pygame.Surface:
        """取块，必要时构建；按字节预算做 LRU 淘汰。"""
        key = (cx, cy)
        surf = self._chunks.get(key)
        if surf is not None:
            # 移到 LRU 队尾
            try:
                self._chunk_lru.remove(key)
            except ValueError:
                pass
            self._chunk_lru.append(key)
            return surf
        surf = self._build_chunk(cx, cy)
        self._chunks[key] = surf
        self._chunk_lru.append(key)
        self._chunk_bytes += surf.get_width() * surf.get_height() * 4
        while self._chunk_bytes > CHUNK_CACHE_BYTES and len(self._chunk_lru) > 1:
            old = self._chunk_lru.pop(0)
            victim = self._chunks.pop(old, None)
            if victim is not None:
                self._chunk_bytes -= victim.get_width() * victim.get_height() * 4
        return surf

    def _visible_chunks(self) -> list[tuple[int, int]]:
        """视口覆盖的块列表。"""
        md = self.state.map
        ts = self.tile
        # 视口在地图坐标系中的格子范围
        gx0 = int((-self.ox) // ts)
        gy0 = int((-self.oy) // ts)
        gx1 = int((self.rect.width - self.ox) // ts) + 1
        gy1 = int((self.rect.height - self.oy) // ts) + 1
        cx0 = max(0, gx0 // CHUNK_TILES)
        cy0 = max(0, gy0 // CHUNK_TILES)
        cx1 = min((md.width - 1) // CHUNK_TILES, gx1 // CHUNK_TILES)
        cy1 = min((md.height - 1) // CHUNK_TILES, gy1 // CHUNK_TILES)
        out: list[tuple[int, int]] = []
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                out.append((cx, cy))
        return out

    def _draw_terrain(self, surf: pygame.Surface) -> None:
        """绘制地形块。"""
        md = self.state.map
        ts = self.tile
        for cx, cy in self._visible_chunks():
            chunk = self._get_chunk(cx, cy)
            x0, y0, x1, y1 = self._chunk_bounds(cx, cy)
            surf.blit(chunk, (int(self.rect.x + self.ox + x0 * ts),
                              int(self.rect.y + self.oy + y0 * ts)))

    def _decorate(self, surf: pygame.Surface, t: Tile, r: pygame.Rect, col) -> None:
        ts = self.tile
        h = (t.x * 73856093 ^ t.y * 19349663) & 0xFFFF
        dark = T.mix(col, (0, 0, 0), 0.26)
        light = T.mix(col, (255, 255, 255), 0.13)

        if t.terrain is Terrain.WATER:
            yy = r.y + ts // 2 + (h % 3) - 1
            pygame.draw.line(surf, T.mix(col, (0, 0, 0), 0.14),
                             (r.x + 2, yy), (r.x + ts - 3, yy), 1)
        elif t.terrain is Terrain.FOREST:
            for i in range(3):
                px = r.x + 3 + ((h >> (i * 3)) % max(1, ts - 7))
                py = r.y + 3 + ((h >> (i * 3 + 5)) % max(1, ts - 7))
                pygame.draw.circle(surf, dark, (px, py), max(1, ts // 13))
        elif t.terrain is Terrain.MOUNTAIN:
            cx, cy = r.centerx, r.centery
            w = ts // 3
            pygame.draw.polygon(surf, dark, [(cx - w, cy + w // 2), (cx, cy - w),
                                             (cx + w, cy + w // 2)])
            pygame.draw.polygon(surf, light, [(cx, cy - w), (cx + w, cy + w // 2),
                                              (cx + w // 3, cy + w // 2)])
        elif t.terrain is Terrain.URBAN:
            s_ = max(3, ts // 5)
            for i in range(2):
                for j in range(2):
                    pygame.draw.rect(surf, dark, pygame.Rect(
                        r.x + 2 + i * (s_ + 2), r.y + 2 + j * (s_ + 2), s_, s_))
        elif t.terrain is Terrain.HILLS:
            pygame.draw.arc(surf, dark, pygame.Rect(r.x + 2, r.y + ts // 3, ts - 4, ts // 2),
                            0.2, 2.9, max(1, ts // 15))

        if t.smoke > 0:
            s_ = pygame.Surface((ts, ts), pygame.SRCALPHA)
            s_.fill((120, 120, 120, 120))
            surf.blit(s_, r.topleft)

    # ------------------------------------------------------------------ #
    # 主绘制
    # ------------------------------------------------------------------ #
    def draw(self, surf: pygame.Surface, hover: tuple[int, int] | None = None,
             reachable: dict | None = None, path: list | None = None,
             pending_target: Unit | None = None) -> None:
        epoch = self._terrain_epoch()
        if self._chunk_epoch != epoch:
            self._chunks.clear()
            self._chunk_lru.clear()
            self._chunk_bytes = 0
            self._chunk_epoch = epoch
        md = self.state.map
        surf.set_clip(self.rect)
        pygame.draw.rect(surf, T.BG, self.rect)

        self._draw_terrain(surf)
        # 地图边框：让居中的留白看起来是刻意的画框
        frame = pygame.Rect(int(self.rect.x + self.ox), int(self.rect.y + self.oy),
                            md.width * self.tile, md.height * self.tile)
        pygame.draw.rect(surf, T.BORDER_HI, frame, 1)

        self._draw_forts(surf)
        self._draw_fire_missions(surf)
        self._draw_supply_lines(surf)
        self._draw_overlay(surf)
        self._draw_objectives(surf)
        self._draw_facilities(surf)
        self._draw_reachable(surf, reachable)
        self._draw_path(surf, path)
        self._draw_missiles(surf)
        self._draw_units(surf)
        self._draw_anim(surf)
        self._draw_fx(surf)

        if hover:
            r = self.grid_to_rect(*hover)
            pygame.draw.rect(surf, (150, 200, 240), r, width=1)
        if pending_target is not None and pending_target.domain is not Domain.SPACE:
            r = self.grid_to_rect(pending_target.x, pending_target.y)
            self._draw_reticle(surf, r)

        self._draw_environment(surf)

        surf.set_clip(None)

    # -- 昼夜与天候的视觉表现 --------------------------------------------
    def _draw_environment(self, surf: pygame.Surface) -> None:
        """按当前时段与天候给战场加一层色调与天气效果。

        夜间压暗并偏蓝、雾天加白纱、雨雪加动态颗粒——让玩家**看一眼地图**
        就知道当前的侦察条件，而不必要读文字。
        """
        env = environment_for(self.state)
        tint, alpha = env_tint(env)
        if alpha > 0:
            # 色调层按 (颜色, 透明度) 缓存：同一时段/天候下图案完全相同，
            # 每帧重建一张全屏 SRCALPHA 表面是纯粹的浪费。
            key = (tint, alpha, self.rect.width, self.rect.height)
            layer = self._tint_cache.get(key)
            if layer is None:
                layer = pygame.Surface(self.rect.size, pygame.SRCALPHA)
                layer.fill((*tint, alpha))
                if len(self._tint_cache) > 6:
                    self._tint_cache.clear()
                self._tint_cache[key] = layer
            surf.blit(layer, self.rect.topleft)

        # 天气颗粒：雨为竖线，雪为缓慢下落的点
        if env.weather.value == "rain":
            self._draw_rain(surf)
        elif env.weather.value == "snow":
            self._draw_snow(surf)

    def _weather_layer(self, kind: str) -> pygame.Surface:
        """取天气图案（雨丝 / 雪花）。

        图案用固定相位生成，因此是**静态**的——每帧重画一遍纯属浪费。
        缓存成位图后每帧只是一次 blit。
        """
        key = (kind, self.rect.width, self.rect.height, max(6, self.tile))
        cached = self._weather_cache.get(key)
        if cached is not None:
            return cached
        layer = pygame.Surface(self.rect.size, pygame.SRCALPHA)
        if kind == "rain":
            ts = max(6, self.tile)
            step = max(14, ts // 2)
            for i in range(0, self.rect.width + step, step):
                h = (i * 7919) % 97
                for j in range(0, self.rect.height + step, step):
                    k = (i * 31 + j * 17 + h) % 53
                    if k > 6:
                        continue
                    x = i + (k * 3) % step
                    y = (j + h) % (self.rect.height + step)
                    pygame.draw.line(layer, (150, 190, 220, 60), (x, y), (x - 3, y + 11), 1)
        else:
            step = max(18, self.tile)
            for i in range(0, self.rect.width + step, step):
                for j in range(0, self.rect.height + step, step):
                    k = (i * 37 + j * 61) % 71
                    if k > 9:
                        continue
                    pygame.draw.circle(layer, (225, 238, 250, 95),
                                       (i + (k * 5) % step, j + (k * 7) % step), 1)
        if len(self._weather_cache) > 8:
            self._weather_cache.clear()
        self._weather_cache[key] = layer
        return layer

    def _draw_rain(self, surf: pygame.Surface) -> None:
        """斜向雨丝：用固定相位的伪随机点，避免每帧跳动造成的视觉噪声。"""
        surf.blit(self._weather_layer("rain"), self.rect.topleft)

    def _draw_snow(self, surf: pygame.Surface) -> None:
        """飘雪：稀疏的白点，越靠下越密（模拟近处视角）。"""
        surf.blit(self._weather_layer("snow"), self.rect.topleft)

    # -- 叠层 -------------------------------------------------------------
    def _draw_overlay(self, surf: pygame.Surface) -> None:
        if self.overlay == "none":
            return
        st = self.state
        if self.overlay == "sensors":
            for u in st.units_of(st.player_side):
                if not u.template.sensors:
                    continue
                rng = u.best_sensor_range()
                if rng <= 0:
                    continue
                self._ring(surf, (u.x, u.y), rng, (60, 150, 190), 1, alpha=42)
        elif self.overlay == "threat":
            for u in st.enemies_of(st.player_side):
                lvl = st.known_level(st.player_side, u)
                if lvl.level < Detection.CLASSIFIED.level:
                    continue
                rng = u.max_intercept_range()
                if rng <= 0:
                    continue
                self._ring(surf, (u.x, u.y), rng, (200, 70, 70), 1, alpha=46)

    def _ring(self, surf: pygame.Surface, center, radius_tiles: float,
              color, width: int = 2, alpha: int = 90, fill: bool = True) -> None:
        cx, cy = self.grid_center(center[0], center[1])
        r = int(radius_tiles * self.tile)
        if r < 2:
            return
        key = (r, color, width, alpha, fill)
        cached = self._ring_cache.get(key)
        if cached is None:
            size = r * 2 + 4
            if size > 2600:
                return
            s = pygame.Surface((size, size), pygame.SRCALPHA)
            if fill:
                pygame.draw.circle(s, (*color, alpha), (r + 2, r + 2), r)
            pygame.draw.circle(s, (*color, min(255, alpha + 70)), (r + 2, r + 2), r, width)
            cached = s
            if len(self._ring_cache) > 60:
                self._ring_cache.clear()
            self._ring_cache[key] = cached
        surf.blit(cached, (cx - r - 2, cy - r - 2))

    def _draw_fire_missions(self, surf: pygame.Surface) -> None:
        """火力准备射界：标定中的虚线圈与准备完成的实线圈。"""
        for u in self.state.units:
            fm = getattr(u, "fire_mission", None)
            if fm is None or not fm.active or u.side is not self.state.player_side:
                continue
            cx, cy = self.grid_center(fm.x, fm.y)
            rad = max(4, int(self.tile * 1.5))
            col = (255, 205, 120) if fm.prepared else (170, 150, 110)
            # 圆环：准备完成为实线，准备中为虚线
            if fm.prepared:
                pygame.draw.circle(surf, col, (cx, cy), rad, 2)
            else:
                for i in range(0, 360, 30):
                    a0 = math.radians(i)
                    a1 = math.radians(i + 15)
                    p0 = (cx + int(rad * math.cos(a0)), cy + int(rad * math.sin(a0)))
                    p1 = (cx + int(rad * math.cos(a1)), cy + int(rad * math.sin(a1)))
                    pygame.draw.line(surf, col, p0, p1, 2)
            # 十字标星
            s = max(3, self.tile // 5)
            pygame.draw.line(surf, col, (cx - s, cy), (cx + s, cy), 1)
            pygame.draw.line(surf, col, (cx, cy - s), (cx, cy + s), 1)
            if self.tile >= 20:
                T.draw_text(surf, "射界" if fm.prepared else "标定",
                            (cx, cy - rad - 12), 11, col, anchor="midbottom")

    def _draw_supply_lines(self, surf: pygame.Surface) -> None:
        """补给线：从后勤枢纽到受补给的部队。

        断线的部队（无补给）用红色虚线标出——一眼看出谁在饿肚子。
        """
        st = self.state
        links = getattr(st, "supply_links", {}) or {}
        if not links:
            return
        for u in st.units_of(st.player_side):
            if not u.alive or u.is_decoy:
                continue
            src = links.get(u.uid)
            if src is None:
                # 断粮：画一个红色叉
                r = self.grid_to_rect(u.x, u.y)
                if not self.rect.colliderect(r):
                    continue
                c = r.center
                s = max(3, self.tile // 4)
                pygame.draw.line(surf, (200, 80, 80), (c[0] - s, c[1] - s),
                                 (c[0] + s, c[1] + s), 1)
                pygame.draw.line(surf, (200, 80, 80), (c[0] - s, c[1] + s),
                                 (c[0] + s, c[1] - s), 1)
                continue
            a = self.grid_center(src.x, src.y)
            b = self.grid_center(u.x, u.y)
            col = (70, 190, 150)
            # 虚线
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = max(1.0, math.hypot(dx, dy))
            steps = int(length // 9)
            for i in range(steps):
                t0 = i / max(1, steps)
                t1 = (i + 0.55) / max(1, steps)
                p0 = (a[0] + dx * t0, a[1] + dy * t0)
                p1 = (a[0] + dx * t1, a[1] + dy * t1)
                pygame.draw.line(surf, col, p0, p1, 1)

    # ------------------------------------------------------------------ #
    # 地块索引（避免每帧全图扫描）
    # ------------------------------------------------------------------ #
    def _scan_tiles(self) -> None:
        """扫描全图，建立目标点 / 工事 / 设施三类索引。

        256×256 有 65536 个格子，每帧扫三遍就是 20 万次迭代——
        这正是大地图掉帧的主因之一。改为一次扫描建索引，之后每帧
        只遍历索引里的少量条目。
        """
        md = self.state.map
        obj: list[Tile] = []
        fort: list[Tile] = []
        fac: list[Tile] = []
        for t in md.iter_tiles():
            if t.objective:
                obj.append(t)
            if t.fort > 0 and t.fort_owner is not Side.NEUTRAL:
                fort.append(t)
            if t.facility is not Facility.NONE:
                fac.append(t)
        self._obj_list = obj
        self._fort_list = fort
        self._fac_list = fac
        self._tile_index_epoch = self.state.map_epoch

    def _ensure_tile_index(self) -> None:
        """确保索引是最新的。"""
        if getattr(self, "_tile_index_epoch", None) != self.state.map_epoch:
            self._scan_tiles()

    def _objective_tiles(self) -> list[Tile]:
        self._ensure_tile_index()
        return self._obj_list

    def _fort_tiles(self) -> list[Tile]:
        self._ensure_tile_index()
        return self._fort_list

    def _facility_tiles(self) -> list[Tile]:
        self._ensure_tile_index()
        return self._fac_list

    def _draw_objectives(self, surf: pygame.Surface) -> None:
        for t in self._objective_tiles():
            r = self.grid_to_rect(t.x, t.y)
            if not self.rect.colliderect(r):
                continue
            col = T.side_color(t.owner, "edge") if t.owner is not Side.NEUTRAL else T.AMBER
            cx, cy = r.centerx, r.centery
            s = max(4, self.tile // 4)
            pygame.draw.polygon(surf, (0, 0, 0), [(cx, cy - s - 1), (cx + s + 1, cy),
                                                  (cx, cy + s + 1), (cx - s - 1, cy)])
            pygame.draw.polygon(surf, col, [(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)])
            pygame.draw.polygon(surf, (250, 250, 250), [(cx, cy - s), (cx + s, cy),
                                                        (cx, cy + s), (cx - s, cy)], 1)
            if self.tile >= 26:
                T.draw_text(surf, t.objective_name, (cx, r.bottom + 1), 11,
                            (235, 228, 200), anchor="midtop")

    def _draw_forts(self, surf: pygame.Surface) -> None:
        """野战工事：地块底部的归属色进度条（长度 = 工事等级）。"""
        ts = self.tile
        if ts < 8:
            return
        for t in self._fort_tiles():
            r = self.grid_to_rect(t.x, t.y)
            if not self.rect.colliderect(r):
                continue
            col = T.side_color(t.fort_owner, "glow")
            w = max(2, int((ts - 4) * min(3, t.fort) / 3.0))
            pygame.draw.rect(surf, (8, 11, 16), (r.x + 1, r.bottom - 5, ts - 2, 4))
            pygame.draw.rect(surf, col, (r.x + 2, r.bottom - 4, w, 3))
            if ts >= 18:
                T.draw_text(surf, "⌂", (r.right - 3, r.y + 2), max(8, ts // 4), col,
                            anchor="topright")

    def _draw_facilities(self, surf: pygame.Surface) -> None:
        """绘制机场/港口/雷达站等固定设施标记。"""
        if self.tile < 16:
            return
        for t in self._facility_tiles():
            r = self.grid_to_rect(t.x, t.y)
            if not self.rect.colliderect(r):
                continue
            col = T.side_color(t.owner, "edge") if t.owner is not Side.NEUTRAL else (170, 176, 186)
            col = T.mix(col, (255, 255, 255), 0.25)
            s = max(3, self.tile // 5)
            cx, cy = r.x + s + 1, r.y + s + 1
            if t.facility is Facility.AIRBASE:
                pygame.draw.line(surf, col, (cx - s, cy), (cx + s, cy), 2)
                pygame.draw.line(surf, col, (cx, cy - s), (cx, cy + s), 1)
            elif t.facility is Facility.PORT:
                pygame.draw.arc(surf, col, pygame.Rect(cx - s, cy - s, s * 2, s * 2), 0.4, 2.7, 2)
                pygame.draw.line(surf, col, (cx, cy - s), (cx, cy + s // 2), 2)
            elif t.facility is Facility.RADAR_SITE:
                pygame.draw.arc(surf, col, pygame.Rect(cx - s, cy - s, s * 2, s * 2), 0.7, 2.4, 2)
                pygame.draw.circle(surf, col, (cx, cy + s // 2), 1)
            elif t.facility is Facility.HQ:
                pygame.draw.line(surf, col, (cx - s // 2, cy - s), (cx - s // 2, cy + s), 1)
                pygame.draw.polygon(surf, col, [(cx - s // 2, cy - s), (cx + s, cy - s // 2),
                                                (cx - s // 2, cy)])
            elif t.facility is Facility.DEPOT:
                pygame.draw.rect(surf, col, pygame.Rect(cx - s, cy - s // 2, s * 2, s), 1)
                pygame.draw.line(surf, col, (cx - s, cy - s // 2), (cx + s, cy + s // 2), 1)
            elif t.facility is Facility.SAM_SITE:
                pygame.draw.polygon(surf, col, [(cx, cy - s), (cx + s, cy + s), (cx - s, cy + s)], 1)
            elif t.facility is Facility.CITY:
                pygame.draw.circle(surf, col, (cx, cy), max(1, s // 2), 1)

    def _draw_reachable(self, surf: pygame.Surface, reachable: dict | None) -> None:
        if not reachable:
            return
        s = pygame.Surface((self.rect.width, self.rect.height), pygame.SRCALPHA)
        for (x, y), cost in reachable.items():
            r = self.grid_to_rect(x, y)
            rr = pygame.Rect(r.x - self.rect.x, r.y - self.rect.y, r.width, r.height)
            if not s.get_rect().colliderect(rr):
                continue
            s.fill((70, 150, 200, 34), rr)
        surf.blit(s, self.rect.topleft)

    def _draw_path(self, surf: pygame.Surface, path: list | None) -> None:
        if not path or len(path) < 2:
            return
        pts = [self.grid_center(x, y) for (x, y) in path]
        pygame.draw.lines(surf, (150, 230, 255), False, pts, 2)
        for p in pts[1:-1]:
            pygame.draw.circle(surf, (200, 245, 255), p, 2)
        pygame.draw.circle(surf, (255, 255, 255), pts[-1], max(3, self.tile // 6), 1)

    def _draw_missiles(self, surf: pygame.Surface) -> None:
        for m in self.state.missiles:
            a = self.grid_center(*m.origin)
            b = self.grid_center(*m.target_pos)
            col = (255, 150, 90) if not m.is_hypersonic else (255, 110, 220)
            # 虚线
            steps = 10
            for i in range(steps):
                if i % 2:
                    continue
                t0, t1 = i / steps, (i + 1) / steps
                p0 = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
                p1 = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
                pygame.draw.line(surf, col, p0, p1, 1)
            pygame.draw.circle(surf, col, b, 3, 1)
            T.draw_text(surf, f"{m.eta}", (b[0] + 5, b[1] - 12), 11, col, bold=True)

    # -- 单位 -------------------------------------------------------------
    def _draw_units(self, surf: pygame.Surface) -> None:
        st = self.state
        visible: list[tuple[Unit, Detection]] = []
        for u in st.units:
            if not u.alive:
                continue
            if u.domain is Domain.SPACE:
                continue
            if u.side is st.player_side:
                lvl = Detection.TRACKED
            else:
                lvl = st.known_level(st.player_side, u)
                if lvl is Detection.NONE:
                    continue
            visible.append((u, lvl))

        visible.sort(key=lambda p: (p[0].domain is Domain.AIR, p[0].side is not st.player_side))
        # 同一格可能停驻多架航空兵（机场停机坪），或地面部队 + 空中支援同处。
        # 画法：地面单位占主体位置，空中单位错位到右上角，
        # 数量标注只统计空中单位——「×N」指的是同格几架飞机。
        groups: dict[tuple[int, int], list] = {}
        for u, lvl in visible:
            groups.setdefault((u.x, u.y), []).append((u, lvl))
        for pos, items in groups.items():
            r = self.grid_to_rect(*pos)
            if not self.rect.colliderect(r.inflate(self.tile * 2, self.tile * 2)):
                continue
            ground_items = [(u, l) for u, l in items if u.domain is not Domain.AIR]
            air_items = [(u, l) for u, l in items if u.domain is Domain.AIR]
            # 地面单位：最多一个，居中绘制
            for i, (u, lvl) in enumerate(ground_items[:1]):
                self._draw_unit_token(surf, u, r, lvl,
                                      stacked=len(ground_items) + len(air_items) > 1)
            # 空中单位：错位到格子右上角
            for i, (u, lvl) in enumerate(air_items):
                rr = r.move(-i * 3, -i * 3)
                self._draw_unit_token(surf, u, rr, lvl, stacked=True)
            if len(air_items) > 1:
                T.draw_text(surf, f"×{len(air_items)}", (r.right - 1, r.y + 1), 11,
                            T.mix(T.AMBER, T.WHITE, 0.4), anchor="topright", bold=True)

    def _draw_unit_token(self, surf: pygame.Surface, u: Unit, r: pygame.Rect,
                         lvl: Detection, stacked: bool = False) -> None:
        ts = self.tile
        pad = max(1, ts // 12)
        body = r.inflate(-pad * 2, -pad * 2)
        cols = T.SIDE_COLORS[u.side]
        selected = self.state.selected_uid == u.uid

        if lvl is Detection.CONTACT:
            pygame.draw.rect(surf, (26, 30, 38), body, border_radius=2)
            pygame.draw.rect(surf, (150, 120, 90), body, width=1, border_radius=2)
            T.draw_text(surf, "?", body.center, max(9, ts // 2),
                        T.mix(T.AMBER, T.WHITE, 0.3),
                        bold=True, anchor="center")
            return

        # 底板（阴影 + 填充 + 边框 + 兵种图标）按 (阵营, 兵种, 尺寸) 缓存：
        # 这些像素在整局里几乎不变，重画是纯粹的浪费。
        sprite = self._token_sprite(u, body.width, body.height)
        if sprite is not None:
            surf.blit(sprite, (body.x - 1, body.y - 1))
        else:
            self._paint_token_body(surf, u, body, cols)

        # 血条
        if body.height >= 12:
            hb = pygame.Rect(body.x + 1, body.bottom - 3, body.width - 2, 2)
            pygame.draw.rect(surf, (18, 20, 26), hb)
            w = int(hb.width * u.hp_ratio)
            if w > 0:
                pygame.draw.rect(surf, T.hp_color(u.hp_ratio), pygame.Rect(hb.x, hb.y, w, hb.height))

        # 名称（放大后显示）
        if ts >= 34:
            T.draw_text(surf, u.template.short, (r.centerx, r.bottom + 1), 11,
                        cols["text"], anchor="midtop")
        # 状态角标
        if u.template.jam_strength > 0 and u.jam_on:
            pygame.draw.circle(surf, T.PURPLE, (body.right - 3, body.y + 3), 2)
        if u.fired:
            pygame.draw.circle(surf, (120, 120, 130), (body.x + 3, body.y + 3), 2)

        if selected:
            self._select_corners(surf, r, (255, 255, 255))

    # -- 棋子精灵缓存 -----------------------------------------------------
    def _token_epoch(self) -> tuple:
        """棋子精灵的失效指纹（主题色变化时必须重画）。"""
        return (self.tile, T.current_theme().id)

    def _token_sprite(self, u: Unit, w: int, h: int) -> pygame.Surface | None:
        """取棋子的缓存底板；缓存未命中时烘焙一张。"""
        epoch = self._token_epoch()
        if self._token_epoch_cache != epoch:
            self._token_cache.clear()
            self._token_epoch_cache = epoch
        # 雷达发射时的边框会变色，这类状态不进缓存
        key = (u.side.value, u.template.unit_class.value,
               u.domain is Domain.AIR, u.template.is_radar_emitter and u.radar_on,
               w, h)
        surf = self._token_cache.get(key)
        if surf is not None:
            return surf
        cols = T.SIDE_COLORS[u.side]
        # 精灵比 body 大 2px：阴影向右下各偏 1px，不能被裁掉
        surf = pygame.Surface((w + 2, h + 2), pygame.SRCALPHA)
        # 把 body 局部坐标搬到精灵上（body 的原点即精灵内偏移 1px）
        self._paint_token_body(surf, u, pygame.Rect(1, 1, w, h), cols)
        if len(self._token_cache) > 400:
            self._token_cache.clear()
        self._token_cache[key] = surf
        return surf

    def _paint_token_body(self, surf: pygame.Surface, u: Unit, body: pygame.Rect,
                          cols: dict) -> None:
        """绘制棋子底板：阴影 + 填充 + 边框 + 兵种图标。"""
        ts = self.tile
        shadow = body.move(1, 1)
        pygame.draw.rect(surf, (0, 0, 0), shadow, border_radius=3)
        fill = cols["fill"]
        if u.domain is Domain.AIR:
            fill = T.mix(fill, (140, 150, 170), 0.12)
        pygame.draw.rect(surf, fill, body, border_radius=3)
        edge = cols["edge"]
        if u.template.is_radar_emitter and u.radar_on and self.state.player_side is u.side:
            edge = T.mix(edge, (120, 255, 190), 0.5)
        pygame.draw.rect(surf, edge, body, width=1, border_radius=3)

        # 兵种图标
        icon_col = T.mix(cols["text"], (255, 255, 255), 0.22)
        self.draw_class_icon(surf, body, u.template.unit_class, icon_col)

    def _select_corners(self, surf: pygame.Surface, r: pygame.Rect, col) -> None:
        L = max(4, r.width // 4)
        pts = [
            [(r.x, r.y + L), (r.x, r.y), (r.x + L, r.y)],
            [(r.right - L, r.y), (r.right, r.y), (r.right, r.y + L)],
            [(r.x, r.bottom - L), (r.x, r.bottom), (r.x + L, r.bottom)],
            [(r.right - L, r.bottom), (r.right, r.bottom), (r.right, r.bottom - L)],
        ]
        for p in pts:
            pygame.draw.lines(surf, col, False, p, 2)

    def _draw_reticle(self, surf: pygame.Surface, r: pygame.Rect) -> None:
        col = (255, 90, 90)
        m = max(2, r.width // 6)
        rr = r.inflate(m, m)
        L = max(3, rr.width // 4)
        for (x, y, dx, dy) in [(rr.x, rr.y, 1, 1), (rr.right, rr.y, -1, 1),
                               (rr.x, rr.bottom, 1, -1), (rr.right, rr.bottom, -1, -1)]:
            pygame.draw.line(surf, col, (x, y), (x + dx * L, y), 2)
            pygame.draw.line(surf, col, (x, y), (x, y + dy * L), 2)
        pygame.draw.circle(surf, col, rr.center, max(4, rr.width // 3), 1)

    # ------------------------------------------------------------------ #
    # 兵种图标（矢量绘制，烘焙后缓存为位图）
    # ------------------------------------------------------------------ #
    def draw_class_icon(self, surf: pygame.Surface, rect: pygame.Rect,
                        cls: UnitClass, color) -> None:
        """绘制兵种图标。

        优先使用图标包提供的位图（core/icons.py）；没有位图时回退到内置
        矢量图形。矢量图形会被烘焙成位图缓存——每个兵种 + 每个尺寸只画一次。
        """
        icon = icon_bitmap(cls, color, min(rect.width, rect.height))
        if icon is not None:
            surf.blit(icon, (rect.centerx - icon.get_width() // 2,
                             rect.centery - icon.get_height() // 2))
            return
        icon = self._vector_icon_bitmap(cls, color, min(rect.width, rect.height))
        if icon is not None:
            surf.blit(icon, (rect.centerx - icon.get_width() // 2,
                             rect.centery - icon.get_height() // 2))
            return
        self._draw_vector_class_icon(surf, rect, cls, color)

    def _vector_icon_bitmap(self, cls: UnitClass, color, size: int):
        """烘焙并缓存矢量兵种图形。

        矢量绘制是棋子渲染中最贵的一环（多边形 + 圆弧，每次十几条指令）。
        同一兵种在同一尺寸下的图形完全相同，烘焙成位图后每次只做一次 blit。
        """
        if size <= 0:
            return None
        key = (cls.value, tuple(color), size)
        cached = self._vector_icons.get(key)
        if cached is not None:
            return cached
        try:
            bmp = pygame.Surface((size, size), pygame.SRCALPHA)
            self._draw_vector_class_icon(bmp, pygame.Rect(0, 0, size, size), cls, color)
        except Exception:
            return None
        if len(self._vector_icons) > 200:
            self._vector_icons.clear()
        self._vector_icons[key] = bmp
        return bmp

    def _draw_vector_class_icon(self, surf: pygame.Surface, rect: pygame.Rect,
                                cls: UnitClass, color) -> None:
        """内置矢量兵种图形——不需要任何素材文件。"""
        draw_vector_class_icon(surf, rect, cls, color)

    # ------------------------------------------------------------------ #
    # 特效
    # ------------------------------------------------------------------ #
    def _draw_fx(self, surf: pygame.Surface) -> None:
        # 特效层复用同一张表面：每帧新建一张全屏 SRCALPHA 是纯粹的浪费
        if self._fx_layer is None or self._fx_layer.get_size() != self.rect.size:
            self._fx_layer = pygame.Surface(self.rect.size, pygame.SRCALPHA)
        s = self._fx_layer
        s.fill((0, 0, 0, 0))
        for f in self.state.fx:
            p = max(0.0, min(1.0, 1.0 - f.ttl / max(0.001, f.life)))
            a = self.grid_center(*f.a)
            a = (a[0] - self.rect.x, a[1] - self.rect.y)
            if f.kind == "explosion":
                rad = int(self.tile * (0.3 + p * 1.5))
                alpha = int(220 * (1 - p))
                pygame.draw.circle(s, (*f.color, alpha), a, rad, max(1, self.tile // 8))
                pygame.draw.circle(s, (255, 240, 190, alpha // 2), a, max(1, rad // 3))
                if f.text:
                    T.draw_text(surf, f.text, (a[0] + self.rect.x, a[1] + self.rect.y - rad - 12),
                                11, (255, 210, 160), anchor="center", bold=True)
            elif f.kind == "tracer" and f.b:
                b = self.grid_center(*f.b)
                b = (b[0] - self.rect.x, b[1] - self.rect.y)
                ex = int(a[0] + (b[0] - a[0]) * min(1.0, p * 1.6))
                ey = int(a[1] + (b[1] - a[1]) * min(1.0, p * 1.6))
                pygame.draw.line(s, (*f.color, int(230 * (1 - p))), a, (ex, ey), 2)
            elif f.kind == "missile" and f.b:
                b = self.grid_center(*f.b)
                b = (b[0] - self.rect.x, b[1] - self.rect.y)
                ex = int(a[0] + (b[0] - a[0]) * p)
                ey = int(a[1] + (b[1] - a[1]) * p)
                pygame.draw.line(s, (*f.color, 160), a, (ex, ey), 1)
                pygame.draw.circle(s, (*f.color, 240), (ex, ey), 2)
            elif f.kind == "intercept":
                rad = int(self.tile * (0.3 + p * 0.9))
                pygame.draw.circle(s, (*f.color, int(230 * (1 - p))), a, rad, 2)
                for ang in range(0, 360, 60):
                    dx = math.cos(math.radians(ang)) * rad
                    dy = math.sin(math.radians(ang)) * rad
                    pygame.draw.line(s, (*f.color, int(200 * (1 - p))), a,
                                     (int(a[0] + dx), int(a[1] + dy)), 1)
            elif f.kind == "ping":
                rad = int(self.tile * (0.2 + p * 1.2))
                pygame.draw.circle(s, (*f.color, int(200 * (1 - p))), a, max(1, rad), 2)
            elif f.kind == "move" and f.b:
                b = self.grid_center(*f.b)
                b = (b[0] - self.rect.x, b[1] - self.rect.y)
                pygame.draw.line(s, (*f.color, int(180 * (1 - p))), a, b, 1)
        surf.blit(s, self.rect.topleft)

    # ------------------------------------------------------------------ #
    # 逐帧战斗动画
    # ------------------------------------------------------------------ #
    def _draw_anim(self, surf: pygame.Surface) -> None:
        """绘制动画轨道中的事件。

        动画只在渲染层发生，不参与任何规则结算——关掉动画不影响战局。
        所有绘制都在地图视口的裁剪范围内进行。
        """
        track = getattr(self.state, "anim", None)
        if track is None or not track.events:
            return

        layer = self._anim_layer
        if layer is None or layer.get_size() != self.rect.size:
            layer = pygame.Surface(self.rect.size, pygame.SRCALPHA)
            self._anim_layer = layer
        else:
            layer.fill((0, 0, 0, 0))
        for e in track.events:
            p = 1.0 - max(0.0, min(1.0, e.ttl / max(0.001, e.life)))
            a = self.grid_center(e.a[0], e.a[1])
            a = (int(a[0] - self.rect.x), int(a[1] - self.rect.y))
            col = e.color

            if e.kind == "trail" and e.b is not None:
                # 弹道拖尾：彗尾状，头部最亮
                pts = anim_mod.trail_points(e.a, e.b, p)
                for i, pt in enumerate(pts):
                    q = self.grid_center(pt[0], pt[1])
                    q = (int(q[0] - self.rect.x), int(q[1] - self.rect.y))
                    fade = (i + 1) / len(pts)
                    alpha = int(210 * fade * (1.0 - p * 0.35))
                    if alpha <= 3:
                        continue
                    rad = max(1, int(self.tile * 0.055 * fade))
                    pygame.draw.circle(layer, (*col, alpha), q, rad)
                # 弹头
                head = self.grid_center(*pts[-1])
                head = (int(head[0] - self.rect.x), int(head[1] - self.rect.y))
                pygame.draw.circle(layer, (*col, 240), head, max(1, int(self.tile * 0.07)))

            elif e.kind == "burst":
                # 爆炸：扩散冲击环 + 火花射线 + 闪光核心
                rad = max(2, int(self.tile * anim_mod.burst_radius(p, e.size)))
                alpha = int(230 * (1.0 - p))
                if alpha <= 3:
                    continue
                pygame.draw.circle(layer, (*col, alpha), a, rad, max(1, int(self.tile * 0.07)))
                inner = max(1, int(rad * 0.55))
                pygame.draw.circle(layer, (255, 240, 210, int(alpha * 0.55)), a, inner)
                for dx, dy in anim_mod.burst_sparks(e.seed, 8):
                    tip = (int(a[0] + dx * rad), int(a[1] + dy * rad))
                    pygame.draw.line(layer, (*col, int(alpha * 0.8)), a, tip, 1)

            elif e.kind == "intercept":
                # 拦截：环形涟漪 + 六向射线
                rad = max(2, int(self.tile * (0.25 + p * 0.85)))
                alpha = int(235 * (1.0 - p))
                if alpha <= 3:
                    continue
                pygame.draw.circle(layer, (*col, alpha), a, rad, 2)
                for ang in range(0, 360, 60):
                    dx = math.cos(math.radians(ang)) * rad
                    dy = math.sin(math.radians(ang)) * rad
                    pygame.draw.line(layer, (*col, int(alpha * 0.85)), a,
                                     (int(a[0] + dx), int(a[1] + dy)), 1)

            elif e.kind == "smoke":
                # 烟幕：数团缓慢膨胀、逐渐淡出
                rad = max(2, int(self.tile * (0.22 + p * 0.42) * e.size))
                alpha = int(165 * (1.0 - p * 0.85))
                if alpha <= 3:
                    continue
                for i in range(3):
                    ox, oy = anim_mod.smoke_offset(e.seed, i)
                    q = (int(a[0] + ox * self.tile), int(a[1] + oy * self.tile))
                    pygame.draw.circle(layer, (*col, alpha // 2), q, rad)

            elif e.kind == "flash":
                # 命中闪光：短促的高亮圆
                rad = max(2, int(self.tile * (0.35 + p * 0.3)))
                alpha = int(215 * (1.0 - p))
                if alpha <= 3:
                    continue
                pygame.draw.circle(layer, (*col, alpha), a, rad)

            elif e.kind == "ping":
                # 探测脉冲：向外扩散的圆环
                rad = max(2, int(self.tile * (0.2 + p * 1.1)))
                alpha = int(200 * (1.0 - p))
                if alpha <= 3:
                    continue
                pygame.draw.circle(layer, (*col, alpha), a, rad, 2)

            elif e.kind == "move" and e.b is not None:
                # 机动轨迹：移动方向的虚线
                b = self.grid_center(e.b[0], e.b[1])
                b = (int(b[0] - self.rect.x), int(b[1] - self.rect.y))
                alpha = int(190 * (1.0 - p))
                if alpha <= 3:
                    continue
                steps = 7
                for i in range(steps):
                    t0 = i / steps
                    t1 = t0 + 0.5 / steps
                    p0 = (int(a[0] + (b[0] - a[0]) * t0), int(a[1] + (b[1] - a[1]) * t0))
                    p1 = (int(a[0] + (b[0] - a[0]) * t1), int(a[1] + (b[1] - a[1]) * t1))
                    pygame.draw.line(layer, (*col, alpha), p0, p1, 1)

            elif e.kind == "label":
                # 飘字：向上飘并淡出
                alpha = int(240 * (1.0 - p * p))
                if alpha <= 3 or not e.text:
                    continue
                pos = (a[0], a[1] - int(p * self.tile * 0.9))
                T.draw_text(layer, e.text, pos, 12, col, anchor="center", bold=True,
                            alpha=alpha)

        layer.set_clip(self.rect)
        surf.blit(layer, self.rect.topleft)

    # ------------------------------------------------------------------ #
    # 缩略图
    # ------------------------------------------------------------------ #
    def draw_minimap(self, surf: pygame.Surface, rect: pygame.Rect) -> None:
        md = self.state.map
        cell = max(1, min(rect.width / md.width, rect.height / md.height))
        ox = rect.x + (rect.width - md.width * cell) / 2
        oy = rect.y + (rect.height - md.height * cell) / 2
        for t in md.iter_tiles():
            base = T.TERRAIN_COLORS.get(t.terrain, T.TERRAIN_COLORS[Terrain.PLAIN])
            col = T.elevation_shade(base, t.elevation)
            pygame.draw.rect(surf, col, pygame.Rect(int(ox + t.x * cell), int(oy + t.y * cell),
                                                    max(1, int(cell)), max(1, int(cell))))
        for t in md.iter_tiles():
            if t.objective:
                pygame.draw.rect(surf, T.side_color(t.owner, "edge") if t.owner is not Side.NEUTRAL
                                 else T.AMBER,
                                 pygame.Rect(int(ox + t.x * cell), int(oy + t.y * cell),
                                             max(2, int(cell)), max(2, int(cell))))
        for u in self.state.units:
            if not u.alive or u.domain is Domain.SPACE:
                continue
            if u.side is not self.state.player_side and \
                    self.state.known_level(self.state.player_side, u) is Detection.NONE:
                continue
            pygame.draw.rect(surf, T.side_color(u.side),
                             pygame.Rect(int(ox + u.x * cell), int(oy + u.y * cell),
                                         max(2, int(cell)), max(2, int(cell))))
        # 视窗框
        md_w = md.width * self.tile
        vx = (-self.ox / md_w) if md_w else 0
        vy = (-self.oy / (md.height * self.tile)) if md.height else 0
        vw = self.rect.width / md_w if md_w else 1
        vh = self.rect.height / (md.height * self.tile) if md.height else 1
        pygame.draw.rect(surf, (255, 255, 255), pygame.Rect(
            int(ox + vx * md.width * cell), int(oy + vy * md.height * cell),
            max(4, int(vw * md.width * cell)), max(4, int(vh * md.height * cell))), 1)


def draw_vector_class_icon(surf: pygame.Surface, rect: pygame.Rect,
                           cls: UnitClass, color) -> None:
    """内置矢量兵种图形——不需要任何素材文件。

    模块级函数：渲染层与图标包生成工具共用同一份代码，
    避免「工具生成的图标」与「游戏里画的图标」不一致。
    """
    cx, cy = rect.center
    s = min(rect.width, rect.height)
    h = s / 2
    w = 1 if s < 16 else 2
    c = color

    def P(px: float, py: float) -> tuple[int, int]:
        return (int(cx + px * h), int(cy + py * h))

    if cls in (UnitClass.MBT, UnitClass.IFV):
        k = 0.62 if cls is UnitClass.MBT else 0.5
        pygame.draw.rect(surf, c, pygame.Rect(int(cx - h * k), int(cy - h * 0.34),
                                              int(h * 1.18), int(h * 0.64)), 1)
        pygame.draw.line(surf, c, P(0.2, 0), P(0.86, 0), w)
    elif cls is UnitClass.INFANTRY:
        pygame.draw.circle(surf, c, P(0, -0.25), max(1, int(h * 0.22)), 1)
        pygame.draw.line(surf, c, P(0, -0.02), P(0, 0.5), w)
        pygame.draw.line(surf, c, P(-0.35, 0.12), P(0.35, 0.12), w)
    elif cls is UnitClass.ARTY:
        pygame.draw.rect(surf, c, pygame.Rect(int(cx - h * 0.6), int(cy - h * 0.2),
                                              int(h * 1.2), int(h * 0.5)), 1)
        pygame.draw.line(surf, c, P(0.1, -0.1), P(0.9, -0.62), w)
    elif cls is UnitClass.MLRS:
        pygame.draw.rect(surf, c, pygame.Rect(int(cx - h * 0.6), int(cy - h * 0.1),
                                              int(h * 1.2), int(h * 0.45)), 1)
        for i in (-0.35, 0.0, 0.35):
            pygame.draw.line(surf, c, P(i, -0.15), P(i + 0.25, -0.7), w)
    elif cls in (UnitClass.SAM, UnitClass.AAA):
        pygame.draw.polygon(surf, c, [P(0, -0.65), P(0.35, 0.25), P(-0.35, 0.25)], w)
        pygame.draw.line(surf, c, P(-0.55, 0.55), P(0.55, 0.55), w)
    elif cls is UnitClass.EW:
        for i, rr in enumerate((0.35, 0.6, 0.85)):
            rr_r = pygame.Rect(0, 0, int(h * rr * 2), int(h * rr * 2))
            rr_r.center = (cx, cy + int(h * 0.45))
            pygame.draw.arc(surf, c, rr_r, 0.6, 2.55, w)
        pygame.draw.circle(surf, c, P(0, 0.45), max(1, int(h * 0.12)))
    elif cls is UnitClass.RADAR:
        pygame.draw.arc(surf, c, pygame.Rect(int(cx - h * 0.5), int(cy - h * 0.55),
                                             int(h * 0.9), int(h * 1.1)), 0.5, 2.2, w)
        pygame.draw.line(surf, c, P(-0.1, 0.1), P(0.1, 0.7), w)
    elif cls is UnitClass.HQ:
        pygame.draw.polygon(surf, c, [P(0, -0.6), P(0.6, 0.3), P(-0.6, 0.3)], w)
        pygame.draw.rect(surf, c, pygame.Rect(int(cx - h * 0.5), int(cy + h * 0.3),
                                              int(h), int(h * 0.3)), 1)
    elif cls in (UnitClass.DESTROYER, UnitClass.FRIGATE, UnitClass.CARRIER):
        k = 0.72 if cls is UnitClass.CARRIER else 0.6
        pygame.draw.polygon(surf, c, [P(-k, -0.35), P(k * 0.55, -0.35), P(k, 0), P(k * 0.55, 0.4),
                                      P(-k, 0.4)], w)
        pygame.draw.line(surf, c, P(-0.15, -0.35), P(-0.15, 0.4), w)
    elif cls is UnitClass.SUB:
        r_ = pygame.Rect(0, 0, int(h * 1.5), int(h * 0.62))
        r_.center = (cx, cy)
        pygame.draw.ellipse(surf, c, r_, 1)
        pygame.draw.line(surf, c, P(-0.05, -0.28), P(-0.05, -0.55), w)
    elif cls in (UnitClass.FIGHTER, UnitClass.UAV):
        pygame.draw.polygon(surf, c, [P(0, -0.7), P(0.22, 0.1), P(0.75, 0.45),
                                      P(0.0, 0.28), P(-0.75, 0.45), P(-0.22, 0.1)], w)
    elif cls is UnitClass.BOMBER:
        pygame.draw.polygon(surf, c, [P(0, -0.55), P(0.28, 0.05), P(0.95, 0.4),
                                      P(0.25, 0.35), P(0.3, 0.62), P(0, 0.45),
                                      P(-0.3, 0.62), P(-0.25, 0.35), P(-0.95, 0.4),
                                      P(-0.28, 0.05)], w)
    elif cls is UnitClass.AEW:
        pygame.draw.polygon(surf, c, [P(0, -0.5), P(0.3, 0.15), P(0.8, 0.4),
                                      P(0, 0.25), P(-0.8, 0.4), P(-0.3, 0.15)], w)
        pygame.draw.circle(surf, c, P(0, -0.15), max(1, int(h * 0.18)), 1)
    elif cls is UnitClass.TANKER:
        pygame.draw.polygon(surf, c, [P(0, -0.45), P(0.2, 0.1), P(0.85, 0.35),
                                      P(0, 0.22), P(-0.85, 0.35), P(-0.2, 0.1)], w)
        pygame.draw.line(surf, c, P(-0.8, 0.45), P(0.8, 0.45), w)
    elif cls is UnitClass.SATELLITE:
        pygame.draw.rect(surf, c, pygame.Rect(int(cx - h * 0.25), int(cy - h * 0.25),
                                              int(h * 0.5), int(h * 0.5)), 1)
        pygame.draw.line(surf, c, P(-0.25, 0), P(-0.9, 0), w)
        pygame.draw.line(surf, c, P(0.25, 0), P(0.9, 0), w)
    else:
        pygame.draw.rect(surf, c, rect.inflate(-4, -4), 1)


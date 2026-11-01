"""地图渲染与坐标变换。

负责：地形预渲染（缓存）、单位棋子与兵种图标、目标点、射程包线、
探测覆盖、导弹在飞轨迹、战斗特效、缩略图。
"""
from __future__ import annotations

import math

import pygame

from core.enums import Detection, Domain, Facility, Side, Terrain, UnitClass
from core.model import Tile, Unit

from . import theme as T

TILE_STEPS = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 32, 36, 40, 46, 52, 60]


class MapView:
    def __init__(self, rect: pygame.Rect, state):
        self.rect = pygame.Rect(rect)
        self.state = state
        self.tile = 22
        self.ox = 0.0
        self.oy = 0.0
        self._terrain_surf: pygame.Surface | None = None
        self._terrain_key: tuple | None = None
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
    # 地形层
    # ------------------------------------------------------------------ #
    def _build_terrain(self) -> None:
        md = self.state.map
        ts = self.tile
        surf = pygame.Surface((md.width * ts, md.height * ts))
        surf.fill(T.BG_MAP)

        for t in md.iter_tiles():
            x0, y0 = t.x * ts, t.y * ts
            base = T.TERRAIN_COLORS.get(t.terrain, T.TERRAIN_COLORS[Terrain.PLAIN])
            col = T.elevation_shade(base, t.elevation)
            # 山体阴影：按西北方向高差做明暗，形成柔和的地形起伏
            if t.terrain is not Terrain.WATER:
                # 中心差分求坡度，避免单侧差分产生条带状伪影
                l_ = md.at(t.x - 1, t.y)
                r_ = md.at(t.x + 1, t.y)
                u_ = md.at(t.x, t.y - 1)
                d_ = md.at(t.x, t.y + 1)
                gx = ((r_.elevation if r_ else t.elevation)
                      - (l_.elevation if l_ else t.elevation)) / 2.0
                gy = ((d_.elevation if d_ else t.elevation)
                      - (u_.elevation if u_ else t.elevation)) / 2.0
                dh = gx * 0.7 + gy * 0.7
                f = 1.0 + max(-0.13, min(0.13, dh / 2600.0))
                col = (max(0, min(255, int(col[0] * f))),
                       max(0, min(255, int(col[1] * f))),
                       max(0, min(255, int(col[2] * f))))
            r = pygame.Rect(x0, y0, ts, ts)
            pygame.draw.rect(surf, col, r)
            self._decorate(surf, t, r, col)

        # 海岸线：陆地与水域相邻处提亮一道细边
        if ts >= 10:
            shore = (150, 196, 214)
            for t in md.iter_tiles():
                if t.terrain is Terrain.WATER:
                    continue
                x0, y0 = t.x * ts, t.y * ts
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    n = md.at(t.x + dx, t.y + dy)
                    if n is None or n.terrain is not Terrain.WATER:
                        continue
                    if dx == 1:
                        pygame.draw.line(surf, shore, (x0 + ts - 1, y0), (x0 + ts - 1, y0 + ts))
                    elif dx == -1:
                        pygame.draw.line(surf, shore, (x0, y0), (x0, y0 + ts))
                    elif dy == 1:
                        pygame.draw.line(surf, shore, (x0, y0 + ts - 1), (x0 + ts, y0 + ts - 1))
                    else:
                        pygame.draw.line(surf, shore, (x0, y0), (x0 + ts, y0))

        # 等高线（每 600 米一级，仅在与邻格分级不同处描一道暗线）
        if ts >= 13:
            for t in md.iter_tiles():
                if t.terrain is Terrain.WATER:
                    continue
                band = t.elevation // 600
                x0, y0 = t.x * ts, t.y * ts
                if t.x + 1 < md.width:
                    n = md.at(t.x + 1, t.y)
                    if n is not None and n.terrain is not Terrain.WATER and n.elevation // 600 != band:
                        pygame.draw.line(surf, (12, 18, 24), (x0 + ts - 1, y0),
                                         (x0 + ts - 1, y0 + ts))
                if t.y + 1 < md.height:
                    n = md.at(t.x, t.y + 1)
                    if n is not None and n.terrain is not Terrain.WATER and n.elevation // 600 != band:
                        pygame.draw.line(surf, (12, 18, 24), (x0, y0 + ts - 1),
                                         (x0 + ts, y0 + ts - 1))

        # 网格：低对比度分隔线，避免干扰地形
        if self.show_grid and ts >= 12:
            for x in range(md.width + 1):
                pygame.draw.line(surf, T.GRID, (x * ts, 0), (x * ts, md.height * ts), 1)
            for y in range(md.height + 1):
                pygame.draw.line(surf, T.GRID, (0, y * ts), (md.width * ts, y * ts), 1)

        self._terrain_surf = surf
        self._terrain_key = (ts, self.show_grid, md.width, md.height, id(md))

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
        if self._terrain_key != (self.tile, self.show_grid, self.state.map.width,
                                 self.state.map.height, id(self.state.map)):
            self._build_terrain()
        md = self.state.map
        surf.set_clip(self.rect)
        pygame.draw.rect(surf, T.BG, self.rect)

        view = pygame.Rect(int(-self.ox), int(-self.oy),
                           self.rect.width + self.tile, self.rect.height + self.tile)
        src = pygame.Rect(max(0, view.x), max(0, view.y),
                          min(view.width, md.width * self.tile - max(0, view.x)),
                          min(view.height, md.height * self.tile - max(0, view.y)))
        if src.width > 0 and src.height > 0:
            surf.blit(self._terrain_surf, (self.rect.x + self.ox + src.x, self.rect.y + self.oy + src.y), src)
        # 地图边框：让居中的留白看起来是刻意的画框
        frame = pygame.Rect(int(self.rect.x + self.ox), int(self.rect.y + self.oy),
                            md.width * self.tile, md.height * self.tile)
        pygame.draw.rect(surf, T.BORDER_HI, frame, 1)

        self._draw_forts(surf)
        self._draw_overlay(surf)
        self._draw_objectives(surf)
        self._draw_facilities(surf)
        self._draw_reachable(surf, reachable)
        self._draw_path(surf, path)
        self._draw_missiles(surf)
        self._draw_units(surf)
        self._draw_fx(surf)

        if hover:
            r = self.grid_to_rect(*hover)
            pygame.draw.rect(surf, (150, 200, 240), r, width=1)
        if pending_target is not None and pending_target.domain is not Domain.SPACE:
            r = self.grid_to_rect(pending_target.x, pending_target.y)
            self._draw_reticle(surf, r)

        surf.set_clip(None)

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

    def _draw_objectives(self, surf: pygame.Surface) -> None:
        for t in self.state.map.iter_tiles():
            if not t.objective:
                continue
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
                T.draw_text(surf, t.objective_name, (cx, r.bottom + 1), 10,
                            (235, 228, 200), anchor="midtop")

    def _draw_forts(self, surf: pygame.Surface) -> None:
        """野战工事：地块底部的归属色进度条（长度 = 工事等级）。"""
        ts = self.tile
        if ts < 8:
            return
        for t in self.state.map.iter_tiles():
            if t.fort <= 0 or t.fort_owner is Side.NEUTRAL:
                continue
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
        for t in self.state.map.iter_tiles():
            if t.facility is Facility.NONE:
                continue
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
            T.draw_text(surf, f"{m.eta}", (b[0] + 5, b[1] - 12), 10, col, bold=True)

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
        # 同一格可能停驻多架航空兵（机场停机坪），做轻微错位并标注数量
        groups: dict[tuple[int, int], list] = {}
        for u, lvl in visible:
            groups.setdefault((u.x, u.y), []).append((u, lvl))
        for pos, items in groups.items():
            r = self.grid_to_rect(*pos)
            if not self.rect.colliderect(r.inflate(self.tile * 2, self.tile * 2)):
                continue
            for i, (u, lvl) in enumerate(items):
                rr = r.move(i * 2, i * 2) if len(items) > 1 else r
                self._draw_unit_token(surf, u, rr, lvl, stacked=len(items) > 1)
            if len(items) > 1:
                T.draw_text(surf, f"×{len(items)}", (r.right - 1, r.y + 1), 9,
                            (255, 235, 180), anchor="topright", bold=True)

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
            T.draw_text(surf, "?", body.center, max(9, ts // 2), (220, 180, 120),
                        bold=True, anchor="center")
            return

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

        # 血条
        if body.height >= 12:
            hb = pygame.Rect(body.x + 1, body.bottom - 3, body.width - 2, 2)
            pygame.draw.rect(surf, (18, 20, 26), hb)
            w = int(hb.width * u.hp_ratio)
            if w > 0:
                pygame.draw.rect(surf, T.hp_color(u.hp_ratio), pygame.Rect(hb.x, hb.y, w, hb.height))

        # 名称（放大后显示）
        if ts >= 34:
            T.draw_text(surf, u.template.short, (r.centerx, r.bottom + 1), 10,
                        cols["text"], anchor="midtop")
        # 状态角标
        if u.template.jam_strength > 0 and u.jam_on:
            pygame.draw.circle(surf, T.PURPLE, (body.right - 3, body.y + 3), 2)
        if u.fired:
            pygame.draw.circle(surf, (120, 120, 130), (body.x + 3, body.y + 3), 2)

        if selected:
            self._select_corners(surf, r, (255, 255, 255))

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
    # 兵种图标（矢量绘制，无需素材）
    # ------------------------------------------------------------------ #
    def draw_class_icon(self, surf: pygame.Surface, rect: pygame.Rect,
                        cls: UnitClass, color) -> None:
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

    # ------------------------------------------------------------------ #
    # 特效
    # ------------------------------------------------------------------ #
    def _draw_fx(self, surf: pygame.Surface) -> None:
        s = pygame.Surface(self.rect.size, pygame.SRCALPHA)
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

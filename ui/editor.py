"""可视化编辑器：地图与武器库。

地图页：地形笔刷、海拔增减、设施/目标点放置、部队部署、属性编辑、保存为 JSON。
兵种页：编辑兵种基础属性与**每件武器的备弹设置**，保存回 armory.json。

所有编辑都直接作用于 JSON 文档结构，保留未知字段，便于手工继续维护。
"""
from __future__ import annotations

import json
import os

import pygame

from core.enums import Facility, Side, TERRAIN_CHARS, Terrain
from core.loader import MAX_MAP_SIZE, LoadError, load_scenario
from core import pkg as _pkg
from core import paths as _paths

from . import theme as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_DIR = os.path.join(ROOT, "data", "maps")
ARMORY = os.path.join(ROOT, "data", "units", "armory.json")
USER_DIR = _paths.user_writable_dir()

TERRAIN_ORDER = ["w", "c", "p", "f", "h", "m", "u", "d", "s"]
TERRAIN_NAME = {"w": "海域", "c": "滩涂", "p": "平原", "f": "林地", "h": "丘陵",
                "m": "山地", "u": "城区", "d": "荒漠", "s": "沼泽"}
FACILITY_ORDER = [Facility.AIRBASE, Facility.PORT, Facility.DEPOT,
                  Facility.RADAR_SITE, Facility.SAM_SITE, Facility.HQ, Facility.CITY]
TOOLS = [("terrain", "地形"), ("elev_up", "海拔+"), ("elev_dn", "海拔-"),
         ("facility", "设施"), ("fort", "工事"), ("objective", "目标点"),
         ("unit", "部署"), ("erase", "擦除"), ("select", "框选")]


# --------------------------------------------------------------------------- #
class TextInput:
    def __init__(self, rect: pygame.Rect, value: str, numeric: bool = False,
                 max_len: int = 40):
        self.rect = pygame.Rect(rect)
        self.value = str(value)
        self.numeric = numeric
        self.max_len = max_len
        self.active = False
        self._caret = 0.0

    def handle(self, e: pygame.event.Event) -> bool:
        if e.type != pygame.KEYDOWN:
            return False
        if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE, pygame.K_TAB):
            self.active = False
            return False
        if e.key == pygame.K_BACKSPACE:
            self.value = self.value[:-1]
            return True
        ch = e.unicode
        if ch and ch.isprintable() and len(self.value) < self.max_len:
            if self.numeric and not (ch.isdigit() or ch == "."):
                return True
            self.value += ch
            return True
        return True

    def as_int(self, default: int = 0) -> int:
        try:
            return int(float(self.value))
        except (TypeError, ValueError):
            return default

    def as_float(self, default: float = 0.0) -> float:
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return default

    def draw(self, s: pygame.Surface, mouse) -> None:
        hov = self.rect.collidepoint(mouse)
        pygame.draw.rect(s, T.PANEL_SUNK if not self.active else T.PANEL_HI,
                         self.rect, border_radius=3)
        pygame.draw.rect(s, T.CYAN if self.active else (T.BORDER_HI if hov else T.BORDER),
                         self.rect, 1, border_radius=3)
        T.draw_text(s, self.value or " ", (self.rect.x + 6, self.rect.centery), 11,
                    T.TEXT, anchor="midleft", mono=self.numeric)


# --------------------------------------------------------------------------- #
class Editor:
    PANEL_W = 340

    def __init__(self, screen: pygame.Surface):
        self.screen = screen
        self.w, self.h = screen.get_size()
        self.tab = "map"        # map / units / campaign
        self.status = ""
        self.error = ""
        self.quit = False
        # 战役编辑器（第三个标签页）——单独成模块，避免本文件过长
        from .campaign_editor import CampaignEditor
        self.campaign_editor = CampaignEditor(self)

        self.tile = 20
        self.ox = 0.0
        self.oy = 0.0
        self._panning = False
        self._dragging = None

        self.map_files = self._list_maps()
        self.file_index = 0
        self.path = ""

        self.tool = "terrain"
        self.brush_terrain = "p"
        self.brush_facility = Facility.AIRBASE
        self.brush_fort = 1
        self.brush_side = Side.BLUE
        self.brush_template = "m1a2"
        self.unit_scroll = 0
        self.tmpl_scroll = 0
        self.tmpl_index = 0
        self.pack_filter = ""          # 预设编队筛选（空 = 全部）
        self.picker_open = False       # 武器选择器
        self.picker_scroll = 0
        self.picker_hover = ""
        self.undo_stack: list = []     # 地图撤销栈（快照）
        self.redo_stack: list = []
        self._stroke_pending = False   # 本次绘制笔画的快照尚未入栈

        self.doc: dict = {}
        self.terr: list[list[str]] = []
        self.elev: list[list[int]] = []
        self.fac: dict[tuple[int, int], dict] = {}
        self.fort: dict[tuple[int, int], dict] = {}
        self.obj: dict[tuple[int, int], dict] = {}
        self.units: list[dict] = []
        self.hover: tuple[int, int] | None = None
        self.focus: TextInput | None = None
        self.inputs: dict[str, TextInput] = {}
        self.buttons: list[tuple[pygame.Rect, str, object]] = []

        # -- 框选（多选）--
        self.sel_anchor: tuple[int, int] | None = None   # 框选起点
        self.sel_rect: tuple[int, int, int, int] | None = None  # (x0,y0,x1,y1) 含端点
        self.sel_cells: set[tuple[int, int]] = set()     # 选中的格子集合
        self._sel_dragging = False

        self.armory_doc: dict = {}
        self.armory_path = ARMORY
        self._load_armory()
        if self.map_files:
            self.open_map(self.map_files[0])
        else:
            self.new_map(30, 30)

    # ------------------------------------------------------------------ #
    # 文件
    # ------------------------------------------------------------------ #
    def _list_maps(self) -> list[str]:
        if not os.path.isdir(MAP_DIR):
            return []
        return sorted(os.path.join(MAP_DIR, f) for f in os.listdir(MAP_DIR)
                      if f.endswith(".json"))

    def _load_armory(self) -> None:
        try:
            with open(ARMORY, "r", encoding="utf-8") as f:
                self.armory_doc = json.load(f)
        except Exception as exc:
            self.armory_doc = {"sensors": [], "weapons": [], "units": []}
            self.error = f"武器库读取失败：{exc}"

    def new_map(self, w: int = 30, h: int = 30) -> None:
        self.doc = {
            "id": "new_map",
            "name": "新建想定",
            "description": "",
            "size": [w, h],
            "tile_km": 12,
            "turn_limit": 16,
            "terrain": [],
            "elevation": {"rows": []},
            "facilities": [],
            "objectives": [],
            "deployments": {"BLUE": [], "RED": []},
        }
        self.terr = [["p"] * w for _ in range(h)]
        self.elev = [[2] * w for _ in range(h)]
        self.fac, self.fort, self.obj, self.units = {}, {}, {}, []
        self.path = ""
        self._bind_inputs()
        self.fit(w, h)

    def open_map(self, path: str) -> None:
        self.undo_stack = []
        self.redo_stack = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as exc:
            self.error = f"读取失败：{exc}"
            return
        self.doc = doc
        self.path = path
        w, h = doc.get("size", [30, 30])
        rows = doc.get("terrain", [])
        self.terr = []
        for y in range(h):
            row = rows[y] if y < len(rows) else "p" * w
            self.terr.append([ch if ch in TERRAIN_CHARS else "p" for ch in row[:w].ljust(w, "p")])
        erows = doc.get("elevation", {}).get("rows", [])
        self.elev = []
        for y in range(h):
            row = erows[y] if y < len(erows) else "2" * w
            vals = []
            for x in range(w):
                ch = row[x] if x < len(row) else "2"
                vals.append(int(ch) if ch.isdigit() else 2)
            self.elev.append(vals)
        self.fac = {}
        for f in doc.get("facilities", []):
            try:
                self.fac[(int(f["x"]), int(f["y"]))] = dict(f)
            except Exception:
                continue
        self.fort = {}
        for f in doc.get("forts", []):
            try:
                self.fort[(int(f["x"]), int(f["y"]))] = dict(f)
            except Exception:
                continue
        self.obj = {}
        for o in doc.get("objectives", []):
            try:
                self.obj[(int(o["x"]), int(o["y"]))] = dict(o)
            except Exception:
                continue
        self.units = []
        for side_key, entries in doc.get("deployments", {}).items():
            for e in entries:
                try:
                    d = dict(e)
                    d["side"] = side_key
                    self.units.append(d)
                except Exception:
                    continue
        self.error = ""
        self._bind_inputs()
        self.fit(w, h)
        self.status = f"已载入 {os.path.basename(path)}"

    def save_map(self, as_new: bool = False) -> None:
        doc = self._rebuild_doc()
        if as_new or not self.path:
            name = doc.get("id") or "new_map"
            path = os.path.join(MAP_DIR, f"{name}.json")
        else:
            path = self.path
        try:
            os.makedirs(MAP_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            self.path = path
            self.map_files = self._list_maps()
            self.status = f"已保存 {os.path.basename(path)}"
            self.error = ""
        except Exception as exc:
            self.error = f"保存失败：{exc}"

    def _rebuild_doc(self) -> dict:
        w, h = len(self.terr[0]), len(self.terr)
        doc = dict(self.doc)
        doc["id"] = self.inputs["id"].value.strip() or "new_map"
        doc["name"] = self.inputs["name"].value.strip() or "未命名想定"
        doc["description"] = self.doc.get("description", "")
        doc["size"] = [w, h]
        doc["tile_km"] = self.inputs["tile_km"].as_float(12.0)
        doc["turn_limit"] = self.inputs["turns"].as_int(16)
        doc["terrain"] = ["".join(r) for r in self.terr]
        doc["elevation"] = {"rows": ["".join(str(min(9, max(0, v))) for v in r) for r in self.elev]}
        doc["facilities"] = [
            {"x": x, "y": y, "facility": d.get("facility", "airbase"),
             "owner": d.get("owner", "BLUE"), "hp": d.get("hp", 100)}
            for (x, y), d in sorted(self.fac.items())
        ]
        doc["forts"] = [
            {"x": x, "y": y, "level": int(d.get("level", 1)),
             "owner": d.get("owner", "BLUE")}
            for (x, y), d in sorted(self.fort.items()) if int(d.get("level", 0)) > 0
        ]
        doc["objectives"] = [
            {"x": x, "y": y, "name": d.get("name", "目标点"),
             "kind": d.get("kind", "capture"), "side": d.get("side", "NEUTRAL"),
             "value": d.get("value", 3), "owner": d.get("owner", "NEUTRAL")}
            for (x, y), d in sorted(self.obj.items())
        ]
        dep: dict[str, list] = {"BLUE": [], "RED": []}
        for u in self.units:
            side = u.get("side", "BLUE")
            item = {"template": u.get("template"), "x": u["x"], "y": u["y"]}
            if u.get("altitude"):
                item["altitude"] = u["altitude"]
            dep.setdefault(side, []).append(item)
        doc["deployments"] = dep
        return doc

    # ------------------------------------------------------------------ #
    # 视图
    # ------------------------------------------------------------------ #
    def _map_rect(self) -> pygame.Rect:
        return pygame.Rect(0, 44, self.w - self.PANEL_W, self.h - 44)

    def fit(self, w: int | None = None, h: int | None = None) -> None:
        w = w or len(self.terr[0])
        h = h or len(self.terr)
        r = self._map_rect()
        self.tile = max(8, min((r.width - 20) // max(1, w), (r.height - 20) // max(1, h)))
        self.ox = (r.width - w * self.tile) / 2
        self.oy = (r.height - h * self.tile) / 2

    def _cell(self, x: int, y: int) -> pygame.Rect:
        r = self._map_rect()
        return pygame.Rect(int(r.x + self.ox + x * self.tile), int(r.y + self.oy + y * self.tile),
                           self.tile, self.tile)

    def _at(self, pos) -> tuple[int, int] | None:
        r = self._map_rect()
        if not r.collidepoint(pos):
            return None
        gx = int((pos[0] - r.x - self.ox) // self.tile)
        gy = int((pos[1] - r.y - self.oy) // self.tile)
        if 0 <= gy < len(self.terr) and 0 <= gx < len(self.terr[0]):
            return gx, gy
        return None

    # ------------------------------------------------------------------ #
    # 输入绑定
    # ------------------------------------------------------------------ #
    def _bind_inputs(self) -> None:
        w, h = (len(self.terr[0]), len(self.terr)) if self.terr else (30, 30)
        px = self.w - self.PANEL_W + 14
        y = 300
        self.inputs = {
            "id": TextInput(pygame.Rect(px + 66, y, 210, 22), self.doc.get("id", "new_map"), max_len=24),
            "name": TextInput(pygame.Rect(px + 66, y + 28, 210, 22), self.doc.get("name", ""), max_len=28),
            "turns": TextInput(pygame.Rect(px + 66, y + 56, 60, 22),
                               str(self.doc.get("turn_limit", 16)), numeric=True, max_len=3),
            "tile_km": TextInput(pygame.Rect(px + 196, y + 56, 80, 22),
                                 str(self.doc.get("tile_km", 12)), numeric=True, max_len=5),
            "w": TextInput(pygame.Rect(px + 66, y + 84, 50, 22), str(w), numeric=True, max_len=2),
            "h": TextInput(pygame.Rect(px + 176, y + 84, 50, 22), str(h), numeric=True, max_len=2),
            "obj_name": TextInput(pygame.Rect(px + 14, 196, 190, 22), "目标点", max_len=14),
            "obj_value": TextInput(pygame.Rect(px + 236, 196, 44, 22), "3", numeric=True, max_len=2),
        }

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def handle_event(self, e: pygame.event.Event) -> None:
        # 战役标签页有独立的交互体系，全部事件交给它处理
        if self.tab == "campaign" and e.type != pygame.VIDEORESIZE:
            self.campaign_editor.handle_event(e)
            self.status = self.campaign_editor.status
            self.error = self.campaign_editor.error
            return
        if e.type == pygame.VIDEORESIZE:
            self.w, self.h = max(1100, e.w), max(720, e.h)
            self._bind_inputs()
            self.fit()
            return
        if e.type == pygame.KEYDOWN:
            if self.focus is not None:
                if e.key == pygame.K_ESCAPE:
                    self.focus.active = False
                    self.focus = None
                else:
                    self.focus.handle(e)
                return
            if e.key == pygame.K_ESCAPE:
                if self.picker_open:
                    self.picker_open = False
                else:
                    self.quit = True
            elif e.key == pygame.K_s and (e.mod & pygame.KMOD_CTRL):
                self.save_map()
            elif e.key == pygame.K_z and (e.mod & pygame.KMOD_CTRL) and self.tab == "map":
                self.undo()
            elif e.key == pygame.K_y and (e.mod & pygame.KMOD_CTRL) and self.tab == "map":
                self.redo()
            return
        if e.type == pygame.MOUSEBUTTONDOWN:
            self._on_mouse_down(e)
        elif e.type == pygame.MOUSEBUTTONUP:
            if e.button == 2:
                self._panning = False
            self._dragging = None
            self._end_stroke()
        elif e.type == pygame.MOUSEMOTION:
            if self._panning:
                self.ox += e.rel[0]
                self.oy += e.rel[1]
            else:
                self.hover = self._at(e.pos)
                if self._sel_dragging and self.sel_anchor is not None:
                    # 框选拖拽中：实时更新选区
                    cur = self._at(e.pos)
                    if cur is not None:
                        self._set_selection(self.sel_anchor[0], self.sel_anchor[1],
                                            cur[0], cur[1])
                        self.status = f"已选 {self.selection_size()} 个格子"
                elif self._dragging == 1 and self.hover and self.tool in ("terrain", "elev_up",
                                                                          "elev_dn", "erase"):
                    self._apply(self.hover)
        elif e.type == pygame.MOUSEWHEEL:
            if self.picker_open:
                self.picker_scroll = max(0, self.picker_scroll - e.y * 3)
                return
            if self.tab == "units":
                self.tmpl_scroll = max(0, self.tmpl_scroll - e.y * 2)
                return
            if self._map_rect().collidepoint(pygame.mouse.get_pos()):
                mx, my = pygame.mouse.get_pos()
                step = 2 if e.y > 0 else -2
                old = self.tile
                self.tile = max(6, min(46, self.tile + step))
                if old != self.tile:
                    self.ox = mx - (mx - self.ox) * self.tile / old
                    self.oy = my - (my - self.oy) * self.tile / old

    def _on_mouse_down(self, e: pygame.event.Event) -> None:
        pos = e.pos
        if e.button == 2:
            self._panning = True
            return

        # 顶部标签
        if pos[1] < 44:
            if pygame.Rect(14, 8, 76, 28).collidepoint(pos):
                self.tab = "map"
            elif pygame.Rect(96, 8, 76, 28).collidepoint(pos):
                self.tab = "units"
            elif pygame.Rect(178, 8, 76, 28).collidepoint(pos):
                self.tab = "campaign"
            elif pygame.Rect(self.w - 96, 8, 82, 28).collidepoint(pos):
                self.quit = True
            return

        # 右侧面板按钮
        for rect, action, arg in self.buttons:
            if rect.collidepoint(pos) and e.button == 1:
                self._do_action(action, arg)
                return
        for inp in (self.inputs.values() if self.tab == "map" else ()):
            if inp.rect.collidepoint(pos):
                if self.focus is not None:
                    self.focus.active = False
                self.focus = inp
                inp.active = True
                return
        if self.focus is not None:
            self.focus.active = False
            self.focus = None

        if self.tab != "map":
            return
        cell = self._at(pos)
        if cell is None:
            return
        if e.button == 1:
            if self.tool == "select":
                # 框选：按下记录起点，拖拽中实时更新
                self.sel_anchor = cell
                self._sel_dragging = True
                self._set_selection(cell[0], cell[1], cell[0], cell[1])
                self.status = "拖拽框选区域"
                return
            self._dragging = 1
            self._apply(cell)
        elif e.button == 3:
            if self.tool == "select" or self.sel_cells:
                # 右键清除选区
                self.clear_selection()
                self.status = "已清除选区"
                return
            self._erase(cell)

    def _do_action(self, action: str, arg) -> None:
        if action == "quit":
            self.quit = True
        elif action == "fill_sel":
            if not self.sel_cells:
                self.status = "尚未框选区域"
                return
            # 填充选区：使用当前笔刷地形
            saved = self.tool
            self.tool = "terrain"
            n = self._apply_to_selection()
            self.tool = saved
            self.status = f"已填充 {n} 个格子为「{TERRAIN_NAME.get(self.brush_terrain, '')}」"
        elif action == "clear_sel":
            self.clear_selection()
            self.status = "已清除选区"
        elif action == "tool":
            self.tool = str(arg)
            if self.tool == "unit":
                self._sync_brush()
        elif action == "terrain":
            self.brush_terrain = str(arg)
            self.tool = "terrain"
        elif action == "facility":
            self.brush_facility = arg
            self.tool = "facility"
        elif action == "fort_lv":
            self.brush_fort = int(arg)
            self.tool = "fort"
        elif action == "side":
            self.brush_side = arg
        elif action == "unit":
            self.brush_template = str(arg)
            self.tool = "unit"
        elif action == "new":
            self.push_undo()
            self.new_map(30, 30)
        elif action == "undo":
            self.undo()
        elif action == "redo":
            self.redo()
        elif action == "open_prev":
            self._cycle_map(-1)
        elif action == "open_next":
            self._cycle_map(1)
        elif action == "reload":
            if self.path:
                self.open_map(self.path)
        elif action == "save":
            self.save_map()
        elif action == "save_as":
            self.save_map(as_new=True)
        elif action == "fit":
            self.fit()
        elif action == "resize":
            self.push_undo()
            self.resize_map(self.inputs["w"].as_int(30), self.inputs["h"].as_int(30))
        elif action == "toggle":
            self._toggle_flag(str(arg))
        elif action == "tmpl":
            self.tmpl_index = int(arg)
            self.picker_open = False
            self._sync_brush()
        elif action == "pack":
            self.pack_filter = str(arg)
            self.tmpl_scroll = 0
            self.tmpl_index = self._filtered_templates()[0][0] if self._filtered_templates() else 0
            self._sync_brush()
        elif action == "loadout":
            self._apply_loadout(str(arg))
        elif action == "rm_weapon":
            self._remove_weapon(int(arg))
        elif action == "add_weapon":
            self.picker_open = True
            self.picker_scroll = 0
        elif action == "pick":
            self._add_weapon(str(arg))
        elif action == "close_picker":
            self.picker_open = False
        elif action == "new_unit":
            self._new_template(False)
        elif action == "dup_unit":
            self._new_template(True)
        elif action == "del_unit":
            self._delete_template()
        elif action == "stat":
            self._bump_stat(*arg)
        elif action == "ammo":
            self._bump_ammo(*arg)
        elif action == "save_armory":
            self.save_armory()
        elif action == "export_package":
            self.export_package()
        elif action == "validate":
            self._validate()

    def _sync_brush(self) -> None:
        """在部署页选中兵种时同步笔刷，避免「列表选了 055、放下去却是 M1A2」。"""
        units = self._units_doc()
        if 0 <= self.tmpl_index < len(units):
            self.brush_template = str(units[self.tmpl_index].get("id", self.brush_template))

    def _cycle_map(self, step: int) -> None:
        if not self.map_files:
            return
        self.file_index = (self.file_index + step) % len(self.map_files)
        self.open_map(self.map_files[self.file_index])

    # ------------------------------------------------------------------ #
    # 编辑操作
    # ------------------------------------------------------------------ #
    # -- 撤销 / 重做 -------------------------------------------------------
    def _map_state(self) -> tuple:
        import copy as _copy
        return (_copy.deepcopy(self.terr), _copy.deepcopy(self.elev),
                _copy.deepcopy(self.fac), _copy.deepcopy(self.fort),
                _copy.deepcopy(self.obj), _copy.deepcopy(self.units),
                list(self.doc.get("size", [30, 30])))

    def _restore_map_state(self, snap: tuple) -> None:
        self.terr, self.elev, self.fac, self.fort, self.obj, self.units, size = snap
        self.doc["size"] = list(size)
        self.inputs["w"].value = str(size[0])
        self.inputs["h"].value = str(size[1])
        self.fit()

    def push_undo(self) -> None:
        """把当前地图状态压入撤销栈（笔画开始 / 危险操作前调用）。"""
        self.undo_stack.append(self._map_state())
        if len(self.undo_stack) > 50:
            del self.undo_stack[:20]
        self.redo_stack = []

    def undo(self) -> None:
        if not self.undo_stack:
            self.status = "没有可撤销的操作"
            return
        self.redo_stack.append(self._map_state())
        self._restore_map_state(self.undo_stack.pop())
        self.status = "已撤销（Ctrl+Z）"

    def redo(self) -> None:
        if not self.redo_stack:
            self.status = "没有可重做的操作"
            return
        self.undo_stack.append(self._map_state())
        self._restore_map_state(self.redo_stack.pop())
        self.status = "已重做（Ctrl+Y）"

    def resize_map(self, nw: int, nh: int) -> None:
        """调整地图尺寸：扩大时以平原/海拔 2 补边，缩小时裁掉右侧与下方。"""
        nw = max(8, min(MAX_MAP_SIZE, nw))
        nh = max(8, min(MAX_MAP_SIZE, nh))
        if not self.terr:
            return
        old_w, old_h = len(self.terr[0]), len(self.terr)
        new_terr = [["p"] * nw for _ in range(nh)]
        new_elev = [[2] * nw for _ in range(nh)]
        for y in range(min(nh, old_h)):
            for x in range(min(nw, old_w)):
                new_terr[y][x] = self.terr[y][x]
                new_elev[y][x] = self.elev[y][x]
        self.terr, self.elev = new_terr, new_elev
        # 越界的设施/目标/部署会被丢弃
        self.fac = {k: v for k, v in self.fac.items() if k[0] < nw and k[1] < nh}
        self.fort = {k: v for k, v in self.fort.items() if k[0] < nw and k[1] < nh}
        self.obj = {k: v for k, v in self.obj.items() if k[0] < nw and k[1] < nh}
        self.units = [u for u in self.units if u["x"] < nw and u["y"] < nh]
        self.doc["size"] = [nw, nh]
        self.inputs["w"].value = str(nw)
        self.inputs["h"].value = str(nh)
        self.fit()
        self.status = f"地图尺寸调整为 {nw}×{nh}"

    def _flag_value(self, t: dict, key: str) -> bool:
        """与加载器保持一致的默认值，避免界面显示与游戏内实际行为不符。"""
        if key in t:
            return bool(t[key])
        if key == "datalink":
            return True
        if key == "is_infantry":
            return t.get("class") == "infantry"
        if key == "one_shot_kill":
            return t.get("domain") == "air"
        if key == "can_fortify":
            return t.get("class") == "infantry"
        return False

    def _toggle_flag(self, key: str) -> None:
        t = self._selected_template()
        if not t:
            return
        t[key] = not self._flag_value(t, key)
        self.status = f"{t.get('id')}.{key} = {t[key]}"

    def _begin_stroke(self) -> None:
        """一笔绘制只在开始前保存一次快照（拖动连续绘制视为一次操作）。"""
        if self._stroke_pending:
            return
        self._stroke_pending = True
        self.push_undo()

    def _end_stroke(self) -> None:
        self._stroke_pending = False

    def _apply(self, cell: tuple[int, int]) -> None:
        # 选区非空时：把工具应用到整个选区（批量编辑）
        if self.sel_cells and self.tool != "select":
            self._apply_to_selection()
            return
        self._begin_stroke()
        x, y = cell
        if self.tool == "terrain":
            self.terr[y][x] = self.brush_terrain
            if self.brush_terrain == "w":
                self.elev[y][x] = 0
                self.fort.pop((x, y), None)   # 海域不能保留工事
            self.fac.pop((x, y), None)
        elif self.tool == "elev_up":
            self.elev[y][x] = min(9, self.elev[y][x] + 1)
        elif self.tool == "elev_dn":
            self.elev[y][x] = max(0, self.elev[y][x] - 1)
        elif self.tool == "facility":
            self.fac[(x, y)] = {"x": x, "y": y, "facility": self.brush_facility.value,
                                "owner": self.brush_side.value, "hp": 100}
        elif self.tool == "fort":
            self.fort[(x, y)] = {"x": x, "y": y, "level": max(1, self.brush_fort),
                                 "owner": self.brush_side.value}
        elif self.tool == "objective":
            self.obj[(x, y)] = {
                "x": x, "y": y,
                "name": self.inputs["obj_name"].value.strip() or "目标点",
                "kind": "capture", "side": self.brush_side.value,
                "value": self.inputs["obj_value"].as_int(3),
                "owner": self.brush_side.value,
            }
        elif self.tool == "unit":
            tpl = next((u for u in self._units_doc() if u.get("id") == self.brush_template), {})
            dom = tpl.get("domain", "land")
            terr = TERRAIN_CHARS.get(self.terr[y][x], Terrain.PLAIN)
            # 与部署规则保持一致：陆上单位不入海（两栖单位除外）
            if dom in ("land", "infra") and terr is Terrain.WATER and not tpl.get("amphibious"):
                self.status = f"{tpl.get('name', self.brush_template)} 为陆上单位，不能部署于海域"
                return
            occupied = [u for u in self.units if u["x"] == x and u["y"] == y]
            if any(u.get("side") != self.brush_side.value for u in occupied):
                self.status = "该格已有敌方单位，不能同格部署"
                return
            same = [u for u in occupied if u.get("side") == self.brush_side.value]
            if same:
                # 航空兵可在己方机场叠放（停机坪容量 4 架）
                f = self.fac.get((x, y))
                ok_air = (dom == "air" and f is not None
                          and f.get("facility") == Facility.AIRBASE.value
                          and f.get("owner") == self.brush_side.value
                          and len(same) < 4)
                if not ok_air:
                    self.status = "该格已有同阵营单位（仅机场可叠放航空兵）"
                    return
            self.units.append({"template": self.brush_template, "x": x, "y": y,
                               "side": self.brush_side.value})
        elif self.tool == "erase":
            self._erase(cell)

    def _erase(self, cell: tuple[int, int]) -> None:
        self._begin_stroke()
        x, y = cell
        self.fac.pop((x, y), None)
        self.fort.pop((x, y), None)
        self.obj.pop((x, y), None)
        for u in list(self.units):
            if u["x"] == x and u["y"] == y:
                self.units.remove(u)
                return
        self.terr[y][x] = "p" if self.terr[y][x] != "w" else "w"

    # ------------------------------------------------------------------ #
    # 框选与批量编辑
    # ------------------------------------------------------------------ #
    def _set_selection(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """设置矩形选区（坐标自动排序，端点含入）。"""
        w = len(self.terr[0]) if self.terr else 0
        h = len(self.terr) if self.terr else 0
        x0, x1 = sorted((max(0, x0), min(w - 1, x1)))
        y0, y1 = sorted((max(0, y0), min(h - 1, y1)))
        self.sel_rect = (x0, y0, x1, y1)
        self.sel_cells = {(x, y)
                          for y in range(y0, y1 + 1)
                          for x in range(x0, x1 + 1)}

    def clear_selection(self) -> None:
        self.sel_anchor = None
        self.sel_rect = None
        self.sel_cells = set()
        self._sel_dragging = False

    def selection_size(self) -> int:
        return len(self.sel_cells)

    def _apply_to_selection(self) -> int:
        """把当前工具应用到整个选区，返回受影响的格数。

        选区为空时不做任何事（普通单格编辑不受影响）。
        """
        if not self.sel_cells:
            return 0
        cells = sorted(self.sel_cells)
        # 一次操作算一次撤销
        self.push_undo()
        for cell in cells:
            self._apply_single(cell)
        n = len(cells)
        self.status = f"批量应用到 {n} 个格子"
        return n

    def _apply_single(self, cell: tuple[int, int]) -> None:
        """对单个格子执行当前工具（不重复压撤销栈）。"""
        x, y = cell
        if not (0 <= y < len(self.terr) and 0 <= x < len(self.terr[0])):
            return
        if self.tool == "terrain":
            self.terr[y][x] = self.brush_terrain
            if self.brush_terrain == "w":
                self.elev[y][x] = 0
                self.fort.pop((x, y), None)
            self.fac.pop((x, y), None)
            self.obj.pop((x, y), None)
        elif self.tool == "elev_up":
            self.elev[y][x] = min(9, self.elev[y][x] + 1)
        elif self.tool == "elev_dn":
            self.elev[y][x] = max(0, self.elev[y][x] - 1)
        elif self.tool == "facility":
            self.fac[(x, y)] = {"x": x, "y": y, "facility": self.brush_facility.value,
                                "owner": self.brush_side.value, "hp": 100}
        elif self.tool == "fort":
            self.fort[(x, y)] = {"x": x, "y": y, "level": max(1, self.brush_fort),
                                 "owner": self.brush_side.value}
        elif self.tool == "objective":
            self.obj[(x, y)] = {
                "x": x, "y": y,
                "name": self.inputs["obj_name"].value.strip() or "目标点",
                "kind": "capture", "side": self.brush_side.value,
                "value": self.inputs["obj_value"].as_int(3),
                "owner": self.brush_side.value,
            }
        elif self.tool == "erase":
            self._erase_single(cell)

    def _erase_single(self, cell: tuple[int, int]) -> None:
        x, y = cell
        self.fac.pop((x, y), None)
        self.fort.pop((x, y), None)
        self.obj.pop((x, y), None)
        for u in list(self.units):
            if u["x"] == x and u["y"] == y:
                self.units.remove(u)
                return
        self.terr[y][x] = "p" if self.terr[y][x] != "w" else "w"

    # ------------------------------------------------------------------ #
    # 兵种编辑
    # ------------------------------------------------------------------ #
    def _units_doc(self) -> list[dict]:
        return self.armory_doc.get("units", [])

    def _selected_template(self) -> dict | None:
        units = self._units_doc()
        if not units:
            return None
        self.tmpl_index = max(0, min(self.tmpl_index, len(units) - 1))
        return units[self.tmpl_index]

    def _bump_stat(self, key: str, delta: float) -> None:
        t = self._selected_template()
        if not t:
            return
        cur = float(t.get(key, 0))
        val = cur + delta
        if key in ("hp", "mp", "cost"):
            t[key] = max(0, int(round(val)))
        else:
            t[key] = round(max(0.0, min(0.95, val)), 2)
        self.status = f"{t.get('id')}.{key} = {t[key]}"

    # -- 预设挂载 ---------------------------------------------------------
    def _loadouts(self) -> list[dict]:
        return self.armory_doc.get("presets", {}).get("loadouts", [])

    def _packs(self) -> list[dict]:
        return self.armory_doc.get("presets", {}).get("packs", [])

    def _filtered_templates(self) -> list[tuple[int, dict]]:
        """按预设编队筛选兵种，返回 (全局索引, 模板) 列表。"""
        units = self._units_doc()
        if not self.pack_filter:
            return list(enumerate(units))
        for pk in self._packs():
            if pk.get("id") == self.pack_filter:
                ids = set(pk.get("units", []))
                return [(i, u) for i, u in enumerate(units) if u.get("id") in ids]
        return list(enumerate(units))

    def _loadouts_for(self, t: dict) -> list[dict]:
        dom = t.get("domain", "")
        out = []
        for lo in self._loadouts():
            if lo.get("domain") in ("", dom):
                out.append(lo)
        return out

    def _apply_loadout(self, loadout_id: str) -> None:
        """把预设挂载套用到当前兵种（替换武器列表，保留其它字段）。"""
        t = self._selected_template()
        if not t:
            return
        lo = next((x for x in self._loadouts() if x.get("id") == loadout_id), None)
        if not lo:
            return
        catalog = {w.get("id"): w for w in self.armory_doc.get("weapons", [])}
        entries = []
        for item in lo.get("weapons", []):
            ref = item.get("ref")
            ammo = int(item.get("ammo", 1))
            if ref in catalog:
                entries.append({"ref": ref, "ammo": ammo})
        if not entries:
            self.error = "该挂载预设为空"
            return
        t["weapons"] = entries
        self.error = ""
        self.status = f"{t.get('id')} 套用挂载「{lo.get('name', loadout_id)}」：{len(entries)} 种武器"

    # -- 武器增删 ---------------------------------------------------------
    def _weapon_catalog(self) -> list[dict]:
        return sorted(self.armory_doc.get("weapons", []), key=lambda w: str(w.get("kind", "")))

    def _add_weapon(self, weapon_id: str) -> None:
        t = self._selected_template()
        if not t or not weapon_id:
            return
        w = next((x for x in self.armory_doc.get("weapons", []) if x.get("id") == weapon_id), None)
        if not w:
            return
        entries = t.setdefault("weapons", [])
        for e in entries:
            ref = e if isinstance(e, str) else e.get("ref")
            if ref == weapon_id:
                self.status = f"该兵种已装备 {w.get('name', weapon_id)}"
                self.picker_open = False
                return
        entries.append({"ref": weapon_id, "ammo": int(w.get("ammo", 1))})
        self.status = f"＋ {w.get('name', weapon_id)}（备弹 {w.get('ammo', 1)}）"
        self.picker_open = False

    def _remove_weapon(self, wi: int) -> None:
        t = self._selected_template()
        if not t:
            return
        entries = t.get("weapons", [])
        if 0 <= wi < len(entries):
            e = entries.pop(wi)
            ref = e if isinstance(e, str) else e.get("ref")
            self.status = f"－ {ref}"

    # -- 兵种增删 ---------------------------------------------------------
    def _new_template(self, copy_current: bool = False) -> None:
        units = self._units_doc()
        base = {
            "id": "new_unit", "name": "新建兵种", "short": "NEW",
            "class": "infantry", "domain": "land",
            "hp": 90, "mp": 3.0, "armor": 0.05, "stealth": 0.05, "evasion": 0.1,
            "sensors": [], "weapons": [], "cost": 100, "notes": "",
        }
        if copy_current and units:
            import copy as _copy
            base = _copy.deepcopy(units[self.tmpl_index])
            base["id"] = str(base.get("id", "unit")) + "_copy"
            base["name"] = str(base.get("name", "")) + "（副本）"
        # 生成不冲突的 id
        used = {u.get("id") for u in units}
        root = base["id"]
        n = 2
        while base["id"] in used:
            base["id"] = f"{root}{n}"
            n += 1
        units.append(base)
        self.tmpl_index = len(units) - 1
        self.status = f"新建兵种 {base['id']}（记得保存武器库）"

    def _delete_template(self) -> None:
        units = self._units_doc()
        if not units:
            return
        t = units[self.tmpl_index]
        tid = t.get("id")
        # 被想定引用的兵种不允许删除
        for path in self.map_files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except Exception:
                continue
            for entries in doc.get("deployments", {}).values():
                if any(e.get("template") == tid for e in entries):
                    self.error = f"{tid} 已被 {os.path.basename(path)} 使用，不能删除"
                    return
        units.pop(self.tmpl_index)
        self.tmpl_index = max(0, min(self.tmpl_index, len(units) - 1))
        self.status = f"已删除兵种 {tid}"
        self.error = ""

    def _bump_ammo(self, wi: int, delta: int) -> None:
        t = self._selected_template()
        if not t:
            return
        weapons = t.get("weapons", [])
        if not (0 <= wi < len(weapons)):
            return
        entry = weapons[wi]
        if isinstance(entry, str):
            entry = {"ref": entry}
            weapons[wi] = entry
        entry["ammo"] = max(0, int(entry.get("ammo", 1)) + delta)
        self.status = f"备弹 → {entry['ammo']}"

    def save_armory(self) -> None:
        try:
            with open(self.armory_path, "w", encoding="utf-8") as f:
                json.dump(self.armory_doc, f, ensure_ascii=False, indent=2)
            self.status = "武器库已保存"
            self.error = ""
        except Exception as exc:
            self.error = f"武器库保存失败：{exc}"

    def export_package(self) -> None:
        """把当前地图导出为社区想定包（.bwscn）。

        有未保存改动时先落盘（包里必须是磁盘上的最新版本）；
        没有改动就直接用磁盘上的文件，避免无谓地重写地图文件
        （重写会把 `12` 变成 `12.0` 这类格式差异弄进版本库）。
        导出到用户数据目录的 `packages/`，随包附带武器库覆盖，
        这样别人导入后能直接玩。
        """
        if self.undo_stack:
            try:
                self.save_map()
            except Exception as exc:
                self.error = f"导出前保存失败：{exc}"
                return
        if not self.path or not os.path.isfile(self.path):
            self.error = "导出失败：地图尚未保存"
            return
        try:
            out_dir = os.path.join(USER_DIR, "packages")
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(self.path))[0]
            out = os.path.join(out_dir, base + _pkg.PACKAGE_SUFFIX)
            doc = self._rebuild_doc()
            meta = doc.get("meta") or {}
            _pkg.export_package([self.path], self.armory_path, out,
                                name=doc.get("name", base),
                                author=meta.get("author", ""),
                                include_armory=True)
            self.status = f"想定包已导出：{out}"
            self.error = ""
        except Exception as exc:
            self.error = f"导出失败：{exc}"

    def _validate(self) -> None:
        try:
            self.save_map()
            sc = load_scenario(self.path, ARMORY)
            self.status = f"校验通过：{sc.map.width}×{sc.map.height}，{len(sc.units)} 个部署单位"
            self.error = ""
        except LoadError as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = f"校验失败：{exc}"

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def draw(self, s: pygame.Surface) -> None:
        self.buttons = []
        s.fill(T.BG)
        self._draw_topbar(s)
        if self.tab == "map":
            self._draw_map(s)
            self._draw_map_panel(s)
        elif self.tab == "units":
            self._draw_units_panel(s)
        else:
            self.campaign_editor.draw(s)
        if self.picker_open:
            self.buttons = []          # 弹层独占交互，避免穿透到底层控件
            self._draw_weapon_picker(s)
        self._draw_status(s)

    def _draw_topbar(self, s: pygame.Surface) -> None:
        pygame.draw.rect(s, T.PANEL, pygame.Rect(0, 0, self.w, 44))
        T.hline(s, 0, self.w, 43, T.BORDER)
        for key, label, rect in (("map", "地图编辑", pygame.Rect(14, 8, 76, 28)),
                                 ("units", "武器编辑", pygame.Rect(96, 8, 76, 28)),
                                 ("campaign", "战役编排", pygame.Rect(178, 8, 76, 28))):
            active = self.tab == key
            pygame.draw.rect(s, T.PANEL_HI if active else T.PANEL_SUNK, rect, border_radius=3)
            if active:
                pygame.draw.rect(s, T.CYAN, pygame.Rect(rect.x, rect.bottom - 2, rect.width, 2))
            T.draw_text(s, label, rect.center, 12, T.TEXT if active else T.TEXT_FAINT, anchor="center")
        T.draw_text(s, "方块兵棋 · 可视化编辑器", (272, 14), 13, T.TEXT_DIM)
        if self.path:
            T.draw_text(s, os.path.basename(self.path), (390, 15), 11, T.TEXT_FAINT)
        # 导出社区想定包
        exp_r = pygame.Rect(self.w - 186, 8, 82, 28)
        hover = exp_r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL, exp_r, border_radius=3)
        pygame.draw.rect(s, T.CYAN if hover else T.BORDER, exp_r, 1, border_radius=3)
        T.draw_text(s, "导出想定包", exp_r.center, 11,
                    T.TEXT if hover else T.TEXT_DIM, anchor="center")
        self.buttons.append((exp_r, "export_package", None))
        quit_r = pygame.Rect(self.w - 96, 8, 82, 28)
        hover = quit_r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL, quit_r, border_radius=3)
        T.draw_text(s, "返回菜单", quit_r.center, 11, T.TEXT_DIM, anchor="center")
        self.buttons.append((quit_r, "quit", None))

    # -- 地图绘制 ---------------------------------------------------------
    def _draw_map(self, s: pygame.Surface) -> None:
        r = self._map_rect()
        s.set_clip(r)
        pygame.draw.rect(s, T.BG_MAP, r)
        h = len(self.terr)
        w = len(self.terr[0]) if h else 0
        for y in range(h):
            for x in range(w):
                cell = self._cell(x, y)
                if not r.colliderect(cell):
                    continue
                terr = TERRAIN_CHARS.get(self.terr[y][x], Terrain.PLAIN)
                base = T.TERRAIN_COLORS.get(terr, T.TERRAIN_COLORS[Terrain.PLAIN])
                col = T.elevation_shade(base, self.elev[y][x] * 300)
                pygame.draw.rect(s, col, cell)
                f = self.fac.get((x, y))
                if f:
                    c = T.side_color(Side(f.get("owner", "NEUTRAL")), "edge")
                    pygame.draw.rect(s, c, cell.inflate(-6, -6), 1)
                    if self.tile >= 12:
                        T.draw_text(s, Facility(f["facility"]).label[:2], cell.center,
                                    max(8, self.tile // 3), c, anchor="center")
                o = self.obj.get((x, y))
                if o:
                    cx, cy = cell.center
                    d = max(3, self.tile // 6)
                    pygame.draw.polygon(s, T.AMBER, [(cx, cy - d), (cx + d, cy),
                                                     (cx, cy + d), (cx - d, cy)])
                if self.tile >= 10:
                    pygame.draw.rect(s, (20, 26, 34), cell, 1)
        # 工事
        for (fx, fy), f in self.fort.items():
            cell = self._cell(fx, fy)
            if not r.colliderect(cell):
                continue
            lvl = int(f.get("level", 0))
            if lvl <= 0:
                continue
            col = T.side_color(Side(f.get("owner", "NEUTRAL")), "edge")
            w = max(1, int((self.tile - 5) * min(3, lvl) / 3.0))
            pygame.draw.rect(s, T.PANEL_SUNK, (cell.x + 2, cell.bottom - 4, self.tile - 5, 3))
            pygame.draw.rect(s, col, (cell.x + 2, cell.bottom - 4, w, 2))

        # 部署单位
        for u in self.units:
            cell = self._cell(u["x"], u["y"])
            if not r.colliderect(cell):
                continue
            side = Side(u.get("side", "BLUE"))
            body = cell.inflate(-4, -4)
            pygame.draw.rect(s, T.SIDE_COLORS[side]["fill"], body, border_radius=2)
            pygame.draw.rect(s, T.side_color(side), body, 1, border_radius=2)
            if self.tile >= 16:
                tpl = u.get("template", "?")
                short = tpl[:4].upper()
                T.draw_text(s, short, body.center, max(7, self.tile // 4),
                            T.SIDE_COLORS[side]["text"], anchor="center")
        # 选区高亮
        if self.sel_cells:
            overlay = pygame.Surface(r.size, pygame.SRCALPHA)
            for (sx, sy) in self.sel_cells:
                cell = self._cell(sx, sy)
                rr = pygame.Rect(cell.x - r.x, cell.y - r.y, cell.width, cell.height)
                overlay.fill((94, 231, 224, 56), rr)
            s.blit(overlay, r.topleft)
            if self.sel_rect:
                x0, y0, x1, y1 = self.sel_rect
                c0 = self._cell(x0, y0)
                c1 = self._cell(x1, y1)
                box = pygame.Rect(c0.x, c0.y, c1.right - c0.x, c1.bottom - c0.y)
                pygame.draw.rect(s, T.CYAN, box, 2)
        # 鼠标格
        if self.hover:
            cell = self._cell(*self.hover)
            pygame.draw.rect(s, (255, 255, 255), cell, 1)
            if self.tool == "terrain":
                terr = TERRAIN_CHARS.get(self.brush_terrain, Terrain.PLAIN)
                c = T.elevation_shade(T.TERRAIN_COLORS[terr], 600)
                overlay = pygame.Surface(cell.size, pygame.SRCALPHA)
                overlay.fill((*c, 120))
                s.blit(overlay, cell.topleft)
        s.set_clip(None)

        # 尺寸提示
        T.draw_text(s, f"{w}×{h} 格 · 每格 {self.inputs['tile_km'].value} km",
                    (r.x + 10, r.bottom - 20), 11, T.TEXT_FAINT)
        if self.sel_cells:
            T.draw_text(s, f"已选 {self.selection_size()} 格"
                        f"（右键清除）",
                        (r.x + 10, r.bottom - 36), 11, T.CYAN)

    # -- 左侧/右侧面板 ----------------------------------------------------
    def _panel_rect(self) -> pygame.Rect:
        return pygame.Rect(self.w - self.PANEL_W, 44, self.PANEL_W, self.h - 44)

    def _draw_map_panel(self, s: pygame.Surface) -> None:
        p = self._panel_rect()
        pygame.draw.rect(s, T.PANEL, p)
        T.vline(s, p.x, p.y, p.bottom, T.BORDER)
        x = p.x + 14
        y = p.y + 12

        T.draw_text(s, "编辑工具", (x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        bw = 68
        for i, (key, label) in enumerate(TOOLS):
            r = pygame.Rect(x + (i % 3) * (bw + 6), y + (i // 3) * 30, bw, 26)
            self._tool_button(s, r, label, self.tool == key, ("tool", key))
        y += ((len(TOOLS) - 1) // 3 + 1) * 30 + 8

        if self.tool == "terrain":
            T.draw_text(s, "地形笔刷", (x, y), 11, T.TEXT_FAINT)
            y += 18
            for i, key in enumerate(TERRAIN_ORDER):
                r = pygame.Rect(x + (i % 5) * 44, y + (i // 5) * 40, 40, 24)
                terr = TERRAIN_CHARS[key]
                pygame.draw.rect(s, T.TERRAIN_COLORS[terr], r, border_radius=3)
                pygame.draw.rect(s, T.CYAN if self.brush_terrain == key else T.BORDER,
                                 r, 2 if self.brush_terrain == key else 1, border_radius=3)
                T.draw_text(s, TERRAIN_NAME[key], (r.centerx, r.bottom + 1), 11, T.TEXT_DIM,
                            anchor="midtop")
                self.buttons.append((r, "terrain", key))
            y += 2 * 40 + 8
        elif self.tool == "facility":
            T.draw_text(s, "设施类型", (x, y), 11, T.TEXT_FAINT)
            y += 18
            for i, fc in enumerate(FACILITY_ORDER):
                r = pygame.Rect(x + (i % 4) * 56, y + (i // 4) * 30, 52, 26)
                self._tool_button(s, r, fc.label, self.brush_facility is fc, ("facility", fc))
            y += 2 * 30 + 6
            T.draw_text(s, "归属", (x, y + 4), 11, T.TEXT_FAINT)
            self._side_buttons(s, x + 44, y)
            y += 34
        elif self.tool == "fort":
            T.draw_text(s, "工事等级（堑壕/掩体/雷场）", (x, y), 11, T.TEXT_FAINT)
            y += 18
            for i, lv in enumerate((1, 2, 3)):
                r = pygame.Rect(x + i * 72, y, 68, 26)
                self._tool_button(s, r, f"Lv{lv}", self.brush_fort == lv, ("fort_lv", lv))
            y += 32
            T.draw_text(s, "归属", (x, y + 4), 11, T.TEXT_FAINT)
            self._side_buttons(s, x + 44, y)
            y += 34
            T.draw_text(s, "敌方部队穿越时会被迟滞并触雷", (x, y), 11, T.TEXT_DIM)
            y += 14
            T.draw_text(s, "归属方获得减伤与隐蔽加成", (x, y), 11, T.TEXT_DIM)
            y += 16
        elif self.tool == "select":
            T.draw_text(s, "框选与批量编辑", (x, y), 11, T.TEXT_FAINT)
            y += 18
            T.draw_text(s, "在地图上左键拖拽框选一块矩形区域，", (x, y), 11, T.TEXT_DIM)
            y += 15
            T.draw_text(s, "然后切换到任意工具并点击地图——", (x, y), 11, T.TEXT_DIM)
            y += 15
            T.draw_text(s, "工具会应用到整个选区。", (x, y), 11, T.TEXT_DIM)
            y += 20
            n = self.selection_size()
            if n:
                T.draw_text(s, f"当前已选 {n} 个格子", (x, y), 11, T.CYAN)
                y += 18
                r = pygame.Rect(x, y, 108, 26)
                self._tool_button(s, r, "填充选区", False, ("fill_sel", None))
                r2 = pygame.Rect(x + 116, y, 108, 26)
                self._tool_button(s, r2, "清除选区", False, ("clear_sel", None))
                y += 34
            else:
                T.draw_text(s, "尚未框选区域", (x, y), 11, T.TEXT_FAINT)
                y += 18
        elif self.tool == "objective":
            T.draw_text(s, "目标点设置", (x, y), 11, T.TEXT_FAINT)
            y += 18
            T.draw_text(s, "名称", (x, y + 4), 11, T.TEXT_DIM)
            self.inputs["obj_name"].rect.topleft = (x + 70, y)
            self.inputs["obj_name"].draw(s, pygame.mouse.get_pos())
            y += 28
            T.draw_text(s, "价值", (x, y + 4), 11, T.TEXT_DIM)
            self.inputs["obj_value"].rect.topleft = (x + 70, y)
            self.inputs["obj_value"].draw(s, pygame.mouse.get_pos())
            T.draw_text(s, "胜负点/回合", (x + 124, y + 4), 11, T.TEXT_FAINT)
            y += 30
            T.draw_text(s, "归属", (x, y + 4), 11, T.TEXT_FAINT)
            self._side_buttons(s, x + 44, y)
            y += 34
        elif self.tool == "unit":
            T.draw_text(s, "阵营", (x, y + 4), 11, T.TEXT_FAINT)
            self._side_buttons(s, x + 40, y)
            y += 30
            # 预设编队筛选
            chips = [("", "全部")] + [(pk.get("id", ""), pk.get("short", pk.get("name", ""))[:4])
                                      for pk in self._packs()]
            cw = 50
            for i, (pid, label) in enumerate(chips):
                rr = pygame.Rect(x + (i % 6) * (cw + 2), y + (i // 6) * 22, cw, 19)
                active = self.pack_filter == pid
                hov = rr.collidepoint(pygame.mouse.get_pos())
                pygame.draw.rect(s, (22, 50, 66) if active else
                                 ((20, 28, 38) if hov else T.PANEL_SUNK), rr, border_radius=2)
                pygame.draw.rect(s, T.CYAN if active else T.BORDER, rr, 1, border_radius=2)
                T.draw_text(s, label, rr.center, 11,
                            T.WHITE if active else T.TEXT_DIM, anchor="center")
                self.buttons.append((rr, "pack", pid))
            y += ((len(chips) - 1) // 6 + 1) * 22 + 4
            y = self._draw_template_list(s, x, y, 196, compact=True)
        else:
            T.draw_text(s, "左键擦除设施/目标/单位", (x, y), 11, T.TEXT_DIM)
            y += 20
            T.draw_text(s, "（地形不会恢复为默认）", (x, y), 11, T.TEXT_FAINT)
            y += 18

        # 想定属性
        y = max(y + 10, p.y + 300)
        T.hline(s, x, p.right - 14, y - 6, T.BORDER)
        T.draw_text(s, "想定属性", (x, y), 11, T.TEXT_FAINT, bold=True)
        y += 20
        props = [("文件 id", "id"), ("名称", "name"), ("回合上限", "turns"), ("每格 km", "tile_km")]
        for label, key in props:
            T.draw_text(s, label, (x, y + 4), 11, T.TEXT_DIM)
            inp = self.inputs[key]
            inp.rect.topleft = (x + 72, y)
            inp.draw(s, pygame.mouse.get_pos())
            y += 28

        # 地图尺寸（宽 × 高 + 应用）
        T.draw_text(s, "地图尺寸", (x, y + 4), 11, T.TEXT_DIM)
        self.inputs["w"].rect.topleft = (x + 72, y)
        self.inputs["w"].draw(s, pygame.mouse.get_pos())
        T.draw_text(s, "×", (x + 130, y + 4), 11, T.TEXT_FAINT)
        self.inputs["h"].rect.topleft = (x + 146, y)
        self.inputs["h"].draw(s, pygame.mouse.get_pos())
        self._tool_button(s, pygame.Rect(x + 210, y, 52, 22), "应用", False, ("resize", None))
        y += 30

        # 文件操作
        y += 6
        T.hline(s, x, p.right - 14, y - 4, T.BORDER)
        ops = [("新建", "new"), ("◀ 上一张", "open_prev"), ("下一张 ▶", "open_next"),
               ("重载", "reload"), ("保存", "save"), ("另存为", "save_as"), ("校验", "validate"),
               ("撤销", "undo"), ("重做", "redo")]
        for i, (label, act) in enumerate(ops):
            r = pygame.Rect(x + (i % 4) * 78, y + (i // 4) * 30, 74, 26)
            self._tool_button(s, r, label, False, (act, None),
                              accent=T.GREEN if act == "save" else T.CYAN)
        y += ((len(ops) - 1) // 4 + 1) * 30 + 4
        T.draw_text(s, "左键绘制 · 拖动连续绘制 · 右键擦除", (x, y), 11, T.TEXT_FAINT)
        T.draw_text(s, "中键平移 · 滚轮缩放", (x, y + 13), 11, T.TEXT_FAINT)

    def _tool_button(self, s, r: pygame.Rect, label: str, active: bool,
                     action: tuple, accent=None) -> None:
        # accent 延迟解析：默认参数会在函数定义时固化颜色，主题切换后就改不掉了
        if accent is None:
            accent = T.CYAN
        hov = r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, T.mix(T.PANEL_SUNK, T.CYAN, 0.30) if active
                         else (T.PANEL_HI if hov else T.PANEL_SUNK),
                         r, border_radius=3)
        pygame.draw.rect(s, accent if active else (T.BORDER_HI if hov else T.BORDER),
                         r, 1, border_radius=3)
        T.draw_text(s, label, r.center, 11, T.WHITE if active else T.TEXT_DIM, anchor="center")
        self.buttons.append((r, action[0], action[1]))

    def _side_buttons(self, s, x: int, y: int) -> None:
        for i, side in enumerate((Side.BLUE, Side.RED, Side.NEUTRAL)):
            r = pygame.Rect(x + i * 76, y, 72, 24)
            active = self.brush_side is side
            c = T.side_color(side)
            pygame.draw.rect(s, T.mix(T.PANEL_SUNK, T.CYAN, 0.30) if active else T.PANEL_SUNK,
                         r, border_radius=3)
            pygame.draw.rect(s, c if active else T.BORDER, r, 2 if active else 1, border_radius=3)
            T.draw_text(s, side.label, r.center, 11, c if active else T.TEXT_DIM, anchor="center")
            self.buttons.append((r, "side", side))

    def _draw_template_list(self, s, x: int, y: int, h: int, compact: bool = False) -> int:
        """部署用兵种列表（可按预设编队筛选）。"""
        items = self._filtered_templates()
        # 列表高度随内容收缩，避免筛选后留一大片空白
        h = max(44, min(h, len(items) * 20 + 10))
        r = pygame.Rect(x, y, self.PANEL_W - 28, h)
        pygame.draw.rect(s, T.PANEL_SUNK, r, border_radius=3)
        pygame.draw.rect(s, T.BORDER, r, 1, border_radius=3)
        row_h = 20
        visible = (h - 6) // row_h
        self.tmpl_scroll = max(0, min(self.tmpl_scroll, max(0, len(items) - visible)))
        for i in range(visible):
            idx = i + self.tmpl_scroll
            if idx >= len(items):
                break
            gi, u = items[idx]
            rr = pygame.Rect(r.x + 2, r.y + 3 + i * row_h, r.width - 4, row_h - 1)
            active = gi == self.tmpl_index
            hov = rr.collidepoint(pygame.mouse.get_pos())
            if active:
                pygame.draw.rect(s, (22, 50, 66), rr)
            elif hov:
                pygame.draw.rect(s, (20, 28, 38), rr)
            short = str(u.get("short", u.get("id", "?")))[:6]
            T.draw_text(s, short, (rr.x + 5, rr.centery), 11,
                        T.CYAN if active else T.TEXT_DIM, anchor="midleft", mono=True)
            T.draw_text(s, str(u.get("name", ""))[:14], (rr.x + 46, rr.centery), 11,
                        T.TEXT if active else T.TEXT_FAINT, anchor="midleft")
            T.draw_text(s, str(u.get("domain", ""))[:2], (rr.right - 5, rr.centery), 11,
                        T.TEXT_FAINT, anchor="midright")
            self.buttons.append((rr, "tmpl", gi))
        if len(items) > visible:
            T.draw_text(s, f"滚轮翻页 {self.tmpl_scroll + 1}-{min(len(items), self.tmpl_scroll + visible)}"
                            f"/{len(items)}", (r.x + 4, r.bottom + 3), 11, T.TEXT_FAINT)
        else:
            T.draw_text(s, f"共 {len(items)} 个兵种", (r.x + 4, r.bottom + 3), 11, T.TEXT_FAINT)
        return r.bottom + 16

    # -- 兵种页 -----------------------------------------------------------
    def _draw_units_panel(self, s: pygame.Surface) -> None:
        p = self._panel_rect()
        p.x = 0
        p.width = self.w
        pygame.draw.rect(s, T.PANEL, p)
        x = 18
        y = p.y + 14
        T.draw_text(s, "兵种模板", (x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        list_w = 260
        self.tmpl_scroll = max(0, self.tmpl_scroll)
        lr = pygame.Rect(x, y, list_w, self.h - y - 112)
        pygame.draw.rect(s, T.PANEL_SUNK, lr, border_radius=3)
        pygame.draw.rect(s, T.BORDER, lr, 1, border_radius=3)
        row_h = 22
        visible = (lr.height - 6) // row_h
        units = self._units_doc()
        self.tmpl_scroll = max(0, min(self.tmpl_scroll, max(0, len(units) - visible)))
        for i in range(visible):
            idx = i + self.tmpl_scroll
            if idx >= len(units):
                break
            u = units[idx]
            rr = pygame.Rect(lr.x + 2, lr.y + 3 + i * row_h, lr.width - 4, row_h - 1)
            active = idx == self.tmpl_index
            if active:
                pygame.draw.rect(s, (22, 50, 66), rr)
            elif rr.collidepoint(pygame.mouse.get_pos()):
                pygame.draw.rect(s, (20, 28, 38), rr)
            T.draw_text(s, u.get("short", u.get("id", "?"))[:6], (rr.x + 6, rr.centery), 11,
                        T.TEXT if active else T.TEXT_DIM, anchor="midleft")
            T.draw_text(s, u.get("name", ""), (rr.x + 60, rr.centery), 11,
                        T.TEXT_FAINT, anchor="midleft")
            self.buttons.append((rr, "tmpl", idx))
        # 滚动提示
        T.draw_text(s, "滚轮滚动列表 · 共 %d 个兵种" % len(units), (lr.x, lr.bottom + 6), 11,
                    T.TEXT_FAINT)

        t = self._selected_template()
        if not t:
            return
        x2 = lr.right + 24
        T.draw_text(s, f"{t.get('name', '')}", (x2, y), 16, T.TEXT, bold=True)
        T.draw_text(s, f"{t.get('id', '')} · {t.get('class', '')} · {t.get('domain', '')}",
                    (x2, y + 24), 11, T.TEXT_FAINT)

        yy = y + 52
        stats = [("hp", "耐久", 10), ("mp", "机动", 0.2), ("armor", "装甲", 0.05),
                 ("stealth", "隐身", 0.05), ("evasion", "规避", 0.05), ("cost", "造价", 10),
                 ("sortie_shots", "每回合发射", 1), ("decoy_charges", "诱饵数", 1),
                 ("fort_power", "工事效率", 1)]
        for i, (key, label, step) in enumerate(stats):
            col_x = x2 + (i % 2) * 300
            row_y = yy + (i // 2) * 26
            T.draw_text(s, label, (col_x, row_y + 4), 11, T.TEXT_DIM)
            default = 1 if key == "sortie_shots" else 0
            self._stepper(s, col_x + 90, row_y, t.get(key, default),
                          ("stat", (key, -step)), ("stat", (key, step)))
        yy += 5 * 26 + 6

        # 布尔开关
        flags = [("datalink", "数据链"), ("is_infantry", "步兵"),
                 ("amphibious", "两栖"), ("one_shot_kill", "一击必毁"),
                 ("can_fortify", "可筑工事")]
        fx = x2
        for key, label in flags:
            r = pygame.Rect(fx, yy, 88, 22)
            on = self._flag_value(t, key)
            hov = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (22, 52, 44) if on else T.PANEL_SUNK, r, border_radius=3)
            pygame.draw.rect(s, T.GREEN if on else (T.BORDER_HI if hov else T.BORDER),
                             r, 1, border_radius=3)
            T.draw_text(s, ("● " if on else "○ ") + label, r.center, 11,
                        T.WHITE if on else T.TEXT_DIM, anchor="center")
            self.buttons.append((r, "toggle", key))
            fx += 94
        yy += 30

        # ---- 预设挂载 ----
        T.draw_text(s, "预设挂载（点击套用）", (x2, yy), 11, T.CYAN, bold=True)
        yy += 18
        cx = x2
        for lo in self._loadouts_for(t):
            label = str(lo.get("name", ""))[:10]
            w_ = max(66, T.text_width(label, 10) + 16)
            if cx + w_ > self.w - 40:
                cx = x2
                yy += 24
            r = pygame.Rect(cx, yy, w_, 20)
            hov = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (26, 44, 58) if hov else T.PANEL_SUNK, r, border_radius=3)
            pygame.draw.rect(s, T.CYAN if hov else T.BORDER, r, 1, border_radius=3)
            T.draw_text(s, label, r.center, 11, T.TEXT if hov else T.TEXT_DIM, anchor="center")
            self.buttons.append((r, "loadout", lo.get("id", "")))
            cx += w_ + 6
        yy += 28

        # ---- 武器列表 ----
        T.draw_text(s, "武器与备弹", (x2, yy), 11, T.CYAN, bold=True)
        add_r = pygame.Rect(x2 + 96, yy - 3, 84, 20)
        hov = add_r.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, (24, 52, 54) if hov else T.PANEL_SUNK, add_r, border_radius=3)
        pygame.draw.rect(s, T.GREEN if hov else T.BORDER, add_r, 1, border_radius=3)
        T.draw_text(s, "＋ 添加武器", add_r.center, 11, T.GREEN if hov else T.TEXT_DIM,
                    anchor="center")
        self.buttons.append((add_r, "add_weapon", None))
        yy += 20

        catalog = {w.get("id"): w for w in self.armory_doc.get("weapons", [])}
        entries = t.get("weapons", [])
        if not entries:
            T.draw_text(s, "该兵种尚未装备任何武器（可点击「添加武器」或套用预设挂载）",
                        (x2, yy + 2), 11, T.TEXT_FAINT)
            yy += 22
        for wi, entry in enumerate(entries):
            ref = entry if isinstance(entry, str) else entry.get("ref", "?")
            ammo = 1 if isinstance(entry, str) else entry.get("ammo", 1)
            row = pygame.Rect(x2, yy, self.w - x2 - 40, 26)
            pygame.draw.rect(s, T.PANEL_SUNK, row, border_radius=3)
            pygame.draw.rect(s, T.BORDER, row, 1, border_radius=3)
            cw = catalog.get(ref, {})
            T.draw_text(s, str(cw.get("name", ref))[:22], (row.x + 8, row.centery), 11, T.TEXT,
                        anchor="midleft")
            T.draw_text(s, ref, (row.x + 250, row.centery), 11, T.TEXT_FAINT, anchor="midleft")
            if cw:
                info = (f"射程 {cw.get('range_min', 0):.1f}-{cw.get('range_max', 0):.1f} · "
                        f"Pk {cw.get('pk', 0) * 100:.0f}% · 伤害 {cw.get('damage', 0):.0f} · "
                        f"{cw.get('kind', '')}")
                T.draw_text(s, info, (row.x + 370, row.centery), 11, T.TEXT_FAINT, anchor="midleft")
            T.draw_text(s, "备弹", (row.right - 150, row.centery), 11, T.TEXT_FAINT, anchor="midleft")
            self._stepper(s, row.right - 112, yy + 3, ammo, ("ammo", (wi, -1)), ("ammo", (wi, 1)))
            xr = pygame.Rect(row.right - 28, yy + 5, 18, 16)
            hov = xr.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (58, 26, 30) if hov else T.PANEL, xr, border_radius=2)
            pygame.draw.rect(s, T.RED if hov else T.BORDER, xr, 1, border_radius=2)
            T.draw_text(s, "×", xr.center, 11, T.RED if hov else T.TEXT_DIM, anchor="center")
            self.buttons.append((xr, "rm_weapon", wi))
            yy += 28
            if yy > self.h - 96:
                T.draw_text(s, f"… 另有 {len(entries) - wi - 1} 种武器未显示",
                            (x2, yy), 11, T.TEXT_FAINT)
                break

        # ---- 底部按钮 ----
        by = self.h - 62
        self._tool_button(s, pygame.Rect(18, by, 74, 30), "新建兵种", False,
                          ("new_unit", None), accent=T.CYAN)
        self._tool_button(s, pygame.Rect(98, by, 74, 30), "复制当前", False,
                          ("dup_unit", None), accent=T.CYAN)
        self._tool_button(s, pygame.Rect(178, by, 60, 30), "删除", False,
                          ("del_unit", None), accent=T.RED)
        self._tool_button(s, pygame.Rect(250, by, 120, 30), "保存武器库", False,
                          ("save_armory", None), accent=T.GREEN)
        T.draw_text(s, "修改后点击保存写入 data/units/armory.json", (382, by + 9), 11,
                    T.TEXT_FAINT)

    def _draw_weapon_picker(self, s: pygame.Surface) -> None:
        """武器选择弹层：列出武器库全部武器，点击即加入当前兵种。"""
        veil = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        veil.fill((4, 6, 10, 190))
        s.blit(veil, (0, 0))
        w, h = min(760, self.w - 120), min(560, self.h - 120)
        r = pygame.Rect((self.w - w) // 2, (self.h - h) // 2, w, h)
        T.panel(s, r, T.PANEL_HI, T.BORDER_HI, radius=5)
        T.draw_text(s, "添加武器", (r.x + 20, r.y + 14), 15, T.TEXT, bold=True)
        t = self._selected_template()
        if t:
            T.draw_text(s, f"目标兵种：{t.get('name', '')}（{t.get('id', '')}）",
                        (r.x + 110, r.y + 19), 11, T.TEXT_FAINT)
        close = pygame.Rect(r.right - 84, r.y + 12, 64, 24)
        hov = close.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, (40, 24, 28) if hov else T.PANEL, close, border_radius=3)
        pygame.draw.rect(s, T.RED if hov else T.BORDER, close, 1, border_radius=3)
        T.draw_text(s, "关闭", close.center, 11, T.TEXT_DIM, anchor="center")
        self.buttons.append((close, "close_picker", None))

        list_r = pygame.Rect(r.x + 16, r.y + 46, r.width - 32, r.height - 64)
        pygame.draw.rect(s, T.PANEL_SUNK, list_r, border_radius=3)
        pygame.draw.rect(s, T.BORDER, list_r, 1, border_radius=3)
        row_h = 24
        visible = (list_r.height - 6) // row_h
        cat = self._weapon_catalog()
        self.picker_scroll = max(0, min(self.picker_scroll, max(0, len(cat) - visible)))
        owned = set()
        if t:
            for e in t.get("weapons", []):
                owned.add(e if isinstance(e, str) else e.get("ref"))
        for i in range(visible):
            idx = i + self.picker_scroll
            if idx >= len(cat):
                break
            wd = cat[idx]
            wid = wd.get("id", "?")
            rr = pygame.Rect(list_r.x + 2, list_r.y + 3 + i * row_h, list_r.width - 4, row_h - 1)
            has = wid in owned
            hov = rr.collidepoint(pygame.mouse.get_pos())
            if hov:
                pygame.draw.rect(s, (20, 32, 44), rr)
            T.draw_text(s, str(wd.get("name", wid))[:24], (rr.x + 8, rr.centery), 11,
                        T.TEXT if not has else T.TEXT_FAINT, anchor="midleft")
            T.draw_text(s, wid, (rr.x + 268, rr.centery), 11, T.TEXT_FAINT,
                        anchor="midleft", mono=True)
            T.draw_text(s, f"{wd.get('range_min', 0):.1f}-{wd.get('range_max', 0):.1f}",
                        (rr.x + 360, rr.centery), 11, T.TEXT_DIM, anchor="midleft", mono=True)
            T.draw_text(s, f"Pk {wd.get('pk', 0) * 100:.0f}%", (rr.x + 430, rr.centery), 11,
                        T.TEXT_DIM, anchor="midleft", mono=True)
            T.draw_text(s, f"伤害 {wd.get('damage', 0):.0f}", (rr.x + 490, rr.centery), 11,
                        T.TEXT_DIM, anchor="midleft", mono=True)
            T.draw_text(s, str(wd.get("kind", "")), (rr.x + 566, rr.centery), 11,
                        T.TEXT_FAINT, anchor="midleft")
            if has:
                T.draw_text(s, "已装备", (rr.right - 8, rr.centery), 11, T.GREEN,
                            anchor="midright")
            self.buttons.append((rr, "pick", wid))
        T.draw_text(s, f"滚轮翻页 · 共 {len(cat)} 种武器 · 点击即加入（备弹取默认值，可再用 ± 调整）",
                    (list_r.x + 4, list_r.bottom + 4), 11, T.TEXT_FAINT)

    def _stepper(self, s, x: int, y: int, value, minus: tuple, plus: tuple) -> None:
        rm = pygame.Rect(x, y, 20, 20)
        rp = pygame.Rect(x + 54, y, 20, 20)
        for r, act in ((rm, minus), (rp, plus)):
            hov = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, (30, 44, 58) if hov else T.PANEL_SUNK, r, border_radius=2)
            pygame.draw.rect(s, T.BORDER_HI if hov else T.BORDER, r, 1, border_radius=2)
            T.draw_text(s, "-" if r is rm else "+", r.center, 11, T.TEXT_DIM, anchor="center")
            self.buttons.append((r, act[0], act[1]))
        txt = f"{value:g}" if isinstance(value, float) else str(value)
        T.draw_text(s, txt, (x + 37, y + 10), 11, T.TEXT, anchor="center", mono=True)

    # -- 状态栏 -----------------------------------------------------------
    def _draw_status(self, s: pygame.Surface) -> None:
        r = pygame.Rect(0, self.h - 24, self.w, 24)
        pygame.draw.rect(s, T.PANEL_SUNK, r)
        T.hline(s, 0, self.w, r.y, T.BORDER)
        msg = self.error or self.status
        T.draw_text(s, msg[:120], (12, r.y + 5), 11, T.RED if self.error else T.TEXT_DIM)
        if self.tab == "campaign":
            hint = "Ctrl+S 保存 · Del 删除关卡 · ↑↓ 调整关卡顺序"
        else:
            hint = "Esc 返回 · Ctrl+S 保存"
        T.draw_text(s, hint, (self.w - 12, r.y + 5), 11,
                    T.TEXT_FAINT, anchor="topright")

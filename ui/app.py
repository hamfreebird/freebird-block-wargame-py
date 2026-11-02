"""主程序：窗口、界面布局、事件处理、回合驱动。

界面结构
    顶栏   战役/回合/胜负点/卫星导航状态 + 主要按钮
    地图书  主视口，支持缩放平移、射程包线、探测覆盖、缩略图、事件滚动条
    侧栏   三个页签：单位详情 / 战役态势 / 作战日志
    底栏   操作提示与光标处地块信息

v1.1 新增
    * 教学想定与新手引导（core/tutorial.py 驱动的分步提示）
    * 交战面板「对手视角」预览（core/rules.counter_threat_profile）
    * 单位悬停信息卡（ui/inspect.py）
    * 键位自定义（core/config.py + ui/settings.py）
    * 战报导出（core/report.py）
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

import pygame

from core import rules
from core.config import ConfigError, UserConfig
from core.enums import Detection, Domain, Facility, Side
from core.engine import GameState
from core.env import environment_for
from core.loader import LoadError, list_maps, load_scenario
from core.model import Unit
from core.save import SaveError, list_slots, load_game, save_game, slot_path
from core.tutorial import Tutorial, build_tutorial

from . import theme as T
from .editor import Editor
from .inspect import InspectCard
from .keymap import build_reverse, hotkey_label, key_code
from .mapview import MapView
from .settings import SettingsPanel
from .widgets import Button, LogView

from core import paths as _paths          # 统一资源路径（兼容 zipapp / 冻结版）
from core import report as _report
ROOT = _paths.data_root()
MAP_DIR = _paths.maps_dir()
ARMORY = _paths.armory_path()
ICON_DIR = _paths.icon_dir()

TOP_H = 50
BOTTOM_H = 26
SIDE_W = 400
MIN_W, MIN_H = 1180, 740

# 教学想定在主菜单中的固定入口（不依赖想定文件名）
TUTORIAL_MAP_ID = "training_ground"


class App:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("方块兵棋 · Block Wargame")
        self._set_window_icon()
        self.screen = pygame.display.set_mode((1500, 920), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()
        self.w, self.h = self.screen.get_size()
        self.mode = "menu"           # menu / game / over / editor
        self.running = True
        self.error = ""

        self.state: GameState | None = None
        self.mapview: MapView | None = None
        self.side = Side.BLUE
        self.tab = "unit"
        self.ai_timer = 0.0
        self.ai_delay = 0.24
        self.fast = False
        self.log_drag = False
        self.mouse = (0, 0)
        self.status = ""
        self.status_t = 0.0
        self.show_help = False
        self.show_menu = False
        self.pending_target: Unit | None = None
        self.pending_facility: tuple[int, int] | None = None
        self.decoy_mode: bool = False
        self.decoy_kind: str = "radar"
        self.smoke_mode: bool = False
        self.smoke_weapon = None
        self.prepare_mode: bool = False       # 火力准备：登记射界模式
        self.command_mode: bool = False       # 指挥模式：选择指令目标
        self.command_issuer: Unit | None = None
        self.hover_unit: Unit | None = None
        self.hover_tile: tuple[int, int] | None = None
        self.path_preview: list | None = None
        self.reachable: dict = {}
        self.buttons: list[Button] = []
        self.weapon_rows: list[tuple[pygame.Rect, str]] = []
        self.action_buttons: list[Button] = []   # 侧栏行动按钮（每帧重建，点击时查询）
        self.salvo_rects: list[tuple[pygame.Rect, int]] = []
        self._sat_rects: list[tuple[pygame.Rect, object]] = []
        self.menu_rects: list[tuple[pygame.Rect, str, object]] = []
        self.logview: LogView | None = None
        self.scenarios: list[dict] = []
        self.menu_index = 0
        self.menu_side = Side.BLUE
        self._menu_buttons: list[Button] = []
        self._tab_rects: list[tuple[pygame.Rect, str]] = []
        self._fire_btn = pygame.Rect(0, 0, 0, 0)
        self.slot_info: list[dict] = []
        self.editor: Editor | None = None

        # -- v1.1：用户配置 / 键位自定义 --
        self.cfg = UserConfig.load(_paths.user_config_path())
        self.keymap = build_reverse(self.cfg)
        self.settings_panel: SettingsPanel | None = None
        self.show_settings = False

        # -- v1.1：教学引导 --
        self.tutorial: Tutorial | None = None
        self.tutorial_visible = False
        self._tutorial_notice = 0.0
        self._tutorial_skip_btn = pygame.Rect(0, 0, 0, 0)

        # -- v1.1：悬停信息卡与对手视角 --
        self.inspect = InspectCard()
        self.hover_card = False

        self._scan_scenarios()
        self._layout_menu()

    # ================================================================== #
    # 想定扫描 / 菜单
    # ================================================================== #
    def _set_window_icon(self) -> None:
        """设置窗口/任务栏图标（找不到资源时静默跳过）。"""
        for name in ("icon_256.png", "icon.png"):
            path = os.path.join(ICON_DIR, name)
            if os.path.exists(path):
                try:
                    img = pygame.image.load(path)
                    pygame.display.set_icon(img)
                except Exception:
                    pass
                return

    def _scan_scenarios(self) -> None:
        self.scenarios = []
        for path in list_maps(MAP_DIR):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.scenarios.append({
                    "path": path,
                    "id": raw.get("id", os.path.basename(path)),
                    "name": raw.get("name", os.path.basename(path)),
                    "desc": raw.get("description", ""),
                    "size": raw.get("size", [0, 0]),
                    "turns": raw.get("turn_limit", 0),
                    "tile_km": raw.get("tile_km", 12),
                    "blue": len(raw.get("deployments", {}).get("BLUE", [])),
                    "red": len(raw.get("deployments", {}).get("RED", [])),
                    "tutorial": bool(raw.get("tutorial")),
                })
            except Exception as exc:
                self.error = f"{os.path.basename(path)}: {exc}"
        if not self.scenarios:
            self.error = f"未在 {MAP_DIR} 找到可用地图"
        # 教学想定排在最前，便于新玩家第一眼看到
        self.scenarios.sort(key=lambda s: (not s.get("tutorial"), s["name"]))

    def _layout_menu(self) -> None:
        self._menu_buttons = []

    # -- 菜单按钮几何 -----------------------------------------------------
    def _menu_button_y(self) -> int:
        return 420 + 122

    def _menu_start_rect(self) -> pygame.Rect:
        return pygame.Rect(60, self._menu_button_y(), 240, 46)

    def _menu_edit_rect(self) -> pygame.Rect:
        return pygame.Rect(316, self._menu_button_y(), 200, 46)

    def _menu_tutorial_rect(self) -> pygame.Rect:
        return pygame.Rect(532, self._menu_button_y(), 170, 46)

    def _menu_settings_rect(self) -> pygame.Rect:
        return pygame.Rect(718, self._menu_button_y(), 110, 46)

    # ------------------------------------------------------------------ #
    # 教学想定
    # ------------------------------------------------------------------ #
    def _tutorial_index(self) -> int:
        """教学想定在想定列表中的下标；不存在返回 -1。"""
        for i, sc in enumerate(self.scenarios):
            if sc.get("tutorial") or sc.get("id") == TUTORIAL_MAP_ID:
                return i
        return -1

    def start_tutorial(self) -> None:
        """从主菜单直接进入教学想定。"""
        idx = self._tutorial_index()
        if idx < 0:
            self.set_status("未找到教学想定", 4)
            return
        self.menu_index = idx
        self.start_game()
        if self.state is not None:
            self.tutorial_visible = True
            self._tutorial_notice = 3.0
            self.set_status("教学开始：按提示逐步操作，随时可按「跳过教学」退出", 6)

    def _toggle_tutorial(self) -> None:
        """显示 / 隐藏教学提示条。"""
        if self.tutorial is None:
            return
        self.tutorial_visible = not self.tutorial_visible

    def _skip_tutorial(self) -> None:
        if self.tutorial is not None:
            self.tutorial.skip()
        self.tutorial_visible = False
        self.set_status("已退出教学", 3)

    def _tutorial_note(self, event: str) -> None:
        """把事件投递给教学引导机，并在推进时刷新提示。"""
        if self.tutorial is None or self.tutorial.finished:
            return
        before = self.tutorial.index
        self.tutorial.note(event)
        if self.tutorial.index != before:
            step = self.tutorial.current
            if step is not None:
                self.state.log_message(None, f"教学：{step.title}", "system")
            elif self.tutorial.finished:
                self.state.log_message(None, "教学完成——接下来就看你的了", "system")
                self.tutorial_visible = False
                self.set_status("教学完成！", 4)

    def _tutorial_poll(self) -> None:
        """轮询状态型教学步骤。"""
        if self.tutorial is None or self.tutorial.finished or self.state is None:
            return
        before = self.tutorial.index
        self.tutorial.poll(self.state)
        if self.tutorial.index != before:
            step = self.tutorial.current
            if step is not None:
                self.state.log_message(None, f"教学：{step.title}", "system")
            elif self.tutorial.finished:
                self.state.log_message(None, "教学完成——接下来就看你的了", "system")
                self.tutorial_visible = False
                self.set_status("教学完成！", 4)

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #
    def open_settings(self) -> None:
        """打开键位设置面板。"""
        if self.settings_panel is None:
            self.settings_panel = SettingsPanel(self.cfg)
        self.show_settings = True

    def close_settings(self) -> None:
        """关闭设置面板并保存配置。"""
        self.show_settings = False
        try:
            self.cfg.save()
            self.keymap = build_reverse(self.cfg)
            self.set_status("键位设置已保存", 3)
        except ConfigError as exc:
            self.set_status(str(exc), 5)

    # ------------------------------------------------------------------ #
    # 战报导出
    # ------------------------------------------------------------------ #
    def export_report(self, fmt: str = "md") -> None:
        """把本局作战过程导出为战报文件。"""
        if self.state is None:
            return
        try:
            path = _report.export_report(self.state, _paths.reports_dir(), fmt)
        except OSError as exc:
            self.set_status(f"战报导出失败：{exc}", 5)
            return
        self.set_status(f"战报已导出：{path}", 6)

    def _draw_menu_features(self, s: pygame.Surface, r: pygame.Rect) -> None:
        T.panel(s, r, T.PANEL, T.BORDER, radius=5)
        T.draw_text(s, "机制速览", (r.x + 16, r.y + 14), 13, T.CYAN, bold=True)
        T.hline(s, r.x + 16, r.right - 16, r.y + 36, T.BORDER)
        items = [
            ("侦察—打击链", "目标须被传感器跟踪至「跟踪」级才能装订诸元；"
                            "情报中断则先进弹药也无用武之地。"),
            ("电子战", "干扰压制雷达与制导链路，并削弱卫星导航；"
                       "反辐射导弹专打开机雷达。"),
            ("防空拦截", "多层防空对来袭弹药独立拦截取并集；"
                         "高超音速与隐身弹药可大幅突防。"),
            ("防区外打击", "远程弹药有飞行时间，途中目标机动或情报中断都会导致脱靶。"),
            ("天基支持", "卫星每 3 回合过顶侦察；卫导完整性直接决定精确制导弹药精度。"),
        ]
        yy = r.y + 48
        for title, desc in items:
            if yy > r.bottom - 30:
                break
            pygame.draw.rect(s, T.CYAN, pygame.Rect(r.x + 16, yy + 3, 2, 10))
            T.draw_text(s, title, (r.x + 24, yy), 12, T.TEXT, bold=True)
            yy += 17
            for line in T.wrap_text(desc, 10, r.width - 40):
                T.draw_text(s, line, (r.x + 24, yy), 10, (146, 164, 184))
                yy += 14
            yy += 6

    def _draw_menu(self) -> None:
        s = self.screen
        s.fill(T.BG)
        # 背景网格
        for x in range(0, self.w, 48):
            pygame.draw.line(s, (16, 22, 30), (x, 0), (x, self.h))
        for y in range(0, self.h, 48):
            pygame.draw.line(s, (16, 22, 30), (0, y), (self.w, y))

        T.draw_text(s, "方块兵棋", (60, 54), 40, T.TEXT, bold=True)
        T.draw_text(s, "BLOCK WARGAME · 现代联合作战推演", (62, 104), 15, T.CYAN)
        T.draw_text(s, "n×n 方块战场 · 回合制 · 侦察-打击链 · 电子战 · 防区外打击", (62, 128), 12, T.TEXT_DIM)
        T.hline(s, 60, self.w - 60, 158, T.BORDER)

        T.draw_text(s, "选择想定", (60, 176), 13, T.TEXT_DIM, bold=True)
        card_w = min(430, (self.w - 150) // max(1, len(self.scenarios)) - 16)
        card_h = 168
        x = 60
        for i, sc in enumerate(self.scenarios):
            r = pygame.Rect(x, 200, card_w, card_h)
            active = i == self.menu_index
            is_tut = bool(sc.get("tutorial"))
            T.panel(s, r, T.PANEL_HI if active else T.PANEL,
                    T.CYAN if active else T.BORDER, radius=5)
            if active:
                pygame.draw.rect(s, T.CYAN, pygame.Rect(r.x, r.y, r.w, 2))
            if is_tut:
                # 教学想定：左上角角标 + 高亮边框
                pygame.draw.rect(s, T.GREEN, r, 1, border_radius=5)
                badge = pygame.Rect(r.x + 12, r.y + 12, 56, 17)
                pygame.draw.rect(s, (26, 74, 58), badge, border_radius=3)
                T.draw_text(s, "教学", badge.center, 10, (170, 255, 210), anchor="center")
            T.draw_text(s, sc["name"], (r.x + 16, r.y + (38 if is_tut else 16)), 17,
                        T.TEXT, bold=True)
            T.draw_text(s, f"{sc['size'][0]}×{sc['size'][1]} 格 · {sc['tile_km']} km/格 · "
                            f"限 {sc['turns']} 回合 · 蓝{sc['blue']}/红{sc['red']}",
                        (r.x + 16, r.y + 62), 11, T.TEXT_FAINT)
            yy = r.y + 84
            for line in T.wrap_text(sc["desc"], 12, r.width - 32)[:3]:
                T.draw_text(s, line, (r.x + 16, yy), 12, T.TEXT_DIM)
                yy += 18
            T.draw_text(s, "点击选择", (r.x + 16, r.bottom - 26), 11,
                        T.CYAN if active else T.TEXT_FAINT)
            if r.collidepoint(self.mouse):
                self.menu_index = i
            x += card_w + 18

        # 右侧机制速览
        px = x + 4
        pw = self.w - px - 60
        if pw >= 250:
            self._draw_menu_features(s, pygame.Rect(px, 200, pw, card_h + 128))

        # 阵营选择
        y = 420
        T.draw_text(s, "选择阵营", (60, y), 13, T.TEXT_DIM, bold=True)
        for j, (side, label, desc) in enumerate([
            (Side.BLUE, "蓝方", "联合部队：体系化侦察与防区外精确打击"),
            (Side.RED, "红方", "区域拒止：远程防空与饱和反舰突击"),
        ]):
            r = pygame.Rect(60 + j * 330, y + 26, 310, 66)
            active = self.menu_side is side
            T.panel(s, r, T.PANEL_HI if active else T.PANEL,
                    T.side_color(side) if active else T.BORDER, radius=4)
            pygame.draw.rect(s, T.side_color(side), pygame.Rect(r.x + 12, r.y + 16, 4, 34))
            T.draw_text(s, label, (r.x + 26, r.y + 14), 16, T.TEXT, bold=True)
            T.draw_text(s, desc, (r.x + 26, r.y + 38), 11, T.TEXT_DIM)
            if r.collidepoint(self.mouse):
                self.menu_side = side

        # 开始按钮
        start = self._menu_start_rect()
        hover = start.collidepoint(self.mouse)
        pygame.draw.rect(s, (30, 104, 128) if hover else (22, 78, 98), start, border_radius=4)
        pygame.draw.rect(s, T.CYAN, start, 1, border_radius=4)
        T.draw_text(s, "开始作战  [Enter]", start.center, 16, T.WHITE, bold=True, anchor="center")

        edit_r = self._menu_edit_rect()
        hover = edit_r.collidepoint(self.mouse)
        pygame.draw.rect(s, (26, 42, 56) if hover else (18, 26, 36), edit_r, border_radius=4)
        pygame.draw.rect(s, T.CYAN if hover else T.BORDER_HI, edit_r, 1, border_radius=4)
        T.draw_text(s, "地图 / 武器编辑器", edit_r.center, 13,
                    T.WHITE if hover else T.TEXT, anchor="center")

        # 快速上手：直接进入教学想定（首次运行时高亮）
        tut_r = self._menu_tutorial_rect()
        hover = tut_r.collidepoint(self.mouse)
        first_run = self.cfg.first_run
        fill = (30, 82, 62) if hover else ((22, 62, 48) if first_run else (18, 26, 36))
        pygame.draw.rect(s, fill, tut_r, border_radius=4)
        pygame.draw.rect(s, T.GREEN if (hover or first_run) else T.BORDER_HI, tut_r, 1,
                         border_radius=4)
        T.draw_text(s, "快速上手  [T]", tut_r.center, 13,
                    T.WHITE if hover else (T.TEXT if not first_run else (180, 255, 215)),
                    bold=first_run, anchor="center")

        # 设置入口
        set_r = self._menu_settings_rect()
        hover = set_r.collidepoint(self.mouse)
        pygame.draw.rect(s, (26, 42, 56) if hover else (18, 26, 36), set_r, border_radius=4)
        pygame.draw.rect(s, T.CYAN if hover else T.BORDER_HI, set_r, 1, border_radius=4)
        T.draw_text(s, "设置", set_r.center, 13, T.WHITE if hover else T.TEXT, anchor="center")

        if first_run:
            T.draw_text(s, "首次使用？点「快速上手」用 3 分钟学会核心操作",
                        (60, y + 190), 12, T.GREEN)

        tips = [
            "数据可编辑：data/maps/*.json 定义地图与部署，data/units/armory.json 定义武器与兵种",
            "左键选择/移动，右键下达机动指令，滚轮缩放，中键拖动平移，空格结束回合，F1 查看规则",
        ]
        yy = self.h - 92
        for t in tips:
            T.draw_text(s, t, (60, yy), 12, T.TEXT_FAINT)
            yy += 20
        if self.error:
            T.draw_text(s, self.error, (60, self.h - 44), 12, T.RED)

    # ================================================================== #
    # 开局
    # ================================================================== #
    def start_game(self) -> None:
        if not self.scenarios:
            return
        path = self.scenarios[self.menu_index]["path"]
        try:
            scenario = load_scenario(path, ARMORY)
        except LoadError as exc:
            self.error = str(exc)
            return
        self.state = GameState(scenario, self.menu_side)
        self.state.scenario_path = path
        self.state.armory_path = ARMORY
        self.side = self.menu_side
        self.mode = "game"
        self.tab = "unit"
        self.pending_target = None
        self.pending_facility = None
        self.show_menu = False
        self.show_settings = False
        self.ai_timer = 0.0
        self.status = ""
        # 教学引导：仅当想定声明了教学脚本时启用
        self.tutorial = build_tutorial(scenario.raw)
        self.tutorial_visible = bool(scenario.raw.get("tutorial")) and not self.cfg.first_run
        if self.tutorial_visible:
            self.state.log_message(None, f"教学开始：{self.tutorial.current.title if self.tutorial.current else ''}", "system")
        self._layout_game()
        self._select_first_unit()

    # -- 系统菜单 / 存档 --------------------------------------------------
    def toggle_menu(self) -> None:
        self.show_menu = not self.show_menu
        if self.show_menu:
            self._refresh_slots()

    def _refresh_slots(self) -> None:
        self.slot_info = list_slots(ROOT)

    def save_slot(self, i: int) -> None:
        if self.state is None:
            return
        try:
            save_game(self.state, slot_path(ROOT, i))
            self.set_status(f"已保存至槽位 {i}（第 {self.state.turn_count} 回合）")
        except SaveError as exc:
            self.set_status(str(exc), 5)
        self._refresh_slots()

    def load_slot(self, i: int) -> None:
        path = slot_path(ROOT, i)
        try:
            st = load_game(path, ARMORY)
        except SaveError as exc:
            self.set_status(str(exc), 5)
            return
        self.state = st
        self.side = st.player_side
        self.mode = "game"
        self.show_menu = False
        self.show_help = False
        self.pending_target = None
        self.pending_facility = None
        self.ai_timer = 0.0
        self.tab = "unit"
        self.mapview.state = st
        self._layout_game()
        self.mapview.state = st
        self.mapview.fit()
        self._select_first_unit()
        self.set_status(f"已读取槽位 {i}：第 {st.turn_count} 回合")

    def _layout_game(self) -> None:
        self.w, self.h = self.screen.get_size()
        map_rect = pygame.Rect(0, TOP_H, self.w - SIDE_W, self.h - TOP_H - BOTTOM_H)
        if self.mapview is None:
            self.mapview = MapView(map_rect, self.state)
        else:
            self.mapview.rect = map_rect
            self.mapview.state = self.state
        self.mapview.fit()
        self._build_buttons()
        side_x = self.w - SIDE_W
        if self.logview is None:
            self.logview = LogView(pygame.Rect(side_x + 12, TOP_H + 48, SIDE_W - 24, 200), size=12)

    def _build_buttons(self) -> None:
        self.buttons = []
        x = self.w - 14
        specs = [
            ("结束回合", self.end_turn, "primary", "end_turn"),
            ("帮助", self.toggle_help, "ghost", "help"),
        ]
        for label, cb, kind, act in specs:
            width = 96 if label == "结束回合" else 62
            x -= width
            self.buttons.append(Button(pygame.Rect(x, 9, width, 32), label, cb, kind,
                                       size=13, hotkey=hotkey_label(self.cfg, act)))
            x -= 8
        # 叠加层切换
        x -= 6
        self.overlay_btn = Button(pygame.Rect(x - 84, 9, 84, 32), "覆盖层", self.cycle_overlay,
                                  "ghost", size=12)
        self.buttons.append(self.overlay_btn)
        # 设置
        x -= 8
        self.settings_btn = Button(pygame.Rect(x - 62, 9, 62, 32), "设置", self.open_settings,
                                   "ghost", size=12)
        self.buttons.append(self.settings_btn)
        # 系统菜单（存档/读档/返回）
        x -= 180
        self.menu_btn = Button(pygame.Rect(x, 9, 82, 32), "菜单", self.toggle_menu,
                               "ghost", size=12, hotkey=hotkey_label(self.cfg, "system_menu"))
        self.buttons.append(self.menu_btn)

    def _select_first_unit(self) -> None:
        for u in self.state.units_of(self.side):
            if u.mp > 0.5:
                self.select_unit(u)
                return
        us = self.state.units_of(self.side)
        if us:
            self.select_unit(us[0])

    # ================================================================== #
    # 交互
    # ================================================================== #
    def select_unit(self, u: Unit | None) -> None:
        self.state.select(u)
        self.pending_target = None
        self.tab = "unit"
        if u:
            self.reachable = rules.flood_reachable(self.state, u)
            self.reachable.pop((u.x, u.y), None)
            self.bring_into_view(u.x, u.y)
            self._tutorial_note("select_unit")
        else:
            self.reachable = {}

    def bring_into_view(self, gx: int, gy: int) -> None:
        mv = self.mapview
        r = mv.grid_to_rect(gx, gy)
        if mv.rect.collidepoint(r.center):
            return
        mv.ox -= r.centerx - mv.rect.centerx
        mv.oy -= r.centery - mv.rect.centery
        mv._clamp()

    def end_turn(self) -> None:
        if self.mode != "game" or self.state is None:
            return
        if self.state.phase != "player" or self.state.over:
            return
        self.pending_target = None
        self.reachable = {}
        self.state.end_player_turn()
        self._tutorial_note("turn_end")
        if self.state.over:          # 末回合/全歼可能在结算时直接结束对局
            self.mode = "over"
            return
        self.state.begin_ai_phase()
        self.ai_timer = 0.0

    def toggle_help(self) -> None:
        self.show_help = not self.show_help

    def cycle_overlay(self) -> None:
        if self.mapview is None:
            return
        order = ["none", "sensors", "threat"]
        i = order.index(self.mapview.overlay) if self.mapview.overlay in order else 0
        self.mapview.overlay = order[(i + 1) % len(order)]
        names = {"none": "无覆盖", "sensors": "侦察覆盖", "threat": "敌方防空包线"}
        self.set_status(f"叠加层：{names[self.mapview.overlay]}")

    def set_status(self, text: str, ttl: float = 3.0) -> None:
        self.status = text
        self.status_t = ttl

    def next_unit(self) -> None:
        us = [u for u in self.state.units_of(self.side) if u.mp > 0.5 and not u.fired]
        if not us:
            return
        cur = self.state.selected_unit()
        if cur in us:
            i = (us.index(cur) + 1) % len(us)
        else:
            i = 0
        self.select_unit(us[i])

    def order_move(self, gx: int, gy: int) -> None:
        u = self.state.selected_unit()
        if not u or self.state.phase != "player":
            return
        path = rules.find_path(self.state, u, (gx, gy))
        if not path:
            self.set_status("无法抵达该位置")
            return
        cost = rules.path_cost(self.state, u, path)
        if cost > u.mp + 1e-6:
            self.set_status(f"机动点不足：需要 {cost:.1f}，剩余 {u.mp:.1f}")
            return
        self.state.move_unit(u, path)
        self.reachable = rules.flood_reachable(self.state, u)
        self.reachable.pop((u.x, u.y), None)

    def confirm_fire(self) -> None:
        u = self.state.selected_unit()
        if self.state.phase != "player":
            return
        if self.pending_facility is not None:
            if not u:
                return
            w = u.weapon_by_id(self.state.selected_weapon or "")
            if w is None:
                self.set_status("本单位无可用武器")
                return
            res = self.state.strike_facility(u, self.pending_facility, w)
            self.set_status(res.message, 3.5)
            self.pending_facility = None
            self.reachable = {}
            if self.state.over:
                self.mode = "over"
            return

        t = self.pending_target
        if not u or not t or not t.alive:
            return
        w = u.weapon_by_id(self.state.selected_weapon or "")
        if w is None:
            self.set_status("本单位无可用武器")
            return
        res = self.state.fire(u, t, w)
        self.set_status(res.message, 3.5)
        if res.ok:
            self._tutorial_note("fire")
        self.pending_target = None
        self.reachable = {}
        if self.state.over:
            self.mode = "over"

    # ------------------------------------------------------------------ #
    def handle_event(self, e: pygame.event.Event) -> None:
        self.mouse = pygame.mouse.get_pos()
        if self.mode == "editor" and self.editor is not None:
            self.editor.handle_event(e)
            if self.editor.quit:
                self.editor = None
                self.mode = "menu"
                self._scan_scenarios()
            return
        if e.type == pygame.QUIT:
            self.running = False
        elif e.type == pygame.VIDEORESIZE:
            self.screen = pygame.display.set_mode((max(MIN_W, e.w), max(MIN_H, e.h)),
                                                  pygame.RESIZABLE)
            self.w, self.h = self.screen.get_size()
            if self.mode == "game":
                self._layout_game()
        elif e.type == pygame.KEYDOWN:
            self._on_key(e)
        elif e.type == pygame.MOUSEBUTTONDOWN:
            self._on_mouse_down(e)
        elif e.type == pygame.MOUSEBUTTONUP:
            if e.button == 2:
                self.log_drag = False
        elif e.type == pygame.MOUSEMOTION:
            if self.log_drag and self.mapview:
                self.mapview.pan(e.rel[0], e.rel[1])
        elif e.type == pygame.MOUSEWHEEL:
            if self.mode != "game":
                return
            if self.mouse[0] >= self.w - SIDE_W and self.tab == "log" and self.logview:
                self.logview.scroll_by(e.y * 3)
            elif self.mapview and self.mapview.contains(self.mouse):
                self.mapview.zoom_at(self.mouse, 1 if e.y > 0 else -1)

    def _on_key(self, e: pygame.event.Event) -> None:
        k = e.key
        action = self.keymap.get(k, "")

        # 设置面板优先：捕获按键进行重绑定
        if self.show_settings and self.settings_panel is not None:
            if self.settings_panel.handle_event(e):
                if getattr(self.settings_panel, "close_requested", False):
                    self.settings_panel.close_requested = False
                    self.close_settings()
                return

        if self.mode == "menu":
            if k in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.start_game()
            elif k == pygame.K_ESCAPE:
                self.running = False
            elif k in (pygame.K_LEFT, pygame.K_a):
                self.menu_index = (self.menu_index - 1) % max(1, len(self.scenarios))
            elif k in (pygame.K_RIGHT, pygame.K_d):
                self.menu_index = (self.menu_index + 1) % max(1, len(self.scenarios))
            elif k in (pygame.K_TAB, pygame.K_s):
                self.menu_side = Side.RED if self.menu_side is Side.BLUE else Side.BLUE
            elif k == pygame.K_t:
                self.start_tutorial()
            return

        # 绑定可配置的动作（帮助 / 存读档 / 系统菜单）
        if action == "help":
            self.toggle_help()
            return
        if self.show_help:
            if action == "cancel" or action == "help":
                self.show_help = False
            return

        if action == "quick_save":
            self.save_slot(1)
            return
        if action == "quick_load":
            self.load_slot(1)
            return
        if action == "system_menu":
            self.toggle_menu()
            return
        if self.show_menu:
            if action == "cancel":
                self.show_menu = False
            return

        if self.mode == "over":
            if k == pygame.K_RETURN:
                self.mode = "menu"
                self._scan_scenarios()
            elif k == pygame.K_ESCAPE:
                self.running = False
            return

        st = self.state
        if action == "end_turn" and st.phase == "player":
            self.end_turn()
        elif action == "end_turn" and st.phase == "ai":
            self.fast = True
        elif action == "cancel":
            if self.pending_target:
                self.pending_target = None
            elif self.pending_facility:
                self.pending_facility = None
            elif self.decoy_mode or self.smoke_mode or self.prepare_mode or self.command_mode:
                self.decoy_mode = False
                self.smoke_mode = False
                self.prepare_mode = False
                self.command_mode = False
                self.command_issuer = None
                self.set_status("已取消当前操作")
            else:
                self.select_unit(None)
        elif action == "next_unit":
            self.next_unit()
        elif action == "radar":
            u = st.selected_unit()
            if u:
                st.toggle_radar(u)
        elif action == "jam":
            u = st.selected_unit()
            if u:
                st.toggle_jam(u)
        elif action == "overlay":
            self.cycle_overlay()
        elif action == "fit_map":
            self.mapview.fit()
        elif action == "confirm":
            self.confirm_fire()
        elif action.startswith("weapon_") and action[7:].isdigit():
            idx = int(action[7:]) - 1
            ws = st.selected_unit().usable_weapons() if st.selected_unit() else []
            if idx < len(ws):
                st.selected_weapon = ws[idx].id

    def _on_mouse_down(self, e: pygame.event.Event) -> None:
        pos = e.pos
        if self.mode == "menu":
            if e.button == 1:
                if self._menu_start_rect().collidepoint(pos):
                    self.start_game()
                elif self._menu_edit_rect().collidepoint(pos):
                    self.open_editor()
                elif self._menu_tutorial_rect().collidepoint(pos):
                    self.start_tutorial()
                elif self._menu_settings_rect().collidepoint(pos):
                    self.open_settings()
            return
        if self.mode == "over":
            if e.button == 1:
                if pygame.Rect(self.w // 2 - 130, self.h // 2 + 108, 260, 44).collidepoint(pos):
                    self.mode = "menu"
                    self._scan_scenarios()
            return

        if self.show_help:
            return
        if self.show_menu:
            if e.button == 1:
                for rect, action, arg in self.menu_rects:
                    if rect.collidepoint(pos):
                        if action == "resume":
                            self.show_menu = False
                        elif action == "save":
                            self.save_slot(int(arg))
                        elif action == "load":
                            self.load_slot(int(arg))
                        elif action == "to_menu":
                            self.show_menu = False
                            self.mode = "menu"
                            self._scan_scenarios()
                        elif action == "export_md":
                            self.export_report("md")
                        elif action == "settings":
                            self.show_menu = False
                            self.open_settings()
                        elif action == "quit":
                            self.running = False
                        return
            return
        if e.button == 2:
            self.log_drag = True
            return

        st = self.state
        # 顶栏按钮
        if pos[1] < TOP_H:
            for b in self.buttons:
                if b.handle_click(pos):
                    return
            return

        # 教学提示条的跳过按钮
        if self.tutorial_visible and self._tutorial_skip_btn.collidepoint(pos) \
                and e.button == 1:
            self._skip_tutorial()
            return

        # 侧栏
        side_x = self.w - SIDE_W
        if pos[0] >= side_x:
            self._on_sidebar_click(pos)
            return

        if not self.mapview.contains(pos):
            return
        grid = self.mapview.screen_to_grid(pos)
        if grid is None:
            return
        u = st.unit_at(*grid)

        if e.button == 3:
            if st.selected_unit():
                self.order_move(*grid)
            return

        if self.decoy_mode and e.button == 1:
            self._place_decoy(grid)
            return
        if self.smoke_mode and e.button == 1:
            self._place_smoke(grid)
            return
        if self.prepare_mode and e.button == 1:
            self._place_prepare_fire(grid)
            return
        if self.command_mode and e.button == 1:
            self._place_command(grid)
            return

        if e.button != 1:
            return

        if u is not None and u.side is self.side:
            # 同格多支部队（如机场停驻的航空兵）时循环选择
            same = [x for x in st.units_at(*grid) if x.side is self.side]
            cur = st.selected_unit()
            if len(same) > 1 and cur in same:
                self.select_unit(same[(same.index(cur) + 1) % len(same)])
            else:
                self.select_unit(u)
            return

        sel = st.selected_unit()

        # 对敌方固定设施的打击（机场/港口/后勤枢纽）
        tile = st.map.at(*grid)
        if (u is None and sel is not None and tile is not None
                and tile.facility is not Facility.NONE and tile.owner is not self.side
                and tile.owner is not Side.NEUTRAL):
            w = sel.weapon_by_id(st.selected_weapon or "")
            prof = rules.facility_strike_profile(st, sel, tile, w) if w else None
            if prof is None or not prof.ok:
                self.set_status(f"无法打击该设施：{prof.reason if prof else '无可用武器'}")
                return
            if self.pending_facility == grid:
                self.confirm_fire()
            else:
                self.pending_target = None
                self.pending_facility = grid
                self.tab = "unit"
                self.set_status(
                    f"选定设施 {tile.facility.label}({tile.x},{tile.y})｜{w.name} "
                    f"Pk {prof.pk * 100:.0f}%｜再次点击或回车确认打击", 5)
            return

        if u is not None and u.side is not self.side and sel is not None:
            lvl = st.known_level(self.side, u)
            if lvl.level < Detection.CLASSIFIED.level:
                self.set_status("目标未识别，无法装订射击诸元")
                return
            if self.pending_target is u:
                self.confirm_fire()
            else:
                self.pending_target = u
                self.state.select(sel)
                self.tab = "unit"
                w = sel.weapon_by_id(st.selected_weapon or "")
                if w:
                    p = rules.shot_profile(st, sel, u, w)
                    if p.ok:
                        self.set_status(f"选定目标 {u.name}｜{w.name} Pk {p.pk * 100:.0f}%"
                                        f"｜再次点击或回车确认开火", 5)
                    else:
                        self.set_status(f"无法射击：{p.reason}", 4)
            return

        if sel is not None:
            self.order_move(*grid)
        else:
            self.select_unit(None)

    def _on_sidebar_click(self, pos: tuple[int, int]) -> None:
        # 页签
        for rect, key in self._tab_rects:
            if rect.collidepoint(pos):
                self.tab = key
                return
        st = self.state
        if self.tab == "ops":
            for rect, sat in self._sat_rects:
                if rect.collidepoint(pos):
                    u = st.selected_unit()
                    if u is None:
                        self.set_status("先在「单位详情」中选择一个具备反卫星能力的单位", 4)
                        return
                    w = next((x for x in u.usable_weapons() if x.asat), None)
                    if w is None:
                        self.set_status(f"{u.name} 不具备反卫星能力", 4)
                        return
                    st.selected_weapon = w.id
                    self.pending_target = sat
                    self.pending_facility = None
                    self.tab = "unit"
                    prof = rules.shot_profile(st, u, sat, w)
                    self.set_status(
                        f"反卫星交战：{sat.name}｜{w.name} Pk {prof.pk * 100:.0f}%"
                        f"｜回车确认发射", 5)
                    return
        # 侧栏行动按钮（雷达 / 干扰 / 筑工事 / 诱饵 / 剖面 / 烟幕 / 待机）
        for b in self.action_buttons:
            if b.handle_click(pos):
                return
        if self.tab == "unit":
            for rect, wid in self.weapon_rows:
                if rect.collidepoint(pos):
                    st.selected_weapon = wid
                    return
            for rect, n in self.salvo_rects:
                if rect.collidepoint(pos):
                    self.state.salvo_setting = n
                    return
            if self._fire_btn.collidepoint(pos):
                self.confirm_fire()
                return
        if self.logview and self.logview.rect.collidepoint(pos):
            pass

    # ================================================================== #
    # 更新
    # ================================================================== #
    def update(self, dt: float) -> None:
        if self.mode != "game" or self.state is None:
            return
        st = self.state
        st.tick_fx(dt)
        if self.status_t > 0:
            self.status_t -= dt
        if self.mapview:
            keys = pygame.key.get_pressed()
            sp = 420 * dt
            if keys[pygame.K_LEFT] and not keys[pygame.K_LSHIFT]:
                self.mapview.pan(sp, 0)
            if keys[pygame.K_RIGHT]:
                self.mapview.pan(-sp, 0)
            if keys[pygame.K_UP]:
                self.mapview.pan(0, sp)
            if keys[pygame.K_DOWN]:
                self.mapview.pan(0, -sp)

        if st.phase == "ai":
            self.ai_timer += dt * (6.0 if (self.fast or pygame.key.get_pressed()[pygame.K_SPACE]) else 1.0)
            if self.ai_timer >= self.ai_delay:
                self.ai_timer = 0.0
                info = st.ai_step()
                if info is None:
                    st.finish_ai_turn()
                    self.fast = False
                    if st.over:
                        self.mode = "over"
                    self._after_turn()
        self.hover_unit = None
        self.hover_card = False
        if self.mapview and self.mapview.contains(self.mouse) and self.mode == "game":
            g = self.mapview.screen_to_grid(self.mouse)
            self.hover_tile = g
            if g:
                u = st.unit_at(*g)
                if u and (u.side is self.side or st.known_level(self.side, u).level >= 2):
                    self.hover_unit = u
                    self.hover_card = True
                    if u.side is not self.side:
                        # 教学：悬停敌方目标
                        self._tutorial_note("hover_enemy")
            sel = st.selected_unit()
            self.path_preview = None
            if sel and g and not (self.hover_unit and self.hover_unit.side is not self.side):
                if (gx := g) != (sel.x, sel.y):
                    p = rules.find_path(st, sel, g)
                    if p and rules.path_cost(st, sel, p) <= sel.mp + 1e-6:
                        self.path_preview = p
        else:
            self.hover_tile = None
            self.path_preview = None

        # 教学：状态型步骤轮询（跟踪判定等）
        self._tutorial_poll()

    def _after_turn(self) -> None:
        st = self.state
        sel = st.selected_unit()
        if sel is None or not sel.alive:
            self._select_first_unit()
        else:
            self.reachable = rules.flood_reachable(st, sel)
            self.reachable.pop((sel.x, sel.y), None)

    # ================================================================== #
    # 绘制
    # ================================================================== #
    def open_editor(self) -> None:
        if self.editor is None:
            self.editor = Editor(self.screen)
        self.editor.w, self.editor.h = self.screen.get_size()
        self.editor.quit = False
        self.mode = "editor"

    def draw(self) -> None:
        if self.mode == "editor":
            if self.editor:
                self.editor.draw(self.screen)
            pygame.display.flip()
            return
        if self.mode == "menu":
            self._draw_menu()
            if self.show_settings and self.settings_panel is not None:
                self.settings_panel.draw(self.screen)
            pygame.display.flip()
            return
        s = self.screen
        s.fill(T.BG)
        self._draw_topbar(s)
        self.mapview.draw(s, self.hover_tile, self.reachable, self.path_preview,
                          self.pending_target)
        if self.pending_facility is not None:
            self.mapview._draw_reticle(s, self.mapview.grid_to_rect(*self.pending_facility))
        self._draw_map_overlay_widgets(s)
        self._draw_sidebar(s)
        self._draw_bottom(s)
        self._draw_tutorial_bar(s)
        if self.hover_card and self.hover_unit is not None:
            known = (Detection.TRACKED if self.hover_unit.side is self.side
                     else self.state.known_level(self.side, self.hover_unit))
            self.inspect.draw(s, self.hover_unit, self.side, known, self.state, self.mouse)
        if self.show_help:
            self._draw_help(s)
        if self.show_menu:
            self._draw_system_menu(s)
        if self.show_settings and self.settings_panel is not None:
            self.settings_panel.draw(s)
        if self.mode == "over":
            self._draw_over(s)
        pygame.display.flip()

    # -- 教学提示条 -------------------------------------------------------
    def _draw_tutorial_bar(self, s: pygame.Surface) -> None:
        """在地图上方绘制当前教学步骤的提示条。"""
        if not self.tutorial_visible or self.tutorial is None:
            return
        step = self.tutorial.current
        if step is None:
            return
        mv = self.mapview
        w = min(660, mv.rect.width - 40)
        x = mv.rect.x + (mv.rect.width - w) // 2
        lines = T.wrap_text(step.body, 11, w - 30)
        h = 40 + len(lines) * 16
        r = pygame.Rect(x, mv.rect.y + 8, w, h)
        surf = pygame.Surface(r.size, pygame.SRCALPHA)
        surf.fill((12, 24, 30, 236))
        s.blit(surf, r.topleft)
        pygame.draw.rect(s, T.GREEN, r, 1, border_radius=4)

        T.draw_text(s, step.title, (r.x + 12, r.y + 7), 12, (170, 255, 210), bold=True)
        T.draw_text(s, self.tutorial.progress_text, (r.right - 12, r.y + 8), 10,
                    T.TEXT_FAINT, anchor="topright")
        if step.goal:
            T.draw_text(s, "目标：" + step.goal, (r.right - 12 - T.text_width(
                self.tutorial.progress_text, 10) - 14, r.y + 8), 10, T.AMBER,
                anchor="topright")
        yy = r.y + 26
        for line in lines[:5]:
            T.draw_text(s, line, (r.x + 12, yy), 11, (200, 220, 235))
            yy += 16

        # 跳过按钮
        b = pygame.Rect(r.right - 74, r.bottom - 26, 64, 20)
        hover = b.collidepoint(self.mouse)
        pygame.draw.rect(s, (30, 48, 44) if hover else (20, 32, 30), b, border_radius=3)
        pygame.draw.rect(s, T.BORDER_HI if hover else T.BORDER, b, 1, border_radius=3)
        T.draw_text(s, "跳过教学", b.center, 10, T.TEXT_DIM, anchor="center")
        self._tutorial_skip_btn = b

    # -- 系统菜单（存档/读档） --------------------------------------------
    def _draw_system_menu(self, s: pygame.Surface) -> None:
        dim = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        dim.fill((5, 8, 12, 190))
        s.blit(dim, (0, 0))

        slots = self.slot_info or list_slots(ROOT)
        w, row_h = 520, 62
        h = 96 + len(slots) * row_h + 76
        r = pygame.Rect((self.w - w) // 2, (self.h - h) // 2, w, h)
        T.panel(s, r, (14, 19, 27), T.BORDER_HI, radius=6)
        T.draw_text(s, "系统菜单", (r.x + 22, r.y + 16), 17, T.TEXT, bold=True)
        T.draw_text(s, "F5 快速保存 · F9 快速读取", (r.right - 22, r.y + 20), 10,
                    T.TEXT_FAINT, anchor="topright")
        T.hline(s, r.x + 20, r.right - 20, r.y + 44, T.BORDER)

        self.menu_rects = []
        y = r.y + 54
        for info in slots:
            row = pygame.Rect(r.x + 16, y, w - 32, row_h - 8)
            pygame.draw.rect(s, T.PANEL_SUNK, row, border_radius=3)
            pygame.draw.rect(s, T.BORDER, row, 1, border_radius=3)
            T.draw_text(s, f"槽位 {info['slot']}", (row.x + 12, row.y + 8), 12, T.TEXT, bold=True)
            if info.get("exists"):
                ts = time.strftime("%m-%d %H:%M", time.localtime(info.get("mtime", 0)))
                sc_name = os.path.splitext(info.get("scenario", ""))[0]
                T.draw_text(s, f"第 {info.get('turn', 0)} 回合 · {sc_name} · "
                                f"{'蓝方' if info.get('player') == 'BLUE' else '红方'} · {ts}",
                            (row.x + 12, row.y + 28), 10, T.TEXT_FAINT)
            else:
                T.draw_text(s, "空槽位", (row.x + 12, row.y + 28), 10, T.TEXT_FAINT)

            b_save = pygame.Rect(row.right - 152, row.y + 9, 66, 26)
            b_load = pygame.Rect(row.right - 80, row.y + 9, 66, 26)
            for b, label, act, enabled in ((b_save, "保存", "save", True),
                                           (b_load, "读取", "load", info.get("exists", False))):
                hover = b.collidepoint(self.mouse) and enabled
                pygame.draw.rect(s, (30, 70, 92) if hover else
                                 ((22, 32, 44) if enabled else (16, 20, 26)), b, border_radius=3)
                pygame.draw.rect(s, T.CYAN if (hover and enabled) else T.BORDER, b, 1, border_radius=3)
                T.draw_text(s, label, b.center, 11,
                            T.WHITE if hover and enabled else (T.TEXT if enabled else T.TEXT_FAINT),
                            anchor="center")
                if enabled:
                    self.menu_rects.append((b, act, info["slot"]))
            y += row_h

        y += 6
        btns = [("继续作战", "resume"), ("返回想定选择", "to_menu"),
                ("导出战报", "export_md"), ("设置", "settings"), ("退出游戏", "quit")]
        bw = (w - 32 - 16) // 5
        for i, (label, act) in enumerate(btns):
            b = pygame.Rect(r.x + 16 + i * (bw + 8), y, bw, 34)
            hover = b.collidepoint(self.mouse)
            pygame.draw.rect(s, (24, 34, 46) if not hover else (32, 46, 62), b, border_radius=3)
            pygame.draw.rect(s, T.BORDER_HI if hover else T.BORDER, b, 1, border_radius=3)
            T.draw_text(s, label, b.center, 12, T.TEXT if hover else T.TEXT_DIM, anchor="center")
            self.menu_rects.append((b, act, None))

    # -- 顶栏 -------------------------------------------------------------
    def _draw_topbar(self, s: pygame.Surface) -> None:
        st = self.state
        pygame.draw.rect(s, T.PANEL, pygame.Rect(0, 0, self.w, TOP_H))
        T.hline(s, 0, self.w, TOP_H - 1, T.BORDER)

        T.draw_text(s, "方块兵棋", (14, 7), 15, T.TEXT, bold=True)
        T.draw_text(s, st.map.name, (14, 28), 10, T.TEXT_FAINT)
        T.vline(s, 156, 10, TOP_H - 10, T.BORDER)

        # 回合
        x = 172
        T.draw_text(s, "回合", (x, 7), 10, T.TEXT_FAINT)
        T.draw_text(s, f"{st.turn_count:02d}", (x, 20), 19, T.TEXT, bold=True, mono=True)
        T.draw_text(s, f"/{st.turn_limit}", (x + 30, 30), 10, T.TEXT_FAINT, mono=True)
        phase_txt = {"player": "蓝方行动" if self.side is Side.BLUE else "红方行动",
                     "ai": "红方行动" if self.side is Side.BLUE else "蓝方行动",
                     "over": "作战结束"}[st.phase]
        col = T.CYAN if st.phase == "player" else T.AMBER
        T.draw_text(s, phase_txt, (x + 66, 22), 12, col, bold=True)
        if st.phase == "ai":
            T.draw_text(s, "推演中", (x + 66, 8), 9, T.AMBER)
        T.vline(s, 318, 10, TOP_H - 10, T.BORDER)

        # 胜负点
        x = 334
        T.draw_text(s, "胜负点", (x, 7), 10, T.TEXT_FAINT)
        T.draw_text(s, "蓝", (x, 25), 10, T.side_color(Side.BLUE))
        T.draw_text(s, str(st.vp[Side.BLUE]), (x + 16, 20), 15,
                    T.side_color(Side.BLUE), bold=True, mono=True)
        bar_r = pygame.Rect(x + 46, 28, 92, 6)
        pygame.draw.rect(s, T.SIDE_COLORS[Side.RED]["fill"], bar_r, border_radius=3)
        total = max(1, st.vp[Side.BLUE] + st.vp[Side.RED])
        bw = int(bar_r.width * st.vp[Side.BLUE] / total)
        if bw:
            pygame.draw.rect(s, T.SIDE_COLORS[Side.BLUE]["edge"],
                             pygame.Rect(bar_r.x, bar_r.y, bw, bar_r.height), border_radius=3)
        T.draw_text(s, str(st.vp[Side.RED]), (x + 146, 20), 15,
                    T.side_color(Side.RED), bold=True, mono=True)
        T.draw_text(s, "红", (x + 146 + T.text_width(str(st.vp[Side.RED]), 15, mono=True) + 4, 25),
                    10, T.side_color(Side.RED))
        T.vline(s, 512, 10, TOP_H - 10, T.BORDER)

        # 兵力
        x = 528
        T.draw_text(s, "在编兵力", (x, 7), 10, T.TEXT_FAINT)
        T.draw_text(s, str(len(st.units_of(Side.BLUE))), (x, 21), 14,
                    T.side_color(Side.BLUE), bold=True, mono=True)
        T.draw_text(s, ":", (x + 20, 24), 10, T.TEXT_FAINT)
        T.draw_text(s, str(len(st.units_of(Side.RED))), (x + 30, 21), 14,
                    T.side_color(Side.RED), bold=True, mono=True)
        T.vline(s, 600, 10, TOP_H - 10, T.BORDER)

        # 卫星导航
        x = 616
        T.draw_text(s, "卫星导航完整性", (x, 7), 10, T.TEXT_FAINT)
        for i, side in enumerate((Side.BLUE, Side.RED)):
            yy = 24 + i * 12
            v = st.gps[side]
            T.draw_text(s, "蓝" if side is Side.BLUE else "红", (x, yy - 2), 9, T.side_color(side))
            T.bar(s, pygame.Rect(x + 14, yy, 50, 5), v, T.hp_color(v))
            T.draw_text(s, f"{v * 100:.0f}%", (x + 70, yy - 4), 9, T.TEXT_DIM, mono=True)
        T.vline(s, 742, 10, TOP_H - 10, T.BORDER)

        # 战场环境（时段 · 天候）
        x = 758
        env = environment_for(st)
        env_col = {"拂晓": T.AMBER, "白天": T.WHITE, "黄昏": T.AMBER,
                   "夜间": (120, 150, 210)}.get(env.time_of_day.label, T.TEXT)
        wcol = {"晴": T.TEXT_DIM, "雨": (120, 180, 240), "雾": (170, 180, 190),
                "雪": (200, 220, 240)}.get(env.weather.label, T.TEXT_DIM)
        T.draw_text(s, "战场环境", (x, 7), 10, T.TEXT_FAINT)
        T.draw_text(s, env.time_of_day.label, (x, 21), 12, env_col, bold=True)
        T.draw_text(s, env.clock, (x + 34, 22), 10, T.TEXT_DIM, mono=True)
        T.draw_text(s, env.weather.label, (x + 68, 22), 11, wcol)
        T.draw_text(s, env.describe_effects()[:26], (x, 36), 8, T.TEXT_FAINT)
        T.vline(s, 886, 10, TOP_H - 10, T.BORDER)

        # 在飞弹药
        x = 900
        T.draw_text(s, "在飞弹药", (x, 7), 10, T.TEXT_FAINT)
        nmiss = str(len(st.missiles))
        T.draw_text(s, nmiss, (x, 21), 14,
                    T.AMBER if st.missiles else T.TEXT_DIM, bold=True, mono=True)
        if st.missiles:
            T.draw_text(s, "枚", (x + T.text_width(nmiss, 14, mono=True) + 4, 27), 9, T.TEXT_FAINT)

        for b in self.buttons:
            b.update(self.mouse)
            if b is getattr(self, "overlay_btn", None):
                b.label = {"none": "无覆盖", "sensors": "侦察覆盖", "threat": "防空包线"}[
                    self.mapview.overlay]
            elif b.label.startswith("结束回合"):
                b.enabled = st.phase == "player" and not st.over
            b.draw(s)

    # -- 地图浮层 ---------------------------------------------------------
    def _draw_map_overlay_widgets(self, s: pygame.Surface) -> None:
        st = self.state
        mv = self.mapview
        # 缩略图
        mm_size = max(96, min(164, min(mv.rect.width, mv.rect.height) // 5))
        mm = pygame.Rect(mv.rect.right - mm_size - 12, mv.rect.bottom - mm_size - 12,
                         mm_size, mm_size)
        T.panel(s, mm.inflate(10, 10), (10, 14, 20), T.BORDER, radius=4)
        mv.draw_minimap(s, mm)
        T.draw_text(s, "战场态势", (mm.x, mm.y - 15), 10, T.TEXT_FAINT)

        # 事件滚动条
        recent = st.log[-3:]
        if recent:
            h = 14 * len(recent) + 10
            r = pygame.Rect(mv.rect.x + 10, mv.rect.bottom - h - 8, 470, h)
            surf = pygame.Surface(r.size, pygame.SRCALPHA)
            surf.fill((8, 11, 16, 190))
            s.blit(surf, r.topleft)
            pygame.draw.rect(s, (28, 38, 50), r, 1)
            y = r.y + 5
            for e in recent:
                col = LogView.KIND_COLOR.get(e.kind, T.TEXT_DIM)
                T.draw_text(s, f"T{e.turn}", (r.x + 6, y), 10, T.TEXT_FAINT, mono=True)
                T.draw_text(s, e.text[:58], (r.x + 32, y), 10, col)
                y += 14

        # 提示条
        if self.status and self.status_t > 0:
            a = min(1.0, self.status_t)
            w = min(640, T.text_width(self.status, 13) + 28)
            r = pygame.Rect(mv.rect.centerx - w // 2, mv.rect.y + 12, w, 30)
            surf = pygame.Surface(r.size, pygame.SRCALPHA)
            surf.fill((14, 30, 42, int(230 * a)))
            s.blit(surf, r.topleft)
            pygame.draw.rect(s, (60, 130, 160), r, 1, border_radius=3)
            T.draw_text(s, self.status, r.center, 13, (200, 240, 255), anchor="center")

    # -- 侧栏 -------------------------------------------------------------
    def _draw_sidebar(self, s: pygame.Surface) -> None:
        self.action_buttons = []
        x = self.w - SIDE_W
        pygame.draw.rect(s, T.PANEL, pygame.Rect(x, TOP_H, SIDE_W, self.h - TOP_H - BOTTOM_H))
        T.vline(s, x, TOP_H, self.h - BOTTOM_H, T.BORDER)

        # 页签
        tabs = [("unit", "单位详情"), ("ops", "战役态势"), ("log", "作战日志")]
        self._tab_rects = []
        r = pygame.Rect(x + 12, TOP_H + 10, (SIDE_W - 24) // 3 - 4, 28)
        for key, label in tabs:
            active = self.tab == key
            pygame.draw.rect(s, T.PANEL_HI if active else T.PANEL_SUNK, r, border_radius=3)
            if active:
                pygame.draw.rect(s, T.CYAN, pygame.Rect(r.x, r.bottom - 2, r.width, 2))
            T.draw_text(s, label, r.center, 12, T.TEXT if active else T.TEXT_FAINT, anchor="center")
            self._tab_rects.append((pygame.Rect(r), key))
            r.x += r.width + 6

        body = pygame.Rect(x + 12, TOP_H + 48, SIDE_W - 24, self.h - TOP_H - BOTTOM_H - 58)
        if self.tab == "unit":
            self._draw_unit_tab(s, body)
        elif self.tab == "ops":
            self._draw_ops_tab(s, body)
        else:
            if self.logview:
                self.logview.rect = pygame.Rect(body.x, body.y, body.width, body.height)
                self.logview.draw(s, self.state.log)

    def _draw_unit_tab(self, s: pygame.Surface, body: pygame.Rect) -> None:
        st = self.state
        u = st.selected_unit()
        self.weapon_rows = []
        y = body.y

        if u is None:
            T.panel(s, pygame.Rect(body.x, y, body.width, 92), T.PANEL_SUNK, T.BORDER)
            T.draw_text(s, "未选中单位", (body.x + 16, y + 22), 14, T.TEXT_DIM)
            T.draw_text(s, "左键点击己方单位进行选择", (body.x + 16, y + 46), 11, T.TEXT_FAINT)
            T.draw_text(s, "Tab 循环切换可行动单位", (body.x + 16, y + 64), 11, T.TEXT_FAINT)
            y += 104
        else:
            y = self._draw_unit_card(s, body, u, y)
            y = self._draw_weapons(s, body, u, y)
            y = self._draw_actions(s, body, u, y)

        tgt = self.pending_target or (self.hover_unit if self.hover_unit and
                                      self.hover_unit.side is not self.side else None)
        if u and (tgt or self.pending_facility):
            y = self._draw_engage_panel(s, body, u, tgt, y)

        # 剩余空间显示兵器资料
        rest_y = y + 6
        if u and body.bottom - rest_y > 70:
            self._draw_unit_notes(s, body, u, rest_y)

    def _draw_unit_card(self, s, body, u: Unit, y: int) -> int:
        has_ew = bool(u.template.jam_bands or u.template.datalink)
        h = 104 if has_ew else 100
        r = pygame.Rect(body.x, y, body.width, h)
        T.panel(s, r, T.PANEL_HI, T.side_color(u.side, "edge"), radius=4)
        # 图标块
        icon_r = pygame.Rect(r.x + 10, r.y + 12, 44, 44)
        pygame.draw.rect(s, T.SIDE_COLORS[u.side]["fill"], icon_r, border_radius=3)
        pygame.draw.rect(s, T.side_color(u.side), icon_r, 1, border_radius=3)
        self.mapview.draw_class_icon(s, icon_r.inflate(-8, -8), u.template.unit_class,
                                     T.side_color(u.side, "text"))
        T.draw_text(s, u.name, (r.x + 64, r.y + 7), 14, T.TEXT, bold=True)
        T.draw_text(s, f"机动 {u.mp:.1f}/{u.template.mp:.1f}", (r.right - 10, r.y + 9), 10,
                    T.CYAN if u.mp > 0.1 else T.TEXT_FAINT, anchor="topright", mono=True)
        T.draw_text(s, f"发射 {u.shots_left}/{u.template.sortie_shots}",
                    (r.right - 10, r.y + 22), 10,
                    T.AMBER if u.shots_left else T.TEXT_FAINT, anchor="topright", mono=True)
        if u.domain.value == "air":
            ml = {"strike": "打击", "cap": "巡逻", "ferry": "转场"}[u.mission]
            T.draw_text(s, f"剖面 {ml}", (r.right - 10, r.y + 35), 10,
                        T.PURPLE if u.mission != "strike" else T.TEXT_FAINT,
                        anchor="topright")
        T.draw_text(s, f"{u.template.unit_class.label} · {u.domain.label} · {u.side.label}",
                    (r.x + 64, r.y + 32), 11, T.TEXT_FAINT)
        T.bar(s, pygame.Rect(r.x + 64, r.y + 49, r.width - 76, 6), u.hp_ratio,
              T.hp_color(u.hp_ratio))
        T.draw_text(s, f"{u.hp:.0f} / {u.template.hp:.0f}", (r.x + 64, r.y + 58), 10,
                    T.TEXT_DIM, mono=True)
        T.draw_text(s, f"坐标 ({u.x:02d},{u.y:02d})", (r.x + 64, r.y + 76), 10,
                    T.TEXT_FAINT, mono=True)
        state_bits = []
        if u.fired:
            state_bits.append("已射击")
        elif u.mp > 0.1:
            state_bits.append("可行动")
        else:
            state_bits.append("待命")
        if u.entrench > 0:
            state_bits.append("掩体" + ("Ⅰ" if u.entrench == 1 else "Ⅱ"))
        _tile = self.state.map.at(u.x, u.y)
        if _tile is not None and _tile.fort > 0 and _tile.fort_owner is u.side:
            state_bits.append(f"阵地 Lv{_tile.fort}")
        if u.suppressed:
            state_bits.append("被压制")
        if u.domain is Domain.AIR:
            state_bits.append(f"燃油{u.fuel * 100:.0f}%")
        if u.template.datalink:
            state_bits.append("数据链通" if u.datalink_on else "数据链断")
        if u.template.command_radius > 0 and u.side is self.side:
            subs = self.state.subordinates_of(u)
            state_bits.append(f"指挥{len(subs)}")
        # 补给状态：有补给线 / 断粮
        if u.side is self.side and u.domain is not Domain.AIR:
            src = rules.supply_source_for(self.state, u)
            state_bits.append("补给通" if src is not None else "断粮")
        col = T.AMBER if u.fired else T.GREEN
        if u.domain is Domain.AIR and u.fuel < 0.35:
            col = T.RED
        T.draw_text(s, " · ".join(state_bits), (r.right - 10, r.y + 76), 10,
                    col, anchor="topright")
        # 电子战频段与受扰提示
        if u.template.jam_bands or u.template.datalink:
            self._draw_ew_status(s, r, u)
        return y + h + 8

    def _draw_ew_status(self, s, r: pygame.Rect, u: Unit) -> None:
        """显示本单位的电子战频段能力与当前受扰情况。"""
        st = self.state
        y = r.y + 90
        bits: list[str] = []
        if u.template.jam_bands:
            labels = {"radar": "雷达", "comm": "通信", "nav": "导航"}
            bands = "、".join(labels.get(b, b) for b in u.template.jam_bands)
            bits.append(f"可干扰：{bands}（半径 {u.template.jam_radius:.0f} 格）")
        # 敌方干扰对本单位的影响
        radar_jam = rules.jam_strength_at(st, (u.x, u.y), u.side, "radar")
        comm_jam = rules.jam_strength_at(st, (u.x, u.y), u.side, "comm")
        if radar_jam > 0.05:
            bits.append(f"雷达受扰 {radar_jam * 100:.0f}%")
        if comm_jam > 0.05:
            bits.append(f"通信受扰 {comm_jam * 100:.0f}%")
        if not bits:
            return
        col = T.RED if any("受扰" in b for b in bits) else T.PURPLE
        T.draw_text(s, " · ".join(bits), (r.x + 64, y), 9, col)

    def _draw_weapons(self, s, body, u: Unit, y: int) -> int:
        st = self.state
        T.draw_text(s, "武器系统", (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        if not u.template.weapons:
            T.draw_text(s, "无武装 · 支援平台", (body.x + 4, y), 12, T.TEXT_DIM)
            return y + 26
        for w in u.template.weapons:
            ammo = u.ammo.get(w.id, 0)
            sel = st.selected_weapon == w.id
            row = pygame.Rect(body.x, y, body.width, 38)
            if sel:
                pygame.draw.rect(s, (20, 40, 56), row, border_radius=3)
                pygame.draw.rect(s, T.CYAN, pygame.Rect(row.x, row.y, 2, row.height))
            elif row.collidepoint(self.mouse):
                pygame.draw.rect(s, (18, 26, 36), row, border_radius=3)
            col = T.TEXT if ammo > 0 else T.TEXT_FAINT
            T.draw_text(s, w.name, (row.x + 8, row.y + 4), 12, col, bold=sel)
            info = f"{w.kind.label} · {w.range_min:.1f}-{w.range_max:.1f}格 · Pk {w.pk * 100:.0f}%"
            T.draw_text(s, info, (row.x + 8, row.y + 21), 10, T.TEXT_FAINT)
            T.draw_text(s, f"×{ammo}", (row.right - 8, row.y + 10), 13,
                        T.AMBER if ammo else T.TEXT_FAINT, anchor="topright", mono=True)
            self.weapon_rows.append((row, w.id))
            y += 40
        return y + 4

    def _draw_actions(self, s, body, u: Unit, y: int) -> int:
        st = self.state
        has_radar = any(sc.kind.value == "radar" for sc in u.template.sensors)
        acts: list[tuple[str, object, bool]] = []
        if has_radar:
            acts.append(("雷达 开" if u.radar_on else "雷达 关",
                         lambda: st.toggle_radar(u), u.radar_on))
        if u.template.jam_strength > 0:
            acts.append(("干扰 开" if u.jam_on else "干扰 关",
                         lambda: st.toggle_jam(u), u.jam_on))
        if u.template.datalink and not u.is_decoy:
            acts.append(("链 通" if u.datalink_on else "链 断",
                         lambda: st.toggle_datalink(u), u.datalink_on))
        if u.template.can_fortify and not u.is_decoy:
            tile = st.map.at(u.x, u.y)
            lvl = tile.fort if tile and tile.fort_owner is u.side else 0
            acts.append((f"筑工事 {lvl}/3", lambda: self._build_fort(u), lvl >= 3))
        if u.decoy_left > 0:
            for kind, label in (("radar", "雷诱"), ("ir", "红诱"), ("visual", "假目标")):
                acts.append((f"{label}", (lambda k=kind: self.begin_decoy(u, k)), False))
        if u.domain.value == "air" and not u.is_decoy:
            label = {"strike": "剖面:打击", "cap": "剖面:巡逻", "ferry": "剖面:转场"}[u.mission]
            acts.append((label, lambda: self.cycle_mission(u), u.mission == "cap"))
        if any(w.kind.value in ("artillery", "mlrs") for w in u.usable_weapons()):
            acts.append(("烟幕", lambda: self.begin_smoke(u), False))
        if u.can_prepare_fire and not u.is_decoy:
            fm = u.fire_mission
            if fm is not None and fm.active:
                acts.append((f"射界 {fm.x},{fm.y}", lambda: self.set_status(
                    f"射界 ({fm.x},{fm.y})｜"
                    + ("已准备，射击命中与毁伤显著提高" if fm.prepared
                       else f"准备中（剩余 {fm.turns_left} 回合）")), True))
            else:
                acts.append(("预先射击", lambda: self.begin_prepare_fire(u), False))
        if u.template.command_radius > 0 and not u.is_decoy and u.side is self.side:
            subs = st.subordinates_of(u)
            acts.append((f"指挥 {len(subs)}", lambda: self.begin_command(u), False))
        acts.append(("待机", lambda: self._standby(u), False))

        bw = (body.width - 12) // 3
        for i, (label, cb, active) in enumerate(acts):
            r = pygame.Rect(body.x + (i % 3) * (bw + 6), y + (i // 3) * 33, bw, 28)
            kind = "toggle" if active else "ghost"
            b = Button(r, label, cb, kind, size=11)
            if active:
                b.active = True
            b.update(self.mouse)
            b.draw(s)
            # 关键：登记到点击分发列表，否则按钮只画不响应
            self.action_buttons.append(b)
        rows = (len(acts) + 2) // 3
        return y + rows * 33 + 6

    def _build_fort(self, u: Unit) -> None:
        st = self.state
        if st.build_fort(u):
            tile = st.map.at(u.x, u.y)
            self.set_status(f"{u.name} 构筑工事至 Lv{tile.fort}（消耗 1.0 机动点）", 4)
            self._tutorial_note("fortify")
            return
        # 给出具体原因，便于玩家判断
        tile = st.map.at(u.x, u.y)
        if not u.template.can_fortify:
            self.set_status(f"{u.name} 不具备构筑工事能力（需步兵或工兵）", 3)
        elif u.mp < 1.0:
            self.set_status(f"机动点不足（{u.mp:.1f} < 1.0），无法构筑工事", 3)
        elif tile is None or tile.terrain.value == "water":
            self.set_status("水域无法构筑野战工事", 3)
        elif tile.fort >= 3 and tile.fort_owner is u.side:
            self.set_status(f"该处工事已达最高等级（Lv3）", 3)
        else:
            self.set_status("无法在此构筑工事", 3)

    def cycle_mission(self, u: Unit) -> None:
        order = ("strike", "cap", "ferry")
        nxt = order[(order.index(u.mission) + 1) % len(order)]
        if self.state.set_mission(u, nxt):
            label = {"strike": "打击", "cap": "制空巡逻", "ferry": "转场"}[nxt]
            self.set_status(f"{u.name} 转入{label}剖面"
                            + ("（雷达增益 / 油耗 ×1.3）" if nxt == "cap" else
                               "（油耗 ×0.5 · 机动 +25% · 禁止射击）" if nxt == "ferry" else ""), 4)

    def begin_smoke(self, u: Unit) -> None:
        """进入烟幕投放模式：点击射程内的地块即施放烟幕。"""
        u2 = self.state.selected_unit()
        if u2 is None:
            return
        self.smoke_mode = True
        self.smoke_weapon = next((w for w in u2.usable_weapons()
                                  if w.kind.value in ("artillery", "mlrs")), None)
        self.set_status("烟幕：点击射程内目标格（Esc 取消）", 8)

    def begin_prepare_fire(self, u: Unit) -> None:
        """进入火力准备模式：点击射程内且有情报的格子登记射界。"""
        u2 = self.state.selected_unit()
        if u2 is None:
            return
        self.prepare_mode = True
        self.set_status("预先射击：点击射程内有情报的格登记射界（Esc 取消）", 8)

    # -- 指挥指令 ---------------------------------------------------------
    def begin_command(self, u: Unit) -> None:
        """进入指挥模式：选择指令类型后再选目标。"""
        subs = self.state.subordinates_of(u)
        if not subs:
            self.set_status(f"{u.name} 指挥半径内没有下级单位", 3)
            return
        self.command_mode = True
        self.command_issuer = u
        self.set_status("指挥：点击敌方目标下达「集中火力」，"
                        "或按 Esc 取消（更多指令见下方说明）", 8)

    def _place_command(self, pos: tuple[int, int]) -> None:
        """在指定位置下达集中火力指令。"""
        u = self.command_issuer
        self.command_mode = False
        self.command_issuer = None
        if u is None:
            return
        st = self.state
        tgt = st.unit_at(*pos)
        if tgt is None:
            self.set_status("该格没有单位，无法下达集中火力指令", 3)
            return
        ok, msg = st.issue_order(u, "focus_fire", target=tgt)
        if ok:
            self.set_status(msg, 4)
        else:
            self.set_status(f"无法下达指令：{msg}", 4)

    def issue_hold_order(self) -> None:
        """下达固守指令：下级单位不再前出。"""
        u = self.state.selected_unit()
        if u is None:
            return
        ok, msg = self.state.issue_order(u, "hold")
        self.set_status(msg if ok else f"无法下达指令：{msg}", 4)

    def issue_priority_order(self, cls: str) -> None:
        """下达优先目标指令：下级优先打击某类目标。"""
        u = self.state.selected_unit()
        if u is None:
            return
        ok, msg = self.state.issue_order(u, "priority", priority_class=cls)
        self.set_status(msg if ok else f"无法下达指令：{msg}", 4)

    def _place_prepare_fire(self, pos: tuple[int, int]) -> None:
        u = self.state.selected_unit()
        self.prepare_mode = False
        if not u:
            return
        ok, msg = self.state.register_fire_mission(u, pos)
        if ok:
            self.set_status(f"射界已登记（{pos[0]},{pos[1]}）——"
                            f"准备完成后对该区域射击命中与毁伤显著提高", 5)
        else:
            self.set_status(f"无法登记射界：{msg}", 4)

    def _place_smoke(self, pos: tuple[int, int]) -> None:
        u = self.state.selected_unit()
        self.smoke_mode = False
        if not u or not self.smoke_weapon:
            return
        if self.state.fire_smoke(u, pos, self.smoke_weapon):
            self.set_status("烟幕已施放：该格 3 回合内遮蔽光电探测与直瞄火力", 4)
        else:
            self.set_status("无法在该位置施放烟幕（需在火炮射程内且高于最小射程）", 3)

    def begin_decoy(self, u: Unit, kind: str = "radar") -> None:
        """进入诱饵投放模式：下一次点击地图即在该格生成假目标。"""
        if u.decoy_left <= 0:
            return
        self.decoy_mode = True
        self.decoy_kind = kind
        label = rules.DECOY_LABELS.get(kind, kind)
        self.set_status(f"投放{label}：选择位置（Esc 取消）", 8)

    def _place_decoy(self, pos: tuple[int, int]) -> None:
        u = self.state.selected_unit()
        self.decoy_mode = False
        if not u:
            return
        if self.state.deploy_decoy(u, pos, getattr(self, "decoy_kind", "radar")):
            self.set_status("诱饵已投放：敌方态势图上出现一个假目标", 4)
        else:
            self.set_status("该位置无法投放诱饵", 3)

    def _standby(self, u: Unit) -> None:
        """放弃本单位剩余机动点。"""
        u.mp = 0.0
        self.set_status(f"{u.name} 转入待机")
        self.select_unit(None)

    def _salvo_options(self, u: Unit, w) -> list[int]:
        if w is None:
            return [1]
        cap = max(1, min(int(w.max_salvo), u.ammo.get(w.id, 0)))
        return list(range(1, cap + 1))

    def _draw_salvo_row(self, s: pygame.Surface, r: pygame.Rect, u: Unit, w, y: int,
                        base_pk: float) -> int:
        """齐射发数选择：多发独立判定，命中概率取并集，但消耗更多备弹。"""
        self.salvo_rects = []
        opts = self._salvo_options(u, w)
        if len(opts) <= 1:
            T.draw_text(s, f"备弹 {u.ammo.get(w.id, 0)} 发 · 齐射 1 发", (r.x + 10, y), 10, T.TEXT_FAINT)
            return y + 18
        T.draw_text(s, "齐射", (r.x + 10, y + 2), 10, T.TEXT_FAINT)
        x = r.x + 40
        cur = max(opts[0], min(self.state.salvo_setting, opts[-1]))
        self.state.salvo_setting = cur
        for n in opts:
            rr = pygame.Rect(x, y - 2, 34, 18)
            active = n == cur
            hover = rr.collidepoint(self.mouse)
            pygame.draw.rect(s, (30, 62, 84) if active else ((26, 34, 46) if hover else T.PANEL_SUNK),
                             rr, border_radius=3)
            pygame.draw.rect(s, T.CYAN if active else T.BORDER, rr, 1, border_radius=3)
            T.draw_text(s, f"{n}", rr.center, 11, T.WHITE if active else T.TEXT_DIM,
                        bold=active, anchor="center", mono=True)
            self.salvo_rects.append((rr, n))
            x += 38
        comb = 1.0 - (1.0 - base_pk) ** cur
        T.draw_text(s, f"×{cur} 发 · 综合命中 {comb * 100:.0f}%", (x + 4, y + 2), 10,
                    T.AMBER if cur > 1 else T.TEXT_FAINT)
        T.draw_text(s, f"备弹 {u.ammo.get(w.id, 0)}", (r.right - 10, y + 2), 10,
                    T.TEXT_FAINT, anchor="topright", mono=True)
        return y + 22

    def _draw_engage_panel(self, s, body, u: Unit, t: Unit | None, y: int) -> int:
        if self.pending_facility is not None:
            return self._draw_facility_panel(s, body, u, y)
        if t is None:
            return y
        st = self.state
        lvl = st.known_level(self.side, t)
        w = u.weapon_by_id(st.selected_weapon or "")
        prof = rules.shot_profile(st, u, t, w) if w else None
        ok = bool(w and prof and prof.ok)
        threat = rules.counter_threat_profile(st, u, t, w) if (w and t) else None
        # 对手视角区块的高度：标题 + 关键指标 + 若干条明细
        th_h = self._threat_panel_height(threat)
        h = (190 if ok else 92) + th_h
        r = pygame.Rect(body.x, y, body.width, h)
        T.panel(s, r, (26, 20, 24), (110, 60, 66), radius=4)
        T.draw_text(s, "交战目标", (r.x + 10, r.y + 8), 11, T.RED, bold=True)
        T.draw_text(s, t.name, (r.x + 10, r.y + 24), 13, T.TEXT, bold=True)
        T.draw_text(s, f"{t.template.unit_class.label} · {lvl.label} · "
                        f"距离 {prof.distance if prof else rules.dist_units(u, t):.1f} 格",
                    (r.x + 10, r.y + 42), 10, T.TEXT_FAINT)
        if lvl is Detection.TRACKED:
            T.draw_text(s, f"HP {t.hp:.0f}/{t.template.hp:.0f}", (r.right - 10, r.y + 24), 11,
                        T.hp_color(t.hp_ratio), anchor="topright", mono=True)

        if prof is not None:
            yy = r.y + 58
            if prof.ok:
                pk_col = T.GREEN if prof.pk > 0.6 else (T.AMBER if prof.pk > 0.35 else T.RED)
                T.draw_text(s, f"{w.name}", (r.x + 10, yy), 11, T.TEXT)
                T.draw_text(s, f"Pk {prof.pk * 100:.0f}%", (r.right - 10, yy - 1), 13,
                            pk_col, anchor="topright", bold=True, mono=True)
                yy += 16
                parts = [f"{k}×{v:.2f}" for k, v in prof.factors.items() if v < 0.999]
                T.draw_text(s, " ".join(parts)[:52] or "无修正", (r.x + 10, yy), 9, T.TEXT_FAINT)
                yy += 14
                ic = " · 拦截风险 %.0f%%" % (prof.intercept_pk * 100) if prof.intercept_pk > 0.01 else ""
                T.draw_text(s, f"伤害 {prof.damage:.0f}{ic}", (r.x + 10, yy), 10,
                            T.AMBER if prof.intercept_pk > 0.2 else T.TEXT_DIM)
                if prof.cue_source:
                    T.draw_text(s, f"信火一体：{prof.cue_source}", (r.x + 10, yy + 12), 9,
                                T.PURPLE)
                yy += 26
                yy = self._draw_salvo_row(s, r, u, w, yy, prof.pk)
            else:
                T.draw_text(s, prof.reason, (r.x + 10, yy), 11, T.RED)

        if self.pending_target is t and ok:
            btn = pygame.Rect(r.x + 10, r.bottom - 30, r.width - 20, 24)
            hover = btn.collidepoint(self.mouse)
            pygame.draw.rect(s, (128, 40, 44) if hover else (92, 30, 34), btn, border_radius=3)
            pygame.draw.rect(s, T.RED, btn, 1, border_radius=3)
            T.draw_text(s, "确认开火  [Enter]", btn.center, 12, (255, 235, 235),
                        bold=True, anchor="center")
            self._fire_btn = btn

        if threat is not None:
            self._draw_counter_threat(s, r, threat, u, t, w)
        return r.bottom + 8

    # ------------------------------------------------------------------ #
    # 对手视角（为什么打不到）
    # ------------------------------------------------------------------ #
    def _threat_panel_height(self, threat) -> int:
        """对手视角区块高度（与绘制逻辑保持一致）。"""
        if threat is None:
            return 0
        lines = self._threat_lines(threat)
        return 30 + len(lines) * 14 + 8

    def _threat_lines(self, threat) -> list[tuple[str, str, tuple[int, int, int]]]:
        """构造对手视角的明细行：(标签, 内容, 颜色)。"""
        out: list[tuple[str, str, tuple[int, int, int]]] = []
        if threat.intercept_pk > 0.01:
            col = T.RED if threat.intercept_pk > 0.35 else T.AMBER
            out.append(("拦截", f"{threat.intercept_pk * 100:.0f}%　"
                                f"{len(threat.interceptors)} 个防空单元", col))
            for name in threat.interceptors[:2]:
                out.append(("　└", name[:30], T.TEXT_FAINT))
        else:
            out.append(("拦截", "无敌方防空覆盖", T.GREEN))

        # 防空网分工：谁负责哪一段防区
        if threat.adn_roles:
            roles = "、".join(sorted({r for _, r, _ in threat.adn_roles}))
            out.append(("防空网", f"{roles}｜余 {threat.adn_channels} 通道", (150, 190, 235)))

        if threat.reply_units:
            col = T.RED if threat.enemy_fire_control else T.AMBER
            out.append(("反击", f"{len(threat.reply_units)} 个敌方单元可打你"
                                f"（最高 Pk {threat.reply_pk * 100:.0f}%）", col))
            for name in threat.reply_weapons[:1]:
                out.append(("　└", name[:30], T.TEXT_FAINT))
        else:
            out.append(("反击", "无可威胁到你的敌方火力", T.GREEN))

        fc = "已跟踪到我方" if threat.enemy_fire_control else "未跟踪到我方"
        out.append(("敌方火控", fc, T.RED if threat.enemy_fire_control else T.TEXT_DIM))
        for b in threat.blockers[:2]:
            out.append(("阻碍", b[:32], T.AMBER))
        return out

    def _draw_counter_threat(self, s, panel: pygame.Rect, threat, u: Unit, t: Unit,
                             w) -> None:
        """绘制「对手视角」区块：解释这一发为什么打不到。"""
        th_h = self._threat_panel_height(threat)
        r = pygame.Rect(panel.x, panel.bottom - th_h, panel.width, th_h)
        pygame.draw.rect(s, (16, 22, 32), r, border_radius=3)
        pygame.draw.rect(s, (60, 84, 110), r, 1, border_radius=3)

        T.draw_text(s, "对手视角", (r.x + 10, r.y + 6), 10, (140, 180, 220), bold=True)
        T.draw_text(s, "这发子弹会遇到什么", (r.right - 10, r.y + 7), 9, T.TEXT_FAINT,
                    anchor="topright")

        yy = r.y + 22
        for label, text, col in self._threat_lines(threat):
            T.draw_text(s, label, (r.x + 10, yy), 9, T.TEXT_FAINT)
            T.draw_text(s, text, (r.x + 54, yy), 9, col)
            yy += 14

        # 教学：玩家查看了对手视角
        if self.tutorial is not None and not self.tutorial.finished:
            self._tutorial_note("counter_viewed")

    def _draw_facility_panel(self, s, body, u: Unit, y: int) -> int:
        st = self.state
        pos = self.pending_facility
        tile = st.map.at(*pos) if pos else None
        w = u.weapon_by_id(st.selected_weapon or "")
        prof = rules.facility_strike_profile(st, u, tile, w) if (tile and w) else None
        ok = bool(prof and prof.ok)
        h = 176 if ok else 92
        r = pygame.Rect(body.x, y, body.width, h)
        T.panel(s, r, (28, 24, 20), (130, 96, 60), radius=4)
        T.draw_text(s, "设施打击", (r.x + 10, r.y + 8), 11, T.AMBER, bold=True)
        if tile is None:
            return r.bottom + 8
        T.draw_text(s, f"{tile.facility.label} ({tile.x},{tile.y})", (r.x + 10, r.y + 24), 13,
                    T.TEXT, bold=True)
        T.draw_text(s, f"结构完整度 {tile.facility_hp_ratio * 100:.0f}% · "
                        f"{'运作中' if tile.operational else '已失能'}",
                    (r.x + 10, r.y + 42), 10,
                    T.hp_color(tile.facility_hp_ratio))
        T.bar(s, pygame.Rect(r.x + 10, r.y + 56, r.width - 20, 5),
              tile.facility_hp_ratio, T.hp_color(tile.facility_hp_ratio))

        if prof is not None and prof.ok:
            yy = r.y + 68
            pk_col = T.GREEN if prof.pk > 0.6 else (T.AMBER if prof.pk > 0.35 else T.RED)
            T.draw_text(s, f"{w.name}", (r.x + 10, yy), 11, T.TEXT)
            T.draw_text(s, f"Pk {prof.pk * 100:.0f}%", (r.right - 10, yy - 1), 13,
                        pk_col, anchor="topright", bold=True, mono=True)
            yy += 16
            parts = [f"{k}×{v:.2f}" for k, v in prof.factors.items() if v < 0.999]
            T.draw_text(s, " ".join(parts)[:52] or "无修正", (r.x + 10, yy), 9, T.TEXT_FAINT)
            yy += 14
            ic = f" · 拦截风险 {prof.intercept_pk * 100:.0f}%" if prof.intercept_pk > 0.01 else ""
            T.draw_text(s, f"结构伤害 {prof.damage:.0f}{ic}", (r.x + 10, yy), 10,
                        T.AMBER if prof.intercept_pk > 0.2 else T.TEXT_DIM)
            yy += 16
            self._draw_salvo_row(s, r, u, w, yy, prof.pk)
        elif prof is not None:
            T.draw_text(s, prof.reason, (r.x + 10, r.y + 68), 11, T.RED)

        if ok:
            btn = pygame.Rect(r.x + 10, r.bottom - 30, r.width - 20, 24)
            hover = btn.collidepoint(self.mouse)
            pygame.draw.rect(s, (130, 96, 40) if hover else (94, 68, 28), btn, border_radius=3)
            pygame.draw.rect(s, T.AMBER, btn, 1, border_radius=3)
            T.draw_text(s, "确认打击  [Enter]", btn.center, 12, (255, 245, 225),
                        bold=True, anchor="center")
            self._fire_btn = btn
        return r.bottom + 8

    def _draw_unit_notes(self, s, body, u: Unit, y: int) -> None:
        sen = "、".join(f"{sc.kind.label} {sc.range_max:.1f}" for sc in u.template.sensors) or "无"
        rows = [("传感器", sen)]
        if u.template.stealth > 0:
            rows.append(("隐身系数", f"{u.template.stealth * 100:.0f}%"))
        if u.template.armor > 0:
            rows.append(("装甲防护", f"{u.template.armor * 100:.0f}%"))
        if u.template.interceptors > 0:
            rows.append(("拦截通道", f"{u.template.interceptors} 枚/回合 · {u.template.intercept_pk * 100:.0f}%"))
        if u.template.jam_radius > 0:
            rows.append(("干扰范围", f"{u.template.jam_radius:.0f} 格 · 强度 {u.template.jam_strength * 100:.0f}%"))
        if u.template.amphibious:
            rows.append(("部署能力", "两栖投送"))
        notes = T.wrap_text(u.template.notes, 10, body.width - 20)[:3] if u.template.notes else []
        h = 26 + len(rows) * 15 + (len(notes) * 13 + 6 if notes else 0) + 10
        h = min(h, body.bottom - y)
        if h < 40:
            return
        r = pygame.Rect(body.x, y, body.width, h)
        T.panel(s, r, T.PANEL_SUNK, T.BORDER, radius=4)
        T.draw_text(s, "平台资料", (r.x + 10, r.y + 8), 10, T.TEXT_FAINT, bold=True)
        yy = r.y + 24
        for k, v in rows:
            T.draw_text(s, k, (r.x + 10, yy), 10, T.TEXT_FAINT)
            T.draw_text(s, v, (r.x + 74, yy), 10, T.TEXT_DIM)
            yy += 15
        if notes:
            yy += 3
            for line in notes:
                T.draw_text(s, line, (r.x + 10, yy), 10, (120, 150, 150))
                yy += 13

    # -- 态势页 -----------------------------------------------------------
    def _draw_ops_tab(self, s: pygame.Surface, body: pygame.Rect) -> None:
        st = self.state
        y = body.y
        # 目标点
        T.draw_text(s, "争夺目标", (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        for t in st.map.objectives:
            r = pygame.Rect(body.x, y, body.width, 26)
            tile = st.map.at(t.x, t.y)
            own = tile.owner if tile else Side.NEUTRAL
            col = T.side_color(own) if own is not Side.NEUTRAL else T.AMBER
            pygame.draw.rect(s, col, pygame.Rect(r.x, r.y + 4, 3, 18))
            T.draw_text(s, f"{t.name}", (r.x + 10, r.y + 5), 12, T.TEXT)
            T.draw_text(s, f"({t.x},{t.y})", (r.x + 10 + T.text_width(t.name, 12) + 6, r.y + 7),
                        9, T.TEXT_FAINT, mono=True)
            owner_txt = own.label if own is not Side.NEUTRAL else "争夺中"
            T.draw_text(s, f"{owner_txt} +{t.value}/回合", (r.right, r.y + 5), 10,
                        col, anchor="topright")
            y += 27
        y += 8

        # 兵力对比
        T.draw_text(s, "兵力构成", (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        doms = [Domain.LAND, Domain.AIR, Domain.SEA, Domain.SUB, Domain.INFRA, Domain.SPACE]
        for d in doms:
            b = len([u for u in st.units_of(Side.BLUE) if u.domain is d])
            r_ = len([u for u in st.units_of(Side.RED) if u.domain is d])
            if b == 0 and r_ == 0:
                continue
            T.draw_text(s, d.label, (body.x, y), 11, T.TEXT_DIM)
            T.draw_text(s, str(b), (body.x + 120, y), 12, T.side_color(Side.BLUE), mono=True)
            T.draw_text(s, str(r_), (body.x + 160, y), 12, T.side_color(Side.RED), mono=True)
            T.bar(s, pygame.Rect(body.x + 190, y + 4, 120, 5), b / max(1, b + r_),
                  T.side_color(Side.BLUE))
            y += 18
        y += 10

        # 在飞弹药
        if st.missiles:
            T.draw_text(s, "在飞弹药", (body.x, y), 11, T.TEXT_FAINT, bold=True)
            y += 18
            for m in st.missiles[:6]:
                col = T.side_color(m.attacker_side)
                T.draw_text(s, f"{m.label}", (body.x, y), 11, col)
                T.draw_text(s, f"{m.eta} 回合", (body.right, y), 11, T.AMBER,
                            anchor="topright", mono=True)
                y += 16
            y += 8

        # 卫星与电子战
        self._sat_rects = []
        T.draw_text(s, "天基资产与电子战", (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        for side in (Side.BLUE, Side.RED):
            sats = st.satellites(side)
            alive = [u for u in sats if u.alive]
            ew = [u for u in st.units_of(side) if u.template.jam_strength > 0 and u.jam_on]
            asat = st.asat_capable(side)
            T.draw_text(s, side.label, (body.x, y), 11, T.side_color(side), bold=True)
            T.draw_text(s, f"卫导 {st.gps[side] * 100:.0f}% · 干扰机 {len(ew)} · "
                            f"反卫星 {len(asat)}", (body.x + 44, y), 10, T.TEXT_DIM)
            y += 16
            if not alive:
                T.draw_text(s, "  无在轨侦察资产：过顶侦察中断", (body.x + 8, y), 9, T.RED)
                y += 14
            for sat in alive:
                row = pygame.Rect(body.x, y - 1, body.width, 15)
                if sat.side is not st.player_side:
                    hov = row.collidepoint(self.mouse)
                    if hov:
                        pygame.draw.rect(s, (36, 24, 26), row)
                    T.draw_text(s, f"  {sat.name}", (body.x + 8, y), 10,
                                T.side_color(sat.side) if hov else T.TEXT_DIM)
                    self._sat_rects.append((row, sat))
                else:
                    T.draw_text(s, f"  {sat.name}", (body.x + 8, y), 10, T.TEXT_DIM)
                T.bar(s, pygame.Rect(body.x + 150, y + 3, 90, 5), sat.hp_ratio,
                      T.hp_color(sat.hp_ratio))
                T.draw_text(s, f"{sat.hp:.0f}", (body.x + 246, y - 1), 9, T.TEXT_FAINT, mono=True)
                if st.satellite_overhead(sat):
                    T.draw_text(s, "过顶中", (body.x + 280, y), 9, T.GREEN, bold=True)
                else:
                    T.draw_text(s, "轨道外", (body.x + 280, y), 9, T.TEXT_FAINT)
                y += 15
        T.draw_text(s, "点击敌方卫星 → 反卫星打击｜过顶中 = 星下点 9 格区域侦察", (body.x + 8, y), 9, T.CYAN)
        y += 16

        # 胜利条件
        y += 6
        if y < body.bottom - 60:
            T.draw_text(s, "胜利条件", (body.x, y), 11, T.TEXT_FAINT, bold=True)
            y += 16
            for line in T.wrap_text(
                    f"{st.turn_limit} 回合内累计胜负点高者获胜。目标点由可占领单位控制，"
                    f"每回合结算一次；击毁敌方单位不直接得分，但可削弱其目标点控制能力。",
                    10, body.width)[:4]:
                T.draw_text(s, line, (body.x, y), 10, T.TEXT_DIM)
                y += 14

        # 战术提示
        tips = self._tactical_tips()
        if tips and y < body.bottom - 50:
            y += 8
            T.draw_text(s, "态势研判", (body.x, y), 11, T.CYAN, bold=True)
            y += 18
            for tip in tips:
                if y > body.bottom - 16:
                    break
                for line in T.wrap_text("· " + tip, 10, body.width)[:2]:
                    T.draw_text(s, line, (body.x, y), 10, (150, 175, 190))
                    y += 14
                y += 2

    def _tactical_tips(self) -> list[str]:
        st = self.state
        tips: list[str] = []
        foe = self.side.foe
        sams = [u for u in st.enemies_of(self.side)
                if st.known_level(self.side, u).level >= Detection.CLASSIFIED.level
                and u.max_intercept_range() > 0]
        if sams:
            names = "、".join(u.template.short for u in sams[:2])
            tips.append(f"侦测到敌方防空系统（{names}），航空兵进入其包线前应先用反辐射导弹或"
                        f"防区外弹药压制。")
        if st.gps[self.side] < 0.85:
            tips.append(f"己方卫星导航完整性降至 {st.gps[self.side] * 100:.0f}%，"
                        f"卫星/惯导制导弹药命中率下降，可考虑改用光学或激光制导武器。")
        ew = [u for u in st.enemies_of(self.side)
              if st.known_level(self.side, u).level >= Detection.CLASSIFIED.level
              and u.template.jam_strength > 0]
        if ew:
            tips.append("敌方电子战力量活跃，雷达探测距离与雷达制导武器效能被削弱，"
                        "必要时切换被动侦察手段。")
        if st.missiles:
            mine = [m for m in st.missiles if m.attacker_side is self.side]
            if mine:
                tips.append(f"我方有 {len(mine)} 枚弹药在飞，抵达前若目标机动或情报中断，"
                            f"命中概率将显著下降。")
        if not tips:
            tips.append("保持传感器网络覆盖：目标只有在被「跟踪」时才能被精确打击，"
                        "雷达开机与关机的取舍决定生存与发现。")
        return tips[:3]

    # -- 底栏 -------------------------------------------------------------
    def _draw_bottom(self, s: pygame.Surface) -> None:
        y = self.h - BOTTOM_H
        pygame.draw.rect(s, T.PANEL, pygame.Rect(0, y, self.w, BOTTOM_H))
        T.hline(s, 0, self.w, y, T.BORDER)
        hint = ("左键选择/机动 · 右键机动 · 滚轮缩放 · 中键平移 · 空格结束回合 · Tab 切换单位 · "
                "1-9 选择武器 · R 雷达 · J 干扰 · E 覆盖层 · F1 帮助")
        T.draw_text(s, hint, (12, y + 6), 11, T.TEXT_FAINT)

        if self.hover_tile:
            t = self.state.map.at(*self.hover_tile)
            if t:
                parts = [f"({t.x},{t.y})", t.terrain.label, f"海拔 {t.elevation}m"]
                if t.facility is not Facility.NONE:
                    parts.append(t.facility.label)
                if t.objective:
                    parts.append(f"◆ {t.objective_name}")
                if t.smoke:
                    parts.append("烟幕")
                T.draw_text(s, " · ".join(parts), (self.w - SIDE_W - 12, y + 6), 11,
                            T.TEXT_DIM, anchor="topright")

    # -- 帮助 -------------------------------------------------------------
    HELP_LEFT = [
        ("h", "侦察—打击链"),
        ("p", "目标须被传感器探测到「跟踪」级才能装订诸元；火炮/火箭炮可对「识别」级实施压制。"),
        ("h", "信火一体"),
        ("p", "友邻雷达或预警机提供跟踪诸元时，本平台即使未自身锁定也可发射；"
              "有数据链无惩罚（A 射 B 导），老式装备命中率 ×0.78。"),
        ("h", "电子战与诱饵"),
        ("p", "干扰压制雷达链路并削弱卫星导航；反辐射导弹只打辐射源。"
              "电子战单位可投放诱饵，在敌方态势图上生成假目标，攻击诱饵将浪费整次射击。"),
        ("h", "步兵与工事"),
        ("p", "步兵对直瞄火力减伤、对面积火力增伤，城区防御尤强。驻守不动可构筑工事，"
              "获得额外防护并降低被发现概率；一旦机动工事尽失。"),
        ("h", "航空兵"),
        ("p", "战机被命中即坠毁；按挂架可在单回合内多次发射（最多 3 次）。"
              "每回合耗油，须返回完好机场或航母补给，否则燃油耗尽坠毁。"
              "任务剖面：巡逻（雷达/空战增益、油耗 ×1.3）、转场（油耗 ×0.5、"
              "机动 +25%、禁止射击）。"),
        ("h", "反卫星与设施打击"),
        ("p", "「战役态势」页点击敌方卫星可实施反卫星打击；点击敌方机场/港口可实施结构打击，"
              "结构完整度低于 25% 的设施将失能。"),
    ]
    HELP_RIGHT = [
        ("h", "操作"),
        ("p", "左键：选择己方单位 / 点选目标 / 机动"),
        ("p", "右键：下达机动指令"),
        ("p", "滚轮：缩放　　中键拖动：平移　　方向键：平移"),
        ("p", "空格：结束回合（AI 阶段可快进）"),
        ("p", "Tab：切换单位　　1-9：选择武器"),
        ("p", "R：雷达　J：干扰　E：覆盖层　Enter：确认"),
        ("p", "F5 / F9：快速保存 / 读取　　F10：系统菜单"),
        ("h", "齐射"),
        ("p", "交战面板可选 1-3 发齐射，多发独立判定、命中概率取并集，但成倍消耗备弹。"),
        ("h", "存档"),
        ("p", "系统菜单提供 5 个槽位，记录单位状态、在飞弹药、设施损毁与目标点归属。"),
        ("h", "数据编辑"),
        ("p", "主菜单「地图 / 武器编辑器」可编辑地形、尺寸、设施、部署、兵种属性、"
              "武器备弹与预设挂载；部署页顶部可按预设编队（中·空 / 美·海 …）筛选。"),
    ]

    def _layout_help_column(self, items, width: int) -> list[tuple[str, int, tuple, int]]:
        out: list[tuple[str, int, tuple, int]] = []
        for kind, text in items:
            if kind == "h":
                out.append((text, 12, T.CYAN, 22))
            else:
                for line in T.wrap_text(text, 11, width):
                    out.append((line, 11, T.TEXT_DIM, 17))
                out.append(("", 10, T.TEXT_DIM, 2))
        return out

    def _draw_help(self, s: pygame.Surface) -> None:
        w = min(900, self.w - 80)
        colw = (w - 76) // 2
        left = self._layout_help_column(self.HELP_LEFT, colw)
        right = self._layout_help_column(self.HELP_RIGHT, colw)
        content_h = max(sum(i[3] for i in left), sum(i[3] for i in right))
        h = min(self.h - 60, content_h + 104)
        r = pygame.Rect((self.w - w) // 2, (self.h - h) // 2, w, h)
        surf = pygame.Surface(r.size, pygame.SRCALPHA)
        surf.fill((8, 11, 16, 248))
        s.blit(surf, r.topleft)
        pygame.draw.rect(s, T.BORDER_HI, r, 1, border_radius=4)
        T.draw_text(s, "作战规则与操作", (r.x + 24, r.y + 16), 17, T.TEXT, bold=True)
        T.hline(s, r.x + 24, r.right - 24, r.y + 44, T.BORDER)

        for items, cx in ((left, r.x + 24), (right, r.x + 52 + colw)):
            yy = r.y + 58
            for text, size, color, dy in items:
                if text:
                    T.draw_text(s, text, (cx, yy), size, color, bold=size >= 12)
                yy += dy
        T.draw_text(s, "按 F1 或 Esc 关闭", (r.centerx, r.bottom - 22), 11, T.TEXT_FAINT,
                    anchor="center")

    # -- 结算 -------------------------------------------------------------
    def _draw_over(self, s: pygame.Surface) -> None:
        st = self.state
        surf = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        surf.fill((5, 8, 12, 200))
        s.blit(surf, (0, 0))
        r = pygame.Rect(self.w // 2 - 260, self.h // 2 - 190, 520, 380)
        T.panel(s, r, (14, 19, 27), T.BORDER_HI, radius=6)
        win = st.winner is self.side
        if st.winner is None:
            title, col = "战 平", T.AMBER
        elif win:
            title, col = "胜 利", T.GREEN
        else:
            title, col = "失 败", T.RED
        T.draw_text(s, title, (r.centerx, r.y + 34), 38, col, bold=True, anchor="center")
        for line in T.wrap_text(st.win_reason, 13, r.width - 60)[:3]:
            T.draw_text(s, line, (r.centerx, r.y + 92), 13, T.TEXT, anchor="center")

        y = r.y + 168
        stats = [
            ("回合", f"{min(st.turn_count, st.turn_limit)} / {st.turn_limit}"),
            ("胜负点", f"蓝 {st.vp[Side.BLUE]}  :  {st.vp[Side.RED]} 红"),
            ("战损", f"蓝 {st.casualties[Side.BLUE]}  :  {st.casualties[Side.RED]} 红"),
            ("剩余兵力", f"蓝 {len(st.units_of(Side.BLUE))}  :  {len(st.units_of(Side.RED))} 红"),
        ]
        for k, v in stats:
            T.draw_text(s, k, (r.x + 120, y), 13, T.TEXT_FAINT)
            T.draw_text(s, v, (r.right - 120, y), 13, T.TEXT, anchor="topright", mono=True)
            y += 26
        btn = pygame.Rect(self.w // 2 - 130, self.h // 2 + 108, 260, 44)
        hover = btn.collidepoint(self.mouse)
        pygame.draw.rect(s, (30, 96, 120) if hover else (22, 70, 90), btn, border_radius=4)
        pygame.draw.rect(s, T.CYAN, btn, 1, border_radius=4)
        T.draw_text(s, "返回想定选择  [Enter]", btn.center, 14, T.WHITE, bold=True, anchor="center")

    # ================================================================== #
    def run(self) -> None:
        while self.running:
            dt = self.clock.tick(60) / 1000.0
            for e in pygame.event.get():
                self.handle_event(e)
            self.update(dt)
            self.draw()
        pygame.quit()


def main() -> int:
    try:
        App().run()
    except Exception:
        traceback.print_exc()
        pygame.quit()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

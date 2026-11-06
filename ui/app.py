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

from core.i18n import tr
from core import mods as _mods
from core import rules
from core import icons
from core.commands import (CMD_DATALINK, CMD_DECOY, CMD_END_TURN, CMD_FIRE,
                           CMD_FIRE_FACILITY, CMD_FORTIFY, CMD_HOLD, CMD_JAM,
                           CMD_MOVE, CMD_ORDER, CMD_PREPARE, CMD_PROFILE,
                           CMD_RADAR, CMD_SMOKE, CommandLog)
from core import pkg as _pkg
from core import i18n
from core.campaign import (CampaignDef, CampaignError, CampaignProgress, apply_carryover,
                           apply_carryover_policy, collect_survivors, list_campaigns,
                           load_campaign,
                           load_progress, resolve_map_path, save_progress)
from core.config import ConfigError, UserConfig
from core.replay import ReplayFile
from core.enums import Detection, Domain, Facility, Side, TimeOfDay, Weather
from core.engine import GameState
from core.env import environment_for
from core.loader import LoadError, list_maps, load_scenario
from core.model import Unit
from core.save import SaveError, list_slots, load_game, save_game, slot_path
from core.tutorial import Tutorial, build_tutorial

from . import theme as T
from .audio import AudioManager
from .briefing import (BriefingData, DebriefData, build_briefing, build_debrief,
                       draw_briefing, draw_debrief)
from .editor import Editor
from .importdlg import ImportDialog
from .replaydlg import ReplayDialog
from .replayplayer import ReplayPlayer
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

#: 相克通道（core.counter 的中文键）→ 界面词条
_COUNTER_TR = {
    "破甲": "ui.counter.armor",
    "爆破": "ui.counter.blast",
    "机动": "ui.counter.mobility",
    "高度": "ui.counter.altitude",
    "频段": "ui.counter.band",
    "交叉火力": "ui.counter.crossfire",
}

# 教学想定在主菜单中的固定入口（不依赖想定文件名）
TUTORIAL_MAP_ID = "training_ground"


def _meta_of(raw: dict) -> dict:
    """从想定 JSON 中提取展示用元数据（不加载完整想定，菜单扫描更轻量）。"""
    m = raw.get("meta")
    if not isinstance(m, dict):
        m = {}
    tags = m.get("tags", raw.get("tags", []))
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    units = m.get("recommended_units", raw.get("recommended_units", []))
    if isinstance(units, str):
        units = [u.strip() for u in units.split(",") if u.strip()]
    return {
        "author": str(m.get("author", raw.get("author", ""))),
        "summary": str(m.get("summary", "")),
        "difficulty": str(m.get("difficulty", raw.get("difficulty", ""))),
        "tags": [str(t) for t in tags],
        "recommended_units": [str(u) for u in units],
        "recommended_side": str(m.get("recommended_side", raw.get("recommended_side", ""))),
        "version": str(m.get("version", raw.get("version", ""))),
        "campaign": str(m.get("campaign", raw.get("campaign", ""))),
        "campaign_step": int(m.get("campaign_step", raw.get("campaign_step", 0)) or 0),
    }


# 难度标签色：跟随主题解析，不要硬编码——
# 硬编码的深色主题值在亮色主题下过浅，标签几乎看不出来。
DIFFICULTY_ACCENTS = {
    "easy": "green", "normal": "cyan", "hard": "amber", "extreme": "red",
}
DIFFICULTY_KEYS = {
    "easy": "ui.diff.easy", "normal": "ui.diff.normal",
    "hard": "ui.diff.hard", "extreme": "ui.diff.extreme",
}


def difficulty_label(code: str) -> str:
    """难度代码 → 本地化显示名；未知代码原样返回。"""
    key = DIFFICULTY_KEYS.get(code)
    return tr(key) if key else code


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
        self.help_scroll = 0          # 帮助面板滚动位置（内容高于窗口时才可见）
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
        # 键盘地图光标：方向键移动，回车在光标处执行与鼠标左键等价的动作。
        # 仅在使用键盘导航后才显示，鼠标玩家不会看到它。
        self.cursor: tuple[int, int] = (0, 0)
        self.cursor_visible: bool = False
        self.path_preview: list | None = None
        self.reachable: dict = {}
        self.buttons: list[Button] = []
        self.weapon_rows: list[tuple[pygame.Rect, str]] = []
        self.action_buttons: list[Button] = []   # 侧栏行动按钮（每帧重建，点击时查询）
        self.salvo_rects: list[tuple[pygame.Rect, int]] = []
        self._sat_rects: list[tuple[pygame.Rect, object]] = []
        self.menu_rects: list[tuple[pygame.Rect, str, object]] = []
        # v3.0 无障碍：系统菜单的键盘焦点（此前 menu_rects 只能鼠标点）
        self.sys_menu_index = 0
        self.logview: LogView | None = None
        self.scenarios: list[dict] = []
        self.menu_index = 0
        self.menu_scroll = 0          # 想定列表的纵向滚动偏移（行）
        self.menu_side = Side.BLUE
        self._menu_import_btn = pygame.Rect(0, 0, 0, 0)
        self._menu_replay_btn = pygame.Rect(0, 0, 0, 0)
        self._import_dialog: ImportDialog | None = None
        self._replay_dialog: ReplayDialog | None = None
        # 回放播放器（非 None 时处于回放模式）
        self.replay_player: ReplayPlayer | None = None
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
        # -- v2.1：单位图标包（用户目录优先，空 = 内置矢量图形）--
        icons.set_current_pack(str(self.cfg.pref("icon_pack", "") or ""))

        # -- v2.0：模组加载（用户目录优先，安装目录其次）--
        self.mods = _mods.load_all()
        # -- v3.0：模组词条注入（必须在本地化初始化之前）--
        try:
            _n = _mods.apply_localization(self.mods)
            if _n:
                self.cfg.set_pref("last_mods", f"已注入 {_n} 条模组词条")
        except Exception as exc:
            self.cfg.set_pref("last_mods", f"模组词条注入失败：{exc}")
        for _m in self.mods.mods:
            if _m.ok:
                self.cfg.set_pref("last_mods", self.mods.summary())
            else:
                self.cfg.set_pref("last_mods", f"{_m.mod_id} 加载失败：{_m.error}")

        # -- v1.4：主题 / 音效 / 本地化 --
        # 语言包：先读安装目录，再读用户目录（用户目录优先，可覆盖）
        i18n.load_language_dir(_paths.lang_dir())
        i18n.load_language_dir(_paths.user_lang_dir())
        self._apply_theme(self.cfg.theme)
        self._apply_language(self.cfg.language)
        self.audio = AudioManager(enabled=self.cfg.sound_enabled,
                                  volume=self.cfg.volume)

        # -- v1.1：教学引导 --
        self.tutorial: Tutorial | None = None
        self.tutorial_visible = False
        self._tutorial_notice = 0.0
        self._tutorial_skip_btn = pygame.Rect(0, 0, 0, 0)

        # -- v1.1：悬停信息卡与对手视角 --
        self.inspect = InspectCard()
        self.hover_card = False

        # -- v1.3：战役模式 --
        self.campaign: CampaignDef | None = None
        self.campaign_progress: CampaignProgress | None = None
        self.campaign_pending: bool = False    # 通关后等待玩家确认进入下一关
        self.campaigns: list[CampaignDef] = []
        self.show_campaign_menu: bool = False

        # -- v1.4：简报与战斗回顾 --
        self.briefing: BriefingData | None = None
        self.show_briefing: bool = False
        self.debrief: DebriefData | None = None
        self._brief_btn = pygame.Rect(0, 0, 0, 0)
        self._debrief_btn = pygame.Rect(0, 0, 0, 0)

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
        """扫描可用想定。

        同时扫描安装目录与用户目录（导入的社区想定），
        同名文件以**用户目录**为准——用户可能改过它。
        """
        self.scenarios = []
        seen: set[str] = set()
        for d in _paths.all_maps_dirs():
            if not os.path.isdir(d):
                continue
            for path in list_maps(d):
                base = os.path.basename(path)
                if base in seen:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                except Exception as exc:
                    self.error = f"{base}: {exc}"
                    continue
                seen.add(base)
                self.scenarios.append({
                    "path": path,
                    "id": raw.get("id", base),
                    "name": raw.get("name", base),
                    "desc": raw.get("description", ""),
                    "size": raw.get("size", [0, 0]),
                    "turns": raw.get("turn_limit", 0),
                    "tile_km": raw.get("tile_km", 12),
                    "blue": len(raw.get("deployments", {}).get("BLUE", [])),
                    "red": len(raw.get("deployments", {}).get("RED", [])),
                    "tutorial": bool(raw.get("tutorial")),
                    "meta": _meta_of(raw),
                    "events": len(raw.get("events", []) or []),
                })
        if not self.scenarios:
            self.error = tr("ui.menu.no_maps_fmt") % (MAP_DIR,)
        # 教学想定排在最前，便于新玩家第一眼看到
        self.scenarios.sort(key=lambda s: (not s.get("tutorial"), s["name"]))
        # 选中项可能因新增/删除想定而越界
        self.menu_index = min(self.menu_index, max(0, len(self.scenarios) - 1))

    def _layout_menu(self) -> None:
        self._menu_buttons = []

    # -- 菜单按钮几何 -----------------------------------------------------
    def _menu_button_y(self) -> int:
        return 420 + 122

    # -- 主菜单想定列表：网格与滚动 --------------------------------------
    def _menu_cards_area(self) -> pygame.Rect:
        """想定卡片区的可视矩形（左侧）。

        右侧为机制速览 + 阵营选择，预留 320px——窄窗口下右侧面板
        可能显示不下，所以这里只保证卡片区不挤压它。
        """
        right_reserved = 320          # 右侧机制速览 + 阵营选择的宽度
        area_w = max(320, self.w - 60 - right_reserved - 20)
        return pygame.Rect(60, 200, area_w, 318)

    def _menu_grid(self) -> tuple[int, int, int, int]:
        """返回 (列数, 卡片宽, 卡片高, 行高)。"""
        area = self._menu_cards_area()
        card_w = 340
        gap = 16
        cols = max(1, area.width // (card_w + gap))
        card_w = min(card_w, (area.width - (cols - 1) * gap) // cols)
        card_h = 148
        return cols, card_w, card_h, card_h + 10

    def _menu_card_rect(self, index: int) -> pygame.Rect | None:
        """第 index 张想定卡片的屏幕矩形；不在可视行内则返回 None。

        绘制与点击**共用**这套几何计算——否则两处一旦不同步，
        就会出现「点得到却选不中」或「悬停高亮错位」。
        """
        cols, card_w, card_h, row_h = self._menu_grid()
        area = self._menu_cards_area()
        row, col = divmod(index, cols)
        vis_rows = max(1, area.height // row_h)
        if row < self.menu_scroll or row >= self.menu_scroll + vis_rows:
            return None
        return pygame.Rect(area.x + col * (card_w + 16),
                           area.y + (row - self.menu_scroll) * row_h, card_w, card_h)

    def _menu_card_at(self, pos: tuple[int, int]) -> int:
        """鼠标位置落在哪张想定卡片上；没有则返回 -1。"""
        area = self._menu_cards_area()
        if not area.collidepoint(pos):
            return -1
        for i in range(len(self.scenarios)):
            r = self._menu_card_rect(i)
            if r is not None and r.collidepoint(pos):
                return i
        return -1

    #: 阵营卡片几何（绘制与命中共用，避免「点得到却选不中」）
    SIDE_CARD_Y = 432
    SIDE_CARD_STEP = 50
    SIDE_CARD_H = 48

    def _menu_side_rects(self) -> list[tuple[Side, pygame.Rect]]:
        """两个阵营卡片的矩形。宽度依赖右侧面板，窄窗口下保底 280。"""
        area = self._menu_cards_area()
        px = area.right + 18
        pw = self.w - px - 48
        side_w = min(pw if pw >= 220 else 280, 320)
        return [(side, pygame.Rect(px, self.SIDE_CARD_Y + j * self.SIDE_CARD_STEP,
                                   side_w, self.SIDE_CARD_H))
                for j, side in enumerate((Side.BLUE, Side.RED))]

    def _menu_side_at(self, pos: tuple[int, int]) -> Side | None:
        """鼠标位置落在哪个阵营卡片上；没有则返回 None。"""
        for side, r in self._menu_side_rects():
            if r.collidepoint(pos):
                return side
        return None

    def _menu_max_scroll(self, cols: int, card_h: int, row_h: int) -> int:
        """最大滚动行数——超出可视行的部分才需要滚动。"""
        area = self._menu_cards_area()
        rows = (len(self.scenarios) + cols - 1) // cols
        vis_rows = max(1, area.height // row_h)
        return max(0, rows - vis_rows)

    def _menu_scroll_to_index(self) -> None:
        """把选中项滚进可视范围。"""
        cols, _cw, _ch, row_h = self._menu_grid()
        if cols <= 0:
            return
        row = self.menu_index // cols
        max_scroll = self._menu_max_scroll(cols, _ch, row_h)
        vis_rows = max(1, self._menu_cards_area().height // row_h)
        if row < self.menu_scroll:
            self.menu_scroll = row
        elif row >= self.menu_scroll + vis_rows:
            self.menu_scroll = row - vis_rows + 1
        self.menu_scroll = max(0, min(self.menu_scroll, max_scroll))

    def _menu_import_rect(self) -> pygame.Rect:
        """主菜单「导入想定」按钮。"""
        return pygame.Rect(self.w - 60 - 110, 168, 110, 24)

    def _menu_replay_rect(self) -> pygame.Rect:
        """主菜单「导入回放」按钮（在「导入想定」左侧）。"""
        return pygame.Rect(self.w - 60 - 110 - 8 - 110, 168, 110, 24)

    def _replays_dir(self) -> str:
        return _paths.replays_dir()

    def _menu_move_cursor(self, delta_row: int) -> None:
        """上下移动想定选中项（按网格行）。"""
        if not self.scenarios:
            return
        cols, _cw, _ch, _rh = self._menu_grid()
        row, col = divmod(self.menu_index, cols)
        row += delta_row
        if row < 0 or row * cols >= len(self.scenarios):
            return
        self.menu_index = row * cols + min(col, len(self.scenarios) - row * cols - 1)
        self._menu_scroll_to_index()

    def _menu_start_rect(self) -> pygame.Rect:
        return pygame.Rect(60, self._menu_button_y(), 240, 46)

    def _menu_edit_rect(self) -> pygame.Rect:
        return pygame.Rect(316, self._menu_button_y(), 200, 46)

    def _menu_tutorial_rect(self) -> pygame.Rect:
        return pygame.Rect(532, self._menu_button_y(), 170, 46)

    def _menu_campaign_rect(self) -> pygame.Rect:
        return pygame.Rect(718, self._menu_button_y(), 130, 46)

    def _menu_settings_rect(self) -> pygame.Rect:
        return pygame.Rect(864, self._menu_button_y(), 110, 46)

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
            self.set_status(tr("ui.st.tutorial_no_map"), 4)
            return
        self.menu_index = idx
        self.start_game()
        if self.state is not None:
            self.tutorial_visible = True
            self._tutorial_notice = 3.0
            self.set_status(tr("ui.st.tutorial_start"), 6)

    def _toggle_tutorial(self) -> None:
        """显示 / 隐藏教学提示条。"""
        if self.tutorial is None:
            return
        self.tutorial_visible = not self.tutorial_visible

    def _skip_tutorial(self) -> None:
        if self.tutorial is not None:
            self.tutorial.skip()
        self.tutorial_visible = False
        self.set_status(tr("ui.st.tutorial_exit"), 3)

    def _tutorial_note(self, event: str) -> None:
        """把事件投递给教学引导机，并在推进时刷新提示。"""
        if self.tutorial is None or self.tutorial.finished:
            return
        before = self.tutorial.index
        self.tutorial.note(event)
        if self.tutorial.index != before:
            step = self.tutorial.current
            if step is not None:
                self.state.log_message(None, tr("ui.log.tutorial_step_fmt") % (step.title,),
                                       "tutorial")
            elif self.tutorial.finished:
                self.state.log_message(None, tr("ui.log.tutorial_done"), "tutorial")
                self.tutorial_visible = False
                self.set_status(tr("ui.st.tutorial_done"), 4)

    def _tutorial_poll(self) -> None:
        """轮询状态型教学步骤。"""
        if self.tutorial is None or self.tutorial.finished or self.state is None:
            return
        before = self.tutorial.index
        self.tutorial.poll(self.state)
        if self.tutorial.index != before:
            step = self.tutorial.current
            if step is not None:
                self.state.log_message(None, tr("ui.log.tutorial_step_fmt") % (step.title,),
                                       "tutorial")
            elif self.tutorial.finished:
                self.state.log_message(None, tr("ui.log.tutorial_done"), "tutorial")
                self.tutorial_visible = False
                self.set_status(tr("ui.st.tutorial_done"), 4)

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #
    def _apply_theme(self, theme_id: str) -> bool:
        """切换界面主题（立即生效）。"""
        ok = T.set_theme(theme_id)
        if ok:
            self.cfg.set_theme(T.current_theme().id)
        return ok

    def _apply_language(self, lang: str) -> bool:
        """切换界面语言（立即生效）。"""
        ok = i18n.set_language(lang)
        if ok:
            self.cfg.set_language(i18n.current_language())
            # 顶栏按钮的文案是构建时取的译文，不重建就会停在旧语言上。
            self._build_buttons()
        return ok

    def _apply_sound(self, enabled: bool) -> None:
        """开关音效（立即生效）。"""
        self.cfg.set_sound(enabled)
        if self.audio is not None:
            self.audio.set_enabled(enabled)
            self.audio.set_volume(self.cfg.volume)

    def _apply_icon_pack(self, pack_id: str) -> None:
        """切换单位图标包（立即生效）。"""
        self.cfg.set_pref("icon_pack", pack_id or "")
        icons.set_current_pack(pack_id or "")
        # 棋子与图标缓存都要重建，否则会残留旧图标的像素
        if self.mapview is not None:
            self.mapview._token_cache.clear()
            self.mapview._vector_icons.clear()
        icons.clear_cache()

    def _sfx(self, name: str) -> None:
        """播放音效（静音或混音器不可用时静默）。"""
        if self.audio is not None:
            self.audio.play(name)

    def open_settings(self) -> None:
        """打开设置面板。"""
        if self.settings_panel is None:
            self.settings_panel = SettingsPanel(
                self.cfg,
                on_theme=self._apply_theme,
                on_sound=self._apply_sound,
                on_language=self._apply_language,
                on_icons=self._apply_icon_pack,
            )
        self.show_settings = True

    def close_settings(self) -> None:
        """关闭设置面板并保存配置。"""
        self.show_settings = False
        try:
            self.cfg.save()
            self.keymap = build_reverse(self.cfg)
            self.set_status(tr("ui.st.keymap_saved"), 3)
        except ConfigError as exc:
            self.set_status(str(exc), 5)

    def _game_over(self) -> None:
        """对局结束：战役模式下结算本关并准备进入下一关。"""
        self.mode = "over"
        # 战斗回顾数据
        if self.state is not None:
            try:
                self.debrief = build_debrief(self.state)
            except Exception:
                self.debrief = None
        # 胜负音效
        if self.state is not None:
            w = self.state.winner
            if w is None:
                self._sfx("click")
            elif w is self.side:
                self._sfx("victory")
            else:
                self._sfx("defeat")
        if self.campaign is not None and self.campaign_progress is not None \
                and self.state is not None and self.state.over:
            self._campaign_step_complete()

    # ------------------------------------------------------------------ #
    # 战役模式
    # ------------------------------------------------------------------ #
    def _scan_campaigns(self) -> None:
        self.campaigns = list_campaigns()

    def _campaign_root(self) -> str:
        """战役进度的存放根目录（用户可写目录，冻结版也能写入）。"""
        return _paths.user_writable_dir()

    def start_campaign(self, camp: CampaignDef) -> None:
        """开始（或继续）一个战役。"""
        prog = load_progress(self._campaign_root(), camp.id)
        if prog is None:
            prog = CampaignProgress(campaign_id=camp.id)
        self.campaign = camp
        self.campaign_progress = prog
        self.campaign_pending = False
        self._start_campaign_step()

    def _start_campaign_step(self) -> None:
        """载入当前关卡，并把上一关的幸存部队带进来。"""
        camp = self.campaign
        prog = self.campaign_progress
        if camp is None or prog is None:
            return
        if prog.step_index >= camp.total_steps:
            self._finish_campaign()
            return
        step = camp.steps[prog.step_index]
        try:
            path = resolve_map_path(step.map_id, MAP_DIR)
            scenario = load_scenario(path, ARMORY)
        except (CampaignError, LoadError) as exc:
            self.set_status(tr("ui.st.campaign_cannot_continue_fmt") % (exc,), 6)
            self.mode = "menu"
            return
        self.state = GameState(scenario, self.menu_side)
        self.state.scenario_path = path
        self.state.armory_path = ARMORY
        # 每局一份新的命令流；种子由 GameState 生成并保存在 state.seed
        self.cmd_log = CommandLog()
        self.side = self.menu_side
        self.mode = "game"
        self.tab = "unit"
        self.pending_target = None
        self.pending_facility = None
        # 键盘光标不跨局保留：新关卡的选中单位与地图都换了，
        # 沿用上一关的坐标会让首次方向键从地图另一头起跳。
        self.cursor_visible = False
        self.show_menu = False
        self.status = ""
        self.tutorial = None
        self.tutorial_visible = False
        # 战役每关都给出简报——但先清掉上一关的残留状态
        self.debrief = None
        self.show_briefing = False
        self.briefing = None
        # 玩家命令流——回放用（想定 + 种子 + 命令 = 可完整重演的一局）
        self.cmd_log = CommandLog()
        # 继承上一关的幸存部队
        carried = apply_carryover(self.state, prog.carryover, self.menu_side)
        if carried:
            self.state.log_message(
                self.menu_side,
                tr("ui.log.campaign_carryover_fmt") % (
                    len(carried), camp.name, step.title),
                "system")
        # 累计上一关的胜负点
        if prog.vp:
            self.state.vp[Side.BLUE] += prog.vp.get("BLUE", 0)
            self.state.vp[Side.RED] += prog.vp.get("RED", 0)
        # 难度曲线：难度越高，红方在本关起始就握有一定优势。
        # 这让「编排难度」成为**可感知**的机制，而不只是个标签。
        diff_bonus = (step.difficulty - 2) * 3
        if diff_bonus:
            self.state.vp[Side.RED] += max(0, diff_bonus)
            self.state.vp[Side.BLUE] += max(0, -diff_bonus)
        self.state.log_message(None, tr("ui.log.campaign_step_fmt") % (camp.name, step.title),
                               "system")
        if step.brief:
            self.state.log_message(None, step.brief, "system")
        self.state.log_message(
            None, tr("ui.log.difficulty_fmt") % (step.difficulty_label,)
                  + (tr("ui.log.difficulty_red_edge") if diff_bonus > 0 else ""), "system")
        self._layout_game()
        self._select_first_unit()
        self.set_status(f"{camp.name} · {step.title}", 6)
        # 每关都给出作战简报——战役同样需要让玩家知道这一关怎么打
        self.briefing = build_briefing(self.state)
        self.show_briefing = True

    def _campaign_step_complete(self) -> None:
        """一关结束：累计胜负点、收集幸存部队、推进到下一关。"""
        camp = self.campaign
        prog = self.campaign_progress
        if camp is None or prog is None or self.state is None:
            return
        prog.vp["BLUE"] = prog.vp.get("BLUE", 0) + self.state.vp[Side.BLUE]
        prog.vp["RED"] = prog.vp.get("RED", 0) + self.state.vp[Side.RED]
        step = camp.steps[prog.step_index]
        prog.completed.append(step.map_id)
        # 按本关的继承策略筛选幸存部队——下一关据此决定带多少兵过去
        prog.carryover = apply_carryover_policy(
            collect_survivors(self.state, self.menu_side), step.carryover)
        prog.step_index += 1
        prog.log.append(tr("ui.log.step_progress_fmt") % (
            step.title, prog.blue_vp, prog.red_vp, len(prog.carryover)))
        save_progress(self._campaign_root(), prog)
        if prog.step_index >= camp.total_steps:
            self._finish_campaign()
        else:
            self.campaign_pending = True
            self.set_status(tr("ui.st.step_done_fmt") % (step.title,), 8)

    def _finish_campaign(self) -> None:
        """战役结束。"""
        camp = self.campaign
        prog = self.campaign_progress
        if camp is None or prog is None:
            return
        self.campaign_pending = False
        won = prog.blue_vp > prog.red_vp
        self.set_status(
            tr("ui.st.campaign_end_fmt") % (
                camp.name, prog.blue_vp, prog.red_vp,
                tr("ui.st.victory") if won else tr("ui.st.defeat")), 10)
        self.state.log_message(
            None,
            tr("ui.log.campaign_end_fmt") % (
                camp.name, prog.blue_vp, prog.red_vp,
                tr("ui.log.side_blue_wins") if won else tr("ui.log.side_red_wins")),
            "victory")
        self.mode = "over"

    def _campaign_next_step(self) -> None:
        """玩家确认后进入下一关。"""
        if not self.campaign_pending:
            return
        self.campaign_pending = False
        self._start_campaign_step()

    # ------------------------------------------------------------------ #
    # 战役菜单
    # ------------------------------------------------------------------ #
    def _draw_campaign_menu(self, s: pygame.Surface) -> None:
        """战役选择浮层：列出可用战役与进度。"""
        if not hasattr(self, "campaigns"):
            self._scan_campaigns()
        camps = getattr(self, "campaigns", [])
        w = 620
        h = 130 + max(1, len(camps)) * 86
        r = pygame.Rect((self.w - w) // 2, max(60, (self.h - h) // 2), w, h)
        dim = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 200))
        s.blit(dim, (0, 0))
        T.panel(s, r, T.mix(T.PANEL, T.PURPLE, 0.15), T.PURPLE, radius=6)
        T.draw_text(s, tr("ui.campaign_title"), (r.x + 22, r.y + 16), 17, T.TEXT, bold=True)
        T.draw_text(s, tr("ui.campaign_desc"),
                    (r.x + 22, r.y + 40), 11, T.TEXT_FAINT)
        T.hline(s, r.x + 20, r.right - 20, r.y + 62, T.BORDER)

        self._campaign_rows: list[tuple[pygame.Rect, CampaignDef]] = []
        if not camps:
            T.draw_text(s, tr("ui.campaign_empty"), (r.x + 22, r.y + 84),
                        12, T.TEXT_DIM)
        y = r.y + 74
        for c in camps:
            prog = load_progress(self._campaign_root(), c.id)
            row = pygame.Rect(r.x + 16, y, r.width - 32, 78)
            hover = row.collidepoint(self.mouse)
            pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL_SUNK, row, border_radius=4)
            pygame.draw.rect(s, T.PURPLE if hover else T.BORDER, row, 1, border_radius=4)
            T.draw_text(s, c.name, (row.x + 12, row.y + 8), 15, T.TEXT, bold=True)
            if c.author:
                T.draw_text(s, tr("ui.author_of") % (c.author,), (row.right - 12, row.y + 11), 11,
                            T.TEXT_FAINT, anchor="topright")
            T.draw_text(s, tr("ui.campaign_step_desc") % (c.total_steps, c.description[:52],),
                        11, T.TEXT_DIM)
            if prog is not None:
                done = prog.step_index
                T.draw_text(s, tr("ui.campaign_progress") % (done, c.total_steps, prog.blue_vp, prog.red_vp,),
                            (row.x + 12, row.y + 50), 11, T.CYAN)
                T.draw_text(s, tr("ui.click_continue"), (row.right - 12, row.y + 50), 11, T.CYAN,
                            anchor="topright")
            else:
                T.draw_text(s, tr("ui.click_start"), (row.right - 12, row.y + 50), 11, T.TEXT_FAINT,
                            anchor="topright")
            self._campaign_rows.append((row, c))
            y += 86

    def _click_campaign_menu(self, pos: tuple[int, int]) -> None:
        for row, c in getattr(self, "_campaign_rows", []):
            if row.collidepoint(pos):
                self.show_campaign_menu = False
                self.start_campaign(c)
                return

    # ------------------------------------------------------------------ #
    # 想定包导入导出
    # ------------------------------------------------------------------ #
    def _packages_dir(self) -> str:
        """社区想定包存放目录（用户数据目录下）。"""
        return os.path.join(_paths.user_writable_dir(), "packages")

    def export_scenario_package(self) -> None:
        """把当前想定导出为社区包（.bwscn）。"""
        if self.state is None or not self.state.scenario_path:
            self.set_status(tr("ui.st.no_export_map"), 3)
            return
        try:
            d = self._packages_dir()
            os.makedirs(d, exist_ok=True)
            meta = getattr(self.state.scenario, "meta", None)
            out = os.path.join(d, os.path.splitext(
                os.path.basename(self.state.scenario_path))[0] + _pkg.PACKAGE_SUFFIX)
            _pkg.export_package([self.state.scenario_path], ARMORY, out,
                                name=self.state.map.name,
                                author=(meta.author if meta else ""),
                                include_armory=False)
            self.set_status(tr("ui.st.package_exported_fmt") % (out,), 6)
        except (_pkg.PackageError, OSError) as exc:
            self.set_status(tr("ui.st.export_failed_fmt") % (exc,), 5)

    def import_scenario_package(self) -> None:
        """从社区包目录导入最近修改的一个想定包。"""
        d = self._packages_dir()
        if not os.path.isdir(d):
            self.set_status(tr("ui.st.no_package_import"), 5)
            return
        packs = _pkg.list_packages(d)
        if not packs:
            self.set_status(tr("ui.st.no_package_file"), 5)
            return
        packs.sort(key=lambda p: os.path.getmtime(p["path"]), reverse=True)
        self._import_package(packs[0]["path"])

    def _import_package(self, path: str) -> None:
        """导入指定的想定包。

        导入目标固定为**用户可写目录**下的 `maps/`——冻结版的安装目录
        未必可写，装到用户目录才可靠。导入后重新扫描想定列表。
        """
        try:
            res = _pkg.import_package(path, _paths.user_maps_dir(), ARMORY,
                                       merge_armory=True)
            self._scan_scenarios()
            extra = (tr("ui.st.package_extra_templates_fmt") % (len(res.added_templates),)
                     if res.added_templates else "")
            self.set_status(
                tr("ui.st.package_imported_fmt") % (
                    os.path.basename(path), len(res.added_maps),
                    res.manifest.name, extra), 6)
        except _pkg.PackageError as exc:
            self.set_status(tr("ui.st.import_failed_fmt") % (exc,), 5)

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
            self.set_status(tr("ui.st.report_export_failed_fmt") % (exc,), 5)
            return
        self.set_status(tr("ui.st.report_exported_fmt") % (path,), 6)

    def _play_replay(self, path: str) -> None:
        """播放一份回放。"""
        try:
            rf = ReplayFile.load(path)
        except Exception as exc:
            self.set_status(tr("ui.st.replay_read_failed_fmt") % (exc,), 5)
            return
        # 想定按文件名在安装目录与用户目录里找
        name = os.path.basename(rf.scenario)
        found = None
        for d in (MAP_DIR, _paths.user_maps_dir()):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                found = cand
                break
        if found is None:
            self.set_status(tr("ui.st.replay_map_missing_fmt") % (name,), 5)
            return
        try:
            scenario = load_scenario(found, ARMORY)
        except Exception as exc:
            self.set_status(tr("ui.st.replay_map_failed_fmt") % (exc,), 5)
            return
        player = ReplayPlayer(scenario, rf)
        self.replay_player = player
        self.state = player.state
        # 回放不跑教学系统——教学提示条不该出现在重演里
        self.tutorial = None
        self.tutorial_visible = False
        self.side = player.state.player_side
        self.mode = "replay"
        self.show_menu = False
        self.show_help = False
        self.pending_target = None
        self.pending_facility = None
        self.reachable = {}
        self.tab = "log"
        self._layout_game()
        self.set_status(tr("ui.st.replay_started_fmt") % (
                        os.path.basename(path), player.total, rf.seed), 6)

    def stop_replay(self) -> None:
        """退出回放，回到主菜单。"""
        self.replay_player = None
        self.mode = "menu"
        self.state = None
        self._scan_scenarios()
        self._scan_campaigns()
        self.set_status(tr("ui.st.replay_exit"), 3)

    def export_replay(self) -> None:
        """把本局导出为回放文件（想定 + 种子 + 命令流）。"""
        if self.state is None or self.cmd_log is None:
            return
        if not len(self.cmd_log):
            self.set_status(tr("ui.st.replay_empty"), 4)
            return
        scenario = os.path.basename(getattr(self.state, "scenario_path", "") or "")
        if not scenario:
            scenario = f"{self.state.map.name}.json"
        try:
            rf = ReplayFile.from_game(self.state, scenario, self.cmd_log,
                                      note=f"{self.state.map.name} · {self.side.label}")
            d = _paths.replays_dir()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(d, f"{os.path.splitext(scenario)[0]}-{stamp}.bwrep")
            rf.save(path)
        except OSError as exc:
            self.set_status(tr("ui.st.replay_export_failed_fmt") % (exc,), 5)
            return
        self.set_status(tr("ui.st.replay_exported_fmt") % (
                        os.path.basename(path), len(self.cmd_log),
                        self.state.seed), 6)

    def _draw_menu_features(self, s: pygame.Surface, r: pygame.Rect) -> None:
        T.panel(s, r, T.PANEL, T.BORDER, radius=5)
        T.draw_text(s, tr("ui.mechanics_brief"), (r.x + 16, r.y + 14), 13, T.CYAN, bold=True)
        T.hline(s, r.x + 16, r.right - 16, r.y + 36, T.BORDER)
        items = [
            (tr("ui.menu.feat_recon_title"), tr("ui.menu.feat_recon_desc")),
            (tr("ui.menu.feat_ew_title"), tr("ui.menu.feat_ew_desc")),
            (tr("ui.menu.feat_aa_title"), tr("ui.menu.feat_aa_desc")),
            (tr("ui.menu.feat_standoff_title"), tr("ui.menu.feat_standoff_desc")),
            (tr("ui.menu.feat_space_title"), tr("ui.menu.feat_space_desc")),
        ]
        yy = r.y + 48
        for title, desc in items:
            if yy > r.bottom - 30:
                break
            pygame.draw.rect(s, T.CYAN, pygame.Rect(r.x + 16, yy + 3, 2, 10))
            T.draw_text(s, title, (r.x + 24, yy), 12, T.TEXT, bold=True)
            yy += 17
            # 换行宽度必须按**绘制字号**算：按 10 号排、用 11 号画，
            # 拉丁字母会宽出一成，右侧被切掉。
            room = max(0, (r.bottom - 12 - yy) // 14)
            for line in T.wrap_text(desc, 11, r.width - 40)[:room]:
                T.draw_text(s, line, (r.x + 24, yy), 11, T.TEXT_DIM)
                yy += 14
            yy += 6

    def _draw_menu(self) -> None:
        s = self.screen
        s.fill(T.BG)
        # 背景网格
        for x in range(0, self.w, 48):
            pygame.draw.line(s, T.GRID, (x, 0), (x, self.h))
        for y in range(0, self.h, 48):
            pygame.draw.line(s, T.GRID, (0, y), (self.w, y))
        T.draw_text(s, tr("ui.game_title"), (60, 54), 40, T.TEXT, bold=True)
        T.draw_text(s, tr("ui.game_subtitle"), (62, 104), 15, T.CYAN)
        T.draw_text(s, tr("ui.game_tagline"), (62, 128), 12, T.TEXT_DIM)
        T.hline(s, 60, self.w - 60, 158, T.BORDER)

        T.draw_text(s, i18n.tr("menu.select_scenario"), (60, 176), 13, T.TEXT_DIM, bold=True)
        # 导入按钮：把社区想定包导入用户目录
        self._menu_import_btn = self._menu_import_rect()
        _imp_hover = self._menu_import_btn.collidepoint(self.mouse)
        pygame.draw.rect(s, T.PANEL_HI if _imp_hover else T.PANEL, self._menu_import_btn,
                         border_radius=4)
        pygame.draw.rect(s, T.CYAN if _imp_hover else T.BORDER_HI, self._menu_import_btn, 1,
                         border_radius=4)
        T.draw_text(s, i18n.tr("menu.import"), self._menu_import_btn.center, 12,
                    T.TEXT if _imp_hover else T.TEXT_DIM, anchor="center")
        # 回放按钮：重演之前打过的某一局
        self._menu_replay_btn = self._menu_replay_rect()
        _rep_hover = self._menu_replay_btn.collidepoint(self.mouse)
        pygame.draw.rect(s, T.PANEL_HI if _rep_hover else T.PANEL, self._menu_replay_btn,
                         border_radius=4)
        pygame.draw.rect(s, T.CYAN if _rep_hover else T.BORDER_HI, self._menu_replay_btn, 1,
                         border_radius=4)
        T.draw_text(s, i18n.tr("menu.replay"), self._menu_replay_btn.center, 12,
                    T.TEXT if _rep_hover else T.TEXT_DIM, anchor="center")

        # 想定卡片：网格排列，超出可视高度时可纵向滚动
        area = self._menu_cards_area()
        cols, card_w, card_h, row_h = self._menu_grid()
        max_scroll = self._menu_max_scroll(cols, card_h, row_h)
        self.menu_scroll = max(0, min(self.menu_scroll, max_scroll))
        vis_rows = max(1, area.height // row_h)

        for i, sc in enumerate(self.scenarios):
            r = self._menu_card_rect(i)
            if r is None:
                continue
            active = i == self.menu_index
            hovered = r.collidepoint(self.mouse)
            is_tut = bool(sc.get("tutorial"))
            # 悬停与选中是**两件事**：悬停只做视觉提示（边框更亮），
            # 真正的选中必须点击——否则鼠标滑过就换想定，手感极差。
            if active:
                T.panel(s, r, T.PANEL_HI, T.CYAN, radius=5)
                pygame.draw.rect(s, T.CYAN, pygame.Rect(r.x, r.y, r.w, 2))
            elif hovered:
                T.panel(s, r, T.PANEL_HI, T.BORDER_HI, radius=5)
            else:
                T.panel(s, r, T.PANEL, T.BORDER, radius=5)
            # 卡片内元素按固定行序排布，避免互相重叠：
            #   标题行 → 规格行 → 元数据行 → 描述（最多 2 行）
            head_y = r.y + 12
            if is_tut:
                # 教学想定：左上角角标 + 高亮边框
                pygame.draw.rect(s, T.GREEN, r, 1, border_radius=5)
                badge = pygame.Rect(r.x + 12, head_y, 56, 17)
                pygame.draw.rect(s, T.mix(T.PANEL, T.GREEN, 0.22), badge, border_radius=3)
                T.draw_text(s, i18n.tr("menu.tutorial_tag"), badge.center, 11,
                            T.mix(T.GREEN, T.WHITE, 0.4), anchor="center")
            T.draw_text(s, sc["name"], (r.x + 16, head_y + (20 if is_tut else 0)), 16,
                        T.TEXT, bold=True)
            T.draw_text(s, tr("ui.map_size_fmt") % (sc['size'][0], sc['size'][1], sc['tile_km'],) +
                            tr("ui.map_turns_fmt") % (sc['turns'], sc['blue'], sc['red']),
                        (r.x + 16, head_y + 38), 11, T.TEXT_FAINT)
            # 元数据：难度标签 + 作者 + 事件数（同一行内横向排布）
            meta = sc.get("meta") or {}
            diff = meta.get("difficulty", "")
            meta_y = head_y + 56
            bx = r.x + 16
            if diff:
                accent = DIFFICULTY_ACCENTS.get(diff, "")
                col_c = getattr(T, accent.upper()) if accent else T.CYAN
                label = difficulty_label(diff)
                bw = T.text_width(label, 11) + 14
                chip = pygame.Rect(bx, meta_y, bw, 16)
                # 芯片底色由主题派生，浅色主题下不会变成深色块
                pygame.draw.rect(s, T.button_fill(col_c), chip,
                                 border_radius=3)
                pygame.draw.rect(s, col_c, chip, 1, border_radius=3)
                T.draw_text(s, label, chip.center, 11, col_c, anchor="center")
                bx += bw + 6
            if meta.get("author"):
                T.draw_text(s, tr("ui.author_fmt") % (meta['author'],), (bx, meta_y + 2), 11, T.TEXT_FAINT)
            if sc.get("events"):
                T.draw_text(s, tr("ui.events_count_fmt") % (sc['events'],), (r.right - 16, meta_y + 2), 11,
                            T.PURPLE, anchor="topright")
            # 标签行（单独一行，不与难度标签重叠）
            # 注意：循环变量不能用 i——外层 i 是想定索引，被覆盖会导致悬停高亮错位
            for ti, tag in enumerate(meta.get("tags", [])[:3]):
                tx = r.x + 16 + ti * 62
                T.draw_text(s, f"#{tag}", (tx, meta_y + 20), 11, T.mix(T.CYAN, T.TEXT_DIM, 0.5))
            # 描述：放在元数据下方，最多 1 行（卡片高度有限，多了会挤到底部提示）
            yy = meta_y + 36
            for line in T.wrap_text(sc["desc"], 11, r.width - 32)[:1]:
                T.draw_text(s, line, (r.x + 16, yy), 11, T.TEXT_DIM)
            # 底部提示：选中态显示「已选中」，悬停但未选中显示「点击选择」
            if active:
                hint, hint_col = tr("ui.menu.picked"), T.CYAN
            elif hovered:
                hint, hint_col = tr("ui.menu.click_pick"), T.mix(T.CYAN, T.TEXT_DIM, 0.4)
            else:
                hint, hint_col = tr("ui.menu.click_pick"), T.TEXT_FAINT
            T.draw_text(s, hint, (r.x + 16, r.bottom - 20), 11, hint_col)

        # 滚动条：想定多于可视行数时才出现
        if max_scroll > 0:
            track = pygame.Rect(area.right - 6, area.y, 4, area.height)
            pygame.draw.rect(s, T.PANEL_SUNK, track, border_radius=2)
            knob_h = max(24, int(track.height * vis_rows / max(1, vis_rows + max_scroll)))
            knob_y = track.y + int((track.height - knob_h)
                                   * (self.menu_scroll / max(1, max_scroll)))
            pygame.draw.rect(s, T.BORDER_HI, pygame.Rect(track.x, knob_y, 4, knob_h),
                             border_radius=2)
            T.draw_text(s, f"{self.menu_index + 1}/{len(self.scenarios)}",
                        (area.right - 14, area.bottom + 4), 11, T.TEXT_FAINT,
                        anchor="topright")

        # 右侧机制速览（窄窗口下允许省略，但不能整块消失）
        px = area.right + 18
        pw = self.w - px - 48
        if pw >= 220:
            self._draw_menu_features(s, pygame.Rect(px, 200, pw, 210))

        # 阵营选择（放在右侧面板下方）
        side_x = px
        T.draw_text(s, tr("ui.select_side"), (side_x, self.SIDE_CARD_Y - 18),
                    T.FS_LABEL, T.TEXT_DIM, bold=True)
        for (side, label, desc), (_sd, r) in zip([
            (Side.BLUE, tr("ui.menu.side_blue_name"), tr("ui.menu.side_blue_desc")),
            (Side.RED, tr("ui.menu.side_red_name"), tr("ui.menu.side_red_desc")),
        ], self._menu_side_rects()):
            active = self.menu_side is side
            hovered = r.collidepoint(self.mouse)
            # 与想定卡片一致：悬停只做视觉提示，选中必须点击
            if active:
                T.panel(s, r, T.PANEL_HI, T.side_color(side), radius=4)
            elif hovered:
                T.panel(s, r, T.PANEL_HI, T.BORDER_HI, radius=4)
            else:
                T.panel(s, r, T.PANEL, T.BORDER, radius=4)
            pygame.draw.rect(s, T.side_color(side), pygame.Rect(r.x + 10, r.y + 9, 4, 24))
            T.draw_text(s, label, (r.x + 22, r.y + 2), 14, T.TEXT, bold=True)
            # 描述自动换行——右栏可能只有 240px 宽，硬截断会把末字切掉。
            # 只留两行且字号降到 FS_SMALL-1：德语/俄语比中文长得多，
            # 11 号两行会顶出卡片下沿。
            for k, line in enumerate(T.wrap_text(desc, T.FS_SMALL, r.width - 32)[:2]):
                T.draw_text(s, line, (r.x + 22, r.y + 22 + k * 11), T.FS_SMALL - 1,
                            T.TEXT_DIM)
            # 提示：已选 / 可点击
            tip = tr("ui.menu.picked") if active else (
                tr("ui.menu.click_pick") if hovered else "")
            if tip:
                T.draw_text(s, tip, (r.right - 12, r.y + 6), 10,
                            T.CYAN if active else T.mix(T.CYAN, T.TEXT_DIM, 0.4),
                            anchor="topright")

        # -- 主菜单按钮 ---------------------------------------------------
        # 底色与文字色都由主题派生（T.button_fill / T.button_text），
        # 不再硬编码深色——否则亮色主题下这些按钮会变成突兀的黑块。

        # 开始按钮（主行动）
        start = self._menu_start_rect()
        hover = start.collidepoint(self.mouse)
        pygame.draw.rect(s, T.button_fill(T.CYAN, hover), start, border_radius=4)
        pygame.draw.rect(s, T.CYAN, start, 1, border_radius=4)
        T.draw_text(s, i18n.tr("menu.start") + "  [Enter]", start.center, 16,
                    T.button_text(T.CYAN, hover), bold=True, anchor="center")

        edit_r = self._menu_edit_rect()
        hover = edit_r.collidepoint(self.mouse)
        pygame.draw.rect(s, T.button_fill(T.CYAN, hover), edit_r, border_radius=4)
        pygame.draw.rect(s, T.CYAN if hover else T.BORDER_HI, edit_r, 1, border_radius=4)
        T.draw_text(s, i18n.tr("menu.editor") + "  [E]", edit_r.center, 13,
                    T.button_text(T.CYAN, hover), anchor="center")

        # 快速上手：直接进入教学想定（首次运行时高亮）
        tut_r = self._menu_tutorial_rect()
        hover = tut_r.collidepoint(self.mouse)
        first_run = self.cfg.first_run
        # 首次运行时用绿色底突出，但亮度仍跟随主题
        pygame.draw.rect(s, T.button_fill(T.GREEN, hover or first_run),
                         tut_r, border_radius=4)
        pygame.draw.rect(s, T.GREEN if (hover or first_run) else T.BORDER_HI, tut_r, 1,
                         border_radius=4)
        T.draw_text(s, i18n.tr("menu.tutorial") + "  [T]", tut_r.center, 13,
                    T.button_text(T.GREEN, hover or first_run),
                    bold=first_run, anchor="center")

        # 战役入口
        camp_r = self._menu_campaign_rect()
        hover = camp_r.collidepoint(self.mouse)
        pygame.draw.rect(s, T.button_fill(T.PURPLE, hover), camp_r, border_radius=4)
        pygame.draw.rect(s, T.PURPLE if hover else T.BORDER_HI, camp_r, 1, border_radius=4)
        T.draw_text(s, tr("ui.campaign_title") + "  [C]", camp_r.center, 13,
                    T.button_text(T.PURPLE, hover), anchor="center")

        # 设置入口
        set_r = self._menu_settings_rect()
        hover = set_r.collidepoint(self.mouse)
        pygame.draw.rect(s, T.button_fill(T.CYAN, hover), set_r, border_radius=4)
        pygame.draw.rect(s, T.CYAN if hover else T.BORDER_HI, set_r, 1, border_radius=4)
        T.draw_text(s, i18n.tr("menu.settings") + "  [O]", set_r.center, 13,
                    T.button_text(T.CYAN, hover), anchor="center")


        if first_run:
            T.draw_text(s, tr("ui.first_run_hint"),
                        (60, y + 190), 12, T.GREEN)

        tips = [tr("ui.menu.tip_data"), tr("ui.menu.tip_controls")]
        yy = self.h - 92
        for t in tips:
            T.draw_text(s, t, (60, yy), 12, T.TEXT_FAINT)
            yy += 20
        if self.error:
            T.draw_text(s, self.error, (60, self.h - 44), 12, T.RED)

        # 导入 / 回放浮层（模态，画在最上层）
        # 两个浮层都要画——漏掉任何一个都会让对应按钮「点了没反应」。
        if self._import_dialog is not None:
            self._import_dialog.draw(s)
        if self._replay_dialog is not None:
            self._replay_dialog.draw(s)

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
        # 每局一份新的命令流；种子由 GameState 生成并保存在 state.seed
        self.cmd_log = CommandLog()
        self.side = self.menu_side
        self.mode = "game"
        self.tab = "unit"
        self.pending_target = None
        self.pending_facility = None
        self.cursor_visible = False       # 见战役入口处的说明：光标不跨局保留
        self.show_menu = False
        self.show_settings = False
        self.ai_timer = 0.0
        self.status = ""
        # 教学引导：仅当想定真的带教学脚本时才启用。
        # build_tutorial 对普通想定返回 None —— 教学不是所有想定的默认行为。
        self.tutorial = build_tutorial(scenario.raw)
        self.tutorial_visible = self.tutorial is not None
        if self.tutorial_visible and self.tutorial.current is not None:
            self.state.log_message(
                None, tr("ui.log.tutorial_start_fmt") % (self.tutorial.current.title,),
                "tutorial")
        self._layout_game()
        self._select_first_unit()
        # 作战简报：开局前先给玩家看敌情与胜利条件
        self.briefing = build_briefing(self.state)
        self.show_briefing = True

    def _close_briefing(self) -> None:
        """关闭作战简报，正式进入战斗。"""
        self.show_briefing = False
        self.briefing = None
        self._sfx("click")

    # -- 系统菜单 / 存档 --------------------------------------------------
    def toggle_menu(self) -> None:
        self.show_menu = not self.show_menu
        if self.show_menu:
            self.sys_menu_index = 0
            self._refresh_slots()

    def _sys_menu_activate(self, action: str, arg: object) -> None:
        """执行系统菜单条目——鼠标点击与键盘 Enter 共用这一处。

        **为什么抽出来**：以前这段逻辑只写在 ``_on_mouse_down`` 里，
        加键盘支持时若复制一份，日后新增菜单项必然只改一处。
        """
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
        elif action == "export_pkg":
            self.export_scenario_package()
        elif action == "export_replay":
            self.export_replay()
        elif action == "settings":
            self.show_menu = False
            self.open_settings()
        elif action == "quit":
            self.running = False

    def _refresh_slots(self) -> None:
        self.slot_info = list_slots(ROOT)

    def save_slot(self, i: int) -> None:
        if self.state is None:
            return
        try:
            save_game(self.state, slot_path(ROOT, i))
            self.set_status(tr("ui.st.saved_slot_fmt") % (i, self.state.turn_count))
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
        self.cursor_visible = False       # 光标不跨局保留，读档后重新落到选中单位
        self.ai_timer = 0.0
        self.tab = "unit"
        # 读档直接进入战斗——简报与回顾状态必须清掉，
        # 否则残留的模态浮层会让整个界面点不动
        self.show_briefing = False
        self.briefing = None
        self.debrief = None
        self.mapview.state = st
        self._layout_game()
        self.mapview.state = st
        self.mapview.fit()
        self._select_first_unit()
        self.set_status(tr("ui.st.loaded_slot_fmt") % (i, st.turn_count))

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
        """构建顶栏按钮。

        从右向左依次排布，每个按钮占位后立即把游标左移到自身左边界之外，
        避免出现「后建按钮压在先建按钮上」导致点不到的情况。
        """
        self.buttons = []
        x = self.w - 14
        specs = [
            (i18n.tr("top.end_turn"), self.end_turn, "primary", "end_turn", 96),
            (i18n.tr("top.help"), self.toggle_help, "ghost", "help", 62),
        ]
        for label, cb, kind, act, width in specs:
            x -= width
            btn = Button(pygame.Rect(x, 9, width, 32), label, cb, kind,
                         size=13, hotkey=hotkey_label(self.cfg, act))
            # 记住「结束回合」按钮本体：绘制时要按身份判断可用性，
            # 拿 label 做字符串匹配的话，一换语言就认不出来。
            if act == "end_turn":
                self.end_turn_btn = btn
            self.buttons.append(btn)
            x -= 8                      # 按钮间距
        # 叠加层切换
        x -= 84
        self.overlay_btn = Button(pygame.Rect(x, 9, 84, 32), i18n.tr("top.overlay"),
                                  self.cycle_overlay,
                                  "ghost", size=12)
        self.buttons.append(self.overlay_btn)
        x -= 8
        # 设置
        x -= 62
        self.settings_btn = Button(pygame.Rect(x, 9, 62, 32),
                                   i18n.tr("menu.settings"), self.open_settings,
                                   "ghost", size=12)
        self.buttons.append(self.settings_btn)
        x -= 8
        # 系统菜单（存档/读档/返回）
        x -= 82
        self.menu_btn = Button(pygame.Rect(x, 9, 82, 32), tr("ui.hud.menu"), self.toggle_menu,
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

    def _pick_unit_at(self, x: int, y: int, prefer_side: Side | None = None) -> Unit | None:
        """挑选该格上的单位。

        同格可能同时有地面部队与空中支援，因此不能只取一个：
        * 优先选**己方**单位（玩家想操作自己的兵）；
        * 其次选敌方单位（作为交战目标）；
        * 同阵营内**地面单位优先**——空中单位是支援角色，
          地面部队才是该格的主体。
        """
        us = self.state.units_at(x, y)
        if not us:
            return None
        side = prefer_side if prefer_side is not None else self.side
        own = [u for u in us if u.side is side]
        foe = [u for u in us if u.side is not side]
        pool = own or foe
        ground = [u for u in pool if u.domain is not Domain.AIR]
        return (ground or pool)[0]

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
        # 命令先记录再推进——回放时按同一条命令重演整个回合（含 AI）
        self._record(CMD_END_TURN)
        self.state.end_player_turn()
        self._tutorial_note("turn_end")
        if self.state.over:          # 末回合/全歼可能在结算时直接结束对局
            self._game_over()
            return
        self.state.begin_ai_phase()
        self.ai_timer = 0.0

    def toggle_help(self) -> None:
        self.show_help = not self.show_help
        if self.show_help:
            self.help_scroll = 0     # 每次打开都从顶部看起

    def cycle_overlay(self) -> None:
        if self.mapview is None:
            return
        order = ["none", "sensors", "threat"]
        i = order.index(self.mapview.overlay) if self.mapview.overlay in order else 0
        self.mapview.overlay = order[(i + 1) % len(order)]
        names = {"none": tr("ui.hud.overlay_none"), "sensors": tr("ui.hud.overlay_sensors"),
                 "threat": tr("ui.hud.overlay_threat")}
        self.set_status(tr("ui.st.overlay_fmt") % (names[self.mapview.overlay],))

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
            self.set_status(tr("ui.st.unreachable"))
            return
        cost = rules.path_cost(self.state, u, path)
        if cost > u.mp + 1e-6:
            self.set_status(tr("ui.st.mp_short_fmt") % (f"{cost:.1f}", f"{u.mp:.1f}"))
            return
        if self.state.move_unit(u, path):
            self._record(CMD_MOVE, uid=u.uid, x=gx, y=gy)
            if u.domain is Domain.AIR:
                # 空中单位的机动是教学里的独立一步（空海天协同）
                self._tutorial_note("air_move")
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
                self.set_status(tr("ui.st.no_weapon"))
                return
            res = self.state.strike_facility(u, self.pending_facility, w)
            if res.ok:
                self._record(CMD_FIRE_FACILITY, attacker=u.uid, weapon=w.id,
                             x=self.pending_facility[0], y=self.pending_facility[1])
            self.set_status(res.message, 3.5)
            self.pending_facility = None
            self.reachable = {}
            if self.state.over:
                self._game_over()
            return

        t = self.pending_target
        if not u or not t or not t.alive:
            return
        w = u.weapon_by_id(self.state.selected_weapon or "")
        if w is None:
            self.set_status(tr("ui.st.no_weapon"))
            return
        res = self.state.fire(u, t, w)
        self.set_status(res.message, 3.5)
        if res.ok:
            # 齐射发数要显式记录——回放时不能依赖回放方的默认设置
            self._record(CMD_FIRE, attacker=u.uid, target=t.uid, weapon=w.id,
                         salvo=int(self.state.salvo_setting))
            self._tutorial_note("fire")
        self.pending_target = None
        self.reachable = {}
        if self.state.over:
            self._game_over()

    # ------------------------------------------------------------------ #
    # 键盘地图光标（8.3 无障碍：纯键盘完成整局）
    # ------------------------------------------------------------------ #
    def _sync_cursor(self, grid: tuple[int, int]) -> None:
        """鼠标操作后让键盘光标跟随，保持两套输入连续。"""
        self.cursor = grid
        self.cursor_visible = True

    def _move_cursor(self, k: int) -> None:
        """方向键移动键盘光标；首次使用自动落到选中单位或地图中心。"""
        st = self.state
        if st is None:
            return
        if not self.cursor_visible:
            u = st.selected_unit()
            if u is not None:
                self.cursor = (u.x, u.y)
            else:
                self.cursor = (st.map.width // 2, st.map.height // 2)
            self.cursor_visible = True
        x, y = self.cursor
        if k == pygame.K_UP:
            y -= 1
        elif k == pygame.K_DOWN:
            y += 1
        elif k == pygame.K_LEFT:
            x -= 1
        elif k == pygame.K_RIGHT:
            x += 1
        x = max(0, min(st.map.width - 1, x))
        y = max(0, min(st.map.height - 1, y))
        self.cursor = (x, y)
        self._ensure_cursor_visible()

    def _ensure_cursor_visible(self) -> None:
        """光标接近地图视窗边缘时自动滚屏，始终保持在视野内。

        ``MapView.pan(dx)`` 让地图**内容**整体右移 dx（与鼠标拖拽同向），
        所以要把越界的光标拉回视野，必须朝**相反**方向平移内容。
        这里的正负号是这个方法唯一容易写反的地方，改前请对照
        :meth:`MapView.pan` 的语义。
        """
        mv = self.mapview
        if mv is None:
            return
        r = mv.grid_to_rect(*self.cursor)
        margin = mv.tile
        view = mv.rect
        if r.right > view.right - margin:
            mv.pan(-(r.right - (view.right - margin)), 0)
        elif r.left < view.left + margin:
            mv.pan((view.left + margin) - r.left, 0)
        if r.bottom > view.bottom - margin:
            mv.pan(0, -(r.bottom - (view.bottom - margin)))
        elif r.top < view.top + margin:
            mv.pan(0, (view.top + margin) - r.top)

    def _activate_at(self, grid: tuple[int, int]) -> None:
        """在 grid 处执行与鼠标左键等价的上下文动作。

        从 _on_mouse_down 抽出，供鼠标点击与键盘光标共用，避免两套分叉逻辑。
        注意：刻意不带回合阶段守卫——与鼠标点击行为保持一致（AI 阶段点击
        选择单位本就是无害的）。
        """
        st = self.state
        if st is None:
            return
        u = self._pick_unit_at(*grid)
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
                self.set_status(tr("ui.st.facility_strike_failed_fmt") % (
                    prof.reason if prof else tr("ui.st.no_weapon"),))
                return
            if self.pending_facility == grid:
                self.confirm_fire()
            else:
                self.pending_target = None
                self.pending_facility = grid
                self.tab = "unit"
                self.set_status(
                    tr("ui.st.facility_selected_fmt") % (
                        tile.facility.label, tile.x, tile.y, w.name,
                        f"{prof.pk * 100:.0f}"), 5)
            return

        if u is not None and u.side is not self.side and sel is not None:
            lvl = st.known_level(self.side, u)
            if lvl.level < Detection.CLASSIFIED.level:
                self.set_status(tr("ui.st.target_unidentified"))
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
                        self.set_status(tr("ui.st.target_selected_fmt") % (
                                        u.name, w.name,
                                        f"{p.pk * 100:.0f}"), 5)
                    else:
                        self.set_status(tr("ui.st.cannot_fire_fmt") % (p.reason,), 4)
            return

        if sel is not None:
            self.order_move(*grid)
        else:
            self.select_unit(None)

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
            if self.show_help:
                # 帮助面板是模态：滚轮只滚它，不再缩放下面的地图
                self.help_scroll = max(0, self.help_scroll - e.y * 48)
                return
            if self.mode == "menu":
                # 主菜单：滚轮滚动想定列表
                if self._import_dialog is not None:
                    return
                cols, _cw, _ch, row_h = self._menu_grid()
                max_scroll = self._menu_max_scroll(cols, _ch, row_h)
                if max_scroll > 0:
                    self.menu_scroll = max(0, min(max_scroll,
                                                  self.menu_scroll - e.y))
                return
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

        if self.mode == "replay":
            # 回放模式：只认播放控制键，其余一律忽略
            p = self.replay_player
            if p is None:
                self.stop_replay()
                return
            if k == pygame.K_ESCAPE:
                self.stop_replay()
            elif k == pygame.K_SPACE:
                p.paused = not p.paused
            elif k == pygame.K_RIGHT:
                p.speed = min(8.0, p.speed * 1.5)
            elif k == pygame.K_LEFT:
                p.speed = max(0.25, p.speed / 1.5)
            elif k == pygame.K_END:
                p.skip_to_end()
            return

        if self.mode == "menu":
            # 导入浮层优先：模态，吞掉所有按键
            if self._import_dialog is not None:
                if self._import_dialog.handle_event(e):
                    if getattr(self._import_dialog, "close_requested", False):
                        self._import_dialog.close_requested = False
                        self._import_dialog = None
                return
            # 回放浮层：模态，吞掉所有按键
            if self._replay_dialog is not None:
                if self._replay_dialog.handle_event(e):
                    if getattr(self._replay_dialog, "close_requested", False):
                        self._replay_dialog.close_requested = False
                        self._replay_dialog = None
                return
            if k in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.start_game()
            elif k == pygame.K_ESCAPE:
                self.running = False
            elif k in (pygame.K_LEFT, pygame.K_a):
                self.menu_index = (self.menu_index - 1) % max(1, len(self.scenarios))
                self._menu_scroll_to_index()
            elif k in (pygame.K_RIGHT, pygame.K_d):
                self.menu_index = (self.menu_index + 1) % max(1, len(self.scenarios))
                self._menu_scroll_to_index()
            elif k in (pygame.K_UP, pygame.K_w):
                self._menu_move_cursor(-1)
            elif k in (pygame.K_DOWN, pygame.K_s):
                self._menu_move_cursor(1)
            elif k == pygame.K_TAB:
                self.menu_side = Side.RED if self.menu_side is Side.BLUE else Side.BLUE
            elif k == pygame.K_t:
                self.start_tutorial()
            # v3.0 无障碍：底部三个入口此前只能鼠标点，补上字母快捷键。
            # 不用 s/a/d/w——它们已被想定网格的移动占用。
            elif k == pygame.K_e:
                self.open_editor()
            elif k == pygame.K_c:
                self.show_campaign_menu = not self.show_campaign_menu
            elif k == pygame.K_o:
                self.open_settings()
            return

        # 帮助面板是模态：先把滚动键吃掉，再走动作分发
        if self.show_help and k in (pygame.K_UP, pygame.K_DOWN, pygame.K_PAGEUP,
                                    pygame.K_PAGEDOWN, pygame.K_HOME, pygame.K_END):
            self.help_scroll = max(0, self.help_scroll + {
                pygame.K_UP: -48, pygame.K_DOWN: 48,
                pygame.K_PAGEUP: -260, pygame.K_PAGEDOWN: 260,
                pygame.K_HOME: -99999, pygame.K_END: 99999,
            }[k])
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
            # 纯键盘导航：↑↓ 在菜单项之间移动，Enter/Space 触发当前项
            if k in (pygame.K_UP, pygame.K_DOWN):
                n = len(self.menu_rects)
                if n:
                    step = -1 if k == pygame.K_UP else 1
                    self.sys_menu_index = (self.sys_menu_index + step) % n
                return
            if k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                if self.menu_rects:
                    i = min(self.sys_menu_index, len(self.menu_rects) - 1)
                    _rect, act, arg = self.menu_rects[i]
                    self._sys_menu_activate(act, arg)
                return
            return

        if self.mode == "over":
            if self.campaign_pending:
                # 战役：本关已完成，回车进入下一关
                if k == pygame.K_RETURN or k == pygame.K_SPACE:
                    self._campaign_next_step()
                elif k == pygame.K_ESCAPE:
                    self.campaign_pending = False
                    self.mode = "menu"
                    self._scan_campaigns()
                return
            if k == pygame.K_RETURN:
                self.mode = "menu"
                self._scan_scenarios()
                self._scan_campaigns()
            elif k == pygame.K_ESCAPE:
                self.running = False
            return

        # 作战简报：回车关闭，其余按键忽略
        if self.show_briefing:
            if k == pygame.K_RETURN or k == pygame.K_SPACE or k == pygame.K_ESCAPE:
                self._close_briefing()
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
                self.set_status(tr("ui.st.cancelled"))
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
            # 已有待确认的目标/设施 → 开火/打击；否则在键盘光标处执行左键等价动作
            if self.pending_target is not None or self.pending_facility is not None:
                self.confirm_fire()
            elif self.cursor_visible and st.phase == "player" and not st.over:
                self._activate_at(self.cursor)
        elif action.startswith("weapon_") and action[7:].isdigit():
            idx = int(action[7:]) - 1
            ws = st.selected_unit().usable_weapons() if st.selected_unit() else []
            if idx < len(ws):
                st.selected_weapon = ws[idx].id
        elif k in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
            # 方向键移动键盘地图光标（纯键盘导航，鼠标玩家不受影响）
            self._move_cursor(k)

    def _on_mouse_down(self, e: pygame.event.Event) -> None:
        pos = e.pos
        # 设置面板是模态：无论当前在哪个界面，鼠标事件都优先交给它
        if self.show_settings and self.settings_panel is not None:
            if self.settings_panel.handle_event(e):
                if getattr(self.settings_panel, "close_requested", False):
                    self.settings_panel.close_requested = False
                    self.close_settings()
            return
        if self.mode == "menu":
            if e.button == 1:
                # 导入浮层优先
                if self._import_dialog is not None:
                    if self._import_dialog.handle_event(e):
                        if getattr(self._import_dialog, "close_requested", False):
                            self._import_dialog.close_requested = False
                            self._import_dialog = None
                    return
                # 回放浮层优先
                if self._replay_dialog is not None:
                    if self._replay_dialog.handle_event(e):
                        if getattr(self._replay_dialog, "close_requested", False):
                            self._replay_dialog.close_requested = False
                            self._replay_dialog = None
                    return
                if self._menu_import_btn.collidepoint(pos):
                    self._import_dialog = ImportDialog(
                        self._packages_dir(), on_import=self._import_package)
                    self._sfx("click")
                    return
                if self._menu_replay_btn.collidepoint(pos):
                    self._replay_dialog = ReplayDialog(
                        self._replays_dir(), on_play=self._play_replay)
                    self._sfx("click")
                    return
                # 想定卡片：点击才改变选中（悬停只做视觉提示）
                idx = self._menu_card_at(pos)
                if idx >= 0 and idx != self.menu_index:
                    self.menu_index = idx
                    self._sfx("click")
                # 阵营卡片：同上
                side = self._menu_side_at(pos)
                if side is not None and side is not self.menu_side:
                    self.menu_side = side
                    self._sfx("click")
                if self._menu_start_rect().collidepoint(pos):
                    self.start_game()
                elif self._menu_edit_rect().collidepoint(pos):
                    self.open_editor()
                elif self._menu_tutorial_rect().collidepoint(pos):
                    self.start_tutorial()
                elif self._menu_campaign_rect().collidepoint(pos):
                    self.show_campaign_menu = not self.show_campaign_menu
                elif self._menu_settings_rect().collidepoint(pos):
                    self.open_settings()
                elif self.show_campaign_menu:
                    self._click_campaign_menu(pos)
            return
        if self.mode == "over":
            if e.button == 1:
                if self._debrief_btn.collidepoint(pos):
                    if self.campaign_pending:
                        self._campaign_next_step()
                    else:
                        self.mode = "menu"
                        self._scan_scenarios()
                        self._scan_campaigns()
                elif not self.debrief and pygame.Rect(
                        self.w // 2 - 130, self.h // 2 + 108, 260, 44).collidepoint(pos):
                    if self.campaign_pending:
                        self._campaign_next_step()
                    else:
                        self.mode = "menu"
                        self._scan_scenarios()
                        self._scan_campaigns()
            return

        # 作战简报：点击「开始作战」关闭
        if self.show_briefing:
            if e.button == 1 and self._brief_btn.collidepoint(pos):
                self._close_briefing()
            return

        # 设置面板是模态：所有鼠标事件都交给它处理，不再穿透到下方界面
        if self.show_settings and self.settings_panel is not None:
            if self.settings_panel.handle_event(e):
                if getattr(self.settings_panel, "close_requested", False):
                    self.settings_panel.close_requested = False
                    self.close_settings()
            return

        if self.show_help:
            return
        if self.show_menu:
            if e.button == 1:
                for rect, action, arg in self.menu_rects:
                    if rect.collidepoint(pos):
                        self._sys_menu_activate(action, arg)
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

        # 左键在格子上的全部上下文动作（选单位 / 选目标 / 移动 / 打击设施）
        # 与键盘光标共用 _activate_at，避免两套分叉逻辑。
        self._activate_at(grid)
        self._sync_cursor(grid)
        return

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
                        self.set_status(tr("ui.st.asat_pick_unit"), 4)
                        return
                    w = next((x for x in u.usable_weapons() if x.asat), None)
                    if w is None:
                        self.set_status(tr("ui.st.asat_no_capability_fmt") % (u.name,), 4)
                        return
                    st.selected_weapon = w.id
                    self.pending_target = sat
                    self.pending_facility = None
                    self.tab = "unit"
                    prof = rules.shot_profile(st, u, sat, w)
                    self.set_status(
                        tr("ui.st.asat_engage_fmt") % (
                            sat.name, w.name,
                            f"{prof.pk * 100:.0f}"), 5)
                    return
        # 侧栏行动按钮（雷达 / 干扰 / 筑工事 / 诱饵 / 剖面 / 烟幕 / 待机）
        for b in self.action_buttons:
            if b.handle_click(pos):
                self._sfx("click")
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
        if self.mode == "replay":
            p = self.replay_player
            if p is not None:
                p.update(dt)
                self.state = p.state
                self.state.tick_fx(dt)
                if self.state.sfx_events:
                    for name in self.state.sfx_events[:6]:
                        self._sfx(name)
                    del self.state.sfx_events[:]
            return
        if self.mode != "game" or self.state is None:
            return
        st = self.state
        st.tick_fx(dt)
        # 消费音效信号队列
        if st.sfx_events:
            for name in st.sfx_events[:6]:
                self._sfx(name)
            del st.sfx_events[:]
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
                        self._game_over()
                    self._after_turn()
        self.hover_unit = None
        self.hover_card = False
        if self.mapview and self.mapview.contains(self.mouse) and self.mode == "game":
            g = self.mapview.screen_to_grid(self.mouse)
            self.hover_tile = g
            if g:
                u = self._pick_unit_at(*g)
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
            if self.show_campaign_menu:
                self._draw_campaign_menu(self.screen)
            if self.show_settings and self.settings_panel is not None:
                self.settings_panel.draw(self.screen)
            pygame.display.flip()
            return
        s = self.screen
        s.fill(T.BG)
        self._draw_topbar(s)
        self.mapview.draw(s, self.hover_tile, self.reachable, self.path_preview,
                          self.pending_target)
        # 键盘地图光标：用琥珀色方框标出当前聚焦的格子（仅键盘导航时显示）
        if self.cursor_visible and self.mapview is not None and self.state is not None:
            if self.state.map.in_bounds(*self.cursor):
                r = self.mapview.grid_to_rect(*self.cursor)
                pygame.draw.rect(s, T.AMBER, r.inflate(-2, -2), width=2)
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
        if self.mode == "replay":
            self._draw_replay_bar(s)
        if self.mode == "over":
            self._draw_over(s)
        if self.show_briefing and self.briefing is not None:
            self._brief_btn = pygame.Rect(self.w // 2 - 100, self.h // 2 + 232, 200, 42)
            draw_briefing(s, self.briefing, self.mouse, self._brief_btn)
        pygame.display.flip()

    def _draw_replay_bar(self, s: pygame.Surface) -> None:
        """回放控制条：进度、当前操作、播放/暂停与倍速。"""
        p = self.replay_player
        if p is None:
            return
        w = self.w
        h = 56
        r = pygame.Rect(0, self.h - h, w, h)
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        surf.fill((*T.PANEL_SUNK, 232))
        s.blit(surf, r.topleft)
        pygame.draw.rect(s, T.BORDER, pygame.Rect(0, r.y, w, 1))
        # 进度条
        bar = pygame.Rect(r.x + 16, r.y + 26, w - 320, 8)
        pygame.draw.rect(s, T.PANEL, bar, border_radius=4)
        if bar.width > 0:
            done = pygame.Rect(bar.x, bar.y, int(bar.width * p.progress), bar.height)
            pygame.draw.rect(s, T.CYAN, done, border_radius=4)
        T.draw_text(s, tr("ui.replay_progress_fmt") % (p.index, p.total,), (r.x + 16, r.y + 8),
                    T.FS_SMALL, T.TEXT_DIM, mono=True)
        T.draw_text(s, p.current_label(), (r.x + 150, r.y + 8), T.FS_SMALL, T.TEXT)
        # 右侧：播放状态与倍速
        state_txt = (tr("ui.replay.finished") if p.finished
                     else tr("ui.replay.paused") if p.paused
                     else tr("ui.replay.playing"))
        T.draw_text(s, f"{state_txt}  ×{p.speed:.1f}", (r.right - 16, r.y + 8),
                    T.FS_SMALL, T.CYAN, anchor="topright")
        hint = tr("ui.replay.hint")
        T.draw_text(s, hint, (r.right - 16, r.y + 28), T.FS_TINY, T.TEXT_FAINT,
                    anchor="topright")

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
        surf.fill((*T.mix(T.PANEL, T.GREEN, 0.12), 236))
        s.blit(surf, r.topleft)
        pygame.draw.rect(s, T.GREEN, r, 1, border_radius=4)

        T.draw_text(s, step.title, (r.x + 12, r.y + 7), 12,
                    T.mix(T.GREEN, T.WHITE, 0.4), bold=True)
        T.draw_text(s, self.tutorial.progress_text, (r.right - 12, r.y + 8), 11,
                    T.TEXT_FAINT, anchor="topright")
        if step.goal:
            T.draw_text(s, tr("ui.objective_label") + step.goal, (r.right - 12 - T.text_width(
                self.tutorial.progress_text, 11) - 14, r.y + 8), 11, T.AMBER,
                anchor="topright")
        yy = r.y + 26
        for line in lines[:5]:
            T.draw_text(s, line, (r.x + 12, yy), 11, T.TEXT)
            yy += 16

        # 跳过按钮
        b = pygame.Rect(r.right - 74, r.bottom - 26, 64, 20)
        hover = b.collidepoint(self.mouse)
        pygame.draw.rect(s, T.mix(T.PANEL, T.GREEN, 0.22) if hover else T.PANEL,
                         b, border_radius=3)
        pygame.draw.rect(s, T.BORDER_HI if hover else T.BORDER, b, 1, border_radius=3)
        T.draw_text(s, tr("ui.skip_tutorial"), b.center, 11, T.TEXT_DIM, anchor="center")
        self._tutorial_skip_btn = b

    # -- 系统菜单（存档/读档） --------------------------------------------
    def _draw_system_menu(self, s: pygame.Surface) -> None:
        dim = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 190))
        s.blit(dim, (0, 0))

        slots = self.slot_info or list_slots(ROOT)
        w, row_h = 560, 66
        h = 104 + len(slots) * row_h + 82
        # 面板高度不得超过窗口——槽位多时压缩，而不是溢出屏幕
        h = min(h, self.h - 40)
        r = pygame.Rect((self.w - w) // 2, (self.h - h) // 2, w, h)
        # 暴露面板矩形——布局自检脚本据此验证底部按钮没有顶出面板
        self.menu_panel_rect = r
        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
        T.draw_text(s, tr("ui.system_menu"), (r.x + 22, r.y + 16), T.FS_TITLE + 1, T.TEXT, bold=True)
        T.draw_text(s, tr("ui.quick_save_hint"), (r.right - 22, r.y + 20), T.FS_SMALL,
                    T.TEXT_FAINT, anchor="topright")
        T.hline(s, r.x + 20, r.right - 20, r.y + 48, T.BORDER)

        self.menu_rects = []
        y = r.y + 58
        for info in slots:
            row = pygame.Rect(r.x + 16, y, w - 32, row_h - 8)
            pygame.draw.rect(s, T.PANEL_SUNK, row, border_radius=3)
            pygame.draw.rect(s, T.BORDER, row, 1, border_radius=3)
            T.draw_text(s, tr("ui.slot_fmt") % (info['slot'],), (row.x + 12, row.y + 8), T.FS_LABEL,
                        T.TEXT, bold=True)
            if info.get("exists"):
                ts = time.strftime("%m-%d %H:%M", time.localtime(info.get("mtime", 0)))
                sc_name = os.path.splitext(info.get("scenario", ""))[0]
                T.draw_text(s, tr("ui.turn_line_fmt") % (info.get('turn', 0), sc_name,) +
                                f"{tr('ui.side_blue') if info.get('player') == 'BLUE' else tr('ui.side_red')} · {ts}",
                            (row.x + 12, row.y + 30), T.FS_SMALL, T.TEXT_FAINT)
            else:
                T.draw_text(s, tr("ui.slot_empty"), (row.x + 12, row.y + 30), T.FS_SMALL, T.TEXT_FAINT)

            b_save = pygame.Rect(row.right - 162, row.y + 10, 72, 30)
            b_load = pygame.Rect(row.right - 84, row.y + 10, 72, 30)
            for b, label, act, enabled in (
                    (b_save, tr("ui.sys.save"), "save", True),
                    (b_load, tr("ui.sys.load"), "load", info.get("exists", False))):
                hover = b.collidepoint(self.mouse) and enabled
                if not enabled:
                    _fill = T.PANEL_SUNK
                else:
                    _fill = T.mix(T.PANEL, T.CYAN, 0.30) if hover else T.PANEL_HI
                pygame.draw.rect(s, _fill, b, border_radius=3)
                pygame.draw.rect(s, T.CYAN if (hover and enabled) else T.BORDER, b, 1,
                                 border_radius=3)
                T.draw_text(s, label, b.center, T.FS_BODY,
                            T.TEXT if (hover or not enabled) else T.TEXT_DIM,
                            anchor="center")
                if enabled:
                    self.menu_rects.append((b, act, info["slot"]))
            y += row_h

        # 底部按钮行
        btns = [(tr("ui.sys.resume"), "resume"), (tr("ui.sys.to_menu"), "to_menu"),
                (tr("ui.sys.export_md"), "export_md"), (tr("ui.sys.export_pkg"), "export_pkg"),
                (tr("ui.sys.export_replay"), "export_replay"), (tr("ui.sys.settings"), "settings"),
                (tr("ui.sys.quit"), "quit")]
        gap = 8
        pad = 16
        # 宽度必须把**按钮间隙**算进去，否则最后一个按钮会顶出面板
        bw = (w - pad * 2 - gap * (len(btns) - 1)) // len(btns)
        bh = 36
        by = min(y + 8, r.bottom - bh - 14)
        for i, (label, act) in enumerate(btns):
            b = pygame.Rect(r.x + pad + i * (bw + gap), by, bw, bh)
            hover = b.collidepoint(self.mouse)
            pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL, b, border_radius=3)
            pygame.draw.rect(s, T.BORDER_HI if hover else T.BORDER, b, 1, border_radius=3)
            T.draw_text(s, label, b.center, T.FS_BODY, T.TEXT if hover else T.TEXT_DIM,
                        anchor="center")
            self.menu_rects.append((b, act, None))

        # 键盘焦点框：与 hover 的细边框区分，用更粗的琥珀色
        if self.menu_rects:
            _fi = min(self.sys_menu_index, len(self.menu_rects) - 1)
            pygame.draw.rect(s, T.AMBER, self.menu_rects[_fi][0].inflate(4, 4),
                             2, border_radius=4)

    # -- 顶栏 -------------------------------------------------------------
    def _draw_topbar(self, s: pygame.Surface) -> None:
        st = self.state
        pygame.draw.rect(s, T.PANEL, pygame.Rect(0, 0, self.w, TOP_H))
        T.hline(s, 0, self.w, TOP_H - 1, T.BORDER)

        T.draw_text(s, tr("ui.game_title"), (14, 7), 15, T.TEXT, bold=True)
        T.draw_text(s, st.map.name, (14, 28), 11, T.TEXT_FAINT)
        T.vline(s, 156, 10, TOP_H - 10, T.BORDER)

        # 回合
        x = 172
        T.draw_text(s, tr("ui.turn_label"), (x, 7), 11, T.TEXT_FAINT)
        T.draw_text(s, f"{st.turn_count:02d}", (x, 20), 19, T.TEXT, bold=True, mono=True)
        T.draw_text(s, f"/{st.turn_limit}", (x + 30, 30), 11, T.TEXT_FAINT, mono=True)
        if st.phase == "over":
            phase_txt = tr("ui.hud.phase_over")
        else:
            actor = self.side if st.phase == "player" else self.side.foe
            phase_txt = tr("ui.hud.phase_fmt") % (actor.label,)
        col = T.CYAN if st.phase == "player" else T.AMBER
        T.draw_text(s, phase_txt, (x + 66, 22), 12, col, bold=True)
        if st.phase == "ai":
            T.draw_text(s, tr("ui.simulating"), (x + 66, 8), 11, T.AMBER)
        if self.mode == "replay":
            # 回放模式：顶栏明确标注，别让人以为是在操作对局
            _badge = pygame.Rect(x + 132, 9, 66, 20)
            pygame.draw.rect(s, T.mix(T.PANEL, T.CYAN, 0.22), _badge, border_radius=3)
            pygame.draw.rect(s, T.CYAN, _badge, 1, border_radius=3)
            T.draw_text(s, tr("ui.replay_marker"), _badge.center, 11, T.CYAN, anchor="center")
        T.vline(s, 318, 10, TOP_H - 10, T.BORDER)

        # 胜负点
        x = 334
        T.draw_text(s, tr("ui.vp_label"), (x, 7), 11, T.TEXT_FAINT)
        T.draw_text(s, tr("ui.side_blue_short"), (x, 25), 11, T.side_color(Side.BLUE))
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
        T.draw_text(s, tr("ui.side_red_short"),
                    (x + 146 + T.text_width(str(st.vp[Side.RED]), 15, mono=True) + 4, 25),
                    11, T.side_color(Side.RED))
        T.vline(s, 512, 10, TOP_H - 10, T.BORDER)

        # 兵力
        x = 528
        T.draw_text(s, tr("ui.forces_label"), (x, 7), 11, T.TEXT_FAINT)
        T.draw_text(s, str(len(st.units_of(Side.BLUE))), (x, 21), 14,
                    T.side_color(Side.BLUE), bold=True, mono=True)
        T.draw_text(s, ":", (x + 20, 24), 11, T.TEXT_FAINT)
        T.draw_text(s, str(len(st.units_of(Side.RED))), (x + 30, 21), 14,
                    T.side_color(Side.RED), bold=True, mono=True)
        T.vline(s, 600, 10, TOP_H - 10, T.BORDER)

        # 卫星导航
        x = 616
        T.draw_text(s, tr("ui.sat_nav_label"), (x, 7), 11, T.TEXT_FAINT)
        for i, side in enumerate((Side.BLUE, Side.RED)):
            yy = 24 + i * 12
            v = st.gps[side]
            T.draw_text(s, tr("ui.side_blue_short") if side is Side.BLUE
                    else tr("ui.side_red_short"), (x, yy - 2), 11, T.side_color(side))
            T.bar(s, pygame.Rect(x + 14, yy, 50, 5), v, T.hp_color(v))
            T.draw_text(s, f"{v * 100:.0f}%", (x + 70, yy - 4), 11, T.TEXT_DIM, mono=True)
        T.vline(s, 742, 10, TOP_H - 10, T.BORDER)

        # 战场环境（时段 · 天候）
        x = 758
        env = environment_for(st)
        env_col = {TimeOfDay.DAWN: T.AMBER, TimeOfDay.DAY: T.WHITE,
                   TimeOfDay.DUSK: T.AMBER, TimeOfDay.NIGHT: T.BLUE}.get(
                       env.time_of_day, T.TEXT)
        wcol = {Weather.CLEAR: T.TEXT_DIM, Weather.RAIN: T.BLUE,
                Weather.FOG: T.TEXT_DIM, Weather.SNOW: T.CYAN}.get(
                    env.weather, T.TEXT_DIM)
        T.draw_text(s, tr("ui.env_label"), (x, 7), 11, T.TEXT_FAINT)
        # 风向箭头 + 风力：烟幕会顺风漂移，这一格现在能决定烟往哪边糊
        T.draw_wind_icon(s, (x + 106, 12), env.wind_vector, env.wind_speed)
        T.draw_text(s, str(env.wind_speed), (x + 116, 7), T.FS_TINY,
                    T.TEXT_FAINT if env.wind_speed <= 1 else T.CYAN, mono=True)
        T.draw_text(s, env.time_of_day.label, (x, 21), 12, env_col, bold=True)
        T.draw_text(s, env.clock, (x + 34, 22), 11, T.TEXT_DIM, mono=True)
        T.draw_text(s, env.weather.label, (x + 68, 22), 11, wcol)
        T.draw_text(s, env.describe_effects()[:26], (x, 36), 11, T.TEXT_FAINT)
        T.vline(s, 886, 10, TOP_H - 10, T.BORDER)

        # 在飞弹药
        x = 900
        T.draw_text(s, tr("ui.inflight_ammo"), (x, 7), 11, T.TEXT_FAINT)
        nmiss = str(len(st.missiles))
        T.draw_text(s, nmiss, (x, 21), 14,
                    T.AMBER if st.missiles else T.TEXT_DIM, bold=True, mono=True)
        if st.missiles:
            T.draw_text(s, tr("ui.rounds_unit"), (x + T.text_width(nmiss, 14, mono=True) + 4, 27), 11, T.TEXT_FAINT)

        for b in self.buttons:
            b.update(self.mouse)
            if b is getattr(self, "overlay_btn", None):
                b.label = {"none": tr("ui.hud.overlay_none"),
                           "sensors": tr("ui.hud.overlay_sensors"),
                           "threat": tr("ui.hud.overlay_threat")}[self.mapview.overlay]
            elif b is self.end_turn_btn:
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
        T.panel(s, mm.inflate(10, 10), T.PANEL_SUNK, T.BORDER, radius=4)
        mv.draw_minimap(s, mm)
        T.draw_text(s, tr("ui.battlefield_situation"), (mm.x, mm.y - 15), 11, T.TEXT_FAINT)

        # 事件滚动条
        recent = st.log[-3:]
        if recent:
            h = 14 * len(recent) + 10
            # 回放模式底部有控制条——事件条上移，别被压住
            _dy = 56 if self.mode == "replay" else 0
            r = pygame.Rect(mv.rect.x + 10, mv.rect.bottom - h - 8 - _dy, 470, h)
            surf = pygame.Surface(r.size, pygame.SRCALPHA)
            surf.fill((*T.PANEL_SUNK, 190))
            s.blit(surf, r.topleft)
            pygame.draw.rect(s, T.BORDER, r, 1)
            y = r.y + 5
            for e in recent:
                col = LogView.kind_color(e.kind)
                T.draw_text(s, f"T{e.turn}", (r.x + 6, y), 11, T.TEXT_FAINT, mono=True)
                T.draw_text(s, e.text[:58], (r.x + 32, y), 11, col)
                y += 14

        # 提示条
        if self.status and self.status_t > 0:
            a = min(1.0, self.status_t)
            w = min(640, T.text_width(self.status, 13) + 28)
            r = pygame.Rect(mv.rect.centerx - w // 2, mv.rect.y + 12, w, 30)
            surf = pygame.Surface(r.size, pygame.SRCALPHA)
            surf.fill((*T.mix(T.PANEL, T.CYAN, 0.12), int(230 * a)))
            s.blit(surf, r.topleft)
            pygame.draw.rect(s, T.mix(T.BORDER, T.CYAN, 0.55), r, 1, border_radius=3)
            T.draw_text(s, self.status, r.center, 13, T.TEXT, anchor="center")

    # -- 侧栏 -------------------------------------------------------------
    def _draw_sidebar(self, s: pygame.Surface) -> None:
        self.action_buttons = []
        x = self.w - SIDE_W
        pygame.draw.rect(s, T.PANEL, pygame.Rect(x, TOP_H, SIDE_W, self.h - TOP_H - BOTTOM_H))
        T.vline(s, x, TOP_H, self.h - BOTTOM_H, T.BORDER)

        # 页签
        tabs = [("unit", i18n.tr("tab.unit")),
                ("ops", i18n.tr("tab.situation")),
                ("log", i18n.tr("tab.log"))]
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
            T.draw_text(s, tr("ui.no_unit_selected"), (body.x + 16, y + 22), 14, T.TEXT_DIM)
            T.draw_text(s, tr("ui.hint_select_unit"), (body.x + 16, y + 46), 11, T.TEXT_FAINT)
            T.draw_text(s, tr("ui.hint_tab_cycle"), (body.x + 16, y + 64), 11, T.TEXT_FAINT)
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
        T.draw_text(s, tr("ui.mp_fmt") % (u.mp, u.template.mp,), (r.right - 10, r.y + 9), 11,
                    T.CYAN if u.mp > 0.1 else T.TEXT_FAINT, anchor="topright", mono=True)
        T.draw_text(s, tr("ui.sortie_fmt") % (u.shots_left, u.template.sortie_shots,),
                    (r.right - 10, r.y + 22), 11,
                    T.AMBER if u.shots_left else T.TEXT_FAINT, anchor="topright", mono=True)
        if u.domain.value == "air":
            ml = {"strike": tr("ui.hud.mission_strike"),
                  "cap": tr("ui.hud.mission_cap"),
                  "ferry": tr("ui.hud.mission_ferry")}[u.mission]
            T.draw_text(s, tr("ui.profile_fmt") % (ml,), (r.right - 10, r.y + 35), 11,
                        T.PURPLE if u.mission != "strike" else T.TEXT_FAINT,
                        anchor="topright")
        T.draw_text(s, f"{u.template.unit_class.label} · {u.domain.label} · {u.side.label}",
                    (r.x + 64, r.y + 32), 11, T.TEXT_FAINT)
        T.bar(s, pygame.Rect(r.x + 64, r.y + 49, r.width - 76, 6), u.hp_ratio,
              T.hp_color(u.hp_ratio))
        T.draw_text(s, f"{u.hp:.0f} / {u.template.hp:.0f}", (r.x + 64, r.y + 58), 11,
                    T.TEXT_DIM, mono=True)
        T.draw_text(s, tr("ui.coord_fmt") % (u.x, u.y,), (r.x + 64, r.y + 76), 11,
                    T.TEXT_FAINT, mono=True)
        state_bits = []
        if u.fired:
            state_bits.append(tr("ui.hud.state_fired"))
        elif u.mp > 0.1:
            state_bits.append(tr("ui.hud.state_ready"))
        else:
            state_bits.append(tr("ui.hud.state_waiting"))
        if u.entrench > 0:
            # 罗马数字是等级符号，不翻译；只翻「掩体」这层壳
            state_bits.append(tr("ui.hud.entrench_fmt") % (
                "Ⅰ" if u.entrench == 1 else "Ⅱ",))
        _tile = self.state.map.at(u.x, u.y)
        if _tile is not None and _tile.fort > 0 and _tile.fort_owner is u.side:
            state_bits.append(tr("ui.hud.fort_level_fmt") % (f"Lv{_tile.fort}",))
            # 工事要攒够工时才涨级：进度不显示出来，玩家会以为按钮失效了
            if _tile.fort < 3:
                state_bits.append(tr("ui.hud.fort_build_fmt") % (
                    f"{_tile.fort_build}/{self.state.fort_need(_tile)}",))
        if u.suppressed:
            state_bits.append(tr("ui.hud.state_suppressed"))
        if u.domain is Domain.AIR:
            state_bits.append(tr("ui.hud.fuel_fmt") % (f"{u.fuel * 100:.0f}%",))
        if u.template.datalink:
            state_bits.append(tr("ui.hud.link_up") if u.datalink_on
                              else tr("ui.hud.link_down"))
        if u.template.command_radius > 0 and u.side is self.side:
            subs = self.state.subordinates_of(u)
            state_bits.append(tr("ui.hud.command_count_fmt") % (len(subs),))
        # 补给状态：充裕 / 紧张 / 枯竭（三档，不再是「有/无」两态）
        if u.side is self.side and u.domain is not Domain.AIR:
            if u.supply_dry >= 3:
                state_bits.append(tr("ui.hud.supply_dry"))
            elif u.supply_dry >= 1:
                state_bits.append(tr("ui.hud.supply_low"))
            else:
                state_bits.append(tr("ui.hud.supply_up"))
        col = T.AMBER if u.fired else T.GREEN
        if u.supply_dry >= 3:
            col = T.RED
        elif u.domain is Domain.AIR and u.fuel < 0.35:
            col = T.RED
        # 状态行是单行右对齐的：塞不下就从头丢起（越靠后越次要），
        # 否则德语/法语的长标签会顶到单位图标上，比少显示一条更难看
        while len(state_bits) > 1 and \
                T.text_width(" · ".join(state_bits), 11) > r.width - 24:
            state_bits.pop()
        T.draw_text(s, " · ".join(state_bits), (r.right - 10, r.y + 76), 11,
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
        jammed = False
        if u.template.jam_bands:
            labels = {"radar": tr("enum.jam_band.radar"),
                      "comm": tr("enum.jam_band.comm"),
                      "nav": tr("enum.jam_band.nav")}
            bands = tr("env.join").join(labels.get(b, b) for b in u.template.jam_bands)
            bits.append(tr("ui.hud.ew_jam_fmt") % (bands, f"{u.template.jam_radius:.0f}"))
        # 敌方干扰对本单位的影响
        radar_jam = rules.jam_strength_at(st, (u.x, u.y), u.side, "radar")
        comm_jam = rules.jam_strength_at(st, (u.x, u.y), u.side, "comm")
        if radar_jam > 0.05:
            bits.append(tr("ui.hud.ew_radar_fmt") % (f"{radar_jam * 100:.0f}",))
            jammed = True
        if comm_jam > 0.05:
            bits.append(tr("ui.hud.ew_comm_fmt") % (f"{comm_jam * 100:.0f}",))
            jammed = True
        if not bits:
            return
        # 别用「文本里含某个词」判断颜色——那等于把中文当语法
        col = T.RED if jammed else T.PURPLE
        T.draw_text(s, " · ".join(bits), (r.x + 64, y), 11, col)

    def _draw_weapons(self, s, body, u: Unit, y: int) -> int:
        st = self.state
        T.draw_text(s, tr("ui.weapon_system"), (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        if not u.template.weapons:
            T.draw_text(s, tr("ui.unarmed_support"), (body.x + 4, y), 12, T.TEXT_DIM)
            return y + 26
        for w in u.template.weapons:
            ammo = u.ammo.get(w.id, 0)
            sel = st.selected_weapon == w.id
            row = pygame.Rect(body.x, y, body.width, 38)
            if sel:
                pygame.draw.rect(s, T.button_fill(T.CYAN, True), row, border_radius=3)
                pygame.draw.rect(s, T.CYAN, pygame.Rect(row.x, row.y, 2, row.height))
            elif row.collidepoint(self.mouse):
                pygame.draw.rect(s, T.button_fill(T.CYAN), row, border_radius=3)
            col = T.TEXT if ammo > 0 else T.TEXT_FAINT
            T.draw_text(s, w.name, (row.x + 8, row.y + 4), 12, col, bold=sel)
            info = f"{w.kind.label} · {w.range_min:.1f}-{w.range_max:.1f}格 · Pk {w.pk * 100:.0f}%"
            T.draw_text(s, info, (row.x + 8, row.y + 21), 11, T.TEXT_FAINT)
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
            acts.append((tr("ui.hud.radar_on") if u.radar_on
                         else tr("ui.hud.radar_off"),
                         lambda: self._act_radar(u), u.radar_on))
        if u.template.jam_strength > 0:
            acts.append((tr("ui.hud.jam_on") if u.jam_on
                         else tr("ui.hud.jam_off"),
                         lambda: self._act_jam(u), u.jam_on))
        if u.template.datalink and not u.is_decoy:
            acts.append((tr("ui.hud.link_toggle_on") if u.datalink_on
                         else tr("ui.hud.link_toggle_off"),
                         lambda: self._act_datalink(u), u.datalink_on))
        if u.template.can_fortify and not u.is_decoy:
            tile = st.map.at(u.x, u.y)
            lvl = tile.fort if tile and tile.fort_owner is u.side else 0
            acts.append((tr("ui.hud.build_fort_fmt") % (lvl,),
                         lambda: self._build_fort(u), lvl >= 3))
        if u.decoy_left > 0:
            for kind, label in (("radar", tr("ui.hud.decoy_radar")),
                                ("ir", tr("ui.hud.decoy_ir")),
                                ("visual", tr("ui.hud.decoy_visual"))):
                acts.append((f"{label}", (lambda k=kind: self.begin_decoy(u, k)), False))
        if u.domain.value == "air" and not u.is_decoy:
            label = {"strike": tr("ui.hud.profile_strike"),
                     "cap": tr("ui.hud.profile_cap"),
                     "ferry": tr("ui.hud.profile_ferry")}[u.mission]
            acts.append((label, lambda: self.cycle_mission(u), u.mission == "cap"))
        if any(w.kind.value in ("artillery", "mlrs") for w in u.usable_weapons()):
            acts.append((tr("ui.hud.smoke"), lambda: self.begin_smoke(u), False))
        if u.can_prepare_fire and not u.is_decoy:
            fm = u.fire_mission
            if fm is not None and fm.active:
                acts.append((tr("ui.hud.fire_mission_fmt") % (fm.x, fm.y),
                             lambda: self.set_status(
                    tr("ui.st.fa_state_fmt") % (
                        fm.x, fm.y,
                        tr("ui.st.fa_prepared") if fm.prepared
                        else tr("ui.st.fa_preparing_fmt") % (fm.turns_left,))), True))
            else:
                acts.append((tr("ui.hud.prepare_fire"),
                             lambda: self.begin_prepare_fire(u), False))
        if u.template.command_radius > 0 and not u.is_decoy and u.side is self.side:
            subs = st.subordinates_of(u)
            acts.append((tr("ui.hud.command_fmt") % (len(subs),),
                         lambda: self.begin_command(u), False))
        acts.append((tr("ui.hud.standby"), lambda: self._standby(u), False))

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
            self._record(CMD_FORTIFY, uid=u.uid)
            self.set_status(tr("ui.st.fort_built_fmt") % (u.name, tile.fort), 4)
            self._tutorial_note("fortify")
            return
        # 给出具体原因，便于玩家判断
        tile = st.map.at(u.x, u.y)
        if not u.template.can_fortify:
            self.set_status(tr("ui.st.fort_no_capability_fmt") % (u.name,), 3)
        elif u.mp < 1.0:
            self.set_status(tr("ui.st.fort_mp_short_fmt") % (f"{u.mp:.1f}",), 3)
        elif tile is None or tile.terrain.value == "water":
            self.set_status(tr("ui.st.fort_water"), 3)
        elif tile.fort >= 3 and tile.fort_owner is u.side:
            self.set_status(tr("ui.st.fort_max"), 3)
        else:
            self.set_status(tr("ui.st.fort_invalid"), 3)

    def cycle_mission(self, u: Unit) -> None:
        order = ("strike", "cap", "ferry")
        nxt = order[(order.index(u.mission) + 1) % len(order)]
        if self.state.set_mission(u, nxt):
            self._record(CMD_PROFILE, uid=u.uid, mission=nxt)
            self._tutorial_note("profile")
            label = {"strike": tr("ui.hud.mission_strike"),
                     "cap": tr("ui.hud.mission_cap_long"),
                     "ferry": tr("ui.hud.mission_ferry")}[nxt]
            self.set_status(
                tr("ui.st.profile_changed_fmt") % (
                    u.name, label,
                    tr("ui.st.profile_cap_note") if nxt == "cap"
                    else tr("ui.st.profile_ferry_note") if nxt == "ferry" else ""), 4)

    def begin_smoke(self, u: Unit) -> None:
        """进入烟幕投放模式：点击射程内的地块即施放烟幕。"""
        u2 = self.state.selected_unit()
        if u2 is None:
            return
        self.smoke_mode = True
        self.smoke_weapon = next((w for w in u2.usable_weapons()
                                  if w.kind.value in ("artillery", "mlrs")), None)
        self.set_status(tr("ui.st.smoke_pick"), 8)

    def begin_prepare_fire(self, u: Unit) -> None:
        """进入火力准备模式：点击射程内且有情报的格子登记射界。"""
        u2 = self.state.selected_unit()
        if u2 is None:
            return
        self.prepare_mode = True
        self.set_status(tr("ui.st.fa_pick"), 8)

    # -- 指挥指令 ---------------------------------------------------------
    def begin_command(self, u: Unit) -> None:
        """进入指挥模式：选择指令类型后再选目标。"""
        subs = self.state.subordinates_of(u)
        if not subs:
            self.set_status(tr("ui.st.command_no_sub_fmt") % (u.name,), 3)
            return
        self.command_mode = True
        self.command_issuer = u
        self.set_status(tr("ui.st.command_pick"), 8)

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
            self.set_status(tr("ui.st.command_no_unit"), 3)
            return
        ok, msg = st.issue_order(u, "focus_fire", target=tgt)
        if ok:
            self._record(CMD_ORDER, commander=u.uid, order="focus_fire",
                         target=tgt.uid)
            self._tutorial_note("order")
            self.set_status(msg, 4)
        else:
            self.set_status(tr("ui.st.command_failed_fmt") % (msg,), 4)

    def issue_hold_order(self) -> None:
        """下达固守指令：下级单位不再前出。"""
        u = self.state.selected_unit()
        if u is None:
            return
        ok, msg = self.state.issue_order(u, "hold")
        self.set_status(msg if ok else tr("ui.st.command_failed_fmt") % (msg,), 4)

    def issue_priority_order(self, cls: str) -> None:
        """下达优先目标指令：下级优先打击某类目标。"""
        u = self.state.selected_unit()
        if u is None:
            return
        ok, msg = self.state.issue_order(u, "priority", priority_class=cls)
        self.set_status(msg if ok else tr("ui.st.command_failed_fmt") % (msg,), 4)

    def _place_prepare_fire(self, pos: tuple[int, int]) -> None:
        u = self.state.selected_unit()
        self.prepare_mode = False
        if not u:
            return
        ok, msg = self.state.register_fire_mission(u, pos)
        if ok:
            self._record(CMD_PREPARE, uid=u.uid, x=pos[0], y=pos[1])
            self._tutorial_note("prepare")
            self.set_status(tr("ui.st.fa_registered_fmt") % (pos[0], pos[1]), 5)
        else:
            self.set_status(tr("ui.st.fa_failed_fmt") % (msg,), 4)

    def _place_smoke(self, pos: tuple[int, int]) -> None:
        u = self.state.selected_unit()
        self.smoke_mode = False
        if not u or not self.smoke_weapon:
            return
        if self.state.fire_smoke(u, pos, self.smoke_weapon):
            self._record(CMD_SMOKE, uid=u.uid, weapon=self.smoke_weapon.id,
                         x=pos[0], y=pos[1])
            self._tutorial_note("smoke")
            self.set_status(tr("ui.st.smoke_done"), 4)
        else:
            self.set_status(tr("ui.st.smoke_invalid"), 3)

    def begin_decoy(self, u: Unit, kind: str = "radar") -> None:
        """进入诱饵投放模式：下一次点击地图即在该格生成假目标。"""
        if u.decoy_left <= 0:
            return
        self.decoy_mode = True
        self.decoy_kind = kind
        label = rules.DECOY_LABELS.get(kind, kind)
        self.set_status(tr("ui.st.decoy_pick_fmt") % (label,), 8)

    def _place_decoy(self, pos: tuple[int, int]) -> None:
        u = self.state.selected_unit()
        self.decoy_mode = False
        if not u:
            return
        if self.state.deploy_decoy(u, pos, getattr(self, "decoy_kind", "radar")):
            self._record(CMD_DECOY, uid=u.uid, kind=getattr(self, "decoy_kind", "radar"),
                         x=pos[0], y=pos[1])
            self._tutorial_note("decoy")
            self.set_status(tr("ui.st.decoy_done"), 4)
        else:
            self.set_status(tr("ui.st.decoy_invalid"), 3)

    def _standby(self, u: Unit) -> None:
        """放弃本单位剩余机动点。"""
        u.mp = 0.0
        self.set_status(tr("ui.st.standby_fmt") % (u.name,))
        self._record(CMD_HOLD, uid=u.uid)
        self.select_unit(None)

    # -- 命令记录（回放） --------------------------------------------------
    def _record(self, cmd_kind: str, **args) -> None:
        """记录一条玩家命令。

        命令流 + 对局种子 = 可完整重演的一局。记录的是**语义化**的
        操作（「3 号单位移到 (10,20)」），不是鼠标坐标——回放不该
        依赖窗口大小与分辨率。

        第一个参数刻意不叫 ``kind``——``args`` 里可能也有叫 ``kind``
        的键（比如诱饵类型 radar/ir/visual），重名会撞车。
        """
        if self.cmd_log is None or self.state is None:
            return
        self.cmd_log.record(cmd_kind, turn=self.state.turn_count, **args)

    def _act_radar(self, u: Unit) -> None:
        self.state.toggle_radar(u)
        self._record(CMD_RADAR, uid=u.uid)
        self._tutorial_note("radar")

    def _act_jam(self, u: Unit) -> None:
        self.state.toggle_jam(u)
        self._record(CMD_JAM, uid=u.uid)
        self._tutorial_note("jam")

    def _act_datalink(self, u: Unit) -> None:
        self.state.toggle_datalink(u)
        self._record(CMD_DATALINK, uid=u.uid)
        self._tutorial_note("datalink")

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
            T.draw_text(s, tr("ui.ammo_salvo_fmt") % (u.ammo.get(w.id, 0),), (r.x + 10, y), 11, T.TEXT_FAINT)
            return y + 18
        T.draw_text(s, tr("ui.salvo"), (r.x + 10, y + 2), 11, T.TEXT_FAINT)
        x = r.x + 40
        cur = max(opts[0], min(self.state.salvo_setting, opts[-1]))
        self.state.salvo_setting = cur
        for n in opts:
            rr = pygame.Rect(x, y - 2, 34, 18)
            active = n == cur
            hover = rr.collidepoint(self.mouse)
            pygame.draw.rect(s, T.mix(T.PANEL_SUNK, T.CYAN, 0.28) if active
                          else (T.PANEL_HI if hover else T.PANEL_SUNK),
                             rr, border_radius=3)
            pygame.draw.rect(s, T.CYAN if active else T.BORDER, rr, 1, border_radius=3)
            T.draw_text(s, f"{n}", rr.center, 11, T.WHITE if active else T.TEXT_DIM,
                        bold=active, anchor="center", mono=True)
            self.salvo_rects.append((rr, n))
            x += 38
        comb = 1.0 - (1.0 - base_pk) ** cur
        T.draw_text(s, tr("ui.salvo_stats_fmt") % (cur, comb * 100,), (x + 4, y + 2), 11,
                    T.AMBER if cur > 1 else T.TEXT_FAINT)
        T.draw_text(s, tr("ui.ammo_fmt") % (u.ammo.get(w.id, 0),), (r.right - 10, y + 2), 11,
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
        T.panel(s, r, T.button_fill(T.RED), T.mix(T.BORDER, T.RED, 0.55), radius=4)
        T.draw_text(s, tr("ui.engage_target"), (r.x + 10, r.y + 8), 11, T.RED, bold=True)
        T.draw_text(s, t.name, (r.x + 10, r.y + 24), 13, T.TEXT, bold=True)
        T.draw_text(s, tr("ui.st.inspect_distance_fmt") % (
                        t.template.unit_class.label, lvl.label,
                        f"{prof.distance if prof else rules.dist_units(u, t):.1f}"),
                    (r.x + 10, r.y + 42), 11, T.TEXT_FAINT)
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
                T.draw_text(s, " ".join(parts)[:52] or tr("ui.st.no_modifiers"), (r.x + 10, yy), 11, T.TEXT_FAINT)
                yy += 14
                ic = " · 拦截风险 %.0f%%" % (prof.intercept_pk * 100) if prof.intercept_pk > 0.01 else ""
                T.draw_text(s, tr("ui.damage_fmt") % (prof.damage, ic,), (r.x + 10, yy), 11,
                            T.AMBER if prof.intercept_pk > 0.2 else T.TEXT_DIM)
                if prof.damage_factors:
                    # 毁伤修正：这一发「为什么打得疼/不疼」，一眼能看出来。
                    # 因子可能有好几项（破甲 × 交叉火力 …），标出**影响最大**的那项，
                    # 否则先入为主的键会掩盖真正起作用的机制。
                    chan = max(prof.damage_factors,
                               key=lambda k: abs(prof.damage_factors[k] - 1.0))
                    key = _COUNTER_TR.get(chan)
                    name = tr(key, chan) if key else chan
                    T.draw_text(s, f"×{prof.damage_mult:.2f} {name}", (r.right - 10, yy), 11,
                                T.GREEN if prof.damage_mult > 1.0 else T.RED,
                                anchor="topright")
                if prof.cue_source:
                    T.draw_text(s, tr("ui.cue_fmt") % (prof.cue_source,), (r.x + 10, yy + 12), 11,
                                T.PURPLE)
                yy += 26
                yy = self._draw_salvo_row(s, r, u, w, yy, prof.pk)
            else:
                T.draw_text(s, prof.reason, (r.x + 10, yy), 11, T.RED)

        if self.pending_target is t and ok:
            btn = pygame.Rect(r.x + 10, r.bottom - 30, r.width - 20, 24)
            hover = btn.collidepoint(self.mouse)
            pygame.draw.rect(s, T.button_fill(T.RED, hover), btn, border_radius=3)
            pygame.draw.rect(s, T.RED, btn, 1, border_radius=3)
            T.draw_text(s, tr("ui.confirm_fire"), btn.center, 12, T.mix(T.RED, T.WHITE, 0.6),
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
            out.append((tr("ui.threat.intercept"),
                        tr("ui.threat.units_fmt") % (f"{threat.intercept_pk * 100:.0f}",
                                                     len(threat.interceptors)), col))
            for name in threat.interceptors[:2]:
                out.append(("　└", name[:30], T.TEXT_FAINT))
        else:
            out.append((tr("ui.threat.intercept"), tr("ui.threat.no_cover"), T.GREEN))

        # 防空网分工：谁负责哪一段防区
        if threat.adn_roles:
            roles = tr("env.join").join(sorted({r for _, r, _ in threat.adn_roles}))
            out.append((tr("ui.threat.adn"),
                        tr("ui.threat.adn_fmt") % (roles, threat.adn_channels), T.BLUE))
        if threat.reply_units:
            col = T.RED if threat.enemy_fire_control else T.AMBER
            out.append((tr("ui.threat.reply"),
                        tr("ui.threat.reply_fmt") % (len(threat.reply_units),
                                                     f"{threat.reply_pk * 100:.0f}"), col))
            for name in threat.reply_weapons[:1]:
                out.append(("　└", name[:30], T.TEXT_FAINT))
        else:
            out.append((tr("ui.threat.reply"), tr("ui.threat.no_reply"), T.GREEN))

        fc = (tr("ui.threat.fc_tracked") if threat.enemy_fire_control
              else tr("ui.threat.fc_untracked"))
        out.append((tr("ui.threat.fc"), fc,
                    T.RED if threat.enemy_fire_control else T.TEXT_DIM))
        for b in threat.blockers[:2]:
            out.append((tr("ui.threat.block"), b[:32], T.AMBER))
        return out

    def _draw_counter_threat(self, s, panel: pygame.Rect, threat, u: Unit, t: Unit,
                             w) -> None:
        """绘制「对手视角」区块：解释这一发为什么打不到。"""
        th_h = self._threat_panel_height(threat)
        r = pygame.Rect(panel.x, panel.bottom - th_h, panel.width, th_h)
        pygame.draw.rect(s, T.PANEL_SUNK, r, border_radius=3)
        pygame.draw.rect(s, T.BORDER_HI, r, 1, border_radius=3)
        T.draw_text(s, tr("ui.counter_view_title"), (r.x + 10, r.y + 6), 11,
                        T.mix(T.BLUE, T.CYAN, 0.4), bold=True)
        T.draw_text(s, tr("ui.counter_view_sub"), (r.right - 10, r.y + 7), 11, T.TEXT_FAINT,
                    anchor="topright")

        yy = r.y + 22
        for label, text, col in self._threat_lines(threat):
            T.draw_text(s, label, (r.x + 10, yy), 11, T.TEXT_FAINT)
            T.draw_text(s, text, (r.x + 54, yy), 11, col)
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
        T.panel(s, r, T.button_fill(T.AMBER), T.mix(T.BORDER, T.AMBER, 0.55), radius=4)
        T.draw_text(s, tr("ui.facility_strike"), (r.x + 10, r.y + 8), 11, T.AMBER, bold=True)
        if tile is None:
            return r.bottom + 8
        T.draw_text(s, f"{tile.facility.label} ({tile.x},{tile.y})", (r.x + 10, r.y + 24), 13,
                    T.TEXT, bold=True)
        T.draw_text(s, tr("ui.structure_fmt") % (tile.facility_hp_ratio * 100,) +
                        f"{tr('ui.st.facility_operational') if tile.operational else tr('ui.st.facility_disabled')}",
                    (r.x + 10, r.y + 42), 11,
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
            T.draw_text(s, " ".join(parts)[:52] or tr("ui.st.no_modifiers"), (r.x + 10, yy), 11, T.TEXT_FAINT)
            yy += 14
            ic = f" · 拦截风险 {prof.intercept_pk * 100:.0f}%" if prof.intercept_pk > 0.01 else ""
            T.draw_text(s, tr("ui.structure_damage_fmt") % (prof.damage, ic,), (r.x + 10, yy), 11,
                        T.AMBER if prof.intercept_pk > 0.2 else T.TEXT_DIM)
            yy += 16
            self._draw_salvo_row(s, r, u, w, yy, prof.pk)
        elif prof is not None:
            T.draw_text(s, prof.reason, (r.x + 10, r.y + 68), 11, T.RED)

        if ok:
            btn = pygame.Rect(r.x + 10, r.bottom - 30, r.width - 20, 24)
            hover = btn.collidepoint(self.mouse)
            pygame.draw.rect(s, T.button_fill(T.AMBER, hover), btn, border_radius=3)
            pygame.draw.rect(s, T.AMBER, btn, 1, border_radius=3)
            T.draw_text(s, tr("ui.confirm_strike"), btn.center, 12, T.mix(T.AMBER, T.WHITE, 0.5),
                        bold=True, anchor="center")
            self._fire_btn = btn
        return r.bottom + 8

    def _draw_unit_notes(self, s, body, u: Unit, y: int) -> None:
        sen = tr("env.join").join(
            f"{sc.kind.label} {sc.range_max:.1f}" for sc in u.template.sensors) or tr("common.none")
        rows = [(tr("ui.note.sensors"), sen)]
        if u.template.stealth > 0:
            rows.append((tr("ui.note.stealth"), f"{u.template.stealth * 100:.0f}%"))
        if u.template.armor > 0:
            rows.append((tr("ui.note.armor"), f"{u.template.armor * 100:.0f}%"))
        if u.template.interceptors > 0:
            rows.append((tr("ui.note.intercept"),
                         tr("ui.note.intercept_fmt") % (
                             u.template.interceptors,
                             f"{u.template.intercept_pk * 100:.0f}")))
        if u.template.jam_radius > 0:
            rows.append((tr("ui.note.jam"),
                         tr("ui.note.jam_fmt") % (
                             f"{u.template.jam_radius:.0f}",
                             f"{u.template.jam_strength * 100:.0f}")))
        if u.template.amphibious:
            rows.append((tr("ui.note.deploy"), tr("ui.note.amphib")))
        notes = T.wrap_text(u.template.notes, 10, body.width - 20)[:3] if u.template.notes else []
        h = 26 + len(rows) * 15 + (len(notes) * 13 + 6 if notes else 0) + 10
        h = min(h, body.bottom - y)
        if h < 40:
            return
        r = pygame.Rect(body.x, y, body.width, h)
        T.panel(s, r, T.PANEL_SUNK, T.BORDER, radius=4)
        T.draw_text(s, tr("ui.platform_info"), (r.x + 10, r.y + 8), 11, T.TEXT_FAINT, bold=True)
        yy = r.y + 24
        for k, v in rows:
            T.draw_text(s, k, (r.x + 10, yy), 11, T.TEXT_FAINT)
            T.draw_text(s, v, (r.x + 74, yy), 11, T.TEXT_DIM)
            yy += 15
        if notes:
            yy += 3
            for line in notes:
                T.draw_text(s, line, (r.x + 10, yy), 11, T.mix(T.GREEN, T.TEXT_DIM, 0.55))
                yy += 13

    # -- 态势页 -----------------------------------------------------------
    def _draw_ops_tab(self, s: pygame.Surface, body: pygame.Rect) -> None:
        st = self.state
        y = body.y
        # 目标点
        T.draw_text(s, tr("ui.contest_objective"), (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        for t in st.map.objectives:
            r = pygame.Rect(body.x, y, body.width, 26)
            tile = st.map.at(t.x, t.y)
            own = tile.owner if tile else Side.NEUTRAL
            col = T.side_color(own) if own is not Side.NEUTRAL else T.AMBER
            pygame.draw.rect(s, col, pygame.Rect(r.x, r.y + 4, 3, 18))
            T.draw_text(s, f"{t.name}", (r.x + 10, r.y + 5), 12, T.TEXT)
            T.draw_text(s, f"({t.x},{t.y})", (r.x + 10 + T.text_width(t.name, 12) + 6, r.y + 7),
                        11, T.TEXT_FAINT, mono=True)
            owner_txt = own.label if own is not Side.NEUTRAL else tr("ui.sit.contested")
            T.draw_text(s, tr("ui.objective_value_fmt") % (owner_txt, t.value,), (r.right, r.y + 5), 11,
                        col, anchor="topright")
            y += 27
        y += 8

        # 兵力对比
        T.draw_text(s, tr("ui.force_composition"), (body.x, y), 11, T.TEXT_FAINT, bold=True)
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
            T.draw_text(s, tr("ui.inflight_ammo"), (body.x, y), 11, T.TEXT_FAINT, bold=True)
            y += 18
            for m in st.missiles[:6]:
                col = T.side_color(m.attacker_side)
                T.draw_text(s, f"{m.label}", (body.x, y), 11, col)
                T.draw_text(s, tr("ui.eta_turns") % (m.eta,), (body.right, y), 11, T.AMBER,
                            anchor="topright", mono=True)
                y += 16
            y += 8

        # 卫星与电子战
        self._sat_rects = []
        T.draw_text(s, tr("ui.space_ew"), (body.x, y), 11, T.TEXT_FAINT, bold=True)
        y += 18
        for side in (Side.BLUE, Side.RED):
            sats = st.satellites(side)
            alive = [u for u in sats if u.alive]
            ew = [u for u in st.units_of(side) if u.template.jam_strength > 0 and u.jam_on]
            asat = st.asat_capable(side)
            T.draw_text(s, side.label, (body.x, y), 11, T.side_color(side), bold=True)
            T.draw_text(s, tr("ui.gps_jammer_fmt") % (st.gps[side] * 100, len(ew),) +
                            tr("ui.st.asat_count_fmt") % (len(asat),), (body.x + 44, y), 11, T.TEXT_DIM)
            y += 16
            if not alive:
                T.draw_text(s, tr("ui.no_sat_asset"), (body.x + 8, y), 11, T.RED)
                y += 14
            for sat in alive:
                row = pygame.Rect(body.x, y - 1, body.width, 15)
                if sat.side is not st.player_side:
                    hov = row.collidepoint(self.mouse)
                    if hov:
                        pygame.draw.rect(s, T.button_fill(T.RED), row)
                    T.draw_text(s, f"  {sat.name}", (body.x + 8, y), 11,
                                T.side_color(sat.side) if hov else T.TEXT_DIM)
                    self._sat_rects.append((row, sat))
                else:
                    T.draw_text(s, f"  {sat.name}", (body.x + 8, y), 11, T.TEXT_DIM)
                T.bar(s, pygame.Rect(body.x + 150, y + 3, 90, 5), sat.hp_ratio,
                      T.hp_color(sat.hp_ratio))
                T.draw_text(s, f"{sat.hp:.0f}", (body.x + 246, y - 1), 11, T.TEXT_FAINT, mono=True)
                if st.satellite_overhead(sat):
                    T.draw_text(s, tr("ui.overhead_now"), (body.x + 280, y), 11, T.GREEN, bold=True)
                else:
                    T.draw_text(s, tr("ui.off_orbit"), (body.x + 280, y), 11, T.TEXT_FAINT)
                y += 15
        T.draw_text(s, tr("ui.sat_hint"), (body.x + 8, y), 11, T.CYAN)
        y += 16

        # 胜利条件
        y += 6
        if y < body.bottom - 60:
            T.draw_text(s, tr("ui.victory_condition"), (body.x, y), 11, T.TEXT_FAINT, bold=True)
            y += 16
            for line in T.wrap_text(
                    tr("ui.sit.victory_fmt") % (st.turn_limit,),
                    10, body.width)[:4]:
                T.draw_text(s, line, (body.x, y), 11, T.TEXT_DIM)
                y += 14

        # 战术提示
        tips = self._tactical_tips()
        if tips and y < body.bottom - 50:
            y += 8
            T.draw_text(s, tr("ui.situation_analysis"), (body.x, y), 11, T.CYAN, bold=True)
            y += 18
            for tip in tips:
                if y > body.bottom - 16:
                    break
                for line in T.wrap_text("· " + tip, 10, body.width)[:2]:
                    T.draw_text(s, line, (body.x, y), 11, T.TEXT_DIM)
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
            names = tr("env.join").join(u.template.short for u in sams[:2])
            tips.append(tr("ui.tip.sam_fmt") % (names,))
        if st.gps[self.side] < 0.85:
            tips.append(tr("ui.tip.gps_fmt") % (f"{st.gps[self.side] * 100:.0f}",))
        ew = [u for u in st.enemies_of(self.side)
              if st.known_level(self.side, u).level >= Detection.CLASSIFIED.level
              and u.template.jam_strength > 0]
        if ew:
            tips.append(tr("ui.tip.ew"))
        if st.missiles:
            mine = [m for m in st.missiles if m.attacker_side is self.side]
            if mine:
                tips.append(tr("ui.tip.inflight_fmt") % (len(mine),))
        if not tips:
            tips.append(tr("ui.tip.default"))
        return tips[:3]

    # -- 底栏 -------------------------------------------------------------
    def _draw_bottom(self, s: pygame.Surface) -> None:
        y = self.h - BOTTOM_H
        pygame.draw.rect(s, T.PANEL, pygame.Rect(0, y, self.w, BOTTOM_H))
        T.hline(s, 0, self.w, y, T.BORDER)
        hint = tr("ui.hud.hint_bar")
        T.draw_text(s, hint, (12, y + 6), 11, T.TEXT_FAINT)

        if self.hover_tile:
            t = self.state.map.at(*self.hover_tile)
            if t:
                parts = [f"({t.x},{t.y})", t.terrain.label,
                         tr("ui.hud.elevation_fmt") % (t.elevation,)]
                if t.facility is not Facility.NONE:
                    parts.append(t.facility.label)
                if t.objective:
                    parts.append(f"◆ {t.objective_name}")
                if t.smoke:
                    parts.append(tr("ui.hud.smoke"))
                T.draw_text(s, " · ".join(parts), (self.w - SIDE_W - 12, y + 6), 11,
                            T.TEXT_DIM, anchor="topright")

    # -- 帮助 -------------------------------------------------------------
    #: 帮助面板内容。这里只存**词条键**——正文在 data/lang/*_ui.json 里，
    #: 社区补一种语言不必改代码。
    HELP_LEFT = [
        ("h", "ui.help.recon_t"),
        ("p", "ui.help.recon_b"),
        ("h", "ui.help.cue_t"),
        ("p", "ui.help.cue_b"),
        ("h", "ui.help.ew_t"),
        ("p", "ui.help.ew_b"),
        ("h", "ui.help.inf_t"),
        ("p", "ui.help.inf_b"),
        ("h", "ui.help.air_t"),
        ("p", "ui.help.air_b"),
        ("h", "ui.help.asat_t"),
        ("p", "ui.help.asat_b"),
    ]
    HELP_RIGHT = [
        ("h", "ui.help.ctrl_t"),
        ("p", "ui.help.ctrl1"),
        ("p", "ui.help.ctrl2"),
        ("p", "ui.help.ctrl3"),
        ("p", "ui.help.ctrl4"),
        ("p", "ui.help.ctrl5"),
        ("p", "ui.help.ctrl6"),
        ("p", "ui.help.ctrl7"),
        ("p", "ui.help.ctrl8"),
        ("h", "ui.help.salvo_t"),
        ("p", "ui.help.salvo_b"),
        ("h", "ui.help.save_t"),
        ("p", "ui.help.save_b"),
        ("h", "ui.help.edit_t"),
        ("p", "ui.help.edit_b"),
    ]

    def _layout_help_column(self, items, width: int) -> list[tuple[str, int, tuple, int]]:
        out: list[tuple[str, int, tuple, int]] = []
        for kind, key in items:
            if kind == "h":
                out.append((tr(key), 12, T.CYAN, 22))
            else:
                for line in T.wrap_text(tr(key), 11, width):
                    out.append((line, 11, T.TEXT_DIM, 17))
                out.append(("", 10, T.TEXT_DIM, 2))
        return out

    def _draw_help(self, s: pygame.Surface) -> None:
        """规则与操作帮助。

        面板高度按内容算，但**上限是窗口高度**——拉丁文比中文长得多，
        同一段话排出来可能高出一截。超出部分靠滚轮 / 方向键 / PageUp
        滚动查看，而不是把面板顶出屏幕。
        """
        w = min(900, self.w - 80)
        colw = (w - 76) // 2
        left = self._layout_help_column(self.HELP_LEFT, colw)
        right = self._layout_help_column(self.HELP_RIGHT, colw)
        content_h = max(sum(i[3] for i in left), sum(i[3] for i in right))
        header_h, footer_h = 54, 34
        h = min(self.h - 60, content_h + header_h + footer_h + 16)
        r = pygame.Rect((self.w - w) // 2, (self.h - h) // 2, w, h)
        surf = pygame.Surface(r.size, pygame.SRCALPHA)
        surf.fill((8, 11, 16, 248))
        s.blit(surf, r.topleft)
        pygame.draw.rect(s, T.BORDER_HI, r, 1, border_radius=4)
        T.draw_text(s, tr("ui.rules_and_controls"), (r.x + 24, r.y + 16), 17, T.TEXT, bold=True)
        T.hline(s, r.x + 24, r.right - 24, r.y + 44, T.BORDER)

        view_h = h - header_h - footer_h
        max_scroll = max(0, content_h + 10 - view_h)
        self.help_scroll = max(0, min(self.help_scroll, max_scroll))

        old_clip = s.get_clip()
        s.set_clip(pygame.Rect(r.x + 12, r.y + header_h, r.width - 24, view_h))
        for items, cx in ((left, r.x + 24), (right, r.x + 52 + colw)):
            yy = r.y + 58 - self.help_scroll
            for text, size, color, dy in items:
                if text:
                    T.draw_text(s, text, (cx, yy), size, color, bold=size >= 12)
                yy += dy
        s.set_clip(old_clip)

        # 滚动条：只在真的装不下时出现
        if max_scroll > 0:
            track = pygame.Rect(r.right - 10, r.y + header_h, 3, view_h)
            pygame.draw.rect(s, T.PANEL_SUNK, track, border_radius=2)
            span = max(18, int(view_h * view_h / (content_h + 10)))
            top = track.y + int((view_h - span) * self.help_scroll / max_scroll)
            pygame.draw.rect(s, T.CYAN, pygame.Rect(track.x, top, track.w, span),
                             border_radius=2)
        # 页脚两段分开画：拼成一行居中会在窄面板上互相压字
        T.draw_text(s, tr("ui.press_f1_close"), (r.centerx, r.bottom - 22), 11,
                    T.TEXT_FAINT, anchor="center")
        if max_scroll > 0:
            T.draw_text(s, tr("ui.help.scroll_hint"), (r.x + 24, r.bottom - 22), 11,
                        T.TEXT_FAINT)

    # -- 结算 -------------------------------------------------------------
    def _draw_over(self, s: pygame.Surface) -> None:
        st = self.state
        surf = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        surf.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 200))
        s.blit(surf, (0, 0))
        if self.debrief is not None:
            self._debrief_btn = pygame.Rect(self.w // 2 - 130, self.h // 2 + 218, 260, 44)
            draw_debrief(s, self.debrief, self.mouse, self._debrief_btn)
            return
        r = pygame.Rect(self.w // 2 - 260, self.h // 2 - 190, 520, 380)
        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=6)
        win = st.winner is self.side
        if st.winner is None:
            title, col = tr("ui.end.draw"), T.AMBER
        elif win:
            title, col = tr("ui.end.win"), T.GREEN
        else:
            title, col = tr("ui.end.lose"), T.RED
        T.draw_text(s, title, (r.centerx, r.y + 34), 38, col, bold=True, anchor="center")
        for line in T.wrap_text(st.win_reason, 13, r.width - 60)[:3]:
            T.draw_text(s, line, (r.centerx, r.y + 92), 13, T.TEXT, anchor="center")

        y = r.y + 168
        pair = tr("ui.end.pair_fmt")
        stats = [
            (tr("ui.end.turn"), f"{min(st.turn_count, st.turn_limit)} / {st.turn_limit}"),
            (tr("ui.end.vp"), pair % (st.vp[Side.BLUE], st.vp[Side.RED])),
            (tr("ui.end.casualties"), pair % (st.casualties[Side.BLUE],
                                              st.casualties[Side.RED])),
            (tr("ui.end.forces"), pair % (len(st.units_of(Side.BLUE)),
                                          len(st.units_of(Side.RED)))),
        ]
        for k, v in stats:
            T.draw_text(s, k, (r.x + 120, y), 13, T.TEXT_FAINT)
            T.draw_text(s, v, (r.right - 120, y), 13, T.TEXT, anchor="topright", mono=True)
            y += 26

        # 战役进度
        prog = self.campaign_progress
        if prog is not None and self.campaign is not None:
            camp = self.campaign
            T.draw_text(s, tr("ui.campaign_step_fmt") % (camp.name, min(prog.step_index, camp.total_steps),) +
                           tr("ui.st.campaign_levels_suffix_fmt") % (camp.total_steps,),
                        (r.centerx, y + 4), 12, T.PURPLE, anchor="center")
            T.draw_text(s, tr("ui.vp_total_fmt") % (prog.blue_vp, prog.red_vp,),
                        (r.centerx, y + 24), 12, T.TEXT, anchor="center")
            y += 44

        btn = pygame.Rect(self.w // 2 - 130, self.h // 2 + 108, 260, 44)
        hover = btn.collidepoint(self.mouse)
        pygame.draw.rect(s, T.button_fill(T.CYAN, hover), btn, border_radius=4)
        pygame.draw.rect(s, T.CYAN, btn, 1, border_radius=4)
        if self.campaign_pending:
            T.draw_text(s, tr("ui.next_stage"), btn.center, 14, T.WHITE, bold=True,
                        anchor="center")
        else:
            T.draw_text(s, tr("ui.back_to_scenarios"), btn.center, 14, T.WHITE, bold=True,
                        anchor="center")

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

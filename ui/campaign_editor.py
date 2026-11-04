"""战役编辑器：在可视化界面里编排多关卡战役。

设计目标
--------
战役原先只能手写 JSON。战役编辑器让**不写代码的人也能做出战役**：

* **关卡列表**：增删、上下移动、复制关卡；
* **每关配置**：选择地图、标题、简报、任务目标、难度、继承策略；
* **战役属性**：名称、简介、作者、难度；
* **难度曲线**：一眼看出整场战役的难度起伏（列表右侧的曲线图）；
* **即时校验**：地图是否存在、关卡是否有重复，避免发布后才发现问题。

与地图编辑器共用同一套布局与交互约定（左侧列表 + 右侧表单），
因此两个编辑器的手感一致。
"""
from __future__ import annotations

import json
import os

import pygame

from core.campaign import (CARRYOVER_ALL, CARRYOVER_LABELS, CARRYOVER_NONE,
                           CARRYOVER_VETERANS, DIFFICULTY_LABELS, CampaignDef,
                           CampaignError, CampaignStep, load_campaign)
from core.i18n import tr
from core import paths as _paths

from . import theme as T
from .editor import TextInput

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_DIR = os.path.join(ROOT, "data", "maps")
CAMPAIGN_DIR = os.path.join(ROOT, "data", "campaigns")

CARRYOVER_ORDER = [CARRYOVER_ALL, CARRYOVER_VETERANS, CARRYOVER_NONE]


def _map_choices() -> list[tuple[str, str]]:
    """可选地图列表：(显示名, 地图 id)。"""
    out: list[tuple[str, str]] = []
    if not os.path.isdir(MAP_DIR):
        return out
    for fn in sorted(os.listdir(MAP_DIR)):
        if not fn.endswith(".json"):
            continue
        mid = fn[:-5]
        label = mid
        try:
            with open(os.path.join(MAP_DIR, fn), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                label = str(raw.get("name", mid))
        except (OSError, json.JSONDecodeError):
            pass
        out.append((f"{label}（{mid}）", mid))
    return out


def _campaign_choices() -> list[tuple[str, str]]:
    """已有战役列表：(显示名, 文件 id)。"""
    out: list[tuple[str, str]] = []
    if not os.path.isdir(CAMPAIGN_DIR):
        return out
    for fn in sorted(os.listdir(CAMPAIGN_DIR)):
        if not fn.endswith(".json"):
            continue
        cid = fn[:-5]
        label = cid
        try:
            with open(os.path.join(CAMPAIGN_DIR, fn), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                label = str(raw.get("name", cid))
        except (OSError, json.JSONDecodeError):
            pass
        out.append((f"{label}（{cid}）", cid))
    return out


class CampaignEditor:
    """战役编排界面。

    由 `ui/editor.py` 的「战役」标签页承载，共用其事件循环与按钮机制。
    """

    LIST_W = 268          # 左侧关卡列表宽度
    HEADER_H = 44         # 顶栏高度（与地图编辑器一致）

    def __init__(self, editor):
        self.ed = editor                      # 宿主编辑器（提供 screen / w / h）
        self.camp: CampaignDef = CampaignDef(
            id="new_campaign", name="新建战役", description="", author="")
        self.path = ""
        self.step_index = 0
        self.scroll = 0
        self.status = ""
        self.error = ""
        self.map_choices = _map_choices()
        self.camp_choices = _campaign_choices()
        self._map_picker_open = False
        self._map_picker_scroll = 0
        self._camp_picker_open = False
        self._camp_picker_scroll = 0
        self._row_buttons: list[tuple[pygame.Rect, str, object]] = []
        self.inputs: dict[str, TextInput] = {}
        self.focus: TextInput | None = None
        self._bind_inputs()

    # ------------------------------------------------------------------ #
    # 数据装载
    # ------------------------------------------------------------------ #
    def new_campaign(self) -> None:
        self.camp = CampaignDef(id="new_campaign", name="新建战役",
                                 description="", author="", difficulty="normal")
        self.path = ""
        self.step_index = 0
        self.scroll = 0
        self.error = ""
        self.status = "已新建战役"
        self._bind_inputs()

    def open_campaign(self, camp_id: str) -> None:
        path = os.path.join(CAMPAIGN_DIR, camp_id + ".json")
        try:
            camp = load_campaign(path)
        except CampaignError as exc:
            self.error = str(exc)
            return
        self.camp = camp
        self.path = path
        self.step_index = 0
        self.scroll = 0
        self.error = ""
        self.status = f"已打开战役「{camp.name}」"
        self._bind_inputs()

    def save_campaign(self, as_new: bool = False) -> None:
        """保存战役定义。"""
        doc = self.camp.to_dict()
        if not doc.get("id"):
            doc["id"] = "new_campaign"
        if as_new or not self.path:
            safe = "".join(c if c.isalnum() or c in "-_" else "_"
                           for c in str(doc["id"]))
            self.path = os.path.join(CAMPAIGN_DIR, f"{safe}.json")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            self.error = f"保存战役失败：{exc}"
            return
        self.error = ""
        self.status = f"已保存战役「{self.camp.name}」→ {os.path.basename(self.path)}"
        self.camp_choices = _campaign_choices()

    def validate(self) -> None:
        """校验战役定义。"""
        problems: list[str] = []
        if not self.camp.steps:
            problems.append("战役至少需要一关")
        seen: dict[str, int] = {}
        for i, st in enumerate(self.camp.steps):
            if not st.map_id:
                problems.append(f"第 {i + 1} 关未选择地图")
                continue
            if st.map_id in seen:
                problems.append(f"第 {i + 1} 关与第 {seen[st.map_id] + 1} 关"
                                f"用了同一张地图")
            else:
                seen[st.map_id] = i
            if not os.path.isfile(os.path.join(MAP_DIR, st.map_id + ".json")):
                problems.append(f"第 {i + 1} 关的地图「{st.map_id}」不存在")
        if problems:
            self.error = "；".join(problems[:3])
        else:
            self.error = ""
            self.status = f"校验通过：{len(self.camp.steps)} 关，地图全部有效"

    # ------------------------------------------------------------------ #
    # 关卡操作
    # ------------------------------------------------------------------ #
    def add_step(self) -> None:
        default = self.map_choices[0][1] if self.map_choices else ""
        self.camp.steps.append(CampaignStep(
            map_id=default, title=f"第 {len(self.camp.steps) + 1} 关",
            brief="", objective="", difficulty=2, carryover=CARRYOVER_ALL))
        self.step_index = len(self.camp.steps) - 1
        self.status = f"已添加第 {self.step_index + 1} 关"
        self._bind_inputs()

    def remove_step(self) -> None:
        if not self.camp.steps:
            return
        if len(self.camp.steps) == 1:
            self.error = "战役至少要保留一关"
            return
        title = self.camp.steps[self.step_index].title
        self.camp.steps.pop(self.step_index)
        self.step_index = max(0, min(self.step_index, len(self.camp.steps) - 1))
        self.status = f"已删除关卡「{title}」"
        self.error = ""
        self._bind_inputs()

    def move_step(self, delta: int) -> None:
        i = self.step_index
        j = i + delta
        if not (0 <= i < len(self.camp.steps)) or not (0 <= j < len(self.camp.steps)):
            return
        steps = self.camp.steps
        steps[i], steps[j] = steps[j], steps[i]
        self.step_index = j
        self.status = f"已移动到第 {j + 1} 位"
        self._bind_inputs()

    def duplicate_step(self) -> None:
        if not self.camp.steps:
            return
        src = self.camp.steps[self.step_index]
        copy = CampaignStep(map_id=src.map_id, title=src.title + "（副本）",
                            brief=src.brief, objective=src.objective,
                            difficulty=src.difficulty, carryover=src.carryover)
        self.camp.steps.insert(self.step_index + 1, copy)
        self.step_index += 1
        self.status = "已复制关卡"
        self._bind_inputs()

    def current(self) -> CampaignStep | None:
        if 0 <= self.step_index < len(self.camp.steps):
            return self.camp.steps[self.step_index]
        return None

    # ------------------------------------------------------------------ #
    # 输入框
    # ------------------------------------------------------------------ #
    def _bind_inputs(self) -> None:
        """按当前选中关卡重建输入框。"""
        w = self.ed.w
        px = self.LIST_W + 24
        st = self.current()
        self.inputs = {
            "camp_name": TextInput(pygame.Rect(px + 84, self.HEADER_H + 14, 240, 22),
                                   self.camp.name, max_len=28),
            "camp_desc": TextInput(pygame.Rect(px + 84, self.HEADER_H + 42, 240, 22),
                                   self.camp.description, max_len=60),
            "camp_author": TextInput(pygame.Rect(px + 84, self.HEADER_H + 70, 240, 22),
                                     self.camp.author, max_len=24),
            "title": TextInput(pygame.Rect(px + 84, self.HEADER_H + 122, 240, 22),
                               st.title if st else "", max_len=24),
            "brief": TextInput(pygame.Rect(px + 84, self.HEADER_H + 150, 240, 22),
                               st.brief if st else "", max_len=60),
            "objective": TextInput(pygame.Rect(px + 84, self.HEADER_H + 178, 240, 22),
                                   st.objective if st else "", max_len=40),
        }
        self.focus = None

    def _apply_inputs(self) -> None:
        """把输入框内容写回数据模型。"""
        self.camp.name = self.inputs["camp_name"].value.strip() or "未命名战役"
        self.camp.description = self.inputs["camp_desc"].value.strip()
        self.camp.author = self.inputs["camp_author"].value.strip()
        st = self.current()
        if st is not None:
            st.title = self.inputs["title"].value.strip() or st.title
            st.brief = self.inputs["brief"].value.strip()
            st.objective = self.inputs["objective"].value.strip()

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def handle_event(self, e: pygame.event.Event) -> None:
        if e.type == pygame.KEYDOWN:
            if self.focus is not None:
                if e.key == pygame.K_ESCAPE:
                    self.focus.active = False
                    self.focus = None
                else:
                    self.focus.handle(e)
                    self._apply_inputs()
                return
            if e.key == pygame.K_s and (e.mod & pygame.KMOD_CTRL):
                self.save_campaign()
            elif e.key == pygame.K_DELETE:
                self.remove_step()
            elif e.key == pygame.K_UP:
                self.move_step(-1)
            elif e.key == pygame.K_DOWN:
                self.move_step(1)
            return
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            self._on_click(e.pos)
        elif e.type == pygame.MOUSEWHEEL:
            if self._map_picker_open:
                self._map_picker_scroll = max(
                    0, self._map_picker_scroll - e.y * 3)
            elif self._camp_picker_open:
                self._camp_picker_scroll = max(
                    0, self._camp_picker_scroll - e.y * 3)
            else:
                self.scroll = max(0, self.scroll - e.y * 2)

    def _on_click(self, pos) -> None:
        # 地图选择浮层优先
        if self._map_picker_open:
            self._click_map_picker(pos)
            return
        # 战役选择浮层
        if self._camp_picker_open:
            self._click_camp_picker(pos)
            return
        # 顶栏按钮
        if pos[1] < self.HEADER_H:
            for rect, action, arg in self._row_buttons:
                if rect.collidepoint(pos):
                    self._do_action(action, arg)
                    return
            return
        # 左侧关卡列表
        if pos[0] < self.LIST_W:
            row = (pos[1] - self.HEADER_H - 46 + self.scroll) // 34
            if 0 <= row < len(self.camp.steps):
                self.step_index = row
                self._bind_inputs()
            return
        # 右侧表单按钮
        for rect, action, arg in self._row_buttons:
            if rect.collidepoint(pos):
                self._do_action(action, arg)
                return
        # 输入框
        for inp in self.inputs.values():
            if inp.rect.collidepoint(pos):
                if self.focus is not None:
                    self.focus.active = False
                self.focus = inp
                inp.active = True
                return
        if self.focus is not None:
            self.focus.active = False
            self.focus = None

    def _do_action(self, action: str, arg) -> None:
        if action == "camp_add":
            self.add_step()
        elif action == "camp_del":
            self.remove_step()
        elif action == "camp_up":
            self.move_step(-1)
        elif action == "camp_down":
            self.move_step(1)
        elif action == "camp_dup":
            self.duplicate_step()
        elif action == "camp_save":
            self._apply_inputs()
            self.save_campaign()
        elif action == "camp_save_as":
            self._apply_inputs()
            self.save_campaign(as_new=True)
        elif action == "camp_validate":
            self._apply_inputs()
            self.validate()
        elif action == "camp_new":
            self.new_campaign()
        elif action == "camp_open":
            self.open_campaign(str(arg))
        elif action == "map_pick":
            self._map_picker_open = True
            self._map_picker_scroll = 0
        elif action == "map_pick_cancel":
            self._map_picker_open = False
        elif action == "map_pick_choose":
            st = self.current()
            if st is not None:
                st.map_id = str(arg)
                self._map_picker_open = False
                self.status = f"本关地图已设为 {st.map_id}"
        elif action == "camp_open_menu":
            self._camp_picker_open = True
            self._camp_picker_scroll = 0
        elif action == "camp_open_choose":
            self._camp_picker_open = False
            self.open_campaign(str(arg))
        elif action == "diff":
            st = self.current()
            if st is not None:
                st.difficulty = max(1, min(4, st.difficulty + int(arg)))
                self.status = f"难度：{st.difficulty_label}"
        elif action == "carry":
            st = self.current()
            if st is not None:
                st.carryover = str(arg)
                self.status = f"继承策略：{CARRYOVER_LABELS.get(st.carryover, '')}"

    def _click_map_picker(self, pos) -> None:
        for rect, action, arg in self._row_buttons:
            if rect.collidepoint(pos):
                self._do_action(action, arg)
                return
        r = self._picker_rect()
        if not r.collidepoint(pos):
            self._map_picker_open = False
            return
        idx = (pos[1] - r.y - 34 + self._map_picker_scroll) // 26
        if 0 <= idx < len(self.map_choices):
            self._do_action("map_pick_choose", self.map_choices[idx][1])

    def _camp_picker_rect(self) -> pygame.Rect:
        w = 320
        h = 60 + min(10, max(1, len(self.camp_choices))) * 26
        return pygame.Rect(14, self.HEADER_H + 84, w, h)

    def _click_camp_picker(self, pos) -> None:
        for rect, action, arg in self._row_buttons:
            if rect.collidepoint(pos):
                self._do_action(action, arg)
                return
        r = self._camp_picker_rect()
        if not r.collidepoint(pos):
            self._camp_picker_open = False
            return
        idx = (pos[1] - r.y - 34 + self._camp_picker_scroll) // 26
        if 0 <= idx < len(self.camp_choices):
            self._do_action("camp_open_choose", self.camp_choices[idx][1])

    def _picker_rect(self) -> pygame.Rect:
        w = 320
        h = 60 + min(10, len(self.map_choices)) * 26
        return pygame.Rect((self.ed.w - w) // 2, 90, w, h)

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def draw(self, s: pygame.Surface) -> None:
        self._row_buttons = []
        s.fill(T.BG)
        self._draw_list(s)
        self._draw_form(s)
        self._draw_curve(s)
        if self._map_picker_open:
            self._draw_map_picker(s)
        if self._camp_picker_open:
            self._draw_camp_picker(s)

    def _draw_list(self, s: pygame.Surface) -> None:
        """左侧：关卡列表 + 操作按钮。"""
        r = pygame.Rect(0, self.HEADER_H, self.LIST_W, self.ed.h - self.HEADER_H)
        pygame.draw.rect(s, T.PANEL, r)
        T.vline(s, r.right, r.y, r.bottom, T.BORDER)

        T.draw_text(s, tr("ui.camp_editor_levels"), (14, r.y + 12), 11, T.TEXT_FAINT, bold=True)
        T.draw_text(s, tr("ui.camp_editor_levels_count_fmt") % (len(self.camp.steps),), (self.LIST_W - 14, r.y + 12),
                    11, T.TEXT_DIM, anchor="topright")

        # 操作按钮行
        by = r.y + 32
        for i, (label, action) in enumerate((("+", "camp_add"), ("−", "camp_del"),
                                             ("↑", "camp_up"), ("↓", "camp_down"),
                                             ("⧉", "camp_dup"))):
            br = pygame.Rect(14 + i * 32, by, 28, 24)
            hover = br.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL_SUNK, br, border_radius=3)
            pygame.draw.rect(s, T.BORDER, br, 1, border_radius=3)
            T.draw_text(s, label, br.center, 12, T.TEXT if hover else T.TEXT_DIM,
                        anchor="center")
            self._row_buttons.append((br, action, None))

        # 打开已有战役 / 新建
        oy = by + 28
        ob = pygame.Rect(14, oy, self.LIST_W - 28, 24)
        hover = ob.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL_SUNK, ob, border_radius=3)
        pygame.draw.rect(s, T.BORDER, ob, 1, border_radius=3)
        T.draw_text(s, tr("ui.camp_editor_open"), ob.center, 11,
                    T.TEXT if hover else T.TEXT_DIM, anchor="center")
        self._row_buttons.append((ob, "camp_open_menu", None))

        # 关卡条目
        ly = oy + 32
        for i, st in enumerate(self.camp.steps):
            row_r = pygame.Rect(8, ly + i * 34 - self.scroll, self.LIST_W - 16, 30)
            if row_r.bottom < r.y or row_r.top > r.bottom:
                continue
            active = i == self.step_index
            hover = row_r.collidepoint(pygame.mouse.get_pos())
            if active:
                pygame.draw.rect(s, T.PANEL_HI, row_r, border_radius=3)
                pygame.draw.rect(s, T.CYAN, pygame.Rect(row_r.x, row_r.y, 3, row_r.height))
            elif hover:
                pygame.draw.rect(s, T.PANEL_SUNK, row_r, border_radius=3)
            T.draw_text(s, f"{i + 1}.", (row_r.x + 8, row_r.centery), 11,
                        T.CYAN if active else T.TEXT_FAINT, anchor="midleft")
            T.draw_text(s, st.title or tr("ui.camp_editor_unnamed"), (row_r.x + 28, row_r.centery), 11,
                        T.TEXT if active else T.TEXT_DIM, anchor="midleft")
            # 难度徽标
            badge = st.difficulty_label
            col = {1: T.GREEN, 2: T.TEXT_DIM, 3: T.AMBER, 4: T.RED}.get(st.difficulty, T.TEXT_DIM)
            T.draw_text(s, badge, (row_r.right - 8, row_r.centery), 10, col, anchor="midright")

        if not self.camp.steps:
            T.draw_text(s, tr("ui.camp_editor_no_levels"), (self.LIST_W // 2, ly + 20),
                        11, T.TEXT_FAINT, anchor="center")

    def _draw_form(self, s: pygame.Surface) -> None:
        """右侧：战役属性与关卡配置表单。"""
        px = self.LIST_W + 24
        y = self.HEADER_H

        T.draw_text(s, tr("ui.camp_editor_props"), (px, y + 14), 12, T.TEXT, bold=True)
        # 属性输入框
        for key, label in (("camp_name", "名称"), ("camp_desc", "简介"),
                           ("camp_author", "作者")):
            T.draw_text(s, label, (px, self.inputs[key].rect.centery), 11,
                        T.TEXT_DIM, anchor="midleft")
            self.inputs[key].draw(s, pygame.mouse.get_pos())

        st = self.current()
        T.draw_text(s, tr("ui.camp_editor_step_fmt") % (self.step_index + 1,), (px, y + 108), 12, T.TEXT, bold=True)
        for key, label in (("title", "标题"), ("brief", "简报"),
                           ("objective", "任务")):
            T.draw_text(s, label, (px, self.inputs[key].rect.centery), 11,
                        T.TEXT_DIM, anchor="midleft")
            self.inputs[key].draw(s, pygame.mouse.get_pos())

        # 地图选择
        T.draw_text(s, tr("ui.camp_editor_map"), (px, y + 214), 11, T.TEXT_DIM, anchor="midleft")
        mr = pygame.Rect(px + 84, y + 202, 240, 24)
        hover = mr.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(s, T.PANEL_SUNK, mr, border_radius=3)
        pygame.draw.rect(s, T.BORDER_HI if hover else T.BORDER, mr, 1, border_radius=3)
        map_label = st.map_id or "（未选择）" if st else "（未选择）"
        T.draw_text(s, map_label, (mr.x + 8, mr.centery), 11, T.TEXT, anchor="midleft")
        T.draw_text(s, tr("ui.camp_editor_select"), (mr.right - 8, mr.centery), 11, T.CYAN, anchor="midright")
        self._row_buttons.append((mr, "map_pick", None))

        # 难度
        T.draw_text(s, tr("ui.camp_editor_difficulty"), (px, y + 250), 11, T.TEXT_DIM, anchor="midleft")
        dr = pygame.Rect(px + 84, y + 238, 240, 24)
        pygame.draw.rect(s, T.PANEL_SUNK, dr, border_radius=3)
        pygame.draw.rect(s, T.BORDER, dr, 1, border_radius=3)
        if st:
            diff_col = {1: T.GREEN, 2: T.TEXT, 3: T.AMBER, 4: T.RED}.get(st.difficulty, T.TEXT)
            T.draw_text(s, st.difficulty_label, (dr.x + 8, dr.centery), 11,
                        diff_col, anchor="midleft")
        for i, d in enumerate((1, 2, 3, 4)):
            br = pygame.Rect(dr.right - 84 + i * 20, dr.y + 4, 18, 16)
            pygame.draw.rect(s, T.PANEL_HI, br, border_radius=2)
            T.draw_text(s, str(d), br.center, 10, T.TEXT_DIM, anchor="center")
            self._row_buttons.append((br, "diff", 1 if d > (st.difficulty if st else 2) else -1))

        # 继承策略
        T.draw_text(s, tr("ui.camp_editor_inherit"), (px, y + 282), 11, T.TEXT_DIM, anchor="midleft")
        cr = pygame.Rect(px + 84, y + 270, 240, 24)
        pygame.draw.rect(s, T.PANEL_SUNK, cr, border_radius=3)
        pygame.draw.rect(s, T.BORDER, cr, 1, border_radius=3)
        if st:
            T.draw_text(s, CARRYOVER_LABELS.get(st.carryover, st.carryover),
                        (cr.x + 8, cr.centery), 11, T.TEXT, anchor="midleft")
        for i, pol in enumerate(CARRYOVER_ORDER):
            br = pygame.Rect(cr.right - 84 + i * 27, cr.y + 4, 25, 16)
            hit = br.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, T.PANEL_HI if hit else T.PANEL, br, border_radius=2)
            T.draw_text(s, {CARRYOVER_ALL: tr("ui.camp_editor_all"), CARRYOVER_VETERANS: tr("ui.camp_editor_veterans"),
                            CARRYOVER_NONE: "无"}[pol],
                        br.center, 10, T.TEXT_DIM if hit else T.TEXT_FAINT, anchor="center")
            self._row_buttons.append((br, "carry", pol))

        # 底部：保存 / 校验
        by = self.ed.h - 60
        for i, (label, action) in enumerate((("保存", "camp_save"),
                                             ("另存为", "camp_save_as"),
                                             ("校验", "camp_validate"))):
            br = pygame.Rect(px + i * 84, by, 78, 26)
            hover = br.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(s, T.PANEL_HI if hover else T.PANEL, br, border_radius=3)
            pygame.draw.rect(s, T.CYAN if hover else T.BORDER, br, 1, border_radius=3)
            T.draw_text(s, label, br.center, 11, T.TEXT if hover else T.TEXT_DIM, anchor="center")
            self._row_buttons.append((br, action, None))

    def _draw_curve(self, s: pygame.Surface) -> None:
        """难度曲线：一眼看出整场战役的难度起伏。"""
        steps = self.camp.steps
        if len(steps) < 2:
            return
        px = self.LIST_W + 24
        w = min(360, self.ed.w - px - 40)
        r = pygame.Rect(px, self.HEADER_H + 312, w, 74)
        pygame.draw.rect(s, T.PANEL_SUNK, r, border_radius=3)
        pygame.draw.rect(s, T.BORDER, r, 1, border_radius=3)
        T.draw_text(s, tr("ui.camp_editor_curve"), (r.x + 8, r.y + 5), 10, T.TEXT_FAINT)

        n = len(steps)
        inner_x = r.x + 12
        inner_w = r.width - 24
        inner_y = r.y + 20
        inner_h = r.height - 28
        pts: list[tuple[int, int]] = []
        for i, st in enumerate(steps):
            cx = inner_x + int(inner_w * (i / max(1, n - 1)))
            cy = inner_y + inner_h - int((st.difficulty - 1) / 3 * inner_h)
            pts.append((cx, cy))
        if len(pts) >= 2:
            pygame.draw.lines(s, T.CYAN, False, pts, 2)
        for i, (cx, cy) in enumerate(pts):
            col = {1: T.GREEN, 2: T.CYAN, 3: T.AMBER, 4: T.RED}.get(steps[i].difficulty, T.CYAN)
            pygame.draw.circle(s, col, (cx, cy), 4)
            T.draw_text_center_x(s, str(i + 1), cx, cy - 12, 10, T.TEXT_FAINT)

    def _draw_map_picker(self, s: pygame.Surface) -> None:
        """地图选择浮层。"""
        dim = pygame.Surface((self.ed.w, self.ed.h), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 190))
        s.blit(dim, (0, 0))
        r = self._picker_rect()
        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=5)
        T.draw_text(s, tr("ui.camp_editor_pick_map"), (r.x + 14, r.y + 10), 12, T.TEXT, bold=True)
        T.hline(s, r.x, r.right, r.y + 32, T.BORDER)

        n = len(self.map_choices)
        shown = min(10, n)
        for i in range(shown):
            idx = i + self._map_picker_scroll
            if idx >= n:
                break
            label, mid = self.map_choices[idx]
            row = pygame.Rect(r.x + 8, r.y + 36 + i * 26, r.width - 16, 24)
            hover = row.collidepoint(pygame.mouse.get_pos())
            if hover:
                pygame.draw.rect(s, T.PANEL_HI, row, border_radius=3)
            T.draw_text(s, label, (row.x + 8, row.centery), 11,
                        T.TEXT if hover else T.TEXT_DIM, anchor="midleft")
            self._row_buttons.append((row, "map_pick_choose", mid))

        if n > shown:
            T.draw_text_center_x(s, f"滚动查看更多（{n} 张）", r.centerx, r.bottom - 16, 10,
                                T.TEXT_FAINT)

    def _draw_camp_picker(self, s: pygame.Surface) -> None:
        """已有战役选择浮层。"""
        dim = pygame.Surface((self.ed.w, self.ed.h), pygame.SRCALPHA)
        dim.fill((*T.mix(T.BG, (0, 0, 0), 0.5), 190))
        s.blit(dim, (0, 0))
        r = self._camp_picker_rect()
        T.panel(s, r, T.PANEL, T.BORDER_HI, radius=5)
        T.draw_text(s, tr("ui.camp_editor_open_campaign"), (r.x + 14, r.y + 10), 12, T.TEXT, bold=True)
        T.hline(s, r.x, r.right, r.y + 32, T.BORDER)

        n = len(self.camp_choices)
        if n == 0:
            T.draw_text(s, tr("ui.camp_editor_no_campaigns"), (r.centerx, r.y + 56),
                        11, T.TEXT_FAINT, anchor="center")
            return
        shown = min(10, n)
        for i in range(shown):
            idx = i + self._camp_picker_scroll
            if idx >= n:
                break
            label, cid = self.camp_choices[idx]
            row = pygame.Rect(r.x + 8, r.y + 36 + i * 26, r.width - 16, 24)
            hover = row.collidepoint(pygame.mouse.get_pos())
            if hover:
                pygame.draw.rect(s, T.PANEL_HI, row, border_radius=3)
            T.draw_text(s, label, (row.x + 8, row.centery), 11,
                        T.TEXT if hover else T.TEXT_DIM, anchor="midleft")
            self._row_buttons.append((row, "camp_open_choose", cid))


__all__ = ["CampaignEditor"]

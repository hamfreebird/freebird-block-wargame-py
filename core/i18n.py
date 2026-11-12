"""本地化框架：字符串抽离到语言文件，支持 8 种语言即时切换。

设计要点
--------
1. **core 不依赖 pygame**：本模块只做字符串查表，纯数据层；
2. **缺失即回退**：语言文件里没有的键回退到中文原文，再回退到键本身——
   绝不会因为漏翻译而崩溃或显示空白；
3. **可热切换**：切换语言后立即生效，无需重启；
4. **社区可扩展**：把新的语言包放进 `data/lang/<code>.json` 即可被发现，
   不需要改代码。界面文案单独放在 `<code>_ui.json` 分层包里，改译文
   不会覆盖基础词条；查表时自动装载安装目录的语言包，UI 层无需关心
   初始化顺序。

用法::

    from core.i18n import tr, set_language
    set_language("en")
    T.draw_text(s, tr("menu.start"), ...)
"""
from __future__ import annotations

import json
import os

DEFAULT_LANGUAGE = "zh"
#: v3.0：从 2 种扩到 8 种。新增的语言包在 ``data/lang/<code>.json``。
SUPPORTED_LANGUAGES: list[str] = ["zh", "en", "ja", "ko", "de", "fr", "es", "ru"]

LANGUAGE_NAMES: dict[str, str] = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "ru": "Русский",
}

# 内置英文词条（缺省语言包；磁盘上的 data/lang/<code>.json 可覆盖）
_BUILTIN_EN: dict[str, str] = {
    # -- 主菜单 ----------------------------------------------------------
    "menu.title": "Block Wargame",
    "menu.start": "Start Operation",
    "menu.editor": "Map / Weapon Editor",
    "menu.tutorial": "Quick Start",
    "menu.tutorial_tag": "Tutorial",
    "menu.campaign": "Campaign",
    "menu.settings": "Settings",
    "menu.select_scenario": "Select Scenario",
    "menu.import": "Import",
    "menu.replay": "Replay",
    "menu.click_to_choose": "Click to select",
    "menu.tips": "Tips",
    "menu.tip_data": "Editable data: data/maps/*.json defines maps and deployments, "
                     "data/units/armory.json defines weapons and units",
    "menu.tip_controls": "Left click to select/move, right click to move, wheel to zoom, "
                         "middle drag to pan, Space to end turn, F1 for rules",

    # -- 顶栏 ------------------------------------------------------------
    "top.turn": "Turn",
    "top.phase.player": "Your turn",
    "top.phase.ai": "Enemy turn",
    "top.vp": "VP",
    "top.forces": "Forces",
    "top.nav": "Satellite nav",
    "top.env": "Environment",
    "top.inflight": "In flight",
    "top.overlay": "Overlay",
    "top.help": "Help",
    "top.end_turn": "End Turn",

    # -- 侧栏 ------------------------------------------------------------
    "tab.unit": "Unit",
    "tab.situation": "Situation",
    "tab.log": "Log",
    "unit.weapons": "Weapons",
    "unit.sensors": "Sensors",
    "unit.ammo": "Ammo",
    "unit.mp": "MP",
    "unit.hp": "HP",
    "unit.fuel": "Fuel",
    "unit.fort": "Fort",
    "unit.status": "Status",
    "unit.actions": "Actions",
    "unit.radar": "Radar",
    "unit.jam": "Jam",
    "unit.datalink": "Link",
    "unit.standby": "Standby",
    "unit.build_fort": "Build fort",
    "unit.smoke": "Smoke",
    "unit.mission": "Profile",
    "unit.prepare_fire": "Pre-fire",
    "unit.command": "Command",
    "situation.objectives": "Objectives",
    "situation.satellites": "Space assets",
    "situation.ew": "EW status",
    "log.empty": "No records yet",

    # -- 交战面板 --------------------------------------------------------
    "engage.target": "Engagement target",
    "engage.confirm": "Confirm fire",
    "engage.counter_view": "Opponent view",
    "engage.intercept": "Intercept",
    "engage.reply": "Counter-fire",
    "engage.enemy_fc": "Enemy fire control",
    "engage.blocker": "Blocker",
    "engage.no_intercept": "No enemy air defense coverage",
    "engage.no_reply": "No enemy weapon can reach you",

    # -- 悬停卡 ----------------------------------------------------------
    "inspect.unknown": "Unknown unit",
    "inspect.contact": "Contact",
    "inspect.classified": "Classified",
    "inspect.tracked": "Tracked",
    "inspect.range": "Range",

    # -- 系统菜单 --------------------------------------------------------
    "menu.system": "System menu",
    "menu.resume": "Resume",
    "menu.to_menu": "Scenario select",
    "menu.export_report": "Export report",
    "menu.export_pkg": "Export package",
    "menu.import_pkg": "Import package",
    "menu.quit": "Quit",

    # -- 结算 ------------------------------------------------------------
    "over.victory": "Victory",
    "over.defeat": "Defeat",
    "over.draw": "Draw",
    "over.back": "Back to scenarios",
    "over.next_step": "Next mission",
    "over.stats": "Battle Debrief",
    "over.efficiency": "Engagement effectiveness",
    "over.vp_curve": "VP progression",
    "over.highlights": "Highlights",
    "over.shots": "Shots fired",
    "over.hits": "Shots hit",
    "over.intercepted": "Intercepted",
    "over.kills": "Units destroyed",
    "over.losses": "Own losses",
    "over.survivors": "Remaining units",
    "over.blue": "Blue",
    "over.red": "Red",

    # -- 简报 ------------------------------------------------------------
    "brief.title": "Mission Briefing",
    "brief.enemy": "Enemy forces",
    "brief.own": "Our forces",
    "brief.objective": "Objectives",
    "brief.victory": "Victory condition",
    "brief.start": "Begin operation",
    "brief.env": "Initial environment",
    "brief.events": "Scheduled events",

    # -- 设置 ------------------------------------------------------------
    "settings.title": "Settings",
    "settings.keys": "Key bindings",
    "settings.theme": "Theme",
    "settings.sound": "Sound",
    "settings.language": "Language",
    "settings.reset": "Reset keys",
    "settings.done": "Done",

    # -- 通用 ------------------------------------------------------------
    "common.cancel": "Cancel",
    "common.confirm": "Confirm",
    "common.none": "None",
    "common.turns": "turns",
    "common.units": "units",
}

# 中文源语言文案：中文界面直接用这张表，英文界面用 _BUILTIN_EN
_BUILTIN_ZH: dict[str, str] = {
    # -- 主菜单 ----------------------------------------------------------
    "menu.title": "方块兵棋",
    "menu.start": "开始作战",
    "menu.editor": "地图 / 武器编辑器",
    "menu.tutorial": "快速上手",
    "menu.tutorial_tag": "教学",
    "menu.campaign": "战役模式",
    "menu.settings": "设置",
    "menu.select_scenario": "选择想定",
    "menu.import": "导入想定",
    "menu.replay": "导入回放",
    "menu.click_to_choose": "点击选择",
    "menu.tips": "提示",
    "menu.tip_data": "数据可编辑：data/maps/*.json 定义地图与部署，"
                     "data/units/armory.json 定义武器与兵种",
    "menu.tip_controls": "左键选择/移动，右键下达机动指令，滚轮缩放，中键拖动平移，"
                         "空格结束回合，F1 查看规则",

    # -- 顶栏 ------------------------------------------------------------
    "top.turn": "回合",
    "top.phase.player": "蓝方行动",
    "top.phase.ai": "红方行动",
    "top.vp": "胜负点",
    "top.forces": "在编兵力",
    "top.nav": "卫星导航完好度",
    "top.env": "战场环境",
    "top.inflight": "在飞弹药",
    "top.overlay": "覆盖层",
    "top.help": "帮助",
    "top.end_turn": "结束回合",

    # -- 侧栏 ------------------------------------------------------------
    "tab.unit": "单位详情",
    "tab.situation": "战役态势",
    "tab.log": "作战日志",
    "unit.weapons": "武器系统",
    "unit.sensors": "传感器",
    "unit.ammo": "备弹",
    "unit.mp": "机动点",
    "unit.hp": "耐久",
    "unit.fuel": "燃油",
    "unit.fort": "工事",
    "unit.status": "状态",
    "unit.actions": "可用操作",
    "unit.radar": "雷达",
    "unit.jam": "干扰",
    "unit.datalink": "数据链",
    "unit.standby": "待机",
    "unit.build_fort": "筑工事",
    "unit.smoke": "烟幕",
    "unit.mission": "剖面",
    "unit.prepare_fire": "预先射击",
    "unit.command": "指挥",
    "situation.objectives": "目标点",
    "situation.satellites": "天基资产",
    "situation.ew": "电子战状态",
    "log.empty": "暂无记录",

    # -- 交战面板 --------------------------------------------------------
    "engage.target": "交战目标",
    "engage.confirm": "确认开火",
    "engage.counter_view": "对手视角",
    "engage.intercept": "拦截",
    "engage.reply": "反击",
    "engage.enemy_fc": "敌方火控",
    "engage.blocker": "阻碍",
    "engage.no_intercept": "无敌方防空覆盖",
    "engage.no_reply": "无可威胁到你的敌方火力",

    # -- 悬停卡 ----------------------------------------------------------
    "inspect.unknown": "不明目标",
    "inspect.contact": "接触",
    "inspect.classified": "识别",
    "inspect.tracked": "跟踪",
    "inspect.range": "射程",

    # -- 系统菜单 --------------------------------------------------------
    "menu.system": "系统菜单",
    "menu.resume": "继续作战",
    "menu.to_menu": "返回想定选择",
    "menu.export_report": "导出战报",
    "menu.export_pkg": "导出想定包",
    "menu.import_pkg": "导入想定包",
    "menu.quit": "退出游戏",

    # -- 结算 ------------------------------------------------------------
    "over.victory": "胜 利",
    "over.defeat": "失 败",
    "over.draw": "战 平",
    "over.back": "返回想定选择",
    "over.next_step": "进入下一关",
    "over.stats": "战斗回顾",
    "over.efficiency": "交战效能",
    "over.vp_curve": "胜负点走势",
    "over.highlights": "要点",
    "over.shots": "开火弹数",
    "over.hits": "命中弹数",
    "over.intercepted": "被拦截",
    "over.kills": "击毁单位",
    "over.losses": "自身损失",
    "over.survivors": "剩余兵力",
    "over.blue": "蓝方",
    "over.red": "红方",

    # -- 简报 ------------------------------------------------------------
    "brief.title": "作战简报",
    "brief.enemy": "敌方编成",
    "brief.own": "我方编成",
    "brief.objective": "目标点与胜利条件",
    "brief.victory": "胜利条件",
    "brief.start": "开始作战",
    "brief.env": "初始环境",
    "brief.events": "预定事件",

    # -- 设置 ------------------------------------------------------------
    "settings.title": "设置",
    "settings.keys": "键位自定义",
    "settings.theme": "界面主题",
    "settings.sound": "音效",
    "settings.language": "界面语言",
    "settings.reset": "恢复默认键位",
    "settings.done": "完成",

    # -- 通用 ------------------------------------------------------------
    "common.cancel": "取消",
    "common.confirm": "确认",
    "common.none": "无",
    "common.turns": "回合",
    "common.units": "单位",
}

_current_lang = DEFAULT_LANGUAGE
_tables: dict[str, dict[str, str]] = {}
_loaded_dirs: list[str] = []
#: 安装目录语言包是否已懒加载过（见 _ensure_default_packs）
_default_packs_loaded = False
#: 懒加载连续失败次数（见 _ensure_default_packs）
_default_pack_failures = 0
#: 连续失败到这么多次就放弃重试，避免每次 tr() 都吃一次异常
_DEFAULT_PACK_MAX_FAILURES = 3


def _builtin_table(lang: str) -> dict[str, str]:
    if lang == "en":
        return dict(_BUILTIN_EN)
    if lang == "zh":
        return dict(_BUILTIN_ZH)
    return {}


def load_language_dir(directory: str) -> int:
    """从目录加载语言包（`*.json` → 语言代码）。返回加载的文件数。

    磁盘上的词条会覆盖内置词条，便于社区补充或修正翻译。

    **分层语言包**（v3.0）：文件名可以是 ``<code>.json``（整包）
    或 ``<code>_<层>.json``（分层，如 ``ja_ui.json``）。分层包的
    词条会合并进同一种语言——这样「基础词条」与「界面词条」可以
    分别维护而不互相覆盖：改 UI 译文时不会丢掉手改过的菜单文案。

    层名只允许字母数字与下划线，避免把怪名字当成新的语言代码
    注册进 ``_tables``（那会让设置界面多出一堆看不懂的选项）。
    """
    if not directory or not os.path.isdir(directory):
        return 0
    count = 0
    for fn in os.listdir(directory):
        if not fn.endswith(".json"):
            continue
        stem = fn[:-5].strip().lower()
        if not stem:
            continue
        # 拆出「语言代码」与可选的「层名」
        if "_" in stem:
            head, _, tail = stem.partition("_")
            if head and tail and all(c.isalnum() or c == "_" for c in tail):
                code = head
            else:
                code = stem
        else:
            code = stem
        if not code:
            continue
        path = os.path.join(directory, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        table = _tables.setdefault(code, {})
        table.update({str(k): str(v) for k, v in data.items()})
        count += 1
    if directory not in _loaded_dirs:
        _loaded_dirs.append(directory)
    return count


def _ensure_default_packs() -> None:
    """首次查表时自动装载安装目录的语言包。

    **为什么只靠 ui/app.py 启动时加载不够**：v3.0 把界面文案抽进了
    ``data/lang/*_ui.json``，但历史上只有主程序启动时会调用
    ``load_language_dir``。专项自检、模组预览工具等若单独使用 ``ui``
    模块，没走那条初始化路径就会让 ``tr()`` 回退成裸键名；而裸键名
    再被 ``% (...)`` 格式化会直接抛 TypeError。把加载下沉到查表层，
    UI 层就不再依赖初始化顺序。

    只加载**安装目录**；用户目录仍由主程序随后加载，保持「用户覆盖」
    的优先级。目录不存在或不可读时静默跳过——内置词条足以兜底，
    查表不该因为一个可选的资源目录而崩。
    """
    global _default_packs_loaded, _default_pack_failures
    if _default_packs_loaded:
        return
    try:
        from .paths import lang_dir
        load_language_dir(lang_dir())
    except Exception:
        # **失败不置「已加载」**：安装目录可能只是一时读不到（网络盘掉线、
        # 权限刚改、路径正在被安装程序替换），下一次查表该有机会重试。
        # 但也不能无限重试——每次都抛一次异常会让 tr() 变成隐性性能坑，
        # 所以连续失败到上限就认命。
        _default_pack_failures += 1
        if _default_pack_failures >= _DEFAULT_PACK_MAX_FAILURES:
            _default_packs_loaded = True
        return
    _default_packs_loaded = True


def available_languages() -> list[str]:
    """全部可用语言代码（内置 + 磁盘加载）。"""
    codes = list(SUPPORTED_LANGUAGES)
    for code in _tables:
        if code not in codes:
            codes.append(code)
    return codes


def set_language(lang: str) -> bool:
    """切换当前语言。返回是否成功（不支持的语言代码被拒绝）。"""
    global _current_lang
    code = (lang or "").strip().lower()
    if code not in available_languages():
        return False
    _current_lang = code
    return True


def current_language() -> str:
    return _current_lang


def language_name(code: str) -> str:
    """语言代码 → 显示名。"""
    if code in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[code]
    return code


def tr(key: str, default: str = "") -> str:
    """查表取译文。

    查找顺序：当前语言的磁盘包 → 当前语言的内置表 → 另一语言的内置表
    → **中文磁盘包** → 传入的 default → 键本身。

    磁盘上的语言包（`data/lang/<code>.json`、用户目录 `lang/<code>.json`）
    优先于内置表，因此社区可以覆盖或补充任何一条译文。

    **为什么要多一层「中文磁盘包」**：v3.0 把 166 条界面文案抽进了
    分层语言包，但翻译是逐步补齐的。某个键在德语包里还没有译文时，
    如果直接返回键本身，玩家会看到 ``ui.editor_fort_level`` 这种
    内部标识；回退到中文原文至少是**可读的中文**，
    比暴露内部键名好得多。

    社区补完译文后，这条回退自然就不再触发。
    """
    _ensure_default_packs()
    if not key:
        return default
    table = _tables.get(_current_lang)
    if table:
        v = table.get(key)
        if v:
            return v
    v = _builtin_table(_current_lang).get(key)
    if v:
        return v
    # 回退到另一语言（中文 ↔ 英文），保证界面不会显示裸键名
    other = "en" if _current_lang == DEFAULT_LANGUAGE else DEFAULT_LANGUAGE
    v = _builtin_table(other).get(key)
    if v:
        return v
    # 回退到中文磁盘包（未翻译的键显示中文原文而非内部键名）
    if _current_lang != DEFAULT_LANGUAGE:
        zh_table = _tables.get(DEFAULT_LANGUAGE)
        if zh_table:
            v = zh_table.get(key)
            if v:
                return v
    if default:
        return default
    return key


def register_mod_entry(lang: str, key: str, value: str) -> bool:
    """注册一条模组词条。返回是否成功。

    **模组不能覆盖游戏本体的键**——键已存在于任一内置语言表时
    会被拒绝。否则一个社区模组就能把「开始作战」翻译成任意内容。

    模组词条放在独立的命名空间里（键形如 ``my_mod.unit_desc``），
    与本体键互不干扰。
    """
    code = (lang or "").strip().lower()
    if not code or not key:
        return False
    # 本体键保护：内置表里已有的键不允许被模组覆盖
    for builtin in (_BUILTIN_ZH, _BUILTIN_EN):
        if key in builtin:
            return False
    # 同一语言包里已有的键不允许覆盖（那是别的模组或用户的贡献）
    table = _tables.setdefault(code, {})
    if key in table:
        return False
    table[key] = str(value)
    return True


def table_size(lang: str = "") -> int:
    """某个语言的词条数量（用于自检）。"""
    code = lang or _current_lang
    return len(_tables.get(code, {})) + len(_builtin_table(code))


__all__ = [
    "DEFAULT_LANGUAGE", "SUPPORTED_LANGUAGES", "LANGUAGE_NAMES",
    "tr", "set_language", "current_language", "language_name",
    "available_languages", "load_language_dir", "table_size",
    "register_mod_entry",
]

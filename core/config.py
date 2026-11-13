"""用户配置：键位自定义与界面偏好。

设计要点：
1. 配置文件为人类可读的 JSON，位于用户可写目录（见 core.paths.user_writable_dir）；
2. 键位以「pygame 键名」字符串保存（如 "space" / "f1" / "a"），
   而非平台相关的整数常量——这样配置文件跨平台可读、可手工编辑；
3. 配置文件损坏、字段缺失或键名非法时，自动回退到默认值，绝不阻断游戏启动；
4. 本模块不依赖 pygame，纯数据层；键名与键码的转换在 UI 层完成。
"""
from __future__ import annotations

import json
import os
from typing import Any

from .i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

CONFIG_VERSION = 1
CONFIG_FILENAME = "user_config.json"

# 可用的界面主题（与 ui/theme.py 中的定义保持一致）
DEFAULT_THEME = "tactical"

# 字号档位与形状标识模式（与 ui/theme.py 保持一致）。
# config 不导入 ui 层（它要能在无 pygame 的环境里跑），所以这里另列一份常量，
# 由 tools/test_a11y.py 断言两处一致——重复的定义必须有人看着。
FONT_SCALE_IDS = ("standard", "large", "xlarge")
DEFAULT_FONT_SCALE = "standard"
SHAPE_MODE_IDS = ("auto", "on", "off")
DEFAULT_SHAPE_MODE = "auto"

#: 对手 AI 的驱动方式：``rules`` 纯规则启发式，``llm`` 本地大模型给决心。
#: 默认关：它要额外的 1.1GB 模型与一个推理进程，不能替玩家做主。
AI_MODES = ("rules", "llm")
DEFAULT_AI_MODE = "rules"


# --------------------------------------------------------------------------- #
# 键位定义
# --------------------------------------------------------------------------- #
# 动作 ID → 显示名。UI 与配置文件都使用动作 ID 作为主键。
ACTION_LABELS: dict[str, str] = {
    "end_turn": "结束回合",
    "next_unit": "切换单位",
    "cancel": "取消 / 取消选择",
    "confirm": "确认开火",
    "radar": "雷达开关",
    "jam": "干扰开关",
    "overlay": "切换覆盖层",
    "fit_map": "地图复位",
    "help": "规则帮助",
    "system_menu": "系统菜单",
    "quick_save": "快速保存",
    "quick_load": "快速读取",
}

# 武器快捷槽（1-9）单独成组，便于在设置面板中分两栏显示
SLOT_ACTIONS: list[str] = [f"weapon_{i}" for i in range(1, 10)]

SLOT_LABELS: dict[str, str] = {f"weapon_{i}": f"武器槽 {i}" for i in range(1, 10)}

DEFAULT_BINDINGS: dict[str, str] = {
    "end_turn": "space",
    "next_unit": "tab",
    "cancel": "escape",
    "confirm": "return",
    "radar": "r",
    "jam": "j",
    "overlay": "e",
    "fit_map": "f",
    "help": "f1",
    "system_menu": "f10",
    "quick_save": "f5",
    "quick_load": "f9",
    "weapon_1": "1",
    "weapon_2": "2",
    "weapon_3": "3",
    "weapon_4": "4",
    "weapon_5": "5",
    "weapon_6": "6",
    "weapon_7": "7",
    "weapon_8": "8",
    "weapon_9": "9",
}

ALL_ACTIONS: list[str] = list(DEFAULT_BINDINGS.keys())

# 允许在设置界面重新绑定的键名黑名单：这些键已被界面本身占用
RESERVED_KEYS: set[str] = {"escape"}


class ConfigError(Exception):
    """配置文件读写失败。"""


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #
class UserConfig:
    """用户配置：键位绑定 + 少量界面偏好。"""

    def __init__(self, path: str = "", bindings: dict[str, str] | None = None,
                 prefs: dict[str, Any] | None = None):
        self.path = path
        self.bindings: dict[str, str] = dict(DEFAULT_BINDINGS)
        if bindings:
            for action, key in bindings.items():
                if action in DEFAULT_BINDINGS and isinstance(key, str) and key:
                    self.bindings[action] = key.lower()
        self.prefs: dict[str, Any] = dict(prefs or {})
        self.load_error = ""

    # -- 读写 -------------------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "UserConfig":
        """读取配置；文件不存在或损坏时返回默认配置（记录错误供 UI 提示）。"""
        cfg = cls(path)
        if not path or not os.path.isfile(path):
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            cfg.load_error = f"配置文件读取失败（已使用默认键位）：{exc}"
            return cfg
        if not isinstance(data, dict):
            cfg.load_error = "配置文件格式错误（已使用默认键位）"
            return cfg
        bindings = data.get("bindings")
        prefs = data.get("prefs")
        cfg = cls(path,
                  bindings=bindings if isinstance(bindings, dict) else None,
                  prefs=prefs if isinstance(prefs, dict) else None)
        if bindings is not None and not isinstance(bindings, dict):
            cfg.load_error = "配置文件 bindings 字段格式错误（已使用默认键位）"
        return cfg

    def save(self) -> None:
        """原子写入配置文件。"""
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            data = {
                "version": CONFIG_VERSION,
                "bindings": {a: self.bindings.get(a, DEFAULT_BINDINGS[a])
                             for a in ALL_ACTIONS},
                "prefs": self.prefs,
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise ConfigError(f"写入配置文件失败：{exc}") from None

    # -- 键位 -------------------------------------------------------------
    def key_for(self, action: str) -> str:
        """动作对应的键名（pygame.key.name 风格）。"""
        return self.bindings.get(action, DEFAULT_BINDINGS.get(action, ""))

    def set_key(self, action: str, key_name: str) -> bool:
        """重新绑定一个动作。返回是否成功（动作未知或键名非法时失败）。"""
        if action not in DEFAULT_BINDINGS:
            return False
        name = (key_name or "").strip().lower()
        if not name:
            return False
        self.bindings[action] = name
        return True

    def reset_keys(self) -> None:
        self.bindings = dict(DEFAULT_BINDINGS)

    def conflicts(self) -> dict[str, list[str]]:
        """找出被多个动作占用的键位，返回 {键名: [动作, ...]}。"""
        seen: dict[str, list[str]] = {}
        for action in ALL_ACTIONS:
            seen.setdefault(self.key_for(action), []).append(action)
        return {k: v for k, v in seen.items() if len(v) > 1}

    # -- 偏好 -------------------------------------------------------------
    def pref(self, key: str, default: Any = None) -> Any:
        return self.prefs.get(key, default)

    def set_pref(self, key: str, value: Any) -> None:
        self.prefs[key] = value

    @property
    def theme(self) -> str:
        """当前主题 ID（非法值回退默认）。"""
        theme = str(self.prefs.get("theme", DEFAULT_THEME))
        valid = {"tactical", "light", "deuteranopia", "protanopia"}
        return theme if theme in valid else DEFAULT_THEME

    def set_theme(self, theme_id: str) -> bool:
        """设置主题。返回是否成功（非法主题 ID 会被拒绝）。"""
        valid = {"tactical", "light", "deuteranopia", "protanopia"}
        if theme_id not in valid:
            return False
        self.prefs["theme"] = theme_id
        return True

    @property
    def sound_enabled(self) -> bool:
        """音效是否开启（默认开启）。"""
        return bool(self.prefs.get("sound", True))

    def set_sound(self, enabled: bool) -> None:
        self.prefs["sound"] = bool(enabled)

    @property
    def volume(self) -> float:
        """音量（0.0 ~ 1.0，默认 0.6）。"""
        try:
            v = float(self.prefs.get("volume", 0.6))
        except (TypeError, ValueError):
            return 0.6
        return max(0.0, min(1.0, v))

    def set_volume(self, value: float) -> None:
        self.prefs["volume"] = max(0.0, min(1.0, float(value)))

    @property
    def language(self) -> str:
        """界面语言（zh / en），非法值回退中文。"""
        lang = str(self.prefs.get("language", DEFAULT_LANGUAGE))
        return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    def set_language(self, lang: str) -> bool:
        if lang not in SUPPORTED_LANGUAGES:
            return False
        self.prefs["language"] = lang
        return True

    # -- v3.1.1 无障碍 ---------------------------------------------------- #
    @property
    def font_scale(self) -> str:
        """字号档位（standard / large / xlarge，非法值回退标准）。"""
        v = str(self.prefs.get("font_scale", DEFAULT_FONT_SCALE))
        return v if v in FONT_SCALE_IDS else DEFAULT_FONT_SCALE

    def set_font_scale(self, scale_id: str) -> bool:
        if scale_id not in FONT_SCALE_IDS:
            return False
        self.prefs["font_scale"] = scale_id
        return True

    @property
    def shape_cues(self) -> str:
        """形状标识模式（auto / on / off）。

        默认 auto：只在色盲主题下启用。配置里存的是模式而不是布尔值，
        因为「跟着主题走」和「强制打开」是两种不同的意图。
        """
        v = str(self.prefs.get("shape_cues", DEFAULT_SHAPE_MODE))
        return v if v in SHAPE_MODE_IDS else DEFAULT_SHAPE_MODE

    def set_shape_cues(self, mode: str) -> bool:
        if mode not in SHAPE_MODE_IDS:
            return False
        self.prefs["shape_cues"] = mode
        return True

    @property
    def ai_mode(self) -> str:
        """对手 AI 的驱动方式（rules / llm）。"""
        v = str(self.prefs.get("ai_mode", DEFAULT_AI_MODE))
        return v if v in AI_MODES else DEFAULT_AI_MODE

    def set_ai_mode(self, mode: str) -> bool:
        if mode not in AI_MODES:
            return False
        self.prefs["ai_mode"] = mode
        return True

    @property
    def llm_dir(self) -> str:
        """模型目录覆盖（空 = 自动探测 ai/）。给把模型放在别处的玩家用。"""
        return str(self.prefs.get("llm_dir", "") or "")

    @property
    def first_run(self) -> bool:
        """是否从未看过引导（用于主菜单高亮「快速上手」）。"""
        return not bool(self.prefs.get("seen_intro", False))

    def mark_intro_seen(self) -> None:
        self.prefs["seen_intro"] = True


def label_for(action: str) -> str:
    """动作 ID → 显示名（跟随界面语言）。

    **v3.0 起走语言包**：词条键是 ``key.<动作 id>``（如
    ``key.end_turn``）。找不到译文时回退到内置的中文标签，
    再回退到动作 id 本身。

    之前这个函数直接返回中文常量，导致设置面板里的键位列表
    在日语/德语界面下**全是中文**——而键位是玩家最需要看懂的部分。
    """
    from .i18n import tr

    key = f"key.{action}"
    localized = tr(key)
    if localized and localized != key:
        return localized
    return ACTION_LABELS.get(action) or SLOT_LABELS.get(action) or action


__all__ = [
    "CONFIG_VERSION", "CONFIG_FILENAME", "ACTION_LABELS", "SLOT_ACTIONS", "SLOT_LABELS",
    "DEFAULT_BINDINGS", "ALL_ACTIONS", "RESERVED_KEYS", "ConfigError", "UserConfig",
    "label_for",
]

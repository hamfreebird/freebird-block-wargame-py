"""键位映射：把 pygame 键码与配置中的动作绑定互相转换。

配置里存的是「pygame 键名」（如 "space"、"f1"、"a"），本模块负责：
* 反查：某个键码对应哪个动作（用于全局分发）；
* 正查：某个动作对应的键名（用于按钮上显示快捷键提示）；
* 捕获：把一次 KEYDOWN 事件转成可写入配置的键名。

放在 UI 层是因为它依赖 pygame；core/config.py 保持纯数据、可在无图形环境下测试。
"""
from __future__ import annotations

import pygame

from core.config import ALL_ACTIONS, UserConfig


def key_name(code: int) -> str:
    """pygame 键码 → 键名（小写）。"""
    try:
        return pygame.key.name(code).lower()
    except Exception:
        return ""


def key_code(name: str) -> int:
    """键名 → pygame 键码；非法名称返回 0。"""
    if not name:
        return 0
    try:
        return pygame.key.key_code(name.lower())
    except Exception:
        return 0


def build_reverse(cfg: UserConfig) -> dict[int, str]:
    """构造 {键码: 动作 ID} 反查表。

    若多个动作绑到同一个键，保留配置表中靠前的动作，避免同一按键
    触发两个互斥命令（例如把「结束回合」与「确认开火」都设成回车）。
    """
    out: dict[int, str] = {}
    for action in ALL_ACTIONS:
        code = key_code(cfg.key_for(action))
        if code and code not in out:
            out[code] = action
    return out


def hotkey_label(cfg: UserConfig, action: str) -> str:
    """返回用于界面展示的快捷键文本（大写、单字符友好）。"""
    name = cfg.key_for(action)
    if not name:
        return ""
    if name.startswith("f") and name[1:].isdigit():
        return name.upper()
    special = {
        "space": "空格", "return": "回车", "escape": "Esc", "tab": "Tab",
        "left": "←", "right": "→", "up": "↑", "down": "↓",
        "left shift": "LShift", "right shift": "RShift",
    }
    if name in special:
        return special[name]
    return name.upper()


def capture_key_name(event: pygame.event.Event) -> str:
    """把一次 KEYDOWN 事件转成键名；修饰键单独按下时返回空串。"""
    if event.key in (pygame.K_LSHIFT, pygame.K_RSHIFT, pygame.K_LCTRL, pygame.K_RCTRL,
                     pygame.K_LALT, pygame.K_RALT, pygame.K_LMETA, pygame.K_RMETA,
                     pygame.K_CAPSLOCK, pygame.K_NUMLOCK, pygame.K_SCROLLOCK):
        return ""
    return key_name(event.key)


__all__ = ["key_name", "key_code", "build_reverse", "hotkey_label", "capture_key_name"]

"""模组接口（v2.0）。

第三方 Python 模块可以往游戏里加新武器、新兵种，并订阅规则钩子。
模组放在用户数据目录的 `mods/` 下（每个 ``.py`` 文件即一个模组），
或安装目录的 ``data/mods/`` 下。

一个模组只需要导出 ``register(api)``::

    def register(api):
        api.add_weapon({
            "id": "laser_pod",
            "name": "机载激光吊舱",
            "kind": "atgm",
            ...
        })
        api.add_unit({
            "id": "my_tank",
            "name": "概念主战坦克",
            "domain": "land",
            "weapons": ["laser_pod"],
            ...
        })
        api.on("modify_damage", boost_laser)

**错误隔离**：单个模组抛异常只会记录一条错误，不会影响游戏启动或
其它模组——社区模组的质量参差不齐，不能让一个坏插件毁掉整局。

**这是 v3.0 官方 SDK 的原型**：这里先把注册机制跑通，v3.0 再把它固化
成有版本号、有文档、有兼容承诺的稳定 API。
"""
from __future__ import annotations

import importlib.util
import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- #
# 钩子事件
# --------------------------------------------------------------------------- #
#: 开火前——可修改单位状态（换目标、装填、撤出等）
EV_BEFORE_FIRE = "before_fire"
#: 开火后——收到开火结果
EV_AFTER_FIRE = "after_fire"
#: 伤害计算——返回新伤害值（或 None 表示不改）
EV_MODIFY_DAMAGE = "modify_damage"
#: 回合开始
EV_TURN_START = "turn_start"
#: 单位被摧毁
EV_UNIT_DESTROYED = "unit_destroyed"

HOOK_EVENTS = (
    EV_BEFORE_FIRE, EV_AFTER_FIRE, EV_MODIFY_DAMAGE,
    EV_TURN_START, EV_UNIT_DESTROYED,
)

#: 各钩子的期望参数个数（用于自检与文档）
HOOK_SIGNATURES = {
    EV_BEFORE_FIRE: "(state, attacker, target, weapon)",
    EV_AFTER_FIRE: "(state, attacker, target, weapon, result)",
    EV_MODIFY_DAMAGE: "(state, attacker, target, weapon, damage)",
    EV_TURN_START: "(state, turn)",
    EV_UNIT_DESTROYED: "(state, unit)",
}


class ModError(Exception):
    """模组自身的错误（注册期）。"""


# --------------------------------------------------------------------------- #
# 注册接口
# --------------------------------------------------------------------------- #
class ModAPI:
    """模组注册接口。

    模组只能通过它向游戏注册内容——不直接碰引擎内部结构，
    这样将来接口演进时兼容性有保障。
    """

    def __init__(self, mod_id: str):
        self.mod_id = mod_id
        self.weapons: list[dict[str, Any]] = []
        self.units: list[dict[str, Any]] = []
        self.hooks: dict[str, list[Callable]] = {}
        self.messages: list[str] = []

    # -- 内容注册 ---------------------------------------------------------
    def add_weapon(self, data: dict[str, Any]) -> None:
        """注册一件武器。``data`` 的字段与武器库 JSON 一致。"""
        if not isinstance(data, dict):
            raise ModError(f"{self.mod_id}: add_weapon 需要字典")
        if not data.get("id"):
            raise ModError(f"{self.mod_id}: 武器缺少 id")
        self.weapons.append(dict(data))

    def add_unit(self, data: dict[str, Any]) -> None:
        """注册一个兵种。``data`` 的字段与武器库 JSON 一致。"""
        if not isinstance(data, dict):
            raise ModError(f"{self.mod_id}: add_unit 需要字典")
        if not data.get("id"):
            raise ModError(f"{self.mod_id}: 兵种缺少 id")
        self.units.append(dict(data))

    # -- 钩子订阅 ---------------------------------------------------------
    def on(self, event: str, fn: Callable) -> None:
        """订阅一个规则钩子。"""
        if event not in HOOK_EVENTS:
            raise ModError(f"{self.mod_id}: 未知钩子 '{event}'"
                           f"（可用：{', '.join(HOOK_EVENTS)}）")
        if not callable(fn):
            raise ModError(f"{self.mod_id}: 钩子必须是可调用对象")
        self.hooks.setdefault(event, []).append(fn)

    # -- 便利方法 ---------------------------------------------------------
    def log(self, message: str) -> None:
        """让模组在加载时输出一条信息（会显示在启动日志里）。"""
        self.messages.append(str(message))


# --------------------------------------------------------------------------- #
# 模组注册表
# --------------------------------------------------------------------------- #
@dataclass
class LoadedMod:
    """一个已加载的模组。"""

    mod_id: str
    path: str
    api: ModAPI = field(default_factory=lambda: ModAPI(""))
    error: str = ""
    #: 已加载的模块对象——钩子函数绑定在它身上，
    #: 需要检查模组内部状态时应当用它而不是重新导入
    module: Any = None

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def weapons(self) -> int:
        return len(self.api.weapons)

    @property
    def units(self) -> int:
        return len(self.api.units)

    @property
    def hooks(self) -> int:
        return sum(len(v) for v in self.api.hooks.values())


class ModRegistry:
    """已加载的模组集合。"""

    def __init__(self, verbose: bool = False) -> None:
        self.mods: list[LoadedMod] = []
        self.errors: list[str] = []
        #: 是否把异常栈打印到 stderr（默认关——社区模组出错很常见，
        #: 不该在每次启动时刷屏）
        self.verbose = verbose

    # -- 加载 -------------------------------------------------------------
    def load_dir(self, directory: str) -> list[LoadedMod]:
        """加载一个目录下的全部模组。

        ``_`` 开头的文件视为私有模块，不加载；``.py`` 之外的文件忽略。
        """
        loaded: list[LoadedMod] = []
        if not os.path.isdir(directory):
            return loaded
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            self.errors.append(f"{directory}: {exc}")
            return loaded
        for name in names:
            if not name.endswith(".py") or name.startswith("_"):
                continue
            loaded.append(self.load_file(os.path.join(directory, name)))
        return loaded

    def load_file(self, path: str) -> LoadedMod:
        """加载单个模组文件。异常被隔离——坏模组不该拖垮整个游戏。"""
        mod_id = os.path.splitext(os.path.basename(path))[0]
        entry = LoadedMod(mod_id=mod_id, path=path)
        try:
            spec = importlib.util.spec_from_file_location(f"bwmod_{mod_id}", path)
            if spec is None or spec.loader is None:
                raise ModError("无法加载模块")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if register is None:
                raise ModError("模组缺少 register(api) 函数")
            api = ModAPI(mod_id)
            register(api)
            entry.api = api
            entry.module = module
        except Exception as exc:
            entry.error = f"{type(exc).__name__}: {exc}"
            self.errors.append(f"{mod_id}: {entry.error}")
            if self.verbose:
                traceback.print_exc()
        self.mods.append(entry)
        return entry

    def load_dirs(self, directories: Iterable[str]) -> None:
        for d in directories:
            self.load_dir(d)

    # -- 内容注入 ---------------------------------------------------------
    def apply_to_armory(self, armory) -> int:
        """把模组注册的武器与兵种注入武器库。

        返回注入的条目数。解析失败只跳过该条目并记录错误——
        一个写错的兵种不该让整个武器库加载失败。
        """
        from .loader import LoadError, _parse_unit, _parse_weapon

        count = 0
        for entry in self.mods:
            if not entry.ok:
                continue
            for wd in entry.api.weapons:
                try:
                    w = _parse_weapon(wd, f"mod:{entry.mod_id}")
                    armory.weapons[w.id] = w
                    count += 1
                except Exception as exc:
                    self.errors.append(f"{entry.mod_id}: 武器 {wd.get('id')} 无效：{exc}")
            for ud in entry.api.units:
                try:
                    t = _parse_unit(ud, 0, armory.weapons, armory.sensors)
                    armory.templates[t.id] = t
                    count += 1
                except Exception as exc:
                    self.errors.append(f"{entry.mod_id}: 兵种 {ud.get('id')} 无效：{exc}")
        return count

    # -- 钩子 -------------------------------------------------------------
    def hooks_for(self, event: str) -> list[Callable]:
        out: list[Callable] = []
        for entry in self.mods:
            if entry.ok:
                out.extend(entry.api.hooks.get(event, ()))
        return out

    def fire(self, event: str, *args: Any) -> Any:
        """触发一个钩子。

        普通事件忽略返回值；``modify_damage`` 依次应用每个返回值
        （非 None 即覆盖），因此多个模组可以叠加修正。
        """
        result: Any = None
        for fn in self.hooks_for(event):
            try:
                out = fn(*args)
                if out is not None:
                    result = out
            except Exception as exc:
                self.errors.append(f"{event} 钩子异常：{type(exc).__name__}: {exc}")
                if self.verbose:
                    traceback.print_exc()
        return result

    # -- 查询 -------------------------------------------------------------
    @property
    def ok_count(self) -> int:
        return sum(1 for m in self.mods if m.ok)

    def summary(self) -> str:
        if not self.mods:
            return "未加载任何模组"
        parts = [f"{m.mod_id}（武器 {m.weapons} · 兵种 {m.units} · 钩子 {m.hooks}）"
                 for m in self.mods]
        return "；".join(parts)


# --------------------------------------------------------------------------- #
# 全局注册表
# --------------------------------------------------------------------------- #
_REGISTRY: ModRegistry | None = None


def default_registry() -> ModRegistry:
    """全局默认注册表（未加载模组时为空）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ModRegistry()
    return _REGISTRY


def set_default_registry(reg: ModRegistry) -> None:
    global _REGISTRY
    _REGISTRY = reg


def default_mod_dirs() -> list[str]:
    """模组搜索目录（用户目录优先于安装目录）。"""
    dirs: list[str] = []
    try:
        from .paths import user_writable_dir
        dirs.append(os.path.join(user_writable_dir(), "mods"))
    except Exception:
        pass
    from .paths import data_root
    dirs.append(os.path.join(data_root(), "data", "mods"))
    return dirs


def load_all(directories: Iterable[str] | None = None) -> ModRegistry:
    """加载全部模组并设为默认注册表。"""
    dirs = list(directories) if directories is not None else default_mod_dirs()
    reg = ModRegistry()
    reg.load_dirs(dirs)
    set_default_registry(reg)
    return reg


__all__ = [
    "ModAPI", "ModRegistry", "ModError", "LoadedMod",
    "EV_BEFORE_FIRE", "EV_AFTER_FIRE", "EV_MODIFY_DAMAGE",
    "EV_TURN_START", "EV_UNIT_DESTROYED", "HOOK_EVENTS", "HOOK_SIGNATURES",
    "default_registry", "set_default_registry", "default_mod_dirs", "load_all",
]

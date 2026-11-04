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

**v3.0 官方 SDK**

本模块是 SDK 的**稳定层**。三条承诺：

1. **API 版本号**——:data:`API_VERSION` 随不兼容变更递增；
   模组可在 manifest 里声明它适配的版本范围（``"api": ">=1.0.0"``），
   不匹配时**拒绝加载并说清原因**，而不是运行时炸掉。
2. **公开 API 白名单**——:data:`PUBLIC_API` 列出模组可以用的方法名。
   内部方法（以 ``_`` 开头）不在承诺范围内，可随时变。
3. **弃用有预告**——标记弃用的 API 会先警告若干版本再移除，
   模组作者有时间迁移。

模组可以带一个 ``mod.json`` 声明元数据（版本、作者、依赖）——
见 :mod:`core.mod_manifest`。
"""
from __future__ import annotations

import importlib.util
import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .mod_manifest import (
    ModManifest,
    describe_spec,
    find_manifest,
    load_manifest,
    version_matches,
)

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

#: **SDK API 版本**。任何不兼容的接口变更都必须递增它。
#: 模组可在 manifest 里用 ``"api": ">=1.0.0,<2.0.0"`` 声明兼容范围。
API_VERSION = "1.0.0"

#: 模组可以调用的公开 API（白名单）。
#: 白名单之外的方法不算承诺——内部实现可随时调整。
PUBLIC_API = (
    "add_weapon", "add_unit", "on", "log",
    "add_sensor", "add_mount", "add_localization",
    "set_metadata", "require", "add_icon",
)

#: 弃用清单：{方法名: (弃用版本, 移除版本, 说明)}
DEPRECATED: dict[str, tuple[str, str, str]] = {}

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

    **公开方法**（见 :data:`PUBLIC_API`）是有版本承诺的稳定接口；
    以 ``_`` 开头的方法是内部实现，随时可能变，不要用。
    """

    def __init__(self, mod_id: str, manifest: ModManifest | None = None):
        self.mod_id = mod_id
        self.manifest = manifest
        self.weapons: list[dict[str, Any]] = []
        self.units: list[dict[str, Any]] = []
        self.sensors: list[dict[str, Any]] = []
        self.mounts: list[dict[str, Any]] = []
        self.hooks: dict[str, list[Callable]] = {}
        self.messages: list[str] = []
        self.localization: dict[str, dict[str, str]] = {}
        self.icons: dict[str, str] = {}
        self.metadata: dict[str, Any] = {}
        self.requires: dict[str, str] = {}

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

    def add_sensor(self, data: dict[str, Any]) -> None:
        """注册一个传感器。``data`` 的字段与武器库 JSON 一致。"""
        if not isinstance(data, dict):
            raise ModError(f"{self.mod_id}: add_sensor 需要字典")
        if not data.get("id"):
            raise ModError(f"{self.mod_id}: 传感器缺少 id")
        self.sensors.append(dict(data))

    def add_mount(self, data: dict[str, Any]) -> None:
        """注册一个挂载预设（武器 + 备弹组合）。"""
        if not isinstance(data, dict):
            raise ModError(f"{self.mod_id}: add_mount 需要字典")
        if not data.get("id"):
            raise ModError(f"{self.mod_id}: 挂载缺少 id")
        self.mounts.append(dict(data))

    def add_icon(self, unit_id: str, path: str) -> None:
        """为某个兵种指定图标文件（PNG / SVG）。

        图标放在模组目录的 ``icons/`` 下；路径相对于模组目录。
        """
        if not unit_id or not path:
            raise ModError(f"{self.mod_id}: add_icon 需要兵种 id 与图标路径")
        self.icons[str(unit_id)] = str(path)

    def add_localization(self, lang: str, entries: dict[str, str]) -> None:
        """为某个语言补充词条。

        ``entries`` 是 ``{键: 译文}``。模组可以用自己的键（如
        ``"my_mod.unit_desc"``），但**不能覆盖**游戏本体的键——
        那是留给游戏自己的。
        """
        if not lang or not isinstance(entries, dict):
            raise ModError(f"{self.mod_id}: add_localization 需要语言代码与词条字典")
        code = str(lang).strip().lower()
        self.localization.setdefault(code, {}).update(
            {str(k): str(v) for k, v in entries.items()})

    def set_metadata(self, key: str, value: Any) -> None:
        """设置模组自定义元数据（供其它模组或界面查询）。"""
        self.metadata[str(key)] = value

    def require(self, mod_id: str, spec: str = "") -> None:
        """声明对其它模组的依赖。

        加载器会检查这些模组是否已加载且版本匹配——
        不满足时**拒绝加载本模组**并说明原因。
        """
        if not mod_id:
            raise ModError(f"{self.mod_id}: require 需要模组 id")
        self.requires[str(mod_id)] = str(spec or "")

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

    # -- 诊断 -------------------------------------------------------------
    def public_api_used(self) -> list[str]:
        """本模组用到的公开 API 方法名（用于自检与文档）。"""
        used: list[str] = []
        if self.weapons:
            used.append("add_weapon")
        if self.units:
            used.append("add_unit")
        if self.sensors:
            used.append("add_sensor")
        if self.mounts:
            used.append("add_mount")
        if self.icons:
            used.append("add_icon")
        if self.localization:
            used.append("add_localization")
        if self.metadata:
            used.append("set_metadata")
        if self.requires:
            used.append("require")
        if self.hooks:
            used.append("on")
        return used



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
    #: 模组清单（没有 manifest 时为 None）
    manifest: ModManifest | None = None
    #: 非致命的警告（缺 manifest、API 即将移除等）
    warnings: list[str] = field(default_factory=list)

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

    @property
    def version(self) -> str:
        """模组版本（来自 manifest，缺失时为 "0.0.0"）。"""
        return self.manifest.version if self.manifest else "0.0.0"

    @property
    def display_name(self) -> str:
        """显示名（来自 manifest，缺失时用 id）。"""
        return self.manifest.display_name if self.manifest else self.mod_id

    def describe(self) -> str:
        """一行摘要（用于模组列表界面）。"""
        if self.manifest and self.manifest.ok:
            return self.manifest.summary()
        return f"{self.display_name} v{self.version}"


def resolve_entry(directory: str, entry_name: str) -> tuple[str, str]:
    """把 manifest 里的 ``entry`` 解析成**目录内**的绝对路径。

    返回 ``(路径, 错误说明)``；错误说明非空时路径无意义。

    **为什么要卡这一道**：``mod.json`` 是第三方写的，而 ``entry`` 会直接
    交给 importlib 执行。写 ``../../main.py``、``/etc/passwd`` 或
    ``C:\\Windows\\system32\\x.py`` 都能让它跑到模组目录之外去。
    眼下模组只从用户自己的 ``mods/`` 目录加载，风险不高；但这个
    「路径来自数据」的位置一旦被复用（导入他人打包的模组、从创意工坊
    拉取），就是现成的入口。

    判定用 :func:`os.path.realpath` 后再比公共前缀，顺带挡住符号链接
    跳出目录；空字符串回退到 ``main.py``。
    """
    name = (entry_name or "").strip() or "main.py"
    if os.path.isabs(name) or os.path.splitdrive(name)[0]:
        return "", f"模组入口不能是绝对路径：'{name}'"
    base = os.path.realpath(directory)
    try:
        target = os.path.realpath(os.path.join(base, name))
        inside = os.path.commonpath([base, target]) == base
    except (OSError, ValueError):
        return "", f"模组入口路径非法：'{name}'"
    if not inside:
        return "", f"模组入口跳出模组目录：'{name}'"
    return target, ""


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

        支持两种形态：

        * **单文件模组**——``mods/xxx.py``（v2.0 风格，向后兼容）；
        * **目录式模组**——``mods/xxx/main.py`` + 可选的 ``mods/xxx/mod.json``。
          目录式是 v3.0 SDK 的推荐形态：能带 manifest、图标与语言包。

        ``_`` 开头的文件视为私有模块，不加载；非 ``.py`` 文件忽略。
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
            if name.startswith("_") or name.startswith("."):
                continue
            full = os.path.join(directory, name)
            if os.path.isdir(full):
                # 目录式模组
                entry = self.load_package(full)
                if entry is not None:
                    loaded.append(entry)
            elif name.endswith(".py"):
                loaded.append(self.load_file(full))
        return loaded

    def load_package(self, directory: str) -> LoadedMod | None:
        """加载一个目录式模组（有 manifest 或入口文件）。

        目录里既没有 ``mod.json`` 也没有 ``main.py`` 时返回 ``None``
        ——那不是模组，只是个普通文件夹。
        """
        manifest = load_manifest(directory)
        entry_name = manifest.entry if (manifest and manifest.ok) else "main.py"
        entry_path, entry_err = resolve_entry(directory, entry_name)
        if entry_err:
            m = manifest or ModManifest(mod_id=os.path.basename(directory))
            mod_id = m.mod_id or os.path.basename(directory)
            entry = LoadedMod(mod_id=mod_id, path=entry_name, manifest=m,
                              error=entry_err)
            self.mods.append(entry)
            self.errors.append(f"{mod_id}: {entry.error}")
            return entry
        if manifest is None and not os.path.isfile(entry_path):
            return None
        if not os.path.isfile(entry_path):
            # 有 manifest 但入口缺失——记录错误
            m = manifest or ModManifest(mod_id=os.path.basename(directory))
            mod_id = m.mod_id or os.path.basename(directory)
            entry = LoadedMod(mod_id=mod_id, path=entry_path, manifest=m,
                              error=f"模组缺少入口文件 '{entry_name}'")
            self.mods.append(entry)
            self.errors.append(f"{mod_id}: {entry.error}")
            return entry
        mod_id = manifest.mod_id if (manifest and manifest.ok) \
            else os.path.basename(directory)
        entry = self._load_module(mod_id, entry_path, manifest, directory)
        self.mods.append(entry)
        return entry

    def load_file(self, path: str) -> LoadedMod:
        """加载单个模组文件。异常被隔离——坏模组不该拖垮整个游戏。"""
        mod_id = os.path.splitext(os.path.basename(path))[0]
        # 单文件模组也可能旁边放着 mod.json（放在父目录）
        manifest = find_manifest(os.path.dirname(path))
        entry = self._load_module(mod_id, path, manifest,
                                  os.path.dirname(path) if manifest else "")
        self.mods.append(entry)
        return entry

    def _load_module(self, mod_id: str, path: str,
                     manifest: ModManifest | None,
                     directory: str) -> LoadedMod:
        """实际的加载流程：读文件 → 执行 → 调 register(api) → 校验。"""
        entry = LoadedMod(mod_id=mod_id, path=path, manifest=manifest)
        try:
            spec = importlib.util.spec_from_file_location(f"bwmod_{mod_id}", path)
            if spec is None or spec.loader is None:
                raise ModError("无法加载模块")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if register is None:
                raise ModError("模组缺少 register(api) 函数")
            api = ModAPI(mod_id, manifest)
            register(api)
            entry.api = api
            entry.module = module
            # -- 校验：API 版本与依赖 --
            self._check_api_version(entry)
            self._check_requirements(entry)
        except Exception as exc:
            entry.error = f"{type(exc).__name__}: {exc}"
            self.errors.append(f"{mod_id}: {entry.error}")
            if self.verbose:
                traceback.print_exc()
        return entry

    def _check_api_version(self, entry: LoadedMod) -> None:
        """校验模组声明的 API 兼容范围。

        不匹配时**拒绝加载**（置 error）——继续加载只会在半个回合后
        以更难懂的方式崩掉。
        """
        m = entry.manifest
        if not m or not m.ok or not m.api:
            if m is None:
                entry.warnings.append("缺少 mod.json（v2.0 风格模组，无法校验兼容性）")
            return
        if version_matches(API_VERSION, m.api):
            return
        entry.error = (f"模组要求 API {describe_spec(m.api)}，"
                       f"当前游戏提供 {API_VERSION}")
        entry.warnings.append(entry.error)
        self.errors.append(f"{entry.mod_id}: {entry.error}")

    def _check_requirements(self, entry: LoadedMod) -> None:
        """校验依赖：前置模组是否已加载且版本匹配。"""
        reqs = dict(entry.api.requires)
        if entry.manifest and entry.manifest.ok:
            reqs.update(entry.manifest.requires)
        if not reqs:
            return
        loaded = {m.mod_id: m for m in self.mods if m.ok}
        for dep_id, spec in reqs.items():
            dep = loaded.get(dep_id)
            if dep is None:
                entry.error = f"缺少依赖模组 '{dep_id}'"
                entry.warnings.append(entry.error)
                self.errors.append(f"{entry.mod_id}: {entry.error}")
                return
            if spec and not version_matches(dep.version, spec):
                entry.error = (f"依赖 '{dep_id}' 版本不满足："
                               f"需要 {describe_spec(spec)}，实际 {dep.version}")
                entry.warnings.append(entry.error)
                self.errors.append(f"{entry.mod_id}: {entry.error}")
                return


    def load_dirs(self, directories: Iterable[str]) -> None:
        for d in directories:
            self.load_dir(d)

    # -- 内容注入 ---------------------------------------------------------
    def apply_to_armory(self, armory) -> int:
        """把模组注册的武器、兵种、传感器与挂载注入武器库。

        返回注入的条目数。解析失败只跳过该条目并记录错误——
        一个写错的兵种不该让整个武器库加载失败。
        """
        from .loader import LoadError, _parse_sensor, _parse_unit, _parse_weapon

        count = 0
        for entry in self.mods:
            if not entry.ok:
                continue
            # 传感器要先注入——兵种引用传感器
            for sd in entry.api.sensors:
                try:
                    # Sensor 对象本身**不带 id**——武器库按字符串索引它。
                    # 这里必须用原始字典里的 id，而不是从解析结果上取。
                    sid = str(sd.get("id", ""))
                    if not sid:
                        raise ModError("传感器缺少 id")
                    armory.sensors[sid] = _parse_sensor(sd, f"mod:{entry.mod_id}")
                    count += 1
                except Exception as exc:
                    self.errors.append(
                        f"{entry.mod_id}: 传感器 {sd.get('id')} 无效：{exc}")
                except Exception as exc:
                    self.errors.append(
                        f"{entry.mod_id}: 传感器 {sd.get('id')} 无效：{exc}")
            for wd in entry.api.weapons:
                try:
                    w = _parse_weapon(wd, f"mod:{entry.mod_id}")
                    armory.weapons[w.id] = w
                    count += 1
                except Exception as exc:
                    self.errors.append(
                        f"{entry.mod_id}: 武器 {wd.get('id')} 无效：{exc}")
            for ud in entry.api.units:
                try:
                    t = _parse_unit(ud, 0, armory.weapons, armory.sensors)
                    armory.templates[t.id] = t
                    count += 1
                except Exception as exc:
                    self.errors.append(
                        f"{entry.mod_id}: 兵种 {ud.get('id')} 无效：{exc}")
            for md in entry.api.mounts:
                try:
                    lo = self._parse_mount(md, entry.mod_id, armory)
                    if lo:
                        presets = armory.meta.setdefault("presets", {})
                        presets.setdefault("loadouts", []).append(lo)
                        count += 1
                except Exception as exc:
                    self.errors.append(
                        f"{entry.mod_id}: 挂载 {md.get('id')} 无效：{exc}")
        return count

    @staticmethod
    def _parse_mount(data: dict, mod_id: str, armory) -> dict:
        """解析模组注册的挂载预设。

        挂载的结构与武器库 ``presets.loadouts`` 一致：
        ``{"id": ..., "name": ..., "domain": ..., "weapons": [{"ref": id, "ammo": n}]}``。

        返回**原始字典**而不是对象——挂载预设只被编辑器消费，
        而编辑器读的是武器库 JSON 文档本身。这里直接写进
        ``armory.meta["presets"]["loadouts"]``，保持同构。
        """
        items = data.get("weapons")
        if not isinstance(items, list) or not items:
            raise ModError("挂载缺少 weapons")
        resolved = []
        for it in items:
            if not isinstance(it, dict):
                raise ModError("挂载的 weapons 元素必须是字典")
            wid = str(it.get("ref", ""))
            if wid not in armory.weapons:
                raise ModError(f"挂载引用了不存在的武器 '{wid}'")
            resolved.append({"ref": wid, "ammo": int(it.get("ammo", 0))})
        out = {
            "id": str(data.get("id", "")),
            "name": str(data.get("name", data.get("id", ""))),
            "weapons": resolved,
        }
        if data.get("domain"):
            out["domain"] = str(data["domain"])
        if data.get("desc"):
            out["desc"] = str(data["desc"])
        return out

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


# --------------------------------------------------------------------------- #
# 模组本地化与状态报告
# --------------------------------------------------------------------------- #
def apply_localization(registry: ModRegistry | None = None) -> int:
    """把模组注册的词条注入 i18n 表。返回注入的词条数。

    模组词条**不能覆盖游戏本体的键**——如果键已存在，
    模组的译文会被跳过并记入错误。这样社区模组不会意外
    把「开始作战」翻译成奇怪的东西。
    """
    from .i18n import tr  # noqa: F401  （确保 i18n 已初始化）

    reg = registry if registry is not None else default_registry()
    count = 0
    for entry in reg.mods:
        if not entry.ok:
            continue
        for lang, entries in entry.api.localization.items():
            for key, value in entries.items():
                # 只允许模组自己的命名空间（前缀与 mod_id 相关）
                if not key.startswith(f"{entry.mod_id}.") and \
                        not key.startswith("mod."):
                    reg.errors.append(
                        f"{entry.mod_id}: 词条 '{key}' 不在模组命名空间内，已跳过")
                    continue
                try:
                    from .i18n import register_mod_entry
                    if register_mod_entry(lang, key, value):
                        count += 1
                except Exception as exc:
                    reg.errors.append(
                        f"{entry.mod_id}: 词条 '{key}' 注册失败：{exc}")
    return count


def mod_report(registry: ModRegistry | None = None) -> list[str]:
    """生成模组状态报告（每行一条），用于设置界面或诊断输出。"""
    reg = registry if registry is not None else default_registry()
    lines: list[str] = []
    if not reg.mods:
        lines.append("未安装任何模组")
        return lines
    for entry in reg.mods:
        if entry.ok:
            bits = [f"{entry.describe()}"]
            bits.append(f"武器 {entry.weapons} · 兵种 {entry.units} · 钩子 {entry.hooks}")
            lines.append("  ".join(bits))
            for w in entry.warnings:
                lines.append(f"    ⚠ {w}")
        else:
            lines.append(f"{entry.mod_id}：{entry.error}")
    for err in reg.errors:
        lines.append(f"  · {err}")
    return lines



__all__ = [
    "ModAPI", "ModRegistry", "ModError", "LoadedMod",
    "EV_BEFORE_FIRE", "EV_AFTER_FIRE", "EV_MODIFY_DAMAGE",
    "EV_TURN_START", "EV_UNIT_DESTROYED", "HOOK_EVENTS", "HOOK_SIGNATURES",
    "API_VERSION", "PUBLIC_API", "DEPRECATED",
    "default_registry", "set_default_registry", "default_mod_dirs", "load_all",
    "apply_localization", "mod_report", "resolve_entry",
]

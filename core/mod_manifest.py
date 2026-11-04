"""模组清单（manifest）—— v3.0 官方 SDK。

一个模组目录可以带一个 ``mod.json``，声明自己的元数据：

.. code-block:: json

    {
      "id": "my_mod",
      "name": "我的模组",
      "version": "1.2.0",
      "author": "飞鸟",
      "description": "一句话说明这个模组做什么",
      "api": ">=1.0.0",
      "requires": {"other_mod": ">=1.0.0"},
      "entry": "main.py"
    }

**为什么要有 manifest**

v2.0 的模组原型只有一个 ``register(api)`` 函数——能跑，但社区没法
知道该模组是哪个版本、依赖什么、能不能和当前游戏兼容。
没有元数据，出问题时用户面对的是一句「模组加载失败」。

manifest 解决三件事：

1. **版本声明**——``version`` 让用户知道装的是哪一版；
2. **兼容承诺**——``api`` 声明它适配的 SDK 版本范围，
   游戏版本变化时能**提前告知**而不是运行时炸掉；
3. **依赖声明**——``requires`` 让加载器检查前置模组是否就位。

**缺 manifest 也能加载**（向后兼容 v2.0 模组），
但会记一条警告——那属于「能用但不规范」。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

#: manifest 文件名
MANIFEST_NAME = "mod.json"

#: manifest 的 schema 版本——将来 manifest 结构变了靠它区分
MANIFEST_SCHEMA = 1


class ManifestError(Exception):
    """manifest 解析错误。"""


@dataclass
class ModManifest:
    """一个模组声明的元数据。"""

    mod_id: str
    name: str = ""
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    #: 适配的 SDK 版本范围，如 ">=1.0.0"
    api: str = ""
    #: 依赖的其它模组：{mod_id: 版本范围}
    requires: dict[str, str] = field(default_factory=dict)
    #: 入口文件名（相对模组目录），默认 "main.py"
    entry: str = "main.py"
    #: 其它未识别的字段（原样保留，便于社区自定义）
    extra: dict[str, Any] = field(default_factory=dict)
    #: manifest 所在目录
    directory: str = ""
    #: manifest 解析是否成功（失败的 manifest 会带错误信息）
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def display_name(self) -> str:
        """显示用名称——没写 name 就退回 id。"""
        return self.name or self.mod_id

    def summary(self) -> str:
        """一行摘要（用于模组列表界面）。"""
        bits = [self.display_name, f"v{self.version}"]
        if self.author:
            bits.append(self.author)
        return " · ".join(bits)


# --------------------------------------------------------------------------- #
# 版本比较
# --------------------------------------------------------------------------- #
def parse_version(text: str) -> tuple[int, ...]:
    """解析版本号为可比较的元组。

    支持 ``1.2.3`` / ``1.2`` / ``1.2.3-beta``（后缀忽略）。
    解析不了的部分按 0 处理，不抛异常——
    版本号写错不该让整个模组加载失败。
    """
    out: list[int] = []
    for part in str(text or "").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def version_matches(version: str, spec: str) -> bool:
    """版本号是否落在规格范围内。

    支持的规格写法：

    * ``">=1.0.0"`` / ``">1.0.0"`` / ``"<=2.0.0"`` / ``"<2.0.0"``
    * ``"==1.2.0"`` / ``"1.2.0"``（等价于 ``==``）
    * ``"^1.2.0"``——兼容到下一个大版本（``1.x``）
    * ``"~1.2.0"``——兼容到下一个次版本（``1.2.x``）
    * 空字符串——不限制
    """
    v = parse_version(version)
    spec = (spec or "").strip()
    if not spec:
        return True

    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith(">="):
            if v < parse_version(part[2:]):
                return False
        elif part.startswith("<="):
            if v > parse_version(part[2:]):
                return False
        elif part.startswith(">"):
            if v <= parse_version(part[1:]):
                return False
        elif part.startswith("<"):
            if v >= parse_version(part[1:]):
                return False
        elif part.startswith("^"):
            base = parse_version(part[1:])
            if v < base:
                return False
            if base[0] > 0:
                if v[0] != base[0]:
                    return False
            elif base[1] > 0:
                if v[0] != 0 or v[1] != base[1]:
                    return False
        elif part.startswith("~"):
            base = parse_version(part[1:])
            if v < base:
                return False
            if v[0] != base[0] or v[1] != base[1]:
                return False
        else:
            target = part[2:] if part.startswith("==") else part
            if v != parse_version(target):
                return False
    return True


def describe_spec(spec: str) -> str:
    """把版本规格转成人话（用于错误提示）。"""
    spec = (spec or "").strip()
    if not spec:
        return "任意版本"
    names = {
        ">=": "≥", "<=": "≤", ">": ">", "<": "<",
        "==": "=", "^": "兼容", "~": "补丁级兼容",
    }
    out = spec
    for k, v in names.items():
        out = out.replace(k, v)
    return out


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_manifest(raw: dict[str, Any], directory: str = "") -> ModManifest:
    """把 manifest 字典解析成 :class:`ModManifest`。

    解析失败不抛异常——填入 ``error`` 并返回，
    让加载器把问题记录下来而不是中断整个启动。
    """
    if not isinstance(raw, dict):
        return ModManifest(mod_id="", directory=directory,
                           error="manifest 必须是 JSON 对象")

    mod_id = str(raw.get("id", "")).strip()
    if not mod_id:
        return ModManifest(mod_id="", directory=directory,
                           error="manifest 缺少 id")

    requires = raw.get("requires") or {}
    if not isinstance(requires, dict):
        return ModManifest(mod_id=mod_id, directory=directory,
                           error="requires 必须是对象")

    known = {"id", "name", "version", "author", "description", "api",
             "requires", "entry"}
    extra = {k: v for k, v in raw.items() if k not in known}

    return ModManifest(
        mod_id=mod_id,
        name=str(raw.get("name", "") or ""),
        version=str(raw.get("version", "") or "0.0.0"),
        author=str(raw.get("author", "") or ""),
        description=str(raw.get("description", "") or ""),
        api=str(raw.get("api", "") or ""),
        requires={str(k): str(v) for k, v in requires.items()},
        entry=str(raw.get("entry", "") or "main.py"),
        extra=extra,
        directory=directory,
    )


def load_manifest(directory: str) -> ModManifest | None:
    """读取模组目录下的 manifest。

    目录里没有 manifest 时返回 ``None``——
    那是合法的（v2.0 风格的单文件模组）。
    """
    path = os.path.join(directory, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return ModManifest(mod_id=os.path.basename(directory),
                           directory=directory,
                           error=f"manifest 解析失败：{exc}")
    return parse_manifest(raw, directory)


def find_manifest(start: str) -> ModManifest | None:
    """从给定路径向上找 manifest（最多三级目录）。

    允许模组放在 ``mods/`` 下的子目录里：
    ``mods/category/my_mod/main.py`` 也能找到
    ``mods/category/my_mod/mod.json``。
    """
    cur = os.path.abspath(start)
    for _ in range(3):
        m = load_manifest(cur)
        if m is not None:
            return m
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


__all__ = [
    "MANIFEST_NAME", "MANIFEST_SCHEMA", "ManifestError",
    "ModManifest", "parse_manifest", "load_manifest", "find_manifest",
    "parse_version", "version_matches", "describe_spec",
]

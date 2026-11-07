"""SDK 文档自检：`SDK.md` 与代码是否对得上。

文档与代码脱节是最难发现的一类问题——写得再详细，一旦新增了一个 API
而没写进文档，读者就永远不知道它存在；反过来文档里写了接口里没有的东西，
照着抄的人会撞墙。本脚本双向校验：

1. **目录 ↔ 标题一致**（复用 `tools/check_docs.py` 的锚点规则）；
2. **引用的符号真实存在**——反引号里的 `core.x.y` / `ui.x.y` 必须能解析；
3. **关键清单全覆盖**——公开 API、钩子事件、命令类型、条件 / 效果类型、
   事件类型、教学触发、枚举取值，代码里有几个文档里就得写几个；
4. **版本号自洽**——文档抬头写的适用版本必须等于 `core.version.GAME_VERSION`。

用法：python tools/check_sdk.py
"""
from __future__ import annotations

import importlib
import os
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.check_docs import _slug  # noqa: E402  锚点规则只维护一份

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "SDK.md")

FAILS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  [FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def read_doc() -> str:
    with open(DOC, encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# 1. 目录 ↔ 标题
# --------------------------------------------------------------------------- #
def check_toc(text: str) -> None:
    print("--- 目录与锚点 ---")
    headings = {}
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            headings.setdefault(_slug(m.group(2).strip()), m.group(2).strip())

    # 只校验「目录」区块（第一个二级标题之前）里的链接
    head, _, _ = text.partition("\n## 1.")
    links = re.findall(r"\]\(#([^)]+)\)", head)
    if not links:
        fail("目录里没有任何锚点链接")
        return
    bad = [l for l in links if l not in headings]
    if bad:
        for l in bad:
            fail(f"目录锚点找不到标题：#{l}")
    else:
        ok(f"目录中的 {len(links)} 个条目均指向真实标题")


# --------------------------------------------------------------------------- #
# 2. 引用的符号存在
# --------------------------------------------------------------------------- #
_SYMBOL = re.compile(r"`((?:core|ui)\.[a-z_]+(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")


def _resolve(path: str) -> bool:
    parts = path.split(".")
    # 从最长前缀开始尝试当模块名
    for i in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:i])
        try:
            obj = importlib.import_module(mod_name)
        except Exception:
            continue
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
            return True
        except AttributeError:
            return False
    return False


def check_symbols(text: str) -> None:
    print("--- 符号引用 ---")
    paths = sorted(set(_SYMBOL.findall(text)))
    if not paths:
        fail("文档里没有引用任何模块符号（是不是格式变了？）")
        return
    bad = [p for p in paths if not _resolve(p)]
    for p in bad:
        fail(f"文档引用了不存在的符号：{p}")
    if not bad:
        ok(f"引用的 {len(paths)} 个模块符号全部存在")


# --------------------------------------------------------------------------- #
# 3. 关键清单全覆盖
# --------------------------------------------------------------------------- #
#: 不该写进文档的项（内部实现 / 非用户可见）
_SKIP: set[str] = set()


def _require_mentions(text: str, items, label: str) -> None:
    missing = [i for i in items if i not in _SKIP and f"`{i}`" not in text
               and f"| `{i}`" not in text and i not in text]
    if missing:
        fail(f"{label} 有 {len(missing)} 项没写进文档：{', '.join(missing)}")
    else:
        ok(f"{label} 全部覆盖（{len(items)} 项）")


def check_coverage(text: str) -> None:
    print("--- 关键清单 ---")
    from core.commands import ALL_KINDS
    from core.events import EVENT_TYPES
    from core.hooks import CONDITION_TYPES, EFFECT_TYPES
    from core.mods import HOOK_EVENTS, PUBLIC_API
    from core.tutorial import EVENTS as TUTORIAL_EVENTS

    _require_mentions(text, list(PUBLIC_API), "公开 API（PUBLIC_API）")
    _require_mentions(text, list(HOOK_EVENTS), "钩子事件（HOOK_EVENTS）")
    _require_mentions(text, list(ALL_KINDS), "命令类型（ALL_KINDS）")
    _require_mentions(text, [c for c in CONDITION_TYPES if c not in ("all", "any", "not")],
                      "规则条件（CONDITION_TYPES）")
    _require_mentions(text, list(EFFECT_TYPES), "规则效果（EFFECT_TYPES）")
    _require_mentions(text, list(EVENT_TYPES), "想定事件（EVENT_TYPES）")
    _require_mentions(text, list(TUTORIAL_EVENTS), "教学触发（tutorial.EVENTS）")

    # 枚举取值：必须在「枚举取值」那张表里逐项出现
    from core import enums as E
    for name in ("Domain", "WeaponKind", "SensorKind", "Guidance", "Altitude",
                 "Detection", "Terrain", "Weather", "Facility", "ObjectiveKind",
                 "TimeOfDay"):
        cls = getattr(E, name, None)
        if cls is None:
            continue
        row = next((ln for ln in text.splitlines() if ln.startswith(f"| `{name}`")), "")
        if not row:
            fail(f"枚举 {name} 没有对应的取值表行")
            continue
        missing = [m.value for m in cls if m.value not in row]
        if missing:
            fail(f"枚举 {name} 的取值表缺：{', '.join(missing)}")
        else:
            ok(f"枚举 {name} 取值完整（{len(list(cls))} 个）")

    # UnitClass 在正文里以逗号串出现
    uc = [m.value for m in E.UnitClass]
    missing = [v for v in uc if v not in text]
    if missing:
        fail(f"UnitClass 缺：{', '.join(missing)}")
    else:
        ok(f"UnitClass 全部列出（{len(uc)} 个）")


# --------------------------------------------------------------------------- #
# 4. 版本号
# --------------------------------------------------------------------------- #
def check_version(text: str) -> None:
    print("--- 版本号 ---")
    from core.version import GAME_VERSION
    m = re.search(r"适用版本：\*\*(v[\d.]+)\*\*", text)
    if not m:
        fail("抬头找不到「适用版本：**vX.Y.Z**」")
        return
    doc_ver = m.group(1).lstrip("v")
    if doc_ver == GAME_VERSION:
        ok(f"文档适用版本与代码一致（v{doc_ver}）")
    else:
        fail(f"版本不一致：SDK.md 写 v{doc_ver}，代码是 v{GAME_VERSION}")


def main() -> int:
    if not os.path.isfile(DOC):
        print(f"[FAIL] 找不到 {DOC}")
        return 1
    text = read_doc()
    print(f"检查 {os.path.basename(DOC)}（{len(text.splitlines())} 行）\n")
    check_toc(text)
    check_symbols(text)
    check_coverage(text)
    check_version(text)
    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败")
        return 1
    print("✓ SDK 文档与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())

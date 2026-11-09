"""程序版本号与兼容性比较。

**为什么要有这个模块**：v3.2 之前版本号只存在于 README 徽章和 git 标签里，
程序自己不知道自己几岁。于是「这个想定包 / 回放是哪个版本做的」无从判断，
分享出去的文件只能靠人肉记忆兼容性。

这里给出唯一的代码内版本号，并配套一个**只比较三段数字**的比较函数。
不做完整的 semver（预发布标签、构建号都不需要）——多出来的规则
只会让「哪个版本更新」这种简单问题变得需要解释。
"""
from __future__ import annotations

import re

#: 与 README 徽章、AUDIT 基线、git 标签必须一致（`tools/check_docs.py` 会查）
GAME_VERSION = "3.3.1"

_NUM = re.compile(r"(\d+)")


def parse(text: str) -> tuple[int, ...]:
    """把版本串拆成数字元组；无法识别时返回空元组。

    只取前三位——`3.2` 与 `3.2.0` 视为同一版本。
    """
    parts = _NUM.findall(str(text or ""))[:3]
    return tuple(int(p) for p in parts)


def normalize(text: str) -> str:
    """补足到三段（`3.2` → `3.2.0`）；无法识别时原样返回。"""
    t = parse(text)
    if not t:
        return str(text or "")
    while len(t) < 3:
        t = t + (0,)
    return ".".join(str(x) for x in t)


def compare(a: str, b: str) -> int:
    """比较两个版本：a<b 返回 -1，相等 0，a>b 返回 1。

    缺位按 0 补齐（`3.2` vs `3.2.1` → -1），解析不出数字的按 0 处理。
    """
    ta = list(parse(a)) + [0, 0, 0]
    tb = list(parse(b)) + [0, 0, 0]
    for x, y in zip(ta[:3], tb[:3]):
        if x != y:
            return -1 if x < y else 1
    return 0


def is_newer(version: str, than: str = "") -> bool:
    """`version` 是否比 `than`（默认当前程序）更新。"""
    return compare(version, than or GAME_VERSION) > 0


def compat_note(version: str) -> str:
    """给出一句人话的兼容性说明；同版本返回空串。

    用于导入想定包 / 播放回放时提示——「文件更新」和「文件更旧」
    是两种完全不同的风险，不能都叫「版本不符」。
    """
    v = normalize(version)
    if not v or not parse(v):
        return ""
    cur = GAME_VERSION
    if compare(v, cur) == 0:
        return ""
    if compare(v, cur) > 0:
        return f"该文件由更新的版本 v{v} 制作（当前 v{cur}），内容可能无法完整读取"
    return f"该文件由更早的版本 v{v} 制作（当前 v{cur}），已按向后兼容方式读取"


__all__ = ["GAME_VERSION", "parse", "normalize", "compare", "is_newer",
           "compat_note"]

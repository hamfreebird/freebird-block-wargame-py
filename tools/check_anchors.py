"""静态扫描：找出所有传给 draw_text 的非法锚点。

pygame 的 Rect.get_rect 只接受 9 个锚点名，且**单轴锚点不可用**
（"top" / "bottom" / "centerx" / "centery" 单独传会抛 TypeError）。
写错锚点在开发期看不出来，只在玩家打开对应界面时才崩。

这个脚本用 AST 扫描所有 `draw_text(..., anchor="...")` 调用，
把非法锚点揪出来——比等玩家踩到崩溃强得多。

用法：python tools/check_anchors.py
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 与 ui/theme.py 的 _VALID_ANCHORS 保持一致
VALID_ANCHORS = frozenset({
    "topleft", "topright", "bottomleft", "bottomright",
    "midleft", "midright", "center",
})

# 历史踩过的坑——单独列出来，报告时高亮
KNOWN_MISTAKES = {
    "mid": "想居中请用 center；想左侧垂直居中用 midleft",
    "midtop": "pygame 无此锚点；用 draw_text_center_x()",
    "midbottom": "pygame 无此锚点；用 draw_text_center_x()",
    "centerx": "单轴锚点不可用；用 draw_text_center_x()",
    "centery": "单轴锚点不可用；用 draw_text_center_x()",
    "top": "单轴锚点不可用；用 topleft / top right 之类的完整锚点",
    "bottom": "单轴锚点不可用；用 bottomleft / bottomright",
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIRS = ("core", "ui", "tools")
SKIP_DIRS = {"__pycache__", ".git", ".workbuddy"}

# 负例豁免：测试里会**故意**用非法锚点验证 draw_text 能否拒绝它们。
# 这些行带有标记注释，扫描时跳过。
NEGATIVE_MARKER = "故意使用非法锚点"


def scan_file(path: str) -> list[tuple[int, str, str]]:
    """返回 [(行号, 锚点, 提示)]。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except (OSError, SyntaxError):
        return []
    lines = src.splitlines()
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name not in ("draw_text", "draw_text_center_x"):
            continue
        # 负例豁免：所在行（含上下文）带标记则跳过
        ctx = "\n".join(lines[max(0, node.lineno - 4):node.lineno + 1])
        if NEGATIVE_MARKER in ctx:
            continue
        for kw in node.keywords:
            if kw.arg != "anchor":
                continue
            if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
                continue        # 变量传入的锚点无法静态判断，交给运行时校验
            anchor = kw.value.value
            if anchor in VALID_ANCHORS:
                continue
            hint = KNOWN_MISTAKES.get(anchor, "锚点名非法")
            out.append((node.lineno, anchor, hint))
    return out


def main() -> int:
    problems: list[tuple[str, int, str, str]] = []
    for d in TARGET_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT)
                for lineno, anchor, hint in scan_file(path):
                    problems.append((rel, lineno, anchor, hint))

    if problems:
        print(f"发现 {len(problems)} 处非法锚点：")
        for rel, lineno, anchor, hint in problems:
            print(f"  [错误] {rel}:{lineno}  anchor={anchor!r}")
            print(f"          {hint}")
        return 1
    print(f"锚点检查通过：全部 draw_text 调用都使用合法锚点"
          f"（{len(VALID_ANCHORS)} 种：{', '.join(sorted(VALID_ANCHORS))}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

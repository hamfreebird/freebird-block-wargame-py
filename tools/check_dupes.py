"""重复定义守卫（v3.5.5）。

**为什么需要**：Python 允许同名函数在模块或类里重复定义，后定义的静默
覆盖前一个。`ruff` 默认规则不报，`check_names` 也看不出。v3.5.4 之前
`ui/mapview.py` 里积累了 5 组重复定义（`_weather_layer` 等），其中一处
新旧版本内容不同，导致「针对 CI 失败的修复」写进了旧版之后、从未生效，
而所有自检仍然全绿。

这类缺陷不会报错、不会让功能崩，只会让"已修"变成"未修"——必须由
静态检查兜住。

用法：python tools/check_dupes.py
"""
from __future__ import annotations

import ast
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 这些目录里的同名定义是刻意为之（测试替身、模组示例），不纳入检查。
SKIP_DIR = {"tests", "examples", "ai"}


def _scan(body: list, where: str, path: str, out: list) -> None:
    seen: dict[str, int] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prev = seen.get(node.name)
            if prev is not None:
                out.append((path, f"{where}{node.name}", prev, node.lineno))
            seen[node.name] = node.lineno
        elif isinstance(node, ast.ClassDef):
            _scan(node.body, f"{where}{node.name}.", path, out)


def main() -> int:
    bad: list = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in SKIP_DIR and d != "__pycache__"]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            scanned += 1
            _scan(tree.body, "", rel, bad)

    print(f"重复定义守卫：扫描 {scanned} 个文件")
    if bad:
        print(f"  [FAIL] 发现 {len(bad)} 处同名重复定义（后者会静默覆盖前者）：")
        for path, name, a, b in bad:
            print(f"    {path}:{b}  {name}  先定义于 {a}")
        return 1
    print("  [OK] 无同名重复定义")
    return 0


if __name__ == "__main__":
    sys.exit(main())

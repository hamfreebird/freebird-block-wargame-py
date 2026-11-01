"""打包脚本与环境兼容性自检（防止 CI 再踩 Windows 编码/路径坑）。

检查项：
1. `packaging/*.ps1` 的**可执行内容**必须全为 ASCII
   （Windows PowerShell 5.1 按系统 ANSI 代码页读无 BOM 文件，
    中文会被误解码，某些字节还会被当成 PowerShell 认可的引号 → 解析失败）。
2. `packaging/*.bat` 全文必须为 ASCII
   （cmd.exe 按 OEM 代码页读批处理文件，非 ASCII 会显示乱码，
    且在批处理中途 chcp 可能破坏后续行的解析）。
3. 所有会打印中文的 Python 脚本必须把 stdout/stderr 切到 UTF-8
   （Windows 运行器的 stdout 是 cp1252 管道，否则 UnicodeEncodeError）。
4. 打包脚本必须显式指定 PyInstaller 的 --distpath/--workpath
   （PyInstaller 默认写到 CWD 下的 dist/，与 packaging/dist 不一致）。

用法：
    python tools/check_scripts.py
退出码：0 = 通过，1 = 有问题
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "packaging")


def ps_executable_lines(path: str) -> list[tuple[int, str]]:
    """返回 .ps1 中非注释、非块注释内部的行。"""
    out: list[tuple[int, str]] = []
    in_block = False
    for i, line in enumerate(open(path, encoding="utf-8-sig"), 1):
        stripped = line.strip()
        if in_block:
            if "#>" in stripped:
                in_block = False
            continue
        if stripped.startswith("<#"):
            if "#>" not in stripped:
                in_block = True
            continue
        if stripped.startswith("#"):
            continue
        # 去掉行尾注释（简单处理：引号外的 # 之后忽略）
        code = []
        quote = None
        for ch in line:
            if quote:
                code.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
                code.append(ch)
                continue
            if ch == "#":
                break
            code.append(ch)
        out.append((i, "".join(code)))
    return out


def check_ps1(path: str, problems: list[str]) -> None:
    rel = os.path.relpath(path, ROOT)
    for line_no, code in ps_executable_lines(path):
        for ch in code:
            if ord(ch) > 127:
                problems.append(f"{rel}:{line_no} 可执行内容含非 ASCII 字符 {ch!r}")
                break
    raw = open(path, "rb").read()
    if not raw.startswith(b"\xef\xbb\xbf"):
        problems.append(f"{rel} 缺少 UTF-8 BOM（PowerShell 5.1 会按 ANSI 代码页读取）")


def check_bat(path: str, problems: list[str]) -> None:
    rel = os.path.relpath(path, ROOT)
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        for ch in line:
            if ord(ch) > 127:
                problems.append(f"{rel}:{i} 含非 ASCII 字符 {ch!r}（cmd.exe 会显示乱码）")
                break


def check_python_utf8_guard(path: str, problems: list[str]) -> None:
    """脚本会向标准输出写中文时，必须把 stdout 切到 UTF-8。

    只看**字符串字面量**（含 f-string 的片段），注释里的中文不算——
    否则会把仅有中文注释的库文件误报。
    """
    rel = os.path.relpath(path, ROOT)
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, rel)
    has_cn_output = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(ord(c) > 127 for c in node.value):
                has_cn_output = True
                break
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str) \
                        and any(ord(c) > 127 for c in part.value):
                    has_cn_output = True
                    break
    if has_cn_output and "_stream.reconfigure" not in src:
        problems.append(f"{rel} 会输出中文但未把 stdout 切到 UTF-8"
                        "（Windows 运行器 stdout 为 cp1252，会 UnicodeEncodeError）")


def check_pyinstaller_paths(path: str, problems: list[str]) -> None:
    rel = os.path.relpath(path, ROOT)
    src = open(path, encoding="utf-8").read()
    if "PyInstaller" not in src:
        return
    if "--distpath" not in src or "--workpath" not in src:
        problems.append(f"{rel} 未显式指定 --distpath/--workpath"
                        "（PyInstaller 默认输出到 CWD 下的 dist/）")


def main() -> int:
    problems: list[str] = []

    for name in sorted(os.listdir(PKG)):
        full = os.path.join(PKG, name)
        if name.endswith(".ps1"):
            check_ps1(full, problems)
        elif name.endswith(".bat"):
            check_bat(full, problems)
        elif name.endswith(".sh"):
            check_pyinstaller_paths(full, problems)

    # 所有打印中文的 Python 脚本
    targets = ["main.py"]
    tools_dir = os.path.join(ROOT, "tools")
    for name in sorted(os.listdir(tools_dir)):
        if name.endswith(".py"):
            targets.append(os.path.join("tools", name))
    targets.append(os.path.join("packaging", "make_portable.py"))
    for rel in targets:
        full = os.path.join(ROOT, rel)
        if os.path.exists(full):
            check_python_utf8_guard(full, problems)

    for p in problems:
        print(f"[不兼容] {p}")
    print(f"检查完成，发现 {len(problems)} 个问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

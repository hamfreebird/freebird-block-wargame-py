"""GitHub Actions 工作流语法自检（v3.5.5）。

**为什么需要**：改 CI 文件时把两行注释/命令插错了缩进，`run: |` 块被
打断，YAML 解析失败——GitHub 在 **0 秒**内就把整条流水线判为 failure，
但本地毫无感知，只能等 push 之后才发现。一次往返十分钟。

这个脚本做两件事：

1. 用 PyYAML 解析每个 workflow（若未安装则明确 SKIP，不假装通过）
2. 检查 `run: |` / `run: >` 块内的非空行缩进必须大于块起始缩进
   ——第 2 条不依赖 PyYAML，能抓住最常见的缩进错误

用法：python tools/check_workflows.py
"""
from __future__ import annotations

import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _check_block_scalars(path: str) -> list[str]:
    """`run: |` 块内的行缩进不得回退到块首行之下（但仍高于键缩进）。

    注意：缩进 ≤ `run:` 自身的行表示**块正常结束**，不是错误。只有
    「缩进比块首行浅、却又比 `run:` 键深」这种中间值才说明块被打断。
    真正的语法权威是后面的 PyYAML 解析，这里只是无 PyYAML 时的补充。
    """
    problems: list[str] = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("run: |") or stripped.startswith("run: >")):
            continue
        base = _indent(line)
        body_indent: int | None = None
        j = i + 1
        while j < len(lines):
            cur = lines[j]
            if cur.strip() == "":
                j += 1
                continue
            ind = _indent(cur)
            if ind <= base:
                break                     # 块结束（兄弟键或上层键）
            if body_indent is None:
                body_indent = ind
            elif ind < body_indent:
                problems.append(
                    f"{os.path.basename(path)}:{j + 1} `run: |` 块内缩进回退："
                    f"{cur.strip()[:50]}")
                break
            j += 1
    return problems


def main() -> int:
    if not os.path.isdir(WF_DIR):
        print("  [SKIP] 没有 .github/workflows 目录")
        return 0
    files = sorted(f for f in os.listdir(WF_DIR) if f.endswith((".yml", ".yaml")))
    if not files:
        print("  [SKIP] 没有 workflow 文件")
        return 0

    problems: list[str] = []
    for fn in files:
        problems.extend(_check_block_scalars(os.path.join(WF_DIR, fn)))

    try:
        import yaml  # type: ignore
    except ImportError:
        print("  [SKIP] 未安装 PyYAML，只做了块缩进检查")
    else:
        for fn in files:
            path = os.path.join(WF_DIR, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    yaml.safe_load(f)
            except Exception as exc:                  # noqa: BLE001
                problems.append(f"{fn} YAML 解析失败：{type(exc).__name__}: {exc}")

    print(f"工作流自检：{len(files)} 个文件")
    if problems:
        print(f"  [FAIL] {len(problems)} 个问题：")
        for p in problems:
            print(f"    {p}")
        return 1
    print("  [OK] workflow 语法与块缩进正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())

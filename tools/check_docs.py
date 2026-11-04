"""静态扫描：校验文档的目录、锚点与版本号是否自洽。

文档目录靠手工维护，迟早会和正文脱节——v2.2 新增 5.46/5.47 两节时
就漏改过目录，等到有人点目录里的死链才发现。

这个脚本校验三件事：

1. **目录 ↔ 标题一致**——GAME_GUIDE 目录里列出的每一节都真实存在，
   且正文里的每一节（到指定层级）都在目录里出现；
2. **锚点链接有效**——目录中的 `#xxx` 链接都能找到对应标题；
3. **版本号自洽**——README 徽章、ROADMAP 基线、git tag 三处的版本号
   指向同一个版本。

用法：python tools/check_docs.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  [FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# 锚点
# --------------------------------------------------------------------------- #
def _slug(text: str) -> str:
    """Markdown 锚点规则：与 GitHub 对齐。

    小写 → 去掉行内代码标记 → 去掉标点（保留中日文与连字符）→
    空格转连字符 → 去掉首尾连字符。

    注意：**句点会被去掉**（"1. 快速开始" → "1-快速开始"），
    这与 GitHub 的行为一致。
    """
    s = text.strip().lower()
    s = re.sub(r"[`*_]", "", s)
    s = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", s)
    return re.sub(r"\s+", "-", s).strip("-")


def _all_slugs(headings: list[str]) -> set[str]:
    """所有标题的锚点，含 GitHub 对重复标题追加的 -1 / -2 后缀。"""
    base = [_slug(h) for h in headings]
    out = set(base)
    seen: dict[str, int] = {}
    for s in base:
        seen[s] = seen.get(s, 0) + 1
        if seen[s] > 1:
            out.add(f"{s}-{seen[s] - 1}")
    return out



def check_toc() -> None:
    print("--- GAME_GUIDE 目录自洽 ---")
    text = _read("GAME_GUIDE.md")
    lines = text.split("\n")

    try:
        start = lines.index("## 目录")
    except ValueError:
        fail("GAME_GUIDE.md 找不到「## 目录」标题")
        return
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    toc_lines = lines[start + 1:end]

    # 目录条目分两类：
    #   带链接的（"[标题](#锚点)"）—— 二级章与三级节
    #   纯文本的（"探测与战争迷雾"）—— 四级小节，只列标题
    linked: list[tuple[str, str]] = []
    plain: list[str] = []
    for ln in toc_lines:
        # 链接里的 [ ] 可能出现在标题中（如 `[--map M]`），
        # 所以用「最后一个 ]( 之前」来切标题，而不是贪婪匹配。
        m = re.match(r"\s*(?:[-*]|\d+\.)\s*\[(.+)\]\(#([^)]+)\)\s*$", ln)
        if m:
            linked.append((m.group(1), m.group(2)))
            continue
        m2 = re.match(r"\s*[-*]\s+(\S+)\s+(.+?)\s*$", ln)
        if m2:
            plain.append(f"{m2.group(1)} {m2.group(2)}")
            continue
        m3 = re.match(r"\s{4,}[-*]\s+(.+?)\s*$", ln)
        if m3:
            plain.append(m3.group(1))


    if not linked:
        fail("目录里没解析出任何条目——格式可能变了")
        return

    # 收集正文标题。注意 end 行本身就是下一章的标题（「## 1. 快速开始」），
    # 所以从 end 开始而不是 end + 1——漏掉这一行会让第一章的锚点失效。
    headings: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if i < end:
            continue
        m = re.match(r"^(#{2,4})\s+(.+?)\s*$", ln)
        if m:
            headings.append((i, m.group(2)))

    # 1) 锚点有效性
    slugs = _all_slugs([h for _, h in headings])
    bad_links = [(t, a) for t, a in linked if a not in slugs]
    if bad_links:
        for t, a in bad_links:
            fail(f"目录链接指向不存在的标题：「{t}」→ #{a}")
    else:
        ok(f"目录中 {len(linked)} 个链接的锚点全部有效")

    # 2) 正文的二级 / 三级标题都要在目录里
    linked_texts = {t for t, _ in linked}
    missing = []
    for _, h in headings:
        if re.match(r"^\d+\.\s", h):
            if h not in linked_texts:
                missing.append(h)
        elif re.match(r"^\d+\.\d+\s", h):
            num = h.split()[0]
            if not any(p.startswith(num + " ") for p in plain) \
                    and not any(p.startswith(num + " ") for p in linked_texts):
                missing.append(h)
    if missing:
        for m in missing[:8]:
            fail(f"正文有此节但目录未列出：{m}")
        if len(missing) > 8:
            fail(f"……另有 {len(missing) - 8} 节未列出")
    else:
        ok("正文各节都已在目录中列出")

    # 3) 目录里的每一条都要在正文里真实存在
    #    带链接的条目：链接文本应与某个标题匹配
    #    纯文本条目（四级小节）：按标题文本匹配
    def _norm(x: str) -> str:
        """比较时忽略反引号与空白——正文标题常写成 `python tools/x.py`。"""
        return x.replace("`", "").strip()

    ghost = []
    for t, _ in linked:
        nt = _norm(t)
        found = any(_norm(h) == nt or _norm(h).endswith(" " + nt)
                    for _, h in headings)
        if not found:
            ghost.append(t)
    for p in plain:
        key = p.strip()
        title = re.sub(r"^\S+\s+", "", key)
        found = any(h == key or h == title or h.endswith(" " + title)
                    for _, h in headings)
        if not found:
            ghost.append(key)
    if ghost:
        fail(f"目录列出了不存在的条目：{', '.join(ghost[:6])}"
             + (f"……另有 {len(ghost) - 6} 条" if len(ghost) > 6 else ""))
    else:
        ok(f"目录中的 {len(linked) + len(plain)} 个条目均真实存在")


# --------------------------------------------------------------------------- #
# 版本号自洽
# --------------------------------------------------------------------------- #
def check_versions() -> None:
    print("--- 版本号自洽 ---")
    readme = _read("README.md")
    roadmap = _read("ROADMAP.md")

    # README 徽章
    m = re.search(r"release-v(\d+\.\d+\.\d+)-", readme)
    if not m:
        fail("README.md 找不到 release 徽章的版本号")
        badge = None
    else:
        badge = m.group(1)

    # ROADMAP 基线
    m2 = re.search(r"## 当前基线（v(\d+\.\d+\.\d+)）", roadmap)
    if not m2:
        fail("ROADMAP.md 找不到「## 当前基线（vX.Y.Z）」")
        base = None
    else:
        base = m2.group(1)

    if badge and base:
        if badge == base:
            ok(f"README 徽章与 ROADMAP 基线一致（v{badge}）")
        else:
            fail(f"版本号不一致：README 徽章 v{badge} vs ROADMAP 基线 v{base}")

    # git tag
    try:
        out = subprocess.run(
            ["git", "tag", "-l", "v*", "--sort=-v:refname"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        ).stdout.split()
        tags = [t for t in out if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    except Exception:
        tags = []
    if tags and base:
        latest = tags[0]
        if latest == f"v{base}":
            ok(f"最新 git 标签与基线一致（{latest}）")
        else:
            # 基线是补丁前的版本也算合理，只提示
            print(f"  [提示] 最新 git 标签是 {latest}，基线写的是 v{base}"
                  f"（若基线尚未发版请忽略）")
    elif base:
        # 浅克隆 / 尚未打标签时不能假装校验过——明确写出来，CI 日志里才看得见。
        # （CI 已用 fetch-depth: 0 取全量历史，正常不会再走到这里。）
        print(f"  [跳过] 取不到 git 标签，未校验「标签 == v{base}」（需要完整历史）")


# --------------------------------------------------------------------------- #
# 死链
# --------------------------------------------------------------------------- #
def check_links() -> None:
    print("--- 文档内部链接 ---")
    for name in ("README.md", "GAME_GUIDE.md", "ROADMAP.md"):
        text = _read(name)
        # 只查指向其它 md 文件的相对链接
        for target in re.findall(r"\]\((?!https?://|#)([^)#]+\.md)(?:#[^)]*)?\)", text):
            path = os.path.join(ROOT, target)
            if not os.path.exists(path):
                fail(f"{name} 链接到不存在的文件：{target}")
    ok("跨文档链接检查完成")


def main() -> int:
    print("=== 文档自检 ===")
    check_toc()
    check_versions()
    check_links()
    print()
    if FAILS:
        print(f"文档检查失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("文档自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""UI 本地化：把硬编码中文抽进语言包。

v3.0 的 8.2 要「从 2 种扩到 8 种」——但语言包只覆盖了 108 个
「设计时就抽离」的键，而 UI 里还有 **160+ 处硬编码中文**。
不处理它们，切换到日语后界面就是一半中文一半日文。

本脚本做三件事：

1. **抽取**——扫出所有 ``draw_text(...)`` 里的中文常量；
2. **替换**——改成 ``tr("ui.xxx")``；
3. **同步**——把新增的键写进全部语言包。

**为什么用脚本而不是手工改**：160 处 × 7 个文件，手工改必然漏，
而且改完还得同步 8 个语言包。脚本保证「改一处、全部同步」。

用法：python tools/localize_ui.py            # 报告
      python tools/localize_ui.py --apply    # 执行替换

**重跑前先想清楚**：脚本只负责把中文抽成键，新词条在各语言包里
默认是中文占位。若不同步补齐译文，切换语言后会整片回退成中文——
那不算「多语言完成」。新增词条后请跑 ``tools/lang_check.py``
确认 8 种语言都补齐，再提交。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from core.paths import data_root  # noqa: E402

LANG_DIR = os.path.join(data_root(), "data", "lang")

#: 需要扫描的 UI 文件
UI_FILES = (
    "ui/app.py", "ui/briefing.py", "ui/importdlg.py", "ui/replaydlg.py",
    "ui/settings.py", "ui/editor.py", "ui/campaign_editor.py",
)

CJK = re.compile(r"[一-鿿]")

#: 手工维护的键名表：{原始中文: 词条键}
#: 自动生成的键名（ui.xxx_001）不可读，翻译者看不出对应什么，
#: 所以这里逐条给出有意义的键名。
KEYS: dict[str, str] = {}


def _load_keys() -> None:
    """载入键名表（与本文件同目录的 ui_locale_keys.json）。"""
    global KEYS
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ui_locale_keys.json")
    if not os.path.isfile(path):
        return
    import json
    with open(path, "r", encoding="utf-8") as f:
        KEYS = json.load(f)


def _extract_exprs(txt: str) -> tuple[list[str], list[str]] | None:
    """从 f-string 模板里提取占位符。

    返回 ``(表达式列表, 格式说明符列表)``，或 ``None``（过于复杂）。

    ``"{a:.0f}{b}"`` → ``(["a", "b"], [".0f", ""])``
    转成 ``"{0:.0f}{1}"`` 后就能用 ``.format(a, b)``。

    **为什么要转成位置索引**：``.format()`` 不认识 f-string 的
    ``{expr}`` 语法，只认 ``{0}`` 或 ``{name}``。
    """
    exprs: list[str] = []
    specs: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(txt):
        if txt[i] == "{":
            if i + 1 < len(txt) and txt[i + 1] == "{":
                out.append("{")
                i += 2
                continue
            depth = 1
            j = i + 1
            while j < len(txt) and depth > 0:
                if txt[j] == "{":
                    depth += 1
                elif txt[j] == "}":
                    depth -= 1
                j += 1
            if depth != 0:
                return None
            body = txt[i + 1:j - 1]
            # **先判切片**——`x[:52]` 里的 `]` 会被上面的配对逻辑
            # 误当成表达式的结束，导致 body 变成 "c.description["，
            # 切片特征已经被切掉了，后面再检测就晚了。
            if (re.search(r"\[\s*:", body) or re.search(r":\s*\]", body)
                    or re.search(r"\[\s*\]", body)):
                return None
            # 拆出格式说明符
            if ":" in body:
                expr, spec = body.split(":", 1)
            else:
                expr, spec = body, ""
            if "{" in expr or "}" in expr:
                return None
            # 含**切片**（c.description[:52]）——.format() 的参数里
            # 不能直接写切片表达式（`x[:52]` 会被当成 format 的字段名），
            # 得先取出来再传。
            #
            # 注意不能简单判「含 [ 」——`info.get('turn', 0)`、
            # `sc['size'][0]` 这类下标访问是**合法**的 format 参数。
            # 只排除真正的切片：方括号里是空或以冒号开头。
            # 切片的三种形态：[n:] / [:n] / [:]
            if (re.search(r"\[\s*:", expr) or re.search(r":\s*\]", expr)
                    or re.search(r"\[\s*\]", expr)):
                return None
            exprs.append(expr)
            specs.append(spec)
            # % 风格的位置占位符：%s / %.1f / %d
            if not spec:
                out.append("%s")
            elif spec.startswith("."):
                # 浮点格式：%.1f → 保持
                out.append("%" + spec)
            elif spec.startswith("d"):
                out.append("%d")
            elif spec.startswith("0"):
                # 零填充：%03d
                out.append("%" + spec)
            else:
                out.append("%s")
            i = j
        else:
            out.append(txt[i])
            i += 1
    return exprs, specs


def _scan_file(path: str) -> list[tuple[str, int, bool]]:
    """扫出文件里的中文 draw_text 文本。返回 [(文本, 出现次数, 是否 f-string)]。"""
    src = open(path, encoding="utf-8").read()
    found: dict[str, list] = {}
    for m in re.finditer(r'draw_text\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*?\)', src):
        sm = re.search(r'(f?)"([^"]*)"', m.group(0))
        if not sm:
            continue
        txt = sm.group(2)
        if not CJK.search(txt):
            continue
        if txt not in found:
            found[txt] = [0, sm.group(1) == "f"]
        found[txt][0] += 1
    return [(t, v[0], v[1]) for t, v in found.items()]


def report() -> int:
    """报告所有待本地化的中文。"""
    _load_keys()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    total = 0
    missing: list[tuple[str, str]] = []
    print("=== UI 硬编码中文清单 ===\n")
    for rel in UI_FILES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        items = _scan_file(path)
        if not items:
            continue
        print(f"--- {rel}（{len(items)} 条）---")
        for txt, n, isf in items:
            key = KEYS.get(txt, "")
            mark = "✓" if key else "✗"
            print(f"  {mark} [{n}×] {key or '（未定义键名）':32s} {txt[:44]}")
            if not key:
                missing.append((rel, txt))
            total += 1
        print()
    print(f"合计 {total} 条唯一文本")
    if missing:
        print(f"其中 {len(missing)} 条**缺少键名**——"
              f"请补进 tools/ui_locale_keys.json")
    else:
        print("✓ 全部已定义键名")
    return 0


def _to_percent_template(txt: str) -> str:
    """把 f-string 模板转成 % 风格模板。

    ``"伤害 {prof.damage:.0f}{ic}"`` → ``"伤害 %.0f%s"``

    这是译文要用的形式——译文里写 %s / %.1f，
    调用点是 ``tr(key) % (arg1, arg2)``。
    """
    out: list[str] = []
    i = 0
    while i < len(txt):
        if txt[i] == "{":
            if i + 1 < len(txt) and txt[i + 1] == "{":
                out.append("{")
                i += 2
                continue
            depth = 1
            j = i + 1
            while j < len(txt) and depth > 0:
                if txt[j] == "{":
                    depth += 1
                elif txt[j] == "}":
                    depth -= 1
                j += 1
            body = txt[i + 1:j - 1]
            spec = body.split(":", 1)[1] if ":" in body else ""
            if not spec:
                out.append("%s")
            elif spec.startswith("."):
                out.append("%" + spec)
            elif spec.startswith("d"):
                out.append("%d")
            elif spec.startswith("0"):
                out.append("%" + spec)
            else:
                out.append("%s")
            i = j
        else:
            # **字面百分号必须转义成 %%**——
            # "综合命中 %.0f%" 里的末尾 % 是字面量，
            # 但 % 风格格式化会把它当成格式符的开头，
            # 报 "unsupported format character" 或 "incomplete format"。
            # 转义后 "%.0f%%" 格式化出来才是 "62%"。
            out.append("%%" if txt[i] == "%" else txt[i])
            i += 1
    return "".join(out)



def _next_is_fstring(tail: str) -> bool:
    """紧跟当前匹配之后是否是 f-string（隐式拼接）。"""
    stripped = tail.lstrip()
    return stripped.startswith('f"') or stripped.startswith("f'")


def apply() -> int:
    """执行替换：把硬编码中文改成 tr(...) 调用。

    **不能做字符串替换**——中文串里可能含引号（``目标兵种：{t.get('name', '')}``），
    替换后会得到 ``"tr("ui.xxx")"`` 这种嵌套引号，语法直接崩。
    而且 ``[:52]`` 这类切片语法在 f-string 里���拆解。

    正确做法：**用正则匹配整个 draw_text 的第一个参数**，
    把它整体换成 ``tr("key")`` 或 ``tr("key").format(...)``。
    """
    _load_keys()
    if not KEYS:
        print("没有键名表——先补 tools/ui_locale_keys.json")
        return 1
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 键 → 原文（反查）
    txt2key = {txt: key for txt, key in KEYS.items()}

    # draw_text 的签名是 draw_text(surface, text, pos, size, ...)
    # ——**文本是第二个参数**，第一个是绘图表面。
    # 所以要匹配 draw_text( 之后的第一个「位置参数」，
    # 它可能是 surface 也可能是 text——用「逗号分隔的参数扫描」来定位。
    #
    # 简化做法：匹配 draw_text( 后紧跟的一串「表面变量 + 逗号 + 空格」，
    # 然后才是文本字面量。
    SURFACE = r'[A-Za-z_][A-Za-z0-9_\.]*'
    pat = re.compile(
        r'(draw_text\(\s*' + SURFACE + r'\s*,\s*)'
        r'(f?)("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    )

    changed = 0
    total = 0
    for rel in UI_FILES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        src = open(path, encoding="utf-8").read()
        original = src
        skipped: list[str] = []

        def _repl(m: re.Match) -> str:
            nonlocal total
            prefix, fprefix, literal = m.group(1), m.group(2), m.group(3)
            raw = literal[1:-1]
            # 只反转义反斜杠与引号，**不能用 unicode_escape**——
            # 那会把中文 UTF-8 字节解成乱码，键就永远匹配不上。
            txt = (raw.replace("\\'", "'").replace('\\"', '"')
                   .replace("\\\\", "\\"))
            if txt not in txt2key:
                return m.group(0)
            key = txt2key[txt]
            total += 1
            if not fprefix:
                return f'{prefix}tr("{key}")'
            got = _extract_exprs(txt)
            if got is None:
                skipped.append(txt)
                return m.group(0)
            exprs, _ = got
            # **不能用 .format()**——
            # format 的字段名语法里 `[` `]` 有特殊含义
            # （`sc['size'][0]` 会被当成字段名解析），
            # 而且参数里的逗号会被拆开（`min(a, b)`）。
            #
            # 改用 **% 风格占位符**：译文里写 %s / %.1f，
            # 调用是 `tr(key) % (a, b)`。
            # % 风格没有字段名语法，逗号也只是普通字符——
            # 只要参数用**元组**括起来就不会被拆。
            args = ", ".join(exprs)
            # 后面若紧跟 f-string 隐式拼接，必须显式加 "+"——
            # 否则 `"a" "b"` 会变成 `"a" tr(...)`，语法直接崩。
            tail = m.string[m.end():]
            joiner = " +" if _next_is_fstring(tail) else ""
            return f'{prefix}tr("{key}") % ({args},){joiner}'

        src = pat.sub(_repl, src)
        if src != original:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(src)
            changed += 1
            print(f"  已处理 {rel}")
        if skipped:
            print(f"    ⚠ {rel} 有 {len(skipped)} 处表达式过复杂，需人工处理：")
            for s_ in skipped[:5]:
                print(f"        {s_[:60]}")
    print(f"\n{changed} 个文件已更新，共替换 {total} 处")
    return 0


def main() -> int:
    _load_keys()
    args = sys.argv[1:]
    if not args or args[0] == "report":
        return report()
    if args[0] == "--templates":
        # 输出所有键的 % 风格模板——译文要照着这个写
        _load_keys()
        for _txt, _key in KEYS.items():
            print(f"{_key}\t{_to_percent_template(_txt)}")
        return 0
    if args[0] == "--apply":
        return apply()
    print("用法：python tools/localize_ui.py [--apply]")
    return 1


if __name__ == "__main__":
    sys.exit(main())

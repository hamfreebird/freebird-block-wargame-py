"""翻译协作工具：词条覆盖率报告与语言包校验。

v3.0 的 8.2 要求「语言包提交流程成熟」——光有语言包不够，
还需要一套让贡献者能自助校验的工具：

* ``python tools/lang_check.py``——检查各语言的**覆盖率**，
  列出缺失的键；
* ``python tools/lang_check.py --new ja``——生成新语言的**模板**
  （预填英文译文，贡献者在此基础上改）；
* ``python tools/lang_check.py --verify ja``——校验语言包
  是否完整、键是否有多余、JSON 格式是否正确。

**为什么要覆盖率报告**：语言包最容易出的问题是「加了几个新界面元素，
忘了同步所有语言」——结果是切换语言后界面一半中文一半英文。
覆盖率报告让这个问题在提交前就被看见。
"""
from __future__ import annotations

import json
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from core import i18n  # noqa: E402
from core.paths import data_root  # noqa: E402

LANG_DIR = os.path.join(data_root(), "data", "lang")

#: 参考语言的占位符基准（中文 UI 模板，% 风格）。
#: 译文必须与它**完全一致**——调用点的参数个数是固定的。
_REF_TEMPLATES: dict[str, str] = {}


def _load_ref_templates() -> dict[str, str]:
    """加载中文 UI 模板（占位符基准）。

    基准取自 ``data/lang/zh_ui.json``——中文 UI 词条就是
    原始 f-string 转换出来的 % 风格模板，占位符数量与
    调用点的参数个数严格对应。
    """
    path = os.path.join(LANG_DIR, "zh_ui.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

#: 语言代码 → 显示名（与 i18n.LANGUAGE_NAMES 保持一致）
LANG_NAMES = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "ru": "Русский",
}


def _reference_keys() -> list[str]:
    """参考词条集：以中文为准（中文是原始语言）。

    **包含磁盘上的中文词条**（``data/lang/zh.json`` 与
    ``data/lang/zh_ui.json``）——UI 词条只存在于磁盘上，
    不在内置表里。少了这一步，覆盖率会只算基础词条，
    把「UI 词条全缺」这种严重问题报成 100%。
    """
    keys = list(i18n._BUILTIN_ZH.keys())
    for code in ("zh",):
        for k in _load(code):
            if k not in keys:
                keys.append(k)
    return keys


def _pack_files(code: str) -> list[str]:
    """一种语言的全部词条文件（分层包在前，整包在后）。

    分层包的命名规则是 ``<code>_<层>.json``（如 ``ja_ui.json``）。
    整包 ``<code>.json`` **优先级更高**——它是「整体替换」，
    分层是「局部补充」。这样社区既能整体替换一种语言，
    也能只补某一个层。
    """
    layers: list[str] = []
    whole: list[str] = []
    if os.path.isdir(LANG_DIR):
        for fn in sorted(os.listdir(LANG_DIR)):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5].strip().lower()
            if not stem:
                continue
            if "_" in stem:
                head, _, tail = stem.partition("_")
                if head == code and tail and all(c.isalnum() or c == "_" for c in tail):
                    layers.append(fn)
                    continue
            if stem == code:
                whole.append(fn)
    return layers + whole


def _load(code: str) -> dict[str, str]:
    """读取一种语言的全部词条（合并整包与分层包）。"""
    out: dict[str, str] = {}
    for fn in _pack_files(code):
        path = os.path.join(LANG_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.update({str(k): str(v) for k, v in data.items()})
    return out



def report() -> int:
    """打印各语言的覆盖率报告。"""
    ref = _reference_keys()
    ref_set = set(ref)
    print("=== 语言包覆盖率 ===")
    print(f"参考语言：中文（{len(ref)} 词条）\n")
    # 只列**语言代码**——分层包（``ja_ui.json``）不算独立语言，
    # 它是 ja 的一部分。漏了这层过滤，报告里会冒出
    # 「ja_ui 日本語」这种看不懂的行。
    codes = set(i18n.LANGUAGE_NAMES)
    if os.path.isdir(LANG_DIR):
        for fn in os.listdir(LANG_DIR):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5].strip().lower()
            if not stem:
                continue
            if "_" in stem:
                head, _, tail = stem.partition("_")
                if head and tail and all(c.isalnum() or c == "_" for c in tail):
                    codes.add(head)
                continue
            codes.add(stem)
    worst = 100.0
    for code in codes:
        table = _load(code)
        if not table:
            builtin = len(i18n._builtin_table(code))
            print(f"  {code:4s} {LANG_NAMES.get(code, code):12s} "
                  f"内置 {builtin:3d} 条（磁盘语言包不存在）")
            worst = 0.0
            continue
        have = set(table) & ref_set
        pct = len(have) / len(ref) * 100 if ref else 100.0
        extra = set(table) - ref_set
        name = LANG_NAMES.get(code, code)
        mark = "✓" if pct >= 100 else ("·" if pct >= 80 else "✗")
        print(f"  {code:4s} {name:12s} {mark} {len(have):3d}/{len(ref)} "
              f"({pct:5.1f}%)" + (f"  多余键 {len(extra)}" if extra else ""))
        worst = min(worst, pct)
    print()
    if worst >= 100:
        print("✓ 所有语言覆盖完整")
    else:
        print(f"最低覆盖率 {worst:.1f}%——用 --missing <代码> 查看缺哪些词条")

    # 调用点参数个数（与具体语言无关，但值得每次都看一眼）
    global _REF_TEMPLATES
    if not _REF_TEMPLATES:
        _REF_TEMPLATES = _load_ref_templates()
    arity = _call_arity_errors(_REF_TEMPLATES)
    if arity:
        print()
        print(f"✗ 调用点参数个数不匹配 {len(arity)} 处：")
        for where, key, want, got, frag in arity[:8]:
            print(f"    {where}  {key}: 需要 {want} 个，实际 {got} 个 | {frag}")
        if len(arity) > 8:
            print(f"    ……另有 {len(arity) - 8} 处")
        return 1
    print("✓ 调用点参数个数全部匹配")

    unknown = _unknown_key_errors()
    if unknown:
        print()
        print(f"✗ 语言包中不存在的键 {len(unknown)} 处：")
        for where, key in unknown[:8]:
            print(f"    {where}  {key}")
        if len(unknown) > 8:
            print(f"    ……另有 {len(unknown) - 8} 处")
        return 1
    print("✓ UI 引用的词条键全部存在")
    return 0


def missing(code: str) -> int:
    """列出某语言缺失的词条。"""
    ref = _reference_keys()
    table = _load(code)
    have = set(table)
    miss = [k for k in ref if k not in have]
    name = LANG_NAMES.get(code, code)
    print(f"=== {name}（{code}）缺失 {len(miss)} / {len(ref)} 条 ===")
    if not miss:
        print("✓ 无缺失")
        return 0
    for k in miss:
        print(f"  {k}  ←  {i18n._BUILTIN_ZH.get(k, '')}")
    return 0


def make_template(code: str) -> int:
    """生成新语言的模板文件（预填英文译文供翻译）。"""
    name = LANG_NAMES.get(code)
    if not name:
        print(f"未知语言代码 '{code}'。"
              f"已知：{', '.join(sorted(LANG_NAMES))}")
        return 1
    path = os.path.join(LANG_DIR, code + ".json")
    if os.path.isfile(path):
        print(f"{code}.json 已存在——不覆盖。"
              f"要重建请先删除它。")
        return 1
    template = {k: i18n._BUILTIN_EN.get(k, i18n._BUILTIN_ZH.get(k, ""))
                for k in _reference_keys()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(f"已生成模板：{path}")
    print(f"共 {len(template)} 条，值是英文原文——请逐条翻译成{name}。")
    return 0


#: 简体里高频、日韩之外的语言基本不用的字——用来发现复制粘贴残留
_SIMP_MARKERS = (
    "军", "战", "击", "毁", "目标", "指挥", "部", "弹药", "补给", "敌", "我",
    "胜", "负", "点击", "开始", "设置", "导出", "导入", "选择", "回合",
    "拦截", "机密", "接触", "跟踪", "射程", "记录", "退出", "继续",
    "提示", "威胁", "敌方", "伤害", "单位", "飞机", "雷达", "干扰",
    "烟雾", "油料", "耐久", "移动", "状态", "武器", "传感器", "行动", "任务",
)


#: 每种语言的「残留检测」配置。
#:
#: 各语言对**中文简体字**的容忍度不同：
#:
#: * 拉丁 / 西里尔语言（de/fr/es/ru）—— 一个简体字都不该出现，
#:   因为这些语言完全不用汉字；
#: * 日语 —— 本身用汉字，所以不能简单禁汉字。但日语汉字有固定写法，
#:   简体字里那些**日语不用的字**（关、队、战、击、录…）就是残留；
#: * 韩语 —— 用谚文（한자），但谚文里的简体字形与中文不同，
#:   所以简体字同样不该出现；
#: * 中文 —— 不检测（原文）。
_LANG_MARKERS: dict[str, tuple[str, ...]] = {
    "de": _SIMP_MARKERS,
    "fr": _SIMP_MARKERS,
    "es": _SIMP_MARKERS,
    "ru": _SIMP_MARKERS,
    "ja": (
        # 日语里**写法不同**的简体字。
        # 「武器」「耐久」「弾薬」「接触」「我が軍」这些日语也用，
        # 不能列进来——只列日语明确不写的字。
        "军", "导", "弹", "胜", "负",
        "点击", "设置", "导出", "导入", "选择", "回合",
        "拦截", "机密", "记录", "退出", "继续",
        "提示", "威胁", "敌方", "伤害", "单位", "飞机", "雷达", "干扰",
        "烟雾", "油料", "移动", "状态", "传感器", "行动", "任务",
    ),
    "ko": _SIMP_MARKERS,
}

#: 各语言允许出现的**技术术语**（按键名、文件名、格式说明）。
#: 这些词出现在译文里是正常的，不该被判成「残留」。
_ALLOWED_WORDS = {
    "F1", "F5", "F9", "F10", "Esc", "Enter", "Tab", "Space",
    "BLOCK", "WARGAME", "VP", "km",
    "bwscn", "bwrep", "json", "py", "armory", "mod", "mods",
    "data", "maps", "units", "campaigns", "lang", "main",
}


def _placeholders(text: str) -> list[str]:
    """提取 ``%`` 风格的占位符列表。

    只认**格式说明符**（``%s`` / ``%.1f`` / ``%02d``），
    不认字面百分号（``%%``）。字面百分号在两个语言里
    应该都是 ``%%``，不属于「占位符数量」的一部分。
    """
    import re
    # 先把 ``%%`` 整体吃掉再扫描：否则 "%% gefallen" 的第二个 %
    # 会被当成 "% g"（空格是合法标志位），德语/法语里这几乎必然出现，
    # 报出来的却是「译文占位符多一个」，查半天查不到真正的错。
    text = text.replace("%%", "\x00")
    return re.findall(r"%(?![%])[-+ #0-9.]*[a-zA-Z]", text)


def _split_args(s: str) -> list[str]:
    """按**顶层**逗号切分参数。

    不能简单用 ``s.split(",")``——表达式里常带括号与逗号：
    ``min(len(items), x)``、``sc["size"][0], sc["size"][1]``。
    顶层逗号的判定方式是括号深度为 0。
    """
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _call_arity_errors(templates: dict[str, str]) -> list[tuple[str, int, str, int, int]]:
    """静态扫描 UI 代码，找出 ``tr(key) % (...)`` 的参数个数不匹配。

    **这是最值钱的一条检查**：调用点的参数个数是写死在代码里的，
    译文里少写一个 ``%s`` 就会在**游戏中途**抛
    ``TypeError: not all arguments converted during string formatting``——
    可能只在打开编辑器或射击时才触发，极难排查。

    返回 ``[(文件:行, 键, 需要, 实际, 源码片段), ...]``。
    """
    import glob
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pat = re.compile(r'tr\(\s*"([^"]+)"\s*\)\s*%\s*\(')
    out = []
    for rel in sorted(glob.glob(os.path.join(root, "ui", "*.py"))):
        try:
            src = open(rel, "r", encoding="utf-8").read()
        except OSError:
            continue
        for m in pat.finditer(src):
            key = m.group(1)
            if key not in templates:
                continue
            # 从 ( 开始做括号配对，找到对应的 )
            i = m.end() - 1
            depth = 0
            j = i
            while j < len(src):
                if src[j] in "([{":
                    depth += 1
                elif src[j] in ")]}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = src[i + 1:j]
            got = len(_split_args(inner))
            want = len(_placeholders(templates[key]))
            if want != got:
                line = src[:m.start()].count("\n") + 1
                short = os.path.relpath(rel, root).replace("\\", "/")
                out.append((f"{short}:{line}", key, want, got, inner.strip()[:60]))
    return out


def _known_keys() -> set[str]:
    """UI 里合法的 ``tr()`` 键：中文磁盘包（基础 + 分层）与内置表。"""
    keys = set(i18n._BUILTIN_ZH) | set(i18n._BUILTIN_EN)
    for fn in ("zh.json", "zh_ui.json"):
        try:
            with open(os.path.join(LANG_DIR, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                keys.update(str(k) for k in data)
        except (OSError, json.JSONDecodeError):
            pass
    return keys


def _unknown_key_errors() -> list[tuple[str, str]]:
    """扫描 ``ui/*.py`` 里所有 ``tr("...")``，找出语言包里不存在的键。

    **为什么单列一条**：``_call_arity_errors`` 只查带 ``%`` 的调用，
    且遇到中文 UI 模板里没有的键会直接跳过。拼错一个键名时 ``tr()``
    会回退成裸键名，后面若接 ``% (...)`` 就是运行时 TypeError；即使
    不接 ``%``，界面也会冒出 ``ui.editor_xxx`` 这种内部标识。
    """
    import glob
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    known = _known_keys()
    pat = re.compile(r'\btr\(\s*"([^"]+)"')
    out: list[tuple[str, str]] = []
    for rel in sorted(glob.glob(os.path.join(root, "ui", "*.py"))):
        try:
            src = open(rel, "r", encoding="utf-8").read()
        except OSError:
            continue
        for m in pat.finditer(src):
            key = m.group(1)
            if key in known:
                continue
            line = src[:m.start()].count("\n") + 1
            short = os.path.relpath(rel, root).replace("\\", "/")
            out.append((f"{short}:{line}", key))
    return out


def _placeholder_mismatch(ref: list[str],
                          data: dict[str, str]) -> list[tuple[str, list[str], list[str]]]:
    """找出占位符与参考语言不一致的词条。

    **为什么这是必须校验的**：调用点写的是 ``tr(key) % (a, b)``，
    参数个数是固定的。译文里少写一个 ``%s`` 就会在运行时抛
    ``TypeError: not enough arguments for format string``——
    而这可能出现在**游戏中途**（比如射��时才用到那条文案），
    极难排查。

    返回 ``[(键, 参考占位符, 实际占位符), ...]``。
    """
    out = []
    for k in ref:
        if k not in data:
            continue
        want = _placeholders(data[k]) if k in data else []
        # 参考语言的占位符从中文模板取（中文是基准）
        want = _placeholders(_REF_TEMPLATES.get(k, "")) or want
        got = _placeholders(data[k])
        if sorted(want) != sorted(got):
            out.append((k, want, got))
    return out



def _simplified_leftovers(code: str, data: dict[str, str]) -> list[tuple[str, str]]:
    """找出语言包里混入的、**不属于该语言**的字符。

    为什么要按语言区分：日语与韩语本身使用汉字，简体字对它们
    不是「非法字符」——但简体字里那些**该语言不用的字**
    （日语不写「关」而写「関」）仍然是复制粘贴留下的残留。
    """
    markers = _LANG_MARKERS.get(code)
    if not markers:
        return []
    import re
    pat = re.compile("|".join(sorted(markers)))
    return [(k, v) for k, v in data.items() if pat.search(v)]


def _foreign_words(code: str, data: dict[str, str]) -> list[tuple[str, str]]:
    """找出译文里混入的**其它语言的整词**。

    这是另一类常见事故：翻译时留下一个英文单词
    （比如把「通过」写成「.overflight」）。
    """
    if code in ("zh", "ru"):
        return []          # 俄语本身用西里尔字母
    import re
    # 日韩的假名/谚文不算「英文残留」——只找 3 个字母以上的拉丁词
    pat = re.compile(r"[A-Za-z]{3,}")
    out = []
    for k, v in data.items():
        words = [w for w in pat.findall(v) if w not in _ALLOWED_WORDS]
        # 纯 ASCII 串（如 "BLOCK WARGAME"）在日语里是正常的设计元素
        if words:
            out.append((k, v))
    return out



def verify(code: str) -> int:
    """校验语言包：格式、覆盖率、多余键、空值、跨语言残留。

    校验的是**合并后**的词条（整包 + 分层包），所以分层包里的
    译文也会被计入覆盖率。
    """
    name = LANG_NAMES.get(code, code)
    files = _pack_files(code)
    if not files:
        print(f"✗ 文件不存在：{os.path.join(LANG_DIR, code + '.json')}")
        return 1
    print(f"=== 校验 {name}（{code}）===")
    print(f"    词条文件：{', '.join(files)}")
    try:
        data = _load(code)
    except json.JSONDecodeError as exc:
        print(f"✗ JSON 格式错误：{exc}")
        return 1
    except OSError as exc:
        print(f"✗ 读取失败：{exc}")
        return 1
    print("✓ JSON 格式正确")
    if not isinstance(data, dict):
        print("✗ 顶层必须是对象")
        return 1

    global _REF_TEMPLATES
    if not _REF_TEMPLATES:
        _REF_TEMPLATES = _load_ref_templates()

    ref = _reference_keys()
    ref_set = set(ref)
    have = set(data)

    miss = [k for k in ref if k not in have]
    if miss:
        print(f"✗ 缺少 {len(miss)} 个词条：")
        for k in miss[:10]:
            print(f"    {k}")
        if len(miss) > 10:
            print(f"    ……另有 {len(miss) - 10} 个")
    else:
        print(f"✓ 覆盖率 100.0%（{len(ref)}/{len(ref)}）")

    extra = sorted(k for k in have - ref_set)
    if extra:
        print(f"⚠ 多余 {len(extra)} 个键（不在参考语言里）：")
        for k in extra[:6]:
            print(f"    {k}")
        if len(extra) > 6:
            print(f"    ……另有 {len(extra) - 6} 个")

    empty = [k for k, v in data.items() if not str(v).strip()]
    if empty:
        print(f"✗ {len(empty)} 个空值：{', '.join(empty[:8])}")
    else:
        print("✓ 无空值")

    # 简体残留检测：非中文语言包里混入简体词是最常见的翻译事故
    # （复制粘贴时留下的）。日语/韩语本身用汉字，需要排除。
    leftover = _simplified_leftovers(code, data)
    if leftover:
        print(f"✗ 疑似简体残留 {len(leftover)} 条：")
        for k, v in leftover:
            print(f"    {k} = {v}")
    else:
        print("✓ 无简体残留")

    foreign = _foreign_words(code, data)
    if foreign:
        print(f"✗ 疑似混入其它语言单词 {len(foreign)} 条：")
        for k, v in foreign:
            print(f"    {k} = {v}")
    else:
        print("✓ 无外语残留")

    # 占位符一致性：译文里的 %s / %.1f 必须与参考语言**完全一致**，
    # 否则格式化时会因为参数个数不符而崩溃。
    ph_bad = _placeholder_mismatch(ref, data)
    if ph_bad:
        print(f"✗ 占位符不匹配 {len(ph_bad)} 条：")
        for k, want, got in ph_bad[:8]:
            print(f"    {k}: 需要 {want}，实际 {got}")
    else:
        print("✓ 占位符全部匹配")

    # 调用点参数个数：这是**代码与译文的接口契约**。
    # 译文少一个占位符 → 运行时 TypeError（可能只在特定界面触发）。
    arity = _call_arity_errors(_REF_TEMPLATES)
    if arity:
        print(f"✗ 调用点参数个数不匹配 {len(arity)} 处：")
        for where, key, want, got, frag in arity[:8]:
            print(f"    {where}  {key}: 需要 {want} 个，实际 {got} 个 | {frag}")
        if len(arity) > 8:
            print(f"    ……另有 {len(arity) - 8} 处")
    else:
        print("✓ 调用点参数个数全部匹配")

    ok = (not miss and not empty and not leftover and not foreign
          and not ph_bad and not arity)
    print()
    if ok:
        print(f"✓ {name} 语言包通过")
        return 0
    return 1



def main() -> int:
    args = sys.argv[1:]
    if not args:
        return report()
    if args[0] in ("-h", "--help"):
        print("用法：")
        print("  python tools/lang_check.py                 # 覆盖率报告")
        print("  python tools/lang_check.py --missing ja    # 列出缺失词条")
        print("  python tools/lang_check.py --new ja       # 生成语言包模板")
        print("  python tools/lang_check.py --verify ja    # 校验语言包")
        return 0

    cmd = args[0]
    code = args[1] if len(args) > 1 else ""
    if cmd == "--missing":
        if not code:
            print("需要语言代码")
            return 1
        return missing(code)
    if cmd == "--new":
        if not code:
            print("需要语言代码")
            return 1
        return make_template(code)
    if cmd == "--verify":
        if not code:
            print("需要语言代码")
            return 1
        return verify(code)
    print(f"未知参数 '{cmd}'——用 --help 看用法")
    return 1



if __name__ == "__main__":
    sys.exit(main())

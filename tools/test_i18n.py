"""v3.0 专项自检：本地化框架的**运行时**行为。

与 ``tools/lang_check.py`` 的分工：

* ``lang_check.py`` 做**静态**校验——覆盖率、占位符一致性、译文残留、
  调用点参数个数；
* 本脚本做**运行时**自检——语言切换、分层包自动加载、回退链、
  ``%`` 格式化不崩、模组词条保护。

最值得守住的一条：``tr()`` 必须能自己装载安装目录的语言包。
v3.0 把界面文案抽进 ``data/lang/*_ui.json`` 之后，如果仍只有主程序
启动时会调用 ``load_language_dir``，那么任何单独使用 ``ui`` 模块的
调用者（专项自检、模组预览工具）都会让 ``tr()`` 回退成裸键名；
而裸键名被 ``% (...)`` 格式化会直接抛
``TypeError: not all arguments converted during string formatting``。
本脚本刻意**不**调用 ``load_language_dir``，正是为了模拟那种调用方式。

用法：python tools/test_i18n.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows CI 的 stdout 默认 cp1252，中文断言信息会直接把脚本打崩
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from core import i18n  # noqa: E402
from core.config import label_for  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def main() -> int:
    # -- 语言切换与显示名 -------------------------------------------------
    i18n.set_language("zh")
    check(i18n.tr("menu.start") == "开始作战", "中文：内置词条")
    i18n.set_language("en")
    check(i18n.tr("menu.start") == "Start Operation", "英文：内置词条")
    check(not i18n.set_language("xx"), "拒绝不支持的语言代码")
    for code in i18n.SUPPORTED_LANGUAGES:
        check(i18n.set_language(code), f"可切换到 {code}")
        check(i18n.language_name(code) != code, f"{code} 有本地化显示名")

    # -- 分层包懒加载与格式化 ---------------------------------------------
    # 注意：本脚本全程没有调用 load_language_dir。
    i18n.set_language("ja")
    lvl = i18n.tr("ui.camp_editor_levels_count_fmt")
    check(lvl != "ui.camp_editor_levels_count_fmt" and "%s" in lvl,
          "分层 UI 词条自动加载（未显式初始化语言目录）")
    check(lvl % (3,) == "3 ステージ", "词条 % 格式化不抛 TypeError")
    check(label_for("end_turn") == "ターン終了", "键位标签跟随界面语言")

    # -- 回退链：中文磁盘包 → 键名 -----------------------------------------
    i18n.register_mod_entry("zh", "test.i18n.zh_only", "仅中文词条")
    i18n.set_language("de")
    check(i18n.tr("test.i18n.zh_only") == "仅中文词条",
          "未翻译键回退到中文磁盘包（而非裸键名）")
    check(i18n.tr("test.i18n.no_such") == "test.i18n.no_such",
          "彻底缺失的键回退为键名")

    # -- 模组词条不覆盖本体键 ---------------------------------------------
    check(not i18n.register_mod_entry("en", "menu.start", "HACK"),
          "模组词条不可覆盖本体键")
    check(i18n.register_mod_entry("en", "test.i18n.custom", "自定义"),
          "模组可注册独立命名空间词条")
    i18n.set_language("en")
    check(i18n.tr("menu.start") == "Start Operation", "本体词条未被篡改")

    i18n.set_language("zh")
    print()
    if FAILS:
        print(f"✗ 本地化运行时自检失败 {len(FAILS)} 项：")
        for msg in FAILS:
            print(f"    - {msg}")
        return 1
    print("✓ 本地化运行时自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

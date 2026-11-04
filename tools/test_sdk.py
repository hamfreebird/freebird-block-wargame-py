"""v3.0 专项自检：官方模组 SDK。

覆盖 v3.0 的 8.1（模组 SDK）：

1. **manifest**——元数据声明、版本范围解析、依赖声明；
2. **API 版本承诺**——不兼容的模组被**拒绝加载**并说清原因；
3. **公开 API 白名单**——新 API 方法都在白名单里；
4. **目录式模组**——与 v2.0 单文件模组并存；
5. **内容注入**——武器 / 兵种 / 传感器 / 挂载都能进武器库；
6. **本地化**——模组词条能注入，但不能覆盖游戏本体的键。

用法：python tools/test_sdk.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from core import mods as _mods  # noqa: E402
from core.mod_manifest import (  # noqa: E402
    describe_spec,
    parse_manifest,
    parse_version,
    version_matches,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def _write_pkg(root: str, name: str, manifest: dict | None,
               main: str = "def register(api):\n    api.log('hi')\n") -> str:
    """造一个目录式模组，返回它的目录。"""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    if manifest is not None:
        with open(os.path.join(d, "mod.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False)
    with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as f:
        f.write(main)
    return d


# --------------------------------------------------------------------------- #
def test_version_parsing() -> None:
    print("--- 版本号解析与范围匹配 ---")
    check(parse_version("1.2.3") == (1, 2, 3), "标准版本号解析正确")
    check(parse_version("1.2") == (1, 2, 0), "缺位版本号补 0")
    check(parse_version("1.2.3-beta") == (1, 2, 3), "预发布后缀被忽略")
    check(parse_version("") == (0, 0, 0), "空版本号按 0 处理")

    cases = [
        ("1.0.0", ">=1.0.0", True),
        ("0.9.0", ">=1.0.0", False),
        ("2.0.0", ">=1.0.0,<2.0.0", False),
        ("1.5.0", ">=1.0.0,<2.0.0", True),
        ("1.5.0", "^1.0.0", True),
        ("2.0.0", "^1.0.0", False),
        ("1.2.9", "~1.2.0", True),
        ("1.3.0", "~1.2.0", False),
        ("1.0.0", "", True),
        ("1.0.0", "1.0.0", True),
        ("1.0.1", "1.0.0", False),
        # 逗号分隔的多条件
        ("1.0.0", ">=0.9.0, <2.0.0", True),
    ]
    bad = []
    for v, spec, expect in cases:
        got = version_matches(v, spec)
        if got != expect:
            bad.append(f"{v} vs {spec}")
    check(not bad, f"{len(cases)} 个版本范围用例全部正确"
          + (f"（错：{bad}）" if bad else ""))
    check(describe_spec(">=1.0.0") == "≥1.0.0", "版本规格转人话正确")


def test_manifest() -> None:
    print("--- manifest 解析 ---")
    m = parse_manifest({
        "id": "my_mod", "name": "我的模组", "version": "1.2.0",
        "author": "飞鸟", "description": "说明", "api": ">=1.0.0",
        "requires": {"base": ">=1.0.0"}, "entry": "main.py",
        "custom_key": 42,
    })
    check(m.ok, "合法 manifest 解析成功")
    check(m.mod_id == "my_mod", "id 解析正确")
    check(m.version == "1.2.0", "version 解析正确")
    check(m.requires.get("base") == ">=1.0.0", "requires 解析正确")
    check(m.extra.get("custom_key") == 42, "未识别字段原样保留")
    check("我的模组" in m.summary(), "摘要含显示名")

    bad = parse_manifest({"name": "没有 id"})
    check(not bad.ok and "id" in bad.error, "缺 id 的 manifest 被拒绝")

    bad2 = parse_manifest({"id": "x", "requires": ["不是字典"]})
    check(not bad2.ok, "requires 类型错误被拒绝")


def test_api_version_gate() -> None:
    print("--- API 版本承诺 ---")
    root = tempfile.mkdtemp()
    try:
        # 兼容
        _write_pkg(root, "ok_mod",
                   {"id": "ok_mod", "version": "1.0.0", "api": ">=1.0.0"})
        # 不兼容（要求未来的 API）
        _write_pkg(root, "future_mod",
                   {"id": "future_mod", "version": "1.0.0", "api": ">=9.0.0"})
        reg = _mods.ModRegistry()
        reg.load_dirs([root])
        by_id = {m.mod_id: m for m in reg.mods}
        check(by_id.get("ok_mod") is not None and by_id["ok_mod"].ok,
              "API 兼容的模组正常加载")
        fut = by_id.get("future_mod")
        check(fut is not None and not fut.ok,
              "API 不兼容的模组被拒绝加载")
        check(fut is not None and "API" in fut.error,
              f"拒绝原因说明了 API 版本（{fut.error if fut else '?'}）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_dependencies() -> None:
    print("--- 依赖声明 ---")
    root = tempfile.mkdtemp()
    try:
        _write_pkg(root, "base_mod",
                   {"id": "base_mod", "version": "1.0.0"},
                   main="def register(api):\n    pass\n")
        _write_pkg(root, "needs_base",
                   {"id": "needs_base", "version": "1.0.0",
                    "requires": {"base_mod": ">=1.0.0"}},
                   main="def register(api):\n    pass\n")
        # 依赖版本不满足
        _write_pkg(root, "needs_new",
                   {"id": "needs_new", "version": "1.0.0",
                    "requires": {"base_mod": ">=2.0.0"}},
                   main="def register(api):\n    pass\n")

        reg = _mods.ModRegistry()
        reg.load_dirs([root])
        by_id = {m.mod_id: m for m in reg.mods}
        check(by_id["base_mod"].ok, "基础模组加载成功")
        check(by_id["needs_base"].ok,
              "依赖满足时正常加载")
        check(not by_id["needs_new"].ok,
              "依赖版本不满足时被拒绝加载")
        check("版本" in by_id["needs_new"].error,
              f"拒绝原因说明了版本问题（{by_id['needs_new'].error}）")

        # 缺失依赖
        root2 = tempfile.mkdtemp()
        try:
            _write_pkg(root2, "orphan",
                       {"id": "orphan", "version": "1.0.0",
                        "requires": {"nonexistent": ">=1.0.0"}},
                       main="def register(api):\n    pass\n")
            reg2 = _mods.ModRegistry()
            reg2.load_dirs([root2])
            check(not reg2.mods[0].ok, "依赖缺失时被拒绝加载")
            check("缺少依赖" in reg2.mods[0].error,
                  f"拒绝原因说明了缺哪个依赖（{reg2.mods[0].error}）")
        finally:
            shutil.rmtree(root2, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_backward_compat() -> None:
    print("--- 向后兼容 v2.0 单文件模组 ---")
    root = tempfile.mkdtemp()
    try:
        with open(os.path.join(root, "legacy.py"), "w", encoding="utf-8") as f:
            f.write("def register(api):\n"
                    "    api.add_weapon({'id': 'w1', 'name': 'W'})\n")
        reg = _mods.ModRegistry()
        reg.load_dirs([root])
        check(len(reg.mods) == 1 and reg.mods[0].ok,
              "v2.0 单文件模组仍能加载")
        check(reg.mods[0].manifest is None, "无 manifest 时 manifest 为 None")
        check(any("缺少 mod.json" in w for w in reg.mods[0].warnings),
              "缺 manifest 给出警告（而不是报错）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_public_api() -> None:
    print("--- 公开 API 白名单 ---")
    api = _mods.ModAPI("test")
    # 白名单里的方法都真实存在
    missing = [n for n in _mods.PUBLIC_API if not hasattr(api, n)]
    check(not missing, f"白名单中 {len(_mods.PUBLIC_API)} 个方法全部存在"
          + (f"（缺：{missing}）" if missing else ""))
    # 白名单不含内部方法
    check(not any(n.startswith("_") for n in _mods.PUBLIC_API),
          "白名单不含内部方法")


def test_content_injection() -> None:
    print("--- 内容注入 ---")
    from core.loader import load_armory
    from core.paths import armory_path

    root = tempfile.mkdtemp()
    try:
        main = (
            "def register(api):\n"
            "    api.add_sensor({'id': 'sd1', 'kind': 'radar', 'range': 20.0})\n"
            "    api.add_weapon({'id': 'w1', 'name': '模组武器', 'kind': 'asm',\n"
            "                   'domain': 'land', 'min_range': 1.0, 'range': 8.0,\n"
            "                   'pk': 0.5, 'damage': 10.0, 'ammo': 4,\n"
            "                   'targets': ['land']})\n"
            "    api.add_unit({'id': 'u1', 'name': '模组兵种', 'domain': 'land',\n"
            "                 'class': 'infantry', 'hp': 50, 'mp': 3.0,\n"
            "                 'cost': 30, 'weapons': [{'ref': 'w1', 'ammo': 4}]})\n"
            "    api.add_mount({'id': 'm1', 'name': '模组挂载',\n"
            "                  'weapons': [{'ref': 'w1', 'ammo': 4}]})\n"
        )
        _write_pkg(root, "content_mod",
                   {"id": "content_mod", "version": "1.0.0"}, main=main)
        reg = _mods.ModRegistry()
        reg.load_dirs([root])
        check(reg.mods[0].ok, f"内容模组加载成功（{reg.mods[0].error}）")

        # 注入走的是**全局注册表**——loader 只认 default_registry()
        _mods.set_default_registry(reg)
        armory = load_armory(armory_path())
        check("w1" in armory.weapons, "模组武器注入武器库")
        check("u1" in armory.templates, "模组兵种注入武器库")
        check("sd1" in armory.sensors, "模组传感器注入武器库")
        los = armory.meta.get("presets", {}).get("loadouts", [])
        check(any(str(x.get("id")) == "m1" for x in los), "模组挂载注入预设表")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_localization() -> None:
    print("--- 模组本地化 ---")
    from core import i18n

    root = tempfile.mkdtemp()
    try:
        main = (
            "def register(api):\n"
            "    api.add_localization('zh', {'test_sdk_mod.hello': '你好'})\n"
            "    api.add_localization('en', {'test_sdk_mod.hello': 'Hello'})\n"
            "    api.add_localization('zh', {'menu.start': '劫持开始按钮'})\n"
        )
        _write_pkg(root, "test_sdk_mod",
                   {"id": "test_sdk_mod", "version": "1.0.0"}, main=main)
        reg = _mods.ModRegistry()
        reg.load_dirs([root])
        check(reg.mods[0].ok, "本地化模组加载成功")

        # 三条里两条合法（zh/en 的 hello），一条越界（覆盖 menu.start）
        n = _mods.apply_localization(reg)
        check(n == 2, f"两条合法词条被注入（实际 {n}）")
        i18n.set_language("zh")
        check(i18n.tr("test_sdk_mod.hello") == "你好", "模组词条可查")
        # 本体键不能被覆盖
        i18n.set_language("zh")
        before = i18n.tr("menu.start")
        check(before != "劫持开始按钮", "模组无法覆盖游戏本体的键")
        check(any("命名空间" in e for e in reg.errors),
              "越界的词条被记录为错误")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_example_mod() -> None:
    print("--- 官方示例模组 ---")
    ex = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "examples", "mods", "example_laser")
    if not os.path.isdir(ex):
        check(False, "示例模组目录存在")
        return
    reg = _mods.ModRegistry()
    reg.load_dirs([os.path.dirname(ex)])
    entry = next((m for m in reg.mods if m.mod_id == "example_laser"), None)
    check(entry is not None, "示例模组被找到")
    if entry is None:
        return
    check(entry.ok, f"示例模组加载成功（{entry.error}）")
    check(entry.manifest is not None and entry.manifest.ok,
          "示例模组的 manifest 合法")
    check(entry.version == "1.0.0", "示例模组版本号正确")
    check(entry.weapons >= 1 and entry.units >= 1 and entry.hooks >= 1,
          f"示例模组注册了内容（武器 {entry.weapons} · 兵种 {entry.units} · "
          f"钩子 {entry.hooks}）")


def test_error_isolation() -> None:
    print("--- 错误隔离 ---")
    root = tempfile.mkdtemp()
    try:
        with open(os.path.join(root, "broken.py"), "w", encoding="utf-8") as f:
            f.write("raise RuntimeError('故意炸')\n")
        with open(os.path.join(root, "good.py"), "w", encoding="utf-8") as f:
            f.write("def register(api):\n    api.log('好模组')\n")
        reg = _mods.ModRegistry()
        reg.load_dirs([root])
        by_id = {m.mod_id: m for m in reg.mods}
        check(not by_id["broken"].ok, "坏模组被标记为失败")
        check(by_id["good"].ok, "坏模组不影响其它模组加载")
        check(reg.errors, "错误被记录")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    print("=== 模组 SDK 自检 ===")
    test_version_parsing()
    test_manifest()
    test_api_version_gate()
    test_dependencies()
    test_backward_compat()
    test_public_api()
    test_content_injection()
    test_localization()
    test_example_mod()
    test_error_isolation()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("模组 SDK 自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

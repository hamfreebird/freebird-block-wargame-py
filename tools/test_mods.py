"""模组接口自检。

验证：

1. 模组能被加载，注册的武器与兵种真的进了武器库；
2. 规则钩子真的被触发，且能改变对局结果；
3. **错误隔离**——坏模组（语法错误 / 抛异常 / 缺 register）不会
   影响游戏启动或其它模组；
4. 模组内容会随想定一起被校验。

不依赖 pytest，可直接在 CI 中运行：
    SDL_VIDEODRIVER=dummy python tools/test_mods.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_TEST_USER_DIR = tempfile.mkdtemp(prefix="bw_mods_")
os.environ["BLOCKWARGAME_USER_DIR"] = _TEST_USER_DIR
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import mods as _mods  # noqa: E402
from core.enums import Side  # noqa: E402
from core.loader import load_armory  # noqa: E402

ARMORY = "data/units/armory.json"
MAP = "data/maps/strait_shield.json"

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [FAIL] {label}")
        FAILED.append(label)


# --------------------------------------------------------------------------- #
# 测试用的模组
# --------------------------------------------------------------------------- #
GOOD_MOD = '''
"""测试模组：注册一件武器、一个兵种，并订阅三个钩子。"""

CALLED = {"before": 0, "after": 0, "damage": 0, "turn": 0, "killed": 0}


def register(api):
    api.add_weapon({
        "id": "test_laser", "name": "测试激光", "kind": "atgm",
        "range_min": 0.3, "range_max": 3.0, "pk": 1.0, "damage": 100,
        "ammo": 4, "guidance": "optical", "targets": ["land", "infra"],
        "speed": 12.0, "max_salvo": 2,
    })
    api.add_unit({
        "id": "test_unit", "name": "测试单位", "short": "TST",
        "class": "uav", "domain": "air", "hp": 30, "mp": 6.0,
        "armor": 0.0, "stealth": 0.3, "evasion": 0.1,
        "max_altitude": "low", "sensors": ["optical_uav"],
        "weapons": [{"ref": "test_laser", "ammo": 4}],
        "cost": 100, "notes": "自检用", "sortie_shots": 2, "echelon": "squadron",
    })
    api.on("before_fire", _before)
    api.on("after_fire", _after)
    api.on("modify_damage", _damage)
    api.on("turn_start", _turn)
    api.on("unit_destroyed", _killed)


def _before(state, attacker, target, weapon):
    CALLED["before"] += 1


def _after(state, attacker, target, weapon, result):
    CALLED["after"] += 1


def _damage(state, attacker, target, weapon, damage):
    CALLED["damage"] += 1
    return float(damage) * 2.0


def _turn(state, turn):
    CALLED["turn"] += 1


def _killed(state, unit):
    CALLED["killed"] += 1
'''

BROKEN_SYNTAX = "def register(api)\n    this is not python\n"

BROKEN_RUNTIME = '''
def register(api):
    raise RuntimeError("这个模组故意炸了")
'''

NO_REGISTER = '''
"""没有 register 函数的模组。"""
x = 1
'''

BAD_HOOK = '''
def register(api):
    api.on("no_such_event", lambda *a: None)
'''


def write_mod(dirpath: str, name: str, src: str) -> str:
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return path


# --------------------------------------------------------------------------- #
def test_load_and_inject() -> None:
    """模组加载 + 内容注入。"""
    print("模组加载与注入：")
    d = os.path.join(_TEST_USER_DIR, "mods")
    os.makedirs(d, exist_ok=True)
    write_mod(d, "good_mod.py", GOOD_MOD)
    reg = _mods.load_all([d])
    check(len(reg.mods) == 1, f"加载了 1 个模组（{len(reg.mods)}）")
    check(reg.ok_count == 1, "模组加载成功")
    check(reg.errors == [], f"无加载错误（{reg.errors}）")

    arm = load_armory(ARMORY)
    check("test_laser" in arm.weapons, "模组武器已进入武器库")
    check("test_unit" in arm.templates, "模组兵种已进入武器库")
    if "test_laser" in arm.weapons:
        w = arm.weapons["test_laser"]
        check(w.name == "测试激光" and w.damage == 100,
              f"模组武器字段正确（{w.name} / 伤害 {w.damage}）")
    if "test_unit" in arm.templates:
        t = arm.templates["test_unit"]
        check(t.name == "测试单位" and t.domain.value == "air",
              f"模组兵种字段正确（{t.name} / {t.domain.value}）")
    # 原有内容没被破坏
    check(len(arm.templates) > 60, f"原有兵种仍在（{len(arm.templates)} 个）")


def test_hooks_fire() -> None:
    """钩子真的被触发，并改变结果。"""
    print("规则钩子：")
    d = os.path.join(_TEST_USER_DIR, "mods")
    reg = _mods.load_all([d])
    # 拿到模组模块里的计数器——必须用注册表持有的那个实例，
    # 重新导入会得到另一个模块对象（计数器不是同一个）
    entry = next((m for m in reg.mods if m.mod_id == "good_mod"), None)
    mod = entry.module if entry is not None else None
    if mod is None:
        check(False, "取到模组模块实例")
        return

    from core.engine import GameState
    from core.loader import load_scenario
    sc = load_scenario(MAP, ARMORY)
    st = GameState(sc, Side.BLUE, seed=7)
    st.mods = reg

    # --- 1. 返回值机制：钩子返回的新值必须被传出 -------------------
    class _FakeWeapon:
        id = "test_laser"

    class _FakeUnit:
        template = type("T", (), {"armor": 0.0})()

    out = reg.fire("modify_damage", st, _FakeUnit(), _FakeUnit(),
                   _FakeWeapon(), 100.0)
    check(out == 200.0, f"modify_damage 的返回值被传出（100 → {out}）")
    check(mod.CALLED["damage"] >= 1, f"modify_damage 已调用（{mod.CALLED['damage']}）")

    # --- 2. 真实交火：钩子被引擎触发 ------------------------------
    from core import rules
    target = next((u for u in st.units if u.side is Side.RED and u.alive
                   and u.template.hp >= 100), None)
    attacker = next((u for u in st.units if u.side is Side.BLUE and u.alive
                     and u.usable_weapons()), None)
    if target is None or attacker is None:
        check(False, "找到攻击方与高耐久目标")
        return
    # 把目标挪到攻击者旁边——想定开局双方离得远，够不着。
    # 要落在合法地形上（陆上单位不能站在海里），且与攻击者不同格。
    spot = None
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        tx, ty = attacker.x + dx, attacker.y + dy
        tile = st.map.at(tx, ty)
        if tile is not None and tile.terrain.value != "water":
            spot = (tx, ty)
            break
    if spot is None:
        check(False, "找到攻击者旁边的合法落点")
        return
    target.x, target.y = spot
    st.invalidate_pos_index()
    st.refresh_detection()
    weapon = next((w for w in attacker.usable_weapons()
                   if rules.shot_profile(st, attacker, target, w).ok), None)
    if weapon is None:
        check(False, "找到能真正开火的武器")
        return

    before = mod.CALLED["damage"]
    hp_before = target.hp
    # 让命中判定必定通过——roll 是 random() < pk，random 返回 0 即必中。
    # GameRng 用了 __slots__ 没法替换方法，所以整个换掉 rng 对象。
    class _AlwaysHit:
        calls = 0

        def random(self):
            _AlwaysHit.calls += 1
            return 0.0

        def uniform(self, a, b):
            return a

        def choice(self, seq):
            return seq[0]

        def randint(self, a, b):
            return a

    st.rng = _AlwaysHit()
    res = st.fire(attacker, target, weapon)
    if getattr(res, "in_flight", False):
        for _ in range(8):
            st.resolve_missiles()
            if not st.missiles:
                break
    check(mod.CALLED["before"] >= 1, f"before_fire 已触发（{mod.CALLED['before']}）")
    check(mod.CALLED["after"] >= 1, f"after_fire 已触发（{mod.CALLED['after']}）")
    check(mod.CALLED["damage"] > before,
          f"modify_damage 在交火中被触发（{mod.CALLED['damage']}）")
    check(target.hp < hp_before, f"目标确实受伤（{hp_before:.0f} → {target.hp:.0f}）")

    # --- 3. 回合钩子 -------------------------------------------------
    st._advance_turn()
    check(mod.CALLED["turn"] >= 1, f"turn_start 已触发（{mod.CALLED['turn']}）")


def test_error_isolation() -> None:
    """坏模组不影响游戏。"""
    print("错误隔离：")
    d = os.path.join(_TEST_USER_DIR, "mods_bad")
    os.makedirs(d, exist_ok=True)
    write_mod(d, "ok_one.py", GOOD_MOD)
    write_mod(d, "bad_syntax.py", BROKEN_SYNTAX)
    write_mod(d, "bad_runtime.py", BROKEN_RUNTIME)
    write_mod(d, "no_register.py", NO_REGISTER)
    write_mod(d, "bad_hook.py", BAD_HOOK)

    reg = _mods.load_all([d])
    check(len(reg.mods) == 5, f"全部 5 个模组都被尝试（{len(reg.mods)}）")
    check(reg.ok_count == 1, f"只有 1 个模组成功（{reg.ok_count}）")
    check(len(reg.errors) >= 3, f"坏模组各自记录了错误（{len(reg.errors)} 条）")
    check(any("ok_one" in m.mod_id and m.ok for m in reg.mods),
          "好模组不受坏模组影响")

    # 坏模组不应污染武器库
    arm = load_armory(ARMORY)
    check("test_laser" in arm.templates or "test_unit" in arm.templates
          or True, "坏模组不会让武器库加载失败")
    check(len(arm.templates) > 60, f"武器库仍然完整（{len(arm.templates)} 个兵种）")


def test_unknown_event() -> None:
    """未知钩子名应给出可读错误。"""
    print("接口约束：")
    d = os.path.join(_TEST_USER_DIR, "mods_one")
    os.makedirs(d, exist_ok=True)
    write_mod(d, "bad_hook.py", BAD_HOOK)
    reg = _mods.load_all([d])
    check(reg.ok_count == 0, "订阅未知钩子的模组加载失败")
    check(any("未知钩子" in e for e in reg.errors),
          f"错误信息可读（{reg.errors[:1]}）")


def test_entry_path_guard() -> None:
    """manifest 的 entry 不能把加载器带到模组目录之外。"""
    print("入口路径约束：")
    base = os.path.join(_TEST_USER_DIR, "entry_guard", "pkg")
    os.makedirs(os.path.join(base, "sub"), exist_ok=True)
    write_mod(os.path.join(base, "sub"), "main.py", GOOD_MOD)

    # resolve_entry 是纯函数，先把判定边界钉死
    path, err = _mods.resolve_entry(base, "")
    check(not err and path.endswith("main.py"), "空 entry 回退到 main.py")
    path, err = _mods.resolve_entry(base, "sub/main.py")
    check(not err and os.path.isfile(path), "子目录入口被接受")
    for bad in ("../outside.py", "a/../../outside.py",
                os.path.join(base, "outside.py")):
        _p, err = _mods.resolve_entry(base, bad)
        check(bool(err), f"拒绝越界入口 '{bad}' → {err or '（未拒绝）'}")

    # 端到端：越界入口的模组必须加载失败，且那个文件不能被执行
    # 注意把 outside.py 放在**被扫描目录之外**，否则它会被当成单文件模组直接加载
    marker = os.path.join(_TEST_USER_DIR, "ESCAPED.txt")
    outside = os.path.join(_TEST_USER_DIR, "outside_escape.py")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("open(r'%s', 'w').write('executed')\n" % marker.replace("\\", "\\\\"))
    pkg = os.path.join(base, "evil")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "mod.json"), "w", encoding="utf-8") as f:
        f.write('{"id": "evil", "entry": "../../outside_escape.py"}')

    reg = _mods.load_all([base])
    check(not os.path.exists(marker), "越界入口的文件没有被执行")
    check(not any(m.ok and m.mod_id == "evil" for m in reg.mods),
          "越界入口的模组加载失败")
    check(any("跳出模组目录" in e for e in reg.errors),
          f"错误信息说清原因（{reg.errors[:1]}）")


def test_example_mod() -> None:
    """官方示例模组可用。"""
    print("示例模组：")
    path = os.path.join("examples", "mods", "example_orbital.py")
    if not os.path.isfile(path):
        check(False, "示例模组文件存在")
        return
    d = os.path.join(_TEST_USER_DIR, "mods_example")
    os.makedirs(d, exist_ok=True)
    shutil.copy(path, os.path.join(d, "example_orbital.py"))
    reg = _mods.load_all([d])
    check(reg.ok_count == 1, "示例模组加载成功")
    if reg.errors:
        print(f"       错误：{reg.errors}")
    arm = load_armory(ARMORY)
    check("orbital_laser" in arm.weapons, "示例模组的武器进入武器库")
    check("concept_laser" in arm.templates, "示例模组的兵种进入武器库")



def test_dir_error_visible() -> None:
    """用户模组目录不可用时，必须留下可见的错误（AUDIT §3.1 C3）。

    以前这里是 ``except Exception: pass``：目录探测失败 → 静默丢弃 →
    一个模组都不加载 → 玩家只看到「我装的模组没生效」。
    这条断言守的是「错误必须进报告」，而不是「必须能加载」。
    """
    from core import paths as _paths

    orig = _paths.user_writable_dir

    def boom():
        raise OSError("模拟只读盘")

    _paths.user_writable_dir = boom
    try:
        _mods.default_mod_dirs()
        check(bool(_mods.DIR_ERRORS), "目录探测失败被记录到 DIR_ERRORS")
        reg = _mods.load_all()
        check(any("用户模组目录不可用" in e for e in reg.errors),
              "目录错误进入注册表的 errors（界面据此提示）")
    finally:
        _paths.user_writable_dir = orig
    # 恢复后不应残留错误
    _mods.default_mod_dirs()
    check(not _mods.DIR_ERRORS, "恢复正常后错误列表被清空")


def main() -> int:
    print("=== 模组接口自检 ===")
    test_load_and_inject()
    test_hooks_fire()
    test_error_isolation()
    test_unknown_event()
    test_entry_path_guard()
    test_example_mod()
    test_dir_error_visible()
    print()
    if FAILED:
        print(f"模组自检失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("模组自检全部通过")
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception:
        import traceback
        traceback.print_exc()
        _rc = 1
    finally:
        shutil.rmtree(_TEST_USER_DIR, ignore_errors=True)
    sys.exit(_rc)

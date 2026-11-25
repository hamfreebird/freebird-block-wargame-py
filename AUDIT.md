# 项目审计报告

> 这份文件取代了原来的 `ROADMAP.md`。路线图回答「接下来做什么」，
> 审计回答「现在到底怎么样」。
>
> **本版为第五轮审计（2026-11-26，基于 v3.5.5 / 待发版，全部 30 条已修）**。
> 前四轮关闭了 53 条问题（v3.4.1 的 10 条、v3.4.2 的 22 条、v3.4.3 的 22 条、
> v3.4.4 处理的 23 条，其中 3 条评估不修）。
>
> **本轮换的角度**：前四轮问「函数算得对不对」「状态在错误时机被写了第二次会怎样」，
> 并已用变异测试验证过门禁会不会失败。本轮问一个更基础的问题——
> **「那些声称修好的东西，今天真的在代码里生效吗？」**
>
> 结果是肯定的必要补充：发现 **5 处同名方法重复定义**，其中 1 处使上一轮的修复
> 完全失效；另发现 1 条前轮声称已修、代码却从未改动的规则缺陷；以及 4 个恒真
> 或永远跳过的门禁。这些都不是"新功能有 bug"，而是**修复与验证本身的可靠性问题**。
>
> 全文没有推测性结论：每条给出**实测数值**或**可点开的代码位置**。
> 实测统一用 `D:/Dev/Python/python.exe`（3.12.4 / pygame 2.6.1）；
> 系统 `python`（3.13）无 pygame，不能用于 UI 测试。
> 审计阶段为只读（变异均在内存中完成）；随后 30 条全部修复并随 v3.5.5 发布，
> 每条修法与验证方式见 §6.1。

## 当前基线（v3.5.5 · 未发版）

| 维度 | 现状 |
|---|---|
| 玩法内核 | 45 项机制：侦察—打击链、逐发拦截、中段反导、电子战与多频段诱饵、信火一体、工事体系、烟幕、任务剖面、卫星过顶窗口、反卫星、设施打击、齐射、对手视角威胁评估、战场天气与时段、补给线与纵深、指挥体系、多点防空协同、火力准备、波段对抗、同格共存、对局回放、AI 观战、想定与回放分享、可复现随机流、模组接口 |
| 数据 | 65 兵种 / 71 武器 / 28 传感器 / 15 挂载预设 / 9 编队预设（`main.py --check` 实测） |
| 想定 | 4 张（海峡对决 128×128、草原雷霆 36×36、海峡屏障 40×40、新兵训练场 20×14） |
| 规则 | JSON 声明式规则钩子（11 基础条件 + all/any/not 3 组合器 × 9 种效果） |
| 呈现 | 4 套主题（含 2 套色盲友好）· 字号三档 · 色盲形状标识 · 9 种音效 · 8 种语言；v3.5.x 新增程序化矢量单位立体、夜间城市柔光、时段色调、天气滚动、地图暗角、面板浮雕 |
| 对手 AI | 规则启发式（默认）· 本地大模型指挥官（可选，只决定作战决心） |
| 代码规模 | core 42 文件 16,808 行 · ui 15 文件 12,704 行 · tools 47 文件 15,039 行 · `main.py` 220 行（全行口径） |
| 自检 | `tools/` 下 32 个 `test_*`/`check_*` 脚本（25 + 7）；CI 9 组 OS×Python 组合、54 步（verify 44 + balance 5 + package 5） |
| 实测性能 | 完整 `App.draw`：晴昼 5.75 ms、晴夜 7.02 ms、雨昼 9.48 ms（最坏）、雪昼 6.95 ms；预算 16.7 ms |
| 质量 | 33 个自检退出 0；`ruff check` 0 告警；决策指纹 `87bc274d413f52b5` / `827316f0cff596ff` 与 v3.4.4 逐字一致 |

---

## 0. 结论速览

| 级别 | 条目 | 一句话证据 | 状态 |
|---|---|---|---|
| **高** | F1 同名方法重复定义，修复被覆盖 | `MapView` 内 5 组重复定义；`_weather_layer` 生效的是旧 self-blit 版（行 931），新修复版（行 735）从未执行 | ✅ 已修 |
| **高** | F2 detect clamp 从未落地 | `detect.py:143` 仍是原始无 clamp 写法，3 个官方传感器 CONTACT 档不可达 | ✅ 已修 |
| **高** | F3 卫星情报不存档 | `save.py` 完全无 `sat_track`/`sat_recon`；读档后探测从「跟踪」塌到「未发现」 | ✅ 已修 |
| **高** | F4 规则触发状态不存档 | `save.py` 无规则 `fired`；`once` 规则读档后重复执行增援/伤害效果 | ✅ 已修 |
| **高** | F5 直接关窗丢失全部设置 | QUIT 与 `run()` 退出均无 `cfg.save()`；唯一落盘点是玩家点"完成" | ✅ 已修 |
| **高** | F6 按键捕获态残留 | 捕获中关闭设置再打开，第一次按键会静默改写上一个键位 | ✅ 已修 |
| **高** | F7 `test_perf` 恒真断言 | `check(True, ...)`，`changed=False` 时照样报 OK | ✅ 已修 |
| **高** | F8 便携版吞掉 `--check` 退出码 | zipapp 入口 `main.main()` 无 `sys.exit`，返回 1 仍以 0 退出 | ✅ 已修 |
| 中 | F9–F22 | 详见 §2–§5 | ✅ 已修 |
| 低 | F23–F26 | 详见 §2–§5 | ✅ 已修 |

**处理结果**：30 条全部修复，随 v3.5.5 发布。修复要点见 §6.1；
其中 F2/F3/F4/F10/F12 涉及规则行为，修复后已单独验证（决策指纹与 v3.5.4
一致——这几个场景不在指纹脚本的 6 回合窗口内，故用分档边界测试与
存档往返测试单独确认）。

---

## 1. 系统性发现：修复被同名后定义静默覆盖

### 1.1 F1（高）五组重复方法定义

对全仓所有 `.py` 做 AST 扫描（模块级与类级函数，记录每一次重名），结果只有 `ui/mapview.py` 命中：

| 方法 | 先定义 | 后定义（生效） | 两版是否相同 |
|---|---|---|---|
| `_draw_vignette` | 588 | **784** | 源码相同 |
| `_urban_tiles` | 635 | **831** | 源码相同 |
| `_glow_sprite` | 648 | **844** | 源码相同 |
| `_draw_city_lights` | 695 | **891** | 源码相同 |
| `_weather_layer` | 735 | **931** | **不同** |

复现：

```bash
python - <<'EOF'
import ast, pathlib
for p in pathlib.Path('.').rglob('*.py'):
    t = ast.parse(p.read_text(encoding='utf-8'))
    # 遍历 ClassDef.body，记录 FunctionDef 重名
EOF
/d/Dev/Python/python.exe -c \
 "import inspect; from ui.mapview import MapView; \
  print(inspect.getsourcelines(MapView._weather_layer)[1])"
# 输出 931，而不是 735
```

**`_weather_layer` 两版的实质差异**：

| 版本 | 实现 | 状态 |
|---|---|---|
| 行 735 | 先生成上半幅到独立 `top` 表面，再 `layer.blit(top,0)` / `layer.blit(top,h)` | **从未生效** |
| 行 931 | `layer.fill(..., 下半幅)` + `layer.blit(layer, ...)`（self-blit） | **实际生效** |

后者正是上一轮提交中判定为"零 alpha 处混合行为依 SDL 版本而异、CI Python 3.13 失败"的写法。

```python
s = inspect.getsource(MapView._weather_layer)
'self-blit' 检测: 'layer.blit(layer' in s  -> True
'独立上半幅' 检测: 'top = pygame.Surface' in s -> False
```

**为什么 CI 后来还是绿了**：上一轮同时把 `test_shake_stateless` 改成注入固定时刻，消除了时钟抖动；
因此在 CI 的 SDL 版本下天气 self-blit 恰好没触发差异。两个改动一个生效一个没生效，
绿的结果掩盖了修复缺失。

**根因不在代码，在编辑流程**：前几轮用脚本按 `text.index("    def _xxx(")` 定位插入新方法时，
若插入点选在"同名旧方法之前"，就变成新增而非替换。Python 允许同名方法重复定义，
`ruff` 默认规则不报，`check_names.py` 也不查——**没有任何门禁能发现这件事**。

**处理**：已删除 5 组重复中的旧副本（`_weather_layer` 保留独立上半幅版），
并新增 `tools/check_dupes.py` AST 门禁接入 CI——这条门禁的价值在 v3.5.5
当天就体现了一次：它要求脚本显式切 UTF-8，立刻抓出新脚本的编码缺陷。

### 1.2 前轮结论的复核

既然发现"修复可能被覆盖"，对前四轮的关键结论做了抽样复核：

| 抽查项 | 原状态 | 实际 |
|---|---|---|
| D5 `mitigation` 下界（`combat.py:222`） | 已修 | ✅ 确有 `1.0 - min(0.90, max(0.0, ...))` |
| D6 `detect` 比例 clamp（`detect.py:143`） | 计入"23 条已修" | ❌ **仍是原始无 clamp 写法**，见 F2 |
| 决策指纹 | 与 v3.4.4 一致 | ✅ 实测 `87bc274d413f52b5` / `827316f0cff596ff`，一致 |
| 5 组重复定义之外 | — | ✅ 全仓 AST 扫描只此 5 处，其余模块无重名 |

结论：**不是全面倒退，是单点遗漏 + 一类未被门禁覆盖的编辑缺陷**。

---

## 2. 规则、数据与存档

### 2.1 F2（高）detect clamp 从未落地，CONTACT 档对 3 个传感器不可达

**位置**：`core/detect.py:142-149`

```python
track_ratio  = 0.35 + 0.30 * s.strength
class_ratio  = 0.70 + 0.22 * s.strength     # ← 无上界
```

当 `strength > 1.36` 时 `class_ratio > 1.0`。而进入分档的前提是 `ratio = d/eff ≤ 1`，
因此 `elif ratio <= class_ratio` 恒真，**CONTACT 分支永远走不到**。

实测（全量遍历 28 个传感器）：

| 传感器 | strength | class_ratio | 结果 |
|---|---|---|---|
| `radar_e3` | 1.40 | 1.008 | CONTACT 不可达 |
| `radar_a50` | 1.45 | 1.019 | CONTACT 不可达 |
| `radar_kj500` | 1.45 | 1.019 | CONTACT 不可达 |

桩掉 `sensor_effective_range` 返回 `eff=10, los=True`，构造 `d=9.6`（ratio=0.96）：
`strength=1.0` 得到 CONTACT，`strength=1.4` 得到 CLASSIFIED。

`GAME_GUIDE.md:351-353` 明确承诺"接触：比值 ≤ 1.0"，与实现冲突。

**影响**：探测等级决定 `shot_profile` 的 `min_detection` 门槛与 AI 目标选择 → **影响决策指纹**（若修）。

### 2.2 F3（高）卫星情报不存档

**位置**：`core/save.py:208-238`（`game_to_dict`）、`core/engine.py:1725-1726`、`core/detect.py:194-198`

`save.py` 中 `sat_track` 出现 **0 次**、`sat_recon` **0 次**。

复现（`strait_confrontation`，蓝方卫星与目标置于 (1,1)）：

```
Tick 后:  sat_recon[BLUE]={48}  sat_track={48: TRACKED}
           detection_level = 跟踪
存档 → JSON 往返 → 读档 → refresh_detection()
          sat_track 1→0, sat_recon 1→0
          detection_level = 未发现
```

读档时不调用 `_tick_satellites`，空集一直维持到下一次回合推进。
玩家读档当回合无法对刚刚还在跟踪的目标开火。**影响决策指纹**（若修）。

### 2.3 F4（高）规则 `fired` 状态不存档

**位置**：`core/save.py:208-238`、`core/hooks.py:272-273,327-337`、`core/engine.py:1785`

存档保存了脚本事件的 `fired`（`save.py:454`），却漏了规则钩子的。

复现：注入 `once=True`、`when={type:turn_at_least,turn:1}`、`then=[{type:message}]` 的规则 →
触发后 `fired=True` → 存档读档 → `fired=False` → 再次求值**重新播报并重新执行效果**。

若效果是 `reinforce` / `damage` / `airstrike`（`hooks.py:354-445`），读档会凭空多出部队或范围伤害。

### 2.4 F9（中）设施打击命令不记录齐射发数

**位置**：`ui/app.py:2044-2047`、`core/replay.py:90-99`、`core/engine.py:955`

对单位开火在 `app.py:2066-2067` 显式记录 `salvo`，`CMD_FIRE_FACILITY` 漏了。

实测（`strait_shield`，M1A2 + `m256_120`，`max_salvo=3`）：

```
实机 salvo_setting=3 → strike_facility() → 弹药 8→5（消耗 3）
同种子回放命令流   → 回放方 salvo_setting=1 → 弹药 8→7（消耗 1）
```

命中判定仍成功，但弹药、伤害、拦截通道占用全不同。`salvo` 被存档保存却不进回放格式。
不影响 AI 决策指纹，但**破坏玩家命令回放一致性**。

### 2.5 F10（中）负 damage 越过 hp 上限

**位置**：`core/hooks.py:427-445` vs 同文件 `:448-458`

`_do_area_damage` 用 `u.hp = max(0.0, u.hp - eff.damage)`，无上界；
相邻的 `_do_area_heal` 却有 `min(template.hp, ...)`。

实测：`Effect(type='airstrike', damage=-50)` 命中 `集装箱超视距雷达`（template.hp=65）：
`hp 65.0 → 115.0`，`hp_ratio > 1`，战损加成异常且需多吃 50 点伤害才死。
`Effect.parse` 接受任意负数。**影响决策指纹**（若修）。

### 2.6 F11（中）事件 fired 以 turn 为键，同回合多事件串台

**位置**：`core/save.py:454-459`

`fired_map = {int(d.get("turn",0)): bool(d.get("fired")) ...}` —— 同一回合两条事件，后者覆盖前者。

官方 4 张图的 turns 为 `[] / [6,12,16,22,28] / [] / [3,2]`，**无重复**，故官方数据不触发；
SDK 开放的想定/模组可构造重复。属于数据契约边界。

### 2.7 F12（中）NaN 伤害制造"僵尸单位"

**位置**：`core/model.py:466-475`、`core/loader.py:114`

`json.load` 默认接受 `NaN` 字面量，`float()` 不排除。实测命中后：

```
max(0.0, nan) = 0.0
nan >= hp - 1e-9 为 False  → hp=0.0 但 destroyed 不变
结果: hp=0.0, destroyed=False, alive=False
```

单位仍占格，不计入 `units_of()`，也不进伤亡统计，`kill_unit` 不会被调用。
另 `min(0.97, nan)=0.97`，NaN 的 pk 被静默拉满。需数据注入触发。

### 2.8 F23（低）事件总线递归无深度保护

**位置**：`core/bus.py:122-154`

订阅者在回调里再次 `emit` 同名事件，递归 200 次：无异常、无 error 记录，
`history` 被有界截到 96 条，但产生 96 条伪造的"已发生事实"。
官方订阅者不主动重投，模组订阅者可触发。

### 2.9 F24（低）模组 `modify_damage` 是覆盖而非叠加

**位置**：`core/mods.py:603-619`

docstring 声称"多个模组可以叠加修正"，实际 `result = out` 覆盖式——
两个模组分别返回 ×2 与 +10 时，只有后注册的值生效。
引擎侧对返回值的 `float()` + `max(0.0,...)` + try/except 兜底**未发现问题**。

### 2.10 已检查且未发现问题的路径

- **JSON 数据契约**：全量遍历 65 模板 / 71 武器 / 28 传感器 / 15 挂载 / 9 编队 / 4 想定，
  除 F2 外无非法取值；`max_salvo ≥ salvo` 全部成立；100 条武器 override 无非法项。
- **存档字段往返**：`strait_shield` 3 回合对局 `game_to_dict → json → _restore_game → game_to_dict`
  递归 diff，除读档追加的 1 条日志外全字段一致，`rng.calls 31→31`。
  （F3/F4 属于"根本没存"，不是"存了读不回"。）
- **AI 可复现**：所有 `roll/jitter/shuffle/choice/randint` 引擎调用均显式传 `state.rng`；
  `PYTHONHASHSEED=0` 与 `=12345` 各跑 `main.py --sim 6 --map 1`，输出 diff 为空。

---

## 3. v3.5.x 视觉层的动态问题

### 3.1 F13（中）暂停期间镜头持续抖动

**位置**：`mapview.py:441-468`（墙钟振荡）、`ui/app.py:2821-2825`（暂停门控）

有 `burst` 事件时打开设置/帮助/简报：`st.tick_fx(dt)` 被跳过，`anim.events` 的 `ttl/life` 冻结，
但 `draw()` 仍每帧调用，`_shake_offset()` 继续读 `pygame.time.get_ticks()`。

实测（ttl=0.4，仅改变 now）：

```
now=0.000 -> (0,2)   now=0.010 -> (2,2)   now=0.030 -> (3,0)
```

即暂停期间地图**无限抖动**，与代码注释"不存在暂停时抖动不衰减的泄漏"直接矛盾。

### 3.2 F14（中）回放与截图基线不可复现

**位置**：`mapview.py:581-586`

雨/雪滚动偏移 `off = int(t*520) % h` / `int(t*58) % h` 完全由墙钟决定，不进入回放状态或截图签名。

实测：同一 `GameState` + 同一 `MapView`，mock `get_ticks` 为 0ms 与 1000ms：

```
像素是否相同: False     差异字节数: 4062
```

回放播放速度、暂停时长、机器启动时间都会改变雨雪颗粒位置；离线基线截图无法稳定比对。

### 3.3 F15（中）震动期间鼠标拾取与屏幕标记错位

**位置**：`mapview.py:473-484`（临时改 ox/oy）、`:146-151`（`screen_to_grid` 用恢复后的 ox）、
`ui/app.py:2861,2929-2932`

地形/单位按 `ox+shake` 绘制，但 `screen_to_grid()` 在 `draw()` 外运行、用原始 ox。
tile=10（最小档）时 3px 相当于 30% 格宽，格界附近点击会选中相邻格。
键盘光标与瞄准环也画在未震动坐标上，不随地图移动。

注：**未发现**重入或 `try/finally` 失衡问题；该处对称且类内无递归 `draw()`。

### 3.4 F16（中）`hsep` 两端渐隐底色硬编码

**位置**：`theme.py:987`、`ui/app.py:947,952`

`c = mix(_current.panel, base, a)` 假设面板底色恒为全局 panel。
战役选择菜单底色是 `mix(PANEL, PURPLE, 0.15)`（tactical 下约 `(38,36,60)`），
但渐隐端点按 `(13,18,26)` 计算。

实测最大通道差：`a=0.2 → 28 级`、`a=0.4 → 21 级`，在暗底上肉眼可见。
分隔线两端显出一条不属于该面板的暗色端帽。

### 3.5 已检查且未发现问题的路径

- **缓存键完整性**：`_env_cache` `(tint,alpha,rect.size)` / `_weather_cache` `(kind,w,h,tile)` /
  `_vignette_cache` `rect.size` / `_glow_cache` `(tile,level)` / `_urban_tiles` `(id(map),map_epoch)` /
  `_vector_icons` `(cls,color,size)` —— 逐一核对内容与键，未发现陈旧结论。
- **城市光晕开销**：`units_at()` 走位置索引、epoch 自校验，O(1)；不构成性能问题。
- **`panel`/`bar` bevel 边界**：尺寸门槛与亮度自适应齐全；light 主题无异常。
  问题只出在 `hsep`（F16）。
- **`_tile_atlas` 残留**：全仓无任何引用，v3.5.1 回退干净。

---

## 4. UI 交互、设置与无障碍

### 4.1 F5（高）直接关窗丢失全部设置

**位置**：`ui/app.py:2214-2215`（QUIT）、`:4540-4550`（`run()` 退出）、
`:714-722`（`close_settings()`，全项目**唯一** `self.cfg.save()`）、`main.py:215`

复现（mock `UserConfig.save` 计数）：

```
A after App():            save_calls=0  theme=tactical  scale=standard
B 切主题 light + 字号 xlarge: cfg 已改     save_calls=0
C QUIT 事件:              running=False  save_calls=0
D 点"完成":               save_calls=1  saved_theme=light
```

`_apply_theme` / `_apply_font_scale` / `_apply_language` / `_apply_icon_pack`（`app.py:626-692`）
只写内存不落盘。玩家若不点"完成"而直接关窗，主题、字号、键位全部回退。

### 4.2 F6（高）按键捕获态跨开关残留

**位置**：`ui/settings.py:169-174`（done 只置 `close_requested`）、`:82-95`（capturing 吞键）、
`ui/app.py:699-722`（面板实例复用，关闭不清 capturing）

复现：

```
1 点击"结束回合"进入捕获  capturing='end_turn'
2 捕获态直接点"完成"      close_requested=True, capturing='end_turn'
3 重开设置（同一实例）     capturing='end_turn', end_turn=space
4 按 Z                    end_turn=z  ← 静默改键，玩家无提示
```

后果：下次按空格无法结束回合，且界面没有任何反馈说明键位被改。

### 4.3 F17（中）字号与形状标识没有键盘焦点框

**位置**：`ui/settings.py:277-279`（`_focus_items` 含 `font_scale`/`shape`）、
`:654-694`（`_draw_focus` 的 if/elif 链没有这两个分支）

hook `pygame.draw.rect` 统计焦点框：

```
theme focus_index=0  amber_rects=1
font_scale =1  amber_rects=0   ← 缺失
shape      =2  amber_rects=0   ← 缺失
icons      =3  amber_rects=1
```

`←→` 确实能改值，但键盘用户看不到焦点在哪。与已修的滚动跟随是不同问题。

### 4.4 F18（中）暗角恒定开启，注释承诺的开关不存在

**位置**：`ui/mapview.py:53-55`（注释称"无障碍设置可关"）、唯一的读取点 `:606,802`

全项目 `_vignette_on` 仅 3 处引用：初始化 1 处、读取 2 处，**无 setter、无 config 键、无 UI**。
12 层黑色环（峰值 alpha=44）始终叠加在最上层。

### 4.5 F19（中）不存在"减少动态效果"开关

**位置**：全仓 grep `reduce_motion|motion|动效|减少动态|quality|画质` → **0 匹配**；
`ui/settings.py:277-279` 的 8 个焦点项无一动效相关。

`core/anim.py:50-61` 有 `AnimTrack.enabled`，但 UI 未暴露、config 未持久化。
即便程序内设 `anim.enabled=False`，**天气滚动、暗角、城市光晕仍照常绘制**——
这三者不读 `anim.events`。

`VISUAL_PLAN.md` 自身也矛盾：`:438-439` 承诺"可在设置里关闭""所有新增动效必须响应
减少动态效果设置项"，而 `:339` 写"不需要额外的无障碍开关"。本轮以实际代码为准：
**承诺未兑现**。

### 4.6 已检查且未发现问题的路径

- **回放/观战只读**：`app.py:2306-2349`（键盘）、`:2547-2554`（回放鼠标）、
  `:2625-2636`+`:2725-2726`（观战侧栏守卫）均无条件 return，未发现新写入路径。
- **字号下限 10**：`smoke_test.py:1006-1040` AST 扫描仍有效；自建更宽松扫描仅命中
  `app.py:1531 T.FS_SMALL-1`（=10，等于下限），无越界。
- **四主题对比度**：补测"夜 + 雪 + 暗角 alpha=44"最坏组合，`map_label` 对比度
  tactical 4.07 / light 3.63 / deuteranopia 4.02 / protanopia 4.03，均高于 `GRAPHIC_MIN=3.0`。
- **异常路径**：坏 JSON、非 dict 配置、缺文件、字体全缺、640×480 与 1×1 窗口均不崩溃
  （小窗口被钳到 1180×740，代价是画面裁剪）。
- **主题切换后缓存重建**：`_terrain_epoch()` 含主题 id；`_apply_theme` 等清 `_token_cache` /
  `_vector_icons` / 图标缓存。未发现残留。

---

## 5. CI、门禁、打包与文档

### 5.1 F7（高）`test_perf.py` 恒真断言

**位置**：`tools/test_perf.py:181-185`

```python
changed = any(state.detection[Side.BLUE].get(k) is not v for k,v in before.items())
check(True, f"单位移动后重新解算（{changed} = 相关结论有变化）")
```

`changed` 只被拼进消息。增量缓存完全失效时门禁照样绿。

变异验证（内存）：把 `changed=False` 传进去 → `[OK] 单位移动后重新解算（False = 相关结论有变化）`，
`FAILED=[]`，退出码 0。**断言文案自己承认没变化，仍然报 OK**。

### 5.2 F20（中）另两处假绿断言

| 脚本 | 行号 | 问题 |
|---|---|---|
| `test_counter.py` | 136-144 | `lo,hi = 9.0,0.0` 哨兵 + `except: continue`；`mult()` 全抛异常时 `0.50<=9.0 and 0.0<=1.46` 恒真，实测输出 `[OK] 全域乘数落在 [9.00, 0.00]` |
| `test_validate.py` | 108-111 | `else: check(True, ...)`；校验器若不再识别"需机动接近"，门禁失效 |
| `balance_check.py` | 225-228 | `except Exception: continue` 静默跳过坏想定；全部加载失败时 findings/errors/warnings 全 0（CI 有 `--check` 兜底，故风险较低） |

### 5.3 F21（高）CI 装了中文字体，字形门禁却永远 SKIP

**位置**：`.github/workflows/ci.yml:55-63`、`tools/smoke_test.py:1063`

CI 注释明写"装字体让字形门禁真正生效"，Linux 执行 `apt-get install fonts-noto-cjk`；
但断言条件是 `if _cjk_files and os.name == "nt"`。9 组矩阵中 8 组是 Linux/macOS，
全部走 `[SKIP]` 分支。

上一轮的结论是"Windows 强制断言、其他平台 SKIP"——但既然 CI 已装字体，
这个折中就不必要了。**装字体的成本在付，门禁的收益没拿到**。

### 5.4 F8（高）便携版 `--check` 恒绿

**位置**：`packaging/make_portable.py:67-69`

```python
f.write("import main\nmain.main()\n")    # 返回值被丢弃
```

`main.main()` 返回 `int`（`main.py:194-216`），zipapp 入口无 `sys.exit`。
实测 mock `check_data=1`：

```
main() 返回值 = 1     进程退出码 = 0
sys.exit(main.main()) → 退出码 1
```

`build_windows.ps1:50-51` / `build_linux.sh:40` 只对 PyInstaller 产物跑 `--check`，
**没有任何流水线对 `.pyz` 自检**，所以这个恒绿端口不会被发现。

### 5.5 F22（中）Windows 打包脚本不受 PyInstaller 路径门禁

**位置**：`tools/check_scripts.py:173-180`

docstring 承诺"打包脚本必须显式指定 `--distpath/--workpath`"，
但分发只对 `.sh` 调用 `check_pyinstaller_paths`。spy 实测：实际检查的脚本只有 `['build_linux.sh']`，
`build_windows.ps1` 删掉 `--distpath` 也无人报警。

### 5.6 F25（中）`event-late` 降级为提示，CI 恒绿

**位置**：`main.py:69-75,87-90`、`core/validate.py:527-530`

只统计 `errors`，warn/info 只打印。实测 `main.py --check` 退出码 0，但输出含：

```
[提示] event-late 事件 4（增援 1×ddg_burke 于 (14,58)）的回合 22 超出想定回合上限 20，永远不会触发
[提示] event-late 事件 5（增援 1×j20 于 (90,44)）的回合 28 超出想定回合上限 20，永远不会触发
```

增援永不触发是功能缺陷，不是权衡性提示（`port_inland` 降级为提示是既定决策，不重复报）。

### 5.7 F26（低）源码包混入缓存

**位置**：`packaging/make_portable.py:85-101`

`skip_dirs` 不含 `.ruff_cache`、`.github`。实测陈旧
`packaging/dist/fangkuai-bingqi-source.zip` 130 条目含 `.ruff_cache/0.17.3/*` 与已删除的 `ROADMAP.md`。
CI 因 `.gitignore` 且每次重建不受影响；本地手动打包会带上。

### 5.8 文档自洽问题

| # | 位置 | 问题 |
|---|---|---|
| F27 中 | 旧 `AUDIT.md:7,26` | 正文是第四轮（v3.4.4）现场，基线标题却被逐版本替换成 v3.5.4；正文残留"与 v3.4.2/v3.4.3 逐字一致"自相矛盾，Q3 仍写"CI 无 macOS"（实际已有），统计写"52 步 8 组"（实测 54 步 9 组）。**本文件替换后解决** |
| F28 中 | `VISUAL_PLAN.md:438-439,456` vs `:339` | 承诺"减少动态效果开关"与"不需要额外开关"自相矛盾；实测开关不存在（F19）。另 `:438` 写震动 ≤4px，实现与测试为 ≤3px |
| F29 低 | `ROADMAP_HISTORY.md` 末章 | 停在 v3.4.4，缺 v3.5.0–v3.5.4；`README.md:265-269` 有记录，归档出现断点 |
| F30 低 | 动画种类计数 | `VISUAL_PLAN.md:64,222` 称 16 种；`AUDIT` 旧版称 8 种；`core/anim.py:16-23` 文档化 8 种 anim + `mapview.py` 6 种 fx。三方口径不一 |

**未发现**：版本号五处（`version.py` / README / GAME_GUIDE / SDK / AUDIT 基线）与最新 tag 全部为
v3.5.4，`check_docs.py` / `check_sdk.py` 全绿；打包产物无陈旧资产（CI 每次重建）。

---

## 6. 建议处理顺序

本轮只审计不修。若决定处理，建议按"修复真实性 → 数据一致性 → 交互 → 门禁 → 文档"排序：

| 顺序 | 条目 | 理由 |
|---|---|---|
| 1 | F1 | 先消除"修复可能被覆盖"的编辑缺陷，否则后续修复仍可能静默失效。加 AST 重名门禁 |
| 2 | F2、F3、F4 | 三条都影响决策或战后结论；且 F2 是前轮"声称已修"的遗漏，需同步修正历史记录 |
| 3 | F5、F6 | 玩家可见的配置损坏，后果是"设置没保存""键位莫名改变"，信任成本高 |
| 4 | F7、F8、F21、F22 | 门禁与发布链路的假绿；这些不修，前面三类问题会继续漏过 CI |
| 5 | F9–F19 | 回放一致性、数值边界、动态视觉的可复现与无障碍 |
| 6 | F20、F25、F26、F28–F30 | 剩余门禁漏洞与文档自洽 |

**注意**：F2/F3/F4 若修，决策指纹会改变。按项目既定约束，这类改动必须走
"实测前后指纹对比 + 深战术 A/B 不退化"的流程，不能只补断言就合并。

---

### 6.1 修复摘要（v3.5.5）

| 条目 | 修法 | 验证方式 |
|---|---|---|
| F1 | 删除 5 组重复定义中的旧副本（`_weather_layer` 保留独立上半幅版），新增 `tools/check_dupes.py` AST 门禁并接入 CI | `inspect.getsourcelines` 确认生效行 735；全仓 AST 扫描 0 重复 |
| F2 | `class_ratio = min(0.98, ...)`、`track_ratio = min(0.96, ...)`，并保证单调 | 分档边界测试：ratio=0.99 现返回 CONTACT（原为 CLASSIFIED） |
| F3 | `save` 增加 `sat.recon` / `sat.track`，读档还原 | 存档往返：`sat_recon` 与 `sat_track` 逐项一致 |
| F4 | `save` 增加 `rules_fired`（按索引），读档还原 | 字段往返测试 |
| F11 | 事件改为按索引存 `fired`，旧档回退到 turn | `events` 每条含 `i` 字段 |
| F5 | QUIT 与 `run()` 退出调用 `_save_config_quietly()`（失败不阻断退出） | spy `cfg.save` 计数 |
| F6 | 新增 `SettingsPanel.reset_transient()`，`close_settings` 调用 | 复现脚本：捕获态关闭后重开，`capturing` 为空 |
| F7 | `check(True, ...)` 改为"位置世代递增 + 增量与全量逐项一致" | 自测通过；原断言下 `changed=False` 会被捕获 |
| F8 | zipapp 入口改为 `sys.exit(main.main())` | 代码审阅 + 变异验证 |
| F9 | `CMD_FIRE_FACILITY` 记录 `salvo`，`replay` 读取并传入 | 与 `CMD_FIRE` 对称 |
| F10 | `_do_area_damage` 对非正 damage 按 0 处理 | 负伤害不再提升 hp |
| F12 | 加载期 `_finite()` 校验 pk/damage；`apply_damage` 拒绝非有限值 | `apply_damage` 源含 `isfinite` |
| F13/F14 | 新增 `MapView.clock`，由 App 在**非暂停时**推进；天气与震动相位改读它 | 暂停时 clock 增量 0；同一 clock 两帧像素一致 |
| F15 | `screen_to_grid` 补偿 `_last_shake` | 与绘制坐标同源 |
| F16 | `hsep(bg=...)`，战役菜单传真实面板底色 | 调用点显式传 `mix(PANEL,PURPLE,0.15)` |
| F17 | `_draw_focus` 补 `font_scale` / `shape` 分支 | 焦点框计数各 1 |
| F18/F19 | `UserConfig.motion`（full/reduced）+ 设置面板一行 + 8 语言；renders 层读 `T.motion_reduced()` | 减弱档：`anim.enabled=False`、震动 (0,0)、天气偏移固定 0、暗角跳过 |
| F20 | 三处假绿断言改为"先收集再判空 / else 判失败 / 记录 warning" | 各脚本自测通过 |
| F21 | 字形门禁去掉 `os.name=="nt"` 限制 | 本地 Windows 仍通过 |
| F22 | `.ps1` 也走 `check_pyinstaller_paths` | 立即抓出 `check_dupes.py` 缺 UTF-8（自证有效） |
| F23 | `EventBus._depth` + `MAX_DEPTH=8`，超限抛错（被引擎兜底记录） | 递归测试：8 层即停，错误可读 |
| F24 | 修正 docstring：链式覆盖而非叠加 | 文档与实现一致 |
| F25 | `event-late` 升为 error；把超限的两条增援事件收到 18/20 回合 | `main.py --check` 退出 0 且无 event-late |
| F26 | 源码包排除 `.ruff_cache/.github/.pytest_cache/.mypy_cache` | 跳过目录清单 |
| F28–F30 | 统一震动上限 3px、动效开关描述、动画计数口径；补 v3.5.x 历史 | `check_docs` 通过 |

## 7. 审计覆盖范围

- **AST 静态扫描**：全仓 `.py` 的类级/模块级函数重名（发现 5 处）。
- **规则与数据**：65 模板 / 71 武器 / 28 传感器 / 15 挂载 / 9 编队 / 4 想定的字段定义域；
  存档字段往返递归 diff；`PYTHONHASHSEED` 双值决策指纹对比。
- **视觉层**：6 类缓存的键与失效时机逐一核对；天气层像素级周期一致性；
  震动无状态性、幅度上限、镜头复原；四主题叠加暗角后的对比度补测。
- **交互层**：模态优先级、回放/观战只读路径、字号 AST 守卫、异常输入与极小窗口、主题切换缓存。
- **门禁层**：32 个自检的断言结构抽查；CI 54 步逐一核对；打包脚本与 zipapp 入口；
  变异验证 4 处假绿。
- **实测环境**：`D:/Dev/Python/python.exe`（3.12.4 / pygame 2.6.1 / SDL 2.28.4），
  `SDL_VIDEODRIVER=dummy`；审计阶段未修改项目文件，所有变异在内存中完成。

---

## 已知的机制边界（有意简化，不是缺陷）

> 以下 12 条与 `GAME_GUIDE.md` §19 一致，不属于待修问题。

1. 探测是「等级」而非连续置信度。
2. 炮弹飞行按整回合抽象，不模拟弹道时间。
3. 电子战是频段压制，不建模具体协议。
4. 工事是标量抵御值，不区分朝向与结构。
5. 士气与疲劳未建模。
6. 后勤只算「是否断粮」，不算弹药消耗速率。
7. 空中单位按简化任务剖面行动，不做完整空战格斗。
8. 卫星过顶是固定窗口，轨道不推演。
9. 地形对机动的影响是离散档位。
10. 同格共存有上限，超过后按规则挤占。
11. 回放记录命令而非每一步内部状态。
12. 模组钩子不能改变规则结论，只能调权重与加表现。

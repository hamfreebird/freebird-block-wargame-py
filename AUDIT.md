# 项目审计报告

> 这份文件取代了原来的 `ROADMAP.md`。路线图回答「接下来做什么」，
> 审计回答「现在到底怎么样」——功能堆到 45 项机制、41k 行代码之后，
> 后一个问题更能决定下一步。
>
> **本版为第二轮审计（2026-11-18，基于 v3.4.2 / 待发版）**。
> 第一轮（v3.4.1）列出的 10 条问题已全部关闭；第二轮（本文件正文）列出的 22 条也已在 v3.4.2 全部关闭；本轮从零重新测一遍，
> 找的是**当时没测到的地方**。结论：规则层核心干净，但**三条测试闸门
> 实际上没守住东西**、**主循环里有一条每帧 100ms 的路径此前从未被测过**。
>
> 全文没有推测性结论：每一条都给出**实测数值**或**可点开的代码位置**。
> 实测统一用 `D:/Dev/Python/python.exe`（3.12.4 / pygame 2.6.1）；
> 系统 `python`（3.13）无 pygame，不能用于 UI 测试。

## 当前基线（v3.4.2）

| 维度 | 现状 |
|---|---|
| 玩法内核 | 45 项机制：侦察—打击链、逐发拦截、中段反导、电子战与多频段诱饵、信火一体、工事体系、烟幕、任务剖面、卫星过顶窗口、反卫星、设施打击、齐射、对手视角威胁评估、战场天气与时段、补给线与纵深、指挥体系、多点防空协同、火力准备、波段对抗、同格共存、对局回放、AI 观战、想定与回放分享、可复现随机流、模组接口 |
| 数据 | 65 兵种 / 71 武器 / 28 传感器 / 15 挂载预设 / 9 编队预设（`main.py --check` 实测） |
| 想定 | 4 张（海峡对决 128×128、草原雷霆 36×36、海峡屏障 40×40、新兵训练场 20×14） |
| 引导 | 教学想定 + 12 步事件驱动引导（`training_ground.json` 实测 12 步） |
| 规则 | JSON 声明式规则钩子（12 种条件 × 9 种效果） |
| 呈现 | 8 种战斗动画 · 4 套主题（含 2 套色盲友好）· 字号三档 · 色盲形状标识 · 9 种音效 · 8 种语言 |
| 对手 AI | 规则启发式（默认）· **本地大模型指挥官**（LFM3 1.7B，可选，只决定作战决心） |
| 代码规模 | core 42 文件 16,639 行 · ui 15 文件 11,694 行 · tools 46 文件 14,492 行 · `main.py` 219 行 |
| 自检 | 31 个 `test_*` / `check_*` 脚本；CI 52 个步骤（verify 42 + balance 5 + package 5），8 组 OS×Python 组合 |
| 实测性能 | `MapView.draw` 128×128 **4.1ms** / 256×256 **4.0ms**；**完整 `App.draw` 128×128 98.5ms / 256×256 373.8ms**（缩略图占 97%，见 §1.1） |
| 内存 | 256×256 三回合 RSS 326→318→305MB，无单调增长；距离场 151/149/155 条；地形块 66 块 6.8MB |
| 质量 | `diag 20` 三张图 0 错误；31 个自检名义全绿（其中 2 个闸门实测无效，见 §2）；`ruff check` 0 告警 |

> 与第一轮的差异：第一轮测的是 `MapView.draw`，本轮补测了**完整 `App.draw`**，
> 才发现缩略图这条每帧 100ms 的路径。第一轮把「31 个自检全绿」当作质量结论，
> 本轮做了变异测试，发现其中两条即使断言失败也照样退出 0。

---

## 0. 结论速览

| 级别 | 条目 | 一句话证据 | 状态 |
|---|---|---|---|
| **高** | P1 缩略图每帧逐格重绘 | 128×128 实测 98.77ms/帧、256×256 377ms/帧；`perf.py` 完全没测这条路径 | ✅ 已修 |
| **高** | P2 `smoke_test` 后半段断言失效 | 注入必失败断言，退出码仍 0，照打「全部通过」 | ✅ 已修 |
| **高** | P3 `lang_check` 覆盖率不是门禁 | 删光韩文词条，覆盖率 0%，退出码仍 0 | ✅ 已修 |
| **高** | P4 空袭事件绕过统一阵亡出口 | 全歼场景下战损 +1，但广播 0 次、`over=False` | ✅ 已修 |
| 中 | P5 `--sim` 不可复现 | 同命令连跑两次存活数 15/13 vs 15/14 | ✅ 已修 |
| 中 | P6 韩文字形渲染为豆腐块 | `msyh.ttc` 渲染「한」与 U+E000 像素完全相同；系统已装 `malgun.ttf` 未纳入 | ✅ 已修 |
| 中 | P7 最小窗口＋特大字号主菜单越界 | 1180×740 / xlarge 下 4/4 按钮 `bottom=764 > 740` | ✅ 已修 |
| 中 | P8 等宽字体漏假名/谚文降级 | Consolas 下「リ」「리」均为豆腐块，`mono=True` 未触发降级 | ✅ 已修 |
| 中 | P9 色盲主题下中立/敌方目标点同色 | protanopia 对比度 1.04:1，且形状一致 | ✅ 已修 |
| 中 | P10 `test_perf` 绝对阈值抖动 | 同一份代码 6.52ms 判失败，重跑 1.99 / 1.42ms 通过 | ✅ 已修 |
| 中 | P11 `check_sdk` 枚举校验盲区 | 新增未文档化枚举后仍退出 0；16 个枚举只校验 11 个 | ✅ 已修 |
| 中 | P12 编辑器无法纯键盘完成核心操作 | 战役编辑器无键新增/切关；地图编辑器平移仅中键拖拽、缩放仅滚轮 | ✅ 已修 |
| 中 | P13 环境色调层在大视口成为第二热点 | 2560×1440 夜雾+雪整帧 11.24ms，环境占 53% | ✅ 已修 |
| 低 | 9 条低优先级项 | 补给扫描 / 回放膨胀 / 诱饵事件 / 烟幕缓存 / 快捷键字号 / 文档失真等，详见 §1.3–1.4、§3.2–3.3、§4.5、§5 | ✅ 已修 |

**没有发现规则结论错误**：`diag 20` 三张图 0 错误，决策指纹逐条一致；
`test_perf` 的 206 项探测结论对比 0 不一致。本轮问题集中在
**测试有效性、主循环渲染、本地化字形与界面可达性**。

---

## 1. 性能

### 1.1 P1：缩略图是主循环里最贵的一帧（高）

**这是本轮最重要的发现，也是第一轮审计的测量盲区。**

`ui/mapview.py:1262` 的 `draw_minimap()` 每帧对**整张地图逐格调用
`pygame.draw.rect`**，并在循环里做 `T.elevation_shade()` 与动态主题属性查找；
没有任何缓存。`ui/app.py:2715` 每帧调用它，`MapView` 本身不知道这件事——
`tools/perf.py` 只测 `MapView.draw`，所以此前的「整帧 3.8ms」是**假的低**。

实测（`/d/Dev/Python/python.exe`，dummy 视频驱动，3 轮×10 帧中位数）：

| 地图 | 尺寸 | `draw_minimap` 单项 | 完整 `App.draw` |
|---|---|---|---|
| 新兵训练场 | 20×14 | 1.34ms | — |
| 草原雷霆 | 36×36 | 7.88ms | — |
| 海峡对决 | 128×128 | **98.77ms** | **98.48ms**（其中 `MapView.draw` 仅 2.50ms） |
| 合成大地图 | 256×256 | **377.4ms** | **373.8ms**（minimap 占 97%） |

cProfile（128×128，10 帧）佐证：`draw_minimap` 累计 1.679s / 总 1.773s；
`theme.py:284(__getattr__)` 调用 **329,840 次**、`elevation_shade` 163,840 次、
`pygame.draw.rect` 165,300 次。主循环目标 `clock.tick(60)`，实际约 10fps（128）
到 2.7fps（256）。

复现方式：

```
python -c "
import os,time,pygame;os.environ['SDL_VIDEODRIVER']='dummy';pygame.init();
s=pygame.display.set_mode((1500,920));
from core.loader import load_scenario
from core.paths import data_root
from core.engine import GameState
from core.enums import Side
from ui.mapview import MapView
sc=load_scenario(os.path.join(data_root(),'data','maps','strait_confrontation.json'),
                 os.path.join(data_root(),'data','units','armory.json'))
st=GameState(sc,Side.BLUE,seed=2);st.refresh_detection()
v=MapView(pygame.Rect(0,0,1180,860),st);v.draw(s)
r=pygame.Rect(1180,8,300,220);ts=[]
for _ in range(12):
    t=time.perf_counter();v.draw_minimap(s,r);ts.append((time.perf_counter()-t)*1000)
print(sorted(ts)[6])"
```

**改法**：把缩略图的**静态地形层**烘进一张缓存表面，签名
`(rect.size, map_epoch, theme.id)`；单位点与视窗框作为动态层每帧叠加。
签名**不要**含 `ox/oy`，否则平移镜头会让静态层反复失效。
临时验证：缓存命中后 0.02ms/帧（首帧构建仍约 90/377ms）。

### 1.2 P13：环境色调层的成本与像素数线性相关（中）

`ui/mapview.py:469-494` 对**整个视口**做 SRCALPHA blit（色调 + 雨/雪）。
实测 `steppe_thunder`（3 轮×30 帧中位数）：

| 视口 | 仅色调 | 色调+天气 | 整帧 |
|---|---|---|---|
| 800×600 | 0.32ms | 0.65ms | — |
| 1600×900 | 1.06ms | 2.14ms | 1.82→4.08ms（+124%） |
| 2560×1440 | 3.18ms | 5.92ms | 11.24ms（环境占 53%） |

白天晴天 `envonly=0.00ms`；色盲主题无额外开销（四主题 2.17–2.28ms）。
改法：大视口下把天气层降到半分辨率再放大，或改用整屏 fill。
优先级低于 P1——修完 P1 后它才是第二热点。

### 1.3 补给源全图扫描（低）

`core/logistics.py:60` 的 `supply_sources()` 每次枚举都遍历全图，`engine.py:1418`
在每回合与设施占领/摧毁时重算。实测中位数：20×14 **0.05ms**、40×40 0.22ms、
128×128 3.05ms、256×256 **8.80ms**（44 个源）。改法：按 `map_epoch` 或
设施归属版本缓存源列表。

### 1.4 回放文件 `indent=2` 膨胀 2.2 倍（低）

`core/replay.py:281` 用 `json.dump(..., indent=2)`。实测 5,000 / 15,000 / 50,000
条命令：文件大小 0.61 / 1.82 / **6.10MB**，紧凑格式 0.27 / 0.82 / 2.76MB；
写盘 71 / 133 / 403ms。命令流自身内存仅 0.3 / 0.8 / 2.8MB，加载 33ms——
不是瓶颈，属于「顺手改」级别。

### 1.5 已实测的性能基线（无问题）

| 项目 | 条件 | 中位数 |
|---|---|---|
| MapView 整帧 | 128×128 / 256×256 ×3 | 4.1ms / 4.0ms |
| 探测全量重算 | 128×128 / 256×256 | 2.1ms / 11.1ms |
| 首帧建块 | 128×128 / 256×256 | 239.6 / 262.1ms（48 / 66 块） |
| 距离场命中率 | 128×128 逐回合 5 次 | 94.4–95.9%，0 淘汰 |
| 小地图整帧 | 36×36 / 20×14 | 4.1ms / 2.9ms |
| 视口缩放 | 800×600 → 2560×1440 | 1.23 → 7.60ms |
| 内存 | 256×256 三回合 | RSS 326→318→305MB，无泄漏 |
| 地形块预算 | 64MB 上限 | 66 块 6.8MB，未越界 |
| 启动 | `main.py --check` ×3 | 3.94s |
| 模块导入 | `ui.app` 累计 | 438ms（pygame.surfarray/numpy 194ms） |
| 大模型冷启 | 1.1GB GGUF | 冷 8.70s / 热 1.73s（`boot_timeout` 90s，不会误判） |

> 曾观察到「距离场缓存 90% 条目是过期 epoch」，交叉验证后是**测量时点假象**
> （`map_epoch` 在回合内递增），不列为问题。这轮特意记下来，避免下次重复踩。

---

## 2. 测试有效性

**本轮最值得警惕的一类问题：自检跑绿了，但根本没守住。**
以下三条都用变异测试坐实（在 `/tmp` 隔离副本注入缺陷，原项目未改）。

### 2.1 P2：`smoke_test` 后半段 18 个断言形同虚设（高）

`tools/smoke_test.py:1082` 就做了 `if FAILED: ... return 1`，
但此后**还有 18 个 `check()` 调用**（v2.2.1「普通想定不得挂教学」、
v3.2 观战控制条、分歧判读等），函数末尾再无第二次 `FAILED` 判定，
最终无条件打印「冒烟测试全部通过」并 `return 0`。

变异验证：在第 1086 行之后注入 `check(False, "[MUTATION]...")`：

```
  [FAIL] [MUTATION] 故意让后半段断言失败，验证是否影响退出码
冒烟测试全部通过
[PIPE_EXIT=0]
```

README 声称 smoke_test 有 212 项断言——其中后半段全部不计入退出码。

### 2.2 P3：`lang_check` 覆盖率不是门禁（高）

`tools/lang_check.py:130` 的 `report()` 中，覆盖率 `worst=0.0` 时只打印
「最低覆盖率 0.0%」，函数照常 `return 0`；CI（`.github/workflows/ci.yml:159`）
跑的正是这个默认模式。真正的失败只在 `--verify <code>` 分支，CI 从不调用。

变异验证：删光 `data/lang/ko*.json`：

```
  ko   한국어          内置   0 条（磁盘语言包不存在）
最低覆盖率 0.0%——用 --missing <代码> 查看缺哪些词条
✓ 调用点参数个数全部匹配
✓ UI 引用的词条键全部存在
[PIPE_EXIT=0]
```

注意这与 P6 的关系：8 语言「740/740 100% 覆盖」是真的（数词条），
但词条存在 ≠ 字形能显示、≠ 源码真的调用了 `tr()`。

### 2.3 P11：`check_sdk` 枚举校验有盲区（中）

`tools/check_sdk.py:156-158` 硬编码 11 个枚举名，
`core.enums` 实际有 16 个业务枚举；`Side`、`JamBand`、`Echelon`、`OrderKind`
完全不在清单内，新增枚举也不触发失败。

变异验证：向 `core/enums.py` 追加一个未文档化的 `NewFancyEnum`，
`check_sdk.py` 输出 `✓ SDK 文档与代码一致`，退出 0。
新增假 API（`zzz_never_documented_api`）**会**被抓出——说明符号清单部分是真的，
盲区只在硬编码枚举表。

### 2.4 P10：`test_perf` 绝对阈值抖动（中）

`tools/test_perf.py:45-46` 用硬编码 `DETECT_LIMIT_MS=6.0ms` 判失败，无重试。
同一份代码首次实测 `探测重算耗时 6.52ms < 6.0ms` 退出 1，
紧接重跑两次 1.99 / 1.42ms 退出 0。与 §1.5「perf.py 不设阈值是因为
共享机器差 20%」的思路矛盾。建议取多次中位数或改相对阈值。

### 2.5 断言强度稀释（中）

`grep -c 'check(True' tools/smoke_test.py` = 19、
`test_campaign_editor.py` 有 6 处「`ed.draw(screen); check(True, "...可渲染")`」，
只证明不崩，不验证绘制内容。真实跳过项应单列 `skip()` 统计，
不计入「N 项通过」。

### 2.6 P5：`--sim` 不可复现（中）

`main.py:151` 的 `random.seed(1234)` 是死代码——`GameState(sc, Side.BLUE)`
不传 seed，取系统熵；引擎随机全走 `state.rng`，全局种子不起作用。

```
run1: 存活：蓝 15 / 红 13 ｜ 战损：蓝 1 / 红 1
run2: 存活：蓝 15 / 红 14 ｜ 战损：蓝 1 / 红 0
```

docstring 写的是「用于平衡性测试与回归验证」——这两件事都需要可复现。
改法：`GameState(sc, Side.BLUE, seed=1234)`。
注意观战、`diag`、平衡体检不受影响（它们另有 seed 入口）。

### 2.7 变异验证过、确实有效的闸门

| 闸门 | 变异 | 结果 |
|---|---|---|
| `check_docs` README↔AUDIT 版本 | 徽章改 v9.9.9 | 退出 1，`[FAIL] 版本号不一致` |
| `check_contrast` 地图标签 | tactical `map_label` 改暗 | 退出 1，报 1.02:1 / 1.28:1 |
| `check_sdk` 符号清单 | 加假 API | 退出 1 |
| `check_docs` 目录/锚点 | — | 165 个目录链接全部有效 |

**已核查无问题**：`test_save`（随机流整份往返、坏档不崩、迁移链连续）、
`test_bus`（五条阵亡路径）、`test_debt`（五条自动失效/早退）、
`test_replay` / `test_share`、`test_units`（40 项）、`test_i18n`、`test_a11y`、
`check_anchors`、`check_layout`、`check_names`、`check_scripts` 均为真校验。

---

## 3. 规则与代码风险

### 3.1 P4：空袭事件绕过统一阵亡出口（高）

`core/events.py:243-247` 的 `_do_airstrike()` 直接记账：

```python
u.apply_damage(dmg)          # model.py:469 内部置 destroyed=True
if u.destroyed:
    state.casualties[u.side] += 1        # ← 绕过 state.kill_unit()
```

实测（只剩 1 个红方单位、hp=1，空袭 damage=50）：

```
前 (alive=True, casualties=0, over=False)
后 (alive=False, casualties=1, over=False)
广播 0 次 | bus 计数 unit_destroyed=0
```

对照：同语义的 `core/hooks.py:434` 走 `state.kill_unit(u, "scenario_event")`；
直接调 `kill_unit` 的对照实验广播 1 次。
后果有三：① `unit_destroyed` 订阅者（模组、UI、教学）收不到；
② `_check_elimination()` 不跑，全歼不判胜负；③ 违背 SDK.md 的契约。
`test_bus.py` 只覆盖了 hooks 路径，没覆盖脚本事件路径。

改法：`state.kill_unit(u, "scenario_airstrike")`。

### 3.2 诱饵被击毁不广播，与到期行为不一致（低）

`engine.py:787-794` 命中诱饵时直接 `target.destroyed = True` 后 return；
而 `engine.py:1695` 的诱饵到期走 `kill_unit(..., count_casualty=False)` 并广播。
同为「诱饵消失」，事件总线行为不一致。
注：`Unit.__setattr__` 会在 `destroyed` 写入时递增位置世代，索引不受影响——
问题只在事件广播与契约一致性。

### 3.3 烟幕列表缓存缺地图身份（低）

`ui/mapview.py:418` 的 key 是 `(turn_count, map_epoch)`，不含 `id(state.map)`。
新开局 `map_epoch=0`（`engine.py:134`）、读档 `map_epoch += 1`（`save.py:422`），
因此两局若回合数与 map_epoch 恰好相同，会 blit 上一张地图的烟幕 Tile。
同文件其他缓存均含 `id(md)`（地形块）或 `id(st.map)`（补给线），此处是遗漏。

### 3.4 已核查确认无问题的部分

- **随机来源**：core 业务随机全走 `state.rng`；全局 `random` 仅 `dice.py`
  兜底（有 `FALLBACK_CALLS` 计数 + `set_strict`）与 `rng.new_seed()`（SystemRandom）。
  唯一例外是 §2.6 的 CLI 入口。
- **uid 分配**：开局按想定顺序重编号并重置模块计数器；增援/脚本/钩子/战役/
  诱饵全部走 `state.alloc_uid()`。`SAVE_VERSION=6`。
- **存档**：整份保存 `rng.getstate()`（含 seed/calls/version/internal/gauss_next）；
  字段异常统一翻译为 `SaveError`；迁移链 5→6 连续；新增字段缺失取默认。
- **事件三处一致**：`bus.CATALOGUE` / `mods.HOOK_SIGNATURES` / `SDK.md §5.4`
  逐项对齐（P4 是**调用点**问题，不是清单不一致）。
- **异常吞噬**：`except Exception: pass` 10 处逐一读过，均为可接受降级
  （图标/音效/窗口图标/句柄清理/坏回放标记），未发现「功能静默消失」。
- **模块边界**：`rules.py` 仅 re-export；跨模块调用写成 `mod.f(...)`。

---

## 4. 视觉、可用性与本地化

### 4.1 韩文界面是豆腐块（中）

`ui/theme.py:522` 的 `_FONT_CANDIDATES_CN` 只有中文字体，首选 `msyh.ttc`；
系统已装 `C:\Windows\Fonts\malgun.ttf` 但未纳入。实测像素比较：

```
msyh: 한 == U+E000（豆腐块）? True
malgun: 한 == U+E000? False
```

`lang_check.py` 只数词条，不管字形。`lang=ko` 时按钮「한국어」显示为 `□□□`。

### 4.2 等宽降级漏假名/谚文（中）

`ui/theme.py:642` 的 `_CN_CHARS = r"[　-〿一-鿿＀-￯]"` 只覆盖 CJK 与全角。
实测 Consolas 字形与 U+E000 像素比对：

| 字符 | Consolas 下是豆腐块 | `render_text(mono=True)` 已降级 |
|---|---|---|
| 回 | 是 | 是 |
| リ | **是** | **否** |
| 리 | **是** | **否** |
| П / A | 否 | 否（符合预期） |

回放进度条 `tr("ui.replay_progress_fmt")` 正是 `mono=True`，日/韩词条分别含
「リプレイ」「리플레이」，必现。改法：正则扩到 U+3040-30FF / U+AC00-D7AF，
或改为「非纯 ASCII 可打印字符就不走 mono」。`smoke_test` 只用「传感器 5.0」
测中文降级，覆盖不到这两类。

### 4.3 最小窗口＋特大字号主菜单越界（中）

`ui/app.py:368` 的 `_menu_button_y()` 只跟卡片区底边走，不按 `self.h` 钳制。
实测 1180×740：

| 字号 | 越界按钮 | 证据 |
|---|---|---|
| standard | 0/4 | — |
| large | 1/4 | `_menu_settings_rect: right=1297 > 1180` |
| xlarge | **4/4** | 四个按钮 `bottom=764 > 740`，完全不可点击 |

`check_layout.py` 的最小窗口检查只覆盖对局界面（`_check_min_size`），
主菜单矩形只在默认 1500×920 下检查，因此自检通过。
顺带：主菜单底部提示钉在 `self.h - 92`（`app.py:1521`），xlarge 下与卡片文字重叠。
改法：`_menu_button_y()` 加 `min(self.h - T.m(72), ...)` 钳制，并把主菜单
纳入最小窗口检查。

### 4.4 色盲主题下中立与敌方目标点无法区分（中）

`ui/mapview.py:738-755` 中立目标点用 `T.AMBER`、敌方用 `side_color(RED,"edge")`，
两者都画成菱形。实测对比度：

| 主题 | 中立 | 敌方 | 对比度 |
|---|---|---|---|
| tactical | (255,196,92) | (248,124,124) | 1.63:1 |
| light | (150,92,8) | (206,62,58) | 1.14:1 |
| deuteranopia | (255,214,120) | (255,198,120) | **1.11:1** |
| protanopia | (255,200,110) | (255,206,120) | **1.04:1** |

`check_contrast.py` 测的是 `map_symbol vs terrain`，不测「中立 vs 敌方」这个语义对。
改法：中立改用 `side_neutral.edge` 或 `MAP_SYMBOL`，并加空心/实心形状差异，
把该语义对加入自检。

### 4.5 快捷键字号通过变量传参绕过 AST 检查（中）

`ui/widgets.py:79-89`：标签过长时 `for cand in range(size-1, 7, -1)` 可降到 8px；
快捷键提示用 `self.size - 11`，默认 `size=13` 时只有 **2px**（实测渲染尺寸 3×4），
实质不可读。`smoke_test` 只查位置参数为字面 int 的 `draw_text`，
`size = cand` 与 `self.size - 11` 都漏检。
同类：`ui/editor.py:1132/1169` 的 `max(8, tile//3)` / `max(7, tile//4)`。
改法：下限统一用 `T.FS_TINY`，并把 AST 检查扩展到变量/关键字参数与 `render_text`。

### 4.6 编辑器无法纯键盘完成核心操作（中）

战役编辑器（`ui/campaign_editor.py:274-302`）无键新增关卡、无键切换选中关卡
（UP/DOWN 是 `move_step` 重排），Tab 不切换输入框焦点。
地图编辑器（`ui/editor.py:371-434`）平移仅中键拖拽、缩放仅滚轮；
`app.py` 与回放播放器的缩放同样只有 MOUSEWHEEL，无 `+`/`-` 键。
`test_keyboard.py` 覆盖设置/主菜单/系统菜单/对局/帮助，未覆盖两个编辑器与缩放。
改法：补 Tab/Shift+Tab 焦点循环、新增/切关快捷键、方向键平移与 `+`/`-` 缩放。

### 4.7 主题名等 UI 文案未接入本地化（中）

实测 `i18n.set_language("en")` 后：

```
tr('settings.theme') = 'Theme'
T.theme_name('tactical') = '深色战术终端'
T.font_scale_name('large') = '大'
T.shape_mode_name('auto') = '自动'
```

AST 扫描另发现 22 处赋值形式的中文状态文案（如
`campaign_editor.py:119 "已新建战役"`、`editor.py:913 "该挂载预设为空"`），
以及 `campaign_editor.py:636 "滚动查看更多"`、`mapview.py:610 "射界/标定"`
两处直接绘制的中文常量。`lang_check.py` 不扫源码硬编码。
改法：接入 `tr()` 并补 8 语言词条；自检增加「`tr()` 之外的中文 UI 字面量」扫描。

### 4.8 底栏操作提示对比度不足（中）

`ui/app.py:4169` 的操作说明用 `T.TEXT_FAINT`，四主题实测 2.70–2.98:1
（tactical 2.75 / light 2.70 / deuteranopia 2.98 / protanopia 2.90），
低于 WCAG AA 4.5:1。`check_contrast.py` 明确把 `text_faint` 列为
「辅助说明，未纳入强制」——但这是键盘用户发现快捷键的主要途径，属功能性提示。
改法：新增 `text_hint` 令牌并纳入强制阈值，或把这类调用提升到 `TEXT_DIM`。

### 4.9 已核查无问题的部分

- **锚点合法性**：7 种锚点全覆盖校验，运行时有 `ValueError` 兜底
  （注：注释写「9 个」是文档失真，见 §5）。
- **中文等宽降级**：中文路径像素级一致；西里尔/拉丁 Consolas 有字形。
- **信息卡语义**：探测等级同时有文字标签，HP 同时有数值，非纯颜色区分。
- **补给线/火力准备**：断粮红叉 vs 正常绿虚线、准备实线圈 vs 虚线圈，
  形状差异在色盲下仍可辨。
- **设施中立色**：用 `MAP_SYMBOL` 灰色，与敌方区分（§4.4 只针对目标点）。
- **设置面板键盘**：31 个焦点项，`_ensure_visible()` 处理滚动。
- **语言热切换**：`tr()` 每次查表，缓存键含文本，无需重启。
- **棋子形状标识**：`test_a11y.py` 像素级验证 mbt/sam/fighter 切角与角标。

---

## 5. 文档失真

| # | 位置 | 写的 | 实际 |
|---|---|---|---|
| D1 | `README.md:33` | 「6 步带你走完核心循环」 | 教学想定 12 步（同文件 :131 又写 12 步） |
| D2 | `core/tutorial.py:179` | docstring「标准新手引导脚本（4 步）」 | 回退脚本返回 6 步；代码里有「第 6 步」标题 |
| D3 | `tools/check_anchors.py:3`、`ui/theme.py:671`、`README.md:133` | 「9 种锚点」 | `frozenset` 实际 7 个，脚本自报也是 7 种 |
| D4 | 上一版 `AUDIT.md:22` | 「45 个 CI 步骤」 | 实测 52 个（verify 42 + balance 5 + package 5） |
| D5 | 上一版 `AUDIT.md:24` vs `:417` | 「31 个自检全绿」vs「跑了 9 个自检」 | 后者是 v3.4.0 历史陈述，未标时点 |

> D4/D5 由本报告的重写一并修正。D1–D3 需改源码/文档，本轮未改。

**已确认未失真**：数据规模（65/71/28）、代码行数、主题 4 / 音效 9 / 语言 8、
版本号五处一致（`GAME_VERSION`、README 徽章、AUDIT 基线、SDK 抬头、git tag）、
GAME_GUIDE 二级标题 1–20 连续、165 个目录锚点全有效、已知机制边界 12 条。

---

## 6. 大模型指挥官（v3.4 新增路径）

### 6.1 实测

| 项 | 数值 |
|---|---|
| 服务启动 | 冷 8.70s / 热 1.73s（`boot_timeout=90s`） |
| 单次研判 | 7.5–13.4s（39 token） |
| 常驻内存 | 约 1.4GB（独立进程） |
| 推理期间整帧 | 3.29ms vs 空闲 3.35ms（p90 4.57 / 4.48）——不掉帧 |
| 决心就绪后 AI 相位 | 0.3s 内开始动作 |

### 6.2 为什么不把动作交给模型

1.7B 输出不了可靠的「uid + 路径 + 武器」动作串（错一格就撞山、进雷场、
把弹药打光），而 `AIPlanner` 在「怎么走、打谁、打几发」上更强。
所以模型只选四个枚举字段，规则层折算成权重。模型再离谱，最坏结果是
「打得更莽」，不会作弊、不会产生非法动作——它根本不产生动作。

### 6.3 已知弱点（不是 bug，是模型能力）

- 实测 6 次研判全给 `assault`：能改主攻方向，判断不出「该固守」。
- 倾向选高价值点而不看可达性；`EFFORT_GAIN` 只放大偏好，不强制执行。
- 自动下载脚本只覆盖 Windows x64。

### 6.4 对可复现性的影响

中性决心 == 不传决心，动作序列逐条一致（`test_llm` 第一条）。
决心随 `end_turn` 入命令流（回放 v3）。观战 / `--sim` / `diag` /
平衡体检不启用模型，仍只由种子决定——但见 §2.6，`--sim` 本身没传 seed。

---

## 7. 建议的处理顺序

**先修「让检查变真」的三条**，否则后续任何改动都无法验证是否修好：

1. **P2 + P3**（半天量级）：把 `smoke_test` 的 `FAILED` 汇总移到函数末尾；
   `lang_check report()` 增加 `worst < 100 → return 1`，或 CI 改为逐语言 `--verify`。
   验收：注入必失败断言 → 退出 1；删光某语言包 → 退出 1。
2. **P11 + P10**：`check_sdk` 改为动态遍历枚举；`test_perf` 改中位数/相对阈值。
3. **P1**（半天量级）：缩略图静态层缓存。验收：128×128 完整 `App.draw`
   从 98.5ms 降到 5ms 以内；把这条加进 `tools/perf.py`。
4. **P4 + P5**：修正阵亡路径与 CLI seed。验收：全歼场景广播 1 次且
   `over=True`；`--sim 3` 连跑两次输出逐字一致。
5. **P6–P9**：字体回退链、主菜单边界钳制、等宽正则、目标点形状差异。
6. 其余低优先级穿插处理。

**不建议做**：`ui/app.py` 4367 行拆分——机械但量大，应与修 bug 分开评审。
**暂不做**：AI 单步 p99 压平——长尾来自首次建场，只能拆帧或换近似距离，
后者会改变 AI 结论，需单独立项评估。

---

## 8. 审计覆盖范围

本轮实际执行：

- 四个维度并行审阅：①代码结构与规则正确性 ②性能实测
  ③UI/无障碍/本地化 ④测试质量与文档一致性
- 31 个自检脚本全跑一遍（30 个退出 0，`test_perf` 首次退出 1 后重跑通过）
- 变异测试 6 组：`smoke_test` 后置断言、`lang_check` 覆盖率、
  `check_sdk` 新枚举、`check_docs` 版本、`check_contrast` 配色、`check_sdk` 假 API
- 实测复现 12 项：空袭记账、`--sim` 种子、缩略图耗时、韩文字形、
  主菜单越界、补给扫描、等宽字形、色盲目标点色差、`test_perf` 抖动等
- `main.py --check`、`ruff check .`、`check_docs`、`check_sdk`、`lang_check` 全过

未覆盖：真实鼠标操作下的多窗口并发、macOS/Linux 的字体可用性、
大模型在真实 1.1GB 权重下的多轮连续决策稳定性（只做了单轮端到端）。

---

## 已知的机制边界（有意简化，不是缺陷）

> 以下 12 条与 GAME_GUIDE §19 一致，不属于待修问题。

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

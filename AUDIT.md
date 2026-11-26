# 项目审计报告

> 这份文件取代了原来的 `ROADMAP.md`。路线图回答「接下来做什么」，
> 审计回答「现在到底怎么样」。
>
> **本版为第六轮审计（2026-11-27，基于 v3.5.6 / 已发布）**。
> 全部 29 条已修复，修法与验证见 §7.1。
> 前五轮共处理 107 条问题（v3.4.1 的 10 条、v3.4.2 的 22 条、v3.4.3 的 22 条、
> v3.4.4 的 23 条（另 3 条评估不修）、v3.5.5 的 30 条）。
>
> **本轮换的角度**：第五轮问「声称修好的东西是否真的生效」，本轮把这个问题
> 再推一步，并加三个此前没查过的维度：
>
> 1. **修法的回归**——v3.5.5 改了 43 文件 / +2515 / −1963 行，这些修复本身
>    有没有引入新问题，或者修法本身就是空操作；
> 2. **长时运行漂移**——反复开局、读档、切主题、缩放后缓存是否收敛；
> 3. **同一规则的多处实现**——UI 显示值与规则判定值是否来自同一个函数。
>
> 结果：发现 **2 条上轮修复仍然无效**、1 条从 v3.5.3 起就存在但被测试
> 盲区掩盖的严重视觉缺陷（震动幅度可达 **337 px**）、以及 6 条玩家可见的
> 规则显示分叉。前五轮的自检全绿依旧成立——问题在**自检没有覆盖的地方**。
>
> 全文没有推测性结论：每条给出**实测数值**或**可点开的代码位置**。
> 实测统一用 `D:/Dev/Python/python.exe`（3.12.4 / pygame 2.6.1 / SDL 2.28.4），
> `SDL_VIDEODRIVER=dummy`。审计阶段未修改任何项目文件；所有变异在内存中完成。

## 当前基线（v3.5.6）

| 维度 | 现状 |
|---|---|
| 玩法内核 | 45 项机制：侦察—打击链、逐发拦截、中段反导、电子战与多频段诱饵、信火一体、工事体系、烟幕、任务剖面、卫星过顶窗口、反卫星、设施打击、齐射、对手视角威胁评估、战场天气与时段、补给线与纵深、指挥体系、多点防空协同、火力准备、波段对抗、同格共存、对局回放、AI 观战、想定与回放分享、可复现随机流、模组接口 |
| 数据 | 65 兵种 / 71 武器 / 28 传感器 / 15 挂载预设 / 9 编队预设 |
| 想定 | 4 张（海峡对决 128×128、草原雷霆 36×36、海峡屏障 40×40、新兵训练场 20×14） |
| 规则 | JSON 声明式规则钩子（11 基础条件 + all/any/not 3 组合器 × 9 种效果） |
| 呈现 | 4 套主题（含 2 套色盲友好）· 字号三档 · 色盲形状标识 · 9 种音效 · 8 种语言；新增「动效：完整/减弱」无障碍开关 |
| 代码规模 | core 42 文件 16,932 行 · ui 15 文件 12,674 行 · tools 49 文件 15,326 行（全行口径） |
| 自检 | `tools/` 下 34 个 `test_*`/`check_*` 脚本（25 + 9）；CI 9 组 OS×Python 组合；PR #15 24 项检查全绿 |
| 实测性能 | 完整 `App.draw`：晴昼 5.75 ms / 晴夜 7.02 ms / 雨昼 9.48 ms（最坏）/ 雪昼 6.95 ms；预算 16.7 ms |
| 质量 | 34 自检退出 0；`ruff` 0 告警；`diag 20` 零错误；决策指纹 `87bc274d413f52b5` / **`6d5b0374e0c10161`**（海峡图因补给/设施/中段弹修复而变化；A/B 对局胜负与 VP 不变） |

---

## 0. 结论速览

| 级别 | 条目 | 一句话证据 | 状态 |
|---|---|---|---|
| **高** | G1 震动幅度公式反向 | `p = 1 - life/ttl`，而 `tick` 递减 ttl → p 恒 ≤0；实测 0.39 s 时偏移 **337 px** | ✅ 已修 |
| **高** | G2 上轮 F20 修复是空操作 | `Report.warnings` 是 property，返回新列表；`rep.warnings.append(...)` 写到临时对象即丢弃 | ✅ 已修 |
| **高** | G3 齐射上限 UI 与规则分叉 | 断补给时 UI 给 1–3 发并显示 90.8%，规则 `salvo_limit=1` 实际只发 1 发（54.8%） | ✅ 已修 |
| **高** | G4 对空伤害三个口径 | 面板 51.4 / 日志 52 / 实际掉 95（一击必杀）；大型机实际 ×2.2 | ✅ 已修 |
| **高** | G5 `_detect_cache` 无上限 | 1000 回合 56,884 条 / 13.84 MB，99.4% 条目永不再命中 | ✅ 已修 |
| **高** | G6 上轮 F21 的 Droid 回退不可达 | 候选不重试：第一个 TTC 加载失败后直接落到 `Font(None)`，纯 TTF 永不尝试 | ✅ 已修 |
| 中 | G7–G20、G22、G25 | 详见 §2–§5 | ✅ 已修 |
| 低 | G21、G23、G24、G26–G29 | 详见 §2–§6 | ✅ 已修 |

**处理结果**：29 条全部修复。修复摘要见 §7.1；其中 8 条改变规则行为，已用 A/B 对局验证胜负与 VP 与 v3.5.5 完全一致（148:112 / 574:364）。

---

## 1. 两条"上轮修复仍无效"与一条严重视觉缺陷

### 1.1 G1（高）震动幅度公式反向：越接近爆炸结束抖得越猛

**位置**：`ui/mapview.py:476-478`，配合 `core/anim.py:71-80`

```python
# anim.tick(): e.ttl -= step     ← 递减的是 ttl，life 保持不变
# mapview:
p = 1.0 - e.life / e.ttl
if p >= 0.35:
    continue                       # 永远不会成立
amp = (1.0 - p / 0.35) * 3.0       # p 越负，amp 越大
```

`life` 是初始时长常量、`ttl` 是剩余时长。随着动画推进 `life/ttl` 从 1 涨到无穷，
`p` 从 0 掉到 −39，「衰减」变成「放大」。

实测（ttl=0.4，逐次 tick，采样 200 个时刻取最大偏移）：

| 已过时间 | 最大偏移 |
|---|---|
| 0.00 s | 3 px |
| 0.10 s | 6 px |
| 0.20 s | 12 px |
| 0.30 s | 29 px |
| 0.35 s | 63 px |
| 0.39 s | **337 px** |

**为什么五轮审计都没抓到**：`tools/test_visual.py:207-212` 只测
`st.anim.add(..., ttl=0.4)` 之后**立刻**调用 `_shake_offset(now=0.0)`，
此时 `ttl == life`、`p == 0`，"≤3 px" 恒真。测试从未 tick 过动画。

**v3.5.5 让它更明显**：改用逻辑时钟后，暂停/结算时 `ttl` 与 `clock` 同时冻结，
地图会停在一个**恒定的非零偏移**上（击杀帧约 3 px，中途暂停可达 12 px+），
边框与格线错位。`screen_to_grid` 会补偿，点击仍与画面一致，但视觉上像渲染故障。

修法：`p = 1.0 - e.ttl / e.life`，并让测试覆盖动画中段与末段。

### 1.2 G2（高）上轮 F20 修复是空操作

**位置**：`tools/balance_check.py:73`（`warnings` property）、`:230`、`:279`

v3.5.5 为了修"想定加载失败被静默跳过"，把裸 `continue` 改成了
`rep.warnings.append(...)`。但 `Report.warnings` 是：

```python
@property
def warnings(self) -> list[Finding]:
    return [f for f in self.findings if f.severity == "warn"]
```

每次访问都生成**新列表**，`append` 写到临时对象后立即丢弃。

复现（内存变异 `load_scenario` 使其全抛 `LoadError`）：

```
预期：至少一条"想定加载失败"结论
实际：findings=[]，summary() 为"共 0 条结论"，main() 返回 0
```

即便改成 `rep.add("warn", ...)`，`main()` 仍只对 `rep.errors` 返回 1——
注释承诺的"想定全部加载失败不能判绿"也没有实现。修法：用 `rep.add("error", ...)`。

### 1.3 G6（高）上轮 F21 的纯 TTF 回退永远不会尝试

**位置**：`ui/theme.py:611`（`_first_existing`）、`:687-694`、`tools/smoke_test.py:1061-1090`

v3.5.5 在候选列表末尾加了两条 Droid **纯 TTF** 路径，试图绕开 Linux 上
pygame 加载 Noto TTC 失败的问题。但加载逻辑是：

```
_first_existing(候选) → 取第一个存在的文件 → pygame.font.Font(path)
                                          → 失败则直接 Font(None)
```

**没有"加载失败就试下一个候选"的重试**。

复现（hook `pygame.font.Font`，让第一个候选抛错）：

```
calls == ['README.md', None]      # 第二个候选从未被尝试
```

Linux CI 装出的 Noto TTC 排在 Droid 之前，所以当 F21 的前提成立时，
新增的两条 Droid 路径**恰好不可达**。这解释了为什么本轮 CI 里
`smoke_test` 仍然只能降级为 WARN。

### 1.4 已确认：上轮其余修法的真实回归检查

| 抽查项 | 结果 |
|---|---|
| 旧档（v3.5.4 的 v6 档）读入 v3.5.5 | ✅ 去掉 `sat`/`rules_fired`/`i` 后正常，`sat_recon` 为空、5 条事件保留 |
| 新档被旧版读 | ✅ 仍保留 `events[].turn`，旧版按 turn 解析可跑（新字段被忽略属预期） |
| 事件按索引恢复 | ✅ 内存构造同回合两条 `fired=True/False`，往返后仍为 `[True, False]` |
| `EventBus._depth` | ✅ 正常/递归/异常三条路径后均归零，无泄漏 |
| `hsep(bg=)` | ✅ 战役面板传的色与 `T.panel` 底色一致，无端帽残留 |
| 设置面板 8 行布局 | ✅ `check_layout` 三档字号均通过 |
| `event-late` 阈值 | ✅ 4 张官方图 errors 均为 0；`turn==limit` 可达（`> turn_limit` 才结束） |
| `check_dupes` 有效性 | ✅ 用 v3.5.4 的 mapview 实测能报出 5 处重复（但覆盖不到 `if/try`，见 G24） |

---

## 2. 玩家可见的规则显示分叉

### 2.1 G3（高）齐射上限：UI 给 3 发，规则只让发 1 发

**位置**：`ui/app.py:3980-3984`（`_salvo_options`）、`core/combat.py:53-61`（`salvo_limit`）

```python
# UI：只看 max_salvo 与备弹
cap = max(1, min(int(w.max_salvo), u.ammo.get(w.id, 0)))
# 规则：补给紧张时只能单发
if unit.supply_state >= 1:
    return 1
```

实测（BLUE M1A2 打相邻 T-90M，`m256_120`，`supply_dry=1`）：

```
_salvo_options(...) → [1, 2, 3]       UI 显示 3 发、命中率 1-(1-0.548)^3 = 90.8%
rules.salvo_limit(...) → 1            fire() 实际只发 1 发，弹药 8→7，命中率 54.8%
```

侧栏与设施打击面板都调用 `_salvo_options`。玩家在断补给状态下看到的
齐射选项与命中率是**虚假的可选项**。

### 2.2 G4（高）对空伤害：面板 51、日志 52、实际 95

**位置**：`core/combat.py:212-245`（`ShotProfile.damage`）、
`core/engine.py:869-880`（`_impact`）、`ui/app.py:4058`（面板显示）

```
if target.domain is Domain.AIR:
    if target.template.one_shot_kill:
        target.hp = 0.0; target.destroyed = True       # 完全无视 prof.damage
    else:
        total_damage *= 2.2                            # 结算乘系数，面板不知道
```

实测：

| 目标 | 面板 `prof.damage` | 日志"伤害" | 实际掉血 |
|---|---|---|---|
| Su-35（one_shot_kill） | 51.4 | 52 | **95 → 0** |
| B-52（大型机） | 49.9 | — | **111.8**（≈2.2×） |

`st.stats` 与导出战报按未乘系数的 52 记账。同一发弹药出现三个数字。

### 2.3 G13（中高）站在补给枢纽上反而"不解渴"

**位置**：`core/engine.py:1448-1485`（`service_tile` 分支）vs `:1318-1352`（`_resupply_by_line`）

实测（M1A2，`supply_dry=3, fuel=0.10`，站在己方 DEPOT (1,6)）：

```
站 DEPOT：   supply_dry=3（仍枯竭，salvo_limit=1、命中×0.88）、fuel=0.10
离开一格：   supply_dry=0、salvo_limit=3、fuel=0.35
```

`service_tile` 分支不修改 `supply_dry`，也不给地面/水面单位加油。
单位停在补给枢纽上会永久卡在枯竭状态。

### 2.4 G14（中）"保底移动"在可达集里可达，实际永远不可达

**位置**：`core/pathfind.py:199-237`（`fallback_moves` 返回含 `step_cost` 的终值）、
`:184-195`（`find_path` 又乘一次）、`:287-295`（`path_cost` 用地形原价）

实测（mp=0.5 的单位放在 `move_cost=3.5` 的山地）：

```
flood_reachable → 8 邻格全部 cost=1.0（UI 高亮"可达"）
find_path → path_cost=4.95 > mp 0.5 → order_move 拒绝，提示"机动点不足"
```

代码注释自述"128×128 海峡对决有 12/99 个单位靠保底才能动"，影响面不小。

### 2.5 G15（中）威胁圈与真实防空网两个方向都错

**位置**：`ui/mapview.py:819-827` + `ui/inspect.py:89-92` 用
`Unit.max_intercept_range()`（`core/model.py:428-433`，只取 SAM/AAM range_max）；
判定在 `core/adn.py:97-150`（要求有拦截弹、有 pk、有通道）

| 单位 | `max_intercept_range` | UI 威胁圈 | ADN 实际节点 |
|---|---|---|---|
| `llsn` | 16.0 | 画 16 格红圈 | **0**（`interceptors=0`，根本不能拦） |
| `cvn` | 0.0 | 不画圈 | **1**（CIWS 末段硬杀伤） |

### 2.6 G16（中）"半径 N 格"有两套几何与两套伤害模型

**位置**：`core/events.py:209-254`（欧氏 + 距离衰减）vs
`core/hooks.py:427-450`（切比雪夫方框 + 定量伤害）

中心 (10,7)、半径 6、伤害 10，同偏移下两条路径的扣血：

| 偏移 | 欧氏距离 | hooks | events |
|---|---|---|---|
| (6,0) | 6.00 | 10 | 5 |
| (4,4) | 5.66 | 10 | 5 |
| (5,5) | 7.07 | 10 | **0** |
| (0,5) | 5.00 | 10 | 6 |

`reveal` 同样分叉：`(6,6)` 处 hooks 暴露=True、events 暴露=False。
想定作者把效果从 `events` 改写成 `rules` 会得到不同结果。

### 2.7 G17（中）AI 硬门槛 TRACKED，永不调用"识别级即可射击"的炮兵

**位置**：`core/ai.py:321-333`（`if known_level < TRACKED: continue`）vs
`core/combat.py:114-118`（用武器的 `min_detection`）

实测（`paladin` 的 `m777_155`，`min_detection=classified`，目标情报=CLASSIFIED）：

```
rules.shot_profile(...)      → ok=True,  Pk=0.278, dmg=17.0
AIPlanner._best_shot(...)    → None
把情报改成 TRACKED           → ('T-90M','m777_155')
```

玩家可以打的炮兵，AI 永远不用。属于行为级弱化，玩家可感知。

### 2.8 G18（中）战报统计与引擎统计两套口径

**位置**：`core/report.py:41-82`（对日志文本做字符串匹配）vs
`core/engine.py:808-847`（`st.stats` 按发数累加）

实测：

```
两次直射命中（日志" A → B｜武器 命中 1 发…"含 → 但不含"发射"）：
  引擎：shots=2, hits=2
  战报：shots=0, hits=2, hit_rate=0.0  → 打印"发射 0｜命中 2（0%）"

一次设施打击命中：
  引擎：shots=0, hits=0（strike_facility 完全不写 stats）
  战报：hits=1（日志含"命中 1 发"）
```

打诱饵的"命中空目标"也会被战报记成一次命中，可产生 >100% 命中率。

### 2.9 G28（低）探测信息门槛三处不同

同为 `CLASSIFIED` 级：地图画血条（`mapview.py:1173-1203`）、悬停卡显示精确
`HP x/y`（`inspect.py:72-80`）、侧栏交战面板**不显示** HP（`app.py:4040`）。
另 `app.py:2917` 等处用魔法数 `>= 2` 代表 CLASSIFIED，而非 `Detection.CLASSIFIED.level`。

### 2.10 补充（低）

- `ui/inspect.py:101` 的"我方能否威胁它"漏了 `RANGE_TOLERANCE`，且内层循环后
  无条件 `break`——只检查第一支地面部队。第一支是无武器的 `radar_tpy2` 时，
  旁边的坦克也不会让信息卡显示"威胁"。
- `ui/app.py:3717` 武器行显示 `Pk {w.pk}`（基础值）与交战面板 `prof.pk`（态势值）
  并列，标签同为"Pk"，实测可差一倍以上。

---

## 3. 长时运行与缓存漂移

### 3.1 G5（高）`_detect_cache` 是唯一无上限、无淘汰的缓存

**位置**：`core/engine.py:140`（初始化）、`core/detect.py:206,216-224`；
全仓无 `.clear()`、无长度判断。

缓存键含 `obs.detect_gen` / `target.detect_gen` / `state._detect_gen`，三者都单调递增，
旧键**永远不会再命中**，但从不淘汰。

| 场景 | 条目 | 估算字节 |
|---|---|---|
| 新建 10 局（每局初始） | 112 / 局 | ~20 KB / 局 |
| 40×40 对局 60 回合 | 7,706 | 1.82 MB（99.4% 键已死） |
| 40×40 对局 120 回合 | 10,666 | 2.40 MB |
| **纯推进 1000 回合** | **56,884** | **13.84 MB** |

官方图回合上限 16–20，正常对局约 2–3 千条 / ≤1 MB；本条在长战役或
高回合上限模组下才显著。跨局随 `GameState` 释放，不累计。

### 3.2 G7（中）`_token_cache` 键缺 `player_side`，换阵营后棋标陈旧

**位置**：`ui/mapview.py:1219-1232` 的失效指纹与缓存键都不含 `player_side`，
但 `:1257-1264` 的绘制用它决定"己方绿边"与"敌方切角"。

实测：蓝方开局生成红方 T-90M 精灵 → 同一 MapView 换成红方玩家的新
`GameState` → 取到**同一个精灵对象**，与全新 MapView 的基准相差 180/1024 字节。
`start_game` 换边、`load_slot` 读另一阵营存档都会触发。

### 3.3 G8（中）缩略图静态层被 `map_epoch` 过度失效

**位置**：`ui/mapview.py:1587`，键含 `self.state.map_epoch`；
而静态层只画地形+海拔，与工事/烟幕/设施无关。

实测（128×128）：缓存命中 **0.106 ms**，重建 **73.56 ms**（694×）。
`map_epoch` 因烟幕、工事构筑/风化、设施打击等事件频繁递增，
每次都会在下一帧造成约 74 ms 卡顿。

### 3.4 G9（中）`_field_cache` 旧世代条目 100% 驻留

**位置**：`core/pathfind.py:347,371,397-403`，上限 192 条，按插入顺序 FIFO 淘汰 1/4（非 LRU）。

实测（40×40，60 回合 AI 对战结束）：`map_epoch=50`，154 条 / 2.72 MB，
键的世代 ≠ 当前世代者为 **154/154 = 100%**。旧距离场全部是死数据，
直到 192 条上限才被 FIFO 清掉。

### 3.5 G10（中）`T._text_cache` 按条数封顶，字节峰值约 70 MB

**位置**：`ui/theme.py:718-719,745-746`，`_CACHE_LIMIT = 3000`，超过即整表清空。

实测 2,900 条唯一字符串（尚未触发清空）：

| 档位 | 串长 | 像素字节 |
|---|---|---|
| standard | ~10 字 | 30.2 MB |
| standard | ~40 字 | 41.5 MB |
| xlarge | ~10 字 | 54.8 MB |
| xlarge | ~40 字 | **68.9 MB** |

条目收敛，但字节上限由"条数 × 单条面积"决定；达到 3,000 条时整表重建。

### 3.6 G11（中）LLM `_boot_failed` 粘滞，一次并发 prefetch 永久降级

**位置**：`core/llm_commander.py:428-429,382,480-489`、`core/llm_client.py:284-290`

`LLMClient.start()` 在另一线程正在启动时返回 `False`，而 `_run` 把任何
`False` 都当作启动失败置 `_boot_failed=True`；`available` 永久受它影响，
`reset()` 也不清。

实测（fake client 精确复刻非阻塞抢锁）：第 1 回合 prefetch 开始 0.4 s 启动，
第 2/3 回合的 prefetch 得到 `False` → `starts=1`、`client.ready=True`、
`_boot_failed=True`、`available` **True→False**、`last_error` 为空。

只要玩家在模型加载完成前结束一次回合，大模型指挥官在整个 App 生命周期内
都不可用，换局也不恢复。

### 3.7 G27（低，POSIX）异常退出可能遗留 `llama-server`

正常退出在 `app.py:4592-4603` 调 `commander.stop()`，异常分支无 `finally`；
Windows 靠 Job Object 兜底，`llm_client.py:75-76` 在 POSIX 直接返回 `None`。
未在 POSIX 实机复现，属代码路径审阅。Windows 侧实测 5 次失败→`stop()` 后
无句柄/进程残留。

### 3.8 未发现跨局累积（附实测）

| 循环 | 指标 | 前 → 后 |
|---|---|---|
| `start_game → over → menu` 10 次（40×40） | `tracemalloc` | 3.163 → 3.163 MB |
| 同上（128×128） | `tracemalloc` | 15.36 → 15.36 MB |
| 连续 `load_game` 10 次 | 对象数 | 48,947 → 48,978（GC 后 −15） |
| 切主题 20 次 | `tracemalloc` | 持平 |
| 缩放 50 次 | `tracemalloc` | 7.35 MB 持平，chunk 4.8 MB < 64 MB |
| `id(map)` 复用压力 500 次 | 复用次数 | **0 / 500** |

`_chunks` 是真正的按字节 LRU；`bus.history/errors`、`state.log/fx/sfx` 均有界。
全屏层缓存（env/weather/vignette）在 1180×700 约 19.8 MB，属固定预算，不是泄漏。

---

## 4. 规则层其余边界与数值

### 4.1 G12（中）detect 的 0.98 上限过紧，多截断了 4 个传感器

**位置**：`core/detect.py:146-149`（v3.5.5 F2 的修法）

官方 28 个传感器中只有 3 个 `class_ratio ≥ 1`，但 `min(0.98, ...)` 把 4 个
原本合法的也一起截断：

| 传感器 | strength | 原 class_ratio | 新 class_ratio |
|---|---|---|---|
| radar_tpy2 / container | 1.35 | 0.997 | 0.98 |
| radar_s400 | 1.30 | 0.986 | 0.98 |
| radar_346b | 1.28 | 0.9816 | 0.98 |

真实格子（E-3G 对 A-50U，ratio=0.9837）：旧公式判 CLASSIFIED，新公式判 CONTACT。
后果：悬停信息卡被 `level >= 2` 门槛挡住；AI 的 `ai.py:219/508` 要求
CLASSIFIED，会改变决策；旧回放中依赖该等级的开火命令会静默失败。

单调性实测：ratio∈[0,1] × strength∈[0,3) 共 30 万点，**无非单调**；
`track_ratio > class_ratio` 保护是死代码（0.96 < 0.98）。

修法：把上限改成 `0.999` 而非 `0.98`——CONTACT 仍数学可达（ratio 最大 1.0），
但对原有识别带的无谓收缩从 1.7% 降到 0.1%。

### 4.2 G22（中）负伤害推给 heal，但 heal 没有钳制，制造负血僵尸

**位置**：`core/hooks.py:461`（`_do_area_heal` 用
`min(template.hp, u.hp - eff.damage)`，无下界）

v3.5.5 的 F10 把负 damage 按 0 处理，注释指向"治疗走 heal 效果"，
但 heal 对负 damage 无防护。实测 `damage=-999` 命中后：
`hp=-984.0, alive=False, destroyed=False`——与 F12 修掉的僵尸单位同类，
`kill_unit` 不调用、不计伤亡。`Effect.parse` 也不校验有限/非负。

### 4.3 G21（低）0 伤害语义改变

**位置**：`core/model.py:468-473`（`apply_damage` 对 `amount<=0` 提前 return）

实测 `hp=5e-17, amount=0`：旧实现会落入 `>= hp-1e-9` 分支清成 `destroyed=True`；
新实现直接 return，残血继续存活。四处调用方都不依赖"负数=治疗"，
但 0 值清理语义已丢失。

### 4.4 G23（低）NaN 门禁只覆盖武器 `pk/damage`

加载期 `_finite()`（`loader.py:136-137`）只用于这两个字段；
`range_max`、传感器 `range/strength`、单位 `hp/mp/armor`、规则效果 damage
仍接受 NaN/Inf。实测 `range_max=nan` 被接受，而 `d > eff` 在 NaN 时恒 False——
射程与探测剪枝失效。`save_game` 用 `allow_nan=True`，NaN hp 可写入存档。

### 4.5 G19（低中）设施打击不走进飞/拦截窗口

**位置**：`core/engine.py:936-1033` vs `:622-684`

实测 10 格外 `tomahawk`：打设施当回合 `facility_hp 100→32`、
`st.missiles` 长度 0；打单位则 `flight=3` 且进入在飞状态。
`strike_facility` 还不检查 `mission=="ferry"` 的转场保险，
也不发 `EV_BEFORE_FIRE/EV_AFTER_FIRE`（模组看不到设施打击）。

### 4.6 G20（低）中段反导弹在一条再装填路径被跳过

**位置**：`core/engine.py:1406-1423`（`_reload_interceptors` 显式
`if w.midcourse: continue`）vs `:1460-1465`/`:1341-1345`（补给线分支补所有武器）

实测 S-400 的 `prs1m`（midcourse, ammo=0）：`_reload_interceptors()` 后仍 0，
`_tick_logistics()` 后变成 1——"中段弹不自动装填"的意图被另一条路径绕过。

### 4.7 已对拍且未发现差异

- **探测优化版 vs 参考实现**：840 组（side,target）对拍，0 处不一致。
- **存活判定**：`alive = not destroyed and hp>0` 是唯一来源，无绕过。
- **`over`/`winner` 写入**：4 处但 `_declare` 幂等，实测不互相覆盖。
- **`min_detection` 比较方向**：全仓只有 `combat.py:116` 一处门槛判定。
- **距离度量**：欧氏/对角 1.414 在各处一致（保底路径除外，见 G14）。
- **回合口径**：`turn_count > turn_limit` 与 UI 显示一致。

---

## 5. 门禁质量与测试假绿

### 5.1 G24（低-中）新门禁自身的覆盖缺口

| 门禁 | 缺口 |
|---|---|
| `check_workflows.py` | CI 里先跑它、**下一步**才 `pip install requirements-dev.txt`（pyyaml 在那）；实测无 pyyaml 时 `[SKIP] 只做缩进检查` 但仍打印 `[OK]`——YAML 解析那一半在 CI 永远不执行。`run: \|` 检测也不匹配 `- run: \|` 列表式写法 |
| `check_dupes.py` | `_scan` 只遍历模块/类体的直接子节点，不进 `if/try/with`；实测平台条件式重名 `[]`（漏判）。`@property` + `@x.setter` 会被误报（当前仓库无 setter） |
| `test_save.py:282-284` | F11 只断言"序列化结果有 `i` 键"，没构造同回合两条事件验证还原；把 `_restore_game` 退回按 turn 实现仍会通过 |
| `test_perf.py:69-90` | "参考实现" `_full_detection_level` 内部调用同一个 `evaluate_detection`，不是独立 oracle，发现不了 clamp 类错误 |
| `test_a11y.py` / `test_visual.py` | 不含新增的 motion 按钮与 `state.fx` 通道 |
| `check_scripts.py:160-167` | PyInstaller 路径只做子串检查，注释里出现 `--distpath` 即满足 |

### 5.2 G25（中）`motion=reduced` 覆盖不全且换局复位

**位置**：`core/engine.py:356`（`add_fx` 无开关）、`ui/mapview.py:1378,1447`
（`_draw_fx`/`_draw_anim` 无判断）、`ui/app.py:742-756,1818-1824`

实测：设 `motion=reduced` 后，
`st.add_fx("explosion", ...)` + `tick_fx(0.45)` → `_draw_fx` 仍改变画面；
`_apply_motion` 只把 `anim.enabled=False`，**不清空已入队事件**；
`_layout_game` 只在 `mapview is None` 时应用档位，
`load_slot` / 第二局 `start_game` / 战役下一关 / 观战开场都走 `else` 分支，
新 `GameState.anim.enabled` 默认 `True`——reduced 丢失。

### 5.3 G26（低-中）逻辑时钟覆盖不全

回放/观战暂停时 `paused` 分支直接 return，但外层仍无条件执行
`state.tick_fx(dt)` 与 `mapview.tick_clock(dt)`——暂停后天气继续滚、动画继续演。
`_last_shake` 只在 `draw()` 里更新，菜单/编辑器模式不调用 mapview.draw，
回对局首帧的 hover/点击会用上一局残留偏移。`zoom_at` 也没补 shake。
另外 clock 由真实 dt 累加，不同帧率下同一逻辑状态的画面仍不同——
"可复现"只对固定 dt 序列成立。

### 5.4 G29（低）文档与实现不一致

`ui/app.py:717-725` 的 `_save_config_quietly` docstring 说"失败只提示，
绝不阻断退出"，实现是裸 `except Exception: pass`，磁盘只读时玩家无感知。

---

## 6. 极端输入精简测试

以下输入实测**未发现崩溃或静默错误**：

| 输入 | 结果 |
|---|---|
| `over=True` 后 `move_unit` | 返回 `False`，正确拒绝 |
| `turn_limit=0` 后 `_advance_turn()` | `turn=2, over=True`，正常结算 |
| 连续 5 次 LLM `start()` 失败 → `stop()` | 句柄/进程/JOB 全为 0 |
| 旧档缺 `sat`/`rules_fired`/`i` | 正常读入（见 §1.4） |

说明：本轮原计划的第四路子代理因额度限制未能完整执行，极端输入部分由主代理
做了上述精简覆盖；`uid` 不存在、超长 Unicode 名称两项因测试脚本构造方式
有误未得到有效结论，不计入本报告。

---

### 7.1 修复摘要（v3.5.6）

| 条目 | 修法 | 验证 |
|---|---|---|
| G1 | `p = 1 - ttl/life`（tick 递减的是 ttl）；全生命期采样 | 最大偏移 3 px（原 337 px） |
| G2 | `rep.warnings.append` → `rep.add("scenario-load-failed","error",...)` | 变异下 4 条 error、退出非 0 |
| G3 | `_salvo_options` 复用 `rules.salvo_limit` | 断补给 UI `[1]` = 规则 1 |
| G4 | 新增 `_display_damage` 统一对空显示；日志/统计按实际伤害 | 51.4 / 95 / 113.1 三口径一致 |
| G5 | `_detect_cache` 加 20,000 条上限 | 灌满后自动清空 |
| G6 | 字体候选逐个重试（失败继续）；bold 也补 Droid | hook 验证第二个候选被尝试 |
| G7 | `_token_epoch` 加 `player_side` | 换阵营后精灵重烘焙 |
| G8 | minimap 静态层键去掉 `map_epoch` | 消除 73.56 ms 重建 |
| G9 | `_field_cache` 按世代清旧条目 | 死条目不再驻留 |
| G10 | `_text_cache` 增加 24 MB 字节预算 | 计数生效 |
| G11 | 启动中 prefetch 不置 `_boot_failed`；reset 清除 | 并发启动不再永久降级 |
| G12 | clamp 0.98 → 0.999 | ratio=0.99 恢复 CLASSIFIED |
| G13 | 枢纽分支补调 `_resupply_by_line` | supply_dry 3→0、fuel 0.10→0.35 |
| G14 | `path_cost` 识别整条保底路径按 mp 计价 | 保底格 path_cost=0.5=mp |
| G15 | 威胁圈用 ADN 实际拦截节点 | （叠层判定源统一） |
| G16 | hooks 区域效果改欧氏圆几何 | 偏移(5,5) 掉血 0 |
| G17 | AI 删 TRACKED 硬门槛，交给 `shot_profile` | 炮兵可被 AI 选中 |
| G18 | `collect_stats` 的 shots/hits 改用引擎 stats | 两次命中导出 2/2/100% |
| G19 | 补转场保险、fire 事件、stats 记账 | ferry 被拒；飞行窗口仍为已知边界 |
| G20 | 补给线补弹跳过 midcourse | 中段弹不再被白送 |
| G21 | `apply_damage` 只拦 `<0`，0 保留残值清理 | hp=5e-17 + 0 伤害 → 判毁 |
| G22 | heal 拒绝非有限/非正 + 圆形几何 | 负治疗不制造僵尸 |
| G23 | loader 对 sensor/weapon 全部浮点做 finite 校验 | `range=nan` 被拒 |
| G24 | CI 先装 pyyaml；dupes 进 if/try 且排除 setter；workflow 覆盖 `- run: \|` | 108 文件扫描 0 重复 |
| G25 | reduced 关 fx、清已入队动画、换局重应用 | fx 跳过、events 清空 |
| G26 | 回放/观战暂停不推进时钟；fit 清 shake；zoom_at 补偏移 | — |
| G27 | `App.run()` 清理移入 finally | POSIX 异常退出也 stop |
| G28 | 魔法数 `>= 2` 统一为 `Detection.CLASSIFIED.level` | 三处一致 |
| G29 | docstring 改为"静默"与实际一致 | — |

## 7. 建议处理顺序

本轮只审计不修。若决定处理，建议：

| 顺序 | 条目 | 理由 |
|---|---|---|
| 1 | G1、G2、G6 | 三条"改了却没生效"——先恢复修复的可信度，否则后面的修复也可能落空 |
| 2 | G3、G4、G13、G14 | 玩家直接看到的错误数字与错误可选项，信任成本最高 |
| 3 | G5、G8、G9、G10 | 长局与大字号下的内存/卡顿；官方图不触发，但模组与高回合上限会 |
| 4 | G7、G11、G12、G17、G18 | 换阵营、LLM 降级、探测阈值、AI 行为、战报统计 |
| 5 | G15、G16、G19、G20、G22–G26 | 几何口径、中途弹、动效覆盖、门禁缺口 |
| 6 | G21、G27–G29 | 边缘语义与文档 |

**注意**：G12、G17、G18、G19、G20 若修，会改变 AI 决策或回放结论。
按项目既定约束，必须走"实测前后指纹对比 + 深战术 A/B 不退化"流程，
不能只补断言就合并。

---

## 8. 审计覆盖范围

- **修法回归**：`git diff v3.5.4..v3.5.5` 全量（43 文件）；抽查 9 类修法的真实行为。
- **实测工具**：34 自检全跑、`diag 20` 三张图、`main.py --check`、`ruff`、
  `check_docs`；AST 全仓重名扫描；内存循环（10 局 / 50 缩放 / 20 主题 / 1000 回合）。
- **规则分叉**：14 个概念的两处以上实现逐对比较，其中 7 组实测对拍确认等价。
- **视觉与交互**：震动幅度全生命期采样、齐射/伤害三口径、补给枢纽、
  可达集与执行路径、防空圈、区域效果几何。
- **极端输入**：见 §6，含未得出结论的诚实标注。

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

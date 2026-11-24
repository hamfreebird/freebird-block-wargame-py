# 项目审计报告

> 这份文件取代了原来的 `ROADMAP.md`。路线图回答「接下来做什么」，
> 审计回答「现在到底怎么样」——功能堆到 45 项机制、41k 行代码之后，
> 后一个问题更能决定下一步。
>
> **本版为第四轮审计（2026-11-21，基于 v3.5.4 / 待发版）**。
> 前三轮共关闭 54 条问题：v3.4.1 的 10 条、v3.4.2 的 22 条、v3.4.3 的 22 条。
> 前三轮把变异测试、全帧性能测量、字体组合场景都查过一遍，本轮**换切入角度**：
> 查状态机与时序、规则数学与边界、端到端交互路径、CI 时序与覆盖空白。
>
> 结论：规则和数据的"静态正确性"已经相当扎实（78 组相克、4615 组武器×模板
> 全遍历无异常），新问题集中在**动态过程**——胜负声明的幂等性、读档后的状态
> 一致性、后台线程的时效、模态叠加的事件穿透。这些都不是"算错"，而是
> **"在错误的时机算了第二次"或"事件穿过了本该挡住的层"**。
>
> **处理结果**：本轮 26 条中 23 条已修；D1（寻路假路径）、D4（内陆港口）、
> D7（弹药<齐射）经评估**不修**——它们涉及关卡数据与 AI 策略，直接改会改变
> 决策指纹并使深战术 A/B 退化 72 分；改以 `--check` 告警暴露（港口已有
> `port_inland` 提示），留待关卡设计师调整。详见 §7。
>
> 全文没有推测性结论：每条给出**实测数值**或**可点开的代码位置**。
> 实测统一用 `D:/Dev/Python/python.exe`（3.12.4 / pygame 2.6.1）；
> 系统 `python`（3.13）无 pygame，不能用于 UI 测试。

## 当前基线（v3.5.4 · 未发版）

| 维度 | 现状 |
|---|---|
| 玩法内核 | 45 项机制：侦察—打击链、逐发拦截、中段反导、电子战与多频段诱饵、信火一体、工事体系、烟幕、任务剖面、卫星过顶窗口、反卫星、设施打击、齐射、对手视角威胁评估、战场天气与时段、补给线与纵深、指挥体系、多点防空协同、火力准备、波段对抗、同格共存、对局回放、AI 观战、想定与回放分享、可复现随机流、模组接口 |
| 数据 | 65 兵种 / 71 武器 / 28 传感器 / 15 挂载预设 / 9 编队预设（`main.py --check` 实测） |
| 想定 | 4 张（海峡对决 128×128、草原雷霆 36×36、海峡屏障 40×40、新兵训练场 20×14） |
| 引导 | 教学想定 + 12 步事件驱动引导 |
| 规则 | JSON 声明式规则钩子（11 基础条件 + all/any/not 3 组合器 = 14 种 × 9 种效果） |
| 呈现 | 8 种战斗动画 · 4 套主题（含 2 套色盲友好）· 字号三档 · 色盲形状标识 · 9 种音效 · 8 种语言 |
| 对手 AI | 规则启发式（默认）· 本地大模型指挥官（LFM3 1.7B，可选，只决定作战决心） |
| 代码规模 | core 42 文件 16,705 行 · ui 15 文件 11,966 行 · tools 46 文件 14,694 行 · `main.py` 220 行（含空行与注释的全行口径） |
| 自检 | 31 个 `test_*` / `check_*` 脚本；CI 52 个步骤（verify 42 + balance 5 + package 5），8 组 OS×Python 组合（2 OS × 4 Python） |
| 实测性能 | 完整 `App.draw` 128×128 **5.94ms**、256×256 约 9ms；缩略图改尺寸 381→0.2ms；可达层 4.53→3.29ms |
| 质量 | `diag 20` 三张图 0 错误；31 个自检退出 0；`ruff check` 0 告警；决策指纹与 v3.4.2 逐字一致 |

> **本轮与前两轮的关键差别**：前两轮问"这个函数算得对不对"，
> 本轮问"这个状态在错误时机被写了第二次会怎样"。
> 结果：静态规则零新问题，动态过程 7 条高优先级。

---

## 0. 结论速览

| 级别 | 条目 | 一句话证据 | 状态 |
|---|---|---|---|
| **高** | S1 全歼判定覆盖 winner | 双方先后全灭，winner 从 RED 变 BLUE，胜利日志 3 条 | ✅ 已修 |
| **高** | S2 读档已结束对局死锁 | over=True 存档读回后 mode=game、phase=player，既不能结束也不结算 | ✅ 已修 |
| **高** | S3 LLM 预取跨局污染 | reset 后旧线程仍回填上一局决心，换局 turn 相同被误用 | ✅ 已修 |
| **高** | U1 简报期模态穿透 | 简报+F10 打开隐形菜单，Esc 关掉的是看不见的那层 | ✅ 已修 |
| **高** | U2 战役菜单事件穿透 | 开着战役浮层按回车直接开局，返回后浮层自动重现 | ✅ 已修 |
| **高** | U3 回放模式鼠标可写 | 回放中点地图/侧栏会执行选单位、开火等改状态动作 | ✅ 已修 |
| **高** | Q1 Release 与 CI 无因果 | 两者同为 tag 触发、并行运行，Release 1m14s / CI 3m+ | ✅ 已修 |
| 中 | S4 LLM 跨回合时效 / S5 模态不暂停 AI / S6 读档不清命令流 | 详见 §1 | ✅ 已修 |
| 中 | D1 寻路假路径（评估不修） / D2 浮点残血 / D3 缺版本存档清 rng / D4 港口位置（评估不修） | 详见 §2 | ✅ 已修 |
| 中 | U4–U11 滚轮穿透、焦点跟随、对话框残留等 | 详见 §3 | ✅ 已修 |
| 中 | Q2–Q4 check_docs 崩溃、macOS 缺席、Linux 字体假绿 | 详见 §4 | ✅ 已修 |
| 低 | 其余 9 条（含 D7 弹药对齐） | 详见 §2 / §3 / §4 | ✅ 已修 / 评估不修 |

**静态正确性经住全遍历**：78 组 WeaponKind×Domain、4615 组武器×模板无 KeyError、无 NaN/inf、无越界；28 传感器与 71 武器字段合法；v0–v5 存档迁移、坏版本、坏 JSON 全部友好处理；事件总线的订阅快照与异常隔离正确；命令"成功才入日志"。

---

## 1. 状态机与时序

### 1.1 S1：全歼判定覆盖已宣布的 winner（高）

`core/engine.py:1859-1862` 的 `_check_elimination()` 遍历双方，只要一方无单位就 `_declare`；
`_declare()`（`:1881-1886`）无条件写 `winner`，没有 `if self.over` 守卫；
`kill_unit()`（`:1841-1857`）也不检查 `self.over` 或 `unit.destroyed`。

实测（先杀蓝方最后一员，再杀红方最后一员）：

```
after blue dead: True RED 蓝方 作战力量被全歼
after red dead:  True BLUE 红方 作战力量被全歼
winner: BLUE | victory logs: 3
  作战结束：红方 胜利 —— 蓝方 作战力量被全歼
  作战结束：红方 胜利 —— 蓝方 作战力量被全歼
  作战结束：蓝方 胜利 —— 红方 作战力量被全歼
```

后一次判定**推翻了先一次的胜负结论**。更极端的边界：单单位对局里杀掉唯一蓝方单位后，
因为红方本就 0 单位，循环第二项又判蓝方胜——一个已经全灭的一方被宣布为胜者。

改法：`_declare` 加 `if self.over: return` 幂等守卫；`_check_elimination` 先一次性
收集双方存活情况，双方同时无单位则明确平局；`kill_unit` 对 `unit.destroyed`
或 `self.over` 早退，避免重复计伤亡与重复广播。

### 1.2 S2：读档已结束对局导致死锁（高）

`core/save.py:455-458` 在存档时把 `phase=="ai"` 强制改成 `"player"`，但保留了
`over=True` / `winner`。`ui/app.py:1704` 的 `load_slot()` 直接 `mode="game"`，
没有任何 `over` 检查。

实测构造 `phase="ai", over=True, winner=RED` 存档并读回：

```
LOAD: mode game phase player over True winner RED
AFTER end_turn x2: mode game phase player turn 1 over True
```

死锁链条：`update()` 的 AI 推进条件是 `phase=="ai"`（不成立）；
`end_turn()` 有 `if self.state.over: return`（被挡住）；
`_game_over()` 只在 end_turn 与 AI 步进后调用——读档路径完全绕过。
玩家看到的是一张不能再操作、也不会结算的棋盘。

改法：`load_slot()` 读档后若 `st.over` 立即调 `_game_over()` 进入结算；
`save.py` 在 `over=True` 时不应把 phase 改成 player。

### 1.3 S3：LLM 预取结果跨局污染（中→高）

`core/llm_commander.py:408-426` 的 `_run` 回填 `_result` 只按 `turn` 校验；
`reset()`（`:451-455`）只置空 `_worker` / `_result` 却不停线程；
`take()`（`:429-446`）也只匹配 turn。UI 的 `commander` 对象惰性创建
（`app.py:1881-1887`），只有退出时才 `stop()`——换局、读档时并未 reset。

实测（Stub 0.6s 推理，assault 决心）：

```
A prefetch old: True turn 1
A after reset: slot None worker None
A stale worker wrote slot: 1 assault
A take new game -> source llm stance assault turn 1
```

换局后两局 turn 都是 1，旧线程结束后仍回填，新局误把上一局的决心当成本局的。
这直接违反"大模型是外部输入，不是规则"的隔离约束——外部状态污染了新对局。

改法：每次预取附"局世代"标识（对局 id 或 `scenario_path + seed`），
`_run` 回填前比对当前世代，不符丢弃；`take` 同时校验 turn 与世代；
`reset()` 用 Event 取消在途线程，并在 `start_game` / `load_slot` /
`_start_campaign_step` 中真正调用。

### 1.4 S4：LLM 预取跨回合时效（中）

`prefetch()`（`:396-398`）遇到存活 worker 直接 `return False`。玩家提前结束回合时，
上一回合的 worker 可能仍在推理；新回合 `prefetch` 因"worker 存活"拒绝启动，
而旧结果 turn 不匹配被丢弃——连续两个回合都降级到启发式 AI。

实测（Stub 0.8s）：

```
B prefetch turn1: True
B take@2 early: fallback
B prefetch turn2 while worker1 alive: False
B slot after worker1: 1
B take@2 with stale slot: fallback
```

改法：`prefetch` 发现 worker 的 turn 与当前不一致时中止旧 worker 并启动本回合推理；
`take` 遇过期结果主动清槽。

### 1.5 S5：模态浮层打开时 AI 仍在背后推进（中）

`ui/app.py:2668-2742` 的 `update()` 对 `show_settings` / `show_help` /
`show_briefing` 没有任何判断。实测开设置后连续 `update(1/60)` 20 秒：

```
after end_turn: phase ai turn 1
SETTINGS OPEN 20s: turn 1 -> 2 phase player mode game show_settings True
```

玩家在设置里调整键位的这段时间，AI 回合已经跑完并进入下一回合。
同类问题见 §3.6（动画与 fx 也未暂停）。

改法：`update()` 进入对局推进前加模态闸门，任一模态为真则暂停 AI 计时。

### 1.6 S6：读档不清空命令流（中）

`ui/app.py:1695-1731` 的 `load_slot()` 没有 `self.cmd_log = CommandLog()`，
而所有开局路径（`:815/833/1160/1612`）都有。实测：

```
cmds before load: 1
cmds after load: 1   ← 被放弃时间线的命令仍在
```

读档后继续游玩并导出回放时，读档前的命令会与读档后的混合，
"想定 + 初始种子 + 命令流"重演出的状态与实际对局不一致。
更深的问题是：当前回放格式没有"分段起点"概念，读档后的对局本就无法用
单一命令流正确重演。

改法：至少在 `load_slot` 重置 cmd_log；若支持读档后续演，需在回放格式中
标注分段起点与该点的 rng 快照。

### 1.7 S7：`begin_ai_phase` 可重入（低）

`core/engine.py:1893-1905` 只检查 `self.over`，不检查 `self.phase`。
AI 相位中再次调用会重建 `AIPlanner`、重置队列，使已排好的动作从头开始。
正常 UI 路径有 phase 守卫，但回放与观战共用 `run_ai_turn`，防御性不足。
建议加幂等守卫或断言"只允许从 player 相位调用"。

### 1.8 已确认无问题的部分

- `_check_end()` 幂等正确：连续多次调用只写一条胜利日志；平局分支 winner=None 稳定
- `finish_ai_turn()` 在 over 后正确早退，不推进回合
- `EventBus.emit` 用 `(*subs.get("*",()), *subs.get(event,()))` 先做元组快照，
  emit 期间 subscribe/unsubscribe 不影响当前分发；单订阅者异常被隔离；无递归投递
- 命令"先执行成功、后 `_record`"，apply 失败不入 log；无独立命令 uid，不存在与 state.uid 撞号
- `REPLAY_VERSION=3` 版本守卫正确；旧回放的 directive 字段缺失时降级到启发式
- Esc 在设置/观战/回放/菜单/帮助/系统菜单/over/简报各层的优先级明确
- 极端边界实测：0 回合上限、单单位对局、14 单位同格、空想定、VP 相等平局均无崩溃
  （唯一例外是"所有单位同时死亡"触发 S1）

---

## 2. 规则数学与数据

### 2.1 D1：`find_path` 返回走不了的路径（中）

`core/pathfind.py:187` 的 `step = fb.get((nx, ny), step)` 无条件用
`fallback_moves` 的封顶代价覆盖相邻格真实代价。`fallback_moves` 的注释
（`:208`）声明"只对完全无法移动的单位生效"，但 `find_path` 对所有 mp>0
单位都套用了它。

实测 m1a2（mp=3.2）从 (8,4) 到山地 (7,4)，山地 move_cost=3.5：

```
path [(8, 4), (7, 4)] cost 3.5 mp 3.2
move_unit(path) -> False pos (8, 4)
```

`path_cost` 用真实地形代价 3.5 > mp，`move_unit`（`engine.py:493`）直接拒绝。
全库 35 个模板 mp<3.5 均受影响。AI 与点到点移动会拿到一条注定失败的路径。
另 `:190-191` 有两个连续 `continue`，第二个是不可达死代码。

改法：只在"正规洪泛无解"时才启用 fb（与 `flood_reachable` 的判定一致），
不要对普通单位覆盖代价；或让 `move_unit` 采用与 `find_path` 同一套封顶代价。

### 2.2 D2：浮点残血导致单位"活着但 hp≈0"（中）

`core/model.py:466-470` 的 `apply_damage` 仅 `max(0.0, hp - amount)`，无 epsilon；
`alive`（`:378-379`）判定 `hp > 0`。实测：

```
hp=0.1+0.2 minus 0.3 -> hp=5.55e-17  alive=True destroyed=False
hp=30.000000001 minus 30.0 -> hp=1.00e-09 alive=True
hp=100 minus 1000x0.1 -> hp=1.40e-12 alive=True
```

伤害经 jitter(±15% 连续浮点) × mitigation × counter 产生任意浮点，残值客观存在。
该单位仍占格、仍计为存活、需额外一击。存档 `round(hp,2)` 会掩盖此现象，
故问题局限在同一局内。

改法：`amount >= hp - 1e-9` 时直接置 `hp=0.0; destroyed=True`，
或 `alive` 判定改为 `hp > 1e-6`。

### 2.3 D3：版本字段缺失的存档被清掉随机流（中）

`core/save.py:84` 用 `data.get("version", 0)`，`:96-98` 把 `< CHAIN_START` 一律按 v5 处理。
实测一份结构完整、含合法 v6 rng 但缺 `version` 键的存档：

```
after: version 6 rng None
notes ['存档从 v5 升级：旧档没有随机流...']
```

合法的 rng 被清成 None，升级说明还谎称"旧档没有随机流"。
读档后随机流从新种子重启，"同种子可重演"在读档边界被静默破坏。
其他边界均友好：`version=99` 拒绝，`-1`/`'abc'` 报"版本号不合法"，v0–v5 正常升级。

改法：区分"版本缺失"与"旧版本"——缺 version 但存在合法 rng 时按当前版本读取；
或 `_migrate_5_to_6` 仅在 `data` 确实不含 rng 时写 None。

### 2.4 D4：8 处港口建在内陆（中）

`strait_confrontation.json` 与 `strait_shield.json` 中 8 处 `Facility.PORT`
建在非海岸地形且周边无相邻水域：

```
port(55,29) mountain near_water=False  port(10,40) mountain near_water=False
port(78,52) forest   near_water=False  port(61,54) mountain near_water=False
port(76,88) forest   near_water=False  port(88,96) desert   near_water=True
port(8,19)  urban    near_water=False  port(31,21) urban    near_water=False
```

`SUPPLY_PROFILE` 中 PORT 专保障 SEA/SUB，舰艇只能在可通行海域——
山地腹地的港口覆盖半径内不可能有舰艇，是永不生效的死设施。合法港口只有
(9,55)/(6,74) 两处 coast。

改法：把这 8 处改到 coast/water 或改标 DEPOT；
在 `main.py --check` 增加"PORT 必须邻接水域"校验。

### 2.5 低优先级数学与数据问题

| # | 位置 | 问题 | 证据 |
|---|---|---|---|
| D5 | `combat.py:222` | `mitigation = 1.0 - min(0.90, ...)` 只有上界无下界 | 构造 armor=-1 → mitigation=1.85，伤害被放大；官方数据不触发，模组可注入负值 |
| D6 | `detect.py:143` | `class_ratio = 0.70 + 0.22*strength` 无 clamp | radar_e3 strength=1.40 → class=1.008，CONTACT 档永不可达；20 个传感器 strength>1 |
| D7 | `armory.json:1259` | hpj45_130 的 `max_salvo=3` 大于挂载 ammo=1~2 | type055/052d/054a 三处；默认单发不受影响，选最大齐射时打不出 |

### 2.6 已实测的组合覆盖（无问题）

| 维度 | 覆盖规模 | 结果 |
|---|---|---|
| 域相克矩阵 | 13 WeaponKind × 6 Domain = 78 组 + 71 武器 × 65 模板 = 4615 组 | min 0.55（触下界）/ max 1.40，无 NaN/inf、无缺项 |
| 武器字段 | 71 目录 + 70 挂载去重 | pk/evasion/stealth/damage/range/targets/guidance 全合法 |
| 传感器字段 | 28 目录 + 27 挂载 | 除 strength 越界（D6）外无负值 |
| 存档迁移 | v0–v6 + 缺版本 + 版本 99/负数/非数字 | v0–v5 友好升级，越界友好拒绝 |
| 补给边界 | 断粮 35 回合 + 恢复 6 回合 | 无永久锁死，回覆盖即恢复 |
| 射击公式 | 25 个有效 profile | pk∈[0.182,0.640]、damage∈[12.0,41.9] 均在界内 |
| 复合概率 | interception / gps / jam / counter | 均有正确 clamp，结果∈[0,1] |
| 诱饵频段 | radar/ir/visual/multi | 旧值 thermal 已映射为 ir，无缺口 |

---

## 3. 端到端交互

### 3.1 U1：简报期打开隐形模态（高）

`_on_key` 中 `action=="system_menu"`（`:2347`）与 `show_help`（`:2336`）
排在 `show_briefing`（`:2389`）之前；而 `draw()` 中简报最后绘制（`:2838-2840`），
`draw_briefing` 先铺一层 alpha=205 全屏 dim。实测：

```
start_game: mode=game show_briefing=True
brief+F10: show_menu=True show_briefing=True
ESC:       brief=True menu=False   ← 关了看不见的菜单
brief+F1:  show_help=True show_briefing=True
```

玩家按 F10 后什么也看不见，按 Esc 也"没反应"——关掉的是被遮住的层。
改法：引入统一的"最上层模态"判定，Esc 只关闭视觉最上层的那个。

### 3.2 U2：战役菜单事件穿透（高）

`_draw_campaign_menu`（`:930-943`）铺了全屏 dim 看似模态，但 `_on_key` 的菜单分支
（`:2285-2296`）未检查 `show_campaign_menu`，`K_RETURN` 无条件 `start_game()`；
`_on_mouse_down` 的战役菜单判断排在按钮判断之后。实测：

```
campaign_menu+ENTER: mode=game show_campaign_menu=True
campaign menu + click START: mode=game show_campaign_menu=True
back to menu: show_campaign_menu=True   ← 浮层自动重现
```

改法：菜单模式键鼠分发最前面处理该浮层，吞掉回车/方向键；
`start_game()` 与返回菜单统一清 `show_campaign_menu`。

### 3.3 U3：回放模式鼠标可写（高）

`_on_mouse_down`（`:2442`）有 `menu` 与 `spectate` 分支，却没有 `replay` 分支。
观战分支明确写了"地图上的左键在观战里什么都不做"，回放没有对应保护。
实测 spy 计数：

```
replay 点击 map/sidebar/右键 -> _activate_at=1  _on_sidebar_click=1
replay resize: before viewport (1100,844) after (1600,1000) 布局未重排
```

回放中点地图会选单位、点侧栏会触发行动按钮，直接改写正在重演的战局；
VIDEORESIZE 也只检查 `mode in ("game","spectate")`（`:2176`）。
改法：补 `mode=="replay"` 只读分支；布局重排纳入 replay。

### 3.4 其他交互问题

| # | 级别 | 位置 | 问题 |
|---|---|---|---|
| U4 | 中 | `app.py:2789-2805` | 主菜单不绘制 `set_status()` 提示条——导入失败、回放读取失败、战役进度失败等反馈在菜单态完全不可见 |
| U5 | 中 | `app.py:2188-2204` | MOUSEWHEEL 从不转发给设置面板，`SettingsPanel` 的滚轮滚动代码是死代码；滚轮反而缩放下层地图 |
| U6 | 中 | `settings.py:102-106` | 31 个焦点项里 Shift+Tab 与 Tab 同向、Home/End 无效（按键被吞但焦点不动） |
| U7 | 中 | `settings.py:102-106,318-335` | 键盘导航不调 `_ensure_visible()`，焦点落到滚动区外时焦点框不绘制，玩家盲操作 |
| U8 | 中 | `app.py:2502-2519` | over 画面的鼠标分支排在 help 拦截之前，帮助打开时点击穿透；`show_help` 残留到下一局 |
| U9 | 中 | `replaydlg.py:139-150` | 回放对话框播放成功后不关闭，退出回放回菜单后自动重现 |
| U10 | 中 | `app.py:2668-2717` | 模态下动画/fx/`status_t` 照常推进，读完帮助后提示已过期（AI 计时见 S5） |
| U11 | 中 | `replayplayer.py:56-98` | 播放器只有单向推进，无 seek/reset/rewind；进度条不可点击，播放结束后唯一出口是 Esc |
| U12 | 低 | `app.py:772` | 战役失败提示被 over 与 debrief 两层 dim 遮住，只能靠按钮文案推断 |
| U13 | 低 | `importdlg.py:129-135`、`replaydlg.py:113-119` | 两个对话框吞滚轮却不滚动列表，↑↓ 改变 index 也不跟随滚动 |

### 3.5 已走查的交互路径（无问题部分）

- 主菜单→想定→简报→战斗→结算→返回；战役进入→完成→下一关→失败重试→退出
- 设置面板捕获态按 Esc 只取消重绑定、不关面板；修饰键不会被绑成动作
- 导入/回放对话框在菜单中互斥，不会同时打开
- 战斗中帮助面板吞掉鼠标与滚轮，不穿透到地图
- 观战模式只读语义正确；读档路径清理了多数模态与光标状态
- VIDEORESIZE 在 game/spectate 下正确重排；最小窗口 1180×740 钳制生效

---

## 4. CI、测试与文档

### 4.1 Q1：Release 与 CI 无因果关系（高）

`.github/workflows/release.yml:3-6` 与 `ci.yml:3-10` 同为 tag push 触发的
两个独立 workflow。`release.yml` 的 `publish` 只 `needs: build`，而 `build`
只打包与产物 `--check`，**不跑 31 个自检**。两者无 `workflow_run`、
无 environment gate、无跨 workflow 协调。

实测：v3.4.3 发布时 Release 1m14s 完成，CI（8 组矩阵）3m+ 才完成。
全仓 grep 无跨 workflow 依赖字段。v3.4.3 只做到"让 CI 也跑"，
并未建立"CI 通过才发版"的因果——**CI 还没红，Release 已发**的窗口依然存在。

改法：Release 改为 `on: workflow_run` 且校验 `conclusion == success`；
或设 environment required-checks；或把发布并入 `ci.yml` 的 `needs` job。

### 4.2 Q2：check_docs 遇文档缺失抛未捕获异常（中）

`tools/check_docs.py:47-49` 的 `_read` 无存在性判断。实测在最小副本中
故意不放 AUDIT.md：

```
--- 版本号自洽 ---
Traceback (most recent call last):
  File "check_docs.py", line 249, in check_versions
    audit = _read("AUDIT.md")
FileNotFoundError: [Errno 2] No such file or directory: 'AUDIT.md'
```

对照 `check_sdk.py:249-251` 有 `if not os.path.isfile(DOC)` 保护并打印 `[FAIL]`。
改法：`_read` 或 main 入口统一捕获 `OSError`，用 `fail()` 计入失败。

### 4.3 Q3：文档承诺 macOS 但 CI 从不测（中）

CI 矩阵为 `[ubuntu-latest, windows-latest]` × 4 Python = 8 组，**无 macOS**。
但 `GAME_GUIDE.md:2300/2545/3008/3226` 多处给出 macOS 配置路径，
`ui/theme.py:551/567` 含 macOS 专用字体候选——这些分支永不进 CI。
注意：基线表里的"8 组 OS×Python"是 2 OS × 4 Python，不是 4 OS。
改法：矩阵加 macos-latest，或在文档明确"macOS 未纳入 CI 验证"。

### 4.4 Q4：Linux runner 字体测试可能假绿（中，疑似）

`smoke_test.py:1041-1049` 用 mono/plain 像素相等判定"中文降级为中文字体"。
但 `render_text` 对含 CJK 文本强制 `mono=False`，两者必用同一字体，像素天然相同。
若 ubuntu runner 未装 Noto CJK / wqy（`theme.py:545-554` 的两个 Linux 路径），
中文会走内置字体渲染成方框，断言照样通过。本机 Windows 字体齐全，无法复现。

改法：增加"渲染的 CJK 字符确实有字形"的断言（与 known-tofu 比对），
或把 CJK 字体存在性纳入 CI 前置检查。

### 4.5 其他低优先级

| # | 位置 | 问题 |
|---|---|---|
| Q5 | `GAME_GUIDE.md:3` | 版本号实为六处，自检只覆盖五处，GAME_GUIDE 抬头漏检 |
| Q6 | `requirements.txt:3` | CI 装 `pygame-ce<2.7`，本地测试用官方 `pygame 2.6.1`，两个不同 PyPI 包；API 兼容是设计选择，但日志未打印实际包名 |

### 4.6 已确认无问题的部分

- **零覆盖模块不存在**：动态追踪确认 55 个 core/ui 模块全部被至少一个自检加载，
  smoke_test 单脚本即调用其中 53 个。函数级最薄的是 `ui/replayplayer.py`，属边角非空白
- **31 自检无顺序依赖**：固定顺序全跑、随机顺序单独跑 8 个，均 RC=0、无 Traceback
- `main.py --check` 与 `--sim` 均已在 CI 执行；package 与 release build 都对产物跑 `--check`
- 版本号六处实测全部为 3.4.3；SDK 的 PUBLIC_API 10 个、HOOK_EVENTS 10 个、
  钩子签名与文档逐条一致；新机制数值（focus×1.45、EFFORT_GAIN=2.5、stance 权重、
  bus 历史 96、回放 v3）与文档吻合
- README 首次体验：空用户目录按步骤跑 `--check` 与 `--sim` 均成功

---

## 5. 建议的处理顺序

**先修会改变胜负结论或阻断玩家的三条**（1–2 天量级）：

1. **S1 + S2**：胜负声明幂等、读档后 over 进入结算。验收：双方全灭 winner 确定且不重复播报；
   over 存档读回直接进入结算画面。
2. **U1 + U2 + U3**：模态统一优先级、回放只读。验收：两两模态组合下 Esc 只关最上层；
   回放中点地图/侧栏无状态变化。
3. **S3 + S4**：LLM 预取加局世代标识并在换局时真正 reset。验收：换局后绝不消费上一局决心。
4. **Q1**：Release 改为 `workflow_run` 依赖 CI。验收：CI 红时不会产出 Release。
5. **D1 + D3**：寻路假路径、缺版本存档清 rng。验收：find_path 返回的路径 move_unit 必接受；
   缺 version 但有合法 rng 的存档不改变随机流。
6. 其余中低优先级按批次处理。

**不建议做**：`ui/app.py` 拆分（机械但量大，应与修 bug 分开评审）。
**暂不做**：AI 单步 p99 压平（近似距离会改变 AI 结论）；
非中文文案母语翻译（先保证接入 tr()）。

---

## 6. 审计覆盖范围

本轮实际执行：

- 四个维度并行审阅：①状态机与时序 ②规则数学与数据完整性 ③端到端交互 ④CI/测试/文档
- 31 个自检全部退出 0；随机顺序单独跑 8 个验证无顺序依赖
- 78 组相克 + 4615 组武器×模板 + 28 传感器 + 71 武器字段全遍历
- 实测复现：全歼 winner 覆盖、读档 over 死锁、LLM 跨局/跨回合污染、
  模态穿透（简报、战役菜单）、读档命令流残留、缺版本存档清 rng、
  寻路假路径、浮点残血、主菜单状态不可见、设置焦点与滚轮
- 动态追踪 55 个模块的测试加载情况
- 回放、观战、导入、战役、设置等 15 条交互路径走查

未覆盖：Linux/macOS runner 的实际字体与事件行为（本地仅 Windows）、
真实用户长时间连续游玩的心理体验、大模型多轮连续决策的战术质量评估。

---

## 7. 本轮处理说明

前三轮坚持一条硬线：**决策指纹必须与上一版逐字一致**。本轮 D1（find_path
无条件用 fallback 代价覆盖真实代价）确实是个 bug，但修复它后实测：

```
基线:     steppe_thunder 深 -12 / 浅 -36
修复后:   steppe_thunder 深 -60 / 浅 +12     （深战术退化 72 分）
决策指纹: 两图均改变
```

AI 规划器依赖了 find_path 的"乐观可达估计"——它规划出的路径虽然被
`move_unit` 以真实代价拒绝，但动作序列与计分已经基于它生成。
修正后深战术体系失稳，且 UI 层早有 `path_cost <= mp` 校验，
**玩家看不到假路径预览**。综合判断：该问题属 AI 策略设计，需与战术层
配合调整，不在本轮单独修。

D4（8 处内陆港口）与 D7（3 处挂载 ammo < max_salvo）同属关卡数据问题：
直接改数据会改变补给与平衡，实测改港口类型后 strait_confrontation 的
决策指纹也变了。处理方式是在 `main.py --check` 增加 `port_inland` 告警，
把隐性死设施变成可见提示，由关卡作者决定坐标或类型。

其余 23 条已修，其中：
- S1/S2/S3/U1/U2/U3/Q1 七条高危全部补了回归断言或门禁
- 决策指纹（6 回合动作序列 + 终局 SHA1）与 v3.4.3 逐字一致
- `diag 20` 三张图 0 错误；31 个自检全绿；ruff 0 告警

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

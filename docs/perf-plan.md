# Argus 执行引擎性能优化 —— 开发 Plan

> 目标：砍墙钟、砍大模型调用次数、减少"慢加载"导致的假失败。
> 基准（baseline，06-chat.feature / 3 台真机）：**墙钟 30m48s，聚合 76min，通过率 15/25=60%**。
> 每阶段完成后用**同一 feature、同 3 台**对照这条基准。

## 0. 现状与瓶颈（来自实测）
- 聚合 76min 的大头 = ① 聊天类 case 等被测 App 自己的 AI 回复（每个十几二十 turn）；② 失败场景烧完 `MAX_TURNS_WITHOUT_PROGRESS=15` 预算才放弃（≈4min/个）；③ **每个 step 每 turn 都调大模型**（分层执行默认关）。
- 分层执行开关 `AGENT_SPLIT_ACT_CHECK` 现为 `false` —— When/Then 每步都走 brain。
- Then 上除了 brain 验证**没有别的 LLM 工作**（planner 不对 then 出 `act`），所以合并/异步只需动"验证"这一环。
- 每 tap 已走 locator 前置定位（`ElementLocator`，UI-TARS）——坐标不再靠 brain 估。

---

## 核心原语（Phase 1，一切的地基）

### P1-A. settle 闸（zero-LLM）
判断"屏幕是否加载完/稳定"，全程不调大模型：
- **visual_diff**（`skills/visual_diff.py`，纯 PIL 像素差）：连续帧 `change_ratio` 连续 N 帧 < 阈值 = 稳定。
- **loading_detector**：转圈/骨架屏消失。
- **wall-clock 超时**兜底：到时仍不稳 → 回退同步 brain 判（宁可慢不假失败）。
- **噪声 mask**（关键）：屏蔽顶栏时钟、光标闪烁等已知微动区，否则永远判不"稳"。settle 与后面 2% 路由**共用同一套 mask**。
- 接口：`wait_settled(platform, timeout_s, stable_frames=N) -> {settled: bool, frames: [窗口内帧序列]}`。mjpeg 下帧本在流，记录 trigger→settle 这段 buffer 近零成本。

### P1-B. 帧采样（When vs Then 不同）
- **When**：settle 后取**稳态 1 帧** → 在其上决策 + locator 定位 + 执行。locator 那次定位也必须在稳态帧上做。
- **Then**：从"触发→settle"窗口采 **首 / 中 / 稳** 三点（横跨窗口，不是挤末尾——否则漏掉早期就消失的 toast）。
  - **2% 自适应路由**（visual_diff，零 LLM）：{首,中,稳} 两两最大变化
    - `< 2%` → 窗口没动 = 静态断言 → **只送稳态 1 帧**给 brain
    - `≥ 2%` → 有过程/动画/瞬态 → **送 3 帧**让 brain 判过程
  - 更稳做法：扫整个 buffer 求最大帧间差当路由，dynamic 时挑"变化最大处前后帧 + 稳态"。
- 现有 `AGENT_ASSERT_BURST_FRAMES=3` 是这套的雏形（无条件 3 帧），本阶段升级为"首/中/稳 + 2% 路由"。

**验证**：断言型 step 的图 token 在静态屏上降到 1 帧；动画/toast 类仍能判过程；通过率不回退。

---

## Phase 2. `wait_for`（等待直到 X 出现）
- 动机：现状靠 per-step 子循环隐式轮询，每轮询烧一次大模型，且总预算被 `MAX_TURNS_WITHOUT_PROGRESS=15` 卡死 → **慢加载（>15 轮）会假失败**；`wait` 动作单次还钳 5s。
- 做法：识别"等待/直到/until"类语义 → 进**代码层轮询**：固定 interval 截图，先用**便宜手段筛**（loading_detector / OCR / 模板匹配先看 X 在不在），只在"疑似出现"时才调一次大模型确认。
- **与 no-progress 解耦**：wait 轮次不计入 `MAX_TURNS_WITHOUT_PROGRESS`；改成独立 **wall-clock 超时**（用例可声明 `timeout`）。
- 复用 P1-A 的 settle 闸（wait_for = settle 的泛化：等到"稳定 / 目标出现"）。
- 接口草案：用例侧可声明超时（tag 或 step 关键字）；agent 侧 `wait_for(cond_fn, timeout_s, interval_s)`。

**验证**：造一个慢加载 case（>15 轮才出结果），现状假失败 → 改后通过，且大模型调用次数从 N 降到 1。

---

## Phase 3. Then-And 合并（连续断言块 → 1 次调用）
- 动机：连续断言步（Then + 继承的 And/But，中间无 When）几乎都在判同一屏 → 现状 K 步各跑一轮 brain。合并成 1 次调用（送 P1-B 采样帧 + K 条断言，逐条回 verdict+evidence）。
- 依赖：P1-B 帧采样（同屏才合并）；需放开 `step_validator` 的 `+0/+1` 硬墙，允许一次推进 K 步（每步带各自 evidence）。

### 🚨 反偷懒红线（合并必须守，违反即回退单步）
1. **逐条问责 schema**：每条断言各自回 `{id, verdict, evidence(≥15字, 引用该条独有元素), where(x_pct/y_pct 或 区域)}`。缺 evidence/where → reject。
2. **现有硬墙逐条套用**：≥15字 + 必须提屏上具体元素 + 禁"假设通过/推断成立/后台逻辑"话术。任一条不达标 → **reject 整个批量响应**，逐条理由喂回重判（reject 不耗配额，3 次判 fail）。
3. **去重闸**：跨条检测雷同/模板化 evidence（K 条给近乎相同证据 = 偷懒）→ reject。
4. **负向断言（But 不展示 X）加压**：必须写成"查了区域 Y，未发现 X"，不能默认没有就 PASS；缺 where 的 absence → reject。
5. **确定性交叉核验**：文字类断言用 OCR skill 在同图查该串字是否真存在；模型说看到但 OCR 没有 → 推翻。**不依赖模型诚实。**
6. **对抗复核**：批量里 evidence 最弱的 1–2 条，单独发"try to refute"怀疑式调用复核（这里才用并发 fan-out）。
7. **保守默认**：任一条 evidence 弱/不可视 → 该条判 fail、不推进。
- **折中档**：只合并"廉价结构类"断言；**负向断言 + P0 关键断言仍走独立调用**。

**验证**：base-001（4 条同屏断言）从 4 轮 brain → 1 次调用；人工抽查 evidence 是否逐条落地、有没有雷同/蒙混。

---

## Phase 4. Then 异步（延迟软断言 + scenario 流水线）【重点】
- 动机：一个 scenario 结尾常是一串 Then，设备要卡等每个 ~11s 裁决才收尾。异步后：抓帧 → 甩异步池判 → 设备**立刻领下一个 case**，把"末尾验证延迟"和"下一个 case 的启动/登录"重叠掉。
- 前提：断言输入（P1-B 采样帧）在 settle 那刻**冻结**，验证何时返回都不影响正确性。
- 机制：Then 处 settle → 冻采样帧 → enqueue 异步验证任务 `{step_idx, frames, assertion, evidence_anchors}` → 主流程继续；有界异步池跑 brain 验证；收尾报告前 `await` 所有待裁决，按 step_idx 回填。

### 🚨 正确性红线（异步必须守）
1. **帧在 settle 那刻同步冻结**，异步任务**绝不能自己再截图**（屏幕早变了）——方案成立的根。
2. **只对"已 settle + 不 gate 后续动作"的断言异步**：末尾 Then 块、失败也不影响后续操作的断言。**"过了才能下一步"的 gating 断言保持同步**。
3. **丢 fail-fast**：变成"跑到底 + 汇总所有失败点"（QA 里通常更想要）。
4. settle 到超时仍确认不了 → **回退同步 brain 判**，别硬冻帧异步。
5. 每个异步裁决打 `step_idx + 对应帧`，报告顺序不乱；**收尾前 await 全部**。
6. **有界并发池**（3 机 × 多断言会打满 LLM 限流，locator/brain 共用端点）。
7. **反偷懒硬墙原样不变**（同一带 evidence 硬墙的调用 + reject 重试环，只是不 inline await）。

- 可与 Phase 3 组合：末尾断言块**先合并成 1 次调用、再异步发出去**。

**验证**：改后单个 scenario 收尾不再阻塞在末尾裁决；整 feature 墙钟下降（异步裁决与下个 case 启动重叠）；通过率与同步版一致（红线守住则不应有差异）。

---

## Phase 5. 配置级提速（可与上面并行，纯 config + 验证）
- `AGENT_SPLIT_ACT_CHECK=true`：操作步（When/Given + 继承的 And）从"每步大模型"变"零大模型"（planner 预拆 + locator 执行）；检查步仍走大模型。
- `LLM_MODEL_PLANNER=google/gemini-2.5-flash-lite`：planner 降档（近乎纯文本推理，质量风险小）。
- brain 检查步维持 `gemini-3.5-flash`（强 VQA + 快的甜点）。
- **禁**给读屏 brain 换国产模型（被测为新闻类，政治内容有内容审查 → 拒答/误判）；locator 用 UI-TARS 无妨（只吐坐标不读内容）。

---

## Phase 0. 测量（无引擎代码，最先跑，指导取舍）
纯 config，对照 baseline（30m48s / 60%），同 3 台同 feature：
- **locator A/B**：`LLM_MODEL_LOCATOR=bytedance/ui-tars-1.5-7b`（开）vs 清空（关，退回 brain 估坐标）→ 量小模型净效果（07-27 引擎差太多不能当基准）。
- **split A/B**：`AGENT_SPLIT_ACT_CHECK` on/off。
- **planner 降档 A/B**：flash vs flash-lite。

---

## 排期 / 依赖
```
Phase 0（测量, config-only）──────── 随时可跑，指导后续
Phase 1（settle 闸 + 帧采样）─── 地基，先做
   └─ Phase 2（wait_for + no-progress 解耦）
   └─ Phase 3（Then-And 合并 + 反偷懒红线）
        └─ Phase 4（Then 异步 + 流水线 + 正确性红线）  ← 最大墙钟收益, 最invasive
Phase 5（config 提速）──────────── 可与 1–4 并行验证
```

## 通用验证纪律
- 每个 Phase 落地后：06-chat.feature / 同 3 台 / 对照 baseline，看**墙钟 + 聚合 + 通过率**三项，任一回退即查。
- 通过率**不许因提速而下降**——尤其 Phase 3/4 的反偷懒 & 正确性红线是硬约束，宁可不提速也不放松。

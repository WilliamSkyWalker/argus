---
name: argus-drive
description: 把当前 Claude Code 会话当 argus 的另一种 driver — 你（Claude）当 brain，adb 当 platform，对话 turn 当 agent.py 主循环，跑 .feature/.md 测试用例并出 argus 风格的 HTML 报告（复用 argus.report.save_html）。支持单 case debug、单 feature 回归、目录批量回归（自动过滤 @auto/@partial，跳过 @manual）+ 断点续跑 + 状态复用。
---

# argus-drive

调用：

- `/argus-drive <feature-path> [TC-id ...]` — 单文件，跑指定 TC（不指定则交互选）
- `/argus-drive <dir>` — 目录批量；递归收集 `*.feature`，自动跑全部 `@auto` + `@partial`，跳过 `@manual`
- 调用时检测到 `/tmp/argus-drive/state.json` → 询问 resume / restart

## 0. 启动检查 + resume 决策

每次 session 都要确认（PATH 不持久化）：

```bash
export PATH=$PATH:$HOME/Library/Android/sdk/platform-tools   # 按本机 platform-tools 实际路径调整
mkdir -p /tmp/argus-drive/journals /tmp/argus-drive/shots
adb devices                                          # 至少 1 个 device
adb shell dumpsys window | grep mCurrentFocus | head -1
ls /tmp/argus-drive/journals/ 2>/dev/null            # resume 检查
cat /tmp/argus-drive/state.json 2>/dev/null
```

**resume 流程**：

- 若 `state.json` + `journals/*.json` 都不存在 → 视为全新 run，初始化 `state.json`
- 若存在 → 默认 **resume**：读取所有 journals 收集已 `pass`/`fail` 的 (feature, TC) 对，本次只跑没记录的 scenario
  - 询问用户是否要重跑 fail 的；不指定就**跳过 fail**（之前的 fail 保留在历史 journal 里）
  - 用户要 restart：`rm -rf /tmp/argus-drive/journals /tmp/argus-drive/state.json` 再开

`state.json` 结构（每开始一个 scenario 就 update）：

```json
{
  "batch_id": "20260608-153012",
  "input": "tests/nb_cases/nb_mobile",
  "started_at": "2026-06-08T15:30:12",
  "current_feature": "tests/nb_cases/nb_mobile/01-account/01-account-login.feature",
  "current_tc": "TC-ACC-001c",
  "features_total": 39,
  "features_done": 2
}
```

若不在目标 App / 初始页：`am start -n <pkg>/<MainActivity>` 拉起，或 `input keyevent KEYCODE_BACK` 回退。

## 1. 解析输入

**目录入参**：

1. `find <dir> -name "*.feature" | sort` 收集
2. 对每个文件读 header 取 `# argus-target:` / `# argus-package:` / `# argus-reset-default:`
3. 抽出所有 `Scenario:` 段，按 tag 过滤：
   - 保留 `@auto`、`@partial`
   - 跳过 `@manual`、`@partial` 中显式 `@manual` 子标
   - 输出每个 case 的：TC-id、title、tags、scenario_steps（Given/When/And/Then/But 顺序保留）

**单文件 + TC-id**：只跑指定 case。

**单文件不带 TC-id**：扫 `@auto` 列表问用户挑（≤5 直接跑，>5 列清单）。

## 2. 单步回路（绝不连发两个 tap）

**每台设备启动先标定一次分辨率**（截图像素 → 设备像素）：

```bash
adb shell wm size                 # 物理分辨率，如 1440x3120
adb exec-out screencap -p | python3 -c "import sys,struct; d=sys.stdin.buffer.read(); i=d.find(b'IHDR'); w,h=struct.unpack('>II',d[i+4:i+12]); print(f'{w}x{h}')"
# 两者相等 → 1:1，照截图坐标直接点；不等（如三星 Override 分辨率）→ 按 设备px/截图px 比例换算
```

单步：

```bash
adb shell screencap -p /sdcard/argus.png
adb pull /sdcard/argus.png /tmp/argus-drive/shots/<TC>-<step>.png
```

→ Read 截图 → **视觉定位目标** → 按标定比例换算成设备坐标 → `adb shell input tap X Y` → 回到截图步骤。

**纯视觉驱动**：若被测 App 是 **Flutter**（整屏一个 canvas / 单 MainActivity），`uiautomator dump` 会报 `ERROR: could not get idle state`、拿不到可用节点树 —— **不要 dump 树，照截图看到的点**。仅当目标 App 是原生控件（dump 得到真实带 bounds 的树）时才用树辅助定位。

**每一步都要先看新截图再决定下一步动作。**

截图存到 `/tmp/argus-drive/shots/`（与 journal 隔离，方便统一清理）。

## 3. 坐标来源优先级

1. **视觉定位 + 分辨率标定**（默认，Flutter App 唯一路径）— 照截图看到的位置，按 §2 标定的 截图px↔设备px 比例换算成 tap 坐标。点不中先查标定比例对不对，**不是**「VLM 有偏差」
2. **uiautomator dump bounds 中心**（仅当目标 App 是原生控件、dump 得到真实带 bounds 的树时）— `[x1,y1][x2,y2]` → `((x1+x2)/2, (y1+y2)/2)`；对 Flutter App 拿不到树，跳过此项

## 4. 反模式（已踩过）

- ❌ **同坐标盲 retry**：2 次失败必须换策略（BACK 键 / swipe / 相对方位推）
- ❌ **链式 tap**：tap A 后必须看截图再决定 tap B；中间常被系统弹窗截断
- ❌ **不标定就估坐标**：点不中的真因是截图px→设备px 缩放没算对（如 Override 分辨率设备 screencap≠wm size），**不是** VLM y 偏差；先按 §2 标定分辨率，再照截图坐标点
- ❌ **忽略伴随弹窗**：常见拦截器要主动 dismiss
  - 首启通知权限（`com.google.android.permissioncontroller`）→ Don't allow
  - Dark Mode 引导（启动 5s 后）→ BACK 键
  - Rating 弹窗（详情页返回偶发）→ 找 X 节点 tap

**经验法则**：tap 后截图无变化时，BACK > 重新视觉估同一坐标。

## 5. 状态复用策略（跨 scenario 不强制 pm_clear）

**默认假设**：上一个 scenario 跑完留下的状态可作为下一个 scenario 的起点。**只在以下三类情况触发 reset：**

| 触发条件 | 动作 |
|---|---|
| scenario 显式标 `@reset:pm_clear` | `adb shell pm clear <pkg>` + relaunch |
| scenario 显式标 `@reset:relaunch` | `adb shell am force-stop <pkg>` + relaunch |
| 当前实际状态与 scenario 的 Given 不匹配 | 按 `tests/nb_cases/_preconditions.md` 恢复 |

**跨 feature 切换**：新 feature 第一个 scenario，按 file `# argus-reset-default:` 执行一次。

**Given 匹配判断**（不必 100% 精准，截图大致判断即可）：

| Given 关键词 | 期望状态 | 截图特征 |
|---|---|---|
| pm_clear 重启 / 未登录 / 登录首页 / 登录引导 Modal | 登录页 | 白底 + Continue with Google/Email/Facebook 按钮 |
| 当前屏幕为登录首页 | 同上 | 同上 |
| 验证码页 / Account Verification | 验证码页 | 6 位输入框 + Enter Verification Code 标题 |
| Email 输入页 / Continue with Email | Email 页 | 单行输入框 + Continue 按钮 + placeholder "Enter your Email" |
| Onboarding 主题选择页 / Build Your Feed | 主题选择 | "Build Your Feed" 大标题 + 滚轮分类 |
| Home Feed / For You Tab | 首页 | 顶部 For You/Headline/Latest tab |
| Settings 页 | 设置页 | "Settings" 标题 + 列表 |
| 已登录在 Home Feed | 首页 | 同 Home Feed |

**匹配失败的恢复路径**（按当前截图判断）：

1. 在登录页 → 需要 Home Feed：走 _preconditions.md 的「未登录 → Home Feed」流程
2. 在 Home Feed → 需要登录页：`adb shell pm clear <pkg>` + relaunch
3. 在中间页（验证码/Email/Onboarding） → 需要登录页：pm_clear + relaunch
4. 在中间页 → 需要 Home Feed：按 _preconditions.md 走完剩余流程
5. 其他不确定：pm_clear + relaunch（兜底）

## 6. 增量 journal + 断点续跑

**每个 feature 单独 journal**：`/tmp/argus-drive/journals/<basename>.json`

```json
{
  "started_at": "2026-06-08T15:30:12",
  "feature": "tests/nb_cases/nb_mobile/02-feed/04-feed-foryou.feature",
  "batch_id": "20260608-153012",
  "scenarios": [
    {
      "case": "TC-FEED-001b｜Top Bar 元素齐全",
      "tags": ["P0", "auto"],
      "result": "pass",
      "reason": "",
      "duration": 8.2,
      "scenario_steps": ["Given ...", "When ...", "Then ..."],
      "step_status": {"1": "pass", "2": "pass"},
      "steps_detail": [
        {
          "step": 1,
          "screenshot": "/tmp/argus-drive/shots/TC-FEED-001b-01.png",
          "action": {"type": "observe"},
          "observation": "...",
          "thinking": "..."
        }
      ]
    }
  ]
}
```

**写盘节奏**：

- 每跑完一个 scenario 立刻 Write 全文件（覆盖写，scenarios 数组追加新元素）
- 同步更新 `state.json` 的 `current_feature` / `current_tc`
- 不缓存：宁可多写也不丢

**resume 时**：

1. `ls /tmp/argus-drive/journals/*.json` 读出所有已存在的
2. 收集已 `pass` 的 (feature, TC) 对
3. 主循环跳过它们
4. `fail` 的默认也跳（已有失败记录），除非用户指定要重试

字段约定（对接 `argus.drive.render`）：

- `result` ∈ {pass, fail, skipped, timeout}
- `step_status` value ∈ {pass, fail, skip, pending}
- `steps_detail[].screenshot` 是文件路径，渲染器自动嵌 base64
- `action` 形如 `{"type":"tap","x":540,"y":199}` / `{"type":"observe"}` / `{"type":"back"}` / `{"type":"input","text":"..."}` / `{"type":"swipe","x1":..,"y1":..,"x2":..,"y2":..}` / `{"type":"adb","cmd":"pm clear ..."}`

## 7. 分文件 HTML 报告

每跑完一个 feature 立即出报告，不等到批次结束（中途崩了也有产物）：

```bash
TS=<batch_id 来自 state.json，全部 feature 共享同一个 ts>
# tests/<first-level>/cc_reports/<ts>/<basename>-claude-<ts>.html
# first-level = tests 下第一级目录（nb_cases / eve-kit ...）
# basename = feature 文件名去扩展名
REPORT_DIR="tests/<first-level>/cc_reports/${TS}"
mkdir -p "$REPORT_DIR"
python3 -m argus.drive.render \
  --journal /tmp/argus-drive/journals/<basename>.json \
  --output "$REPORT_DIR/<basename>-claude-${TS}.html"
```

批次结束打一份 markdown 总览：

```
批次报告目录: tests/nb_cases/cc_reports/20260608-153012/

| Feature | Pass | Fail | Skip | 报告 |
|---|---|---|---|---|
| 01-account-login | 15 | 2 | 0 | …01-account-login-claude-20260608-153012.html |
```

## 8. scope 与中断

| 入参 | 默认行为 |
|---|---|
| 单文件 + TC-id 列表 | 直接跑 |
| 单文件无 TC（≤5 auto） | 直接跑 |
| 单文件无 TC（>5 auto） | 列清单问 |
| 目录 | **视为同意全量跑**，不再询问；过滤 @auto+@partial，跳过 @manual |

中断恢复：

- 用户 Ctrl+C 中断 → state.json + 已写 journal 保留
- 重入直接 resume，不重复跑 pass 的
- 想完全重跑：`rm -rf /tmp/argus-drive/journals /tmp/argus-drive/state.json`

## 9. 单步 wall-clock 预算

目录批量本质很慢（单线程 + 每步看截图）。给用户合理预期：

- 单 scenario 平均：30s–5min（多步交互更长）
- 整目录批量（几十个 feature × 数百个 @auto/@partial）→ 数小时到几十小时
- 优先级建议：先跑 P0，跑完出报告，再跑 P1/P2

**默认顺序**：按文件名字典序，文件内按 Scenario 声明顺序。

## 10. 跟 argus.cli run 的差别

| | argus.cli run | /argus-drive |
|---|---|---|
| 主循环 | agent.py Python | Claude Code 会话 turn |
| 决策 | LLM API（Qwen 等） | 当前 Claude |
| 平台层 | argus.platforms.* | Bash + adb |
| 并发 | -j N + Selenium Grid | 单线程 |
| 报告 | HTML（report.py） | HTML（同一个 report.py，文件名带 -claude） |
| 状态复用 | 每 case pm_clear | 跨 case 默认不 reset |
| 断点续跑 | 不支持（CI 整跑） | 支持（state.json + per-feature journal） |
| 适用 | 批量回归 / CI | 调 prompt / 调 skills / 单 case debug / 目录小批量回归 |

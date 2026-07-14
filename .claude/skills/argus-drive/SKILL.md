---
name: argus-drive
description: 把当前 Claude Code 会话当 argus 的另一种 driver — 你(Claude)当 brain，Appium(argus MCP device_* tools)当 platform，对话 turn 当主循环，跑 .feature/.md 用例并出 argus 风格 HTML 报告(复用 argus.report)。支持单 case debug / 单 feature / 目录批量(过滤 @auto+@partial，跳 @manual) + 断点续跑 + 状态复用。
---

# argus-drive

**你(Claude)= brain，MCP `device_*` = platform，对话 turn = agent 主循环。纯视觉：看截图决策，不拿 UI 树。**

## 调用
- `/argus-drive <feature> [TC-id ...]` — 单文件跑指定 TC(不给则交互选)
- `/argus-drive <dir>` — 目录批量：递归收 `*.feature`，跑全部 `@auto`+`@partial`，跳 `@manual`
- 启动检测到 `/tmp/argus-drive/state.json` → 问 resume / restart

## 平台层：用 argus MCP device_* tools（backed by AppiumPlatform，别手搓底层脚本）
| tool | 作用 | 关键点 |
|---|---|---|
| `device_screenshot(serial,out_path?)` | 截屏落盘 | 返回 `path`+`screen_size`+**`scale`**(≠1 说明截图被缩放，坐标要 ×(1/scale) 换算——点不中先看这里) |
| `device_tap(x,y,serial)` | 点击 | 设备像素坐标 |
| `device_input(text,serial)` | 写文字 | 经 IME(mobile:type)，Flutter/原生+CJK 通吃；需先 tap 聚焦；**不发送** |
| `device_type_send(text,input_x,input_y,send_x,send_y,wait_s,serial,out_path?)` | 填+提交+等+截屏 一步 | 多轮「填+提交+观察」省往返 |
| `device_swipe(x1,y1,x2,y2,duration_ms,serial)` | 滑动 | 惯性滚动调大 duration |
| `device_key(key,serial)` | 按键 | enter/back/home/…；⚠️ 部分 Flutter App back 会退出 App，关浮层用界面关闭控件 |
| `device_launch(pkg,activity?,serial,force_stop?)` | 拉起/relaunch | |

用法：`device_screenshot` → Read 图 → 视觉定位 → `device_tap/input/swipe` → 回截图。多设备每 call 传 `serial`。全程走 MCP，不碰 adb。

## 0. 启动检查 + resume
```bash
mkdir -p /tmp/argus-drive/journals /tmp/argus-drive/shots
ls /tmp/argus-drive/journals/ 2>/dev/null; cat /tmp/argus-drive/state.json 2>/dev/null
```
- 无 state/journals → 全新 run，初始化 state.json
- 有 → 默认 **resume**：读所有 journal 收集已 pass/fail 的 (feature,TC)，只跑没记录的；fail 默认跳(除非用户要重试)；restart = `rm -rf /tmp/argus-drive/journals /tmp/argus-drive/state.json`
- `state.json`：`{batch_id, input, started_at, current_feature, current_tc, features_total, features_done}`，每开始一个 scenario 就 update

## 1. 解析输入
- 目录：`find <dir> -name '*.feature' | sort`；读 header `# argus-target/package/reset-default`；抽 `Scenario:` 段按 tag 过滤(留 @auto/@partial，跳 @manual)，输出每 case 的 TC-id/title/tags/steps(Given/When/And/Then/But 顺序)
- 单文件+TC-id：只跑指定；单文件无 TC：扫 @auto 问用户挑(≤5 直接跑)

## 2. 单步回路（绝不连发两个 tap）
`device_screenshot` → Read → **视觉定位** → 按 `scale` 换算(scale==1 直接用截图坐标；≠1 则 ×1/scale)→ `device_tap`。**每步先看新截图再决定下一步**。截图存 `/tmp/argus-drive/shots/`。
- **Flutter App(整屏 canvas/单 Activity)**：纯视觉，别 dump 树(拿不到)；原生控件才可辅助。
- 点不中的真因是 **scale/分辨率标定**没算对，**不是** VLM 偏差；先核 `device_screenshot` 返回的 scale。

## 3. 反模式（踩过）
- ❌ 同坐标盲 retry：2 次失败必换策略(BACK/swipe/换方位)
- ❌ 链式 tap：tap A 后必看截图再 tap B(常被弹窗截断)
- ❌ 不标定就估坐标
- ❌ 忽略伴随弹窗：通知权限→Don't allow、Dark Mode 引导→BACK、Rating→点 X
- 法则：tap 后无变化时，BACK > 重估同坐标

## 4. 状态复用（跨 scenario 不强制 reset）
默认上个 scenario 的结束态作下个起点。仅三类触发 reset：
| 触发 | 动作 |
|---|---|
| `@reset:relaunch` | `device_launch(pkg, force_stop=True)` |
| `@reset:pm_clear` | 数据级重置：`install_apk` 重装，或走 `_preconditions.md` 恢复到未登录态(无纯 driver 清数据原语) |
| 当前态与 scenario Given 不符 | 按 `tests/<target>/_preconditions.md` 恢复 |

跨 feature 切换：新 feature 首个 scenario 按 file `# argus-reset-default:` 执行一次。Given 匹配靠截图大致判断(不必精准)，不符就按 `_preconditions.md` 的恢复流程走。

## 5. journal + 断点续跑 + 报告
- **每 feature 一个 journal**：`/tmp/argus-drive/journals/<basename>.json`，每跑完一个 scenario 立刻覆盖写全文件(不缓存)，同步 update state.json。
- journal 结构对接 `argus.drive.render`：`{started_at, feature, batch_id, scenarios:[{case, tags, result(pass|fail|skipped|timeout), reason, duration, scenario_steps, step_status{n:pass|fail|skip|pending}, steps_detail:[{step, screenshot(路径,渲染器自动嵌base64), action, observation, thinking}]}]}`
  - action 形如 `{"type":"tap","x":..,"y":..}` / `observe` / `back` / `{"type":"input","text":..}` / `swipe` / `{"type":"launch","pkg":..}`
- **每跑完一个 feature 立即出报告**(中途崩也有产物)：
  ```bash
  # tests/<first-level>/cc_reports/<batch_id>/<basename>-claude-<batch_id>.html
  python3 -m argus.drive.render --journal /tmp/argus-drive/journals/<basename>.json --output <上述路径>
  ```
- 批次末打 markdown 总览表(Feature|Pass|Fail|Skip|报告路径)。

## 6. scope / 中断 / 预算
- 目录入参 = **视为同意全量跑**，不再问。
- 中断(Ctrl+C)：state.json + 已写 journal 保留，重入直接 resume 不重跑 pass 的。
- 慢：单 scenario 30s–5min，整目录数小时~几十小时；建议先 P0 出报告再 P1/P2。默认文件名字典序、文件内 Scenario 声明序。

## 7. vs `argus.cli run`
| | argus.cli run | /argus-drive |
|---|---|---|
| 主循环 | agent.py Python | Claude 会话 turn |
| 决策 | LLM API | 当前 Claude |
| 平台 | argus.platforms(进程内 Appium) | MCP `device_*`(同 AppiumPlatform) |
| 并发 | -j N | 单线程 |
| 状态 | 每 case reset | 跨 case 默认不 reset |
| 续跑 | 不支持 | 支持(state.json+per-feature journal) |
| 适用 | 批量回归/CI | 调 prompt/skills、单 case debug、小批量 |

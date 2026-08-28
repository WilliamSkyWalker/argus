# [项目名] 测试前置状态恢复指南（样例 — 按实际产品改写）

> argus.cli 自动加载本目录的 `_preconditions.md`，prepend 到本 target 下每个 case 文本前。
> 当 LLM 发现当前屏幕与 Background/Given 描述的前置状态不符时，按此指引先恢复，再开始测试主体。
> ⚠️ 删掉本说明段、把下面的占位内容替换成你产品的真实状态机与导航路径，否则会误导 LLM。

## 1. 判断当前所处状态（按屏幕特征，优先级高→低）

| 屏幕特征 | 当前状态 |
|---|---|
| 出现 "Continue with Email/Google/..." 等按钮的全屏页 | **未登录**（登录页） |
| 6 位验证码输入框 / "Enter the code" | **验证码页**（登录中途） |
| 顶部导航 + 主内容列表 | **首页**（已登录正常态） |
| 文章/详情正文页 | **子页**（已进入内容） |

> 系统弹窗（通知权限 / 引导 / 评分）会盖在上述任一状态之上，先 dismiss 再判断主状态。

## 2. 恢复流程（样例）

### 未登录 → 首页
1. 点 **"Continue with Email"** → 输入 `${EMAIL}` → 点 **"Continue"**
2. 验证码页输入 `123456`（按你产品的 mock OTP 改）
3. 落到 Onboarding 就走完默认选项，最终到首页

### 在 Onboarding → 首页
按默认值逐步 Continue 直到落到首页。

## 3. 首页 → 子页 导航（样例，按实际改）

| 目标页 | 路径（从首页起步） | 关键 UI 特征 |
|---|---|---|
| 设置页 | 左上菜单 → Settings | 顶部 "Settings" 标题 |
| 个人页 | 左上菜单 → Profile | 头像 + 用户名 |

**导航规则**：先 check 当前是否已在目标页（在则跳过）；不在则按上表走；连续 2 次找不到入口 → 整 case fail，reason 标 "fixture 导航失败：找不到 X 入口"。

## 4. 常见拦截弹窗（一律 dismiss 再继续）

| 弹窗 | 处理 |
|---|---|
| 首启权限弹窗（system dialog） | 点 "Don't allow" / "拒绝" |
| 引导/评分弹窗 | 找 X 关闭，找不到按 BACK |
| 其他未知弹窗 | 优先 X / 取消 / 返回，再不行按 BACK |

## 5. 行为约束

- **只在不符合前置态时执行**：已在正确状态就别多此一举。
- **恢复路径上的 step 不计入 step_progress**：`current_step_index` 从测试主体第一个 Given/When/Then 起算；恢复期间在 thinking 里注明"正在做前置恢复/导航"。
- **恢复中点错按钮算环境问题**，不算 case fail —— 重试/换路径，连续两次失败再 fail。

## 6. 测试账号速查
- Email: `${EMAIL}`（账号池 `_accounts.json`，多设备 worker[i]→accounts[i]）
- OTP / 密码：按你产品改（如固定 mock OTP，或 `${PASSWORD}`）

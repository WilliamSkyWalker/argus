# [项目名称]

被测项目简要描述（`argus list` 会取本文件第一段非标题行做说明）。

- **URL**: https://example.com         （browser target 用）
- **平台**: browser | ios | android
- **包名**: com.example.app            （android target 用；跑测/重置要用）
- **备注**: 其他信息（测试账号、特殊前置等）

## 目录里该有什么

```
tests/<target>/
  ├── cases/                 # 用例：*.feature（主推）或子目录；旧 .md 也兼容
  │   └── example.feature
  ├── _preconditions.md      # 异常态恢复指南，auto-prepend 到每个 case（强烈建议写）
  ├── _accounts.json         # 账号池（真账号，被 .gitignore；不要 commit）
  ├── _accounts.json.example # 账号池模板（入库）
  └── reports/<ts>/*.html    # 报告（argus run --report 自动落盘）
```

## .feature 元数据头（4 行，见 cases/example.feature）

```
# argus-target: <target>           # 报告归类，一般同目录名
# argus-platform: android|ios|browser
# argus-package: com.x.y           # 仅 android 需要
# argus-reset-default: relaunch    # android 默认重置：pm_clear|relaunch|none
```

## 多账号并发（可选）

多设备并发跑同一 suite 时，每台设备用不同账号避免冲突：把账号池写到本 target 顶层
`_accounts.json`（格式见 `_accounts.json.example`）。

分配规则：`argus run <target> --device s1 s2 s3` 时 s1→accounts[0]、s2→accounts[1]、
s3→accounts[2]；账号池小于设备数时报警并 fallback 到 [0]。

用例文本里用 `${EMAIL}` / `${PASSWORD}` / `${USERNAME}` 等占位符（键名取自
`_accounts.json` 每条 dict、自动大写），argus 每个 case 跑前替换；未匹配的原样保留。

## 跑起来

```bash
argus run <target>                                  # 整目录递归
argus run <target>/cases/example.feature            # 单文件
argus run <target> --device s1 s2 --apk app.apk --report   # 多设备 + 装包 + 报告
```

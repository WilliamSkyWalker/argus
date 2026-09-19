# ios_demo

iOS 自动化演示：在系统「设置」App 里进入「通用 › 关于本机」并校验页面结构。

- **平台**: ios
- **包名**: com.apple.Preferences
- **备注**: 真机需 `.env` 配 `IOS_TEAM_ID`/`IOS_BUNDLE_ID=com.apple.Preferences`；模拟器可 `argus setup` 后直接跑。系统语言若为英文，把用例中的中文菜单名对应为 General / About。

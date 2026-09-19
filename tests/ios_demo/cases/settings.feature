# language: zh-CN
# encoding: utf-8
# argus-target: ios_demo
# argus-platform: ios
# argus-package: com.apple.Preferences

Feature: iOS 设置 App 自动化演示

  Background:
    Given 「设置」App 已打开并位于设置首页（若在子页面，则点击左上角返回直到回到首页）

  @TC-IOS-001 @P0 @auto @ios
  Scenario: 进入「关于本机」并校验页面
    When 用户点击「通用」（General）
    Then 页面标题显示「通用」，列表首项为「关于本机」（About）
    When 用户点击「关于本机」
    Then 页面标题显示「关于本机」
    And 列表中出现「软件版本」/「iOS 版本」一行，其右侧显示形如 "17.x" 或 "18.x" 的版本号
    And 列表中出现「型号名称」或「名称」一行
    When 用户点击左上角返回按钮两次
    Then 回到设置首页，页面顶部显示「设置」大标题

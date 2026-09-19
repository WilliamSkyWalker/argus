# language: zh-CN
# encoding: utf-8
# argus-target: android_demo
# argus-platform: android
# argus-package: com.android.settings
# argus-reset-default: relaunch

Feature: Android 设置 App 自动化演示

  Background:
    Given 「设置」App 已打开并位于设置首页

  @TC-AND-001 @P0 @auto @android
  Scenario: 通过搜索进入「关于手机」
    When 用户点击顶部搜索框并输入 "关于手机"
    Then 搜索结果列表中出现「关于手机」（或「关于设备」/「About phone」）条目
    When 用户点击该条目
    Then 页面标题显示「关于手机」（或「关于设备」）
    And 页面中出现「Android 版本」一行并显示版本号
    And 页面中出现「设备名称」或「型号」一行
    When 用户向下滑动列表
    Then 页面能滚动，且出现「版本号」（Build number）一行

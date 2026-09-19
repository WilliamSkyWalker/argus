# language: zh-CN
# encoding: utf-8
# argus-target: mac_demo
# argus-platform: mac

Feature: macOS 计算器自动化演示

  @TC-MAC-001 @P0 @auto @mac
  Scenario: 用计算器完成一次加法
    Given 计算器窗口已打开且显示区为 0（若不是则先点击 AC/C 清零）
    When 用户依次点击数字键 "1"、"2"，运算符 "+"，数字键 "3"、"4"，再点击 "="
    Then 计算器显示区显示结果 "46"
    When 用户点击清零键（AC 或 C）
    Then 计算器显示区重新显示 "0"

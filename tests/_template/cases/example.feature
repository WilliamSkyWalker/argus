# language: zh-CN
# encoding: utf-8
#
# ── Argus 元数据头（cli.py 解析；新建 target 时按实际改这 4 行）──
# ⚠️ 值行不要写行内 # 注释，会被一起读进值里；说明只放在下面整行注释。
# argus-target: example-target
# argus-platform: android
# argus-package: com.example.app
# argus-reset-default: relaunch
#
# 字段含义：
#   argus-target   报告归类用，一般同目录名
#   argus-platform ios | android | browser（限定本文件用例跑的平台）
#   argus-package  仅 android 需要：被测包名（reset/relaunch 用）
#   argus-reset-default  android 每 scenario 前默认重置：pm_clear | relaunch | none
#
# 说明：
#   - 这是 .feature（Gherkin/Cucumber）样例，argus 主推 & 默认格式（gherkin.py 全套解析）。
#   - browser target 去掉 argus-package；platform 写 browser。
#   - argus 也兼容旧的 TDD 三段式 .md（### TC- 切块），但新 target 一律用 .feature。

Feature: 示例功能 - 首页基本可用
  作为该产品的用户
  我希望进入首页能看到核心内容
  以便确认主流程可用

  Background:
    Given 测试账号 ${EMAIL} 已登录
    And App/页面已打开并落在首页

  @TC-TPL-001 @P0 @auto @android
  Scenario: 首页主结构可见
    When argus 观察首页
    Then 屏幕顶部显示标题或顶栏
    And 主体区域显示至少一条内容
    But 不出现报错/空白/崩溃提示

  @TC-TPL-002 @P1 @auto @android
  Scenario: 点击首条内容进入详情
    Given 首页列表已加载
    When 用户点击首条内容
    Then 屏幕进入对应详情页（顶部可见该内容标题）

  # Tag 约定（gherkin.py 识别）：
  #   @P0/@P1/@P2 优先级 · @auto/@partial/@manual 自动化程度（partial/manual 自动 skip）
  #   @ios/@android/@both 平台 · @TC-XXX 用例ID · @reset:pm_clear|relaunch|none 覆盖默认重置
  #   @skip/@wip 整 scenario 跳过
  #
  # 书写约定：每 Scenario 自包含（前置写进 Background）；Then 写成可独立验证的列表；
  #   位置参考写方位不写坐标；不可视觉验证的断言（埋点/后端/系统时间）打 @skip-vision 或改写。

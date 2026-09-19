# language: zh-CN
# encoding: utf-8
# argus-target: web_demo
# argus-platform: browser

Feature: 浏览器示例站点自动化演示

  Background:
    Given 浏览器已打开 https://example.com

  @TC-WEB-001 @P0 @auto @browser
  Scenario: 首页内容可见并可跟随链接跳转
    Then 页面显示大标题 "Example Domain"
    And 标题下方有一段说明文字，且包含一条 "More information..." 链接
    When 用户点击 "More information..." 链接
    Then 页面跳转到 IANA 相关页面，页面中出现 "IANA" 或 "Example Domains" 字样
    And 地址栏不再是 example.com 首页

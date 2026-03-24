---
name: browser_visible
description: "当用户希望看到浏览器操作画面时，使用 browser_use 启动浏览器（headless 模式），浏览器画面通过 Chat 页面右侧的 Live View 面板实时展示，无需弹出额外的浏览器窗口。适用于用户想看到页面、演示或调试场景。"
metadata:
  {
    "builtin_skill_version": "1.0",
    "copaw":
      {
        "emoji": "🖥️",
        "requires": {}
      }
  }
---

# 可见浏览器（Live View 面板）参考

**browser_use** 始终以无头（headless）模式运行，不会弹出浏览器窗口。当用户希望**看到浏览器操作画面**时，Chat 页面右侧会自动弹出 **Live View 面板**，实时展示浏览器截图流（~5fps），用户还可以通过面板直接用鼠标点击、键盘输入来干预浏览器。

## 何时使用

- 用户说：「打开浏览器」「我想看到浏览器」「帮我浏览网页」
- 用户希望看到页面加载、点击、填表等过程（演示、调试、教学）
- 用户需要与页面交互（如登录、验证码等需人工参与的场景）—— 通过 Live View 面板即可操作

## 使用方式（browser_use）

1. **启动浏览器**
   调用 **browser_use**，`action` 为 `start`：
   ```json
   {"action": "start"}
   ```
   浏览器以 headless 模式启动，Chat 右侧自动弹出 Live View 面板显示实时画面。

2. **打开页面并操作**
   与普通模式用法相同，例如：
   - 打开 URL：`{"action": "open", "url": "https://example.com"}`
   - 获取页面结构：`{"action": "snapshot"}`
   - 点击、输入等：使用 `ref` 或 `selector` 进行 click、type 等

3. **关闭浏览器**
   使用完毕后调用：`{"action": "stop"}` 关闭浏览器，Live View 面板自动隐藏。

## Live View 面板功能

| 功能 | 说明 |
|------|------|
| 实时画面 | ~5fps JPEG 截图流，实时反映 AI 操作 |
| 鼠标交互 | 用户可直接在面板中点击、双击、滚轮 |
| 键盘输入 | 聚焦面板后可直接键盘输入 |
| URL 导航 | 面板顶部 URL 栏可手动导航、后退、刷新 |
| 自动显隐 | 浏览器启动时面板自动出现，关闭时自动隐藏 |

## 注意

- 不再需要 `headed: true` 参数，浏览器始终以 headless 模式运行。
- 即使传入 `headed: true`，也会被忽略，强制使用 headless 模式。
- Live View 面板通过 WebSocket 双向通信，支持用户实时干预浏览器操作。

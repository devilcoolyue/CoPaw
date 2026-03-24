# Browser Preview 浏览器预览功能 — 技术总览

> 本文档是 CoPaw 浏览器预览功能的完整技术介绍，涵盖架构设计、Live View 实时面板、多 agent 上下文隔离、多标签页管理、会话持久化等全部子系统。

---

## 一、功能概述

CoPaw 的浏览器预览功能基于 Playwright，为 AI agent 提供完整的浏览器自动化能力。核心特性：

| 特性 | 说明 |
|------|------|
| **Headless 浏览器** | Playwright 驱动，始终以 headless 模式运行，无需弹出浏览器窗口 |
| **Live View 实时面板** | Chat 页面右侧实时展示浏览器画面（~5fps），支持鼠标/键盘交互 |
| **多标签页** | Chrome 风格 Tab 栏，支持新建/关闭/切换标签页 |
| **多 Agent 隔离** | 每个 agent 拥有独立的 BrowserContext，cookie/session 互不干扰 |
| **会话持久化** | stop/restart 后自动恢复 cookie、localStorage、sessionStorage |
| **手动 Cookie 管理** | get/set cookies、save/load storage state 四个 API |
| **CDP 高效帧推送** | Chromium 使用 CDP Screencast 事件驱动推帧，WebKit 自动降级截图 |

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Frontend (React)                            │
│                                                                     │
│  ┌──────────────────────┐    ┌────────────────────────────────────┐ │
│  │   Chat Page           │    │   BrowserLiveView Component       │ │
│  │                       │    │                                    │ │
│  │  - 浏览器状态轮询      │    │  Tab Bar (Chrome 风格标签栏)       │ │
│  │  - 切换按钮 (Globe)    │    │  ├─ Traffic Lights 装饰            │ │
│  │  - 拖拽调宽            │    │  ├─ Tab × N (点击切换/关闭)        │ │
│  │                       │    │  └─ + 新建 Tab 按钮                │ │
│  │                       │    │                                    │ │
│  │                       │    │  Toolbar (地址栏)                  │ │
│  │                       │    │  ├─ Back / Forward / Reload        │ │
│  │                       │    │  ├─ URL Input                      │ │
│  │                       │    │  └─ Hide 按钮                      │ │
│  │                       │    │                                    │ │
│  │                       │    │  Canvas (渲染 JPEG 帧)             │ │
│  │                       │    │  ├─ 鼠标事件 → 归一化坐标 → WS      │ │
│  │                       │    │  └─ 键盘事件 → WS                  │ │
│  │                       │    │                                    │ │
│  │                       │    │  Status Bar (连接状态)              │ │
│  └──────────────────────┘    └────────────────────────────────────┘ │
│           │                              │                          │
│           │ REST /browser/status         │ WebSocket /browser/ws    │
└───────────┼──────────────────────────────┼──────────────────────────┘
            │                              │
            ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Backend (FastAPI + Playwright)                    │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                browser_live_view.py (Router)                 │   │
│  │                                                              │   │
│  │  REST: GET /browser/status   → browser state summary         │   │
│  │        GET /browser/tabs     → tab list                      │   │
│  │                                                              │   │
│  │  WS:   /browser/ws?agent_id= → 双向通信                      │   │
│  │        Server→Client: frame / session / navigation / tabs    │   │
│  │        Client→Server: mouse / keyboard / navigate /          │   │
│  │                       switch_tab / new_tab / close_tab       │   │
│  │                                                              │   │
│  │  Screencaster:                                               │   │
│  │    Chromium → CDP Page.startScreencast (事件驱动)             │   │
│  │    WebKit   → page.screenshot() 轮询 (降级)                  │   │
│  └─────────────────────┬───────────────────────────────────────┘   │
│                        │                                            │
│                        ▼                                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              browser_control.py (Core Engine)                │   │
│  │                                                              │   │
│  │  共享层 (_browser_state):                                    │   │
│  │    playwright / browser / headless / browser_kind            │   │
│  │                                                              │   │
│  │  隔离层 (_agent_contexts):                                   │   │
│  │    agent_id → AgentBrowserContext                            │   │
│  │      ├─ context (Playwright BrowserContext)                  │   │
│  │      ├─ pages {page_id: Page}                               │   │
│  │      ├─ refs / console_logs / network_requests              │   │
│  │      ├─ pending_dialogs / pending_file_choosers             │   │
│  │      └─ _pending_session_storage                            │   │
│  │                                                              │   │
│  │  持久化层:                                                   │   │
│  │    ~/.copaw/browser_state/{agent_id}.json                   │   │
│  │    (cookie + localStorage + sessionStorage)                 │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 三、核心子系统

### 3.1 浏览器引擎管理

#### 两层状态模型

```python
# 共享：所有 agent 共用一个 Playwright 进程和浏览器实例
_browser_state = {
    "playwright": None,      # Playwright 实例
    "browser": None,         # Browser 实例
    "headless": True,        # 始终 headless
    "browser_kind": None,    # "chromium" | "webkit"
    "_idle_task": None,      # 空闲看门狗
}

# 隔离：每个 agent 独立的 BrowserContext
_agent_contexts: dict[str, AgentBrowserContext] = {}
```

#### 两阶段启动 (`_ensure_browser`)

```
Phase 1: 确保共享浏览器进程
  ├─ 检测系统默认浏览器 (Chrome/WebKit)
  ├─ 优先使用 Chromium（支持 CDP）
  ├─ macOS 无 Chromium 时降级 WebKit
  └─ 记录 browser_kind

Phase 2: 确保 agent 有 BrowserContext
  ├─ 检测 ~/.copaw/browser_state/{agent_id}.json
  ├─ 有 → new_context(storage_state=path) 恢复状态
  ├─ 无 → new_context() 创建空白
  ├─ 附加事件监听器
  └─ 加载 pending sessionStorage 数据
```

#### Hybrid Sync/Async 模式

| 平台 | 模式 | 原因 |
|------|------|------|
| macOS / Linux | Async Playwright | 最佳性能 |
| Windows + Uvicorn reload | Sync Playwright + ThreadPool | 规避 `asyncio.create_subprocess_exec` 的 `NotImplementedError` |

所有 Playwright 调用均通过 `_USE_SYNC_PLAYWRIGHT` 标志分流，sync 模式使用 `run_in_executor` 桥接。

#### Idle Watchdog

```python
async def _idle_watchdog(idle_seconds=1800.0):  # 默认 30 分钟
    while True:
        await asyncio.sleep(60)  # 每分钟检查
        # 逐 agent 检查，关闭超时的 context
        expired = [aid for aid, ctx in _agent_contexts.items()
                   if (now - ctx.last_activity_time) >= idle_seconds]
        for aid in expired:
            await _async_close_agent_context(aid)  # 自动保存 + 关闭
        # 全空时停止浏览器进程
        if not _agent_contexts:
            await _async_stop_browser_process()
            return
```

---

### 3.2 Live View 实时面板

#### 通信协议

**WebSocket 端点：** `/api/browser/ws?token=&agent_id=`

| 方向 | 类型 | 格式 | 说明 |
|------|------|------|------|
| Server → Client | frame | text JSON + binary JPEG | 浏览器画面帧 |
| Server → Client | session | text JSON | 浏览器状态变更 |
| Server → Client | navigation | text JSON | 导航事件 |
| Server → Client | tabs | text JSON | 标签页列表更新 |
| Client → Server | mouse | text JSON | 鼠标事件（归一化坐标） |
| Client → Server | keyboard | text JSON | 键盘事件 |
| Client → Server | navigate | text JSON | URL 导航 |
| Client → Server | switch_tab | text JSON | 切换标签页 |
| Client → Server | new_tab | text JSON | 新建标签页 |
| Client → Server | close_tab | text JSON | 关闭标签页 |

#### 帧传输详情

**Server → Client 帧格式：**

```
Text frame:   {"type": "frame", "ts": 1711234567890, "w": 1280, "h": 720}
Binary frame: <JPEG bytes>
```

前端收到 text metadata 后等待下一个 binary 消息 → Blob → ObjectURL → Image → `canvas.drawImage()`。

#### 双模帧采集

| 引擎 | 模式 | 原理 | 优势 |
|------|------|------|------|
| Chromium (async) | CDP Screencast | `Page.startScreencast` 事件驱动 | 页面静止时零开销，内建 ACK 流控 |
| WebKit / Chromium (sync) | Screenshot 轮询 | `page.screenshot(type="jpeg", quality=90)` 每 0.2s | 全引擎通用 |

CDP Screencast 生命周期：

```
WS 客户端连接 → 检测 Chromium → cdp = context.new_cdp_session(page)
  → cdp.send("Page.startScreencast", {format, quality, maxWidth, maxHeight})
  → cdp.on("Page.screencastFrame", handler)
  → handler: base64 decode → 广播 JPEG → cdp.send("Page.screencastFrameAck")

页面切换 → 检测 page 对象变化 → stop 旧 CDP → start 新 CDP
全部断开 → cdp.send("Page.stopScreencast") → cdp.detach()
```

#### 输入处理

鼠标坐标归一化流程：

```
前端 canvas click (clientX, clientY)
  → getBoundingClientRect() → 归一化 (x/w, y/h) → 值域 [0, 1]
  → WS 发送 {"type": "mouse", "action": "click", "x": 0.45, "y": 0.32}
  → 后端: x * viewport.width, y * viewport.height → page.mouse.click(px, py)
```

---

### 3.3 多 Agent 上下文隔离

#### 隔离粒度

```
Playwright 进程 (共享)
└── Browser 实例 (共享)
    ├── BrowserContext A (agent "default")
    │   ├── cookie/localStorage/session 独立
    │   ├── Page 1 (login page)
    │   └── Page 2 (dashboard)
    └── BrowserContext B (agent "9N5Fn7")
        ├── cookie/localStorage/session 独立
        └── Page 1 (another site)
```

#### 前端感知

```typescript
// WebSocket 连接按 agent_id 隔离
`/api/browser/ws?token=xxx&agent_id=9N5Fn7`

// 状态轮询按 agent_id 隔离
browserApi.getStatus(selectedAgent)

// BrowserLiveView 组件接收 agentId prop
<BrowserLiveView agentId={selectedAgent} onHide={...} />
```

#### 后端隔离

```python
# WS 客户端按 agent_id 分组
_ws_clients: dict[str, set[WebSocket]] = {}

# Screencaster 循环遍历所有有客户端的 agent
async def _screencaster_loop():
    while _has_any_clients():
        for agent_id in list(_ws_clients.keys()):
            page = get_page(agent_id=agent_id)
            jpeg_bytes = await page.screenshot(...)
            await _broadcast_to_agent(agent_id, ...)
```

#### 生命周期独立性

| 场景 | 行为 |
|------|------|
| Agent A stop | 仅关闭 A 的 context，B 不受影响 |
| Agent A idle 超时 | 仅关闭 A 的 context，B 不受影响 |
| 所有 agent 都关闭 | 停止共享浏览器进程 |
| 前端切换 agent | `useEffect` 依赖 `agentId`，自动重连 WS |

---

### 3.4 多标签页管理

#### Tab Bar 组件

```
BrowserLiveView
├── Tab Bar
│   ├── Traffic Lights (红/黄/绿装饰圆点)
│   ├── Tab × N
│   │   ├── tabTitle (12px, ellipsis)
│   │   └── tabClose (X 按钮, hover 显示)
│   └── newTabBtn (+)
├── Toolbar (地址栏)
└── Canvas
```

#### 标签页变更检测

使用指纹比较避免频繁推送：

```python
def _tab_fingerprint(tabs):
    """只比较 page_id + active 状态，不含 title/url"""
    return "|".join(f"{t['page_id']}:{'A' if t['active'] else '-'}" for t in tabs)
```

| 比较策略 | 问题 |
|----------|------|
| 全量比较（含 title/url） | 页面加载中 title 不断变化 → 每帧都推送 |
| 只比 page_id + active | 仅结构变化时推送，稳定可靠 |

生命周期事件和手动操作使用 `force=True` 确保即时推送。

---

### 3.5 会话持久化

#### 存储层次

| 存储类型 | Playwright 原生支持 | 我们的处理 |
|----------|:------------------:|-----------|
| Cookie | ✅ `storage_state()` 包含 | 自动保存/加载 |
| localStorage | ✅ `storage_state()` 包含 | 自动保存/加载 |
| sessionStorage | ❌ 不包含 | JS evaluate 抓取 + 延迟注入 |
| 内存变量 | ❌ 无法序列化 | 页面复用 (navigate) 保留 |

#### 自动持久化流程

```
             ┌─────────────────┐
             │  浏览器运行中     │
             │  (BrowserContext │
             │   cookie/storage │
             │   sessionStorage)│
             └────────┬────────┘
                      │
            stop / idle超时 / exit
                      │
                      ▼
        ┌─────────────────────────────┐
        │  _async_close_agent_context │
        │  / _close_agent_context     │
        │                             │
        │  1. context.storage_state() │ ← cookie + localStorage
        │  2. page.evaluate(JS)       │ ← sessionStorage
        │  3. json.dump(state, file)  │
        └─────────────┬───────────────┘
                      │
                      ▼
     ~/.copaw/browser_state/{agent_id}.json
                      │
              下次 start 时
                      │
                      ▼
        ┌─────────────────────────────┐
        │     _ensure_browser()       │
        │     Phase 2                 │
        │                             │
        │  1. new_context(            │
        │       storage_state=path)   │ ← cookie + localStorage 恢复
        │  2. ctx._pending_session_   │
        │       storage = ss_data     │ ← sessionStorage 暂存
        └─────────────┬───────────────┘
                      │
              open(url) 时
                      │
                      ▼
        ┌─────────────────────────────┐
        │  _action_open → new_page()  │
        │  → page.goto(url)           │
        │  → _restore_session_storage │ ← sessionStorage 注入
        │  → page.reload()            │ ← SPA 重新初始化
        └─────────────────────────────┘
```

#### 页面复用

```python
# _action_open() 中的关键逻辑
if page_id in ctx.pages and ctx.pages[page_id]:
    # 已有页面 → navigate 复用，保留 sessionStorage 和内存变量
    return await _action_navigate(url, page_id)
# 否则创建新页面
```

---

## 四、`browser_use` 工具完整 Action 列表

### 4.1 生命周期

| Action | 说明 |
|--------|------|
| `start` | 启动浏览器（headless），已运行时返回 "already running" |
| `stop` | 关闭当前 agent 的 context（自动保存状态），最后一个 agent 关闭时停止浏览器进程 |
| `install` | 安装 Playwright 浏览器引擎 |

### 4.2 导航

| Action | 关键参数 | 说明 |
|--------|---------|------|
| `open` | `url` | 打开 URL；已有页面时复用（navigate），否则新建 |
| `navigate` | `url` | 在当前页面导航到 URL |
| `navigate_back` | — | 浏览器后退 |
| `close` | `page_id` | 关闭指定页面 |

### 4.3 页面信息

| Action | 关键参数 | 说明 |
|--------|---------|------|
| `snapshot` | `frame_selector` | 获取页面无障碍树（含 ref） |
| `screenshot` | `path`, `full_page` | 截图保存 |
| `pdf` | `path` | 导出 PDF |
| `console_messages` | `level` | 获取控制台日志 |
| `network_requests` | `include_static` | 获取网络请求 |

### 4.4 交互

| Action | 关键参数 | 说明 |
|--------|---------|------|
| `click` | `ref`/`selector`, `double_click`, `button` | 点击元素 |
| `type` | `ref`/`selector`, `text`, `submit` | 输入文本 |
| `press_key` | `key` | 按键（如 "Enter", "Control+a"） |
| `hover` | `ref`/`selector` | 悬停 |
| `drag` | `start_ref`, `end_ref` | 拖拽 |
| `select_option` | `ref`, `values_json` | 选择下拉选项 |
| `fill_form` | `fields_json` | 批量填表 |
| `file_upload` | `paths_json` | 文件上传 |
| `handle_dialog` | `accept`, `prompt_text` | 处理弹窗 |
| `resize` | `width`, `height` | 调整视口大小 |
| `wait_for` | `text`/`text_gone`, `wait_time` | 等待元素出现/消失 |

### 4.5 代码执行

| Action | 关键参数 | 说明 |
|--------|---------|------|
| `eval` | `code` | 执行 JS（简单） |
| `evaluate` | `code`, `ref` | 在元素上下文执行 JS |
| `run_code` | `code` | 执行 JS 并返回结果 |

### 4.6 标签页管理

| Action | 关键参数 | 说明 |
|--------|---------|------|
| `tabs` | `tab_action=list` | 列出所有标签页 |
| `tabs` | `tab_action=new` | 新建标签页 |
| `tabs` | `tab_action=close`, `index` | 关闭指定标签页 |
| `tabs` | `tab_action=select`, `index` | 切换到指定标签页 |

### 4.7 Cookie 与会话管理

| Action | 关键参数 | 说明 |
|--------|---------|------|
| `get_cookies` | `cookies_url`, `cookies_file` | 导出 cookie（可选过滤 URL、保存文件） |
| `set_cookies` | `cookies_json`/`cookies_file` | 注入 cookie（JSON 或文件） |
| `save_storage_state` | `cookies_file` | 保存完整状态（cookie + localStorage）到文件 |
| `load_storage_state` | `cookies_file` | 从文件恢复完整状态（重建 BrowserContext） |

---

## 五、文件清单

| 文件路径 | 说明 |
|----------|------|
| `src/copaw/agents/tools/browser_control.py` | 浏览器核心引擎：action 派发、Playwright 管理、状态隔离、会话持久化 |
| `src/copaw/agents/tools/browser_snapshot.py` | 页面无障碍树快照构建 |
| `src/copaw/agents/tools/__init__.py` | 导出公共函数 |
| `src/copaw/app/routers/browser_live_view.py` | Live View WebSocket router、Screencaster、CDP 管理 |
| `src/copaw/app/routers/__init__.py` | 注册 browser_live_view router |
| `src/copaw/agents/skills/browser_visible/SKILL.md` | 可见浏览器 Skill 定义 |
| `console/src/components/BrowserLiveView/index.tsx` | Live View React 组件（Tab Bar + 地址栏 + Canvas） |
| `console/src/components/BrowserLiveView/index.module.less` | 面板样式 |
| `console/src/pages/Chat/index.tsx` | Chat 页面集成（状态轮询 + 面板切换 + 拖拽调宽） |
| `console/src/api/modules/browser.ts` | 浏览器 API 模块 |
| `console/src/api/index.ts` | 导出 browserApi |

---

## 六、数据流总览

### 6.1 AI 操作流

```
用户消息 → Agent (ReActAgent)
  → browser_use(action="click", ref="E15")
  → _action_click() → page.click(selector)
  → _notify_lifecycle("navigated", ...)
  → browser_live_view.py 回调 → WS 广播给前端
  → 前端 canvas 显示更新画面
```

### 6.2 用户 Live View 交互流

```
用户在 canvas 上点击
  → getBoundingClientRect() → 归一化坐标
  → WS 发送 {"type": "mouse", "action": "click", "x": 0.45, "y": 0.32}
  → browser_live_view.py 接收
  → page.mouse.click(x * viewport.width, y * viewport.height)
  → touch_activity() 重置 idle 计时器
  → Screencaster 推送新帧 → 前端 canvas 更新
```

### 6.3 会话持久化流

```
登录成功 (cookie/storage 在 BrowserContext 中)
  → agent stop / idle 超时
  → _async_close_agent_context()             (全平台)
    → context.storage_state()          → cookie + localStorage
    → page.evaluate(sessionStorage JS) → sessionStorage
    → json.dump → ~/.copaw/browser_state/{agent_id}.json
  (注: 进程退出 atexit 走 sync 版本, 仅 Windows 可保存;
   macOS/Linux async Playwright 在 sync 中无法调用)

下次启动:
  → _ensure_browser() Phase 2
    → new_context(storage_state=path)  → cookie + localStorage 恢复
    → _pending_session_storage = [...]  → 暂存
  → _action_open(url)
    → page.goto(url)
    → _restore_session_storage(page)   → sessionStorage 注入
    → page.reload()                    → SPA 重新初始化
```

---

## 七、安全考量

| 方面 | 措施 |
|------|------|
| WS 认证 | 复用 AuthMiddleware，`?token=` query param 传递 Bearer token |
| Cookie 存储 | 文件权限跟随用户 home 目录，不上传到远程 |
| 输入注入 | 坐标归一化确保不会超出 viewport 范围 |
| 跨 agent 隔离 | BrowserContext 级别隔离，cookie/session 不互通 |
| Idle 超时 | 默认 30 分钟无操作自动关闭，释放资源 |

---

## 八、已知限制

| 限制 | 说明 | 可能的后续改进 |
|------|------|--------------|
| sessionStorage 跨 session 恢复需 reload | SPA 初始化时读 storage，注入后需 reload | 可考虑 hook page.goto 自动注入 |
| 内存变量无法跨 session 恢复 | JavaScript 运行时变量无法序列化 | 页面复用（navigate）可部分缓解 |
| Cookie 可能过期 | 保存时有效不代表加载时有效 | 可添加 token 刷新机制 |
| 强制 headless | Live View 阶段禁用 headed 模式 | 稳定后可考虑恢复 headed 选项 |
| WebKit 无 CDP | 只能用 screenshot 轮询，效率较低 | WebKit 无 CDP 替代方案 |

---

## 九、相关文档

| 文档 | 内容 |
|------|------|
| `docs/browser-live-view-changes.md` | Live View 实时面板实现详情 |
| `docs/browser-context-isolation-changes.md` | 多 Agent 上下文隔离实现详情 |
| `docs/browser-tab-management-changes.md` | 标签页管理与切换按钮实现详情 |
| `docs/browser-session-persistence-changes.md` | 会话持久化实现详情 |

# 多智能体浏览器上下文隔离 — 修改文档

> 本文档记录多智能体浏览器上下文隔离功能的所有代码变更，供合码 review 参考。

---

## 一、变更概览

此前 `browser_control.py` 使用一个进程级全局 `_state` 字典，所有 agent 共享同一个浏览器进程 **和** 同一个 `BrowserContext`。这导致：

- cookie/localStorage/session 在 agent 之间互相可见
- 一个 agent 执行 `stop` 会杀掉所有 agent 的浏览器
- 多 agent 场景下无法独立浏览

**本次变更**将全局状态拆分为两层：

```
_browser_state (共享)          _agent_contexts (per-agent)
┌─────────────────────┐       ┌──────────────────────────────────┐
│ playwright           │       │ "default" → AgentBrowserContext  │
│ browser              │       │ "agent_2" → AgentBrowserContext  │
│ headless             │       │ ...                              │
│ _idle_task           │       └──────────────────────────────────┘
│ _sync_browser        │
│ _sync_playwright     │       每个 AgentBrowserContext:
│ _last_browser_error  │         context, pages, refs, refs_frame,
└─────────────────────┘         console_logs, network_requests,
                                pending_dialogs, pending_file_choosers,
                                current_page_id, page_counter,
                                last_activity_time, _sync_context
```

- **共享**：所有 agent 使用同一个 Playwright 进程和浏览器实例（节省资源）
- **隔离**：每个 agent 拥有独立的 `BrowserContext`（隔离 cookie/session）
- **独立生命周期**：一个 agent 执行 `stop` 只关闭自己的 context，其他 agent 不受影响；最后一个 agent 关闭时才停止浏览器进程
- **前端感知**：Live View 面板、REST 状态端点、WebSocket 连接均按 `agent_id` 隔离

---

## 二、文件清单

| 操作 | 文件路径 | 改动量 | 说明 |
|:----:|----------|:------:|------|
| 修改 | `src/copaw/agents/tools/browser_control.py` | **大** | 核心重构：两层状态拆分 |
| 修改 | `src/copaw/agents/tools/__init__.py` | **小** | 导出新增函数 |
| 修改 | `src/copaw/app/routers/browser_live_view.py` | **中** | agent_id 感知 |
| 修改 | `console/src/api/modules/browser.ts` | **小** | agentId 参数 |
| 修改 | `console/src/components/BrowserLiveView/index.tsx` | **小** | agentId prop |
| 修改 | `console/src/pages/Chat/index.tsx` | **小** | 传入 agentId |

---

## 三、后端变更详情

### 3.1 `browser_control.py` — 核心重构

#### 3.1.1 新增 `AgentBrowserContext` dataclass

```python
from dataclasses import dataclass, field

@dataclass
class AgentBrowserContext:
    """Per-agent browser state: owns a Playwright BrowserContext."""
    agent_id: str
    context: Any = None
    pages: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, dict] = field(default_factory=dict)
    refs_frame: dict[str, Any] = field(default_factory=dict)
    console_logs: dict[str, list] = field(default_factory=dict)
    network_requests: dict[str, list] = field(default_factory=dict)
    pending_dialogs: dict[str, list] = field(default_factory=dict)
    pending_file_choosers: dict[str, list] = field(default_factory=dict)
    current_page_id: str | None = None
    page_counter: int = 0
    last_activity_time: float = 0.0
    _sync_context: Any = None
```

#### 3.1.2 全局变量替换

**删除**旧的 `_state` 字典，**替换为**：

```python
# 共享浏览器进程状态
_browser_state: dict[str, Any] = {
    "playwright": None, "browser": None, "headless": True,
    "_idle_task": None, "_last_browser_error": None,
    "_sync_browser": None, "_sync_playwright": None,
}

# Per-agent 上下文
_agent_contexts: dict[str, AgentBrowserContext] = {}
```

#### 3.1.3 新增辅助函数

| 函数 | 说明 |
|------|------|
| `_get_agent_id()` | 从 `ContextVar` 获取当前 agent ID（调用 `get_current_agent_id()`） |
| `_get_agent_ctx(agent_id="")` | 获取指定 agent 的 `AgentBrowserContext`，不存在返回 `None` |
| `_get_or_create_agent_ctx(agent_id="")` | 获取或创建 agent context |
| `_close_agent_context(agent_id)` | **同步**关闭指定 agent 的 context 和所有 page |
| `_async_close_agent_context(agent_id)` | **异步**版本 |
| `_stop_browser_process()` | **同步**关闭共享浏览器进程和 Playwright |
| `_async_stop_browser_process()` | **异步**版本 |

#### 3.1.4 公共 API 签名变更

所有公共函数新增可选 `agent_id` 参数，不传时自动获取当前 agent：

| 函数 | 变更 |
|------|------|
| `get_browser_state_summary(agent_id="")` | 返回特定 agent 的状态，response 新增 `agent_id` 字段 |
| `get_page(page_id="", agent_id="")` | 获取特定 agent 的页面 |
| `touch_activity(agent_id="")` | 重置特定 agent 的空闲计时器 |
| `is_browser_running()` | **不变**，检查共享浏览器进程 |
| `is_agent_browser_active(agent_id="")` | **新增**，检查 agent 是否有活跃 context |

#### 3.1.5 `_sync_browser_launch()` 变更

返回值从 `(pw, browser, context)` 改为 `(pw, browser)`。不再创建 `BrowserContext`——context 创建移至 `_ensure_browser()` 的第二阶段。

#### 3.1.6 `_ensure_browser()` — 两阶段逻辑

```
Phase 1: 确保共享浏览器进程在运行
  ├── 检查 _browser_state 中 browser 是否存在
  ├── 不存在 → 启动 Playwright + launch browser
  └── 写入 _browser_state

Phase 2: 确保当前 agent 有 BrowserContext
  ├── 检查 ctx.context / ctx._sync_context 是否存在
  ├── 不存在 → browser.new_context()
  ├── _attach_context_listeners(context, ctx)
  └── 写入 ctx
```

#### 3.1.7 `_action_start()` 简化

- 删除了重复的浏览器启动代码，改为统一调用 `_ensure_browser()`
- 检查当前 agent 是否已有 context，有则返回 "already running"
- 生命周期通知携带 `agent_id`

#### 3.1.8 `_action_stop()` — 分步关闭

```
Step 1: _async_close_agent_context(agent_id)  → 关闭当前 agent 的 context
Step 2: _notify_lifecycle("stopped", agent_id=agent_id)
Step 3: if not _agent_contexts → _cancel_idle_watchdog() + _async_stop_browser_process()
```

关键变化：**只有当所有 agent 都关闭后，才停止共享浏览器进程**。

#### 3.1.9 所有 `_action_*` 函数的机械替换

统一模式：

| 旧 | 新 |
|----|-----|
| `_state["pages"]` | `ctx.pages` |
| `_state["refs"]` | `ctx.refs` |
| `_state["context"]` | `ctx.context` |
| `_state["current_page_id"]` | `ctx.current_page_id` |
| `_state["console_logs"]` | `ctx.console_logs` |
| `_state["network_requests"]` | `ctx.network_requests` |
| `_state["pending_dialogs"]` | `ctx.pending_dialogs` |
| `_state["pending_file_choosers"]` | `ctx.pending_file_choosers` |
| `_state["page_counter"]` | `ctx.page_counter` |
| `_state["headless"]` | `_browser_state["headless"]` |
| `_state["browser"]` | `_browser_state["browser"]` |
| `_state["_idle_task"]` | `_browser_state["_idle_task"]` |

每个函数通过 `ctx = _get_agent_ctx()` 获取当前 agent 的状态。

#### 3.1.10 监听器闭包捕获 `ctx`

`_attach_context_listeners(context, ctx)` 和 `_attach_page_listeners(page, page_id, ctx)` 的签名新增 `ctx` 参数。闭包直接捕获 `ctx` 实例，而非运行时动态查找 ContextVar——因为回调异步触发时 ContextVar 可能已变。

```python
def _attach_page_listeners(page, page_id: str, ctx: AgentBrowserContext) -> None:
    logs = ctx.console_logs.setdefault(page_id, [])
    def on_console(msg):
        logs.append(...)  # 捕获的是 ctx 实例上的 logs 引用
    page.on("console", on_console)
    # ...
```

#### 3.1.11 `_next_page_id(ctx)` 变更

签名从 `_next_page_id()` 改为 `_next_page_id(ctx: AgentBrowserContext)`，使用 agent 自己的 `page_counter`，避免跨 agent 计数器冲突。

#### 3.1.12 Idle Watchdog — 全局巡检

从「全局超时 → 停止整个浏览器」改为「逐个检查 agent → 关闭超时 agent 的 context → 全空时停止浏览器」：

```python
async def _idle_watchdog(idle_seconds=1800.0):
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        expired = [aid for aid, ctx in _agent_contexts.items()
                   if (now - ctx.last_activity_time) >= idle_seconds]
        for aid in expired:
            await _async_close_agent_context(aid)
            await _notify_lifecycle("stopped", agent_id=aid)
        if not _agent_contexts:
            await _async_stop_browser_process()
            return
```

#### 3.1.13 `_atexit_cleanup()` 变更

同步清理所有 agent context，然后停止浏览器进程：

```python
def _atexit_cleanup():
    if not _is_browser_running():
        return
    for aid in list(_agent_contexts.keys()):
        _close_agent_context(aid)
    _stop_browser_process()
```

#### 3.1.14 `_notify_lifecycle()` 变更

所有调用点新增 `agent_id=` 关键字参数，使 `browser_live_view.py` 的回调能区分事件来源。

---

### 3.2 `tools/__init__.py` — 导出

新增导入和导出 `is_agent_browser_active`：

```python
from .browser_control import (
    # ... 原有 ...
    is_agent_browser_active,
)

__all__ = [
    # ... 原有 ...
    "is_agent_browser_active",
]
```

---

### 3.3 `browser_live_view.py` — agent_id 感知

#### 3.3.1 WS 客户端按 agent_id 分组

```python
# 旧
_ws_clients: set[WebSocket] = set()

# 新
_ws_clients: dict[str, set[WebSocket]] = {}
```

新增辅助函数：

| 函数 | 说明 |
|------|------|
| `_has_any_clients()` | 是否有任何 agent 有 WS 客户端 |
| `_get_clients(agent_id)` | 获取指定 agent 的客户端集合 |
| `_broadcast_to_agent(agent_id, text, data)` | 向指定 agent 的所有客户端广播 |

#### 3.3.2 REST 端点

```python
@router.get("/status")
async def browser_status(agent_id: str = Query("default")):
    return get_browser_state_summary(agent_id=agent_id)
```

#### 3.3.3 WebSocket 端点

```python
@router.websocket("/ws")
async def browser_ws(
    websocket: WebSocket,
    agent_id: str = Query("default"),
):
```

- 连接时按 `agent_id` 加入对应客户端集合
- 断开时清理空集合
- 输入事件转发带 `agent_id`

#### 3.3.4 Screencaster 循环

从「对单个 page 截图」改为「遍历所有有 WS 客户端的 agent_id，分别截图并广播」：

```python
async def _screencaster_loop():
    while _has_any_clients():
        for agent_id in list(_ws_clients.keys()):
            clients = _ws_clients.get(agent_id)
            if not clients:
                continue
            page = get_page(agent_id=agent_id)
            if page is None:
                continue
            # 截图 + 广播给该 agent 的客户端
            jpeg_bytes = await page.screenshot(type="jpeg", quality=65)
            await _broadcast_to_agent(agent_id, text=metadata, data=jpeg_bytes)
        await asyncio.sleep(0.2)
```

#### 3.3.5 生命周期回调

```python
async def _on_browser_lifecycle(event, **kwargs):
    agent_id = kwargs.get("agent_id", "default")
    # 只广播给对应 agent_id 的 WS 客户端
    await _broadcast_to_agent(agent_id, text=msg)
```

#### 3.3.6 输入处理

`_handle_mouse(data, agent_id)`、`_handle_keyboard(data, agent_id)`、`_handle_navigate(data, agent_id)` 均新增 `agent_id` 参数：

- `get_page(agent_id=agent_id)` 获取对应 agent 的页面
- `touch_activity(agent_id=agent_id)` 重置对应 agent 的 idle 计时器

---

## 四、前端变更详情

### 4.1 `api/modules/browser.ts` — agentId 参数

```typescript
export interface BrowserStatus {
  // ... 原有 ...
  agent_id: string;  // 新增
}

export const browserApi = {
  getStatus: (agentId?: string) =>
    request<BrowserStatus>(
      `/browser/status${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`,
    ),
};
```

---

### 4.2 `BrowserLiveView/index.tsx` — agentId prop

**Props 变更：**

```typescript
interface BrowserLiveViewProps {
  onClose?: () => void;
  agentId?: string;  // 新增，默认 "default"
}
```

**WebSocket URL 变更：**

```typescript
// 旧
`?token=${encodeURIComponent(getApiToken())}`

// 新
`?token=${encodeURIComponent(getApiToken())}&agent_id=${encodeURIComponent(agentId)}`
```

**useEffect 依赖变更：**

```typescript
// 旧
}, []);

// 新：agentId 变化时重新连接 WS
}, [agentId]);
```

---

### 4.3 `Chat/index.tsx` — 传入 agentId

**浏览器状态轮询：**

```typescript
// 旧
const res = await browserApi.getStatus();

// 新：传入当前选中的 agent
const res = await browserApi.getStatus(selectedAgent);
```

`useEffect` 依赖从 `[]` 改为 `[selectedAgent]`，切换 agent 时重新轮询。

**BrowserLiveView 组件：**

```tsx
// 旧
<BrowserLiveView onClose={...} />

// 新
<BrowserLiveView agentId={selectedAgent} onClose={...} />
```

---

## 五、关键设计决策

| 决策点 | 选择 | 原因 |
|--------|------|------|
| 隔离粒度 | `BrowserContext` 级别 | Playwright BrowserContext 天然隔离 cookie/localStorage/session，且共享浏览器进程节省资源 |
| Agent ID 来源 | `ContextVar` + `get_current_agent_id()` | 复用已有的 agent context 系统，工具函数内无需传参 |
| 闭包捕获 `ctx` | 函数参数传入 | 事件回调异步触发时 ContextVar 可能已变，必须在注册时捕获实例 |
| Idle 超时策略 | 逐 agent 检查 | 避免一个活跃 agent 延长另一个 agent 的超时 |
| 浏览器进程关闭时机 | 最后一个 agent context 关闭时 | 过早关闭会影响其他 agent |
| WS 按 agent_id 分组 | `dict[str, set[WebSocket]]` | 每个 agent 只收到自己的帧，避免混淆 |
| 公共 API `agent_id` 默认值 | `""` → 自动获取 | 向后兼容，单 agent 场景无需改调用方 |

---

## 六、边界情况

| 场景 | 处理方式 |
|------|----------|
| 单 agent 场景 | `_get_agent_id()` 返回 `"default"`，行为完全不变 |
| Agent A stop，Agent B 仍在浏览 | 只关闭 A 的 context，B 不受影响，浏览器进程继续运行 |
| 所有 agent 都 stop | 最后一个 agent 关闭后，停止共享浏览器进程 |
| Agent A idle 超时 | 只关闭 A 的 context，B 不受影响 |
| 所有 agent 都 idle 超时 | 全部关闭后停止浏览器进程 |
| 前端切换 agent | `useEffect` 依赖 `agentId`，自动重连 WS 到新 agent |
| 前端轮询状态 | `getStatus(selectedAgent)` 带上 agent_id，返回对应 agent 的状态 |
| 多个前端 tab 看同一 agent | 都加入同一 `agent_id` 的 WS 客户端集合，收到相同帧 |
| 多个前端 tab 看不同 agent | 各自加入各自的 `agent_id` 集合，互不干扰 |
| atexit 清理 | 同步关闭所有 agent context，然后停止浏览器进程 |
| `_attach_*_listeners` 闭包 | 捕获 `ctx` 实例而非动态查找，确保回调写入正确的 agent 状态 |

---

## 七、向后兼容

| 方面 | 兼容性 |
|------|--------|
| 单 agent 用户 | 完全兼容，`agent_id` 默认 `"default"` |
| `browser_use` 工具接口 | 无变化，action/参数签名不变 |
| 前端不传 `agent_id` | REST 和 WS 端点默认 `"default"` |
| 生命周期回调 | 新增 `agent_id` 关键字参数，旧回调通过 `**kwargs` 接收不会报错 |
| 公共函数签名 | 新增可选 `agent_id` 参数，不传时行为与旧版一致 |

---

## 八、验证步骤

1. **单 agent 回归**：`copaw app` → Chat → 让 AI 执行 `browser_use` start/open/snapshot/stop，行为和之前一致
2. **多 agent 隔离**：配置两个 agent → 分别让它们 `browser_use start` → 确认各自有独立 context（访问不同网站，cookie 不互通）
3. **Stop 隔离**：Agent A stop → Agent B 浏览器仍在运行
4. **Idle 超时**：Agent A 空闲超时 → Agent A context 关闭，Agent B 不受影响
5. **Live View**：在前端切换 agent → 面板显示对应 agent 的浏览器画面
6. **状态轮询**：切换 agent 后，`/browser/status?agent_id=xxx` 返回正确状态
7. **运行已有测试**：`pytest tests/unit/` 确保无回归（98 passed，1 pre-existing failure）

---

## 九、无改动说明

- 无数据库/配置/迁移变更
- 无新依赖引入
- 无破坏性变更，所有新增代码在未使用多 agent 时为空操作
- 未修改任何测试文件（建议后续补充多 agent context 隔离的单元测试）

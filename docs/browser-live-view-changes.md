# Browser Live-View 实时浏览器面板 — 修改文档

> 本文档记录 Browser Live-View 功能的所有代码变更，供合码 review 参考。

---

## 一、变更概览

在 Chat 页面右侧新增可交互的浏览器实时面板，用户可以：
- 实时看到 AI 的 `browser_use` 操作画面（~5fps JPEG 截图流）
- 直接用鼠标点击、滚轮、键盘输入来干预浏览器
- 通过 URL 栏导航、后退、刷新
- 面板随浏览器启停自动出现/隐藏，宽度可拖拽调整

通信方式：WebSocket 双向通信（server→client 帧数据，client→server 用户输入）。

---

## 二、文件清单

| 操作 | 文件路径 | 说明 |
|:----:|----------|------|
| 修改 | `src/copaw/agents/tools/browser_control.py` | 添加生命周期回调 + 公共访问函数 |
| 修改 | `src/copaw/agents/tools/__init__.py` | 导出新增的公共函数 |
| **新建** | `src/copaw/app/routers/browser_live_view.py` | WebSocket router + status REST 端点 |
| 修改 | `src/copaw/app/routers/__init__.py` | 注册 browser_live_view router |
| **新建** | `console/src/components/BrowserLiveView/index.tsx` | 浏览器面板 React 组件 |
| **新建** | `console/src/components/BrowserLiveView/index.module.less` | 面板样式 |
| 修改 | `console/src/pages/Chat/index.tsx` | Chat 页面集成面板 + 状态轮询 + 拖拽调宽 |
| **新建** | `console/src/api/modules/browser.ts` | 浏览器 API 模块 |
| 修改 | `console/src/api/index.ts` | 导出 browserApi |

---

## 三、后端变更详情

### 3.1 `browser_control.py` — 生命周期回调 + 公共 API

**新增模块级变量和函数（约 80 行）：**

```python
# 回调注册系统
_lifecycle_callbacks: list[Callable] = []
register_browser_lifecycle_callback(cb)   # 注册回调
unregister_browser_lifecycle_callback(cb)  # 注销回调
_notify_lifecycle(event, **kwargs)         # 触发回调（async）

# 公共访问函数（供 router 使用，不暴露 _state）
get_browser_state_summary() -> dict  # {running, headless, current_page_id, url, viewport}
is_browser_running() -> bool         # 公开版 _is_browser_running
get_page(page_id="")                 # 公开版 _get_page，空 page_id 取 current
touch_activity()                     # 公开版，重置 idle 计时器
```

**在关键位置插入回调通知（3 处）：**

| 位置 | 事件 | 参数 |
|------|------|------|
| `_action_start()` 成功后（`_start_idle_watchdog()` 之后） | `"started"` | 无 |
| `_action_stop()` 执行清理前 | `"stopped"` | 无 |
| `_action_open()` 成功后 | `"navigated"` | `url=, page_id=` |
| `_action_navigate()` 成功后 | `"navigated"` | `url=, page_id=` |

**其他：** `typing` 导入中增加了 `Callable`。

---

### 3.2 `tools/__init__.py` — 导出

在 `from .browser_control import` 块中新增 6 个符号：

```python
get_browser_state_summary, get_page, is_browser_running,
register_browser_lifecycle_callback, touch_activity,
unregister_browser_lifecycle_callback
```

同步更新 `__all__` 列表。

---

### 3.3 `browser_live_view.py` — 新建 WebSocket Router

**路由前缀：** `/browser`（最终挂载在 `/api/browser/`）

#### REST 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/browser/status` | 返回 `get_browser_state_summary()` JSON |

#### WebSocket 端点

| 路径 | 说明 |
|------|------|
| `/browser/ws` | 双向实时通信 |

**认证方式：** 复用现有 `AuthMiddleware._extract_token()`，WS 连接通过 `?token=` query param 传递 Bearer token。

#### Server → Client 消息

```jsonc
// 文本帧：会话状态（连接时 + 生命周期变化时）
{"type": "session", "status": "started"|"stopped", "viewport": {...}, "url": "..."}

// 文本帧：导航事件
{"type": "navigation", "url": "...", "page_id": "..."}

// 文本帧 + 二进制帧：画面帧
{"type": "frame", "ts": 1711234567890, "w": 1280, "h": 720}
// 紧跟一个 binary WebSocket frame (JPEG bytes)
```

#### Client → Server 消息

```jsonc
// 鼠标（坐标归一化 0-1）
{"type": "mouse", "action": "click"|"dblclick"|"move"|"wheel", "x": 0.45, "y": 0.32, "button": "left", "deltaY": -120}

// 键盘
{"type": "keyboard", "action": "press"|"type", "key": "Enter", "text": "hello"}

// 导航
{"type": "navigate", "url": "https://example.com"}
{"type": "navigate_back"}
{"type": "reload"}
```

#### 核心机制

- **Screencaster 循环**：有 WS 客户端时启动，无客户端时自动取消。`page.screenshot(type="jpeg", quality=65)`，间隔 0.2s (~5fps)。
- **输入处理**：归一化坐标 × viewport 尺寸 = 实际像素坐标，调用 `page.mouse.*` / `page.keyboard.*`。
- **生命周期回调**：import 时通过 `register_browser_lifecycle_callback` 注册，浏览器 start/stop/navigate 时广播给所有 WS 客户端。
- **Sync/Async 兼容**：所有 Playwright 调用检查 `_USE_SYNC_PLAYWRIGHT`，sync 模式走 `run_in_executor`。
- **Idle 计时器**：用户通过面板交互时调用 `touch_activity()` 重置 idle 超时。

---

### 3.4 `routers/__init__.py` — 注册

```python
from .browser_live_view import router as browser_live_view_router
# ...
router.include_router(browser_live_view_router)
```

---

## 四、前端变更详情

### 4.1 `BrowserLiveView/index.tsx` — 新建组件

**组件结构：**

```
BrowserLiveView
├── Toolbar (URL Input + Back/Reload/Close 按钮)
├── Canvas Container
│   └── <canvas> (渲染 JPEG + 捕获鼠标/键盘)
└── Status Bar (连接状态 dot + 当前 URL)
```

**核心逻辑：**

| 功能 | 实现 |
|------|------|
| WS 连接 | `ws(s)://{host}/api/browser/ws?token=...`，`binaryType="arraybuffer"`，断线 2s 自动重连 |
| 帧渲染 | 收到 frame metadata 后等待下一个 binary 消息 → Blob → ObjectURL → Image → `canvas.drawImage()` |
| 鼠标事件 | `onClick/onDoubleClick/onWheel` → `getBoundingClientRect()` 归一化坐标 → 发 JSON |
| 键盘事件 | canvas `tabIndex={0}` 可聚焦，`onKeyDown` 捕获，单字符 → type，修饰键/特殊键 → press |
| URL 栏 | 显示来自 `navigation` 事件的 URL，输入 + Enter → 发 `navigate` 命令，自动补 `https://` |

**Props：**

```typescript
interface BrowserLiveViewProps {
  onClose?: () => void;  // 关闭面板回调
}
```

---

### 4.2 `BrowserLiveView/index.module.less` — 新建样式

使用 CSS 变量（`--ant-color-*`）适配 antd 主题和暗色模式。主要样式类：

| 类名 | 说明 |
|------|------|
| `.container` | 全高 flex column，左边框分隔 |
| `.toolbar` | URL 栏 + 导航按钮行 |
| `.canvasWrapper` | 黑底居中容器 |
| `.canvas` | `max-width/height: 100%`，crosshair 光标 |
| `.statusBar` | 底部状态栏 |
| `.statusDot*` | 绿/红连接状态圆点 |

---

### 4.3 `Chat/index.tsx` — 集成面板

**新增 import：**

```tsx
import BrowserLiveView from "../../components/BrowserLiveView";
import { browserApi } from "../../api/modules/browser";
```

**新增状态：**

```tsx
const [browserOpen, setBrowserOpen] = useState(false);   // 面板可见
const [panelWidth, setPanelWidth] = useState(480);        // 面板宽度 px
const resizingRef = useRef(false);                        // 拖拽中标记
```

**新增 Effects：**

1. **浏览器状态轮询**（每 2 秒）：调用 `browserApi.getStatus()`，根据 `running` 自动 show/hide 面板。
2. **拖拽调宽 handler**：`startResize` 监听 `mousemove/mouseup`，最小宽度 360px。

**布局变更：**

原布局 `flexDirection: "column"` 改为 `"row"`：

```
Before:                      After:
┌──────────────┐             ┌────────────┬──┬──────────┐
│              │             │            │拖│ Browser  │
│   Chat UI    │             │  Chat UI   │拽│ LiveView │
│              │             │            │条│          │
└──────────────┘             └────────────┴──┴──────────┘
```

Chat UI 用 `flex: 1; minWidth: 0` 自适应剩余空间。Browser 面板 `width: panelWidth; flexShrink: 0`。中间 4px 宽的 resize handle（`cursor: col-resize`）。

---

### 4.4 `api/modules/browser.ts` — 新建 API 模块

```typescript
export interface BrowserStatus {
  running: boolean;
  headless: boolean;
  current_page_id: string | null;
  url: string;
  viewport: { width: number; height: number };
}

export const browserApi = {
  getStatus: () => request<BrowserStatus>("/browser/status"),
};
```

### 4.5 `api/index.ts` — 导出

```typescript
import { browserApi } from "./modules/browser";
// ...
export const api = {
  // ...
  ...browserApi,
};
export { browserApi };
```

---

## 五、关键设计决策

| 决策点 | 选择 | 原因 |
|--------|------|------|
| 帧传输 | CDP Screencast 优先 + `page.screenshot()` 兜底 | Chromium 用事件驱动推帧（按需、省资源），WebKit 不支持 CDP 时回退周期截图 |
| 通信协议 | WebSocket | 需双向通信（帧 + 用户输入），SSE 只能单向 |
| 坐标系统 | 归一化 0-1 | 前端 canvas 尺寸 ≠ 浏览器 viewport，归一化后后端乘实际尺寸 |
| 面板显隐 | 轮询 `/browser/status` | 简单可靠，2s 间隔开销极小 |
| 认证 | 复用 AuthMiddleware | WS 连接通过 `?token=` query param，已有中间件支持 |
| JPEG 质量 | quality=90 | 原 quality=65 文字模糊，90 接近无损且 CDP 按需推帧不怕带宽 |
| 浏览器引擎检测 | `_browser_state["browser_kind"]` | 启动时记录 `"chromium"` / `"webkit"`，供 live-view 决定采集模式 |
| Tab 变更检测 | 指纹比较（page_id + active） | 避免 title/url 加载中变化导致常量刷新或漏检 |

---

## 六、边界情况

| 场景 | 处理方式 |
|------|----------|
| 浏览器未启动时连接 WS | 发送 `{"type":"session","status":"stopped"}`，保持连接等待 start |
| 浏览器中途崩溃 | screencaster 捕获异常，继续循环等待恢复 |
| 多个前端 tab 同时打开 | 所有 WS 客户端都收到帧广播 |
| AI 和人同时操作 | Playwright 串行化所有输入，last-writer-wins |
| WebKit 回退（macOS） | `page.screenshot()` 全引擎通用 |
| Sync 模式（Windows） | 所有 Playwright 调用走 `run_in_executor` |
| 新标签页打开 | `_attach_context_listeners` 已有 `on_page` 回调，screencaster 跟随 `current_page_id`；tab 栏实时更新 |
| CDP screencast 页面切换 | screencaster 循环检测 `current_page_id` 变化，自动 stop + restart CDP session |
| CDP 启动失败 | 自动降级到 `page.screenshot()` 模式，不影响使用 |
| Sync Playwright + Chromium | 不使用 CDP（避免线程桥接复杂度），直接走 screenshot 模式 quality=90 |
| WS 断线 | 前端 2s 自动重连 |
| Idle 超时 | 用户面板交互也会 `touch_activity()` 重置计时器 |

---

## 七、验证步骤

1. `copaw app` 启动服务
2. 打开 Console → Chat 页面
3. 发消息让 AI 执行 `browser_use` start + open 操作
4. 验证右侧面板自动弹出，显示浏览器实时画面
5. AI 执行 click/type 操作时，面板实时反映变化
6. 在面板中手动点击链接、输入文字，验证交互正常
7. AI 执行 stop 后，面板自动关闭
8. 手动拖拽调整面板宽度，验证最小 360px 限制
9. 关闭浏览器 tab 后重开 Chat，验证 WS 自动重连

---

## 八、强制 Headless 模式（测试阶段）

> Live View 面板已替代可见浏览器窗口的功能，因此浏览器始终以 headless 模式运行，`headed=true` 参数被忽略。

### 8.1 `browser_control.py` — 忽略 headed 参数

在 `_action_start()` 入口处（browser_exists 判断前）强制覆盖：

```python
# Force headless — Live View panel replaces visible window.
headed = False
```

**影响：**
- `headed=True` 传入后被立即置为 `False`，下游 `_state["headless"] = not headed` 始终为 `True`
- `if headed and current_headless` 的重启分支永远不会触发（已是 headless，无需重启）
- sync 模式同理，`_state["_sync_headless"]` 也始终为 `True`

### 8.2 `browser_visible` Skill — 更新描述

`src/copaw/agents/skills/browser_visible/SKILL.md` 全文重写：

| 变更 | 说明 |
|------|------|
| description | 移除「打开真实浏览器窗口」描述，改为「通过 Live View 面板实时展示」 |
| 使用方式 | `{"action": "start"}` 不再需要 `headed: true` |
| 新增内容 | Live View 面板功能说明表格（实时画面、鼠标/键盘交互、URL 导航、自动显隐） |
| 注意事项 | 明确说明 `headed: true` 会被忽略 |

### 8.3 后续计划

- 当 Live View 功能稳定后，可移除 `browser_control.py` 中的强制覆盖（搜索 `# Force headless`）
- 届时可考虑保留 `headed=true` 作为调试选项，或彻底移除该参数

---

## 九、无改动说明

- 无数据库/配置/迁移变更
- 无新依赖引入（Playwright 和 FastAPI WebSocket 均为已有依赖）
- 无破坏性变更，所有新增代码在浏览器未启动时为空操作
- 未修改任何测试文件（建议后续补充 WS router 的单元测试）

---

## 十、CDP Screencast 双模帧采集

> 替换原有的纯 `page.screenshot()` 轮询方案，改为 Chromium 优先使用 CDP 事件驱动推帧，WebKit 自动降级到周期截图。

### 10.1 背景与动机

原方案每 0.2s 调用 `page.screenshot(type="jpeg", quality=65)` 无差别截图：

| 问题 | 影响 |
|------|------|
| 页面静止时仍持续截图 | CPU + 带宽浪费（~150-400 KB/s） |
| JPEG quality=65 | 文字边缘模糊，清晰度不足 |
| 全引擎通用但低效 | 未利用 Chromium 原生能力 |

CDP `Page.startScreencast` 是 Chromium 内建的事件驱动帧推送：
- 页面内容不变时零开销
- 内建 ACK 流控，前端处理不过来时自动降帧
- 支持 `maxWidth`/`maxHeight`/`quality` 参数

### 10.2 `browser_control.py` — 浏览器引擎类型追踪

**新增 `_browser_state["browser_kind"]` 字段：**

| 位置 | 值 | 时机 |
|------|----|------|
| `_sync_browser_launch()` | `"chromium"` 或 `"webkit"` | 函数返回时（返回值从 `(pw, browser)` 改为 `(pw, browser, kind)`） |
| `_ensure_browser()` async 分支 | `"chromium"` 或 `"webkit"` | 浏览器启动成功后 |
| `_stop_browser_process()` | `None` | 浏览器关闭时重置 |
| `_async_stop_browser_process()` | `None` | 同上（async 版本） |

**新增公共函数：**

```python
def get_browser_kind() -> str | None:
    """返回当前浏览器引擎类型: 'chromium', 'webkit', 或 None"""

async def get_browser_tabs(agent_id: str = "") -> list[dict]:
    """返回指定 agent 的所有打开标签页列表
    每项: {page_id, url, title, active}
    async 函数，正确处理 sync/async Playwright 的 page.title() 调用"""

def set_current_page(page_id: str, agent_id: str = "") -> bool:
    """切换指定 agent 的活动页面，返回是否成功"""
```

### 10.3 `browser_live_view.py` — 双模采集核心逻辑

#### 模式判定

```python
def _should_use_cdp() -> bool:
    return not _USE_SYNC_PLAYWRIGHT and get_browser_kind() == "chromium"
```

| 条件 | 模式 | 原因 |
|------|------|------|
| Chromium + async | CDP Screencast | 最优效率 |
| WebKit（任意模式） | screenshot 轮询 | CDP 仅限 Chromium |
| Sync Playwright（Windows） | screenshot 轮询 | 避免线程桥接复杂度 |
| CDP 启动失败 | 自动降级 screenshot | 容错 |

#### CDP Screencast 生命周期

```
WS 客户端连接
  → _ensure_screencaster()
    → _screencaster_loop() 检测到 Chromium
      → _start_cdp_screencast(agent_id)
        → page.context.new_cdp_session(page)
        → cdp.send("Page.startScreencast", {format, quality, maxWidth, maxHeight})
        → cdp.on("Page.screencastFrame", handler)

CDP 帧到达时：
  handler:
    → base64 解码 JPEG → 广播 metadata JSON + binary 给 WS 客户端
    → cdp.send("Page.screencastFrameAck") 确认

页面切换（新标签页）：
  → _handle_cdp_agent() 检测 page 对象变化
    → _stop_cdp_screencast()  → 停止旧 session
    → _start_cdp_screencast() → 绑定新 page

所有客户端断开 / 浏览器停止：
  → _stop_all_cdp_screencasts()
    → cdp.send("Page.stopScreencast")
    → cdp.detach()
```

#### 模块级状态

```python
_cdp_sessions: dict[str, Any] = {}   # agent_id → CDPSession
_cdp_pages: dict[str, Any] = {}      # agent_id → 启动 CDP 时的 page 对象
_JPEG_QUALITY = 90                    # 替代原 quality=65
```

#### 截图降级路径（WebKit / sync）

逻辑与原方案一致，仅 quality 从 65 提升到 90：

```python
jpeg_bytes = await page.screenshot(type="jpeg", quality=_JPEG_QUALITY)
```

### 10.4 前后端帧协议不变

CDP 模式输出的帧格式与 screenshot 模式完全一致：

```
Server → Client:
  text:   {"type": "frame", "ts": 1711234567890, "w": 1280, "h": 720}
  binary: <JPEG bytes>
```

**前端无需任何修改即可兼容双模切换。**

---

## 十一、Chrome 风格多标签页（Tab Bar）

> 在 Live View 面板顶部新增 Chrome 风格标签栏，实时显示所有打开的浏览器页面，支持点击切换。

### 11.1 背景与动机

原实现中，AI 先后打开多个页面时：
- Live View 面板只显示 `current_page_id` 对应的页面
- 切换页面后画面不更新，需刷新才能看到新页面
- 用户无法感知浏览器中有多少标签页打开

### 11.2 后端变更

#### 新增 REST 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/browser/tabs` | 返回 `get_browser_tabs()` 的标签页列表 JSON |

#### WebSocket 新增消息类型

**Server → Client：**

```jsonc
// 标签页列表更新（结构变化时推送）
{"type": "tabs", "tabs": [
  {"page_id": "page_1", "url": "https://...", "title": "Page Title", "active": true},
  {"page_id": "page_2", "url": "https://...", "title": "Other Page", "active": false}
]}
```

**Client → Server：**

```jsonc
// 切换标签页
{"type": "switch_tab", "page_id": "page_2"}
```

#### 标签页广播机制

```python
_last_tab_snapshots: dict[str, str] = {}  # agent_id → 指纹字符串

def _tab_fingerprint(tabs: list[dict]) -> str:
    """只比较 page_id + active 状态，不含 title/url"""
    return "|".join(f"{t['page_id']}:{'A' if t['active'] else '-'}" for t in tabs)

async def _broadcast_tabs(agent_id: str, force: bool = False) -> None:
    """指纹变化时或 force=True 时发送 tabs 消息"""
```

**广播触发点：**

| 触发时机 | force | 说明 |
|----------|-------|------|
| 生命周期回调（started/stopped/navigated） | `True` | 确定性状态变化，立即推送 |
| screencaster 循环（每帧迭代后） | `False` | 兜底检测，仅指纹变化时发送 |
| WS 客户端连接时 | — | 直接发送当前 tabs 列表 |
| `switch_tab` 处理后 | `True` | 用户切换标签，立即广播 |

#### `switch_tab` 处理流程

```python
async def _handle_switch_tab(data, agent_id):
    page_id = data.get("page_id")
    set_current_page(page_id, agent_id)  # 更新 current_page_id
    # 如果 CDP 活跃，重启 session 绑定新页面
    if _is_cdp_active(agent_id):
        await _stop_cdp_screencast(agent_id)
        await _start_cdp_screencast(agent_id)
    await _broadcast_tabs(agent_id, force=True)
```

### 11.3 前端变更

#### 组件结构变更

```
BrowserLiveView（变更后）
├── Tab Bar（新增）
│   ├── Traffic Lights（红/黄/绿装饰圆点）
│   └── Tab List（可横向滚动）
│       └── Tab × N（点击切换活动页）
├── Toolbar（地址栏 — 原有）
│   ├── Back / Forward / Reload 按钮
│   ├── URL Input（锁图标 + 输入框）
│   └── Close 按钮
└── Canvas Container（原有）
```

#### 新增状态

```tsx
const [tabs, setTabs] = useState<TabInfo[]>([]);

interface TabInfo {
  page_id: string;
  url: string;
  title: string;
  active: boolean;
}
```

#### WS 消息处理新增

```tsx
} else if (type === "tabs") {
  const tabList = msg.tabs as TabInfo[];
  setTabs(tabList);
  // 从活动 tab 同步 URL
  const active = tabList.find(t => t.active);
  if (active?.url) {
    setUrl(active.url);
    setInputUrl(active.url);
  }
}
```

#### Tab 切换

```tsx
function handleTabClick(pageId: string) {
  sendMessage({ type: "switch_tab", page_id: pageId });
}
```

### 11.4 Tab Bar 样式（`index.module.less`）

仿 Chrome 标签栏设计：

| 元素 | 样式 |
|------|------|
| `.tabBar` | 高 38px，灰底（`--ant-color-bg-layout`），flex 布局 |
| `.trafficLights` | 左侧红/黄/绿 10px 圆点（纯装饰，不可点击） |
| `.tabList` | 横向滚动，隐藏滚动条，底部对齐 |
| `.tab` | 圆角顶部（`8px 8px 0 0`），最大宽度 180px，hover 浅灰背景 |
| `.tabActive` | 白底突出（`--ant-color-bg-container`），与地址栏视觉连续 |
| `.tabTitle` | 12px 字号，超长省略（`text-overflow: ellipsis`） |
| `.toolbar`（调整） | 背景改为白底，URL 输入框改为灰底圆角，与标签栏形成层次分隔 |

---

## 十二、技术要点备忘

### `page.title()` 的 sync/async 差异

Playwright 的 `page.title()` 在 async API 下返回 coroutine，必须 `await`：

```python
# 错误（返回 coroutine 对象，不是字符串）
title = page.title()

# 正确
if _USE_SYNC_PLAYWRIGHT:
    title = page.title()      # sync API: 直接返回字符串
else:
    title = await page.title() # async API: 需要 await
```

因此 `get_browser_tabs()` 必须声明为 `async def`。

### CDP Screencast 回调模型

CDP `screencastFrame` 事件回调在 Playwright 的 async 模式下由事件循环触发。回调内部使用 `asyncio.ensure_future()` 调度广播和 ACK，避免阻塞事件处理：

```python
def _on_frame(params: dict) -> None:
    jpeg_bytes = base64.b64decode(params["data"])
    asyncio.ensure_future(_broadcast_to_agent(agent_id, text=metadata, data=jpeg_bytes))
    asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))
```

### Tab 指纹 vs 全量比较

使用 `_tab_fingerprint()` 只比较 `page_id` + `active` 状态的原因：

| 比较策略 | 问题 |
|----------|------|
| 全量 `==` 比较（含 title/url） | 页面加载中 title 不断变化 → 每帧都推送 tabs 消息，产生大量无效通信 |
| 只比较 page_id + active | 只在标签页数量或活动标签变化时推送，稳定可靠 |

生命周期事件（start/stop/navigate）使用 `force=True` 确保即时推送。

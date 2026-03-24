# Browser Tab Management & Toggle — 修改文档

> 本文档记录浏览器面板新增 **标签页管理（新建/关闭 Tab）** 和 **面板展开/隐藏切换按钮** 的所有代码变更。

---

## 一、变更概览

在原有 Browser Live-View 基础上新增：

1. **新建标签页** — Tab 栏右侧 "+" 按钮，点击创建空白标签页
2. **关闭标签页** — 每个 Tab 悬停显示 "X" 关闭按钮（仅剩一个 Tab 时隐藏）
3. **浏览器面板展开/隐藏切换** — Chat 页面右上角全局图标按钮，浏览器运行时显示：
   - 面板已展开：图标高亮（primary 色），点击隐藏面板
   - 面板已隐藏：图标置灰，点击展开面板
4. **工具栏关闭按钮改为隐藏按钮** — 原 `CloseOutlined` 改为 `LayoutOutlined`，语义从"关闭"变为"隐藏"

---

## 二、文件清单

| 操作 | 文件路径 | 说明 |
|:----:|----------|------|
| 修改 | `src/copaw/agents/tools/browser_control.py` | 新增 `create_new_tab()` / `close_tab_by_id()` 公共函数 |
| 修改 | `src/copaw/agents/tools/__init__.py` | 导出新增的两个公共函数 |
| 修改 | `src/copaw/app/routers/browser_live_view.py` | 新增 `new_tab` / `close_tab` WebSocket 消息处理 |
| 修改 | `console/src/components/BrowserLiveView/index.tsx` | Tab 关闭按钮 + 新建 Tab 按钮 + onClose→onHide |
| 修改 | `console/src/components/BrowserLiveView/index.module.less` | `.tabClose` / `.newTabBtn` 样式 |
| 修改 | `console/src/pages/Chat/index.tsx` | 浏览器切换按钮 + `browserRunning` 状态 |

---

## 三、后端变更

### 3.1 `browser_control.py` — 新增公共函数

```python
async def create_new_tab(agent_id: str = "") -> dict:
    """创建空白标签页，返回 {"ok": True, "page_id": "page_N"}"""

async def close_tab_by_id(page_id: str, agent_id: str = "") -> dict:
    """关闭指定标签页，自动切换到剩余页，返回 {"ok": True, "page_id": "当前活跃页"}"""
```

**关键逻辑：**
- `create_new_tab`：复用 `_next_page_id()` 分配 ID，`_attach_page_listeners()` 绑定事件，触发 `navigated` 生命周期回调
- `close_tab_by_id`：关闭 page 并清理所有关联状态（refs / console_logs / network_requests / dialogs / file_choosers），若关闭的是当前活跃页则自动切到剩余页的第一个

### 3.2 `browser_live_view.py` — 新增 WebSocket 消息类型

新增两个 handler 函数并注册到 WebSocket 消息分发：

```python
async def _handle_new_tab(agent_id: str) -> None:
    """处理 {"type": "new_tab"} 消息"""
    # 1. 调用 create_new_tab()
    # 2. 重启 CDP screencast（如果正在使用）
    # 3. 广播更新后的 tab 列表

async def _handle_close_tab(data: dict, agent_id: str) -> None:
    """处理 {"type": "close_tab", "page_id": "..."} 消息"""
    # 1. 若关闭的是 CDP 活跃页，先停止 CDP screencast
    # 2. 调用 close_tab_by_id()
    # 3. 为新的活跃页启动 CDP screencast
    # 4. 广播更新后的 tab 列表
```

WebSocket 消息分发新增：

```python
elif msg_type == "new_tab":
    await _handle_new_tab(agent_id)
elif msg_type == "close_tab":
    await _handle_close_tab(data, agent_id)
```

---

## 四、前端变更

### 4.1 `BrowserLiveView/index.tsx`

**Props 变更：**
- `onClose` → `onHide`：语义从"关闭浏览器"改为"隐藏面板"

**新增图标导入：**
```tsx
import { CloseOutlined, LayoutOutlined, PlusOutlined } from "@ant-design/icons";
```

**新增事件处理函数：**
```tsx
function handleNewTab() {
  sendMessage({ type: "new_tab" });
}

function handleCloseTab(e: MouseEvent<HTMLSpanElement>, pageId: string) {
  e.stopPropagation();  // 阻止冒泡到 tab 切换
  sendMessage({ type: "close_tab", page_id: pageId });
}
```

**Tab 栏 UI 变更：**
- 每个 Tab 内新增关闭按钮 `<span className={styles.tabClose}>`（仅 `tabs.length > 1` 时渲染）
- Tab 列表末尾新增 "+" 按钮 `<button className={styles.newTabBtn}>`

**工具栏变更：**
- 关闭按钮图标从 `CloseOutlined` 改为 `LayoutOutlined`
- title 从 "Close" 改为 "Hide browser"

### 4.2 `BrowserLiveView/index.module.less` — 新增样式

```less
.tabClose {
  // 16x16 圆形按钮，9px 字号
  // 默认 opacity: 0，tab hover 或 tabActive 时 opacity: 1
  // hover 时显示背景色
}

.newTabBtn {
  // 28x28 圆形按钮，12px 字号
  // 紧跟在 tabList 右侧，flex-shrink: 0
}
```

同时修改 `.tabTitle` 添加 `flex: 1; min-width: 0;` 以适配关闭按钮的空间。

### 4.3 `Chat/index.tsx` — 浏览器切换按钮

**新增状态：**
```tsx
const [browserRunning, setBrowserRunning] = useState(false);
```

在 status 轮询 effect 中同步更新：
```tsx
setBrowserRunning(res.running);
```

**新增切换回调：**
```tsx
const toggleBrowser = useCallback(() => {
  if (browserOpen) {
    userClosedBrowserRef.current = true;
    setBrowserOpen(false);
  } else {
    userClosedBrowserRef.current = false;
    setBrowserOpen(true);
  }
}, [browserOpen]);
```

**切换按钮 UI：**
```tsx
{browserRunning && (
  <Tooltip title={browserOpen ? "Hide browser" : "Show browser"}>
    <Button
      type="text"
      icon={<GlobalOutlined />}
      onClick={toggleBrowser}
      style={{
        position: "absolute",
        top: 8, right: 8, zIndex: 10,
        opacity: browserOpen ? 0.9 : 0.6,
        color: browserOpen ? "var(--ant-color-primary)" : undefined,
      }}
    />
  </Tooltip>
)}
```

- 仅在 `browserRunning` 为 true 时显示
- 展开时图标使用 primary 色 + 高透明度，隐藏时使用默认色 + 低透明度
- 使用 `position: absolute` 定位在 Chat 区域右上角

---

## 五、WebSocket 协议变更

### 新增客户端→服务端消息

| type | 字段 | 说明 |
|------|------|------|
| `new_tab` | — | 创建空白标签页 |
| `close_tab` | `page_id: string` | 关闭指定标签页 |

服务端响应：通过已有的 `tabs` 广播消息通知所有客户端更新标签列表。

---

## 六、交互流程

```
用户点击 "+" 按钮
  → WS 发送 {"type": "new_tab"}
  → 后端创建 page，分配 page_id
  → 后端广播 {"type": "tabs", "tabs": [...]}
  → 前端更新 tab 列表，新 tab 高亮

用户点击 tab 上的 "X"
  → WS 发送 {"type": "close_tab", "page_id": "page_3"}
  → 后端关闭 page，切换活跃页
  → 后端广播更新后的 tab 列表
  → 前端更新，自动切到新活跃页

用户点击 Chat 右上角 GlobalOutlined 图标
  → browserOpen ? 隐藏面板 : 展开面板
  → 面板隐藏时设置 userClosedBrowserRef，阻止自动弹出
  → 面板展开时清除该标记
```

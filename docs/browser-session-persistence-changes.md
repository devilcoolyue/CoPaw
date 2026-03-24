# 浏览器会话持久化 — 修改文档

> 本文档记录浏览器 Cookie/Storage 会话持久化功能的所有代码变更，供合码 review 参考。

---

## 一、变更概览

此前浏览器的 `BrowserContext` 在 `stop`、idle 超时或进程退出时被销毁，**所有 cookie、localStorage、sessionStorage 随之丢失**。用户通过 Live View 手动登录后，下次 agent 打开同一网站仍需重新登录。

此外，`_action_open()` 每次都调用 `context.new_page()` 创建新页面，即使同一 agent 已有页面。新页面无法继承旧页面的 `sessionStorage`，导致即使在同一会话内，重新打开 URL 也会丢失登录态。

**本次变更**实现三层会话持久化：

```
┌─────────────────────────────────────────────────────────┐
│ 第一层：同会话页面复用                                     │
│ open() 检测到已有页面 → navigate 复用 → sessionStorage 保留 │
├─────────────────────────────────────────────────────────┤
│ 第二层：跨会话自动持久化                                    │
│ stop/idle/exit → 自动保存 cookie + localStorage +         │
│ sessionStorage 到 ~/.copaw/browser_state/{agent_id}.json │
│ start → 自动检测并加载保存的状态                            │
├─────────────────────────────────────────────────────────┤
│ 第三层：手动控制 API                                       │
│ get_cookies / set_cookies / save_storage_state /          │
│ load_storage_state 四个 action 供精细控制                  │
└─────────────────────────────────────────────────────────┘
```

---

## 二、文件清单

| 操作 | 文件路径 | 改动量 | 说明 |
|:----:|----------|:------:|------|
| 修改 | `src/copaw/agents/tools/browser_control.py` | **大** | 核心功能：自动持久化 + 手动 API + 页面复用 |

---

## 三、后端变更详情

### 3.1 新增存储路径常量和辅助函数

```python
# Auto-persist storage state directory
_STORAGE_STATE_DIR = os.path.join(
    os.path.expanduser("~"), ".copaw", "browser_state"
)

def _storage_state_path(agent_id: str) -> str:
    """Return the auto-persist file path for an agent's storage state."""
    safe_id = agent_id.replace("/", "_").replace("\\", "_")
    return os.path.join(_STORAGE_STATE_DIR, f"{safe_id}.json")
```

存储文件路径示例：`~/.copaw/browser_state/default.json`

### 3.2 `AgentBrowserContext` dataclass 新增字段

```python
_pending_session_storage: list = field(default_factory=list)
```

用于 context 加载后延迟恢复 sessionStorage（因 sessionStorage 是 per-page 的，必须等页面创建后才能注入）。

### 3.3 自动保存 — `_async_close_agent_context()` 变更

在关闭 context 前，自动保存完整状态（cookie + localStorage + sessionStorage）：

```python
async def _async_close_agent_context(agent_id: str) -> None:
    ctx = _agent_contexts.pop(agent_id, None)
    if ctx is None:
        return

    # ===== 新增：自动保存 =====
    context = ctx._sync_context if _USE_SYNC_PLAYWRIGHT else ctx.context
    if context is not None:
        # 1. 获取 Playwright 原生 storage_state（cookie + localStorage）
        state = await context.storage_state()

        # 2. 额外用 JS 抓取 sessionStorage（Playwright 不包含）
        session_storage = []
        for pid, page in ctx.pages.items():
            ss = await page.evaluate("""(() => {
                const d = {};
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    d[k] = sessionStorage.getItem(k);
                }
                return { origin: location.origin, data: d };
            })()""")
            if ss and ss.get("data"):
                session_storage.append(ss)

        # 3. 合并写入文件
        state["sessionStorage"] = session_storage
        with open(state_path, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ... 原有关闭逻辑 ...
```

**同步版本** `_close_agent_context()`（用于 atexit 清理）仅在 `_USE_SYNC_PLAYWRIGHT` 为 True 时（Windows）执行保存。macOS/Linux 使用 async Playwright，其 `storage_state()` / `evaluate()` 返回 coroutine，在 sync 函数中无法 `await`，因此 atexit 路径不保存。正常关闭路径（`stop`、idle 超时）均走 `_async_close_agent_context()`，全平台正确保存。

### 3.4 自动加载 — `_ensure_browser()` Phase 2 变更

创建新 `BrowserContext` 时自动检测并加载保存的状态：

```python
# Phase 2: ensure agent has a BrowserContext
if not has_context:
    state_path = _storage_state_path(agent_id)
    has_saved_state = os.path.isfile(state_path)

    if has_saved_state:
        # 用 storage_state 参数创建 context（自动恢复 cookie + localStorage）
        context = await browser.new_context(storage_state=state_path)
    else:
        context = await browser.new_context()

    # 延迟加载 sessionStorage 数据到 ctx
    if has_saved_state:
        with open(state_path) as f:
            saved = json.load(f)
        ss_data = saved.get("sessionStorage", [])
        if ss_data:
            ctx._pending_session_storage = ss_data
```

### 3.5 sessionStorage 延迟恢复 — 新增 `_restore_session_storage()`

```python
async def _restore_session_storage(page, ctx):
    """Restore sessionStorage to a page if pending data exists."""
    ss_data = getattr(ctx, "_pending_session_storage", None)
    if not ss_data:
        return
    origin = await page.evaluate("location.origin")
    for entry in ss_data:
        if entry.get("origin") == origin:
            await page.evaluate(
                "(data) => {"
                "  for (const [k, v] of Object.entries(data)) {"
                "    sessionStorage.setItem(k, v);"
                "  }"
                "}",
                entry["data"],
            )
            break
```

sessionStorage 是 per-page 的，不能在 context 级别设置，因此采用**延迟恢复**策略：先存入 `ctx._pending_session_storage`，等 `_action_open()` 创建页面并导航后再注入。

### 3.6 页面复用 — `_action_open()` 变更

```python
async def _action_open(url, page_id):
    # ...
    ctx = _get_or_create_agent_ctx()

    # ===== 新增：复用已有页面 =====
    if page_id in ctx.pages and ctx.pages[page_id]:
        return await _action_navigate(url, page_id)

    # 原有创建新页面逻辑 ...
    page = await ctx.context.new_page()
    await page.goto(url)

    # ===== 新增：恢复 sessionStorage 并 reload =====
    await _restore_session_storage(page, ctx)
    if getattr(ctx, "_pending_session_storage", None):
        # SPA 初始化时读 sessionStorage，需要 reload 才能生效
        if page.url.rstrip("/") == url.rstrip("/"):
            await page.reload()
        ctx._pending_session_storage = []
```

**逻辑说明：**

| 场景 | 行为 | 效果 |
|------|------|------|
| page_id 已存在 | 复用 → `_action_navigate()` | sessionStorage 保留，不创建新 tab |
| page_id 不存在，有 pending sessionStorage | 新建页面 → 注入 sessionStorage → reload | SPA 重新初始化时读取 token |
| page_id 不存在，无 pending | 新建页面（原有行为） | 无变化 |

### 3.7 新增手动 API — 四个 Action

#### 3.7.1 `get_cookies` — 导出 Cookie

```python
async def _action_get_cookies(cookies_url="", cookies_file=""):
```

| 参数 | 说明 |
|------|------|
| `cookies_url` | 按 URL 过滤 cookie（可选） |
| `cookies_file` | 保存到文件路径（可选，不传则直接返回） |

#### 3.7.2 `set_cookies` — 注入 Cookie

```python
async def _action_set_cookies(cookies_json="", cookies_file=""):
```

| 参数 | 说明 |
|------|------|
| `cookies_json` | JSON 数组字符串，直接传入 cookie |
| `cookies_file` | 从文件加载 cookie |

#### 3.7.3 `save_storage_state` — 保存完整状态

```python
async def _action_save_storage_state(cookies_file=""):
```

调用 Playwright 原生 `context.storage_state()`，保存 cookie + localStorage 到文件。

#### 3.7.4 `load_storage_state` — 加载完整状态

```python
async def _action_load_storage_state(cookies_file=""):
```

**流程：**

```
关闭当前 agent 的所有 page 和 context
    → 用 storage_state=cookies_file 创建新 context
    → 附加事件监听器
    → 返回加载的 cookie/origin 数量
```

### 3.8 `browser_use()` 函数签名变更

新增三个参数：

```python
async def browser_use(
    # ... 原有参数 ...
    cookies_json: str = "",   # set_cookies 用
    cookies_file: str = "",   # get/set/save/load 用
    cookies_url: str = "",    # get_cookies 过滤用
) -> ToolResponse:
```

Action 列表新增：

```python
if action == "get_cookies":
    return await _action_get_cookies(cookies_url, cookies_file)
if action == "set_cookies":
    return await _action_set_cookies(cookies_json, cookies_file)
if action == "save_storage_state":
    return await _action_save_storage_state(cookies_file)
if action == "load_storage_state":
    return await _action_load_storage_state(cookies_file)
```

---

## 四、存储文件格式

`~/.copaw/browser_state/{agent_id}.json` 结构：

```jsonc
{
  "cookies": [
    {
      "name": "session_id",
      "value": "abc123",
      "domain": "192.168.3.123",
      "path": "/",
      "expires": 1711234567,
      "httpOnly": true,
      "secure": false,
      "sameSite": "Lax"
    }
  ],
  "origins": [
    {
      "origin": "http://192.168.3.123:31813",
      "localStorage": [
        { "name": "token", "value": "eyJhbGci..." }
      ]
    }
  ],
  // Playwright 原生不包含，我们自行扩展
  "sessionStorage": [
    {
      "origin": "http://192.168.3.123:31813",
      "data": {
        "auth_token": "eyJhbGci...",
        "user_info": "{\"name\":\"admin\"}"
      }
    }
  ]
}
```

---

## 五、自动持久化触发点

| 触发场景 | 保存入口 | 模式 |
|----------|---------|------|
| agent 调用 `stop` | `_async_close_agent_context()` | async，全平台 |
| idle 超时（默认 30 分钟） | `_async_close_agent_context()` | async，全平台 |
| 进程退出 atexit | `_close_agent_context()` | sync，仅 Windows（macOS/Linux 的 async Playwright 无法在 sync 中调用） |

| 触发场景 | 加载入口 | 说明 |
|----------|---------|------|
| `_ensure_browser()` Phase 2 | `browser.new_context(storage_state=path)` | cookie + localStorage |
| `_action_open()` 新建页面后 | `_restore_session_storage()` | sessionStorage 延迟注入 |

---

## 六、关键设计决策

| 决策点 | 选择 | 原因 |
|--------|------|------|
| sessionStorage 处理 | JS evaluate 抓取 + 延迟注入 | Playwright `storage_state()` 不含 sessionStorage；sessionStorage 是 per-page 的，必须有页面后才能注入 |
| 页面复用 vs 新建 | page_id 已存在时复用 | 避免创建新 tab 导致 sessionStorage 和内存变量丢失 |
| 注入后 reload | 仅在 URL 未跳转时 reload | SPA 初始化时读 sessionStorage，需 reload 触发；若已被重定向（如跳转登录），则不 reload 避免循环 |
| 自动 vs 手动 | 默认自动，额外提供手动 API | 大多数用户只想"登录一次就行"；高级用户可手动控制跨域 cookie 等 |
| 存储路径 | `~/.copaw/browser_state/` | 与 workspace 数据目录一致，不污染项目目录 |
| agent_id 作文件名 | 替换 `/` `\` 为 `_` | 安全文件名，支持多 agent 隔离 |

---

## 七、边界情况

| 场景 | 处理方式 |
|------|----------|
| 保存的 cookie 已过期 | 浏览器自动忽略过期 cookie，服务端会返回 401/302 |
| sessionStorage 为空 | `_restore_session_storage` 检查后直接跳过 |
| 存储文件不存在 | `_ensure_browser()` 检查 `os.path.isfile()`，不存在则创建空白 context |
| 存储文件格式损坏 | try/except 捕获，回退到空白 context |
| 多 agent 同时使用 | 每个 agent 有独立的存储文件，互不干扰 |
| 同一 page_id 重复 open | 复用现有页面（navigate），不创建新 tab |
| SPA 使用内存变量存 token | 页面复用（navigate）可保留；跨 session 无法恢复（内存变量无法序列化） |
| 保存时页面已关闭 | evaluate 失败被 try/except 捕获，sessionStorage 部分跳过，cookie/localStorage 仍保存 |

---

## 八、使用场景

### 场景 1：同会话内复用登录态

```
用户: "打开浏览器，访问 login 页面"
agent: start → open(login)
用户: (Live View 手动登录)
用户: "现在打开 Overview 页面"
agent: open(Overview) → 检测到已有页面 → navigate → 登录态保留 ✅
```

### 场景 2：跨会话自动恢复

```
对话 1:
  agent: start → open(login) → 用户手动登录 → stop
  (自动保存到 ~/.copaw/browser_state/default.json)

对话 2:
  agent: start (自动加载保存的状态) → open(Overview)
  (cookie + localStorage 自动恢复 → 登录态保留 ✅)
  (sessionStorage 延迟注入 + reload → 登录态保留 ✅)
```

### 场景 3：手动管理 Cookie（高级）

```json
// 登录后手动导出
{"action": "get_cookies", "cookies_url": "http://example.com", "cookies_file": "~/.copaw/cookies/example.json"}

// 注入到另一个系统（SSO）
{"action": "set_cookies", "cookies_file": "~/.copaw/cookies/example.json"}

// 完整保存（含 localStorage）
{"action": "save_storage_state", "cookies_file": "~/.copaw/state/full.json"}

// 完整恢复（重建 BrowserContext）
{"action": "load_storage_state", "cookies_file": "~/.copaw/state/full.json"}
```

---

## 九、验证步骤

1. **同会话页面复用**：start → open(login) → 手动登录 → open(Overview) → 验证不跳转登录
2. **跨会话自动恢复**：登录后 stop → 检查 `~/.copaw/browser_state/` 生成了文件 → start → open(Overview) → 验证不跳转登录
3. **手动 API**：get_cookies 返回正确 cookie 列表 → set_cookies 注入后访问验证
4. **多 agent 隔离**：两个 agent 分别登录不同系统 → 各自的 storage 文件独立
5. **Idle 超时恢复**：登录后等待超时 → 重新 start → 验证登录态恢复

---

## 十、无改动说明

- 无数据库/配置/迁移变更
- 无新依赖引入
- 无前端变更
- 无破坏性变更，未使用手动 API 时行为与旧版一致（自动持久化透明运行）
- 未修改任何测试文件（建议后续补充 storage state 保存/加载的单元测试）

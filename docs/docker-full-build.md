# Docker Full Build（独立镜像，内嵌浏览器）

基于公共基础镜像构建 CoPaw 的完整 Docker 镜像，包含 Python、Node.js、Chromium 浏览器及虚拟显示环境，可在任意 Docker 环境中运行。

## 镜像组成

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.11 | 基础镜像 `python:3.11-slim-bookworm` |
| Node.js | 20.x | 通过 nodesource 安装，用于前端构建及运行时 |
| Chromium | 146.x | 系统 apt 安装，Playwright 直接调用 |
| Xvfb | - | 虚拟帧缓冲，提供 DISPLAY :1 (1280x800x24) |
| XFCE4 | - | 轻量桌面环境，浏览器操作需要 |
| Supervisor | - | 进程管理（dbus、Xvfb、XFCE4、CoPaw App） |

## 文件说明

```
deploy/Dockerfile.full              # 独立 Dockerfile（公共基础镜像）
deploy/entrypoint.sh                # 容器入口脚本
deploy/config/supervisord.conf.template  # Supervisor 配置模板
scripts/docker_build_full.sh        # 构建脚本
docker-compose.yml                  # Docker Compose 配置
```

## 构建

### 基本构建

```bash
bash scripts/docker_build_full.sh copaw:latest
```

### 需要代理时

```bash
docker build -f deploy/Dockerfile.full \
    --build-arg http_proxy=http://host.docker.internal:7890 \
    --build-arg https_proxy=http://host.docker.internal:7890 \
    --add-host=host.docker.internal:host-gateway \
    -t copaw:latest .
```

### 自定义频道

```bash
# 排除指定频道
COPAW_DISABLED_CHANNELS=imessage,voice bash scripts/docker_build_full.sh

# 仅启用指定频道
COPAW_ENABLED_CHANNELS=discord,telegram bash scripts/docker_build_full.sh
```

### 跨架构构建（Apple Silicon Mac 构建 x86_64 镜像）

```bash
docker build -f deploy/Dockerfile.full --platform linux/amd64 -t copaw:latest .
```

> 注意：跨架构编译通过 QEMU 模拟，速度较慢。建议在目标架构的机器上构建。

## 运行

### 快速启动

```bash
docker run -d -p 8088:8088 --name copaw copaw:latest
```

### 带持久化存储

```bash
docker run -d -p 8088:8088 --name copaw \
    -v copaw-data:/app/working \
    -v copaw-secrets:/app/working.secret \
    copaw:latest
```

### 自定义端口

```bash
docker run -d -e COPAW_PORT=3000 -p 3000:3000 --name copaw copaw:latest
```

### 启用认证

```bash
docker run -d -p 8088:8088 --name copaw \
    -e COPAW_AUTH_ENABLED=true \
    -e COPAW_AUTH_USERNAME=admin \
    -e COPAW_AUTH_PASSWORD=yourpassword \
    copaw:latest
```

### 使用 Docker Compose

```bash
docker-compose up -d
```

## 导出与迁移

```bash
# 导出镜像
docker save copaw:latest | gzip > copaw-latest.tar.gz

# 在目标机器加载
docker load < copaw-latest.tar.gz

# 然后运行
docker run -d -p 8088:8088 --name copaw copaw:latest
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `COPAW_PORT` | `8088` | 应用端口 |
| `COPAW_WORKING_DIR` | `/app/working` | 工作目录 |
| `COPAW_SECRET_DIR` | `/app/working.secret` | 密钥存储目录 |
| `COPAW_DISABLED_CHANNELS` | `imessage` | 禁用的频道（逗号分隔） |
| `COPAW_ENABLED_CHANNELS` | (空) | 启用的频道白名单（逗号分隔） |
| `COPAW_AUTH_ENABLED` | `false` | 是否启用认证 |
| `COPAW_AUTH_USERNAME` | - | 认证用户名 |
| `COPAW_AUTH_PASSWORD` | - | 认证密码 |
| `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` | `/usr/bin/chromium` | Chromium 路径 |

## 容器内进程

Supervisor 管理以下进程：

| 进程 | 优先级 | 说明 |
|------|--------|------|
| dbus | 0 | D-Bus 消息总线 |
| xvfb | 10 | 虚拟显示服务 (:1) |
| xfce4 | 20 | 桌面环境 |
| app | 30 | CoPaw 应用 (`copaw app --host 0.0.0.0`) |

## 验证

容器启动后访问 `http://<host>:8088` 即可看到 CoPaw Console 页面。

```bash
# 检查容器状态
docker logs copaw

# 检查内嵌浏览器
docker exec copaw chromium --version

# 检查 Python 版本
docker exec copaw python --version
```

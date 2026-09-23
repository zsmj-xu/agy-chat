# 双子对话台 · Claude × agy Chat

一个极简的高性能多模型对话网页，可在同一界面里分别跟 **Claude** 和 **agy (Gemini)** 对话。后端零第三方 Web 框架，只依赖 Python 标准库起服务。

已针对**公网 / 互联网部署**强化安全防护机制，深度优化前端交互体验，并支持通过 **Docker Compose 一键部署**。

---

## 架构（混合设计）

| 侧 | 接法 | 认证方式 | 支持模型 |
|---|---|---|---|
| **Claude** | [`anthropic`](https://pypi.org/project/anthropic/) SDK | 环境变量 `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` | `global.anthropic.claude-opus-4-8`（自动按序兜底 4-7 / 4-6 / haiku） |
| **agy** | [`agy`](https://antigravity.google) CLI（headless `--prompt=`） | 共享宿主已有的 OAuth 登录凭证 | `gemini-3.1-pro-high` / `gemini-3.8-flash-*` |

> **为何 agy 走 CLI 而非 SDK**：`google-antigravity` SDK 强制要求 `GEMINI_API_KEY` 或 Vertex AI，**不支持个人 OAuth 登录态**；而 `agy` CLI 能直接复用机器现有的 OAuth 登录态，零配置开箱即用。

---

## 安全与防护（面向互联网环境）

1. **密码访问保护**：
   - 支持环境变量 `AUTH_PASSWORD` 设置全局访问密码。
   - 若未配置，首次启动会自动生成 16 位高强度随机密码并保存在本地 `./data/.auth_password`。
2. **防暴力破解与限流 (Anti-Brute Force)**：
   - 连续 5 次输错密码将自动锁定该 IP 15 分钟（返回 HTTP 429），并提示剩余时间。
   - 所有密码比对与 Token 校验均采用 `hmac.compare_digest`，免疫时序侧信道攻击。
3. **安全 Session 机制**：
   - 采用 HMAC-SHA256 服务端签名 Token，支持 `HttpOnly; SameSite=Lax` Cookie 与 `Authorization: Bearer` Header 双重传递。
   - 签名私钥自动持久化在本地 `./data/.server_secret`，容器重启后已登录设备不失效。
4. **端口本地独占绑定**：
   - Docker Compose 端口映射默认指定为 `127.0.0.1:8765:8765`，**仅允许本地回环访问**，不对互联网暴露直接端口，安全交由反向代理统一接管。
5. **子进程安全隔离与断开即停**：
   - 用户点击前端「停止生成」或关闭网页/断网时，后端立即收到信号并 kill 子进程与终止流，避免浪费服务器资源。

---

## 前端交互升级

- **Markdown 实时流式排版 & 语法高亮**：
  - 自动渲染代码块（带语言标识、专属暗色容器）。
  - 每个代码块右上角配备「一键复制代码」按钮。
  - 每条 AI 回复底部提供「一键复制全文」操作。
- **主动打断生成 (Stop Generation)**：
  - 生成过程中发送按钮无缝切换为「停止」按钮，支持即时打断长文本输出。
- **智能吸底滚动**：
  - 当阅读之前记录时暂停自动吸底；右下角浮动出现「↓ 回到底部」快捷按钮。
- **多端会话持久化**：
  - 采用 `localStorage` 分离保存 Claude 与 agy 历史记录，刷新页面不丢失。
  - 顶部导航栏增加「一键清空」当前 Agent 对话按钮（带二次确认）。
- **移动端与自适应高度**：
  - 输入框随内容自动撑高与回缩，支持 `Enter` 发送、`Shift+Enter` 换行。

---

## 运行方式

### 方式一：Docker Compose 运行（推荐）

1. **环境准备**：
   - 复制配置模板（若 `.env` 尚不存在）：
     ```bash
     cp .env.example .env
     ```
   - 编辑 `.env` 填入你的密钥配置（若不填 `AUTH_PASSWORD`，容器启动时将自动生成保存在 `./data/.auth_password`）。

2. **启动容器**：
   ```bash
   # 构建并后台启动
   docker compose up -d --build
   
   # 查看运行日志与自动生成的密码
   docker compose logs -f
   
   # 停止容器
   docker compose down
   ```
   > 默认配置下，端口仅绑定在 `127.0.0.1:8765`，外部网络无法直连，保证开发与部署安全。

---

### 方式二：宿主机 Python 直接运行

```bash
# 激活环境并安装依赖
python3 -m venv venv
./venv/bin/pip install anthropic

# 设定访问密码并启动
AUTH_PASSWORD="your-strong-password" ./venv/bin/python server.py
```

---

## 公网/反向代理配置建议 (Nginx)

当你要在互联网上对外提供服务时，可使用 Nginx 作为反代网关并配置 HTTPS：

```nginx
server {
    listen 443 ssl http2;
    server_name chat.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/chat.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.yourdomain.com/privkey.pem;

    location / {
        # 指向本地 Docker Compose 绑定的 127.0.0.1:8765
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 流式传输关键配置（关闭代理缓冲，启用即时输出）
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

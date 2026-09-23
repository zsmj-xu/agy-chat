# 双子对话台 · Claude × agy Chat

一个极简的本地网页，可在同一界面里分别跟 **Claude** 和 **agy(Gemini)** 聊天。后端零第三方 Web 框架，只用 Python 标准库起服务。

## 架构（混合）

| 侧 | 接法 | 认证 | 模型 |
|---|---|---|---|
| **Claude** | [`anthropic`](https://pypi.org/project/anthropic/) SDK | 环境变量 `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`（可走代理网关） | `global.anthropic.claude-opus-4-8`（带兜底 4-7 / 4-6 / haiku） |
| **agy** | [`agy`](https://antigravity.google) CLI（headless `--prompt=`） | agy 自身的 OAuth 登录 | `gemini-3.1-pro-high` / `gemini-3.8-flash-*` |

> 为什么 agy 走 CLL 而不是 SDK：`google-antigravity` SDK 硬性要求 `GEMINI_API_KEY` 或 Vertex，**不认 OAuth**；而 agy CLI 复用你已有的 OAuth 登录，开箱即用。若日后有 Gemini API key，可无缝切到 SDK。

后端把浏览器消息转发给对应通道，回复以 HTTP chunked 实时流式回传。

## 前置条件

- Python 3.10+
- [`agy` CLI](https://antigravity.google/download) 已安装并完成 OAuth 登录（`agy` 能正常对话）
- 环境变量：`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`（指向可用的 Anthropic 兼容端点）

## 运行

```bash
python3 -m venv venv
./venv/bin/pip install anthropic
./venv/bin/python server.py            # 默认 http://127.0.0.1:8765
PORT=9000 ./venv/bin/python server.py  # 换端口
```

浏览器打开 `http://127.0.0.1:8765`。SSH 远程时先做端口转发：`ssh -L 8765:127.0.0.1:8765 用户@主机`。

## 使用

- 右上角切换 **Claude / agy**；选 agy 时可选 Gemini 模型
- `Enter` 发送，`Shift+Enter` 换行
- 两个 agent 各自独立的对话线程

## 说明

- 不含任何硬编码密钥，凭证全部来自运行环境。
- 单文件后端 `server.py` + 单页前端 `index.html`，无构建步骤。

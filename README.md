# DeepSeek Web API Bridge

<p align="center">
  <b>DeepSeek Web → OpenAI / Anthropic Compatible API Bridge</b>
</p>

<p align="center">
  <a href="#中文">中文</a> · <a href="#english">English</a>
</p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-experimental-orange">
  <img alt="python" src="https://img.shields.io/badge/python-3.12+-blue">
  <img alt="fastapi" src="https://img.shields.io/badge/FastAPI-API-green">
  <img alt="playwright" src="https://img.shields.io/badge/Playwright-browser%20automation-purple">
  <img alt="docker" src="https://img.shields.io/badge/Docker-ready-blue">
</p>

---

# 中文

[English](#english)

## 项目简介

**DeepSeek Web API Bridge** 是一个自托管桥接服务，用来把已经登录的 DeepSeek 网页会话转换成 OpenAI-compatible / Anthropic-compatible 的本地 API。

它适合这些场景：

- 你可以正常使用 DeepSeek 官方网页；
- 你希望把网页能力接入 OpenClaw、Hermes Agent、Telegram Bot 或其他 Agent；
- 你希望在 VPS / Docker 上运行一个统一的中转 API；
- 你不想每个客户端都手动打开网页对话。

项目通过 Playwright 启动浏览器、恢复 DeepSeek 登录态、在网页中发送消息、从 DOM 中提取回答，并对外暴露兼容接口。

> 重要说明：这不是 DeepSeek 官方 API。它是浏览器自动化桥，速度、稳定性和兼容性都无法完全等同官方 API。

## 核心能力

### OpenAI-compatible API

```text
POST /v1/chat/completions
GET  /v1/models
```

常见配置：

```text
Base URL: http://你的VPS:8000/v1
API Key: sk-local-change-me
Model: deepseek-web
```

### Anthropic-compatible API

V20 新增基础 Anthropic Messages 兼容：

```text
POST /anthropic/v1/messages
GET  /anthropic/v1/models
```

推荐优先使用 OpenAI-compatible 路径；Anthropic 路径目前是基础兼容层。

### 多账号 Worker

- 导入多个 DeepSeek 登录态；
- 每个账号可启动独立 Worker；
- 支持账号优先级、权重、任务队列；
- 支持失败恢复、超时重启、任务历史；
- 前端面板可管理账号和 Worker 状态。

### 会话隔离

每个 `sessionId` 对应一个独立 DeepSeek 网页页面和对话：

```text
telegram:user-123     -> 独立 DeepSeek 对话
openclaw:session-abc  -> 独立 DeepSeek 对话
web-test              -> 独立 DeepSeek 对话
```

支持从这些位置自动识别 session：

- `X-Bridge-Session-Id`
- `X-Channel-Id + X-Conversation-Id`
- request body `sessionId`
- OpenAI `user`
- OpenAI `metadata`
- OpenClaw sender metadata

### OpenClaw / Hermes Agent Adapter

项目针对 OpenClaw / Hermes 这类 Agent 做了适配：

- 支持 OpenAI `tools`;
- 支持返回 OpenAI `tool_calls`;
- 支持 `role=tool` 工具结果续写；
- 支持 Skill 请求识别，例如 `运行 opennews`;
- 支持 Skill → Tool 映射；
- 支持工具结果快速返回；
- 支持流式 keepalive，减少 Telegram 一直“正在输入”。

### Tool Result Fast Finalizer

Agent 执行工具后，通常还会把工具结果发回模型做最终总结。浏览器桥如果再走 DeepSeek 网页，会慢很多。

V19+ 默认：

```text
agentToolResultMode = fast_final
```

当收到 `role=tool` 时，桥会优先直接整理工具结果并返回，避免 Telegram / OpenClaw 长时间卡住。

### DeepSeek 原生工具标签

V20 借鉴 ds-free-api 的思路，新增 DeepSeek 风格工具标签：

```text
<｜tool▁call▁begin｜>[{"name":"工具名","arguments":{...}}]<｜tool▁call▁end｜>
```

并增加了更严格的工具调用提示和解析逻辑，让 OpenClaw / Hermes 更容易识别工具调用。

### DOM 调试面板

前端包含 DOM 调试页面，可查看：

- 输入框候选节点；
- 发送按钮候选节点；
- Assistant 回复候选节点；
- 选中的回答；
- 发送前后 DOM 差异；
- selector / source / order / length / preview。

当 DeepSeek 网页结构变化时，可以用它定位提取问题。

## 快速开始

### 1. 克隆仓库

```bash
cd /root
git clone https://github.com/illria/deepseek-web-api-bridge.git
cd deepseek-web-api-bridge
```

或者使用压缩包：

```bash
cd /root
unzip deepseek-web-api-bridge-v20.zip -d /root
cd /root/deepseek-web-api-bridge-v20
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

生成加密 key：

```bash
docker run --rm python:3.12-slim sh -lc 'pip install cryptography -q && python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
```

编辑 `.env`：

```env
STATE_ENCRYPTION_KEY=你的加密key
BRIDGE_API_KEYS=sk-local-change-me
```

### 3. 启动

```bash
docker compose up -d --build
docker compose logs -f --tail=100
```

### 4. 打开控制台

```text
http://你的VPS:8000/
```

### 5. 导入 DeepSeek 登录态

在控制台中粘贴提取到的 DeepSeek 登录态 JSON。

导入后：

```text
账号管理 -> 启动
账号管理 -> 重置会话
```

## API 示例

### 普通聊天

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-change-me" \
  -H "Content-Type: application/json" \
  -H "X-Bridge-Session-Id: web-test" \
  -d '{
    "model": "deepseek-web",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

### Streaming

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-change-me" \
  -H "Content-Type: application/json" \
  -H "X-Bridge-Session-Id: telegram-user-123" \
  -d '{
    "model": "deepseek-web",
    "stream": true,
    "messages": [
      {"role": "user", "content": "介绍一下秦国历史"}
    ]
  }'
```

### Tool Calling

```json
{
  "model": "deepseek-web",
  "stream": true,
  "messages": [
    {"role": "user", "content": "看一下系统当前有什么任务"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "exec",
        "description": "Run shell command",
        "parameters": {
          "type": "object",
          "properties": {
            "command": {"type": "string"}
          },
          "required": ["command"]
        }
      }
    }
  ]
}
```

## OpenClaw 配置建议

推荐：

```text
Base URL: http://你的VPS:8000/v1
Model: deepseek-web
API Key: sk-local-change-me
Streaming: enabled
Timeout: 300s - 600s
```

必须确认 OpenClaw 会把 `tools` 传给模型 provider。否则 Skill 本身只是说明书，无法被任何 OpenAI-compatible provider 真正执行。

调试接口：

```http
GET /api/debug/last-openai-request
```

重点看：

```json
{
  "tools": ["exec", "web_search", "web_fetch"],
  "skillDetected": {"skill": "opennews"},
  "directToolCall": true,
  "hasToolMessage": true,
  "toolResultFastFinal": true
}
```

## Hermes 配置建议

Hermes 如果支持自定义 OpenAI-compatible provider，建议使用：

```text
Base URL: http://你的VPS:8000/v1
Model: deepseek-web
API Key: sk-local-change-me
Streaming: enabled
Timeout: 300s+
```

V20 也提供基础 Anthropic Messages 兼容：

```text
Base URL: http://你的VPS:8000/anthropic/v1
Model: deepseek-web
```

但当前更推荐 OpenAI-compatible 路径。

## 重要接口

| Endpoint | 说明 |
|---|---|
| `GET /` | Web 控制台 |
| `GET /api/health` | 服务健康检查 |
| `GET /api/accounts` | 账号列表 |
| `POST /api/accounts/import` | 导入登录态 |
| `POST /api/accounts/{id}/worker/start` | 启动 Worker |
| `POST /api/accounts/{id}/worker/stop` | 停止 Worker |
| `POST /api/accounts/{id}/worker/reset` | 重置 DeepSeek 对话 |
| `GET /api/workers/status` | Worker 状态 |
| `POST /api/jobs` | 创建异步任务 |
| `GET /api/jobs` | 任务历史 |
| `GET /api/debug/last-openai-request` | 最近 OpenAI 请求调试 |
| `GET /api/accounts/{id}/worker/dom-debug` | DOM 快照 |
| `POST /api/accounts/{id}/worker/dom-probe` | DOM 探针 |
| `GET /v1/models` | OpenAI 模型列表 |
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `GET /anthropic/v1/models` | Anthropic 模型列表 |
| `POST /anthropic/v1/messages` | Anthropic Messages |

## 安全说明

不要提交这些内容：

```text
.env
data/
state/
cookies
登录态 JSON
运行时账号文件
```

推荐 `.gitignore`：

```gitignore
.env
data/
state/
*.log
*.sqlite
*.db
__pycache__/
*.pyc
.DS_Store
```

## 项目状态

本项目适合：

- 个人自动化；
- Telegram Bot 实验；
- OpenClaw / Hermes 适配测试；
- DeepSeek Web 到 API 的桥接；
- Agent Provider 原型验证。

不建议用于强稳定性生产场景。

---

# English

[中文](#中文)

## Overview

**DeepSeek Web API Bridge** is a self-hosted bridge that turns a logged-in DeepSeek Web session into an OpenAI-compatible and partially Anthropic-compatible local API.

It is useful when:

- you can access DeepSeek through the official web UI;
- you want to connect it to OpenClaw, Hermes Agent, Telegram bots, or other agents;
- you want to run a unified API gateway on a VPS or Docker;
- you do not want each client to manually open a browser session.

The project uses Playwright to launch a browser, restore your DeepSeek login state, send prompts through the web UI, extract answers from the DOM, and expose local API endpoints.

> Disclaimer: this is not an official DeepSeek API. It is a browser automation bridge, so latency, reliability, and compatibility cannot fully match a real API.

## Features

### OpenAI-compatible API

```text
POST /v1/chat/completions
GET  /v1/models
```

Typical client configuration:

```text
Base URL: http://your-vps:8000/v1
API Key: sk-local-change-me
Model: deepseek-web
```

### Anthropic-compatible API

V20 adds a minimal Anthropic Messages compatibility layer:

```text
POST /anthropic/v1/messages
GET  /anthropic/v1/models
```

The OpenAI-compatible path is still recommended as the primary integration route.

### Multi-account workers

- import multiple DeepSeek login states;
- start one browser worker per account;
- account priority and weight;
- job queue;
- failure recovery;
- timeout restart;
- job history;
- web control panel.

### Session isolation

Each logical `sessionId` maps to a dedicated DeepSeek browser page and conversation:

```text
telegram:user-123     -> isolated DeepSeek conversation
openclaw:session-abc  -> isolated DeepSeek conversation
web-test              -> isolated DeepSeek conversation
```

Session ID can be inferred from:

- `X-Bridge-Session-Id`
- `X-Channel-Id + X-Conversation-Id`
- request body `sessionId`
- OpenAI `user`
- OpenAI `metadata`
- OpenClaw sender metadata

### OpenClaw / Hermes Agent Adapter

The bridge includes an adapter layer for OpenClaw and Hermes-style agents:

- accepts OpenAI `tools`;
- returns OpenAI `tool_calls`;
- handles `role=tool` messages;
- detects Skill-like requests such as `运行 opennews`;
- maps Skill requests to available tools;
- fast-finalizes tool results;
- sends streaming keepalive chunks.

### Tool Result Fast Finalizer

Agent frameworks often call tools and then send the tool result back to the model for final wording. With a browser bridge, that extra browser round trip can be slow.

V19+ defaults to:

```text
agentToolResultMode = fast_final
```

When `role=tool` messages are present, the bridge can return a compact final answer directly instead of waiting for DeepSeek Web again.

### DeepSeek-native tool tags

V20 adds DeepSeek-style tool-call tags inspired by ds-free-api:

```text
<｜tool▁call▁begin｜>[{"name":"tool_name","arguments":{...}}]<｜tool▁call▁end｜>
```

The parser also supports multiple fallback formats, including markdown JSON and raw JSON arrays.

### DOM debug panel

The web panel includes DOM debugging tools for:

- composer candidates;
- send button candidates;
- assistant message candidates;
- selected answer;
- before/after DOM probes;
- selector/source/order/length/preview.

This is useful when the DeepSeek Web UI changes.

## Quick Start

### 1. Clone

```bash
cd /root
git clone https://github.com/illria/deepseek-web-api-bridge.git
cd deepseek-web-api-bridge
```

Or unzip a release package:

```bash
cd /root
unzip deepseek-web-api-bridge-v20.zip -d /root
cd /root/deepseek-web-api-bridge-v20
```

### 2. Create `.env`

```bash
cp .env.example .env
```

Generate an encryption key:

```bash
docker run --rm python:3.12-slim sh -lc 'pip install cryptography -q && python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
```

Edit `.env`:

```env
STATE_ENCRYPTION_KEY=your-generated-key
BRIDGE_API_KEYS=sk-local-change-me
```

### 3. Start

```bash
docker compose up -d --build
docker compose logs -f --tail=100
```

### 4. Open the panel

```text
http://your-vps:8000/
```

### 5. Import DeepSeek login state

Paste the exported DeepSeek login-state JSON into the panel.

After importing:

```text
Accounts -> Start
Accounts -> Reset conversation
```

## API Examples

### Basic chat

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-change-me" \
  -H "Content-Type: application/json" \
  -H "X-Bridge-Session-Id: web-test" \
  -d '{
    "model": "deepseek-web",
    "messages": [
      {"role": "user", "content": "Hello"}
    ]
  }'
```

### Streaming

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-change-me" \
  -H "Content-Type: application/json" \
  -H "X-Bridge-Session-Id: telegram-user-123" \
  -d '{
    "model": "deepseek-web",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Tell me about the history of Qin."}
    ]
  }'
```

## OpenClaw Notes

Recommended provider settings:

```text
Base URL: http://your-vps:8000/v1
Model: deepseek-web
API Key: sk-local-change-me
Streaming: enabled
Timeout: 300s - 600s
```

Make sure OpenClaw sends `tools` to the model provider. Without tools, Skills cannot be executed by any OpenAI-compatible provider.

Debug endpoint:

```http
GET /api/debug/last-openai-request
```

Important fields:

```json
{
  "tools": ["exec", "web_search", "web_fetch"],
  "skillDetected": {"skill": "opennews"},
  "directToolCall": true,
  "hasToolMessage": true,
  "toolResultFastFinal": true
}
```

## Hermes Notes

Recommended OpenAI-compatible provider settings:

```text
Base URL: http://your-vps:8000/v1
Model: deepseek-web
API Key: sk-local-change-me
Streaming: enabled
Timeout: 300s+
```

V20 also exposes a minimal Anthropic Messages layer:

```text
Base URL: http://your-vps:8000/anthropic/v1
Model: deepseek-web
```

The OpenAI-compatible path is currently better tested.

## Security

Never commit:

```text
.env
data/
state/
cookies
login-state JSON
runtime account files
```

Recommended `.gitignore`:

```gitignore
.env
data/
state/
*.log
*.sqlite
*.db
__pycache__/
*.pyc
.DS_Store
```

## Project Status

This project is experimental but usable for:

- personal automation;
- Telegram bot experiments;
- OpenClaw / Hermes integration tests;
- DeepSeek Web to API bridging;
- Agent provider prototyping.

It is not recommended for production-critical workloads that require official API-level reliability.

## License

MIT or your preferred open-source license.

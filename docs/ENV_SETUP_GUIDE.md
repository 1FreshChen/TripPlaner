# 真实服务配置指南

更新时间：2026-06-01

基础 mock 版本不需要任何 API Key 即可运行。只有在需要真实景点、天气、地图、LLM 行程和景点图片时，才需要按本文填写配置。

## 配置文件位置

后端：

```text
D:\Agent-demo\backend\.env
```

前端：

```text
D:\Agent-demo\frontend\.env
```

不要把 `.env` 文件提交到 Git，也不要在聊天或截图中公开 Key。

## 第一优先级：高德 Web 服务 Key

用途：后端查询真实 POI 景点、酒店和天气。

申请入口：

- 控制台：https://console.amap.com/dev/key/app
- 官方说明：https://lbs.amap.com/api/webservice/create-project-and-key

申请步骤：

1. 注册并登录高德开放平台。
2. 进入“应用管理”，创建新应用。
3. 点击“添加 Key”。
4. 服务平台选择“Web 服务”。
5. 将创建得到的 Key 填入后端 `.env`：

```dotenv
AMAP_API_KEY=你的高德Web服务Key
```

## 第二优先级：高德 JS API Key 和安全密钥

用途：前端显示真实高德地图和景点标记。

申请入口：

- 控制台：https://console.amap.com/dev/key/app
- 官方说明：https://lbs.amap.com/api/javascript-api/guide/abc/prepare

申请步骤：

1. 可以继续使用上一步创建的高德应用。
2. 再添加一个 Key。
3. 服务平台选择“Web 端 (JSAPI)”。
4. 设置域名白名单。开发阶段可加入 `localhost` 和 `127.0.0.1`。
5. 保存后会得到 JSAPI Key 和 `jscode` 安全密钥。
6. 填入前端 `.env`：

```dotenv
VITE_AMAP_JS_KEY=你的高德JSAPIKey
VITE_AMAP_SECURITY_CODE=你的jscode安全密钥
```

说明：

- 后端的 `AMAP_API_KEY` 和前端的 `VITE_AMAP_JS_KEY` 是两个不同平台类型的 Key。
- 高德官方说明指出，2021-12-02 之后申请的新 JSAPI Key 必须配合 `jscode` 使用。
- 当前项目使用适合本地开发的前端静态安全密钥方式。正式部署时，应按照高德官方建议使用代理服务器转发方式。

## 第三优先级：LLM API Key

用途：调用真实大模型生成结构化行程。OpenAI 和 DeepSeek 二选一即可。

### 方案 A：OpenAI

申请入口：

- API Key：https://platform.openai.com/api-keys
- 官方快速入门：https://platform.openai.com/docs/quickstart/step-2-setup-your-api-key
- API 账单：https://platform.openai.com/account/billing/overview

填入后端 `.env`：

```dotenv
LLM_API_KEY=你的OpenAIAPIKey
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

说明：

- 项目当前默认使用 `gpt-4o-mini`，该模型支持 Chat Completions 和结构化输出。
- ChatGPT 订阅和 OpenAI API 账单彼此独立。即使已经订阅 ChatGPT，也需要单独开通 API 账单。
- API Key 只放在后端 `.env`，不要写入前端。

### 方案 B：DeepSeek

申请入口：

- API Key：https://platform.deepseek.com/api_keys
- 官方快速入门：https://api-docs.deepseek.com/zh-cn/

填入后端 `.env`：

```dotenv
LLM_API_KEY=你的DeepSeekAPIKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

说明：

- DeepSeek 官方 API 提供 OpenAI-compatible 格式，适合当前项目。
- 截至 2026-06-01，官方推荐模型名包括 `deepseek-v4-flash` 和 `deepseek-v4-pro`。
- 不建议新项目使用旧模型名 `deepseek-chat` 或 `deepseek-reasoner`；官方文档注明它们将在 2026-07-24 弃用。

## 可选：Unsplash Access Key

用途：为景点补充图片。留空不会影响行程生成和地图展示。

申请入口：

- 开发者页面：https://unsplash.com/developers
- 官方文档：https://unsplash.com/documentation

申请步骤：

1. 注册 Unsplash 开发者账号。
2. 进入 Your Apps。
3. 点击 New Application。
4. 接受 API 条款并填写应用信息。
5. 复制 Access Key。
6. 填入后端 `.env`：

```dotenv
UNSPLASH_ACCESS_KEY=你的UnsplashAccessKey
```

说明：

- Secret Key 不需要填入本项目。
- Unsplash 文档说明，新应用初始为 demo mode，适合教育和演示用途。
- 正式展示图片时，需要遵守 Unsplash 的图片署名和链接规范。

## 可选：高德 MCP Server

用途：进一步复现章节中的 MCP 工具调用方案。当前基础版直接调用高德 HTTP API，不安装 MCP Server 也能运行。

项目地址：

- GitHub：https://github.com/sugarforever/amap-mcp-server

该项目当前发布在 PyPI，README 提供的 stdio 配置使用：

```json
{
  "mcpServers": {
    "amap-mcp-server": {
      "command": "uvx",
      "args": ["amap-mcp-server"],
      "env": {
        "AMAP_MAPS_API_KEY": "你的高德Web服务Key"
      }
    }
  }
}
```

这属于后续增强，不是当前运行前置条件。

## 推荐填写顺序

1. 先填写 `AMAP_API_KEY`，验证真实景点和天气。
2. 再填写 `VITE_AMAP_JS_KEY` 和 `VITE_AMAP_SECURITY_CODE`，验证真实地图。
3. 选择 OpenAI 或 DeepSeek，填写对应 LLM 配置。
4. 最后按需填写 `UNSPLASH_ACCESS_KEY`。

每次修改 `.env` 后，需要重启对应服务。

# 智能旅行助手

这是根据 Datawhale HelloAgents 第十三章“智能旅行助手”实现的基础版项目。项目采用前后端分离架构：后端使用 FastAPI 和 Pydantic，前端使用 Vue 3、TypeScript、Vite，并保留了高德地图、LLM、Unsplash 的接入位置。

## 已实现能力

- 输入目的地、日期、偏好、预算、交通方式、住宿类型。
- 生成每日行程、景点、餐饮、酒店、天气、预算。
- 无 API Key 时使用本地 mock 数据，保证项目可启动和演示。
- 预留高德地图 Web 服务 API、LLM API、Unsplash API 接入。
- 前端展示行程概览、预算、地图区域、每日行程、天气。
- 支持删除景点、上下移动景点、保存或取消编辑。
- 支持导出图片和 PDF。

## 需要你填写的 Key

分别复制后端和前端环境变量模板：

```bash
copy .env.example backend\.env
copy frontend\.env.example frontend\.env
```

需要填写：

- `LLM_API_KEY`：OpenAI、DeepSeek 或其他 OpenAI-compatible 服务的 API Key。
- `LLM_BASE_URL`：LLM 服务地址，默认是 `https://api.openai.com/v1`。
- `LLM_MODEL`：模型名称。
- `AMAP_API_KEY`：高德地图 Web 服务 Key，用于后端 POI 和天气查询。
- `UNSPLASH_ACCESS_KEY`：Unsplash 图片搜索 Key，可选。
- `VITE_AMAP_JS_KEY`：高德地图 JS API Key，用于前端地图。
- `VITE_AMAP_SECURITY_CODE`：高德地图 JS API 安全密钥，如你的高德应用要求填写。

## 启动后端

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy ..\.env.example .env
uvicorn app.api.main:app --reload
```

访问：

- API 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>

## 启动前端

```bash
cd frontend
npm install
copy .env.example .env
npm run dev
```

访问：<http://localhost:5173>

## 测试

```bash
cd backend
python -m pytest tests -q
```

如本机尚未安装 FastAPI、Pydantic 等依赖，请先执行 `pip install -r requirements.txt`。

## 项目结构

```text
Agent-demo/
├── backend/
│   ├── app/
│   │   ├── agents/
│   │   ├── api/
│   │   ├── models/
│   │   └── services/
│   ├── tests/
│   ├── requirements.txt
│   └── run.py
├── frontend/
│   ├── src/
│   │   ├── router/
│   │   ├── services/
│   │   ├── types/
│   │   └── views/
│   └── package.json
└── docs/
    ├── API_KEYS.md
    └── PROJECT_STATUS.md
```

## 高德地图 QPS 治理

个人 Key 通常只有约 3 QPS 配额，而 MCP 文本搜索会为每个 POI 逐条拉取详情，多节点并行时容易触发 `CUQPS_HAS_EXCEEDED_THE_LIMIT`（错误码 10021）。代码内置了进程级限流与 QPS 错误退避重试：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `AMAP_QPS_BUDGET` | `2.0` | 进程级每秒允许的高德调用额度；`0` 关闭限流 |
| `AMAP_QPS_RETRY_ATTEMPTS` | `2` | 遇到 QPS 限流错误时的退避重试次数 |
| `AMAP_QPS_RETRY_DELAY_SECONDS` | `1.0` | QPS 重试前的等待秒数 |
| `AMAP_ENRICH_DETAIL_LIMIT` | `3` | 每次 POI 搜索最多拉取的详情条数；`0` 禁用详情富化 |

POI 详情拉取失败不再击穿整次搜索，仅返回未富化的 POI。限流只在单进程内生效，多 Worker 部署时应按 Worker 数相应调低 `AMAP_QPS_BUDGET`。

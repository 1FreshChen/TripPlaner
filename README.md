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

复制根目录 `.env.example` 到后端和前端对应位置：

```bash
copy .env.example backend\.env
copy .env.example frontend\.env
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
copy ..\.env.example .env
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

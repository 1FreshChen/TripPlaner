# 下次承接运行文档

更新时间：2026-05-30

## 当前状态摘要

项目已按第十三章“智能旅行助手”的架构完成基础 MVP 源码搭建，并在无 API Key 模式下完成本地运行验证。用户指定目录为：

```text
D:\Agent-demo
```

## 已完成内容

- 后端 FastAPI 项目骨架。
- Pydantic 数据模型：
  - `Location`
  - `Attraction`
  - `Meal`
  - `Hotel`
  - `Budget`
  - `WeatherInfo`
  - `DayPlan`
  - `TripPlan`
  - `TripPlanRequest`
- 多 Agent 协作骨架：
  - `AttractionSearchAgent`
  - `WeatherQueryAgent`
  - `HotelAgent`
  - `PlannerAgent`
  - `TripPlannerAgent`
- 外部服务适配层：
  - 高德地图 Web 服务封装。
  - Unsplash 图片服务封装。
  - OpenAI-compatible LLM 服务封装。
- 无 API Key 时的 mock 兜底规划。
- 后端 API：
  - `GET /api/health`
  - `POST /api/trip/plan`
- 前端 Vue 3 + TypeScript + Vite 项目骨架。
- 前端页面：
  - `Home.vue`：旅行需求表单、加载进度。
  - `Result.vue`：行程概览、预算、地图区域、每日行程、天气、编辑、导出。
- 文档：
  - `README.md`
  - `.env.example`
  - `docs/API_KEYS.md`
  - `docs/PROJECT_STATUS.md`
  - `docs/superpowers/plans/2026-05-29-smart-trip-assistant.md`

## 当前验证结果

后端依赖已安装：

```bash
cd "C:\Users\czy\Documents\New project 4\Agent-demo\backend"
python -m pip install -r requirements.txt
```

后端语法检查通过：

```bash
python -m compileall app tests -q
```

后端测试通过：

```bash
python -m pytest tests -q
```

结果：

```text
6 passed, 1 warning in 0.36s
```

前端依赖已安装：

```bash
cd "C:\Users\czy\Documents\New project 4\Agent-demo\frontend"
npm install
```

前端构建命令：

```bash
npm run build
```

前端构建已通过：

```text
✓ built in 1m 10s
```

已修复 `frontend/src/views/Home.vue` 中的 Vue Router 类型问题：删除重复的 `state: { tripPlan }`，使用 `sessionStorage` 传递行程数据。

本地服务已启动并验证：

```text
后端：http://127.0.0.1:8000
前端：http://127.0.0.1:5173
```

健康接口：

```text
GET /api/health
{"status":"ok","service":"trip-planner"}
```

无 Key mock 规划接口：

```text
POST /api/trip/plan
城市：北京
天数：3
天气条目：3
总预算：1373
首个景点：故宫博物院
```

## 下次优先任务

1. 填入 `AMAP_API_KEY`，验证真实 POI 和天气数据。

```bash
Copy-Item D:\Agent-demo\.env.example D:\Agent-demo\backend\.env
```

2. 填入 `VITE_AMAP_JS_KEY` 和可选的 `VITE_AMAP_SECURITY_CODE`，验证真实地图标记。

```bash
Copy-Item D:\Agent-demo\frontend\.env.example D:\Agent-demo\frontend\.env
```

3. 填入 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，验证真实 LLM JSON 输出。

4. 按需要继续接入 HelloAgents 原生 `SimpleAgent` / `MCPTool` 和高德 MCP 服务。

5. 评估前端依赖安全更新：

```bash
cd D:\Agent-demo\frontend
npm audit
```

不要直接执行 `npm audit fix --force`，先检查是否会升级主版本。

## 再次启动

启动后端：

```bash
cd D:\Agent-demo\backend
python -m uvicorn app.api.main:app --reload
```

如果 `uvicorn` 不在 PATH 中：

```bash
python run.py
```

启动前端：

```bash
cd D:\Agent-demo\frontend
npm run dev
```

打开：

```text
http://localhost:5173
```

## API Key 待填写

文件：

```text
D:\Agent-demo\backend\.env
D:\Agent-demo\frontend\.env
```

可以先从根目录模板复制：

```powershell
Copy-Item D:\Agent-demo\.env.example D:\Agent-demo\backend\.env
Copy-Item D:\Agent-demo\frontend\.env.example D:\Agent-demo\frontend\.env
```

待填写：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `AMAP_API_KEY`
- `UNSPLASH_ACCESS_KEY`
- `VITE_AMAP_JS_KEY`
- `VITE_AMAP_SECURITY_CODE`

无 Key 时后端会使用 mock 数据，前端地图会使用坐标卡片兜底。

## 重要文件入口

- 后端入口：`backend/app/api/main.py`
- 旅行规划接口：`backend/app/api/routes/trip.py`
- 数据模型：`backend/app/models/schemas.py`
- 多 Agent 协作：`backend/app/agents/trip_planner.py`
- Agent 提示词：`backend/app/agents/prompts.py`
- 高德服务：`backend/app/services/amap_service.py`
- LLM 服务：`backend/app/services/llm_service.py`
- Unsplash 服务：`backend/app/services/unsplash_service.py`
- 首页：`frontend/src/views/Home.vue`
- 结果页：`frontend/src/views/Result.vue`
- 前端 API：`frontend/src/services/api.ts`
- 前端类型：`frontend/src/types/index.ts`

## 注意事项

- `node_modules` 不建议提交或长期保存；下次如缺失，重新运行 `npm install`。
- Python 依赖本轮安装到了用户级 Python 环境，pip 输出提示 Anaconda 环境里存在 pydantic 版本冲突警告；当前项目后端测试已通过。
- `npm install` 当前报告 `4 vulnerabilities (3 moderate, 1 critical)`，尚未执行破坏性自动升级。
- 前端构建存在 bundle 大小警告，基础 MVP 可运行，后续可做路由懒加载优化。
- 当前会话没有可用的 in-app Browser 工具，因此前端验证使用本地 HTTP 请求完成，未做浏览器截图级视觉复核。

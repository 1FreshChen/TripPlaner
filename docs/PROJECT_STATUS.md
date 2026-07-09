# 项目完成清单

更新时间：2026-05-30

## 已完成

- [x] 阶段 0：项目目录和文档
  - [x] 创建 `backend/`、`frontend/`、`docs/`。
  - [x] 编写 `.env.example`。
  - [x] 记录需要用户自行填写的 API Key。
  - [x] 编写 README 启动说明。
- [x] 阶段 1：后端基础框架
  - [x] 创建 FastAPI 应用入口。
  - [x] 创建 CORS 配置。
  - [x] 创建 `/api/health` 和 `/api/trip/plan`。
- [x] 阶段 2：数据模型设计
  - [x] 实现 `Location`、`Attraction`、`Meal`、`Hotel`、`Budget`、`WeatherInfo`。
  - [x] 实现 `DayPlan`、`TripPlan`、`TripPlanRequest`。
  - [x] 实现日期校验和温度字符串解析。
- [x] 阶段 3：后端 API
  - [x] `POST /api/trip/plan` 返回标准 `TripPlan`。
  - [x] 无 API Key 时自动返回 mock 规划结果。
- [x] 阶段 4：多 Agent 骨架
  - [x] 实现 `AttractionSearchAgent`。
  - [x] 实现 `WeatherQueryAgent`。
  - [x] 实现 `HotelAgent`。
  - [x] 实现 `PlannerAgent`。
  - [x] 实现 `TripPlannerAgent` 协作流程。
- [x] 阶段 5：外部服务接入位
  - [x] 实现高德 POI/天气 HTTP 服务封装。
  - [x] 实现 Unsplash 图片服务封装。
  - [x] 实现 OpenAI-compatible LLM 服务封装。
  - [x] 保留无 Key 兜底逻辑。
- [x] 阶段 6：前端基础应用
  - [x] 创建 Vue 3 + TypeScript + Vite 结构。
  - [x] 创建类型定义和 API 封装。
  - [x] 创建路由、首页和结果页。
- [x] 阶段 7：首页表单
  - [x] 目的地、日期、偏好、预算、交通、住宿表单。
  - [x] 提交时调用后端生成行程。
  - [x] 添加进度条和状态提示。
- [x] 阶段 8：结果页展示
  - [x] 行程概览。
  - [x] 预算明细。
  - [x] 每日行程。
  - [x] 天气信息。
- [x] 阶段 9：地图可视化
  - [x] 接入高德 JS API 加载逻辑。
  - [x] 无前端 Key 时展示本地坐标路线视图。
  - [x] 填入 `VITE_AMAP_JS_KEY` 后可显示高德地图标记。
- [x] 阶段 10：行程编辑
  - [x] 支持进入编辑模式。
  - [x] 支持上移、下移、删除景点。
  - [x] 支持保存和取消。
- [x] 阶段 11：导出功能
  - [x] 使用 `html2canvas` 导出图片。
  - [x] 使用 `jspdf` 导出 PDF。
  - [x] 导出时隐藏动态地图，保留文字和行程内容。
- [x] 阶段 12：基础测试
  - [x] 编写后端模型测试。
  - [x] 编写后端规划器测试。
  - [x] 编写 API 测试。
  - [x] 安装后端依赖并运行 `python -m pytest tests -q`，结果为 `6 passed, 1 warning`。

## 当前已知问题

- [x] 前端 `npm run build` 的 TypeScript 错误已修复。
  - 文件：`frontend/src/views/Home.vue`
  - 修复：删除 `router.push` 中重复的 `state: { tripPlan }`，继续使用 `sessionStorage` 传递行程数据。
- [ ] 前端生产构建存在 bundle 大小警告。
  - 当前主 bundle 约 `2.16 MB`，gzip 后约 `664 KB`。
  - 基础 MVP 可运行，后续可通过路由懒加载和拆分依赖优化。
- [ ] `npm install` 报告 `4 vulnerabilities (3 moderate, 1 critical)`。
  - 未自动执行 `npm audit fix --force`，避免引入破坏性依赖升级。
- [ ] 后端测试存在一个 FastAPI `TestClient` 弃用警告。
  - 当前不影响基础功能。

## 待完成

- [x] 安装后端依赖后运行完整 `pytest`。
- [x] 修复 `Home.vue` 的 Vue Router history state 类型问题。
- [x] 修复后重新运行 `npm run build`。
- [x] 启动本地 FastAPI 和 Vite 服务。
- [x] 验证 `GET /api/health` 返回 `{"status":"ok","service":"trip-planner"}`。
- [x] 验证无 Key 模式下 `POST /api/trip/plan` 可返回北京 3 日 mock 行程。
- [x] 验证前端首页 `http://127.0.0.1:5173/` 返回应用入口。
- [ ] 填入高德 Web 服务 Key，验证真实 POI 和天气。
- [ ] 填入高德 JS API Key，验证真实地图。
- [ ] 填入 LLM API Key，验证真实模型生成 JSON。
- [ ] 按本机可用的 HelloAgents 包调整真实 `SimpleAgent` / `MCPTool` 集成。
- [ ] 若需要完全复现章节中的 MCP 方案，安装并验证 `@sugarforever/amap-mcp-server` 或等价 MCP 服务。
- [ ] 加强前端表单异常提示和移动端细节。
- [ ] 添加前端单元测试或 e2e 测试。

## 下次继续建议

1. 填 `AMAP_API_KEY`，验证后端真实 POI 和天气数据。
2. 填 `VITE_AMAP_JS_KEY`，验证前端真实地图标记。
3. 填 `LLM_API_KEY`，把 `PlannerAgent` 的 LLM JSON 输出调稳定。
4. 再接入 HelloAgents 原生 `SimpleAgent` 和 MCPTool，替换当前的工程化适配层。
5. 根据 `npm audit` 明细评估前端依赖升级方案。

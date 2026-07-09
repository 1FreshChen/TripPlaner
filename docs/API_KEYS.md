# API Key 填写记录

当前项目中涉及具体 API Key 的位置已全部留空。你需要按下面清单自行填写。

## 后端 `.env`

文件路径：`D:\Agent-demo\backend\.env`

| 环境变量 | 用途 | 当前状态 |
| --- | --- | --- |
| `LLM_API_KEY` | 调用大模型生成结构化行程 | 待填写 |
| `LLM_BASE_URL` | OpenAI-compatible API 地址 | 可使用默认值 |
| `LLM_MODEL` | 模型名称 | 可使用默认值 |
| `AMAP_API_KEY` | 高德地图 Web 服务 Key，查询 POI 和天气 | 待填写 |
| `UNSPLASH_ACCESS_KEY` | 搜索景点图片 | 可选，待填写 |
| `ENABLE_EXTERNAL_SERVICES` | 是否启用外部服务 | 默认 `true` |
| `FRONTEND_ORIGIN` | 前端开发服务器地址 | 默认 `http://localhost:5173` |

## 前端 `.env`

文件路径：`D:\Agent-demo\frontend\.env`

| 环境变量 | 用途 | 当前状态 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | 后端 API 地址 | 默认 `http://localhost:8000/api` |
| `VITE_AMAP_JS_KEY` | 高德地图 JS API Key | 待填写 |
| `VITE_AMAP_SECURITY_CODE` | 高德地图 JS API 安全密钥 | 按高德控制台要求填写 |

## 无 Key 时的行为

- 后端不会报错退出，会使用本地 mock 数据生成可演示行程。
- 前端地图区域会显示本地坐标路线视图；填入 `VITE_AMAP_JS_KEY` 后会加载高德地图。
- Unsplash Key 为空时，景点图片字段保持为空，不影响主流程。

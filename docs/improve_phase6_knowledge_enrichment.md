# Phase 6: 外部知识增强

## 改进的目的

当前系统生成的行程存在严重的"知识空洞"：

### 问题表现

| 字段 | 当前值 | 问题 |
|------|--------|------|
| `attraction.description` | `"根据XX偏好从高德地图搜索得到的目的地。"` | 完全无意义的模板文本 |
| `attraction.visit_duration` | 固定值 `120`（分钟） | 所有景点都是 2 小时，不符合现实 |
| `attraction.ticket_price` | 固定值 `0` | 不真实 |
| `meal.description` | 无或泛泛 | 没有具体菜品名 |
| `overall_suggestions` | 通用模板 | 无城市特色 |
| `day.description` | 仅列举景点 | 无游览路线逻辑 |

### 根因

高德地图 POI 搜索 API 返回的数据非常基础：

```json
{
  "name": "故宫博物院",
  "address": "北京市东城区景山前街4号",
  "location": "116.397,39.916",
  "type": "旅游景点;国家级景点",
  "rating": "4.8"
}
```

没有描述、没有开放时间、没有游览建议。`poi_to_attraction()` 方法（`amap_service.py` 第 94 行）只能填入占位符。

## 改进的内容

### 方案一：利用 LLM 训练数据中的知识（推荐，零额外成本）

LLM（GPT-4 / DeepSeek-V3 等）训练数据中包含了大量知名景点的信息。通过改进 prompt，让 LLM 用自己的知识补充 Amap 数据的不足：

**在系统 prompt 中增加指令**：

```
## 知识补充要求

对于每个景点，请基于你的训练知识生成以下内容（不要使用 API 返回的占位文本）：

1. **description（80-200字）**：
   - 该景点最值得看的具体亮点（命名具体的建筑/展品/自然景观）
   - 推荐的游览路线或顺序
   - 一个实用的 tips（如：是否需要预约、最佳拍照点、避开人群的时间段）
   - 如果你对某个景点了解有限，请标注"建议出发前查询官方最新信息"

2. **visit_duration（分钟）**：
   - 根据景点的实际规模估算，不要使用默认值
   - 大型博物馆/园林：180-300 分钟
   - 中型景点/公园：120-180 分钟
   - 小型景点/纪念馆/特色街区：60-120 分钟
   - 观景台/拍照点：30-60 分钟

3. **ticket_price（元）**：
   - 根据你的知识估算实际票价
   - 如不确定，标注一个合理范围并注明"以官方公示为准"

4. **餐饮推荐**：
   - 推荐该景点周边的具体餐厅名或必吃菜品（菜名要具体）
   - 说明大致的价格区间
```

### 方案二：景点描述批量增强（可选，消耗额外 token）

在 Stage 0 数据收集完成后、传给 PlannerAgent 之前，对 Amap 返回的 POI 做一次 LLM 增强：

```python
# 在 llm_planner.py 中
async def _enrich_attractions(
    self, attractions: List[Attraction], city: str
) -> List[Attraction]:
    """
    对 Amap 返回的景点做批量 LLM 知识增强
    替换占位描述、修正游览时长和票价
    """
    if not self.enable_enrichment:
        return attractions

    # 批量构建 prompt（一次调用处理多个，节省 token）
    prompt = f"请为以下{city}的景点补充实用信息：\n"
    for i, attr in enumerate(attractions):
        prompt += f"{i+1}. {attr.name}（地址：{attr.address}，类型：{attr.category}）\n"
    prompt += "\n返回 JSON，为每个景点补充：description(80-150字),visit_duration(分钟),ticket_price(元)"

    result = await self.llm_service.generate_json(
        system_prompt=ENRICHMENT_PROMPT,
        user_prompt=prompt
    )

    # 合并增强数据
    for i, attr in enumerate(attractions):
        if enriched := result.get(f"attraction_{i+1}"):
            attr.description = enriched.get("description", attr.description)
            attr.visit_duration = enriched.get("visit_duration", attr.visit_duration)
            attr.ticket_price = enriched.get("ticket_price", attr.ticket_price)

    return attractions
```

该方案每次旅行规划额外消耗约 500-2000 token（取决于景点数量），通过 `ENABLE_ATTRACTION_ENRICHMENT` 开关控制。

### 方案三：预置城市知识库（可选，零 token 消耗）

创建本地知识库文件，为热门城市预置实用旅行 tips：

**文件**：`backend/app/data/city_knowledge.json`

```json
{
  "北京": {
    "local_tips": [
      "故宫需提前7天在官网预约，现场不售票",
      "八达岭长城建议乘坐S2线市郊铁路，比自驾快且便宜",
      "王府井小吃街价格偏高，本地人更推荐去牛街",
      "北京地铁高峰期（7:30-9:00, 17:30-19:00）非常拥挤，建议错峰",
      "春季（4月）沙尘暴高发，建议备口罩和墨镜"
    ],
    "must_try_foods": [
      {"name": "卤煮火烧", "where": "小肠陈（前门店）", "price": "30-50元"},
      {"name": "豆汁儿配焦圈", "where": "锦芳小吃（磁器口）", "price": "10-20元"},
      {"name": "炸酱面", "where": "海碗居（增光路）", "price": "25-40元"}
    ],
    "seasons": {
      "spring": "4月有沙尘暴，5月气候宜人，适合户外游览",
      "summer": "7-8月高温多雨，建议室内景点为主",
      "autumn": "9-10月最佳旅游季节，秋高气爽、红叶季",
      "winter": "11-2月寒冷干燥，故宫雪景是亮点但需穿厚羽绒服"
    },
    "transportation_hacks": [
      "地铁用'亿通行'APP扫码，公交用'北京一卡通'",
      "天安门区域步行可达故宫、国家博物馆、前门，无需打车"
    ]
  },
  "上海": { ... },
  "杭州": { ... }
}
```

在 `MemoryRecall` 或 `build_planner_query()` 中加载匹配城市的知识，注入 prompt 的上下文。

### 方案对比

| 方案 | 成本 | 覆盖率 | 准确性 | 维护负担 |
|------|------|--------|--------|---------|
| 方案一：LLM 自带知识 | 零额外 | 知名景点全覆盖 | 中（LLM 可能幻觉） | 无 |
| 方案二：批量增强 | 500-2000 token/次 | 与方案一相同 | 高（显式调用 LLM） | 无 |
| 方案三：本地知识库 | 零 | 仅预置的城市 | 高（人工审核） | 需要持续维护 |

**建议**：方案一必做，方案二作为可选增强，方案三作为长期优化（积累用户反馈后逐步完善）。

## 改进的方法

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/agents/prompts.py` | 在 `PLANNER_AGENT_PROMPT` 中添加"知识补充要求"段落（方案一） |
| `backend/app/agents/llm_planner.py` | 添加可选的 `_enrich_attractions()` 方法（方案二） |
| `backend/app/services/amap_service.py` | 修改 `poi_to_attraction()`：移除占位描述，改为空字符串（让 LLM 填充） |
| `backend/app/config.py` | 新增 `ENABLE_ATTRACTION_ENRICHMENT` 配置项（方案二） |

### 新建文件（可选）

`backend/app/data/city_knowledge.json` —— 预置城市知识库（方案三）

### 实施步骤

**必做（方案一）**：
1. 修改 `PLANNER_AGENT_PROMPT`，添加知识补充要求
2. 修改 `poi_to_attraction()`：不再生成占位描述

**可选（方案二）**：
1. 在 `llm_planner.py` 中实现 `_enrich_attractions()`
2. 添加配置开关
3. 在 `execute()` 的数据准备阶段调用

**长期（方案三）**：
1. 创建 `city_knowledge.json`
2. 在 `build_planner_query()` 中加载对应城市的知识
3. 根据用户反馈持续补充

### 验证方式

1. 生成"北京 3 天"行程
2. 抽查 5 个景点描述：
   - [ ] 每个描述 ≥ 80 字
   - [ ] 内容具体（非模板文本）
   - [ ] 包含至少一个实用 tips
3. 检查 `visit_duration`：不同景点的值应不同（不再全是 120）
4. 检查 `ticket_price`：不应全是 0
5. 检查 `overall_suggestions`：包含城市特定的建议（如"故宫需提前预约"）
6. 对比增强开关开启/关闭时的输出质量差异

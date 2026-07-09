# Phase 3: 工具调用系统

> **所属项目**: 智能旅行助手 Harness 架构升级
> **依赖**: Phase 1（Redis 和配置，工具层本身独立于 DB）
> **被依赖**: Phase 4（PlannerAgent 的 LLM tool-calling 依赖 ToolRegistry）

---

## 目标

将当前 Service 层直接调用的模式升级为正式的 **Tool Registry** 系统：每个工具拥有标准化的 JSON Schema 参数定义、统一的执行中间件（超时/重试/缓存）、以及 LLM 动态工具选择能力。

---

## 核心设计

```
ToolRegistry (单例)                    ToolExecutor
     │                               (超时/重试/缓存)
     │
     ├── AmapPOISearchTool           parameters_schema (JSON Schema)
     ├── AmapWeatherTool             to_openai_tool_definition()
     ├── HotelSearchTool
     ├── BudgetCalculatorTool
     └── UnsplashImageTool

LLM Agent (PlannerAgent)
     │
     ├── 获取所有工具定义 → 发送给 LLM
     ├── LLM 决定调用哪些工具
     ├── 执行工具并将结果回传
     └── LLM 综合所有结果生成最终行程
```

---

## 3.1 BaseTool 接口

```python
# backend/app/tools/base.py

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseTool(ABC):
    """
    统一工具接口。
    每个工具必须定义：
      - name:          全局唯一标识
      - description:   自然语言描述（给 LLM 看的）
      - parameters:    参数 JSON Schema（OpenAI function-calling 兼容）
      - execute(**kwargs): 异步执行方法
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """
        JSON Schema 格式的参数定义。
        示例：
        {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "搜索关键词"},
                "city":     {"type": "string", "description": "城市名称"},
            },
            "required": ["keywords", "city"],
        }
        """
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        """执行工具，返回结构化结果字典"""
        ...

    def to_openai_function(self) -> Dict[str, Any]:
        """导出为 OpenAI function-calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        """通用序列化（供前端展示可用工具列表）"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
```

---

## 3.2 ToolRegistry

```python
# backend/app/tools/registry.py

from typing import Dict, List, Optional


class ToolRegistry:
    """工具注册中心（单例模式）"""

    _instance: Optional['ToolRegistry'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools: Dict[str, BaseTool] = {}
        return cls._instance

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_all(self) -> List[BaseTool]:
        return list(self._tools.values())

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        """获取所有工具定义，供 LLM tool-calling 使用"""
        return [t.to_openai_function() for t in self._tools.values()]

    def find_by_keyword(self, keyword: str) -> List[BaseTool]:
        """关键词搜索工具（可用于前端展示或 LLM 路由）"""
        kw = keyword.lower()
        return [
            t for t in self._tools.values()
            if kw in t.name.lower() or kw in t.description.lower()
        ]
```

---

## 3.3 各工具的参数 Schema

```python
# --- AmapPOISearchTool ---
name: "amap_poi_search"
description: "搜索指定城市的 POI（景点/餐厅/商场等），返回名称、地址、坐标、评分等信息。"
parameters: {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "string",
            "description": "搜索关键词，如 '博物馆'、'公园'、'美食街'。多个关键词用 | 分隔",
        },
        "city": {
            "type": "string",
            "description": "城市名称，如 '北京'、'上海'",
        },
        "offset": {
            "type": "integer",
            "description": "返回结果数量上限，默认 10，最大 25",
            "default": 10,
            "minimum": 1,
            "maximum": 25,
        },
    },
    "required": ["keywords", "city"],
}

# --- AmapWeatherTool ---
name: "amap_weather"
description: "查询指定城市未来数天的天气预报，包含白天/夜间天气、温度、风力风向。"
parameters: {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "城市名称，如 '杭州'",
        },
    },
    "required": ["city"],
}

# --- HotelSearchTool ---
name: "hotel_search"
description: "搜索指定城市的酒店住宿信息，可按类型过滤。"
parameters: {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "城市名称",
        },
        "hotel_type": {
            "type": "string",
            "description": "酒店类型，如 '经济型酒店'、'精品酒店'、'豪华酒店'",
            "default": "经济型酒店",
        },
        "limit": {
            "type": "integer",
            "description": "返回酒店数量",
            "default": 5,
            "minimum": 1,
            "maximum": 10,
        },
    },
    "required": ["city"],
}

# --- BudgetCalculatorTool ---
name: "budget_calculator"
description: "根据每日行程、交通方式和住宿天数计算旅行预算。"
parameters: {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "旅行天数",
            "minimum": 1,
        },
        "transportation": {
            "type": "string",
            "description": "交通方式：步行|公共交通|地铁|打车|自驾",
            "enum": ["步行", "公共交通", "地铁", "打车", "自驾"],
        },
        "attraction_count": {
            "type": "integer",
            "description": "景点总数",
        },
        "avg_ticket_price": {
            "type": "integer",
            "description": "平均门票价格（元）",
        },
        "hotel_price_per_night": {
            "type": "integer",
            "description": "每晚酒店费用（元）",
        },
        "meal_level": {
            "type": "string",
            "description": "餐饮档次：经济|中等|舒适|高",
            "enum": ["经济", "中等", "舒适", "高"],
        },
    },
    "required": ["days", "transportation", "attraction_count", "hotel_price_per_night", "meal_level"],
}

# --- UnsplashImageTool ---
name: "unsplash_image"
description: "搜索旅游景点相关的图片，用于行程可视化展示。"
parameters: {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "图片搜索关键词（景点名称 + 城市名效果最佳）",
        },
        "count": {
            "type": "integer",
            "description": "返回图片数量",
            "default": 1,
            "minimum": 1,
            "maximum": 5,
        },
    },
    "required": ["query"],
}
```

---

## 3.4 工具实现示例

```python
# backend/app/tools/implementations/amap_search.py

from app.tools.base import BaseTool
from app.services.amap_service import AmapService


class AmapPOISearchTool(BaseTool):
    name = "amap_poi_search"
    description = "搜索指定城市的 POI（景点/餐厅/商场等），返回名称、地址、坐标、评分等信息。"

    parameters = {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "string",
                "description": "搜索关键词，如 '博物馆'、'公园'、'美食街'。多个关键词用 | 分隔",
            },
            "city": {
                "type": "string",
                "description": "城市名称，如 '北京'、'上海'",
            },
            "offset": {
                "type": "integer",
                "description": "返回结果数量上限，默认 10，最大 25",
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": ["keywords", "city"],
    }

    def __init__(self, amap_service: AmapService):
        self._amap = amap_service

    async def execute(self, keywords: str, city: str, offset: int = 10, **kwargs) -> dict:
        pois = self._amap.search_pois(keywords, city, offset)
        return {"pois": pois, "count": len(pois), "city": city}
```

---

## 3.5 ToolExecutor — 执行中间件

```python
# backend/app/tools/executor.py

import asyncio
import hashlib
import json
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ToolExecutor:
    """
    工具执行中间件：每个工具调用经过此层进行：
      1. 缓存检查（Redis）
      2. 超时控制
      3. 重试
    """

    def __init__(self, cache: Optional['ToolCache'] = None, default_timeout: float = 30.0):
        self._cache = cache
        self._default_timeout = default_timeout

    async def execute(self, tool: BaseTool, **kwargs) -> Dict[str, Any]:
        cache_key = self._build_cache_key(tool.name, kwargs)

        # 1. 查缓存
        if self._cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        # 2. 执行（带超时 + 重试）
        last_error = None
        for attempt in range(3):
            try:
                start = time.time()
                result = await asyncio.wait_for(
                    tool.execute(**kwargs),
                    timeout=self._default_timeout,
                )
                duration_ms = (time.time() - start) * 1000
                logger.info("工具 '%s' 执行成功, 耗时 %.0fms", tool.name, duration_ms)

                # 3. 写入缓存
                if self._cache and result:
                    await self._cache.set(cache_key, result, ttl=300)

                return result
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"工具 '{tool.name}' 超时 ({self._default_timeout}s)")
            except Exception as e:
                last_error = e

            if attempt < 2:
                delay = 1.0 * (2 ** attempt)
                logger.warning("工具 '%s' 第 %d 次失败, %0.1fs后重试: %s", tool.name, attempt + 1, delay, last_error)
                await asyncio.sleep(delay)

        logger.error("工具 '%s' 所有重试耗尽: %s", tool.name, last_error)
        return {"error": str(last_error), "tool": tool.name, "success": False}

    def _build_cache_key(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        raw = json.dumps({"tool": tool_name, "args": kwargs}, sort_keys=True, ensure_ascii=False)
        return f"tool_cache:{hashlib.md5(raw.encode()).hexdigest()}"
```

---

## 3.6 ToolCache — Redis 缓存

```python
# backend/app/tools/cache.py

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ToolCache:
    """Redis 工具结果缓存"""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def get(self, key: str) -> Optional[Dict]:
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("缓存读取失败: %s", e)
            return None

    async def set(self, key: str, value: Dict, ttl: int = 300) -> None:
        try:
            await self._redis.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        except Exception as e:
            logger.warning("缓存写入失败: %s", e)
```

---

## 3.7 LLM Tool-Calling 集成

```python
# backend/app/services/llm_service.py 扩展

class LLMService:
    # ... 现有代码 ...

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        max_tool_rounds: int = 5,
    ) -> Tuple[str, List[Dict], 'TokenUsage']:
        """
        支持 tool-calling 的聊天接口。
        返回: (最终回复文本, 所有工具调用记录, Token 用量)
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tool_calls_log = []
        total_usage = TokenUsage(model=self.model, provider=self._infer_provider())

        for _ in range(max_tool_rounds):
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.4,
                },
                timeout=120,
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]

            # 累加 token 用量
            usage = body.get("usage", {})
            total_usage.prompt_tokens += usage.get("prompt_tokens", 0)
            total_usage.completion_tokens += usage.get("completion_tokens", 0)
            total_usage.total_tokens += usage.get("total_tokens", 0)

            if message.get("tool_calls"):
                messages.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": message["tool_calls"],
                })
                for tc in message["tool_calls"]:
                    tool_name = tc["function"]["name"]
                    tool_args = json.loads(tc["function"]["arguments"])
                    tool_calls_log.append({
                        "tool": tool_name,
                        "arguments": tool_args,
                        "id": tc["id"],
                    })
                # 返回 tool_calls_log，由上层 ToolExecutor 执行工具后注入结果
                return message.get("content") or "", tool_calls_log, total_usage
            else:
                return message["content"], tool_calls_log, total_usage

        return messages[-1].get("content", ""), tool_calls_log, total_usage

    def _infer_provider(self) -> str:
        if "openai" in self.base_url:
            return "openai"
        if "deepseek" in self.base_url:
            return "deepseek"
        return "custom"
```

---

## 3.8 工具注册引导

```python
# backend/app/tools/bootstrap.py

from app.tools.registry import ToolRegistry
from app.tools.implementations.amap_search import AmapPOISearchTool
from app.tools.implementations.amap_weather import AmapWeatherTool
from app.tools.implementations.hotel_search import HotelSearchTool
from app.tools.implementations.budget_calculator import BudgetCalculatorTool
from app.tools.implementations.image_search import UnsplashImageTool


def bootstrap_tools(amap_service, unsplash_service) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(AmapPOISearchTool(amap_service))
    registry.register(AmapWeatherTool(amap_service))
    registry.register(HotelSearchTool(amap_service))
    registry.register(BudgetCalculatorTool())
    registry.register(UnsplashImageTool(unsplash_service))
    return registry
```

---

## 关键文件清单

| 文件 | 操作 |
|---|---|
| `backend/app/tools/__init__.py` | 新建 |
| `backend/app/tools/base.py` | 新建：BaseTool 抽象 |
| `backend/app/tools/registry.py` | 新建：ToolRegistry 单例 |
| `backend/app/tools/executor.py` | 新建：ToolExecutor 中间件 |
| `backend/app/tools/cache.py` | 新建：Redis 缓存 |
| `backend/app/tools/bootstrap.py` | 新建：工具注册引导 |
| `backend/app/tools/implementations/__init__.py` | 新建 |
| `backend/app/tools/implementations/amap_search.py` | 新建 |
| `backend/app/tools/implementations/amap_weather.py` | 新建 |
| `backend/app/tools/implementations/hotel_search.py` | 新建 |
| `backend/app/tools/implementations/budget_calculator.py` | 新建 |
| `backend/app/tools/implementations/image_search.py` | 新建 |
| `backend/app/services/llm_service.py` | 扩展：chat_with_tools 方法 |
| `backend/tests/test_tools.py` | 新建 |

---

## 验证方式

1. 注册所有工具，调用 `ToolRegistry.list_all()` 验证返回 5 个工具
2. 验证 `to_openai_function()` 输出符合 OpenAI function-calling 格式
3. 验证 `ToolExecutor.execute()` 在超时时触发重试并最终返回 error
4. 验证 `ToolCache` get/set 流程正常工作
5. Mock LLM 返回 tool_calls，验证 `chat_with_tools()` 正确解析
6. `pytest tests/test_tools.py -v`

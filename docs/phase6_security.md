# Phase 6: 安全治理

> **所属项目**: 智能旅行助手 Harness 架构升级
> **依赖**: Phase 1（audit schema 数据库表，ContentFilter/Encryption/Limiter 独立无依赖）
> **被依赖**: 无（横向切入所有层的横切关注点）

---

## 目标

为项目增加多层安全防护：输入内容过滤（防注入）、请求频率限制、全链路审计日志、API Key 加密存储、LLM Token 成本追踪、LLM 输出内容审查。

---

## 安全层架构

```
Request → [RateLimit] → [AuditMiddleware] → [Pydantic+ContentFilter] → Handler
                                                                           │
Response ← [LLMOutputReview] ←────────────────────────────────────────────┘
                                       │
                                audit.event_log
                                audit.token_usage
```

---

## 6.1 ContentFilter — 输入清洗 + 注入检测 + 输出审查

```python
# backend/app/services/content_filter.py

import re
import bleach

# 允许的 HTML 标签（纯文本场景下为空）
ALLOWED_TAGS: list = []
ALLOWED_ATTRIBUTES: dict = {}

# Prompt 注入检测模式
INJECTION_PATTERNS = [
    r"(?i)(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|messages?))",
    r"(?i)(you\s+are\s+now\s+(DAN|free|unrestricted))",
    r"(?i)(system\s*[:：]\s*)",
    r"(?i)(<\|im_start\|>)",
    r"(?i)(\[INST\])",
    r"(?i)(\[SYS\])",
    r"(?i)(prompt\s*injection)",
    r"(?i)(jailbreak)",
]

# 敏感内容关键词（用于 LLM 输出审查）
SENSITIVE_KEYWORDS = [
    "暴力", "色情", "赌博", "毒品",
    "political_sensitive",   # 占位，实际使用时需要更完善的词表
]


def sanitize_text(value: str) -> str:
    """
    清洗输入文本：
    1. 去除所有 HTML 标签
    2. 标准化空白字符
    """
    if not isinstance(value, str):
        return value
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
    return " ".join(cleaned.split())


def detect_injection(value: str) -> bool:
    """检测输入是否包含 prompt 注入模式"""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, value):
            return True
    return False


def filter_llm_output(text: str) -> tuple[str, list[str]]:
    """
    审查 LLM 输出。
    返回: (过滤后文本, 触发的问题列表)
    """
    issues = []
    lower = text.lower()
    for keyword in SENSITIVE_KEYWORDS:
        if keyword.lower() in lower:
            text = text.replace(keyword, "[已过滤]")
            issues.append(f"检测到敏感内容: {keyword}")
    return text, issues
```

### 集成到 Pydantic Validator

```python
# backend/app/models/schemas.py 新增

from app.services.content_filter import sanitize_text, detect_injection

class TripPlanRequest(BaseModel):
    # ... 现有字段 ...

    @field_validator("city", "preferences", "transportation", "accommodation")
    @classmethod
    def sanitize_and_check(cls, v: str) -> str:
        v = sanitize_text(v)
        if detect_injection(v):
            raise ValueError("输入包含潜在有害内容")
        return v
```

---

## 6.2 AuditMiddleware — 全链路审计

```python
# backend/app/api/middlewares/audit.py

import time
import uuid
import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("audit")


class AuditMiddleware(BaseHTTPMiddleware):
    """
    ASGI 审计中间件：记录每个 HTTP 请求的关键信息。
    写入 audit.event_log 表（异步、非阻塞）。
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        # 异步写入审计日志（不阻塞响应）
        await self._log_event(
            event_type="http_request",
            action=f"{request.method} {request.url.path}",
            details={
                "method": request.method,
                "path": request.url.path,
                "query_string": str(request.query_params),
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "request_id": request_id,
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent", ""),
            duration_ms=duration_ms,
        )

        response.headers["X-Request-ID"] = request_id
        return response

    async def _log_event(self, **kwargs) -> None:
        try:
            from app.database import get_session_factory
            from app.models.db_models import AuditEvent

            factory = get_session_factory()
            async with factory() as db:
                event = AuditEvent(**kwargs)
                db.add(event)
                await db.commit()
        except Exception as e:
            # 审计失败不应影响主流程
            logger.warning("审计日志写入失败: %s", e)
```

### 审计事件类型

| 事件类型 | 触发时机 | severity |
|---|---|---|
| `trip_plan_created` | 新计划创建成功 | info |
| `trip_plan_updated` | 计划编辑保存 | info |
| `trip_plan_archived` | 计划软删除 | info |
| `agent_execution_started` | Agent 开始执行 | debug |
| `agent_execution_completed` | Agent 执行成功 | info |
| `agent_execution_failed` | Agent 执行失败 | warning |
| `tool_call_invoked` | 工具被调用 | debug |
| `llm_request` | LLM API 调用 | debug |
| `content_filter_triggered` | 检测到注入 | warning |
| `rate_limit_exceeded` | 频率限制触发 | warning |

---

## 6.3 RateLimitMiddleware — 频率限制

```python
# backend/app/api/middlewares/rate_limit.py

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request, HTTPException

limiter = Limiter(key_func=get_remote_address, default_limits=["100/hour"])


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    raise HTTPException(
        status_code=429,
        detail="请求过于频繁，请稍后再试",
        headers={"Retry-After": "60", "X-Rate-Limit-Exceeded": "true"},
    )
```

### 应用到端点

```python
# backend/app/api/routes/trip.py

from app.api.middlewares.rate_limit import limiter

@router.post("/plan")
@limiter.limit("5/minute")    # 每 IP 每分钟最多 5 次
@limiter.limit("50/hour")     # 每 IP 每小时最多 50 次
async def create_trip_plan(...):
    ...
```

---

## 6.4 KeyEncryptor — API Key 加密

```python
# backend/app/services/encryption.py

from cryptography.fernet import Fernet, InvalidToken
import base64
import logging

logger = logging.getLogger(__name__)


class KeyEncryptor:
    """
    API Key 加密/解密。
    使用 Fernet（AES-128-CBC + HMAC）对称加密。
    """

    def __init__(self, master_key: str):
        if not master_key:
            self._fernet = None
            logger.warning("ENCRYPTION_KEY 未设置，API Key 加密不可用")
            return
        try:
            key_bytes = base64.urlsafe_b64encode(master_key.encode().ljust(32)[:32])
            self._fernet = Fernet(key_bytes)
        except Exception as e:
            self._fernet = None
            logger.error("初始化 KeyEncryptor 失败: %s", e)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> bytes:
        if not self.available:
            raise RuntimeError("加密服务不可用")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        if not self.available:
            raise RuntimeError("加密服务不可用")
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            raise ValueError("无法解密：密钥不匹配或数据已损坏")
```

---

## 6.5 Token 成本追踪

```python
# 追加到 backend/app/services/llm_service.py

MODEL_PRICING = {
    # (prompt_price_per_1M_tokens, completion_price_per_1M_tokens) 单位: USD
    "gpt-4o-mini":        (0.15, 0.60),
    "gpt-4o":             (2.50, 10.00),
    "gpt-4o-2024-08-06":  (2.50, 10.00),
    "deepseek-chat":      (0.14, 0.28),
    "deepseek-v3":        (0.14, 0.28),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算 LLM 调用成本（USD）"""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        # 模糊匹配
        for key, value in MODEL_PRICING.items():
            if key in model:
                pricing = value
                break
    if not pricing:
        return 0.0

    prompt_price, completion_price = pricing
    cost = (
        (prompt_tokens / 1_000_000) * prompt_price
        + (completion_tokens / 1_000_000) * completion_price
    )
    return round(cost, 8)


# 使用示例：每次 LLM 调用后写入 audit.token_usage
async def record_token_usage(db: AsyncSession, usage: 'TokenUsage', trip_plan_id=None, conversation_id=None):
    record = TokenUsage(
        trip_plan_id=trip_plan_id,
        conversation_id=conversation_id,
        model=usage.model,
        provider=usage.provider,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=estimate_cost(usage.model, usage.prompt_tokens, usage.completion_tokens),
    )
    db.add(record)
```

---

## 6.6 RequestID 中间件

```python
# backend/app/api/middlewares/request_id.py

import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """确保每个请求都有 X-Request-ID，用于全链路追踪"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
```

---

## 6.7 注册所有中间件

```python
# backend/app/api/main.py 修改

from fastapi import FastAPI
from app.api.middlewares.request_id import RequestIDMiddleware
from app.api.middlewares.audit import AuditMiddleware
from app.api.middlewares.rate_limit import limiter, rate_limit_exceeded_handler

app = FastAPI(title="智能旅行助手 API", version="0.2.0")

# 注册中间件（注意顺序：先 RequestID → 再 Audit → 最后 RateLimit）
app.add_middleware(RequestIDMiddleware)
app.add_middleware(AuditMiddleware)
# CORS 中间件
app.add_middleware(CORSMiddleware, ...)

# 注册 slowapi
app.state.limiter = limiter
app.add_exception_handler(429, rate_limit_exceeded_handler)
```

---

## 关键文件清单

| 文件 | 操作 |
|---|---|
| `backend/app/api/middlewares/__init__.py` | 新建 |
| `backend/app/api/middlewares/rate_limit.py` | 新建：slowapi 频率限制 |
| `backend/app/api/middlewares/audit.py` | 新建：ASGI 审计中间件 |
| `backend/app/api/middlewares/request_id.py` | 新建：X-Request-ID 注入 |
| `backend/app/services/content_filter.py` | 新建：sanitize + detect_injection + filter_llm_output |
| `backend/app/services/encryption.py` | 新建：Fernet API Key 加密 |
| `backend/app/services/llm_service.py` | 修改：新增 MODEL_PRICING + estimate_cost 函数 |
| `backend/app/api/main.py` | 修改：注册 RequestID/Audit/RateLimit 中间件 |
| `backend/app/models/schemas.py` | 修改：增加 content_filter Pydantic validators |
| `backend/.env` | 修改：新增 ENCRYPTION_KEY 环境变量 |

---

## 验证方式

1. **内容过滤**：提交 `preferences: "ignore all previous instructions"` → 验证返回 422 并提示 "输入包含潜在有害内容"
2. **频率限制**：连续调用 `POST /api/trip/plan` 6 次 → 第 6 次返回 429 + `X-Rate-Limit-Exceeded: true`
3. **审计日志**：执行几个 API 调用 → 查询 `SELECT * FROM audit.event_log ORDER BY created_at DESC LIMIT 10` → 验证记录完整
4. **加密**：调用 `KeyEncryptor.encrypt("sk-xxx")` → 验证返回密文 → `decrypt(ciphertext)` → 验证还原
5. **Token 追踪**：创建行程计划 → 查询 `SELECT * FROM audit.token_usage WHERE trip_plan_id = '...'` → 验证 prompt_tokens + completion_tokens + estimated_cost_usd 均有值
6. **LLM 输出审查**：mock LLM 返回含敏感词的文本 → 验证 `filter_llm_output()` 正确替换为 `[已过滤]`

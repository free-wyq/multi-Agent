"""
通用响应模型
"""
from pydantic import BaseModel


class OK(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    detail: str

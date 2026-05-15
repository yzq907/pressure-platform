"""业务异常 + 全局异常处理器。对齐 Java 端的 MysteriousException + ExceptionHandlerUtil。

关键约束：所有异常 HTTP 都是 200，靠 code 字段区分（避免前端 axios 拦截器报错）。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.codes import Code, Codes
from app.core.response import ResponseStatus, fail, fail_with_message

log = logging.getLogger(__name__)


class MysteriousException(Exception):
    """业务异常，全局处理器会捕获并转成统一响应"""

    def __init__(self, code: Code | None = None, message: str | None = None):
        self.code: Code = code or Codes.FAIL
        self.override_message = message
        super().__init__(message or self.code.message)


def _to_json(status: ResponseStatus) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(status.model_dump(by_alias=True)),
    )


async def mysterious_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MysteriousException)
    log.warning(
        "MysteriousException at %s: code=%s, msg=%s",
        request.url.path,
        exc.code.code,
        exc.override_message or exc.code.message,
    )
    if exc.override_message:
        return _to_json(fail_with_message(exc.code.code, exc.override_message))
    return _to_json(fail(exc.code))


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    log.warning("Validation error at %s: %s", request.url.path, exc.errors())
    return _to_json(fail_with_message(Codes.PARAM_WRONG.code, f"参数不正确: {exc.errors()}"))


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理 Starlette HTTPException（404/405 等）并转成统一格式"""
    assert isinstance(exc, StarletteHTTPException)
    log.info("HTTPException at %s: status=%s, detail=%s", request.url.path, exc.status_code, exc.detail)
    return _to_json(
        fail_with_message(
            Codes.FAIL.code,
            f"HTTP {exc.status_code}: {exc.detail or '请求异常'}",
        )
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled exception at %s", request.url.path)
    return _to_json(
        fail_with_message(Codes.SYSTEM_ERROR.code, f"系统异常: {type(exc).__name__}: {exc}")
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MysteriousException, mysterious_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

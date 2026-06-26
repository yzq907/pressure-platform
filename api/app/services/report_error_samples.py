"""Failure sample artifact parsing for report detail."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.exceptions import MysteriousException
from app.core.jmeter_error_samples import ERROR_SAMPLE_FILENAME
from app.crud import report as report_crud
from app.models.report import Report
from app.schemas.report import ErrorSamplesVO, ErrorSampleVO


log = logging.getLogger(__name__)

_SENSITIVE_LINE_RE = re.compile(
    r"(?im)^([^:\r\n]*(authorization|cookie|token|password|secret)[^:\r\n]*\s*:\s*).+$"
)


def _report_artifact_dir(report: Report) -> str | None:
    if report.artifact_dir:
        return report.artifact_dir
    report_dir = (report.report_dir or "").rstrip(os.sep)
    if not report_dir:
        return None
    return str(Path(report_dir).parent / "artifacts")


def _error_sample_path(report: Report) -> Path | None:
    artifact_dir = _report_artifact_dir(report)
    if not artifact_dir:
        return None
    return Path(artifact_dir) / ERROR_SAMPLE_FILENAME


def _mask_sensitive_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return _SENSITIVE_LINE_RE.sub(r"\1******", text)


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _sample_time_text(sample_time: int) -> str:
    if sample_time <= 0:
        return ""
    return datetime.fromtimestamp(sample_time / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_error_type(data: dict[str, Any]) -> str:
    error_type = str(data.get("errorType") or data.get("error_type") or "").strip()
    if error_type:
        return error_type
    response_code = str(data.get("responseCode") or data.get("response_code") or "").strip()
    failure_message = str(data.get("failureMessage") or data.get("failure_message") or "").strip()
    if response_code == "200" and failure_message:
        return "ASSERTION_FAILED"
    if not response_code or response_code.startswith("Non HTTP response code"):
        return "ASSERTION_FAILED" if failure_message else "EXCEPTION"
    return response_code


def _to_error_sample(data: dict[str, Any]) -> ErrorSampleVO:
    sample_time = _to_int(data.get("sampleTime") or data.get("sample_time"))
    return ErrorSampleVO(
        sample_time=sample_time,
        sample_time_text=_sample_time_text(sample_time),
        label=str(data.get("label") or ""),
        thread_name=str(data.get("threadName") or data.get("thread_name") or ""),
        response_code=str(data.get("responseCode") or data.get("response_code") or ""),
        response_message=str(data.get("responseMessage") or data.get("response_message") or ""),
        elapsed=_to_int(data.get("elapsed")),
        failure_message=str(data.get("failureMessage") or data.get("failure_message") or ""),
        request_url=str(data.get("requestUrl") or data.get("request_url") or ""),
        request_headers=_mask_sensitive_text(data.get("requestHeaders") or data.get("request_headers")),
        request_body=_mask_sensitive_text(data.get("requestBody") or data.get("request_body")),
        response_headers=_mask_sensitive_text(data.get("responseHeaders") or data.get("response_headers")),
        response_body=str(data.get("responseBody") or data.get("response_body") or ""),
        truncated=bool(data.get("truncated")),
        error_type=_normalize_error_type(data),
    )


def parse_error_samples_file(path: str | Path, limit: int = 100) -> ErrorSamplesVO:
    samples: list[ErrorSampleVO] = []
    file_path = Path(path)
    if not file_path.is_file():
        return ErrorSamplesVO()

    max_items = max(1, min(int(limit or 100), 1000))
    try:
        with file_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("忽略非法错误样本行: %s", file_path)
                    continue
                if not isinstance(data, dict):
                    continue
                samples.append(_to_error_sample(data))
    except OSError as e:
        log.warning("读取错误样本失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="错误详情读取失败") from e

    samples.sort(key=lambda item: item.sample_time, reverse=True)
    groups = Counter(item.error_type or "EXCEPTION" for item in samples)
    return ErrorSamplesVO(
        total=len(samples),
        groups=dict(groups),
        list=samples[:max_items],
    )


async def get_error_samples(db: AsyncSession, report_id: int, limit: int = 100) -> ErrorSamplesVO:
    report = await report_crud.get_by_id(db, report_id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    path = _error_sample_path(report)
    result = parse_error_samples_file(path, limit) if path is not None else ErrorSamplesVO()
    result.report_id = report_id
    return result

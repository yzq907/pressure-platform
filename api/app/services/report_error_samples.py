"""Failure sample artifact parsing for report detail."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from lxml import etree
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import Codes
from app.core.enums import TestCaseStatus
from app.core.exceptions import MysteriousException
from app.core.jmeter_error_samples import (
    DEFAULT_ERROR_SAMPLE_PER_CODE_LIMIT,
    DEFAULT_ERROR_SAMPLE_TEXT_MAX_BYTES,
    DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT,
    ERROR_SAMPLE_FILENAME,
    ERROR_SAMPLE_SNAPSHOT_FILENAME,
    ERROR_SAMPLE_XML_FILENAME,
    error_sample_dir_from_artifact_dir,
)
from app.crud import report as report_crud
from app.models.report import Report
from app.schemas.report import ErrorSamplesVO, ErrorSampleVO

log = logging.getLogger(__name__)
_error_sample_snapshot_tasks: set[int] = set()

_SENSITIVE_LINE_RE = re.compile(
    r"(?im)^([^:\r\n]*(authorization|cookie|token|password|secret)[^:\r\n]*\s*:\s*).+$"
)
ERROR_SAMPLE_XML_SCAN_LIMIT = 1000
ERROR_SAMPLE_JTL_SCAN_LIMIT = 5000


def _report_artifact_dir(report: Report) -> str | None:
    if report.artifact_dir:
        return report.artifact_dir
    report_dir = (report.report_dir or "").rstrip(os.sep)
    if not report_dir:
        return None
    return str(Path(report_dir).parent / "artifacts")


def _error_sample_paths(report: Report) -> list[Path]:
    artifact_dir = _report_artifact_dir(report)
    if not artifact_dir:
        return []
    path = error_sample_dir_from_artifact_dir(artifact_dir) / ERROR_SAMPLE_FILENAME
    return [path] if path.is_file() else []


def _error_sample_snapshot_path(report: Report) -> Path | None:
    artifact_dir = _report_artifact_dir(report)
    if not artifact_dir:
        return None
    return error_sample_dir_from_artifact_dir(artifact_dir) / ERROR_SAMPLE_SNAPSHOT_FILENAME


def _report_base_dir(report: Report) -> Path | None:
    artifact_dir = _report_artifact_dir(report)
    if artifact_dir:
        return Path(artifact_dir).parent
    report_dir = (report.report_dir or "").rstrip(os.sep)
    if not report_dir:
        return None
    path = Path(report_dir)
    return path.parent if path.name in {"data", "jtl"} else path


def _report_jtl_paths(report: Report) -> list[Path]:
    base_dir = _report_base_dir(report)
    if base_dir is None:
        return []
    jtl_dir = base_dir / "jtl"
    if not jtl_dir.is_dir():
        return []
    return sorted(path for path in jtl_dir.glob("*.jtl") if path.is_file())


def _report_error_xml_paths(report: Report) -> list[Path]:
    artifact_dir = _report_artifact_dir(report)
    if not artifact_dir:
        return []
    path = error_sample_dir_from_artifact_dir(artifact_dir)
    if not path.is_dir():
        return []
    direct = path / ERROR_SAMPLE_XML_FILENAME
    slave_files = sorted(path.glob("error_samples.*.xml"))
    return [item for item in [direct, *slave_files] if item.is_file()]


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


def _truncate_text(value: Any, max_chars: int = DEFAULT_ERROR_SAMPLE_TEXT_MAX_BYTES) -> str:
    text = "" if value is None else str(value)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


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


def _read_error_samples_file(path: str | Path) -> list[ErrorSampleVO]:
    samples: list[ErrorSampleVO] = []
    file_path = Path(path)
    if not file_path.is_file():
        return samples

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
    return samples


def _build_error_samples_vo(samples: list[ErrorSampleVO], limit: int = 100) -> ErrorSamplesVO:
    max_items = max(1, min(int(limit or 100), 1000))

    samples.sort(key=lambda item: item.sample_time, reverse=True)
    groups = Counter(item.error_type or "EXCEPTION" for item in samples)
    return ErrorSamplesVO(
        total=len(samples),
        groups=dict(groups),
        list=samples[:max_items],
    )


def parse_error_samples_file(path: str | Path, limit: int = 100) -> ErrorSamplesVO:
    return _build_error_samples_vo(_read_error_samples_file(path), limit)


def _child_text(node: Any, *names: str) -> str:
    for child in node:
        tag = str(child.tag)
        if tag in names or tag.rsplit("}", 1)[-1] in names:
            return child.text or ""
    return ""


def _xml_failure_message(node: Any) -> str:
    messages: list[str] = []
    for assertion in node.findall("./assertionResult"):
        failed = (_child_text(assertion, "failure").strip().lower() == "true")
        errored = (_child_text(assertion, "error").strip().lower() == "true")
        if not failed and not errored:
            continue
        message = _child_text(assertion, "failureMessage").strip()
        if message:
            messages.append(message)
    return "\n".join(messages)


def _xml_error_record(node: Any) -> dict[str, Any] | None:
    success = str(node.get("s") or "").strip().lower()
    failure_message = _xml_failure_message(node)
    response_code = str(node.get("rc") or "").strip()
    if success == "true" and not failure_message:
        return None
    request_headers = _child_text(node, "requestHeader", "requestHeaders")
    response_headers = _child_text(node, "responseHeader", "responseHeaders")
    response_body = _child_text(node, "responseData")
    return {
        "sampleTime": node.get("ts") or 0,
        "label": node.get("lb") or "",
        "threadName": node.get("tn") or "",
        "responseCode": response_code,
        "responseMessage": node.get("rm") or "",
        "elapsed": node.get("t") or 0,
        "failureMessage": failure_message,
        "requestUrl": _child_text(node, "java.net.URL"),
        "requestHeaders": _truncate_text(request_headers),
        "requestBody": _truncate_text(_child_text(node, "samplerData")),
        "responseHeaders": _truncate_text(response_headers),
        "responseBody": _truncate_text(response_body),
        "truncated": any(
            len(value or "") > DEFAULT_ERROR_SAMPLE_TEXT_MAX_BYTES
            for value in (request_headers, response_headers, response_body)
        ),
    }


def _sample_key(sample: ErrorSampleVO) -> tuple[int, str, str, str]:
    return (
        sample.sample_time,
        sample.label,
        sample.thread_name,
        sample.response_code,
    )


def _append_sample_if_allowed(
    samples: list[ErrorSampleVO],
    sample: ErrorSampleVO,
    groups: Counter,
    seen: set[tuple[int, str, str, str]],
) -> bool:
    error_type = sample.error_type or "EXCEPTION"
    if groups[error_type] >= DEFAULT_ERROR_SAMPLE_PER_CODE_LIMIT:
        return False
    if len(samples) >= DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT:
        return False
    key = _sample_key(sample)
    if key in seen:
        return False
    samples.append(sample)
    seen.add(key)
    groups[error_type] += 1
    return True


def _append_xml_error_samples(report: Report, samples: list[ErrorSampleVO], limit: int = 100) -> None:
    groups = Counter(item.error_type or "EXCEPTION" for item in samples)
    seen = {_sample_key(item) for item in samples}
    target_total = max(1, min(int(limit or 100), DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT))
    if len(samples) >= target_total:
        return

    for xml_path in _report_error_xml_paths(report):
        scanned_errors = 0
        try:
            context = etree.iterparse(str(xml_path), events=("end",), tag=("httpSample", "sample"), recover=True)
            for _event, node in context:
                record = _xml_error_record(node)
                if record is not None:
                    scanned_errors += 1
                    _append_sample_if_allowed(samples, _to_error_sample(record), groups, seen)
                node.clear()
                if len(samples) >= target_total or scanned_errors >= ERROR_SAMPLE_XML_SCAN_LIMIT:
                    return
        except (OSError, etree.XMLSyntaxError) as e:
            log.warning("读取错误 XML 样本失败: %s", e)


def _jtl_error_record(row: dict[str, Any]) -> dict[str, Any] | None:
    if str(row.get("success") or "").strip().lower() == "true":
        return None
    return {
        "sampleTime": row.get("timeStamp") or row.get("timestamp") or 0,
        "label": row.get("label") or "",
        "threadName": row.get("threadName") or "",
        "responseCode": row.get("responseCode") or "",
        "responseMessage": row.get("responseMessage") or "",
        "elapsed": row.get("elapsed") or 0,
        "failureMessage": row.get("failureMessage") or "",
        "requestUrl": row.get("URL") or "",
        "requestHeaders": "",
        "requestBody": "",
        "responseHeaders": "",
        "responseBody": "",
        "truncated": False,
    }


def _append_jtl_backfill_samples(report: Report, samples: list[ErrorSampleVO], limit: int = 100) -> None:
    groups = Counter(item.error_type or "EXCEPTION" for item in samples)
    seen = {_sample_key(item) for item in samples}
    total_limit = max(1, min(int(limit or 100), DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT))
    remaining = total_limit - len(samples)
    if remaining <= 0:
        return

    for jtl_path in _report_jtl_paths(report):
        scanned_errors = 0
        try:
            with jtl_path.open(newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    if remaining <= 0:
                        return
                    record = _jtl_error_record(row)
                    if record is None:
                        continue
                    scanned_errors += 1
                    sample = _to_error_sample(record)
                    error_type = sample.error_type or "EXCEPTION"
                    if groups[error_type] >= DEFAULT_ERROR_SAMPLE_PER_CODE_LIMIT:
                        if scanned_errors >= ERROR_SAMPLE_JTL_SCAN_LIMIT:
                            return
                        continue
                    key = _sample_key(sample)
                    if key in seen:
                        if scanned_errors >= ERROR_SAMPLE_JTL_SCAN_LIMIT:
                            return
                        continue
                    samples.append(sample)
                    seen.add(key)
                    groups[error_type] += 1
                    remaining -= 1
                    if scanned_errors >= ERROR_SAMPLE_JTL_SCAN_LIMIT:
                        return
        except OSError as e:
            log.warning("读取 JTL 兜底错误样本失败: %s", e)


def _collect_error_samples(report: Report, limit: int, *, include_jtl: bool) -> ErrorSamplesVO:
    samples: list[ErrorSampleVO] = []
    seen: set[tuple[int, str, str, str]] = set()
    for path in _error_sample_paths(report):
        for sample in _read_error_samples_file(path):
            key = _sample_key(sample)
            if key in seen:
                continue
            seen.add(key)
            samples.append(sample)
    _append_xml_error_samples(report, samples, limit)
    if include_jtl:
        _append_jtl_backfill_samples(report, samples, limit)
    result = _build_error_samples_vo(samples, limit)
    result.report_id = report.id
    return result


def _write_error_sample_snapshot(report: Report, result: ErrorSamplesVO) -> None:
    path = _error_sample_snapshot_path(report)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "complete": True,
        "data": result.model_dump(mode="json", by_alias=False),
    }
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def _read_error_sample_snapshot(report: Report, limit: int) -> ErrorSamplesVO | None:
    path = _error_sample_snapshot_path(report)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("complete") is not True:
            return None
        result = ErrorSamplesVO.model_validate(payload.get("data") or {})
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("读取错误样本快照失败: %s", exc)
        return None
    max_items = max(1, min(int(limit or 100), 1000))
    result.list = result.list[:max_items]
    return result


async def generate_error_samples_snapshot_for_report(
    db: AsyncSession,
    report_id: int,
    limit: int = DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT,
) -> int:
    report = await report_crud.get_by_id(db, report_id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)
    result = await asyncio.to_thread(_collect_error_samples, report, limit, include_jtl=True)
    await asyncio.to_thread(_write_error_sample_snapshot, report, result)
    return result.total


async def _generate_error_sample_snapshot_background(report_id: int) -> None:
    from app.db import session as session_module

    async with session_module.AsyncSessionLocal() as db:
        count = await generate_error_samples_snapshot_for_report(db, report_id)
    log.info("报告错误样本快照生成完成: report_id=%s rows=%s", report_id, count)


def _on_error_sample_snapshot_task_done(report_id: int, task: asyncio.Task) -> None:
    _error_sample_snapshot_tasks.discard(report_id)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.warning(
            "报告错误样本快照生成失败: report_id=%s",
            report_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def schedule_error_sample_snapshot_generation(report_id: int) -> bool:
    if report_id in _error_sample_snapshot_tasks:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _error_sample_snapshot_tasks.add(report_id)
    task = loop.create_task(
        _generate_error_sample_snapshot_background(report_id),
        name=f"report-error-sample-snapshot-{report_id}",
    )
    task.add_done_callback(lambda done_task: _on_error_sample_snapshot_task_done(report_id, done_task))
    return True


async def get_error_samples(db: AsyncSession, report_id: int, limit: int = 100) -> ErrorSamplesVO:
    report = await report_crud.get_by_id(db, report_id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    result = await asyncio.to_thread(_read_error_sample_snapshot, report, limit)
    if result is not None:
        return result

    result = await asyncio.to_thread(_collect_error_samples, report, limit, include_jtl=False)
    if report.status != TestCaseStatus.RUN_ING.value:
        schedule_error_sample_snapshot_generation(report_id)
    return result

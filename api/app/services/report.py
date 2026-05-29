"""Report 业务服务。Phase 5 最小版本：list / getById / listByTestCase / add（仅供 jmeter_runner / debug_testcase / run_testcase 内部调用）。

Phase 6 补齐 download / clean / view。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import ExecType
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.crud import report as crud, testcase as testcase_crud
from app.models.report import Report
from app.schemas.report import ArtifactVO, ReportByTestCaseQuery, ReportParam, ReportQuery, ReportStatsVO, ReportVO
from app.services import config as config_service

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _to_vo(obj: Report) -> ReportVO:
    return ReportVO.model_validate(obj)


async def add_report(db: AsyncSession, param: ReportParam, user: UserContext) -> int:
    """供 debug_testcase / run_testcase 内部调用。"""
    if param is None:
        raise MysteriousException(Codes.PARAMS_EMPTY)
    obj = Report(
        name=param.name or "",
        description=param.description or "",
        test_case_id=param.test_case_id or 0,
        report_dir=param.report_dir or "",
        exec_type=param.exec_type if param.exec_type is not None else 1,
        status=param.status if param.status is not None else 0,
        response_data=param.response_data or "",
        jmeter_log_file_path=param.jmeter_log_file_path or "",
        region=param.region or "",
        service_name=param.service_name or "",
        total_threads=param.total_threads or 0,
        slave_count=param.slave_count or 0,
        grafana_instance=param.grafana_instance or "",
        artifact_dir=param.artifact_dir or "",
    )
    stamp_create(obj, user)
    await crud.add(db, obj)
    return obj.id


async def update_status(db: AsyncSession, id: int, status: int) -> bool:
    return await crud.update_status(db, id, status)


async def update_report(db: AsyncSession, obj: Report, user: UserContext | None) -> bool:
    """供 jmeter_runner callback 在完成时更新 response_data + status 等"""
    if user is not None:
        stamp_modify(obj, user)
    return await crud.update(db, obj)


async def get_by_id(db: AsyncSession, id: int) -> ReportVO | None:
    obj = await crud.get_by_id(db, id)
    return _to_vo(obj) if obj else None


async def get_report_list(db: AsyncSession, query: ReportQuery) -> PageVO[ReportVO]:
    page_vo: PageVO[ReportVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, name=query.name, region=query.region)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_reports(db, name=query.name, region=query.region, offset=offset, limit=query.size)
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_report_list_by_test_case(
    db: AsyncSession, query: ReportByTestCaseQuery
) -> PageVO[ReportVO]:
    page_vo: PageVO[ReportVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, name=query.name, test_case_id=query.test_case_id)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    items = await crud.list_by_test_case(
        db, name=query.name, test_case_id=query.test_case_id, offset=offset, limit=query.size
    )
    page_vo.list = [_to_vo(o) for o in items]
    return page_vo


async def get_report_stats(db: AsyncSession, query: ReportQuery) -> ReportStatsVO:
    counts = await crud.count_by_status(db, name=query.name, region=query.region)
    success = counts.get(2, 0)
    failed = counts.get(3, 0)
    executed = success + failed
    success_rate = 100.0 if executed == 0 else round(success / executed * 100, 1)
    return ReportStatsVO(
        total=sum(counts.values()),
        idle=counts.get(0, 0),
        running=counts.get(1, 0),
        success=success,
        failed=failed,
        success_rate=success_rate,
    )


async def get_debug_reports_by_test_case_id(
    db: AsyncSession, test_case_id: int, exec_type: int | None, limit: int
) -> list[ReportVO]:
    """供 testcase getJMeterResult 用：拉最近 N 条该用例的报告"""
    items = await crud.get_debug_reports_by_test_case_id(db, test_case_id, exec_type, limit)
    return [_to_vo(o) for o in items]


async def clean_report(db: AsyncSession, id: int) -> bool:
    """清理报告：先删 DB 记录，再删磁盘目录（有问题无法回滚，但 Java 也是这个顺序）。

    删除磁盘时取 report_dir 的父目录（../timestamp/ 级别），
    因为 report_dir 指向的是 data/ 或 jtl/ 子目录。
    """
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    log.info("清理测试报告, id: %s", id)
    await crud.delete(db, id)

    report_dir = report.report_dir or ""
    clean_dir = _parent_timestamp_dir(report_dir)
    if clean_dir and os.path.exists(clean_dir):
        shutil.rmtree(clean_dir, ignore_errors=True)
    return True


def _parent_timestamp_dir(report_dir: str) -> str | None:
    """对齐 Java 逻辑：去掉末尾 / 后找最后一个 /，取父目录。"""
    if not report_dir:
        return None
    normalized = report_dir.rstrip("/")
    last_idx = normalized.rfind("/")
    if last_idx <= 0:
        return None
    return normalized[:last_idx]


async def download_report(db: AsyncSession, id: int) -> str:
    """下载报告：将 report_dir 下的 data/ 目录打包成 zip 返回 zip 文件路径。

    - DEBUG 报告不可下载
    - 目录不存在 / 为空报错
    - zip 已存在则直接返回，不重新打包
    """
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    if report.exec_type == ExecType.DEBUG.value:
        raise MysteriousException(Codes.DEBUG_REPORT_NOT_DOWNLOAD)

    report_dir = report.report_dir or ""
    if not os.path.exists(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_NOT_EXIST)

    if not os.listdir(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_IS_EMPTY)

    # reportDir 以 /data/ 结尾，取前面部分
    report_path = report_dir[: report_dir.rfind("data")]
    src_path = report_path + "data"
    zip_path = report_path + report.name + ".zip"

    if not os.path.exists(zip_path):
        _compress_directory(src_path, zip_path)

    return zip_path


def _compress_directory(src_dir: str, dest_zip: str) -> None:
    """将 src_dir 压缩为 dest_zip（保留目录结构）。"""
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, src_dir)
                zf.write(file_path, arcname)


async def view_report(db: AsyncSession, id: int) -> str:
    """预览报告：返回 index.html 的完整 URL。

    - 只有 RUNNING(exec_type=2) 类型报告可预览
    - 目录不存在 / 为空报错
    """
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    if report.exec_type != ExecType.EXEC.value:
        raise MysteriousException(Codes.DEBUG_REPORT_NOT_VIEW)

    report_dir = report.report_dir or ""
    if not os.path.exists(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_NOT_EXIST)

    if not os.listdir(report_dir):
        raise MysteriousException(Codes.REPORT_DIR_IS_EMPTY)

    # 构造相对路径：去掉 mysterious-data 前缀或 /data 前缀
    if "mysterious-data" in report_dir:
        relative = report_dir.split("mysterious-data")[1]
    else:
        relative = report_dir.lstrip("/data")

    if not relative.endswith("/"):
        relative += "/"

    host = await config_service.get_value(db, "MASTER_HOST_PORT")
    return f"http://{host}/reports{relative}index.html"


def _to_epoch_ms(dt: datetime | None) -> int:
    if dt is None:
        dt = datetime.now(SHANGHAI)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return int(dt.timestamp() * 1000)


def _parse_offset_minutes(raw: str, default: int) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _join_url(base_url: str, dashboard_path: str) -> str:
    return f"{base_url.rstrip('/')}/{dashboard_path.lstrip('/')}"


def _parse_instance_map(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


async def resolve_grafana_instance(
    db: AsyncSession,
    service_name: str = "",
    testcase_name: str = "",
    report_name: str = "",
) -> str:
    """按服务名等候选 key 解析 Grafana instance，例如 EMM-API -> 10.10.27.42:9200。"""
    mapping = _parse_instance_map(
        await config_service.get_value_or_default(db, "GRAFANA_INSTANCE_MAP", "")
    )
    candidates = [
        service_name,
        testcase_name,
        report_name,
    ]
    for key in candidates:
        if key and key in mapping:
            return mapping[key]
    return await config_service.get_value_or_default(db, "GRAFANA_DEFAULT_INSTANCE", "")


async def _resolve_grafana_instance(db: AsyncSession, report: Report) -> str:
    """优先使用报告快照；老报告没有快照时回退到当前用例信息。"""
    if report.grafana_instance:
        return report.grafana_instance

    testcase = await testcase_crud.get_by_id(db, report.test_case_id)
    return await resolve_grafana_instance(
        db,
        service_name=report.service_name or (testcase.service if testcase else ""),
        testcase_name=testcase.name if testcase else "",
        report_name=report.name,
    )


async def get_grafana_url(db: AsyncSession, id: int) -> str:
    """根据报告时间窗口和服务实例生成 Grafana 资源监控跳转地址。"""
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    url = await config_service.get_value_or_default(db, "GRAFANA_DASHBOARD_URL", "")
    if not url:
        base_url = await config_service.get_value(db, "GRAFANA_BASE_URL")
        dashboard_path = await config_service.get_value(db, "GRAFANA_DASHBOARD_PATH")
        url = _join_url(base_url, dashboard_path)

    org_id = await config_service.get_value_or_default(db, "GRAFANA_ORG_ID", "")
    instance_var = await config_service.get_value_or_default(db, "GRAFANA_INSTANCE_VAR", "instance")
    instance = await _resolve_grafana_instance(db, report)
    from_offset = _parse_offset_minutes(
        await config_service.get_value_or_default(db, "GRAFANA_FROM_OFFSET_MINUTES", "15"),
        15,
    )
    to_offset = _parse_offset_minutes(
        await config_service.get_value_or_default(db, "GRAFANA_TO_OFFSET_MINUTES", "15"),
        15,
    )

    start = report.create_time
    end = report.modify_time or report.create_time
    if start and end and end < start:
        end = start

    params = {
        "from": str(_to_epoch_ms(start - timedelta(minutes=from_offset) if start else None)),
        "to": str(_to_epoch_ms(end + timedelta(minutes=to_offset) if end else None)),
    }
    if org_id:
        params["orgId"] = org_id
    if instance:
        params[f"var-{instance_var}"] = instance

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def get_jmeter_log(db: AsyncSession, id: int) -> str:
    """返回报告的 jmeter.log 文件内容。"""
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    log_path = report.jmeter_log_file_path
    if not log_path or not Path(log_path).exists():
        raise MysteriousException(Codes.FILE_NOT_EXIST)

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        log.warning("读取 jmeter.log 失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="日志读取失败") from e


def _find_jtl_file(report_dir: str) -> str | None:
    """查找 JTL 文件。

    report_dir 运行时指向 data/ 或 jtl/ 子目录，
    取父目录（timestamp 级别）再找 jtl/ 子目录。
    """
    if not report_dir:
        return None
    # report_dir 以 /data/ 或 /jtl/ 结尾，取父目录
    parent = os.path.dirname(report_dir.rstrip(os.sep))
    jtl_dir = os.path.join(parent, "jtl")
    if not os.path.isdir(jtl_dir):
        return None
    for name in os.listdir(jtl_dir):
        if name.endswith(".jtl") or name.endswith(".xml"):
            return os.path.join(jtl_dir, name)
    return None


def _find_report_root(report_dir: str) -> str | None:
    if not report_dir:
        return None
    return os.path.dirname(report_dir.rstrip(os.sep))


def _artifact_dir(report_dir: str) -> str | None:
    report_root = _find_report_root(report_dir)
    if not report_root:
        return None
    return os.path.join(report_root, "artifacts")


def _report_artifact_dir(report: Report) -> str | None:
    if report.artifact_dir:
        return report.artifact_dir
    return _artifact_dir(report.report_dir)


def _safe_artifact_path(artifact_dir: str | None, name: str) -> str:
    if not name or Path(name).name != name:
        raise MysteriousException(Codes.PARAM_WRONG, message="产物文件名不合法")

    if not artifact_dir:
        raise MysteriousException(Codes.REPORT_DIR_NOT_EXIST)

    root = Path(artifact_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise MysteriousException(Codes.PARAM_WRONG, message="产物文件名不合法")
    return str(path)


async def list_artifacts(db: AsyncSession, id: int) -> list[ArtifactVO]:
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    artifact_dir = _report_artifact_dir(report)
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return []

    items: list[ArtifactVO] = []
    for path in sorted(Path(artifact_dir).iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        stat = path.stat()
        items.append(
            ArtifactVO(
                name=path.name,
                size=stat.st_size,
                modify_time=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    return items


async def download_artifact(db: AsyncSession, id: int, name: str) -> str:
    report = await crud.get_by_id(db, id)
    if report is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    path = _safe_artifact_path(_report_artifact_dir(report), name)
    if not os.path.isfile(path):
        raise MysteriousException(Codes.FILE_NOT_EXIST)
    return path


def _load_run_meta(report_dir: str) -> dict:
    report_root = _find_report_root(report_dir)
    if not report_root:
        return {}
    meta_path = os.path.join(report_root, "run_meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _meta_int(meta: dict, key: str, default: int = 0) -> int:
    try:
        return int(meta.get(key) or default)
    except (TypeError, ValueError):
        return default


def _normalize_threads(raw_threads: int, run_meta: dict | None) -> int:
    """分布式 JTL 的 allThreads 通常是单台压力机线程数，这里换算成总线程数。"""
    if not run_meta:
        return raw_threads
    slave_count = _meta_int(run_meta, "slave_count", 1)
    per_slave_threads = _meta_int(run_meta, "per_slave_threads", 0)
    total_threads = _meta_int(run_meta, "total_threads", 0)
    if slave_count <= 1:
        return raw_threads

    # 如果 JTL 已经给出全局线程数，不再重复乘；否则按实际压力机数换算。
    threads = raw_threads
    if per_slave_threads <= 0 or raw_threads <= per_slave_threads:
        threads = raw_threads * slave_count
    if total_threads > 0:
        threads = min(threads, total_threads)
    return threads


def _percentile(sorted_values: list[float], p: float) -> float:
    """计算已排序数组的百分位数。"""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p / 100.0
    f = int(k)
    c = f + 1
    if c >= n:
        return sorted_values[-1]
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


def _parse_jtl_metrics(
    jtl_path: str,
    window_sec: int = 5,
    run_meta: dict | None = None,
) -> list[dict]:
    """解析 JTL 文件，按时间窗口聚合指标。"""
    import csv
    from datetime import datetime

    window_ms = window_sec * 1000
    buckets: dict[int, dict] = {}

    try:
        with open(jtl_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = int(row.get("timeStamp", "0"))
                    elapsed = int(row.get("elapsed", "0"))
                    success = row.get("success", "true").lower() == "true"
                    raw_threads = int(row.get("allThreads", "0") or row.get("grpThreads", "0"))
                    threads = _normalize_threads(raw_threads, run_meta)
                    thread_name = (row.get("threadName") or "").strip()
                except (ValueError, TypeError):
                    continue
                if ts <= 0:
                    continue
                key = ts // window_ms * window_ms
                bucket = buckets.setdefault(
                    key,
                    {"elapsed": [], "fail": 0, "threads": 0, "thread_names": set(), "count": 0},
                )
                bucket["elapsed"].append(elapsed)
                if not success:
                    bucket["fail"] += 1
                bucket["threads"] = max(bucket["threads"], threads)
                if thread_name:
                    bucket["thread_names"].add(thread_name)
                bucket["count"] += 1
    except OSError as e:
        log.warning("读取 JTL 失败: %s", e)
        return []

    results = []
    for key in sorted(buckets):
        b = buckets[key]
        count = b["count"]
        if count == 0:
            continue
        elapsed_sorted = sorted(b["elapsed"])
        dt = datetime.fromtimestamp(key / 1000.0)
        active_threads = max(b["threads"], len(b.get("thread_names") or set()))
        total_threads = _meta_int(run_meta or {}, "total_threads", 0)
        if total_threads > 0:
            active_threads = min(active_threads, total_threads)
        results.append(
            {
                "timestamp": dt.strftime("%H:%M:%S"),
                "qps": round(count / window_sec, 1),
                "avg_rt": round(sum(elapsed_sorted) / count, 1),
                "p99_rt": round(_percentile(elapsed_sorted, 99), 1),
                "error_rate": round(b["fail"] / count * 100, 2),
                "threads": active_threads,
            }
        )
    return results


async def get_jtl_metrics(
    db: AsyncSession, report_id: int, window_sec: int = 5
) -> list[dict]:
    """读取指定报告 JTL 文件，按窗口聚合返回监控指标。"""
    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    jtl_path = _find_jtl_file(rpt.report_dir)
    if not jtl_path:
        return []

    return _parse_jtl_metrics(jtl_path, window_sec, _load_run_meta(rpt.report_dir))


def _normalize_to_relative(items: list[dict]) -> list[dict]:
    """将绝对时间戳转换为相对偏移（从 0s 开始），方便两份报告叠加对比。"""
    if not items:
        return []
    # 原始 timestamp 是 "%H:%M:%S" 格式，先转成秒数
    from datetime import datetime

    def _to_seconds(ts: str) -> int:
        try:
            dt = datetime.strptime(ts, "%H:%M:%S")
            return dt.hour * 3600 + dt.minute * 60 + dt.second
        except ValueError:
            return 0

    base_sec = _to_seconds(items[0]["timestamp"])
    out = []
    for item in items:
        sec = _to_seconds(item["timestamp"]) - base_sec
        out.append({**item, "timestamp": f"{sec}s"})
    return out


async def compare_reports(
    db: AsyncSession, base_id: int, target_id: int, window_sec: int = 5
) -> dict:
    """对比两份报告的 JTL 指标，返回相对时间轴数据。"""
    base_rpt = await crud.get_by_id(db, base_id)
    target_rpt = await crud.get_by_id(db, target_id)
    if base_rpt is None or target_rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    base_metrics = await get_jtl_metrics(db, base_id, window_sec)
    target_metrics = await get_jtl_metrics(db, target_id, window_sec)

    return {
        "base_name": base_rpt.name or f"报告 #{base_id}",
        "target_name": target_rpt.name or f"报告 #{target_id}",
        "base": _normalize_to_relative(base_metrics),
        "target": _normalize_to_relative(target_metrics),
    }


async def get_jmeter_result_by_report(db: AsyncSession, report_id: int) -> list:
    """读取指定报告 jmeter.log 的实时 summary 数据。（兼容旧接口）"""

    from app.schemas.testcase import JMeterResultVO
    import re

    _RESULT_RE = re.compile(
        r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d{3} INFO.*summary \+.* (\d+\.\d+)/s Avg: +(\d+)"
    )

    rpt = await crud.get_by_id(db, report_id)
    if rpt is None:
        raise MysteriousException(Codes.REPORT_NOT_EXIST)

    log_path = rpt.jmeter_log_file_path
    if not log_path or not Path(log_path).exists():
        return []

    results: list = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _RESULT_RE.search(line)
                if m:
                    results.append(
                        JMeterResultVO(
                            currentTime=m.group(1),
                            throughput=float(m.group(2)),
                            avgResponseTime=float(m.group(3)),
                        )
                    )
    except OSError as e:
        log.warning("读取 jmeter.log 失败: %s", e)
        raise MysteriousException(Codes.FAIL, message="实时数据读取失败") from e
    return results

"""Node 业务服务，对齐 Java INodeService + NodeService。Phase 5 把 enable/disable 补齐。"""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import stamp_create, stamp_modify
from app.core.codes import Codes
from app.core.context import UserContext
from app.core.enums import NodeStatus, NodeType
from app.core.exceptions import MysteriousException
from app.core.response import PageVO
from app.core.ssh import SSHClient
from app.crud import node as crud
from app.models.node import Node
from app.schemas.node import NodeParam, NodeQuery, NodeVO
from app.services import config as config_service

log = logging.getLogger(__name__)

_MD5_RE = re.compile(r"^[a-fA-F0-9]{32}$")


def _check_param(param: NodeParam) -> None:
    if param is None:
        raise MysteriousException(Codes.PARAMS_EMPTY)
    if not param.host or not param.username or param.port is None or param.type is None:
        raise MysteriousException(Codes.PARAM_MISSING)


def _to_vo(obj: Node, mask_password: bool = False) -> NodeVO:
    data = NodeVO.model_validate(obj)
    if mask_password:
        data.password = "******"
    return data


async def add_node(db: AsyncSession, param: NodeParam, user: UserContext) -> int:
    _check_param(param)
    existing = await crud.get_by_host(db, param.host or "")
    if existing is not None:
        raise MysteriousException(Codes.NODE_EXIST)

    obj = Node(
        name=param.name or "",
        description=param.description or "",
        type=param.type or 0,
        host=param.host or "",
        username=param.username or "",
        password=param.password or "",
        port=param.port or 0,
        status=NodeStatus.DISABLED.value,
        region=param.region or "",
    )
    stamp_create(obj, user)
    await crud.add(db, obj)
    return obj.id


async def update_node(
    db: AsyncSession, id: int, param: NodeParam, user: UserContext
) -> bool:
    existing = await crud.get_by_id(db, id)
    if existing is None:
        return False

    sent = param.model_dump(exclude_unset=True, exclude_none=True, by_alias=False)
    for field in ("name", "description", "type", "host", "username", "password", "port", "region"):
        if field in sent:
            setattr(existing, field, sent[field])
    stamp_modify(existing, user)
    return await crud.update(db, existing)


async def get_by_id(db: AsyncSession, id: int) -> NodeVO | None:
    obj = await crud.get_by_id(db, id)
    if obj is None:
        return None
    # Java 行为：getById 不脱敏密码
    return _to_vo(obj, mask_password=False)


async def delete_node(db: AsyncSession, id: int) -> bool:
    existing = await crud.get_by_id(db, id)
    if existing is None:
        return False
    return await crud.delete(db, id)


async def get_node_list(db: AsyncSession, query: NodeQuery) -> PageVO[NodeVO]:
    page_vo: PageVO[NodeVO] = PageVO(page=query.page, size=query.size, total=0, list=[])
    total = await crud.count(db, name=query.name, host=query.host)
    if total <= 0:
        return page_vo
    page_vo.total = total
    offset = PageVO.offset(query.page, query.size)
    nodes = await crud.list_nodes(
        db, name=query.name, host=query.host, offset=offset, limit=query.size
    )
    # Java 行为：list 把 password 替换为 ******
    page_vo.list = [_to_vo(n, mask_password=True) for n in nodes]
    return page_vo


async def get_enable_slave_list(db: AsyncSession) -> list[NodeVO]:
    """给 Phase 5 的 JMeter 分布式调度用。仅查 type=SLAVE && status=ENABLE 的节点。"""
    nodes = await crud.list_enable_slaves(db)
    return [_to_vo(n, mask_password=False) for n in nodes]


async def get_enable_slave_count(db: AsyncSession, region: str | None = None) -> int:
    """返回健康（已启用且在线）的 slave 节点数量，可选按区域过滤。"""
    nodes = await crud.list_enable_slaves(db, region=region)
    return len([n for n in nodes if n.health_status == 1])


async def get_all_regions(db: AsyncSession) -> list[str]:
    """返回所有启用 slave 的区域列表（去重），供前端下拉选择。"""
    return await crud.get_all_regions(db)


# ---------------------------------------------------------------------------
# Phase 5：enable / disable
# ---------------------------------------------------------------------------


async def _mark_failed(db: AsyncSession, obj: Node, user: UserContext) -> None:
    obj.status = NodeStatus.FAILED.value
    stamp_modify(obj, user)
    await crud.update(db, obj)


async def enable_node(db: AsyncSession, id: int, user: UserContext) -> bool:
    """对齐 Java NodeService.enableNode + enableSlaveNode：
    1) type=SLAVE
    2) status != ENABLE
    3) 早提交 status=ENABLE
    4) SSH telnet 检查连通性
    5) md5sum jmeter-server 检查文件存在
    6) ps 检查进程不存在
    7) 启动 jmeter-server，输出含 host 或 'Using local port'
    8) ps 再检查进程存在
    9) 任一失败：status=FAILED，re-raise
    """
    obj = await crud.get_by_id(db, id)
    if obj is None or obj.type != NodeType.SLAVE.value:
        raise MysteriousException(Codes.NODE_TYPE_ERROR)
    if obj.status == NodeStatus.ENABLE.value:
        raise MysteriousException(Codes.NODE_IS_ENABLE)

    # 早提交 ENABLE（对齐 Java；SSH 失败再改为 FAILED）
    obj.status = NodeStatus.ENABLE.value
    stamp_modify(obj, user)
    await crud.update(db, obj)

    try:
        slave_bin = await config_service.get_value(db, "SLAVE_JMETER_BIN_HOME")
        slave_log = await config_service.get_value(db, "SLAVE_JMETER_LOG_HOME")
        jmeter_server = f"{slave_bin}/jmeter-server"

        ssh = SSHClient(obj.host, obj.port, obj.username, obj.password)

        # telnet
        try:
            await ssh.telnet(200)
        except MysteriousException as e:
            log.info("节点 telnet 失败 host=%s: %s", obj.host, e)
            raise MysteriousException(Codes.NODE_CANNOT_CONNECT) from e

        # md5sum
        try:
            md5 = await ssh.exec_command(f"md5sum {jmeter_server} | cut -d ' ' -f 1")
        except MysteriousException as e:
            log.info("md5 命令异常 host=%s", obj.host)
            raise MysteriousException(Codes.SSH_AND_LOGIN_ERROR) from e
        if not _MD5_RE.match(md5):
            log.info("节点 %s 找不到 jmeter-server 文件 (md5=%s)", obj.name, md5)
            raise MysteriousException(Codes.JMETER_SERVER_NOT_FOUND)

        # ps 检查（应为 null）
        ps_before = await ssh.exec_command("ps aux | grep jmeter-server | grep -v grep")
        if ps_before != "null":
            raise MysteriousException(Codes.JMETER_SERVER_IS_ENABLE)

        # 启动
        start_cmd = (
            f"cd {slave_log}\n{jmeter_server} -Djava.rmi.server.hostname={obj.host}"
        )
        result = await ssh.exec_command(start_cmd)
        log.info("启动命令行输出 host=%s: %s", obj.host, result)
        if obj.host not in result and "Using local port" not in result:
            raise MysteriousException(Codes.JMETER_SERVER_ENABLE_ERROR)

        # 启动后再次 ps
        ps_after = await ssh.exec_command("ps aux | grep jmeter-server | grep -v grep")
        if ps_after == "null":
            raise MysteriousException(Codes.JMETER_SERVER_IS_NOT_ENABLE)
    except MysteriousException:
        await _mark_failed(db, obj, user)
        raise
    except Exception as e:
        log.exception("enable_node 未知异常")
        await _mark_failed(db, obj, user)
        raise MysteriousException(Codes.SSH_EXEC_ERROR) from e
    return True


async def disable_node(db: AsyncSession, id: int, user: UserContext) -> bool:
    """对齐 Java NodeService.disableNode：
    1) 节点存在，type=SLAVE（否则 ONLY_SLAVE_CAN_DISABLE）
    2) DB status=DISABLED
    3) SSH ps 检查（无进程抛 JMETER_SERVER_IS_NOT_ENABLE）
    4) kill -9，再次 ps，仍在再 kill 一次
    """
    obj = await crud.get_by_id(db, id)
    if obj is None:
        raise MysteriousException(Codes.NODE_NOT_EXIST)
    if obj.type != NodeType.SLAVE.value:
        raise MysteriousException(Codes.ONLY_SLAVE_CAN_DISABLE)

    obj.status = NodeStatus.DISABLED.value
    stamp_modify(obj, user)
    await crud.update(db, obj)

    ssh = SSHClient(obj.host, obj.port, obj.username, obj.password)
    if (await ssh.exec_command("ps aux | grep jmeter-server | grep -v grep")) == "null":
        raise MysteriousException(Codes.JMETER_SERVER_IS_NOT_ENABLE)
    await ssh.exec_command(
        "ps aux | grep jmeter-server | grep -v grep | awk '{print $2}' | xargs kill -9"
    )
    if (await ssh.exec_command("ps aux | grep jmeter-server | grep -v grep")) != "null":
        await ssh.exec_command(
            "ps aux | grep jmeter-server | grep -v grep | awk '{print $2}' | xargs kill -9"
        )
    return True


# ---------------------------------------------------------------------------
# 通用：上传/删除文件时同步到所有 enabled slave（对齐 Java CsvService / JarService 行为）
# 任何 SSH 失败仅日志，不抛——主流程不被 slave 故障阻塞
# ---------------------------------------------------------------------------


async def scp_to_enabled_slaves(
    db: AsyncSession, local_path: str, remote_dir: str
) -> None:
    """对每个 enabled slave scp 一份"""
    for node in await crud.list_enable_slaves(db):
        try:
            ssh = SSHClient(node.host, node.port, node.username, node.password)
            await ssh.scp_file(local_path, remote_dir)
        except Exception:  # noqa: BLE001
            log.warning("scp 到 slave %s 失败", node.host, exc_info=True)


async def rm_on_enabled_slaves(db: AsyncSession, remote_path: str) -> None:
    """对每个 enabled slave 执行 rm -rf"""
    for node in await crud.list_enable_slaves(db):
        try:
            ssh = SSHClient(node.host, node.port, node.username, node.password)
            await ssh.exec_command(f"rm -rf {remote_path}")
        except Exception:  # noqa: BLE001
            log.warning("rm -rf 在 slave %s 失败", node.host, exc_info=True)

"""Pacing timer injection for JMeter JMX thread groups."""

from __future__ import annotations

from typing import Any

from lxml import etree

from app.core.jmeter_thread_groups import as_bool, thread_group_key, walk_jmeter_pairs
from app.core.jmeter_xml_support import is_enabled, parse_jmx, write_jmx


def _transaction_config_key(item: dict[str, Any]) -> str:
    return str(item.get("key") or item.get("name") or "").strip()


def _transaction_config_by_key(items: list[dict] | None) -> dict[str, dict]:
    configs: dict[str, dict] = {}
    for item in items or []:
        key = _transaction_config_key(item)
        if key and as_bool(item.get("enabled", True)):
            configs[key] = item
    return configs


def _format_integer(value: Any) -> str:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = 0
    return str(max(0, number))


_PACING_CYCLE_TIMER_SCRIPT = """long pacingMs = 0L
try {
    pacingMs = Long.parseLong((Parameters ?: "0").trim())
} catch (Throwable ignored) {
    pacingMs = 0L
}

if (pacingMs <= 0L) {
    return 0L
}

String groupName = ctx.getThreadGroup() == null ? "default" : ctx.getThreadGroup().getName()
String key = "platform_pacing_next_start_" + groupName
Long nextStart = vars.getObject(key) as Long
long now = System.currentTimeMillis()

if (nextStart == null) {
    vars.putObject(key, now + pacingMs)
    return 0L
}

long delay = Math.max(0L, nextStart - now)
long scheduledStart = now + delay
vars.putObject(key, scheduledStart + pacingMs)
return delay
"""


def _jsr223_cycle_timer(name: str, pacing_ms: Any) -> Any:
    timer = etree.Element(
        "JSR223Timer",
        guiclass="TestBeanGUI",
        testclass="JSR223Timer",
        testname=f"平台Pacing_{name}",
        enabled="true",
    )
    etree.SubElement(timer, "stringProp", name="cacheKey").text = "true"
    etree.SubElement(timer, "stringProp", name="filename").text = ""
    etree.SubElement(timer, "stringProp", name="parameters").text = _format_integer(pacing_ms)
    etree.SubElement(timer, "stringProp", name="script").text = _PACING_CYCLE_TIMER_SCRIPT
    etree.SubElement(timer, "stringProp", name="scriptLanguage").text = "groovy"
    return timer


def _insert_timer_at_scope_start(scope_hash: Any, timer: Any) -> None:
    scope_hash.insert(0, etree.Element("hashTree"))
    scope_hash.insert(0, timer)


def apply_thread_group_pacing(
    jmx_path: str,
    dest_path: str,
    thread_group_overrides: list[dict[str, Any]] | None,
) -> None:
    """Insert JSR223 cycle timers for thread groups with positive pacing_ms."""
    tree = parse_jmx(jmx_path)
    root_hash = tree.getroot().find("hashTree")
    if root_hash is None:
        write_jmx(tree, dest_path)
        return

    overrides = _transaction_config_by_key(thread_group_overrides)
    if not overrides:
        write_jmx(tree, dest_path)
        return

    thread_group_index = 0
    for node, child_hash, _thread_group_name, _parent_hash in walk_jmeter_pairs(root_hash):
        indexed = thread_group_key(node, thread_group_index)
        if not indexed:
            continue
        key, _group_type = indexed
        thread_group_index += 1
        override = overrides.get(key) or overrides.get(node.get("testname") or "")
        if override is None or child_hash is None:
            continue
        if str(override.get("name") or node.get("testname") or "") != (node.get("testname") or ""):
            continue
        try:
            pacing_ms = int(override.get("pacing_ms") or 0)
        except (TypeError, ValueError):
            pacing_ms = 0
        if pacing_ms <= 0 or not is_enabled(node):
            continue
        timer = _jsr223_cycle_timer(
            node.get("testname") or "未命名线程组",
            pacing_ms,
        )
        _insert_timer_at_scope_start(child_hash, timer)

    write_jmx(tree, dest_path)

"""Thread group and transaction helpers for JMeter JMX files."""

from __future__ import annotations

from typing import Any

from lxml import etree

from app.core.jmeter_xml_support import (
    first_named_prop_text,
    is_enabled,
    parse_jmx,
    set_named_props,
    to_non_negative_int,
    write_jmx,
)

STEPPING_TG = "kg.apc.jmeter.threads.SteppingThreadGroup"
CONCURRENCY_TG = "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup"
THREAD_GROUP_TYPE_BY_TAG = {
    "ThreadGroup": "thread_group",
    STEPPING_TG: "stepping_thread_group",
    CONCURRENCY_TG: "concurrency_thread_group",
}

DEBUG_THREADGROUP_VALUES = {
    "LoopController.loops": "1",
    "ThreadGroup.num_threads": "1",
    "ThreadGroup.ramp_time": "1",
}
DEBUG_STEPPING_VALUES = {
    "ThreadGroup.num_threads": "2",
    "Threads initial delay": "0",
    "Start users count": "1",
    "Start users count burst": "1",
    "Start users period": "1",
    "Stop users count": "1",
    "Stop users period": "0",
    "flighttime": "1",
    "rampUp": "0",
    "LoopController.loops": "1",
}
DEBUG_CONCURRENCY_VALUES = {
    "TargetLevel": "1",
    "RampUp": "1",
    "Steps": "1",
    "Hold": "1",
    "Unit": "S",
}


def update_debug_thread(jmx_path: str) -> None:
    """Lower enabled thread groups to debug-level pressure."""
    tree = parse_jmx(jmx_path)
    for el in tree.iter():
        if el.tag == "ThreadGroup" and el.get("testclass") == "ThreadGroup" and is_enabled(el):
            set_named_props(el, DEBUG_THREADGROUP_VALUES)
        elif el.tag == STEPPING_TG and is_enabled(el):
            set_named_props(el, DEBUG_STEPPING_VALUES)
        elif el.tag == CONCURRENCY_TG and is_enabled(el):
            set_named_props(el, DEBUG_CONCURRENCY_VALUES)
    write_jmx(tree, jmx_path)


def list_thread_groups(jmx_path: str) -> list[dict[str, str]]:
    """List all supported thread groups for run-time overrides."""
    tree = parse_jmx(jmx_path)
    groups: list[dict[str, str]] = []
    for el in tree.iter():
        indexed = thread_group_key(el, len(groups))
        if indexed:
            key, group_type = indexed
            groups.append({
                "key": key,
                "name": el.get("testname") or "",
                "type": group_type,
                "enabled": is_enabled(el),
            })
    return groups


def sum_enabled_thread_group_threads(jmx_path: str) -> int:
    """Sum thread counts of enabled supported thread groups."""
    tree = parse_jmx(jmx_path)
    total = 0
    for el in tree.iter():
        if not is_enabled(el):
            continue
        group_type = thread_group_type(el)
        if not group_type:
            continue
        if group_type == "concurrency_thread_group":
            total += to_non_negative_int(first_named_prop_text(el, "TargetLevel"))
        else:
            total += to_non_negative_int(first_named_prop_text(el, "ThreadGroup.num_threads"))
    return total


def thread_group_key(el: Any, index: int) -> tuple[str, str] | None:
    group_type = thread_group_type(el)
    if not group_type:
        return None
    return f"{group_type}:{index}", group_type


def thread_group_type(el: Any) -> str | None:
    if el.tag == "ThreadGroup" and el.get("testclass") == "ThreadGroup":
        return THREAD_GROUP_TYPE_BY_TAG["ThreadGroup"]
    if el.tag in (STEPPING_TG, CONCURRENCY_TG):
        return THREAD_GROUP_TYPE_BY_TAG[el.tag]
    return None


def is_thread_group(el: Any) -> bool:
    return thread_group_type(el) is not None


def _is_transaction_controller(el: Any) -> bool:
    return el.tag == "TransactionController" and (el.get("testclass") in (None, "TransactionController"))


def pairs_in_hash_tree(hash_tree: Any):
    children = list(hash_tree)
    index = 0
    while index < len(children):
        node = children[index]
        child_hash = children[index + 1] if index + 1 < len(children) and children[index + 1].tag == "hashTree" else None
        yield node, child_hash
        index += 2 if child_hash is not None else 1


def walk_jmeter_pairs(hash_tree: Any, thread_group_name: str = ""):
    for node, child_hash in pairs_in_hash_tree(hash_tree):
        current_thread_group = thread_group_name
        if is_thread_group(node):
            current_thread_group = node.get("testname") or ""
        yield node, child_hash, current_thread_group, hash_tree
        if child_hash is not None:
            yield from walk_jmeter_pairs(child_hash, current_thread_group)


def list_transactions(jmx_path: str) -> list[dict[str, str]]:
    """List TransactionController targets, falling back to thread groups."""
    tree = parse_jmx(jmx_path)
    root_hash = tree.getroot().find("hashTree")
    if root_hash is None:
        return []

    transactions: list[dict[str, str]] = []
    for node, _child_hash, thread_group_name, _parent_hash in walk_jmeter_pairs(root_hash):
        if not _is_transaction_controller(node):
            continue
        transactions.append({
            "key": f"transaction:{len(transactions)}",
            "name": node.get("testname") or "",
            "thread_group": thread_group_name,
            "enabled": is_enabled(node),
        })
    if transactions:
        return transactions

    thread_groups: list[dict[str, str]] = []
    for node, _child_hash, _thread_group_name, _parent_hash in walk_jmeter_pairs(root_hash):
        indexed = thread_group_key(node, len(thread_groups))
        if not indexed:
            continue
        key, _group_type = indexed
        name = node.get("testname") or ""
        thread_groups.append({
            "key": key,
            "name": name,
            "thread_group": name,
            "enabled": is_enabled(node),
        })
    return thread_groups


def enable_transaction_parent_samples(jmx_path: str) -> int:
    """Enable parent samples and return the number of enabled controllers."""
    tree = parse_jmx(jmx_path)
    enabled_count = 0
    for node in tree.iter("TransactionController"):
        if not _is_transaction_controller(node) or not is_enabled(node):
            continue
        enabled_count += 1
        prop = node.find("./boolProp[@name='TransactionController.parent']")
        if prop is None:
            prop = etree.SubElement(node, "boolProp", name="TransactionController.parent")
        if prop.text != "true":
            prop.text = "true"
    write_jmx(tree, jmx_path)
    return enabled_count


def _override_key(item: dict[str, str]) -> str:
    return str(item.get("key") or item.get("name") or "").strip()


def _override_by_key(thread_group_overrides: list[dict] | None) -> dict[str, dict]:
    overrides: dict[str, dict] = {}
    for item in thread_group_overrides or []:
        key = _override_key(item)
        if key:
            overrides[key] = item
    return overrides


def _resolve_thread_values(
    el: Any,
    key: str,
    overrides: dict[str, dict],
    num_threads: str,
    ramp_time: str,
    duration: str,
) -> tuple[str, str, str] | None:
    override = overrides.get(key) or overrides.get(el.get("testname") or "")
    if override and override.get("mode") == "custom":
        return (
            str(override.get("num_threads") or num_threads),
            str(override.get("ramp_time") or ramp_time),
            duration,
        )
    return num_threads, ramp_time, duration


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def update_run_thread(
    jmx_path: str,
    dest_path: str,
    num_threads: str,
    ramp_time: str,
    duration: str,
    thread_group_overrides: list[dict[str, str]] | None = None,
) -> None:
    """Apply run-time thread group settings and write to dest_path."""
    tree = parse_jmx(jmx_path)
    overrides = _override_by_key(thread_group_overrides)
    group_index = 0
    for el in tree.iter():
        indexed = thread_group_key(el, group_index)
        if indexed:
            key, _group_type = indexed
            group_index += 1
        else:
            key = ""
        override = overrides.get(key) or overrides.get(el.get("testname") or "")
        if override and override.get("enabled") is not None:
            el.set("enabled", "true" if as_bool(override.get("enabled")) else "false")
        if not is_enabled(el):
            continue
        fixed_mode = bool(override and override.get("mode") == "fixed")

        if el.tag == "ThreadGroup" and el.get("testclass") == "ThreadGroup":
            if fixed_mode:
                set_named_props(el, {
                    "LoopController.continue_forever": "true",
                    "LoopController.loops": "-1",
                    "ThreadGroup.duration": duration,
                    "ThreadGroup.scheduler": "true",
                })
                continue
            values = _resolve_thread_values(el, key, overrides, num_threads, ramp_time, duration)
            if values is None:
                continue
            group_num_threads, group_ramp_time, group_duration = values
            set_named_props(el, {
                "LoopController.continue_forever": "true",
                "LoopController.loops": "-1",
                "ThreadGroup.num_threads": group_num_threads,
                "ThreadGroup.ramp_time": group_ramp_time,
                "ThreadGroup.duration": group_duration,
                "ThreadGroup.scheduler": "true",
            })
        elif el.tag == STEPPING_TG:
            if fixed_mode:
                set_named_props(el, {"flighttime": duration})
                continue
            values = _resolve_thread_values(el, key, overrides, num_threads, ramp_time, duration)
            if values is None:
                continue
            group_num_threads, group_ramp_time, group_duration = values
            set_named_props(el, {
                "ThreadGroup.num_threads": group_num_threads,
                "Threads initial delay": "0",
                "Start users count burst": "0",
                "Start users count": "1",
                "Start users period": group_ramp_time,
                "flighttime": group_duration,
                "rampUp": "1",
            })
        elif el.tag == CONCURRENCY_TG:
            if fixed_mode:
                set_named_props(el, {"Hold": duration})
                continue
            values = _resolve_thread_values(el, key, overrides, num_threads, ramp_time, duration)
            if values is None:
                continue
            group_num_threads, group_ramp_time, group_duration = values
            set_named_props(el, {
                "TargetLevel": group_num_threads,
                "RampUp": group_ramp_time,
                "Hold": group_duration,
            })

    write_jmx(tree, dest_path)

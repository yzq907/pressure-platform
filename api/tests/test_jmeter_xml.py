"""app/core/jmeter_xml.py 的单测。纯函数测试，不需要 DB / HTTP client。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from app.core import jmeter_xml

SAMPLE_JMX = Path(__file__).parent / "fixtures" / "sample.jmx"


def _copy_sample(tmp_path: Path) -> Path:
    """拷贝 sample.jmx 到 tmp 目录，避免在原文件上改"""
    dst = tmp_path / "test.jmx"
    shutil.copy(SAMPLE_JMX, dst)
    return dst


def _find_named_text(tree: etree._ElementTree, parent_tag: str, name: str) -> list[str | None]:
    """返回所有 <parent_tag> 下面 name=name 的节点的 text"""
    results: list[str | None] = []
    for parent in tree.iter(parent_tag):
        for prop in parent.iter():
            if prop.get("name") == name:
                results.append(prop.text)
    return results


def test_update_debug_thread_handles_default_threadgroup(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    jmeter_xml.update_debug_thread(str(jmx))

    tree = etree.parse(str(jmx))
    # 默认 ThreadGroup 的 num_threads / ramp_time / LoopController.loops 都被压低为 1
    nums = _find_named_text(tree, "ThreadGroup", "ThreadGroup.num_threads")
    ramps = _find_named_text(tree, "ThreadGroup", "ThreadGroup.ramp_time")
    loops = _find_named_text(tree, "ThreadGroup", "LoopController.loops")
    assert nums == ["1"]
    assert ramps == ["1"]
    assert loops == ["1"]


def test_update_debug_thread_skips_disabled_stepping(tmp_path: Path) -> None:
    """sample.jmx 里 SteppingThreadGroup 是 disabled，不应被改写"""
    jmx = _copy_sample(tmp_path)
    jmeter_xml.update_debug_thread(str(jmx))

    tree = etree.parse(str(jmx))
    stepping_nums = _find_named_text(
        tree, "kg.apc.jmeter.threads.SteppingThreadGroup", "ThreadGroup.num_threads"
    )
    # 原始是 200，不被改写还是 200
    assert stepping_nums == ["200"]


def test_update_debug_thread_handles_concurrency(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    jmeter_xml.update_debug_thread(str(jmx))

    tree = etree.parse(str(jmx))
    target = _find_named_text(
        tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "TargetLevel"
    )
    ramp = _find_named_text(
        tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "RampUp"
    )
    steps = _find_named_text(
        tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "Steps"
    )
    hold = _find_named_text(
        tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "Hold"
    )
    unit = _find_named_text(
        tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "Unit"
    )
    assert target == ["1"]
    assert ramp == ["1"]
    assert steps == ["1"]
    assert hold == ["1"]
    assert unit == ["S"]


def test_update_debug_thread_modifies_stepping_when_enabled(tmp_path: Path) -> None:
    """手动把 SteppingThreadGroup 改为 enabled 后，重新跑 update_debug_thread 应会修改"""
    jmx = _copy_sample(tmp_path)
    # 先把 stepping 改为 enabled
    tree = etree.parse(str(jmx))
    for el in tree.iter("kg.apc.jmeter.threads.SteppingThreadGroup"):
        el.set("enabled", "true")
    tree.write(str(jmx), xml_declaration=True, encoding="UTF-8", standalone=False)

    jmeter_xml.update_debug_thread(str(jmx))

    tree = etree.parse(str(jmx))
    nums = _find_named_text(
        tree, "kg.apc.jmeter.threads.SteppingThreadGroup", "ThreadGroup.num_threads"
    )
    starts = _find_named_text(
        tree, "kg.apc.jmeter.threads.SteppingThreadGroup", "Start users count"
    )
    flight = _find_named_text(
        tree, "kg.apc.jmeter.threads.SteppingThreadGroup", "flighttime"
    )
    # 对齐 _DEBUG_STEPPING_VALUES
    assert nums == ["2"]
    assert starts == ["1"]
    assert flight == ["1"]


def test_exist_csv_filename_true(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    assert jmeter_xml.exist_csv_filename(str(jmx), "data.csv") is True


def test_exist_csv_filename_false_for_missing_name(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    assert jmeter_xml.exist_csv_filename(str(jmx), "not_exist.csv") is False


def test_update_csv_filename_changes_filepath(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    new_path = "/data/users/case_a/csv/data.csv"
    jmeter_xml.update_csv_filename(str(jmx), "data.csv", new_path)

    tree = etree.parse(str(jmx))
    filenames = _find_named_text(tree, "CSVDataSet", "filename")
    assert filenames == [new_path]


def test_update_jar_classpath_changes_testplan(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    new_path = "/data/users/case_a/jar"
    jmeter_xml.update_jar_classpath(str(jmx), new_path)

    tree = etree.parse(str(jmx))
    cps = _find_named_text(tree, "TestPlan", "TestPlan.user_define_classpath")
    assert cps == [new_path]


def test_xml_still_valid_after_all_ops(tmp_path: Path) -> None:
    """连续跑 4 个操作后，XML 应仍可被重新解析"""
    jmx = _copy_sample(tmp_path)
    jmeter_xml.update_debug_thread(str(jmx))
    jmeter_xml.update_csv_filename(str(jmx), "data.csv", "/new/path.csv")
    jmeter_xml.update_jar_classpath(str(jmx), "/new/jar/dir")
    assert jmeter_xml.exist_csv_filename(str(jmx), "data.csv") is True
    # 可以重新解析
    etree.parse(str(jmx))

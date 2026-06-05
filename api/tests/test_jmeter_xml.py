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


def _jmeter_property_value(parent: etree._Element, name: str) -> str | None:
    for prop in parent.iter():
        if prop.get("name") == name:
            return prop.text
        prop_children = list(prop)
        if prop_children and prop_children[0].tag == "name" and prop_children[0].text == name:
            for child in prop_children[1:]:
                if child.tag == "value":
                    return child.text
    return None


def _write_transaction_sample(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="业务线程组" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">5</stringProp>
      </ThreadGroup>
      <hashTree>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="策略获取" enabled="true">
          <boolProp name="TransactionController.parent">true</boolProp>
        </TransactionController>
        <hashTree>
          <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="策略接口" enabled="true"/>
          <hashTree/>
        </hashTree>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="获取设备信息" enabled="false"/>
        <hashTree/>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""",
        encoding="utf-8",
    )


def _write_thread_group_transaction_sample(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="2_策略获取" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">5</stringProp>
      </ThreadGroup>
      <hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="策略接口" enabled="true"/>
        <hashTree/>
      </hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="7_获取应用版本" enabled="false">
        <stringProp name="ThreadGroup.num_threads">5</stringProp>
      </ThreadGroup>
      <hashTree/>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""",
        encoding="utf-8",
    )


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


def test_update_run_thread_standard_threadgroup_uses_duration_until_stopped(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    dest = tmp_path / "run.jmx"

    jmeter_xml.update_run_thread(str(jmx), str(dest), "50", "30", "600")

    tree = etree.parse(str(dest))
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.num_threads") == ["50"]
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.ramp_time") == ["30"]
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.duration") == ["600"]
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.scheduler") == ["true"]
    assert _find_named_text(tree, "ThreadGroup", "LoopController.continue_forever") == ["true"]
    assert _find_named_text(tree, "ThreadGroup", "LoopController.loops") == ["-1"]


def test_update_run_thread_applies_named_thread_group_overrides(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    dest = tmp_path / "run.jmx"

    jmeter_xml.update_run_thread(
        str(jmx),
        str(dest),
        "50",
        "30",
        "600",
        [
            {
                "name": "Default ThreadGroup",
                "mode": "custom",
                "num_threads": "1",
                "ramp_time": "1",
            },
            {
                "name": "Concurrency Group",
                "mode": "fixed",
            },
        ],
    )

    tree = etree.parse(str(dest))
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.num_threads") == ["1"]
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.ramp_time") == ["1"]
    assert _find_named_text(tree, "ThreadGroup", "ThreadGroup.duration") == ["600"]
    assert _find_named_text(tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "TargetLevel") == ["100"]
    assert _find_named_text(tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "RampUp") == ["60"]
    assert _find_named_text(tree, "com.blazemeter.jmeter.threads.concurrency.ConcurrencyThreadGroup", "Hold") == ["300"]


def test_list_thread_groups_returns_all_groups_with_key_and_enabled(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)

    groups = jmeter_xml.list_thread_groups(str(jmx))

    assert groups == [
        {"key": "thread_group:0", "name": "Default ThreadGroup", "type": "thread_group", "enabled": True},
        {"key": "stepping_thread_group:1", "name": "Disabled Stepping", "type": "stepping_thread_group", "enabled": False},
        {"key": "concurrency_thread_group:2", "name": "Concurrency Group", "type": "concurrency_thread_group", "enabled": True},
    ]


def test_update_run_thread_applies_enabled_override_by_key(tmp_path: Path) -> None:
    jmx = _copy_sample(tmp_path)
    dest = tmp_path / "run.jmx"

    jmeter_xml.update_run_thread(
        str(jmx),
        str(dest),
        "50",
        "30",
        "600",
        [
            {"key": "thread_group:0", "name": "Default ThreadGroup", "mode": "global", "enabled": False},
            {
                "key": "stepping_thread_group:1",
                "name": "Disabled Stepping",
                "mode": "custom",
                "enabled": True,
                "num_threads": "2",
                "ramp_time": "1",
            },
        ],
    )

    tree = etree.parse(str(dest))
    assert next(tree.iter("ThreadGroup")).get("enabled") == "false"
    stepping = next(tree.iter("kg.apc.jmeter.threads.SteppingThreadGroup"))
    assert stepping.get("enabled") == "true"
    assert _find_named_text(tree, "kg.apc.jmeter.threads.SteppingThreadGroup", "ThreadGroup.num_threads") == ["2"]
    assert _find_named_text(tree, "kg.apc.jmeter.threads.SteppingThreadGroup", "flighttime") == ["600"]


def test_list_transactions_returns_transaction_controllers_with_thread_group(tmp_path: Path) -> None:
    jmx = tmp_path / "transactions.jmx"
    _write_transaction_sample(jmx)

    transactions = jmeter_xml.list_transactions(str(jmx))

    assert transactions == [
        {
            "key": "transaction:0",
            "name": "策略获取",
            "thread_group": "业务线程组",
            "enabled": True,
        },
        {
            "key": "transaction:1",
            "name": "获取设备信息",
            "thread_group": "业务线程组",
            "enabled": False,
        },
    ]


def test_list_transactions_falls_back_to_thread_groups_when_no_transaction_controller(tmp_path: Path) -> None:
    jmx = tmp_path / "thread_group_transactions.jmx"
    _write_thread_group_transaction_sample(jmx)

    transactions = jmeter_xml.list_transactions(str(jmx))

    assert transactions == [
        {
            "key": "thread_group:0",
            "name": "2_策略获取",
            "thread_group": "2_策略获取",
            "enabled": True,
        },
        {
            "key": "thread_group:1",
            "name": "7_获取应用版本",
            "thread_group": "7_获取应用版本",
            "enabled": False,
        },
    ]


def test_apply_thread_group_pacing_adds_jsr223_cycle_timer_when_pacing_positive(tmp_path: Path) -> None:
    src = tmp_path / "thread_group_transactions.jmx"
    dest = tmp_path / "run.jmx"
    _write_thread_group_transaction_sample(src)

    jmeter_xml.apply_thread_group_pacing(
        str(src),
        str(dest),
        [{"key": "thread_group:0", "name": "2_策略获取", "enabled": True, "pacing_ms": 155}],
    )

    tree = etree.parse(str(dest))
    thread_group = next(tree.iter("ThreadGroup"))
    thread_group_hash = thread_group.getnext()
    assert thread_group_hash is not None
    timer = thread_group_hash[0]
    assert timer.tag == "JSR223Timer"
    assert timer.get("testname") == "平台Pacing_2_策略获取"
    assert _jmeter_property_value(timer, "parameters") == "155"
    assert _jmeter_property_value(timer, "scriptLanguage") == "groovy"
    assert "platform_pacing_next_start" in (_jmeter_property_value(timer, "script") or "")
    assert list(tree.iter("ConstantTimer")) == []
    assert next(thread_group_hash.iter("HTTPSamplerProxy")).get("testname") == "策略接口"


def test_apply_thread_group_pacing_skips_when_pacing_zero(tmp_path: Path) -> None:
    src = tmp_path / "thread_group_transactions.jmx"
    dest = tmp_path / "run.jmx"
    _write_thread_group_transaction_sample(src)

    jmeter_xml.apply_thread_group_pacing(
        str(src),
        str(dest),
        [{"key": "thread_group:0", "name": "2_策略获取", "enabled": True, "pacing_ms": 0}],
    )

    tree = etree.parse(str(dest))
    assert list(tree.iter("JSR223Timer")) == []
    assert list(tree.iter("ConstantTimer")) == []


def test_sum_enabled_thread_group_threads_counts_final_enabled_groups(tmp_path: Path) -> None:
    jmx = tmp_path / "thread_group_transactions.jmx"
    _write_thread_group_transaction_sample(jmx)

    jmeter_xml.update_run_thread(
        str(jmx),
        str(jmx),
        "10",
        "1",
        "60",
        [
            {"key": "thread_group:0", "name": "2_策略获取", "enabled": True, "mode": "custom", "num_threads": "7"},
            {"key": "thread_group:1", "name": "7_获取应用版本", "enabled": True, "mode": "custom", "num_threads": "1"},
        ],
    )

    assert jmeter_xml.sum_enabled_thread_group_threads(str(jmx)) == 8


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


def test_update_upload_file_paths_changes_http_file_arg_only(tmp_path: Path) -> None:
    jmx = tmp_path / "upload_file.jmx"
    jmx.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="upload" enabled="true">
      <elementProp name="HTTPsampler.Files" elementType="HTTPFileArgs">
        <collectionProp name="HTTPFileArgs.files">
          <elementProp name="avatar.jpg" elementType="HTTPFileArg">
            <stringProp name="File.path">avatar.jpg</stringProp>
            <stringProp name="File.paramname">file</stringProp>
            <stringProp name="File.mimetype">image/jpeg</stringProp>
          </elementProp>
        </collectionProp>
      </elementProp>
      <stringProp name="Argument.value">avatar.jpg</stringProp>
    </HTTPSamplerProxy>
  </hashTree>
</jmeterTestPlan>
""",
        encoding="utf-8",
    )

    new_path = "/data/case/upload/avatar.jpg"
    assert jmeter_xml.exist_upload_file_path(str(jmx), "avatar.jpg") is True

    jmeter_xml.update_upload_file_paths(str(jmx), {"avatar.jpg": new_path})

    tree = etree.parse(str(jmx))
    assert _find_named_text(tree, "HTTPSamplerProxy", "File.path") == [new_path]
    assert _find_named_text(tree, "HTTPSamplerProxy", "Argument.value") == ["avatar.jpg"]


def test_csv_filename_matches_windows_path_basename(tmp_path: Path) -> None:
    jmx = tmp_path / "windows_csv_path.jmx"
    jmx.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <CSVDataSet guiclass="TestBeanGUI" testclass="CSVDataSet" testname="csv data" enabled="true">
      <stringProp name="filename">D:\\sessionId_data.dat</stringProp>
    </CSVDataSet>
  </hashTree>
</jmeterTestPlan>
""",
        encoding="utf-8",
    )

    assert jmeter_xml.exist_csv_filename(str(jmx), "sessionId_data.dat") is True


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

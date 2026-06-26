from __future__ import annotations

from lxml import etree

from app.core.jmeter_error_samples import apply_error_sample_listener


SAMPLE_JMX = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Thread Group" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
      </ThreadGroup>
      <hashTree/>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


def _prop(parent, name: str) -> str | None:
    for child in parent.iter():
        if child.get("name") == name:
            return child.text
    return None


def test_apply_error_sample_listener_injects_single_testplan_listener(tmp_path) -> None:
    src = tmp_path / "source.jmx"
    dest = tmp_path / "run.jmx"
    src.write_text(SAMPLE_JMX, encoding="utf-8")

    apply_error_sample_listener(str(src), str(dest))

    tree = etree.parse(str(dest))
    listeners = tree.findall(".//JSR223Listener")
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener.get("testname") == "平台错误请求采样"
    assert _prop(listener, "scriptLanguage") == "groovy"
    assert "platform.errorSampleFile" in (_prop(listener, "script") or "")
    assert "platform.errorSamplePerCodeLimit" in (_prop(listener, "script") or "")
    assert "platform.errorSampleTotalLimit" in (_prop(listener, "script") or "")
    assert "ASSERTION_FAILED" in (_prop(listener, "script") or "")

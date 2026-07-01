from __future__ import annotations

from lxml import etree

from app.core.jmeter_error_samples import apply_error_result_collector, apply_error_sample_listener


SAMPLE_JMX = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan>
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Thread Group" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
      </ThreadGroup>
      <hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="GET /api" enabled="true">
          <stringProp name="HTTPSampler.domain">example.com</stringProp>
        </HTTPSamplerProxy>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="业务断言" enabled="true"/>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


def _prop(parent, name: str) -> str | None:
    for child in parent.iter():
        if child.get("name") == name:
            return child.text
    return None


def test_apply_error_sample_listener_injects_sampler_assertion_after_existing_assertions(tmp_path) -> None:
    src = tmp_path / "source.jmx"
    dest = tmp_path / "run.jmx"
    src.write_text(SAMPLE_JMX, encoding="utf-8")

    apply_error_sample_listener(str(src), str(dest))

    tree = etree.parse(str(dest))
    assert tree.findall(".//JSR223Listener") == []
    assertions = tree.findall(".//JSR223Assertion[@testname='平台错误请求采样']")
    assert len(assertions) == 1
    assertion = assertions[0]
    assert assertion.get("testname") == "平台错误请求采样"
    assert _prop(assertion, "scriptLanguage") == "groovy"
    assert "platform.errorSampleFile" in (_prop(assertion, "script") or "")
    assert "platform.errorSamplePerCodeLimit" in (_prop(assertion, "script") or "")
    assert "platform.errorSampleTotalLimit" in (_prop(assertion, "script") or "")
    assert "ASSERTION_FAILED" in (_prop(assertion, "script") or "")

    sampler_hash = assertion.getparent()
    assert sampler_hash.getprevious().tag == "HTTPSamplerProxy"
    assert sampler_hash.getprevious().get("testname") == "GET /api"
    assert [child.get("testname") for child in sampler_hash if child.tag != "hashTree"] == ["业务断言", "平台错误请求采样"]


def test_apply_error_result_collector_injects_error_only_xml_collector(tmp_path) -> None:
    src = tmp_path / "source.jmx"
    dest = tmp_path / "run.jmx"
    error_xml = tmp_path / "artifacts" / "error_samples.xml"
    src.write_text(SAMPLE_JMX, encoding="utf-8")

    apply_error_result_collector(str(src), str(dest), str(error_xml))

    tree = etree.parse(str(dest))
    collectors = tree.findall(".//ResultCollector[@testname='平台错误结果树']")
    assert len(collectors) == 1
    collector = collectors[0]
    assert _prop(collector, "ResultCollector.error_logging") == "true"
    assert _prop(collector, "filename") == str(error_xml)
    assert collector.find(".//responseData").text == "true"
    assert collector.find(".//responseHeaders").text == "true"
    assert collector.find(".//requestHeaders").text == "true"
    assert collector.find(".//xml").text == "true"

"""JMeterXMLBuilder 单元测试。不需要 DB/HTTP client，纯 XML 操作验证。"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from app.core.jmeter_xml import JMeterXMLBuilder
from app.schemas.jmx_assertion import AssertionVO
from app.schemas.jmx_csv import CsvDataVO, CsvFileVO
from app.schemas.jmx_http import HttpVO
from app.schemas.jmx_java import JavaVO
from app.schemas.jmx_thread import (
    ConcurrencyThreadGroupVO,
    SteppingThreadGroupVO,
    ThreadGroupVO,
)

BASE_HTTP = Path(__file__).resolve().parent.parent / "jmx_base" / "thread_group_http.jmx"
BASE_JAVA = Path(__file__).resolve().parent.parent / "jmx_base" / "thread_group_java.jmx"


@pytest.fixture
def builder() -> JMeterXMLBuilder:
    b = JMeterXMLBuilder()
    b.init(str(BASE_HTTP))
    return b


@pytest.fixture
def builder_java() -> JMeterXMLBuilder:
    b = JMeterXMLBuilder()
    b.init(str(BASE_JAVA))
    return b


def _reparse(path: str) -> etree._ElementTree:
    return etree.parse(path)


def _find_first_text(root: etree._Element, tag: str, name: str) -> str | None:
    for el in root.iter(tag):
        if el.get("name") == name:
            return el.text
    return None


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_init_and_write_does_not_corrupt(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.write_jmx_file(str(out))
    # 重新 parse 不报错
    tree = _reparse(str(out))
    assert tree.getroot().tag == "jmeterTestPlan"


def test_write_expands_empty_elements(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    """空元素必须展开为 <tag></tag> 而不是自闭和 <tag/>"""
    out = tmp_path / "out.jmx"
    builder.write_jmx_file(str(out))
    content = out.read_text()
    # 模板里有大量 <stringProp name="..."></stringProp>
    assert '<stringProp name="TestPlan.comments"></stringProp>' in content


# ---------------------------------------------------------------------------
# thread groups
# ---------------------------------------------------------------------------


def test_update_thread_group(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.update_thread_group(
        ThreadGroupVO(num_threads="100", ramp_time="60", loops="10", scheduler=0, delayed_start=0, same_user_on_next_iteration=0)
    )
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    assert _find_first_text(root, "stringProp", "ThreadGroup.num_threads") == "100"
    assert _find_first_text(root, "stringProp", "ThreadGroup.ramp_time") == "60"
    # scheduler==0 → false
    assert _find_first_text(root, "boolProp", "ThreadGroup.scheduler") == "false"
    # same_user_on_next_iteration==0 → false
    assert _find_first_text(root, "boolProp", "ThreadGroup.same_user_on_next_iteration") == "false"


def test_update_stepping_thread_group(tmp_path: Path) -> None:
    b = JMeterXMLBuilder()
    b.init(str(Path(__file__).resolve().parent.parent / "jmx_base" / "stepping_thread_group_http.jmx"))
    out = tmp_path / "out.jmx"
    b.update_stepping_thread_group(
        SteppingThreadGroupVO(
            num_threads="500", first_wait_for_seconds="0", then_start_threads="10",
            next_add_threads="50", next_add_threads_every_seconds="30",
            using_ramp_up_seconds="5", then_hold_load_for_seconds="60",
            finally_stop_threads="50", finally_stop_threads_every_seconds="10",
        )
    )
    b.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    assert _find_first_text(root, "stringProp", "ThreadGroup.num_threads") == "500"
    assert _find_first_text(root, "stringProp", "Threads initial delay") == "0"
    assert _find_first_text(root, "stringProp", "flighttime") == "60"


def test_update_concurrency_thread_group(tmp_path: Path) -> None:
    b = JMeterXMLBuilder()
    b.init(str(Path(__file__).resolve().parent.parent / "jmx_base" / "concurrency_thread_group_http.jmx"))
    out = tmp_path / "out.jmx"
    b.update_concurrency_thread_group(
        ConcurrencyThreadGroupVO(target_concurrency="200", ramp_up_time="60", ramp_up_steps_count="5", hold_target_rate_time="300")
    )
    b.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    assert _find_first_text(root, "stringProp", "TargetLevel") == "200"
    assert _find_first_text(root, "stringProp", "RampUp") == "60"
    assert _find_first_text(root, "stringProp", "Steps") == "5"
    assert _find_first_text(root, "stringProp", "Hold") == "300"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def test_update_http_sample(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.update_http_sample(HttpVO(domain="api.example.com", port="443", protocol="HTTPS", path="/v1/user", content_encoding="UTF-8", method="POST"))
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    assert _find_first_text(root, "stringProp", "HTTPSampler.domain") == "api.example.com"
    assert _find_first_text(root, "stringProp", "HTTPSampler.port") == "443"
    assert _find_first_text(root, "stringProp", "HTTPSampler.protocol") == "HTTPS"
    assert _find_first_text(root, "stringProp", "HTTPSampler.path") == "/v1/user"
    assert _find_first_text(root, "stringProp", "HTTPSampler.method") == "POST"


def test_add_http_header(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.add_http_header("Content-Type", "application/json")
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    headers = []
    for ep in root.iter("elementProp"):
        if ep.get("elementType") == "Header":
            name = None
            value = None
            for child in ep:
                if child.get("name") == "Header.name":
                    name = child.text
                elif child.get("name") == "Header.value":
                    value = child.text
            headers.append((name, value))
    assert ("Content-Type", "application/json") in headers


def test_add_http_param(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.add_http_param("page", "1")
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    found = False
    for ep in root.iter("elementProp"):
        if ep.get("elementType") == "HTTPArgument" and ep.get("name") == "page":
            for child in ep:
                if child.get("name") == "Argument.value" and child.text == "1":
                    found = True
    assert found


def test_add_http_body(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.add_http_body('{"key":"val"}')
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    # postBodyRaw = true
    assert _find_first_text(root, "boolProp", "HTTPSampler.postBodyRaw") == "true"
    # Argument.value = body
    assert _find_first_text(root, "stringProp", "Argument.value") == '{"key":"val"}'


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


def test_update_java_request(builder_java: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder_java.update_java_request("com.example.MySampler")
    builder_java.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    assert _find_first_text(root, "stringProp", "classname") == "com.example.MySampler"


def test_add_java_param(builder_java: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder_java.add_java_param("threads", "100")
    builder_java.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    found = False
    for ep in root.iter("elementProp"):
        if ep.get("elementType") == "Argument" and ep.get("name") == "threads":
            for child in ep:
                if child.get("name") == "Argument.value" and child.text == "100":
                    found = True
    assert found


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------


def test_add_assertion_all_three(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.add_assertion(0, response_code="200", response_message="OK", json_path="$.code", expected_value="0")
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    # 采样器同级 hashTree 下应该有 ResponseAssertion × 2 + JSONPathAssertion × 1
    sampler = next(root.iter("HTTPSamplerProxy"))
    parent = sampler.getparent()
    ht = parent.find("hashTree")
    # hashTree 的子节点里应该有 3 个 assertion 和 3 个 trailing hashTree
    ras = [el for el in ht if el.tag == "ResponseAssertion"]
    jpas = [el for el in ht if el.tag == "JSONPathAssertion"]
    assert len(ras) == 2
    assert len(jpas) == 1


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_add_csv(builder: JMeterXMLBuilder, tmp_path: Path) -> None:
    out = tmp_path / "out.jmx"
    builder.add_csv(
        CsvDataVO(
            csv_file_vo_list=[CsvFileVO(filename="data.csv", variable_names="user,pass")],
            file_encoding="UTF-8", delimiter=",", ignore_first_line=1,
            allow_quoted_data=0, recycle_on_eof=1, stop_thread_on_eof=0,
            sharing_mode="Current thread group",
        ),
        sample_type=0,
    )
    builder.write_jmx_file(str(out))
    root = _reparse(str(out)).getroot()
    csv_sets = list(root.iter("CSVDataSet"))
    assert len(csv_sets) == 1
    assert _find_first_text(root, "stringProp", "filename") == "data.csv"
    assert _find_first_text(root, "stringProp", "variableNames") == "user,pass"

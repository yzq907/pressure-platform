"""Failure sample listener injection for JMeter run JMX files."""

from __future__ import annotations

from typing import Any
from pathlib import Path

from lxml import etree

from app.core.jmeter_thread_groups import walk_jmeter_pairs
from app.core.jmeter_xml_support import parse_jmx, write_jmx


ERROR_SAMPLE_FILENAME = "error_samples.jsonl"
ERROR_SAMPLE_XML_FILENAME = "error_samples.xml"
ERROR_SAMPLE_INTERNAL_DIRNAME = "_internal/error_samples"
ERROR_SAMPLE_FILE_PROP = "platform.errorSampleFile"
ERROR_SAMPLE_PER_CODE_LIMIT_PROP = "platform.errorSamplePerCodeLimit"
ERROR_SAMPLE_TOTAL_LIMIT_PROP = "platform.errorSampleTotalLimit"
ERROR_SAMPLE_TEXT_MAX_BYTES_PROP = "platform.errorSampleTextMaxBytes"
DEFAULT_ERROR_SAMPLE_PER_CODE_LIMIT = 10
DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT = 100
DEFAULT_ERROR_SAMPLE_TEXT_MAX_BYTES = 8192


def error_sample_dir_from_artifact_dir(artifact_dir: str | Path) -> Path:
    """Return the internal report directory used for error sample files."""
    return Path(artifact_dir).resolve().parent / ERROR_SAMPLE_INTERNAL_DIRNAME


def is_error_sample_artifact_name(name: str) -> bool:
    """Whether a top-level artifact name is an internal error sample file."""
    path_name = Path(name).name
    if path_name != name:
        return False
    return (
        path_name in {ERROR_SAMPLE_FILENAME, ERROR_SAMPLE_XML_FILENAME}
        or (
            path_name.startswith("error_samples.")
            and path_name.endswith((".jsonl", ".xml"))
        )
    )


_ERROR_SAMPLE_LISTENER_SCRIPT = r"""import groovy.json.JsonOutput

if (prev == null || prev.isSuccessful()) {
    return
}

String propOrDefault(String name, String defaultValue) {
    String value = props.getProperty(name)
    return value == null || value.trim().length() == 0 ? defaultValue : value.trim()
}

int intProp(String name, int defaultValue) {
    try {
        return Integer.parseInt(propOrDefault(name, String.valueOf(defaultValue)))
    } catch (Throwable ignored) {
        return defaultValue
    }
}

String truncateText(Object value, int maxChars) {
    if (value == null) {
        return ""
    }
    String text = value.toString()
    if (maxChars <= 0 || text.length() <= maxChars) {
        return text
    }
    return text.substring(0, maxChars) + "\n... [truncated]"
}

String maskSensitiveHeaders(Object value) {
    String text = value == null ? "" : value.toString()
    return text
        .replaceAll(/(?im)^(Authorization\s*:\s*).+$/, '$1******')
        .replaceAll(/(?im)^(Cookie\s*:\s*).+$/, '$1******')
        .replaceAll(/(?im)^(Set-Cookie\s*:\s*).+$/, '$1******')
        .replaceAll(/(?im)^([^:\r\n]*(token|password|secret)[^:\r\n]*\s*:\s*).+$/, '$1******')
}

List assertionMessages = []
boolean assertionFailed = false
def assertions = prev.getAssertionResults()
if (assertions != null) {
    assertions.each { assertion ->
        if (assertion != null && (assertion.isFailure() || assertion.isError())) {
            assertionFailed = true
            String message = assertion.getFailureMessage()
            if (message != null && message.trim().length() > 0) {
                assertionMessages.add(message.trim())
            }
        }
    }
}

String responseCode = prev.getResponseCode() == null ? "" : prev.getResponseCode().trim()
String errorType = responseCode
if (responseCode.length() == 0 || responseCode.startsWith("Non HTTP response code")) {
    errorType = assertionFailed ? "ASSERTION_FAILED" : "EXCEPTION"
} else if (responseCode == "200" && assertionFailed) {
    errorType = "ASSERTION_FAILED"
}
if (errorType.length() == 0) {
    errorType = "EXCEPTION"
}

int perCodeLimit = intProp("platform.errorSamplePerCodeLimit", 10)
int totalLimit = intProp("platform.errorSampleTotalLimit", 100)
if (perCodeLimit <= 0 || totalLimit <= 0) {
    return
}

String safeType = errorType.replaceAll(/[^A-Za-z0-9_.-]/, "_")
String typeKey = "platform.errorSamples.count." + safeType
String totalKey = "platform.errorSamples.total"

boolean shouldWrite = false
synchronized (props) {
    int typeCount = 0
    int totalCount = 0
    try { typeCount = Integer.parseInt(String.valueOf(props.get(typeKey) ?: "0")) } catch (Throwable ignored) {}
    try { totalCount = Integer.parseInt(String.valueOf(props.get(totalKey) ?: "0")) } catch (Throwable ignored) {}
    if (typeCount < perCodeLimit && totalCount < totalLimit) {
        props.put(typeKey, String.valueOf(typeCount + 1))
        props.put(totalKey, String.valueOf(totalCount + 1))
        shouldWrite = true
    }
}
if (!shouldWrite) {
    return
}

String filePath = propOrDefault("platform.errorSampleFile", "")
if (filePath.length() == 0) {
    return
}

int maxText = intProp("platform.errorSampleTextMaxBytes", 8192)
File file = new File(filePath)
File parent = file.getParentFile()
if (parent != null) {
    parent.mkdirs()
}

String requestHeaders = truncateText(maskSensitiveHeaders(prev.getRequestHeaders()), maxText)
String responseHeaders = truncateText(maskSensitiveHeaders(prev.getResponseHeaders()), maxText)
String responseBody = truncateText(prev.getResponseDataAsString(), maxText)
String requestUrl = ""
try {
    requestUrl = prev.getUrlAsString() ?: ""
} catch (Throwable ignored) {
    try {
        requestUrl = prev.getURL() == null ? "" : prev.getURL().toString()
    } catch (Throwable ignoredAgain) {
        requestUrl = ""
    }
}

def record = [
    sampleTime: prev.getTimeStamp(),
    label: prev.getSampleLabel() ?: "",
    threadName: prev.getThreadName() ?: "",
    responseCode: responseCode,
    responseMessage: prev.getResponseMessage() ?: "",
    elapsed: prev.getTime(),
    failureMessage: truncateText(assertionMessages.join("\n"), maxText),
    requestUrl: requestUrl,
    requestHeaders: requestHeaders,
    requestBody: "",
    responseHeaders: responseHeaders,
    responseBody: responseBody,
    truncated: responseBody.endsWith("... [truncated]") || requestHeaders.endsWith("... [truncated]") || responseHeaders.endsWith("... [truncated]"),
    errorType: errorType
]

synchronized (props) {
    file.withWriterAppend("UTF-8") { writer ->
        writer.write(JsonOutput.toJson(record))
        writer.write(System.lineSeparator())
    }
}
"""


def _jsr223_error_sample_assertion() -> Any:
    assertion = etree.Element(
        "JSR223Assertion",
        guiclass="TestBeanGUI",
        testclass="JSR223Assertion",
        testname="平台错误请求采样",
        enabled="true",
    )
    etree.SubElement(assertion, "stringProp", name="cacheKey").text = "true"
    etree.SubElement(assertion, "stringProp", name="filename").text = ""
    etree.SubElement(assertion, "stringProp", name="parameters").text = ""
    etree.SubElement(assertion, "stringProp", name="script").text = _ERROR_SAMPLE_LISTENER_SCRIPT
    etree.SubElement(assertion, "stringProp", name="scriptLanguage").text = "groovy"
    return assertion


def _sample_save_configuration() -> Any:
    value = etree.Element("value", {"class": "SampleSaveConfiguration"})
    fields = {
        "time": True,
        "latency": True,
        "timestamp": True,
        "success": True,
        "label": True,
        "code": True,
        "message": True,
        "threadName": True,
        "dataType": True,
        "encoding": False,
        "assertions": True,
        "subresults": True,
        "responseData": True,
        "samplerData": True,
        "xml": True,
        "fieldNames": True,
        "responseHeaders": True,
        "requestHeaders": True,
        "responseDataOnError": True,
        "saveAssertionResultsFailureMessage": True,
        "assertionsResultsToSave": 0,
        "bytes": True,
        "sentBytes": True,
        "url": True,
        "threadCounts": True,
        "idleTime": True,
        "connectTime": True,
    }
    for name, field_value in fields.items():
        el = etree.SubElement(value, name)
        if isinstance(field_value, bool):
            el.text = "true" if field_value else "false"
        else:
            el.text = str(field_value)
    return value


def _error_result_collector(error_xml_path: str) -> Any:
    collector = etree.Element(
        "ResultCollector",
        guiclass="ViewResultsFullVisualizer",
        testclass="ResultCollector",
        testname="平台错误结果树",
        enabled="true",
    )
    etree.SubElement(collector, "boolProp", name="ResultCollector.error_logging").text = "true"
    obj_prop = etree.SubElement(collector, "objProp")
    etree.SubElement(obj_prop, "name").text = "saveConfig"
    obj_prop.append(_sample_save_configuration())
    etree.SubElement(collector, "stringProp", name="filename").text = error_xml_path
    return collector


def _find_testplan_hash(root_hash: Any) -> Any | None:
    for node, child_hash, _thread_group_name, _parent_hash in walk_jmeter_pairs(root_hash):
        if node.tag == "TestPlan" and child_hash is not None:
            return child_hash
    return None


def _remove_legacy_error_sample_listeners(root_hash: Any) -> None:
    for listener in list(root_hash.findall(".//JSR223Listener[@testname='平台错误请求采样']")):
        parent = listener.getparent()
        if parent is None:
            continue
        children = list(parent)
        try:
            index = children.index(listener)
        except ValueError:
            continue
        parent.remove(listener)
        if index < len(parent) and parent[index].tag == "hashTree":
            parent.remove(parent[index])


def _remove_error_result_collectors(root_hash: Any) -> None:
    for collector in list(root_hash.findall(".//ResultCollector[@testname='平台错误结果树']")):
        parent = collector.getparent()
        if parent is None:
            continue
        children = list(parent)
        try:
            index = children.index(collector)
        except ValueError:
            continue
        parent.remove(collector)
        if index < len(parent) and parent[index].tag == "hashTree":
            parent.remove(parent[index])


def _ensure_sampler_hash(parent_hash: Any, sampler: Any, child_hash: Any | None) -> Any:
    if child_hash is not None:
        return child_hash
    children = list(parent_hash)
    try:
        index = children.index(sampler)
    except ValueError:
        child_hash = etree.Element("hashTree")
        parent_hash.append(child_hash)
        return child_hash
    child_hash = etree.Element("hashTree")
    parent_hash.insert(index + 1, child_hash)
    return child_hash


def _has_error_sample_assertion(sampler_hash: Any) -> bool:
    return sampler_hash.find("./JSR223Assertion[@testname='平台错误请求采样']") is not None


def _inject_error_sample_assertions(root_hash: Any) -> int:
    injected = 0
    for node, child_hash, _thread_group_name, parent_hash in list(walk_jmeter_pairs(root_hash)):
        if node.tag != "HTTPSamplerProxy":
            continue
        sampler_hash = _ensure_sampler_hash(parent_hash, node, child_hash)
        if _has_error_sample_assertion(sampler_hash):
            continue
        sampler_hash.append(_jsr223_error_sample_assertion())
        sampler_hash.append(etree.Element("hashTree"))
        injected += 1
    return injected


def apply_error_sample_listener(jmx_path: str, dest_path: str) -> None:
    """Inject sampler-scoped JSR223 Assertions that store capped failed samples."""
    tree = parse_jmx(jmx_path)
    root_hash = tree.getroot().find("hashTree")
    if root_hash is None:
        write_jmx(tree, dest_path)
        return

    _remove_legacy_error_sample_listeners(root_hash)
    _inject_error_sample_assertions(root_hash)
    write_jmx(tree, dest_path)


def apply_error_result_collector(jmx_path: str, dest_path: str, error_xml_path: str) -> None:
    """Inject one error-only ResultCollector for detailed failed samples."""
    tree = parse_jmx(jmx_path)
    root_hash = tree.getroot().find("hashTree")
    if root_hash is None:
        write_jmx(tree, dest_path)
        return

    _remove_error_result_collectors(root_hash)
    scope_hash = _find_testplan_hash(root_hash)
    if scope_hash is None:
        scope_hash = root_hash
    scope_hash.append(_error_result_collector(error_xml_path))
    scope_hash.append(etree.Element("hashTree"))
    write_jmx(tree, dest_path)

"""Failure sample listener injection for JMeter run JMX files."""

from __future__ import annotations

from typing import Any

from lxml import etree

from app.core.jmeter_thread_groups import pairs_in_hash_tree
from app.core.jmeter_xml_support import parse_jmx, write_jmx


ERROR_SAMPLE_FILENAME = "error_samples.jsonl"
ERROR_SAMPLE_FILE_PROP = "platform.errorSampleFile"
ERROR_SAMPLE_PER_CODE_LIMIT_PROP = "platform.errorSamplePerCodeLimit"
ERROR_SAMPLE_TOTAL_LIMIT_PROP = "platform.errorSampleTotalLimit"
ERROR_SAMPLE_TEXT_MAX_BYTES_PROP = "platform.errorSampleTextMaxBytes"
DEFAULT_ERROR_SAMPLE_PER_CODE_LIMIT = 10
DEFAULT_ERROR_SAMPLE_TOTAL_LIMIT = 100
DEFAULT_ERROR_SAMPLE_TEXT_MAX_BYTES = 8192


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


def _jsr223_error_sample_listener() -> Any:
    listener = etree.Element(
        "JSR223Listener",
        guiclass="TestBeanGUI",
        testclass="JSR223Listener",
        testname="平台错误请求采样",
        enabled="true",
    )
    etree.SubElement(listener, "stringProp", name="cacheKey").text = "true"
    etree.SubElement(listener, "stringProp", name="filename").text = ""
    etree.SubElement(listener, "stringProp", name="parameters").text = ""
    etree.SubElement(listener, "stringProp", name="script").text = _ERROR_SAMPLE_LISTENER_SCRIPT
    etree.SubElement(listener, "stringProp", name="scriptLanguage").text = "groovy"
    return listener


def _find_testplan_hash(root_hash: Any) -> Any | None:
    for node, child_hash in pairs_in_hash_tree(root_hash):
        if node.tag == "TestPlan" and child_hash is not None:
            return child_hash
    return None


def apply_error_sample_listener(jmx_path: str, dest_path: str) -> None:
    """Inject one TestPlan-scoped JSR223 Listener that stores capped failed samples."""
    tree = parse_jmx(jmx_path)
    root_hash = tree.getroot().find("hashTree")
    if root_hash is None:
        write_jmx(tree, dest_path)
        return

    if root_hash.find(".//JSR223Listener[@testname='平台错误请求采样']") is not None:
        write_jmx(tree, dest_path)
        return

    scope_hash = _find_testplan_hash(root_hash)
    if scope_hash is None:
        scope_hash = root_hash
    scope_hash.append(_jsr223_error_sample_listener())
    scope_hash.append(etree.Element("hashTree"))
    write_jmx(tree, dest_path)

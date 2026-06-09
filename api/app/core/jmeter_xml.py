"""JMX XML 操作工具。用 lxml 复刻 Java 端 JMeterUtil 的四个核心函数。

JMX 是 JMeter 压测述表语，本质上是 XML。结构如下：
- 根节点 <jmeterTestPlan>
- 下一层 <hashTree> 包裹
- 各类 TestPlan / ThreadGroup / CSVDataSet 等节点递归嵌套在 <hashTree> 里

为了容易处理任意嵌套深度，这里一律用 `tree.iter()` 全树扫描。
"""

from __future__ import annotations

from lxml import etree

from app.core.jmeter_csv import (
    csv_ignore_first_line,
    exist_csv_filename,
    update_csv_filename,
    update_csv_filenames,
)
from app.core.jmeter_pacing import apply_thread_group_pacing
from app.core.jmeter_thread_groups import (
    CONCURRENCY_TG as _CONCURRENCY_TG,
    STEPPING_TG as _STEPPING_TG,
    list_thread_groups,
    list_transactions,
    sum_enabled_thread_group_threads,
    update_debug_thread,
    update_run_thread,
)
from app.core.jmeter_upload_files import exist_upload_file_path, update_upload_file_paths
from app.core.jmeter_xml_support import parse_jmx as _parse, write_jmx as _write


def update_jar_classpath(jmx_path: str, jar_dir: str) -> None:
    """找 <TestPlan>/<stringProp name=TestPlan.user_define_classpath>，改写为 jar_dir。

    对齐 Java JMeterUtil.updateJmxJarFilePath。一个 JMX 只有一个 TestPlan，所以找到第一个就 break。
    """
    tree = _parse(jmx_path)
    for test_plan in tree.iter("TestPlan"):
        for prop in test_plan.iter():
            if prop.get("name") == "TestPlan.user_define_classpath":
                prop.text = jar_dir
        break
    _write(tree, jmx_path)


# ---------------------------------------------------------------------------
# Phase 4 — JMeterXMLBuilder（对齐 Java JMeterXMLService，dom4j → lxml）
# ---------------------------------------------------------------------------

from pathlib import Path

from app.schemas.jmx_assertion import AssertionVO
from app.schemas.jmx_csv import CsvDataVO
from app.schemas.jmx_http import HttpVO
from app.schemas.jmx_java import JavaVO
from app.schemas.jmx_thread import (
    ConcurrencyThreadGroupVO,
    SteppingThreadGroupVO,
    ThreadGroupVO,
)

# lxml 默认把空元素写成自闭和 <tag/>；JMeter 有时要求 <tag></tag>。
# Java 端 OutputFormat.setExpandEmptyElements(true) 解决了这个问题。
# Python 端：写盘前 walk tree，把所有 "无子节点且 text is None" 的 element 的 text 设成 ""


def _ensure_expanded_empty_elements(root) -> None:
    """确保所有叶子节点的 text 不为 None，防止 lxml 写出自闭和标签。"""
    for el in root.iter():
        if len(el) == 0 and el.text is None:
            el.text = ""


class JMeterXMLBuilder:
    """基于 base JMX 模板，逐步修改 DOM 并生成最终 JMX 文件。

    对齐 Java JMeterXMLService 的核心 API：init → 多次 update_xxx/add_xxx → write_jmx_file。
    """

    def __init__(self):
        self._tree: etree._ElementTree | None = None

    # --- 生命周期 ---

    def init(self, base_jmx_path: str) -> None:
        """加载 base 模板到内存 DOM。"""
        self._tree = _parse(base_jmx_path)

    def write_jmx_file(self, dest_path: str) -> None:
        """将内存 DOM 写出到 dest_path。"""
        if self._tree is None:
            raise RuntimeError("Builder 未 init，请先调用 init()")
        _ensure_expanded_empty_elements(self._tree.getroot())
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        self._tree.write(
            dest_path,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=False,
            pretty_print=True,
        )

    # --- 私有 helper ---

    def _find_first(self, tag: str) -> etree._Element:
        """DFS 找第一个 tag 匹配的 element。对齐 Java findElement。"""
        if self._tree is None:
            raise RuntimeError("Builder 未 init")
        return next(self._tree.iter(tag))

    @staticmethod
    def _set_child_text_by_name(parent: etree._Element, name: str, text: str | None) -> None:
        """遍历 parent 的所有后代，把第一个 name attribute 匹配的子节点的 text 设为 text。"""
        for child in parent.iter():
            if child.get("name") == name:
                child.text = text if text is not None else ""
                return

    @staticmethod
    def _mk_child(parent: etree._Element, tag: str, name: str | None = None, text: str | None = None, **attrs) -> etree._Element:
        """在 parent 下新建一个子 element，设 name attribute 和 text。"""
        attrib = dict(attrs)
        if name is not None:
            attrib["name"] = name
        el = etree.SubElement(parent, tag, attrib=attrib)
        if text is not None:
            el.text = text
        return el

    # --- 线程组 ---

    def update_thread_group(self, vo: ThreadGroupVO) -> None:
        """对齐 Java updateThreadGroup。"""
        tg = self._find_first("ThreadGroup")
        # num_threads / ramp_time / scheduler / duration / delay / same_user_on_next_iteration
        self._set_child_text_by_name(tg, "ThreadGroup.num_threads", vo.num_threads)
        self._set_child_text_by_name(tg, "ThreadGroup.ramp_time", vo.ramp_time)
        # scheduler: 0 → false；模板默认 true，Java 只在 scheduler==0 时改 false
        if vo.scheduler is not None and vo.scheduler == 0:
            self._set_child_text_by_name(tg, "ThreadGroup.scheduler", "false")
        # duration / delay: 只在 scheduler==1 且非空时写
        if vo.scheduler == 1:
            if vo.duration:
                self._set_child_text_by_name(tg, "ThreadGroup.duration", vo.duration)
            if vo.delay:
                self._set_child_text_by_name(tg, "ThreadGroup.delay", vo.delay)
        # same_user_on_next_iteration: 0 → false
        if vo.same_user_on_next_iteration is not None and vo.same_user_on_next_iteration == 0:
            self._set_child_text_by_name(tg, "ThreadGroup.same_user_on_next_iteration", "false")
        # loops: 在 LoopController.main_controller 下的 LoopController.loops
        for child in tg.iter():
            if child.get("name") == "ThreadGroup.main_controller":
                for loop in child.iter():
                    if loop.get("name") == "LoopController.loops":
                        loop.text = vo.loops if vo.loops is not None else ""
                        break
                break
        # delayed_start: 1 时新增 boolProp
        if vo.delayed_start == 1:
            self._mk_child(tg, "boolProp", name="ThreadGroup.delayedStart", text="true")

    def update_stepping_thread_group(self, vo: SteppingThreadGroupVO) -> None:
        """对齐 Java updateSteppingThreadGroup。"""
        tg = self._find_first(_STEPPING_TG)
        self._set_child_text_by_name(tg, "ThreadGroup.num_threads", vo.num_threads)
        self._set_child_text_by_name(tg, "Threads initial delay", vo.first_wait_for_seconds)
        self._set_child_text_by_name(tg, "Start users count", vo.next_add_threads)
        self._set_child_text_by_name(tg, "Start users count burst", vo.then_start_threads)
        self._set_child_text_by_name(tg, "Start users period", vo.next_add_threads_every_seconds)
        self._set_child_text_by_name(tg, "Stop users count", vo.finally_stop_threads)
        self._set_child_text_by_name(tg, "Stop users period", vo.finally_stop_threads_every_seconds)
        self._set_child_text_by_name(tg, "flighttime", vo.then_hold_load_for_seconds)
        self._set_child_text_by_name(tg, "rampUp", vo.using_ramp_up_seconds)

    def update_concurrency_thread_group(self, vo: ConcurrencyThreadGroupVO) -> None:
        """对齐 Java updateConcurrencyThreadGroup。"""
        tg = self._find_first(_CONCURRENCY_TG)
        self._set_child_text_by_name(tg, "TargetLevel", vo.target_concurrency)
        self._set_child_text_by_name(tg, "RampUp", vo.ramp_up_time)
        self._set_child_text_by_name(tg, "Steps", vo.ramp_up_steps_count)
        self._set_child_text_by_name(tg, "Hold", vo.hold_target_rate_time)

    # --- HTTP ---

    def update_http_sample(self, vo: HttpVO) -> None:
        """对齐 Java updateHttpSample。"""
        sampler = self._find_first("HTTPSamplerProxy")
        self._set_child_text_by_name(sampler, "HTTPSampler.domain", vo.domain)
        self._set_child_text_by_name(sampler, "HTTPSampler.port", vo.port)
        self._set_child_text_by_name(sampler, "HTTPSampler.protocol", vo.protocol)
        self._set_child_text_by_name(sampler, "HTTPSampler.contentEncoding", vo.content_encoding)
        self._set_child_text_by_name(sampler, "HTTPSampler.path", vo.path)
        self._set_child_text_by_name(sampler, "HTTPSampler.method", vo.method)

    def add_http_header(self, name: str, value: str) -> None:
        """对齐 Java addHttpHeader。
        在 jmeterTestPlan/hashTree/hashTree/HeaderManager/collectionProp 下加 elementProp。
        """
        root = self._tree.getroot()
        collection_prop = root.find("hashTree").find("hashTree").find("HeaderManager").find("collectionProp")
        ep = self._mk_child(collection_prop, "elementProp", name="", elementType="Header")
        self._mk_child(ep, "stringProp", name="Header.name", text=name)
        self._mk_child(ep, "stringProp", name="Header.value", text=value)

    def add_http_param(self, name: str, value: str) -> None:
        """对齐 Java addHttpParam。在 HTTPSamplerProxy/elementProp/collectionProp 下加 HTTPArgument。"""
        sampler = self._find_first("HTTPSamplerProxy")
        collection_prop = sampler.find("elementProp").find("collectionProp")
        ep = self._mk_child(collection_prop, "elementProp", name=name, elementType="HTTPArgument")
        self._mk_child(ep, "boolProp", name="HTTPArgument.always_encode", text="true")
        self._mk_child(ep, "stringProp", name="Argument.value", text=value)
        self._mk_child(ep, "stringProp", name="Argument.metadata", text="=")
        self._mk_child(ep, "boolProp", name="HTTPArgument.use_equals", text="true")
        self._mk_child(ep, "stringProp", name="Argument.name", text=name)

    def add_http_body(self, body: str) -> None:
        """对齐 Java addHttpBody。在 HTTPSamplerProxy 下加 boolProp(postBodyRaw) + elementProp(Arguments)。"""
        sampler = self._find_first("HTTPSamplerProxy")
        self._mk_child(sampler, "boolProp", name="HTTPSampler.postBodyRaw", text="true")
        args_ep = self._mk_child(sampler, "elementProp", name="HTTPsampler.Arguments", elementType="Arguments")
        coll = self._mk_child(args_ep, "collectionProp", name="Arguments.arguments")
        arg_ep = self._mk_child(coll, "elementProp", name="", elementType="HTTPArgument")
        self._mk_child(arg_ep, "boolProp", name="HTTPArgument.always_encode", text="false")
        self._mk_child(arg_ep, "stringProp", name="Argument.value", text=body)

    # --- Java ---

    def update_java_request(self, classpath: str) -> None:
        """对齐 Java updateJavaRequest。设 JavaSampler 的 classname。"""
        sampler = self._find_first("JavaSampler")
        self._set_child_text_by_name(sampler, "classname", classpath)

    def add_java_param(self, name: str, value: str) -> None:
        """对齐 Java addJavaParam。在 JavaSampler/elementProp/collectionProp 下加 Argument。"""
        sampler = self._find_first("JavaSampler")
        collection_prop = sampler.find("elementProp").find("collectionProp")
        ep = self._mk_child(collection_prop, "elementProp", name=name, elementType="Argument")
        self._mk_child(ep, "stringProp", name="Argument.name", text=name)
        self._mk_child(ep, "stringProp", name="Argument.value", text=value)
        self._mk_child(ep, "stringProp", name="Argument.metadata", text="=")

    # --- Assertion ---

    def add_assertion(
        self,
        sample_type: int,
        response_code: str | None,
        response_message: str | None,
        json_path: str | None,
        expected_value: str | None,
    ) -> None:
        """对齐 Java addAssertion。在采样器同级 hashTree 下加 ResponseAssertion / JSONPathAssertion。
        sample_type: 0=HTTP, 1=Java, 2=Dubbo（Phase 4 不做 Dubbo）。
        """
        sampler_tag = "HTTPSamplerProxy" if sample_type == 0 else "JavaSampler"
        sampler = self._find_first(sampler_tag)
        parent = sampler.getparent()
        hash_tree = parent.find("hashTree")
        if hash_tree is None:
            hash_tree = self._mk_child(parent, "hashTree")

        # Response Code Assertion
        if response_code:
            ra = self._mk_child(
                hash_tree, "ResponseAssertion",
                guiclass="AssertionGui", testclass="ResponseAssertion",
                testname="ResponseCodeAssertion", enabled="true",
            )
            cp = self._mk_child(ra, "collectionProp", name="Asserion.test_strings")
            self._mk_child(cp, "stringProp", name="49586", text=response_code)
            self._mk_child(ra, "stringProp", name="Assertion.custom_message")
            self._mk_child(ra, "stringProp", name="Assertion.test_field", text="Assertion.response_code")
            self._mk_child(ra, "boolProp", name="Assertion.assume_success", text="false")
            self._mk_child(ra, "intProp", name="Assertion.test_type", text="8")
            self._mk_child(hash_tree, "hashTree")

        # Response Message Assertion
        if response_message:
            ra = self._mk_child(
                hash_tree, "ResponseAssertion",
                guiclass="AssertionGui", testclass="ResponseAssertion",
                testname="ResponseMessageAssertion", enabled="true",
            )
            cp = self._mk_child(ra, "collectionProp", name="Asserion.test_strings")
            self._mk_child(cp, "stringProp", name="789079806", text=response_message)
            self._mk_child(ra, "stringProp", name="Assertion.custom_message")
            self._mk_child(ra, "stringProp", name="Assertion.test_field", text="Assertion.response_data")
            self._mk_child(ra, "boolProp", name="Assertion.assume_success", text="false")
            self._mk_child(ra, "intProp", name="Assertion.test_type", text="2")
            self._mk_child(hash_tree, "hashTree")

        # JSON Path Assertion
        if expected_value and json_path:
            ja = self._mk_child(
                hash_tree, "JSONPathAssertion",
                guiclass="JSONPathAssertionGui", testclass="JSONPathAssertion",
                testname="JSONAssertion", enabled="true",
            )
            self._mk_child(ja, "stringProp", name="JSON_PATH", text=json_path)
            self._mk_child(ja, "stringProp", name="EXPECTED_VALUE", text=expected_value)
            self._mk_child(ja, "boolProp", name="JSONVALIDATION", text="true")
            self._mk_child(ja, "boolProp", name="EXPECT_NULL", text="false")
            self._mk_child(ja, "boolProp", name="INVERT", text="false")
            self._mk_child(ja, "boolProp", name="ISREGEX", text="false")
            self._mk_child(hash_tree, "hashTree")

    # --- CSV ---

    def add_csv(self, csv_data_vo: CsvDataVO, sample_type: int) -> None:
        """对齐 Java addCsv。在采样器同级 hashTree 下加 CSVDataSet + hashTree。"""
        sampler_tag = "HTTPSamplerProxy" if sample_type == 0 else "JavaSampler"
        sampler = self._find_first(sampler_tag)
        parent = sampler.getparent()
        hash_tree = parent.find("hashTree")
        if hash_tree is None:
            hash_tree = self._mk_child(parent, "hashTree")

        for csv_file in csv_data_vo.csv_file_vo_list:
            ds = self._mk_child(
                hash_tree, "CSVDataSet",
                guiclass="TestBeanGUI", testclass="CSVDataSet",
                testname=csv_file.filename or "", enabled="true",
            )
            self._mk_child(ds, "stringProp", name="filename", text=csv_file.filename or "")
            self._mk_child(ds, "stringProp", name="fileEncoding", text=csv_data_vo.file_encoding or "UTF-8")
            self._mk_child(ds, "stringProp", name="variableNames", text=csv_file.variable_names or "")
            self._mk_child(
                ds, "boolProp", name="ignoreFirstLine",
                text="true" if csv_data_vo.ignore_first_line == 1 else "false",
            )
            self._mk_child(ds, "stringProp", name="delimiter", text=csv_data_vo.delimiter or ",")
            self._mk_child(
                ds, "boolProp", name="quotedData",
                text="true" if csv_data_vo.allow_quoted_data == 1 else "false",
            )
            self._mk_child(
                ds, "boolProp", name="recycle",
                text="true" if csv_data_vo.recycle_on_eof == 1 else "false",
            )
            self._mk_child(
                ds, "boolProp", name="stopThread",
                text="true" if csv_data_vo.stop_thread_on_eof == 1 else "false",
            )
            # sharingMode mapping
            sharing = csv_data_vo.sharing_mode or "Current thread group"
            if sharing == "All threads":
                share_val = "shareMode.all"
            elif sharing == "Current thread":
                share_val = "shareMode.thread"
            else:
                share_val = "shareMode.group"
            self._mk_child(ds, "stringProp", name="shareMode", text=share_val)
            self._mk_child(hash_tree, "hashTree")

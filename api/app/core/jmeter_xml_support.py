"""Shared helpers for JMeter XML modules."""

from __future__ import annotations

import ntpath
import os
from typing import Any

from lxml import etree


def is_enabled(el: Any) -> bool:
    """JMeter nodes are enabled unless enabled="false"."""
    return el.get("enabled") != "false"


def set_named_props(parent: Any, name_to_value: dict[str, str]) -> None:
    for child in parent.iter():
        name = child.get("name")
        if name and name in name_to_value:
            child.text = name_to_value[name]


def first_named_prop_text(parent: Any, prop_name: str) -> str | None:
    for child in parent.iter():
        if child.get("name") == prop_name:
            return child.text
    return None


def to_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def parse_jmx(jmx_path: str) -> etree._ElementTree:
    parser = etree.XMLParser(remove_blank_text=False)
    return etree.parse(jmx_path, parser)


def write_jmx(tree: etree._ElementTree, jmx_path: str) -> None:
    tree.write(jmx_path, xml_declaration=True, encoding="UTF-8", standalone=False)


def path_basename(path: str) -> str:
    return os.path.basename(ntpath.basename(path))

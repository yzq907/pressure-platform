"""HTTP file upload path helpers for JMeter JMX files."""

from __future__ import annotations

from lxml import etree

from app.core.jmeter_xml_support import parse_jmx, path_basename, write_jmx


def _http_file_arg_path_props(tree: etree._ElementTree):
    for file_arg in tree.iter("elementProp"):
        if file_arg.get("elementType") != "HTTPFileArg":
            continue
        for prop in file_arg.iter():
            if prop.get("name") == "File.path":
                yield prop


def exist_upload_file_path(jmx_path: str, filename: str) -> bool:
    """Return whether an HTTPFileArg File.path basename matches filename."""
    tree = parse_jmx(jmx_path)
    return any(path_basename(prop.text or "") == filename for prop in _http_file_arg_path_props(tree))


def update_upload_file_paths(jmx_path: str, file_path_by_filename: dict[str, str]) -> None:
    """Batch rewrite HTTPFileArg File.path values.

    Only HTTPFileArg/File.path is touched, so normal params, assertions, and
    script text containing the same filename are left unchanged.
    """
    if not file_path_by_filename:
        return
    tree = parse_jmx(jmx_path)
    for prop in _http_file_arg_path_props(tree):
        basename = path_basename(prop.text or "")
        if basename in file_path_by_filename:
            prop.text = file_path_by_filename[basename]
    write_jmx(tree, jmx_path)

"""CSVDataSet helpers for JMeter JMX files."""

from __future__ import annotations

from app.core.jmeter_xml_support import is_enabled, parse_jmx, path_basename, write_jmx


def _csv_matches(csv_node, csv_filename: str) -> bool:
    """Match by CSVDataSet testname or filename basename."""
    if csv_node.get("testname") == csv_filename:
        return True
    for prop in csv_node.iter():
        if prop.get("name") == "filename" and prop.text:
            if path_basename(prop.text) == csv_filename:
                return True
    return False


def exist_csv_filename(jmx_path: str, csv_filename: str) -> bool:
    """Return whether an enabled CSVDataSet references csv_filename."""
    tree = parse_jmx(jmx_path)
    for el in tree.iter("CSVDataSet"):
        if is_enabled(el) and _csv_matches(el, csv_filename):
            return True
    return False


def csv_ignore_first_line(jmx_path: str, csv_filename: str) -> bool:
    """Read ignoreFirstLine for a matching CSVDataSet. JMeter default is false."""
    tree = parse_jmx(jmx_path)
    for csv_node in tree.iter("CSVDataSet"):
        if not is_enabled(csv_node) or not _csv_matches(csv_node, csv_filename):
            continue
        for prop in csv_node.iter():
            if prop.get("name") == "ignoreFirstLine":
                return (prop.text or "").strip().lower() == "true"
    return False


def update_csv_filename(jmx_path: str, csv_filename: str, csv_filepath: str) -> None:
    """Rewrite filename for all enabled CSVDataSet nodes matching csv_filename."""
    tree = parse_jmx(jmx_path)
    for csv_node in tree.iter("CSVDataSet"):
        if not is_enabled(csv_node):
            continue
        if not _csv_matches(csv_node, csv_filename):
            continue
        for prop in csv_node.iter():
            if prop.get("name") == "filename":
                prop.text = csv_filepath
    write_jmx(tree, jmx_path)


def update_csv_filenames(jmx_path: str, csv_path_by_filename: dict[str, str]) -> None:
    """Batch rewrite CSVDataSet filenames."""
    if not csv_path_by_filename:
        return
    tree = parse_jmx(jmx_path)
    for csv_node in tree.iter("CSVDataSet"):
        if not is_enabled(csv_node):
            continue
        for csv_filename, csv_filepath in csv_path_by_filename.items():
            if not _csv_matches(csv_node, csv_filename):
                continue
            for prop in csv_node.iter():
                if prop.get("name") == "filename":
                    prop.text = csv_filepath
            break
    write_jmx(tree, jmx_path)

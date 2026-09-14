import sys
from pathlib import Path

from app.core.config import default_log_dir


def test_frozen_log_path_ignores_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'service' / 'mysterious-api'))
    monkeypatch.chdir(tmp_path)
    assert default_log_dir() == str(tmp_path / 'service' / 'logs')


def test_source_log_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.chdir(tmp_path)
    assert Path(default_log_dir()) == Path(__file__).resolve().parents[1] / 'logs'

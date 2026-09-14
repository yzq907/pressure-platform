from datetime import date
import logging

from app.core.log_rotation import DailySizeHandler


def record(text):
    return logging.LogRecord('test', logging.INFO, '', 0, text, (), None)


def test_size_rotation_never_overwrites(tmp_path):
    path = tmp_path / 'mysterious.log'
    handler = DailySizeHandler(path, maxBytes=10)
    day = handler.today()
    try:
        for message in ['abcdef', 'ghijkl', 'mnopqr']:
            handler.emit(record(message))
        assert (tmp_path / f'mysterious.log.{day}').read_text() == 'abcdef\n'
        assert (tmp_path / f'mysterious.log.{day}.1').read_text() == 'ghijkl\n'
        assert path.read_text() == 'mnopqr\n'
    finally:
        handler.close()


def test_daily_rotation_and_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(DailySizeHandler, 'today', lambda self: date(2026, 9, 14))
    (tmp_path / 'mysterious.log.2026-09-06.1').write_text('expired')
    (tmp_path / 'mysterious.log.2026-09-07').write_text('keep')
    (tmp_path / 'other.log.2026-09-01').write_text('unrelated')
    handler = DailySizeHandler(tmp_path / 'mysterious.log')
    try:
        assert not (tmp_path / 'mysterious.log.2026-09-06.1').exists()
        assert (tmp_path / 'mysterious.log.2026-09-07').exists()
        assert (tmp_path / 'other.log.2026-09-01').exists()
        handler.emit(record('yesterday'))
        monkeypatch.setattr(DailySizeHandler, 'today', lambda self: date(2026, 9, 15))
        handler.emit(record('today'))
        assert (tmp_path / 'mysterious.log.2026-09-14').read_text() == 'yesterday\n'
        assert not (tmp_path / 'mysterious.log.2026-09-07').exists()
    finally:
        handler.close()

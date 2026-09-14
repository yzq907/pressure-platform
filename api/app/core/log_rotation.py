"""Single-process daily/size rotation with calendar-day retention."""
from datetime import datetime, timedelta
from logging.handlers import BaseRotatingHandler
from pathlib import Path
import re
from zoneinfo import ZoneInfo


class DailySizeHandler(BaseRotatingHandler):
    def __init__(self, filename, maxBytes=52428800, backupDays=7, encoding="utf-8"):
        if maxBytes <= 0 or backupDays < 1:
            raise ValueError("日志大小必须大于 0，保留天数必须至少为 1")
        self.max_bytes = maxBytes
        self.backup_days = backupDays
        self.timezone = ZoneInfo("Asia/Shanghai")
        path = Path(filename)
        self.active_day = (
            datetime.fromtimestamp(path.stat().st_mtime, self.timezone).date()
            if path.exists() and path.stat().st_size else self.today()
        )
        super().__init__(filename, "a", encoding=encoding, delay=False)
        self.cleanup_day = None
        self.cleanup()

    def today(self):
        return datetime.now(self.timezone).date()

    def cleanup(self):
        today = self.today()
        cutoff = today - timedelta(days=self.backup_days)
        base = Path(self.baseFilename)
        pattern = re.compile(re.escape(base.name) + r"\.(\d{4}-\d{2}-\d{2})(?:\.(\d+))?$")
        for path in base.parent.iterdir():
            match = pattern.fullmatch(path.name)
            if not match or not path.is_file() or path.is_symlink():
                continue
            try:
                day = datetime.strptime(match[1], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
        self.cleanup_day = today

    def shouldRollover(self, record):
        if self.cleanup_day != self.today():
            self.cleanup()
        size = Path(self.baseFilename).stat().st_size
        message_size = len((self.format(record) + self.terminator).encode(self.encoding))
        return size > 0 and (self.active_day != self.today() or size + message_size > self.max_bytes)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        archive = f"{self.baseFilename}.{self.active_day.isoformat()}"
        target = archive
        number = 1
        while Path(target).exists():
            target = f"{archive}.{number}"
            number += 1
        self.rotate(self.baseFilename, target)
        self.active_day = self.today()
        self.stream = self._open()
        self.cleanup()

    def emit(self, record):
        super().emit(record)
        self.active_day = self.today()

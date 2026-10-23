"""Short-lived capabilities restricted to one HTML report and its assets.

The signing key is process-local: links are invalidated when the service restarts.
No user login token or deployment secret is placed in the preview URL.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

PREVIEW_TTL_SECONDS = 30 * 60
_SIGNING_KEY = secrets.token_bytes(32)
_ASSET_SUFFIXES = {
    ".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot",
}


def report_preview_directory(data_dir: str, report_dir: str) -> Path:
    """Require an HTML report directory strictly within the configured data root."""
    data_root = Path(data_dir).resolve()
    directory = Path(report_dir).resolve()
    relative = directory.relative_to(data_root)
    if not relative.parts or directory.name != "data" or not (directory / "index.html").is_file():
        raise ValueError("Not an HTML report directory")
    return directory


def create_preview_url(data_dir: str, report_dir: str) -> str:
    directory = report_preview_directory(data_dir, report_dir)
    relative = directory.relative_to(Path(data_dir).resolve()).as_posix()
    payload = json.dumps(
        [relative, int(time.time()) + PREVIEW_TTL_SECONDS], separators=(",", ":")
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_SIGNING_KEY, encoded.encode(), hashlib.sha256).hexdigest()
    return f"/reports/{encoded}.{signature}/index.html"


def _verify_capability(capability: str) -> str:
    if len(capability) > 4096:
        raise ValueError("Invalid preview link")
    encoded, signature = capability.rsplit(".", 1)
    expected = hmac.new(_SIGNING_KEY, encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid preview signature")
    payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    directory, expires_at = json.loads(payload)
    if not isinstance(directory, str) or not isinstance(expires_at, int) or expires_at <= time.time():
        raise ValueError("Expired preview link")
    return directory


class ReportPreviewFiles(StaticFiles):
    """Never expose raw data paths, even when the requester has a login token."""

    async def get_response(self, path, scope):
        capability, _, asset = path.partition("/")
        try:
            relative = _verify_capability(capability)
        except (ValueError, TypeError, UnicodeError):
            return PlainTextResponse("Invalid or expired report preview link", status_code=403)

        try:
            root = Path(self.directory).resolve()
            directory = report_preview_directory(str(root), str(root / relative))
            if directory.relative_to(root).as_posix() != relative:
                raise ValueError("Report directory changed")
            asset_path = Path(asset)
            if (not asset or asset_path.is_absolute()
                    or any(part.startswith(".") for part in asset_path.parts)
                    or asset_path.suffix.lower() not in _ASSET_SUFFIXES):
                raise ValueError("Invalid report asset")
            file_path = (directory / asset_path).resolve()
            resolved_asset = file_path.relative_to(directory)
            if (file_path.suffix.lower() not in _ASSET_SUFFIXES
                    or any(part.startswith(".") for part in resolved_asset.parts)):
                raise ValueError("Invalid resolved report asset")
            response = await super().get_response(file_path.relative_to(root).as_posix(), scope)
        except (ValueError, OSError, HTTPException):
            return PlainTextResponse("Report asset not found", status_code=404)

        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

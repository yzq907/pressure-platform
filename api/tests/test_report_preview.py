from datetime import datetime, timedelta
from urllib.parse import urljoin

import pytest

from app.core.config import get_settings
from app.models.report import Report
from app.models.role import Role
from app.models.user import User


@pytest.fixture
async def preview(db, tmp_path, monkeypatch):
    from app.main import app, create_app

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    directory = tmp_path / "case" / "report" / "run" / "data"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text('<script src="content/app.js"></script>')
    (directory / "content").mkdir()
    (directory / "content" / "app.js").write_text("preview-script")
    (directory / "statistics.json").write_text('{"samples": 1}')
    (directory.parent / "log").mkdir()
    (directory.parent / "log" / "jmeter.log").write_text("secret-log")
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "users.csv").write_text("secret-csv")
    (directory / "leak.html").symlink_to(directory.parent / "log" / "jmeter.log")
    (directory / "secret.jtl").write_text("secret-jtl")
    (directory / "local-leak.html").symlink_to(directory / "secret.jtl")
    other = tmp_path / "other" / "report" / "run" / "data"
    other.mkdir(parents=True)
    (other / "index.html").write_text("secret-other-report")
    report = Report(name="preview", exec_type=2, report_dir=str(directory))
    db.add(report)
    await db.commit()
    test_app = create_app()
    test_app.dependency_overrides.update(app.dependency_overrides)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        yield client, report.id, directory


async def signed_url(preview, auth_client):
    client, report_id, _ = preview
    response = await client.get(f"/report/view/{report_id}", headers=auth_client.headers)
    assert response.json()["code"] == 0
    return response.json()["data"]


@pytest.mark.asyncio
async def test_old_static_paths_deny_anonymous_access(preview, auth_client):
    client, _, _ = preview
    for path in (
        "/reports/common/users.csv",
        "/reports/case/report/run/log/jmeter.log",
        "/reports/case/report/run/data/index.html",
        "/mysterious/reports/common/users.csv",
    ):
        response = await client.get(path)
        assert response.status_code == 403
        assert "secret-" not in response.text
        assert (await client.get(path, headers=auth_client.headers)).status_code == 403


@pytest.mark.asyncio
async def test_link_and_relative_assets_work_without_login_header(preview, auth_client):
    client, _, _ = preview
    url = await signed_url(preview, auth_client)
    assert (await client.get(url)).status_code == 200
    asset = await client.get(urljoin(url, "content/app.js"))
    assert asset.text == "preview-script"
    assert (await client.get(urljoin(url, "statistics.json"))).json() == {"samples": 1}
    assert (await client.get("/mysterious" + url)).status_code == 200
    assert (await client.head(url)).status_code == 200
    assert asset.headers["cache-control"] == "no-store"
    assert asset.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_missing_login_and_report_permission_cannot_issue_link(preview, db):
    client, report_id, _ = preview
    assert (await client.get(f"/report/view/{report_id}")).json()["code"] != 0
    role = Role(name="no-report", code="no-report")
    db.add(role)
    await db.flush()
    db.add(User(username="no-report", password="unused", role_id=role.id,
                token="no-report-token", expire_time=datetime.now() + timedelta(days=1)))
    await db.commit()
    response = await client.get(f"/report/view/{report_id}", headers={"token": "no-report-token"})
    assert response.json()["code"] != 0


@pytest.mark.asyncio
async def test_signature_tampering_and_expiry_are_rejected(preview, auth_client, monkeypatch):
    from app.core import report_preview

    client, _, _ = preview
    url = await signed_url(preview, auth_client)
    parts = url.split("/")
    parts[2] = parts[2][:-1] + ("0" if parts[2][-1] != "0" else "1")
    assert (await client.get("/".join(parts))).status_code == 403
    now = report_preview.time.time()
    monkeypatch.setattr(report_preview.time, "time", lambda: now + 1801)
    assert (await client.get(url)).status_code == 403


@pytest.mark.asyncio
async def test_signed_link_cannot_escape_report_or_serve_raw_data(preview, auth_client):
    client, _, _ = preview
    url = await signed_url(preview, auth_client)
    base = url.rsplit("/", 1)[0]
    for path in ("%2e%2e/log/jmeter.log", "%2e%2e%2f%2e%2e%2fother/data/index.html",
                 "leak.html", "local-leak.html", "secret.jtl"):
        response = await client.get(base + "/" + path)
        assert response.status_code in (403, 404)
        assert "secret-" not in response.text


@pytest.mark.asyncio
async def test_restart_invalidates_old_links(preview, auth_client, monkeypatch):
    from app.core import report_preview

    client, _, _ = preview
    url = await signed_url(preview, auth_client)
    monkeypatch.setattr(report_preview, "_SIGNING_KEY", b"new-process-key")
    assert (await client.get(url)).status_code == 403


@pytest.mark.asyncio
async def test_outside_data_root_cannot_issue_link(preview, auth_client, db, tmp_path):
    client, report_id, _ = preview
    outside = tmp_path.parent / (tmp_path.name + "-outside") / "data"
    outside.mkdir(parents=True)
    (outside / "index.html").write_text("outside")
    report = await db.get(Report, report_id)
    report.report_dir = str(outside)
    await db.commit()
    assert (await client.get(f"/report/view/{report_id}", headers=auth_client.headers)).json()["code"] != 0

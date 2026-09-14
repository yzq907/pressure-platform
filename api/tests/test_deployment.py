from httpx import ASGITransport, AsyncClient
import pytest
from app.core.config import get_settings
from app.main import create_app


@pytest.mark.parametrize('value', ['', '   '])
def test_data_dir_required(monkeypatch, value):
    monkeypatch.setattr(get_settings(), 'data_dir', value)
    with pytest.raises(RuntimeError, match='必须配置数据目录 MYSTERIOUS_DATA_DIR'):
        create_app()


def test_data_dir_is_created(tmp_path, monkeypatch):
    directory = tmp_path / 'new' / 'reports'
    monkeypatch.setattr(get_settings(), 'data_dir', str(directory))
    create_app()
    assert directory.is_dir()


def test_data_dir_cannot_be_file(tmp_path, monkeypatch):
    file = tmp_path / 'file'
    file.write_text('not a directory')
    monkeypatch.setattr(get_settings(), 'data_dir', str(file))
    with pytest.raises(RuntimeError, match='无法创建或访问数据目录'):
        create_app()


@pytest.mark.asyncio
async def test_prefixed_health(client):
    response = await client.get('/mysterious/health')
    assert response.json()['data']['db'] == 'ok'


@pytest.mark.asyncio
async def test_static_ui_and_api(tmp_path, monkeypatch):
    (tmp_path / 'index.html').write_text('<html>deployment-ui</html>')
    (tmp_path / 'bundle.js').write_text('console.log("bundle")')
    monkeypatch.setattr(get_settings(), 'frontend_dist', str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert 'deployment-ui' in (await client.get('/')).text
        assert (await client.get('/')).headers['cache-control'] == 'no-cache'
        assert 'bundle' in (await client.get('/bundle.js')).text
        assert 'openapi' in (await client.get('/mysterious/v2/api-docs')).json()
        assert 'deployment-ui' not in (await client.get('/mysterious/nonexistent')).text


@pytest.mark.asyncio
async def test_data_dir_override(monkeypatch):
    from app.services.config import get_value, get_value_or_default
    monkeypatch.setattr(get_settings(), 'data_dir', '/tmp/deployment-data')
    assert await get_value(None, 'MASTER_DATA_HOME') == '/tmp/deployment-data'
    assert await get_value_or_default(None, 'MASTER_DATA_HOME') == '/tmp/deployment-data'

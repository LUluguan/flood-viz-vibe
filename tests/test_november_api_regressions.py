"""11 月审计回归：断言行为，不以源码字符串替代执行。"""
import asyncio
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

import app
import store


@pytest.mark.parametrize('step', [5, 10, 15])
def test_design_storm_reports_full_hour(step):
    data = TestClient(app.app).get('/api/design_storm', params={
        'return_period': 100, 'duration_min': 120, 'step_min': step}).json()
    # 100年/60min官方公式得135.4200mm，不是首12块(并非最大窗口)之和111.17。
    assert data['max_60min_mm'] == pytest.approx(135.42, abs=0.02)


def test_design_storm_rejects_fractional_blocks():
    response = TestClient(app.app).get('/api/design_storm', params={'duration_min': 121})
    assert response.status_code == 400


def test_memo_coalesces_first_miss_and_propagates_failures():
    memo = app._Memo(limit=2)
    barrier = threading.Barrier(8)
    calls = []
    def compute():
        calls.append(1)
        time.sleep(0.1)
        return 42
    def worker(_):
        barrier.wait()
        return memo.get_or_compute('same', compute)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(worker, range(8))) == [42] * 8
    assert len(calls) == 1
    barrier.reset()
    errors = []
    def fail():
        errors.append(1)
        time.sleep(0.1)
        raise ValueError('test failure')
    def waiter(_):
        barrier.wait()
        with pytest.raises(ValueError, match='test failure'):
            memo.get_or_compute('bad', fail)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(waiter, range(8)))
    assert len(errors) == 1
    assert memo.get_or_compute('bad', lambda: 17) == 17


def test_security_headers_keep_local_geolocation():
    response = TestClient(app.app).get('/login.html')
    assert response.headers.get('x-frame-options') == 'DENY'
    assert response.headers.get('x-content-type-options') == 'nosniff'
    assert response.headers.get('referrer-policy') == 'no-referrer'
    assert 'geolocation=(self)' in response.headers.get('permissions-policy', '')


def test_upload_content_length_rejected_before_read():
    response = TestClient(app.app).post('/api/predict', content=b'', headers={
        'content-length': str(100 * 1024 * 1024), 'content-type': 'multipart/form-data; boundary=x'})
    assert response.status_code == 413


def test_chunked_upload_rejected_before_endpoint_consumption():
    from service_support import RequestGuardMiddleware
    consumed = []
    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            consumed.append(len(message.get('body', b'')))
            if not message.get('more_body'):
                break
    chunks = iter([{'type': 'http.request', 'body': b'abcd', 'more_body': True},
                   {'type': 'http.request', 'body': b'efgh', 'more_body': False}])
    events = []
    async def receive():
        return next(chunks)
    async def send(message):
        events.append(message)
    scope = {'type': 'http', 'path': '/api/predict', 'method': 'POST', 'headers': []}
    asyncio.run(RequestGuardMiddleware(downstream, upload_limit=6)(scope, receive, send))
    assert consumed == [4]
    assert events[0]['status'] == 413


def test_tiff_series_shape_rejected_before_decode(monkeypatch):
    # 小真实TIFF + 缩小测试预算，验证元数据检查发生在 asarray 之前。
    data = io.BytesIO()
    tifffile.imwrite(data, np.zeros((8, 8, 5), dtype='uint16'), photometric='minisblack')
    monkeypatch.setattr(app, 'MAX_PREDICT_PIXELS', 16, raising=False)
    def no_decode(*args, **kwargs):
        raise AssertionError('oversized series must not be decoded')
    monkeypatch.setattr(tifffile.TiffPageSeries, 'asarray', no_decode)
    response = TestClient(app.app).post('/api/predict', files={'file': ('test.tif', data.getvalue(), 'image/tiff')})
    assert response.status_code == 413


def test_report_rejects_nonfinite_coordinates():
    client = TestClient(app.app)
    for lon in ['nan', 'inf', '1e308']:
        response = client.post('/api/report', json={'lon': lon, 'lat': 23.11})
        assert response.status_code == 400
    assert store._load_reports() == []


def test_anonymous_report_list_hides_pending_and_private_fields():
    client = TestClient(app.app)
    response = client.post('/api/report', json={'lon': 113.33219, 'lat': 23.11234, 'desc': 'private address'})
    rid = response.json()['id']
    assert client.get('/api/report').json()['reports'] == []
    token = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'}).json()['token']
    headers = {'Authorization': 'Bearer ' + token}
    full = client.get('/api/report', headers=headers).json()['reports']
    assert full[0]['desc'] == 'private address'
    client.post('/api/report/' + rid + '/status', headers=headers, json={'status': '已核实'})
    public = client.get('/api/report').json()['reports'][0]
    assert 'desc' not in public and 'location_text' not in public and 'image_url' not in public
    assert public['lon'] == 113.332 and public['lat'] == 23.112


def test_warning_get_never_sends_notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(app, 'WECOM_WEBHOOK_URL', 'https://example.invalid/webhook')
    monkeypatch.setattr(app, 'notify_async', lambda *args: sent.append(args))
    response = TestClient(app.app).get('/api/warning?return_period=100')
    assert response.status_code == 200
    assert sent == []


def test_unknown_store_schema_is_json_failure():
    store._atomic_write_json(store._REPORTS_FILE, ['bad row'])
    store._load_reports()
    response = TestClient(app.app, raise_server_exceptions=False).get('/api/report')
    assert response.status_code == 503
    assert response.json()['error']


def test_unhandled_defect_has_json_request_id(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError('internal secret must not be exposed')
    monkeypatch.setattr(app, '_load_depth_tif', broken)
    response = TestClient(app.app, raise_server_exceptions=False).get('/api/cascade')
    assert response.status_code == 500
    assert response.json()['request_id']
    assert 'internal secret' not in response.text


def test_ogc_links_do_not_trust_host():
    response = TestClient(app.app).get('/api/ogc', headers={'host': 'untrusted.invalid'})
    assert response.status_code == 200
    assert all('untrusted.invalid' not in link['href'] for link in response.json()['links'])


def test_static_dockerfile_not_public():
    assert TestClient(app.app).get('/Dockerfile').status_code == 404


def test_multipolygon_buildings_keep_all_exterior_area_and_skip_holes(monkeypatch, tmp_path):
    import json
    data = {"features": [{"type": "Feature", "properties": {"name": "测试建筑", "height": 20},
        "geometry": {"type": "MultiPolygon", "coordinates": [
            [[[113, 23], [113.001, 23], [113.001, 23.001], [113, 23.001], [113, 23]],
             [[113.0002, 23.0002], [113.0008, 23.0002], [113.0008, 23.0008], [113.0002, 23.0008]]],
            [[[114, 24], [114.001, 24], [114.001, 24.001], [114, 24.001], [114, 24]]]]}}]}
    path = tmp_path / 'gz_tower_buildings.geojson'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(app, 'ROOT', str(tmp_path))
    monkeypatch.setattr(app, '_bld_m', app._Memo())
    rows = app._get_buildings()
    assert rows, (app.ROOT, data)
    row = rows[0]
    expected = (app._ring_area_m2(data['features'][0]['geometry']['coordinates'][0][0], 23.0005)
                - app._ring_area_m2(data['features'][0]['geometry']['coordinates'][0][1], 23.0005)
                + app._ring_area_m2(data['features'][0]['geometry']['coordinates'][1][0], 24.0005))
    assert row['area_m2'] == pytest.approx(expected, rel=0.01)


def test_unknown_username_returns_uniform_error_not_500(monkeypatch):
    monkeypatch.setenv('FLOOD_DEMO', '0')
    monkeypatch.setenv('FLOOD_ADMIN_PASSWORD', 'fixture-secret-2026')
    response = TestClient(app.app).post('/api/auth/login', json={'username': 'missing', 'password': 'not-the-password'})
    assert response.status_code == 401
    assert response.json() == {'error': '用户名或密码错误'}


def test_secure_mode_blocks_legacy_demo_password(monkeypatch):
    store._load_users()  # 先在显式demo下初始化临时演示表
    monkeypatch.setenv('FLOOD_DEMO', '0')
    response = TestClient(app.app).post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'})
    assert response.status_code in (401, 503)

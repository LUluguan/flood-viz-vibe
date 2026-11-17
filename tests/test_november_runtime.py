"""跨部署时区、连接释放与确认后通知契约。外部网络全部替换。"""
import datetime
import hashlib
import re
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app
import store


def test_hourly_index_uses_shanghai_not_server_timezone():
    utc = datetime.datetime(2026, 11, 5, 2, 30, tzinfo=datetime.timezone.utc)
    om = {'times': ['2026-11-05T09:00', '2026-11-05T10:00', '2026-11-05T11:00']}
    assert app._om_now_index(om, now=utc) == 1


def test_db_connection_has_timeout_and_health_closes_it(monkeypatch):
    calls = []
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append('cursor.close')
        def execute(self, sql):
            calls.append(sql)
    class Conn:
        def cursor(self):
            return Cursor()
        def close(self):
            calls.append('close')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass  # psycopg2 的 conn 上下文并不关闭连接
    def connect(**kwargs):
        assert 0 < kwargs.get('connect_timeout', 0) <= 3
        return Conn()
    monkeypatch.setattr(app, 'psycopg2', SimpleNamespace(connect=connect))
    monkeypatch.setattr(app, '_db_health_cache', None, raising=False)
    assert app._db_reachable()
    assert calls.count('close') == 1
    assert calls.count('cursor.close') == 1
    assert app._db_reachable()
    assert calls.count('close') == 1  # 探活短缓存，不每请求建连


def test_ensemble_cache_is_bounded():
    memo = app._ens_m
    for n in range(100):
        memo.get_or_compute(('test', n), lambda: 1)
    assert len(memo._data) <= 32


def test_public_photo_requires_admin(tmp_path):
    path = tmp_path / 'not-used'
    del path
    response = TestClient(app.app).get('/reports/img_rabc.jpg')
    assert response.status_code == 401


def test_subscribe_confirmation_is_required_and_token_is_single_use(monkeypatch):
    messages = []
    def enqueue(*args):
        messages.append(args)
        return {'queued': True, 'reason': '', 'future': None}
    monkeypatch.setattr(app, 'notify_async', enqueue)
    monkeypatch.setattr(app, 'SMTP_CFG', {'host': 'smtp.example.invalid', 'user': 'test', 'password': '', 'port': 465})
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://flood.example')
    monkeypatch.setenv('FLOOD_DEMO', '0')
    client = TestClient(app.app)
    response = client.post('/api/subscribe', json={'email': 'person@example.com', 'zone': '区1'})
    assert response.status_code == 200 and 'total' not in response.json()
    rows = store._load_subscribers()
    assert rows[0]['confirmed'] is False
    assert len(messages) == 1
    token = re.search(r'#confirm=([A-Za-z0-9_-]+)', messages[0][4]).group(1)
    assert token not in str(rows)
    confirmed = client.post('/api/subscribe/confirm', json={'token': token})
    assert confirmed.status_code == 200
    assert store._load_subscribers()[0]['confirmed'] is True
    assert client.post('/api/subscribe/confirm', json={'token': token}).status_code == 400
    unsubscribe = re.search(r'#unsubscribe=([A-Za-z0-9_-]+)', messages[0][4]).group(1)
    assert client.post('/api/subscribe/unsubscribe', json={'token': unsubscribe}).status_code == 200
    assert store._load_subscribers() == []


def test_warning_publish_requires_admin_and_matching_confirmed_zone(monkeypatch):
    sent = []
    monkeypatch.setattr(app, 'WECOM_WEBHOOK_URL', 'https://example.invalid/hook')
    monkeypatch.setattr(app, 'notify_async', lambda *args: sent.append(args) or {'queued': True, 'reason': ''})
    client = TestClient(app.app)
    assert client.post('/api/warning/publish', json={'return_period': 100}).status_code == 401
    token = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'}).json()['token']
    store._save_subscribers([
        {'email': 'right@example.com', 'zone': '区1', 'confirmed': True},
        {'email': 'wrong@example.com', 'zone': '区2', 'confirmed': True},
        {'email': 'pending@example.com', 'zone': '区1', 'confirmed': False}])
    monkeypatch.setattr(app, 'warning', lambda **kwargs: {
        'city_level': '橙色', 'scenario': '100年一遇', 'flooded_area_km2': 1.5, 'advice': 'test',
        'zones': [{'zone': 1, 'level': '橙色'}, {'zone': 2, 'level': '无'}]})
    response = client.post('/api/warning/publish', headers={'Authorization': 'Bearer ' + token}, json={'return_period': 100})
    assert response.status_code == 200 and response.json()['queued']
    assert response.json()['sent'] is False
    assert sent[0][-1] == ['right@example.com']


def test_existing_confirmed_subscription_cannot_be_changed_without_confirmation(monkeypatch):
    store._save_subscribers([{'email': 'owner@example.com', 'zone': '区1', 'confirmed': True}])
    response = TestClient(app.app).post('/api/subscribe', json={'email': 'owner@example.com', 'zone': '区9'})
    assert response.status_code == 200
    saved = store._load_subscribers()[0]
    assert saved['zone'] == '区1' and saved['confirmed'] is True


def test_expired_confirmation_does_not_activate():
    token = 'test-expired-token'
    store._save_subscribers([{'email': 'old@example.com', 'zone': '区1', 'confirmed': False,
        'confirm_hash': hashlib.sha256(token.encode()).hexdigest(), 'confirm_expires': time.time() - 1}])
    response = TestClient(app.app).post('/api/subscribe/confirm', json={'token': token})
    assert response.status_code == 400
    assert store._load_subscribers()[0]['confirmed'] is False

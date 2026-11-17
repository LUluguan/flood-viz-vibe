# -*- coding: utf-8 -*-
"""外部集成离线回归: 网络/SMTP/远端 rasterio 必须由本文件独立封锁。"""
import copy
import datetime
import json
import logging
import os
import socket
import ssl
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import numpy as np
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import externals
import sar_change
import sat_data


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("unexpected external I/O: install a test double")

    monkeypatch.setattr(requests.sessions.Session, "request", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(externals.smtplib.SMTP, "connect", denied)
    monkeypatch.setattr(externals.smtplib.SMTP_SSL, "connect", denied)
    monkeypatch.setattr(sat_data.rasterio, "open", denied)
    monkeypatch.setattr(externals, "_om_cache", copy.deepcopy(externals._om_cache))


def response(payload=None, status=200, content_type="application/json"):
    r = requests.Response()
    r.status_code = status
    r.url = "https://example.invalid/mock"
    r.headers["content-type"] = content_type
    r._content = json.dumps(payload).encode("utf-8")
    return r


def hourly(value=0.25):
    t0 = datetime.datetime(2026, 11, 3)
    return {"hourly": {
        "time": [(t0 + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
                 for i in range(24)],
        "precipitation": [value] * 24,
    }}


def test_openmeteo_cache_separates_both_day_parameters(monkeypatch):
    calls = []

    def get(url, **kwargs):
        p = kwargs["params"]
        calls.append((p["past_days"], p["forecast_days"]))
        assert url == externals.OM_FORECAST_URL
        assert p["timezone"] == "Asia/Shanghai"
        return response(hourly(p["past_days"] + p["forecast_days"]))

    monkeypatch.setattr(requests, "get", get)
    for past, future, want in [(0, 3, 3.0), (1, 3, 4.0), (1, 2, 3.0)]:
        out = externals.fetch_openmeteo(past, future)
        assert out["precip"] == [want] * 24
        assert set(out) == {"times", "precip", "fetched_at"}
    assert externals.fetch_openmeteo("1", "3")["precip"] == [4.0] * 24
    assert calls == [(0, 3), (1, 3), (1, 2)]


def test_openmeteo_ttl_and_lru_bound(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(externals.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(externals.time, "time", lambda: clock[0])
    monkeypatch.setattr(externals, "_OM_CACHE_MAX", 2, raising=False)
    get = Mock(side_effect=lambda *a, **k: response(hourly()))
    monkeypatch.setattr(requests, "get", get)
    externals.fetch_openmeteo(0, 3, cache_s=10)
    externals.fetch_openmeteo(1, 3, cache_s=10)
    externals.fetch_openmeteo(0, 3, cache_s=10)  # refresh LRU, not TTL
    externals.fetch_openmeteo(2, 3, cache_s=10)
    externals.fetch_openmeteo(1, 3, cache_s=10)  # evicted
    assert get.call_count == 4
    assert len(externals._om_cache) <= 2
    clock[0] += 11
    externals.fetch_openmeteo(1, 3, cache_s=10)
    assert get.call_count == 5


def test_openmeteo_same_key_concurrent_fetch_once(monkeypatch):
    start = threading.Barrier(12)
    get = Mock(return_value=response(hourly()))
    monkeypatch.setattr(requests, "get", get)

    def fetch(_):
        start.wait(timeout=5)
        return externals.fetch_openmeteo()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(fetch, range(12)))
    assert all(r["precip"] == [0.25] * 24 for r in results)
    assert get.call_count == 1


def test_openmeteo_callers_cannot_poison_cache(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response(hourly()))
    first = externals.fetch_openmeteo()
    first["precip"][0] = 999
    first["times"].clear()
    second = externals.fetch_openmeteo()
    assert second["precip"][0] == 0.25 and len(second["times"]) == 24


@pytest.mark.parametrize("bad", [None, True, "2.5", float("nan"), float("inf"), -1, {}])
def test_openmeteo_rejects_bad_precipitation_not_cached(monkeypatch, bad):
    malformed = hourly()
    malformed["hourly"]["precipitation"][0] = bad
    get = Mock(side_effect=[response(malformed), response(hourly())])
    monkeypatch.setattr(requests, "get", get)
    assert externals.fetch_openmeteo() is None
    assert externals.fetch_openmeteo()["precip"][0] == 0.25
    assert get.call_count == 2


@pytest.mark.parametrize("kind", ["root", "hourly", "times_string", "precip_dict", "short", "unequal",
                                  "bad_time", "numeric_time", "non_hourly", "unordered"])
def test_openmeteo_rejects_malformed_hourly(monkeypatch, kind):
    payload = hourly()
    h = payload["hourly"]
    if kind == "root":
        payload = []
    elif kind == "hourly":
        payload["hourly"] = []
    elif kind == "times_string":
        h["time"] = "x" * 24
    elif kind == "precip_dict":
        h["precipitation"] = {str(i): i for i in range(24)}
    elif kind == "short":
        h["time"], h["precipitation"] = [], []
    elif kind == "unequal":
        h["precipitation"].pop()
    elif kind == "bad_time":
        h["time"][0] = "2026-13-99T00:00"
    elif kind == "numeric_time":
        h["time"][0] = 1
    elif kind == "non_hourly":
        h["time"][0] = "2026-11-03T00:30"
    else:
        h["time"][1] = h["time"][0]
    monkeypatch.setattr(requests, "get", lambda *a, **k: response(payload))
    assert externals.fetch_openmeteo() is None


@pytest.mark.parametrize("failure", [requests.Timeout("offline"), response({}, 503)])
def test_openmeteo_network_failure_falls_back(monkeypatch, failure):
    monkeypatch.setattr(requests, "get", Mock(side_effect=failure) if isinstance(failure, Exception)
                        else Mock(return_value=failure))
    assert externals.fetch_openmeteo() is None


def test_notify_due_concurrent_key_only_one_true(tmp_path):
    path = tmp_path / "state.json"
    start = threading.Barrier(20)

    def due(_):
        start.wait(timeout=5)
        return externals.notify_due(path, "T100:red")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(due, range(20)))
    assert results.count(True) == 1
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"T100:red"}


@pytest.mark.parametrize("raw", ["{", "[]", '{"k":"bad"}', '{"k":null}',
                                 '{"k":true}', '{"k":NaN}', '{"k":Infinity}', '{"k":-1}'])
def test_notify_due_corrupt_state_fails_closed(tmp_path, caplog, raw):
    path = tmp_path / "state.json"
    path.write_text(raw, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="flood.externals"):
        assert externals.notify_due(path, "new") is False
    assert path.read_text(encoding="utf-8") == raw
    assert caplog.records


@pytest.mark.parametrize("stage", ["replace", "dump"])
def test_notify_due_failed_atomic_write_preserves_old_state(tmp_path, monkeypatch, caplog, stage):
    path = tmp_path / "state.json"
    old = '{"previous": 100.0}'
    path.write_text(old, encoding="utf-8")
    if stage == "replace":
        monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))
    else:
        def broken_dump(obj, stream, **kwargs):
            stream.write('{"partial":')
            raise OSError("disk full")
        monkeypatch.setattr(externals.json, "dump", broken_dump)
    with caplog.at_level(logging.WARNING, logger="flood.externals"):
        assert externals.notify_due(path, "new") is False
    assert path.read_text(encoding="utf-8") == old
    assert [p for p in tmp_path.iterdir() if p.is_file()] == [path]
    assert caplog.records


def test_notify_due_uses_unique_temps_and_retains_cooldown(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    replace = os.replace
    sources = []

    def record(src, dst):
        sources.append(str(src))
        assert os.path.dirname(src) == str(tmp_path)
        assert str(src) != str(dst)
        replace(src, dst)

    monkeypatch.setattr(os, "replace", record)
    assert externals.notify_due(path, "a")
    assert externals.notify_due(path, "b")
    assert not externals.notify_due(path, "a")
    assert len(set(sources)) == 2
    assert [p for p in tmp_path.iterdir() if p.is_file()] == [path]


def test_notify_due_bound_does_not_evict_active_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(externals, "_NOTIFY_MAX_KEYS", 2, raising=False)
    path = tmp_path / "state.json"
    assert externals.notify_due(path, "a")
    assert externals.notify_due(path, "b")
    assert externals.notify_due(path, "c") is False
    assert externals.notify_due(path, "a") is False
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2


def test_notify_due_expires_old_entries(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    clock = [4_000_000.0]
    monkeypatch.setattr(externals.time, "time", lambda: clock[0])
    path.write_text('{"ancient": 1}', encoding="utf-8")
    assert externals.notify_due(path, "a", cooldown_s=60)
    assert "ancient" not in json.loads(path.read_text(encoding="utf-8"))
    clock[0] += 61
    assert externals.notify_due(path, "a", cooldown_s=60)


def smtp_double(monkeypatch, refused=False):
    events = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            events.append((type(self).__name__, host, port, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close",))

        def ehlo(self):
            events.append(("ehlo",))

        def starttls(self, *, context):
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
            events.append(("starttls",))

        def login(self, user, password):
            events.append(("login",))

        def sendmail(self, sender, recipients, message):
            events.append(("mail", recipients[0]))
            return {recipients[0]: (550, b"rejected")} if refused else {}

    class SMTP_SSL(SMTP):
        pass

    monkeypatch.setattr(externals.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(externals.smtplib, "SMTP_SSL", SMTP_SSL)
    return events


@pytest.mark.parametrize("extra, transport, port, tls", [
    ({"security": "starttls", "use_tls": True}, "SMTP", 587, True),
    ({"security": "ssl", "use_tls": False}, "SMTP_SSL", 465, False),
    ({"security": "none"}, "SMTP", 25, False),
    ({"use_tls": True}, "SMTP_SSL", 465, False),
    ({"use_tls": False, "port": 2525}, "SMTP", 2525, False),
])
def test_smtp_security_and_legacy_config(monkeypatch, extra, transport, port, tls):
    events = smtp_double(monkeypatch)
    cfg = {"host": "example.invalid", "user": "sender", "password": "fake", **extra}
    assert externals.send_smtp(cfg, "subject", "body", ["a@example.invalid"]) == (1, [])
    assert events[0][:3] == (transport, "example.invalid", port)
    names = [e[0] for e in events]
    assert ("starttls" in names) is tls
    if tls:
        assert names.index("ehlo") < names.index("starttls") < names.index("login")
    if transport == "SMTP_SSL":
        context = events[0][3]["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_smtp_recipients_deduplicated_and_capped(monkeypatch):
    events = smtp_double(monkeypatch)
    cfg = {"host": "example.invalid", "user": "sender", "password": "fake"}
    targets = [" a@example.invalid ", "a@example.invalid", "A@example.invalid", None, ""]
    targets += [f"p{i}@example.invalid" for i in range(30)]
    n, errs = externals.send_smtp(cfg, "s", "b", targets)
    actual = [e[1] for e in events if e[0] == "mail"]
    assert n == 20 and not errs
    assert actual == ["a@example.invalid"] + [f"p{i}@example.invalid" for i in range(19)]


def test_smtp_refusal_not_counted_as_accepted(monkeypatch):
    smtp_double(monkeypatch, refused=True)
    n, errs = externals.send_smtp({"host": "x", "user": "u", "password": "fake"}, "s", "b", ["a@b.c"])
    assert n == 0 and errs


def test_smtp_bad_security_rejected_without_connect(monkeypatch):
    events = smtp_double(monkeypatch)
    n, errs = externals.send_smtp({"host": "x", "user": "u", "password": "fake", "security": "typo"},
                                "s", "b", ["a@b.c"])
    assert n == 0 and errs and not events


@pytest.fixture
def notify_pool(monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(externals, "_notify_executor", pool, raising=False)
        monkeypatch.setattr(externals, "_notify_slots", threading.BoundedSemaphore(2), raising=False)
        yield pool


def test_notify_async_bound_queue_and_releases_slots(monkeypatch, notify_pool):
    entered, finish = threading.Event(), threading.Event()

    def webhook(*args):
        entered.set()
        assert finish.wait(timeout=5)
        return True, ""

    monkeypatch.setattr(externals, "send_wecom", webhook)
    submitted = []
    try:
        first = externals.notify_async("https://example.invalid", {}, "s", "md", "b", [])
        assert entered.wait(timeout=5)
        submitted.append(first)
        second = externals.notify_async("https://example.invalid", {}, "s", "md", "b", [])
        submitted.append(second)
        rejected = externals.notify_async("https://example.invalid", {}, "s", "md", "b", [])
        assert first["queued"] and second["queued"]
        assert rejected["queued"] is False and rejected["reason"]
        assert rejected["future"] is None
        assert "sent" not in first
    finally:
        finish.set()
        for job in submitted:
            if isinstance(job, dict):
                job["future"].result(timeout=5)
            else:  # old implementation must not leave its daemon running on RED
                job.join(timeout=5)
    again = externals.notify_async("https://example.invalid", {}, "s", "md", "b", [])
    assert again["queued"]
    assert again["future"].result(timeout=5)["wecom"]["ok"]


def test_notify_async_reports_partial_failure(monkeypatch, notify_pool, caplog):
    seen = []
    monkeypatch.setattr(externals, "send_wecom", lambda *a: (False, "HTTP 503"))

    def mail(cfg, subject, body, recipients):
        seen.extend(recipients)
        return 1, ["second recipient refused"]

    monkeypatch.setattr(externals, "send_smtp", mail)
    with caplog.at_level(logging.INFO, logger="flood.externals"):
        job = externals.notify_async("https://example.invalid", {"host": "x"}, "s", "md", "b",
                                     ["a@b.c", "a@b.c", "d@b.c"])
        assert job["queued"] is True
        out = job["future"].result(timeout=5)
    assert seen == ["a@b.c", "d@b.c"]
    assert out["wecom"] == {"ok": False, "error": "HTTP 503"}
    assert out["smtp"]["ok"] is False
    assert out["smtp"]["accepted"] == 1 and out["smtp"]["requested"] == 2
    assert out["smtp"]["errors"]
    assert any("FAIL" in r.message for r in caplog.records)


def test_notify_async_submit_failure_and_missing_channels(monkeypatch, notify_pool):
    assert externals.notify_async("", {}, "s", "md", "b", [])["queued"] is False
    monkeypatch.setattr(notify_pool, "submit", Mock(side_effect=RuntimeError("shut down")))
    for _ in range(3):
        job = externals.notify_async("https://example.invalid", {}, "s", "md", "b", [])
        assert job["queued"] is False and "shut down" in job["reason"]
    assert externals._notify_slots.acquire(blocking=False)
    externals._notify_slots.release()


def chat(content="answer", tool_calls=None):
    msg = {"content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def tool(arguments='{"return_period": 100}'):
    return {"id": "c1", "type": "function", "function": {"name": "get_warning", "arguments": arguments}}


@pytest.mark.parametrize("payload", [chat(), chat(None, [tool()])])
def test_llm_valid_chat_and_tools_preserve_api(monkeypatch, payload):
    post = Mock(return_value=response(payload))
    monkeypatch.setattr(requests, "post", post)
    assert externals.llm_chat("https://example.invalid/v1/", "fake", "model", [], tools=[]) == payload
    assert post.call_args.args[0] == "https://example.invalid/v1/chat/completions"
    assert post.call_args.kwargs["timeout"] == externals._LLM_TIMEOUT


@pytest.mark.parametrize("payload", [[], {"choices": []}, {"choices": "bad"}, {"choices": [None]},
    {"choices": [{"message": []}]}, chat([]), chat(""), chat(None), chat(None, {}),
    chat(None, [None]), chat(None, [{"id": "c", "function": []}]),
    chat(None, [tool("{broken")]), chat(None, [tool("[]")]), chat(None, [tool("null")]),
    chat(None, [tool('{"n":NaN}')]), chat(None, [tool({})])])
def test_llm_malformed_response_returns_fallback(monkeypatch, payload):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response(payload))
    assert externals.llm_chat("https://example.invalid", "fake", "m", []) is None


@pytest.mark.parametrize("failure", [requests.Timeout("offline"), response({}, 503), "not json"])
def test_llm_transport_failure_returns_fallback(monkeypatch, failure):
    if failure == "not json":
        r = response()
        r._content = b"{broken"
        post = Mock(return_value=r)
    elif isinstance(failure, Exception):
        post = Mock(side_effect=failure)
    else:
        post = Mock(return_value=failure)
    monkeypatch.setattr(requests, "post", post)
    assert externals.llm_chat("https://example.invalid", "fake", "m", []) is None


def test_llm_malformed_actual_transport_keeps_app_rule_fallback(monkeypatch):
    import app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app, "LLM_CFG", {"api_key": "fake", "base_url": "https://example.invalid", "model": "m"})
    monkeypatch.setattr(app, "llm_chat", externals.llm_chat)
    monkeypatch.setattr(requests, "post", lambda *a, **k: response(chat(None, [tool("[]")])))
    r = TestClient(app.app).post("/api/assistant", json={"question": "洪涝模拟原理是什么？"})
    assert r.status_code == 200
    assert r.json().get("answer") and not r.json().get("engine", "").startswith("llm")


@pytest.mark.parametrize("status,payload,want", [(200, {"errcode": 0}, True), (200, {"errcode": 1}, False),
                                                (503, {}, False), (200, [], False),
                                                (200, {}, False), (200, {"errcode": False}, False)])
def test_wecom_offline_status(monkeypatch, status, payload, want):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response(payload, status))
    ok, err = externals.send_wecom("https://example.invalid", "x")
    assert ok is want
    assert ok or err


def test_wecom_non_json_200_is_not_delivery_confirmation(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response({}, 200, "text/html"))
    ok, err = externals.send_wecom("https://example.invalid", "x")
    assert not ok and err


def test_wecom_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(requests, "post", Mock(side_effect=requests.Timeout("offline")))
    ok, err = externals.send_wecom("https://example.invalid", "x")
    assert not ok and "offline" in err


def test_smtp_starttls_failure_never_logs_in_or_downgrades(monkeypatch):
    events = smtp_double(monkeypatch)
    monkeypatch.setattr(externals.smtplib.SMTP, "starttls",
                        Mock(side_effect=ssl.SSLCertVerificationError("untrusted")))
    n, errs = externals.send_smtp({"host": "x", "user": "u", "password": "fake", "security": "starttls"},
                                "s", "b", ["a@b.c"])
    assert n == 0 and errs
    assert not any(e[0] in ("login", "mail", "SMTP_SSL") for e in events)


def test_notify_async_channel_exception_does_not_hide_other_channel(monkeypatch, notify_pool):
    monkeypatch.setattr(externals, "send_wecom", Mock(side_effect=RuntimeError("webhook failed")))
    monkeypatch.setattr(externals, "send_smtp", lambda *a: (1, []))
    job = externals.notify_async("https://example.invalid", {"host": "x"}, "s", "md", "b", ["a@b.c"])
    result = job["future"].result(timeout=5)
    assert result["wecom"] == {"ok": False, "error": "webhook failed"}
    assert result["smtp"] == {"ok": True, "accepted": 1, "requested": 1, "errors": []}


def test_notify_due_oversized_state_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(externals, "_NOTIFY_MAX_BYTES", 10)
    path = tmp_path / "state.json"
    old = '{"k":1234567890}'
    path.write_text(old, encoding="utf-8")
    assert externals.notify_due(path, "new") is False
    assert path.read_text(encoding="utf-8") == old


@pytest.mark.parametrize("cooldown", [float("inf"), float("nan"), -1, 31 * 24 * 3600])
def test_notify_due_invalid_cooldown_never_creates_state(tmp_path, cooldown):
    path = tmp_path / "state.json"
    assert externals.notify_due(path, "k", cooldown) is False
    assert not path.exists()


@pytest.mark.parametrize("limit,count", [(1, 1), (5, 2), (5, 0)])
def test_stac_first_page_limit_sort_and_empty_page(monkeypatch, limit, count):
    # 现有契约仅一页；无 next-link fixture，不能声称已覆盖远端续页协议。
    features = [{"id": f"item-{i}", "assets": {"vv": {"href": "https://example.invalid/cog"}}}
                for i in range(count)]
    post = Mock(return_value=response({"features": features}))
    monkeypatch.setattr(requests, "post", post)
    assert sat_data.stac_search("sentinel-1-rtc", [1, 2, 3, 4], "2022-06-26/2022-06-27",
                               limit=limit, sortby="datetime") == features
    assert post.call_count == 1
    assert post.call_args.kwargs["json"] == {
        "collections": ["sentinel-1-rtc"], "bbox": [1, 2, 3, 4],
        "datetime": "2022-06-26/2022-06-27", "limit": limit,
        "sortby": [{"field": "datetime", "direction": "asc"}],
    }


def test_stac_http_failure_not_silently_empty(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response({}, 503))
    with pytest.raises(requests.HTTPError):
        sat_data.stac_search("sentinel-1-rtc", [1, 2, 3, 4], "2022-06-26/2022-06-27")


def test_sign_retries_transient_and_returns_existing_href_field(monkeypatch):
    get = Mock(side_effect=[response({}, 429), response({}, 503), response({"href": "https://example.invalid/signed"})])
    waits = []
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr("time.sleep", waits.append)
    assert sat_data.sign_url("https://example.invalid/unsigned") == "https://example.invalid/signed"
    assert waits == [5, 15]
    assert get.call_args.kwargs["params"] == {"href": "https://example.invalid/unsigned"}


def test_sign_retry_exhaustion_explicit_http_error_no_final_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: response({}, 503))
    monkeypatch.setattr("time.sleep", waits.append)
    with pytest.raises(requests.HTTPError, match="重试耗尽") as exc:
        sat_data.sign_url("https://example.invalid/unsigned", retries=2)
    assert exc.value.response.status_code == 503
    assert waits == [5]


def test_sign_zero_attempts_rejected_before_network():
    with pytest.raises(ValueError, match="retries"):
        sat_data.sign_url("https://example.invalid/unsigned", retries=0)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_sign_permanent_error_not_retried(monkeypatch, status):
    get = Mock(return_value=response({}, status))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr("time.sleep", lambda _: pytest.fail("permanent errors must not back off"))
    with pytest.raises(requests.HTTPError):
        sat_data.sign_url("https://example.invalid/unsigned")
    assert get.call_count == 1


@pytest.mark.parametrize("resampling", [None, sat_data.Resampling.nearest])
def test_cog_window_nodata_transform_and_resampling(monkeypatch, resampling):
    from rasterio.crs import CRS
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    src = Mock()
    src.crs = CRS.from_epsg(4326)
    src.transform = from_origin(0, 4, 1, 1)
    src.nodata = -32768
    src.read.return_value = np.array([[1, -32768], [3, 4]], dtype="int16")
    opened = Mock()
    opened.__enter__ = Mock(return_value=src)
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(sat_data.rasterio, "open", Mock(return_value=opened))

    def reproject(source, destination, **kwargs):
        np.testing.assert_array_equal(source, [[1, -9999], [3, 4]])
        assert source.dtype == np.float32
        assert kwargs["src_transform"] == from_origin(1, 3, 1, 1)
        assert kwargs["dst_transform"] == from_origin(1, 3, 1, 1)
        assert kwargs["src_nodata"] == -9999
        assert np.isnan(kwargs["dst_nodata"])
        want = sat_data.Resampling.bilinear if resampling is None else sat_data.Resampling.nearest
        assert kwargs["resampling"] == want
        destination[:] = np.where(source == -9999, np.nan, source)

    monkeypatch.setattr(sat_data, "reproject", reproject)
    out = sat_data.read_window("https://example.invalid/cog", [1, 1, 3, 3], 4326, 2, 2, resampling)
    src.read.assert_called_once_with(1, window=Window(1, 1, 2, 2), boundless=True)
    np.testing.assert_allclose(out, [[1, np.nan], [3, 4]], equal_nan=True)
    assert out.shape == (2, 2) and out.dtype == np.float32
    opened.__exit__.assert_called_once()


def test_sar_legacy_threshold_argument_remains_compatible():
    arr = np.array([0.1, 1.0, np.nan], dtype="float32")
    np.testing.assert_allclose(sar_change.to_db(arr, linear_thresh=999), [-10, 0, np.nan], atol=1e-6)
    np.testing.assert_allclose(sar_change.to_db(arr, 0.001), [-10, 0, np.nan], atol=1e-6)
    db = np.array([-30.0, -10.0, np.nan])
    np.testing.assert_array_equal(sar_change.to_db(db), db)
    empty = np.array([np.nan])
    np.testing.assert_array_equal(sar_change.to_db(empty), empty)

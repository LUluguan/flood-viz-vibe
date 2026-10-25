# -*- coding: utf-8 -*-
"""v1.6 外部集成与大改造回归测试(对标§四2/5/8/9/10/14/15/17/21)。
全部离线: Open-Meteo / LLM / webhook / SMTP 均打桩, 不触真实外网。
"""
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from fastapi.testclient import TestClient
import app
import externals
import flood_core
import numpy as np


def _fake_om(hours=90, heavy=(10, 20)):
    """构造 Open-Meteo 形状的假序列: now 附近对齐, hours 逐时, heavy 时段强降雨。"""
    now = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
    t0 = now - datetime.timedelta(hours=24)
    times, precip = [], []
    for i in range(hours + 25):
        t = t0 + datetime.timedelta(hours=i)
        times.append(t.strftime("%Y-%m-%dT%H:00"))
        precip.append(12.0 if heavy[0] <= (t - now).total_seconds() / 3600 < heavy[1] else 0.2)
    return {"times": times, "precip": precip, "fetched_at": 0.0}


# ---------- A1/A2: Open-Meteo 实时雨情与预报驱动 ----------
def test_realtime_rain_openmeteo_and_fallback(monkeypatch):
    monkeypatch.setattr(app, "fetch_openmeteo", lambda **kw: _fake_om())
    c = TestClient(app.app)
    d = c.get("/api/realtime_rain").json()
    assert "Open-Meteo 实况" in d["note"], d["note"]
    assert d["city_rain24h_mm"] > 0 and len(d["stations"]) == 5
    assert all(0 <= s["rain24h_mm"] <= 200 for s in d["stations"])
    # 失败回退演示
    monkeypatch.setattr(app, "fetch_openmeteo", lambda **kw: None)
    d2 = c.get("/api/realtime_rain").json()
    assert "回退演示" in d2["note"], d2["note"]


def test_forecast_flood(monkeypatch):
    monkeypatch.setattr(app, "fetch_openmeteo", lambda **kw: _fake_om())
    c = TestClient(app.app)
    r = c.get("/api/forecast_flood", params={"c": 0.5})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert [s["t_h"] for s in d["series"]] == [6, 12, 24, 48, 72]
    assert any(s["flooded_area_km2"] > 0 for s in d["series"])
    assert d["worst_t_h"] in (12, 24, 48, 72)
    assert d["extent"]["features"], "最不利时距应有淹没多边形(带 W)"
    assert "W" in d["extent"]["features"][0]["properties"]
    # 预报源不可用 → 503(无假预报兜底)
    monkeypatch.setattr(app, "fetch_openmeteo", lambda **kw: None)
    assert c.get("/api/forecast_flood").status_code == 503


# ---------- A3: 预警真实触达(wecom + 冷却) ----------
def test_warning_notify_channel(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(app, "WECOM_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(app, "_NOTIFY_STATE", str(tmp_path / "notify_state.json"))
    monkeypatch.setattr(app, "notify_async",
                        lambda *a, **kw: sent.append(a) or _noop_thread())
    c = TestClient(app.app)
    r1 = c.get("/api/warning", params={"return_period": 100}).json()
    assert r1["notify"]["channel"] == "企业微信+邮件"
    if r1["city_level"] in ("橙色", "红色"):
        assert r1["notify"].get("sent"), "橙/红预警且已配置 webhook 应触发触达"
        assert "洪涝预警" in sent[0][3]           # markdown 标题
        # 冷却: 同情景同等级 30 分钟内不重复
        c.get("/api/warning", params={"return_period": 100})
        assert len(sent) == 1, "冷却期内不应重复触达"
    else:
        assert not sent


def _noop_thread():
    class _T:
        def start(self):
            pass
    return _T()


def test_externals_unit(tmp_path):
    ok, err = externals.send_wecom("", "x")
    assert not ok and "未配置" in err
    n, errs = externals.send_smtp({}, "s", "b", ["a@b.c"])
    assert n == 0 and errs
    # 冷却状态: 首次 True, 立即再试 False
    st = str(tmp_path / "s.json")
    assert externals.notify_due(st, "k1", cooldown_s=60)
    assert not externals.notify_due(st, "k1", cooldown_s=60)
    assert externals.notify_due(st, "k2", cooldown_s=60)


# ---------- A4: LLM function-calling 问答 ----------
def test_llm_assistant_with_tools(monkeypatch):
    calls = {"n": 0}

    def fake_llm(base, key, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:   # 第 1 轮: 请求调用工具
            return {"choices": [{"message": {
                "content": "", "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "get_warning",
                                 "arguments": json.dumps({"return_period": 100})}}]}}]}
        return {"choices": [{"message": {
            "content": "100年一遇情景为橙色预警, 建议减少外出并转移低洼人员。"}}]}

    monkeypatch.setattr(app, "LLM_CFG", {"api_key": "k", "base_url": "http://x",
                                         "model": "glm-4-flash"})
    monkeypatch.setattr(app, "llm_chat", fake_llm)
    c = TestClient(app.app)
    r = c.post("/api/assistant", json={"question": "现在什么预警?"})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["engine"].startswith("llm"), d
    assert "橙色" in d["answer"]


def test_llm_assistant_fallback_rules(monkeypatch):
    # 无 key: 直接走规则引擎(现有行为保持)
    monkeypatch.setattr(app, "LLM_CFG", {"api_key": "", "base_url": "", "model": ""})
    c = TestClient(app.app)
    r = c.post("/api/assistant", json={"question": "100年一遇淹多大?"})
    assert r.status_code == 200
    assert "engine" not in r.json()
    # 有 key 但调用失败: 回退规则引擎
    monkeypatch.setattr(app, "LLM_CFG", {"api_key": "k", "base_url": "http://x",
                                         "model": "m"})
    monkeypatch.setattr(app, "llm_chat", lambda *a, **kw: None)
    r2 = c.post("/api/assistant", json={"question": "100年一遇淹多大?"})
    assert r2.status_code == 200 and "engine" not in r2.json()


# ---------- B1: 排水折减(工程预案) ----------
def test_online_sim_drain():
    c = TestClient(app.app)
    d0 = c.get("/api/online_sim", params={"rain_mm": 300, "c": 0.5, "drain_mm": 0}).json()
    d20 = c.get("/api/online_sim", params={"rain_mm": 300, "c": 0.5, "drain_mm": 20}).json()
    assert d20["drain_mm"] == 20.0
    assert d20["flooded_area_km2"] <= d0["flooded_area_km2"], (d0, d20)
    assert d20["water_level_m"] <= d0["water_level_m"]
    assert c.get("/api/online_sim", params={"rain_mm": 300, "drain_mm": 99}).status_code == 422


def test_flood_core_drain_unit():
    z = np.full((9, 9), 12.0)
    z[0, :] = -5.0
    z[1:5, 0:5] = 2.0
    z[2:4, 1:4] = 0.5
    m = flood_core.DepressionModel(z)
    d0, _ = m.flood(0.3, 900.0)
    d1, _ = m.flood(0.3, 900.0, drain_depth_m=0.2)
    assert d1.sum() < d0.sum()
    d2, _ = m.flood(0.3, 900.0, drain_depth_m=0.5)   # 全部折减 → 干
    assert d2.sum() == 0


# ---------- B2: 集合预报 ----------
def test_ensemble():
    c = TestClient(app.app)
    d = c.get("/api/ensemble", params={"return_period": 100}).json()
    assert d["members"] == 24 and d["W_p10"] <= d["W_p50"] <= d["W_p90"], d
    assert d["flooded_area_km2_range"][0] <= d["flooded_area_km2_range"][1]
    r = c.get("/api/ensemble_png", params={"return_period": 100})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


# ---------- B3: 损失曲线族 ----------
def test_impact_loss_curves():
    c = TestClient(app.app)
    demo = c.get("/api/impact", params={"return_period": 100, "loss_curve": "demo"}).json()
    hazus = c.get("/api/impact", params={"return_period": 100, "loss_curve": "hazus"}).json()
    assert demo["loss_curve"] == "demo" and hazus["loss_curve"] == "hazus"
    assert hazus["estimated_loss_wan"] > 0
    assert "示意" in hazus["loss_note"], "必须诚实标注 HAZUS 数值为示意逼近"
    assert c.get("/api/impact", params={"loss_curve": "x"}).status_code == 422


# ---------- B4: OGC API-Features + COG ----------
def test_ogc_features():
    c = TestClient(app.app)
    land = c.get("/api/ogc").json()
    assert any("conformance" in l["href"] for l in land["links"])
    cols = c.get("/api/ogc/collections").json()["collections"]
    assert {"flood-extent", "buildings"} <= {x["id"] for x in cols}
    r = c.get("/api/ogc/collections/flood-extent/items", params={"return_period": 100})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    d = r.json()
    assert d["numberMatched"] > 0 and d["numberReturned"] > 0
    assert "W" in d["features"][0]["properties"]
    b = c.get("/api/ogc/collections/buildings/items", params={"limit": 300}).json()
    assert b["numberMatched"] >= 200
    assert c.get("/api/ogc/collections/nope/items").status_code == 404


def test_cog_endpoint():
    c = TestClient(app.app)
    r = c.get("/api/cog/100")
    if not os.path.exists(os.path.join(ROOT, "flood_out", "cog", "flood_depth_100y_cog.tif")):
        import pytest
        pytest.skip("COG 未生成(需先运行 tools/make_cogs.py)")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/tiff")
    assert r.content[:4] == b"II\x2a\x00"           # little-endian TIFF magic
    assert c.get("/api/cog/7").status_code == 404


if __name__ == "__main__":
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                if len(inspect.signature(fn).parameters) == 0:
                    fn()
                else:
                    print("(skip %s: 需要 fixture, 由 pytest 运行)" % name)
            except Exception as e:
                print("FAIL", name, e)
                raise
    print("test_v16_integrations OK(不含 fixture 用例)")

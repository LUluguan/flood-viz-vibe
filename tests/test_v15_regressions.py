# -*- coding: utf-8 -*-
"""v1.5 改造回归测试(对标分析 §四 实施, 2026-10-25):
多洼地算法/安全加固(PBKDF2/token清扫/IP限流/report锁)/hydrograph/前端请求头。
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from fastapi.testclient import TestClient
import app
import store


# ---------- BE-01: report_status 与并发提交不丢 ----------
def test_report_status_concurrent_no_loss():
    orig = app._rate_ok
    app._rate_ok = lambda ip, limit=20, window=60.0: True
    errs = []
    out = {"created": [], "verified": 0}
    rid_box = {}

    def creator(i):
        try:
            cl = TestClient(app.app)
            r = cl.post("/api/report", json={"location_text": "并发%d" % i})
            out["created"].append((r.json() or {}).get("id"))
        except Exception as e:
            errs.append(str(e))

    try:
        # 先建一条供管理员核实
        cl = TestClient(app.app)
        rid = cl.post("/api/report", json={"location_text": "待核实目标"}).json()["id"]
        rid_box["rid"] = rid
        # 并发: 10 公众提交 × 同步循环核实
        ts = [threading.Thread(target=creator, args=(i,)) for i in range(10)]
        for t in ts:
            t.start()
        for _ in range(10):
            cl.post("/api/report/%s/status" % rid, json={"status": "已核实"},
                    headers={"x-auth-token": _admin_tok(cl)})
        for t in ts:
            t.join()
    finally:
        app._rate_ok = orig
    assert not errs, errs
    ids = [i for i in out["created"] if i]
    assert len(ids) == 10, out
    on_disk = {x["id"] for x in store._load_reports()}
    assert set(ids) <= on_disk, "核实与提交并发时丢失了公众报汛(BE-01 回归)"
    tgt = [x for x in store._load_reports() if x["id"] == rid_box["rid"]]
    assert tgt and tgt[0]["status"] == "已核实"


def _admin_tok(cl):
    return cl.post("/api/auth/login",
                   json={"username": "admin", "password": "admin123"}).json()["token"]


# ---------- BE-02: _tokens 过期清扫 ----------
def test_tokens_gc():
    # 塞入 600 条过期 token 触发清扫(阈值 512)
    now = time.time()
    for i in range(600):
        app._tokens["expired%d" % i] = {"username": "x", "role": "public",
                                        "ts": now - 13 * 3600}
    app._tokens["alive"] = {"username": "a", "role": "admin", "ts": now}
    cl = TestClient(app.app)
    cl.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert len(app._tokens) < 100, "过期 token 应被清扫, 剩余 %d" % len(app._tokens)
    assert "alive" in app._tokens


# ---------- BE-05: 登录 IP 维度限流(口令喷洒) ----------
def test_login_ip_spray_limited():
    cl = TestClient(app.app)
    codes = []
    for i in range(35):   # 同 IP 轮换用户名, 每个用户名只试 1 次 → 绕过 (ip,user) 键
        r = cl.post("/api/auth/login", json={"username": "victim%03d" % i,
                                             "password": "wrong"})
        codes.append(r.status_code)
    assert 429 in codes, "纯 IP 维度限流未生效(口令喷洒可绕过): %s" % codes


# ---------- BE-06: online_sim 频控 ----------
def test_online_sim_rate_limited():
    cl = TestClient(app.app)
    codes = [cl.get("/api/online_sim", params={"rain_mm": 30}).status_code
             for _ in range(32)]
    assert 429 in codes, "online_sim 频控未生效: %s" % codes


# ---------- BE-07: PBKDF2 + 旧哈希平滑升级 ----------
def test_pbkdf2_and_legacy_upgrade():
    # 新播种账号应为 pbkdf2 格式
    cl = TestClient(app.app)
    r = cl.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    users = store._load_users()
    admin = next(u for u in users if u["username"] == "admin")
    assert admin["hash"].startswith("pbkdf2$"), admin["hash"]
    # 旧 sha256 格式: 验证成功后自动升级
    legacy_salt = "abcd1234"
    import hashlib
    legacy_hash = hashlib.sha256((legacy_salt + "public").encode()).hexdigest()
    store._atomic_write_json(store._AUTH_FILE, [
        {"username": "legacy", "salt": legacy_salt, "hash": legacy_hash, "role": "public"}])
    cl2 = TestClient(app.app)
    r2 = cl2.post("/api/auth/login", json={"username": "legacy", "password": "public"})
    assert r2.status_code == 200, r2.text
    users2 = store._load_users()
    legacy = next(u for u in users2 if u["username"] == "legacy")
    assert legacy["hash"].startswith("pbkdf2$"), "旧 sha256 登录成功后未升级"
    # 错误口令仍被拒(两种格式)
    assert cl2.post("/api/auth/login", json={"username": "legacy", "password": "x"}).status_code == 401


# ---------- BE-04: store_quarantines 接线 ----------
def test_health_store_quarantines_wired():
    cl = TestClient(app.app)
    store._state_quarantines[0] = 0
    # 制造损坏文件触发隔离
    with open(store._REPORTS_FILE, "w", encoding="utf-8") as f:
        f.write("{not-json")
    cl.get("/api/report")
    h = cl.get("/api/health").json()
    assert h["store_quarantines"] >= 1, "隔离计数未透出到 /api/health"


# ---------- BE-08: assistant 数据缺失守卫 ----------
def test_assistant_missing_data_guard(monkeypatch):
    monkeypatch.setattr(app, "_load_depth_tif", lambda T: None)
    # 钉住规则引擎兜底分支(.env 有真实 key 时 LLM 会先接管, 其行为由 test_llm_* 覆盖)
    monkeypatch.setattr(app, "LLM_CFG", {"api_key": "", "base_url": "", "model": ""})
    cl = TestClient(app.app)
    r = cl.post("/api/assistant", json={"question": "影响多少建筑"})
    assert r.status_code == 200 and "缺失" in r.json()["answer"]
    r2 = cl.post("/api/assistant", json={"question": "经济损失多少钱"})
    assert r2.status_code == 200 and "缺失" in r2.json()["answer"]


# ---------- 对标§四13: hydrograph ----------
def test_hydrograph():
    cl = TestClient(app.app)
    r = cl.get("/api/hydrograph", params={"lon": 113.33, "lat": 23.11, "rain_mm": 300})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert len(d["series"]) == 16 and d["series"][0]["t_h"] == 1.5
    depths = [p["depth_m"] for p in d["series"]]
    assert depths == sorted(depths), "涨水段水深应单调不减"
    assert d["peak_depth_m"] == max(depths)
    if d["flooded"]:
        assert d["onset_t_h"] is not None
    # 研究区外/河道点 → 400
    assert cl.get("/api/hydrograph", params={"lon": 113.33, "lat": 23.0}).status_code == 400


# ---------- 对标§四1: scenarios 与 online_sim 的多洼地一致性 ----------
def test_scenarios_multi_depression_fields():
    cl = TestClient(app.app)
    scens = {s["return_period_y"]: s for s in cl.get("/api/scenarios").json()}
    for T, s in scens.items():
        assert s.get("method") == "multi-depression", s
        assert s.get("n_pools", 0) > 0 and len(s.get("top_pools", [])) >= 1
    # 本地 extent 多边形带 per-pool W
    fc = cl.get("/api/flood_extent", params={"return_period": 100}).json()
    ws = [f.get("properties", {}).get("W") for f in fc["features"]]
    assert any(w and w > 0 for w in ws), "淹没多边形应携带自身水面 W"


# ---------- FE-01/02/03: 前端 token 走请求头 + esc 上移 ----------
def test_frontend_token_header_regression():
    idx = open("index.html", encoding="utf-8").read()
    mob = open("mobile.html", encoding="utf-8").read()
    cjs = open("web/common.js", encoding="utf-8").read()
    assert "'x-auth-token': tok" in idx or '"x-auth-token": tok' in idx
    assert "auth/me?token=" not in idx
    assert "'x-auth-token': localStorage" in mob or '"x-auth-token": localStorage' in mob
    assert "status?token=" not in mob
    assert "FLOOD.esc" in idx and "esc:" in cjs
    # 水体按斑块独立水面
    assert "polyW" in idx and "waterScale" in idx
    assert "waterPrimitive" not in idx, "死代码 waterPrimitive 应已清理"
    # 单点过程线面板
    assert "/api/hydrograph" in idx
    unet = open("unet.html", encoding="utf-8").read()
    assert "分区域 IoU" in unet, "by_region 区块应可渲染"
    assert "级验证结论一致)</span>';" not in unet, "unet.html:110 分号截断使 + (m.by_region...) 成为孤立表达式(FE-08)"


if __name__ == "__main__":
    test_report_status_concurrent_no_loss()
    test_tokens_gc()
    test_login_ip_spray_limited()
    test_online_sim_rate_limited()
    test_pbkdf2_and_legacy_upgrade()
    test_health_store_quarantines_wired()
    test_hydrograph()
    test_scenarios_multi_depression_fields()
    test_frontend_token_header_regression()
    print("test_v15_regressions OK")

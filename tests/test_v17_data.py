# -*- coding: utf-8 -*-
"""v1.7 官方数据落地测试: 广州暴雨强度公式设计雨型 / USACE-HAZUS 数值曲线 /
关键设施连锁(天地图 POI)。全部离线(设施数据已落盘 flood_out/critical_facilities.json)。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from fastapi.testclient import TestClient
import app
from pipeline_config import gz_rainfall_intensity


# ---------- §四4: 广州官方暴雨强度公式 + 交替块设计雨型 ----------
def test_gz_idf_values():
    # 官方区间公式直算校验(与抓取的参数逐值复核)
    i60_100 = gz_rainfall_intensity(60, 100)      # 100年 60min
    assert 1.8 < i60_100 < 3.0, "100年1h 强度应约 2.2mm/min(≈135mm/h), 实际 %.3f" % i60_100
    i60_2 = gz_rainfall_intensity(60, 2)          # 2年 60min ≈ 1.0mm/min(≈60mm/h)
    assert 0.7 < i60_2 < 1.4, i60_2
    assert gz_rainfall_intensity(120, 50) < gz_rainfall_intensity(60, 50)  # 历时越长平均强度越低


def test_design_storm_idf_consistency():
    """交替块法核心性质: 任一历时 k·Δ 的最大窗口雨量 == 官方公式累计值。"""
    c = TestClient(app.app)
    for T in (2, 50, 100):
        d = c.get("/api/design_storm", params={"return_period": T,
                                               "duration_min": 120, "step_min": 5}).json()
        h = d["hyetograph_mm"]
        assert len(h) == 24 and abs(sum(h) - d["total_mm"]) < 0.05
        for k in (1, 2, 4, 12):                    # 5/10/20/60 分钟
            w = k
            best = max(sum(h[i:i + w]) for i in range(len(h) - w + 1))
            expect = gz_rainfall_intensity(k * 5, T) * k * 5
            assert abs(best - expect) < 0.15, (T, k, best, expect)  # 逐块2位舍入累计容差
        # 峰值块应位于峰现位置附近(±1 块)
        peak_i = h.index(max(h))
        assert abs(peak_i - round(0.4 * len(h))) <= 1, peak_i
    assert c.get("/api/design_storm", params={"return_period": 3}).status_code == 200


# ---------- §四15: USACE/HAZUS 官方数值曲线 ----------
def test_hazus_curve_official_values():
    assert abs(app._loss_rate(1 * 0.3048, "住宅", "hazus") - 0.09) < 0.01
    assert abs(app._loss_rate(5 * 0.3048, "住宅", "hazus") - 0.38) < 0.01
    # 深水段末段饱和(保守)
    assert app._loss_rate(4.0, "住宅", "hazus") == 0.56
    assert app._loss_rate(0.01, "住宅", "hazus") == 0.0
    c = TestClient(app.app)
    d = c.get("/api/impact", params={"return_period": 100, "loss_curve": "hazus"}).json()
    assert "USACE" in d["loss_note"] and "示意" in d["loss_note"], d["loss_note"]


# ---------- §四16 简化版: 关键设施受淹与服务缺口 ----------
def test_facilities_data_present():
    p = os.path.join(ROOT, "flood_out", "critical_facilities.json")
    assert os.path.exists(p), "设施 JSON 应随仓库(天地图 POI 抓取产物)"
    j = json.load(open(p, encoding="utf-8"))
    kinds = {x["kind"] for x in j["features"]}
    assert len(j["features"]) >= 8 and {"医院", "消防站"} <= kinds, kinds


def test_cascade_endpoint():
    c = TestClient(app.app)
    r = c.get("/api/cascade", params={"return_period": 100})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["total"] >= 8
    assert d["grid_nodes"] == 0 and "电网节点" in d["note"], "当前数据源无变电站, 应如实标注"
    if d["failed"]:
        bad = [n for n in d["nodes"] if n["failed"]]
        assert all(n["depth_m"] > 0.5 for n in bad)
        if d["edges"]:
            e = d["edges"][0]
            assert e["to"]["depth_m"] > app.DEPTH_THRESH


if __name__ == "__main__":
    test_gz_idf_values()
    test_design_storm_idf_consistency()
    test_hazus_curve_official_values()
    test_facilities_data_present()
    test_cascade_endpoint()
    print("test_v17_data OK")

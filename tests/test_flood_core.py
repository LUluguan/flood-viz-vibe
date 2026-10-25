# -*- coding: utf-8 -*-
"""flood_core(多洼地独立水面浴缸法)单元测试: 对标分析 §四1 改造的算法正确性。

合成小地形验证四条不变量:
  1) 高位"悬空"洼地不再被全局水面"隔山淹没"(旧全局浴缸的核心伪影),
     且洼地水位相互独立;
  2) 体积守恒: Σ洼地蓄水 + 溢流出境 == q × Σ洼地汇水面积(直排河道部分不进洼地);
  3) 水深随 q 逐格单调不减, 展示口径 water_level_m 单调不减;
  4) 无洼地地形(排水通畅)不产生淹没; 满蓄后水位钳制在溢口。
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flood_core import DepressionModel

A = 900.0   # 单格面积(m²), 30m×30m


def _terrain_with_two_pools():
    """9×9: 第0行河道(-5), 陆域高12; 左低坑(底0.5/围2.0), 右高位塘(底6.0/围8.5),
    另有一条 z=3.0 的明渠直通南边界(排水通畅的低地, 不构成洼地)。"""
    z = np.full((9, 9), 12.0)
    z[0, :] = -5.0                      # 河道(出口)
    z[1:5, 0:5] = 2.0                   # 左坑围(行1邻河, 自由排水)
    z[2:4, 1:4] = 0.5                   # 左坑底(6格)
    z[1:5, 5:9] = 8.5                   # 右塘围(东缘接网格边界=出口)
    z[2:4, 6:8] = 6.0                   # 右塘底(4格, 高位"悬空")
    z[5:9, 4] = 3.0                     # 明渠: 低但通畅(直排边界), 非洼地
    return z


def _old_global_bathtub_W(z, q):
    """旧全局浴缸(60 轮二分), 仅用于对照伪影。"""
    land = z > 0
    lo, hi = 0.0, float(z.max()) + q
    for _ in range(60):
        W = 0.5 * (lo + hi)
        if float(np.clip(W - z[land], 0, None).mean()) < q:
            lo = W
        else:
            hi = W
    return 0.5 * (lo + hi)


def test_perched_pool_independent_levels():
    """核心伪影消除与洼地独立性(对标§四1):
    1) 排水通畅的低地(明渠 z=3, 非洼地)不再被旧全局水面"隔山淹没";
    2) 高位"悬空"塘水位由自身汇水解析决定, 与远处低坑存在与否无关;
    3) 各洼地水位相互独立(info["levels"] 一塘一水位)。"""
    z = _terrain_with_two_pools()
    m = DepressionModel(z)
    assert m.n_dep == 2
    q = 1.8
    depth, info = m.flood(q, A)
    # 1) 明渠: 旧全局浴缸 W_old≈7+ 会给它 4m 水深; 新算法排水通畅 → 干
    W_old = _old_global_bathtub_W(z, q)
    assert W_old > 4.5, W_old
    assert float(depth[6, 4]) == 0.0
    # 2) 高位塘(4 格)水位解析: W = 6.0 + q × catchment/cells(未满蓄)
    dep_r = next(d for d in range(m.n_dep) if len(m._cells[d]) == 4)
    v = q * float(m.catchment[dep_r]) * A
    assert v < m._cap[dep_r] * A, "测试参数应未触满蓄"
    expect_w = 6.0 + q * float(m.catchment[dep_r]) / 4.0
    assert abs(float(depth[2, 6]) - (expect_w - 6.0)) < 1e-3, (depth[2, 6], expect_w)
    # 独立性: 填平左低坑(其余地形不变), 高位塘水位完全不变
    z2 = z.copy()
    z2[2:4, 1:4] = 2.0
    m2 = DepressionModel(z2)
    depth2, _ = m2.flood(q, A)
    assert abs(float(depth2[2, 6]) - float(depth[2, 6])) < 1e-6
    # 3) 两洼地水位互不相同且各 only 一档
    assert len(info["levels"]) == 2
    assert info["levels"][0]["W_m"] >= info["levels"][1]["W_m"]


def test_volume_conservation_with_spill():
    """Σ洼地蓄水 + 溢流出境 == q × Σ洼地汇水面积(含满蓄溢流)。"""
    z = _terrain_with_two_pools()
    m = DepressionModel(z)
    assert m.catchment.sum() > 0
    for q in (0.02, 0.08, 0.2, 0.5, 2.0, 3.5):   # 3.5: 两塘均满蓄溢流
        depth, info = m.flood(q, A)
        stored = float(depth.sum()) * A
        target = q * float(m.catchment.sum()) * A
        got = stored + info["spilled_m3"]
        assert abs(got - target) < max(1.0, 1e-3 * target), (q, stored, info["spilled_m3"], target)


def test_monotonic_in_q():
    z = _terrain_with_two_pools()
    m = DepressionModel(z)
    prev_depth = np.zeros_like(z)
    prev_w = 0.0
    for q in (0.01, 0.05, 0.1, 0.3, 1.0, 3.0):
        depth, info = m.flood(q, A)
        assert (depth >= prev_depth - 1e-6).all(), q
        assert info["water_level_m"] >= prev_w - 1e-9, q
        prev_depth, prev_w = depth, info["water_level_m"]


def test_no_depression_no_flood():
    """单斜坡(每行向河道递降)无洼地: 任何 q 都不淹没。"""
    z = np.tile(np.linspace(8.0, 1.0, 9)[:, None], (1, 9))
    z[0, :] = -5.0
    m = DepressionModel(z)
    assert m.n_dep == 0
    depth, info = m.flood(0.5, A)
    assert depth.sum() == 0 and info["water_level_m"] == 0.0


def test_full_pool_capped_at_spill():
    """超大来水: 各洼地水位被钳制在各自溢口, 余量全部出境(spilled_m3>0)。"""
    z = _terrain_with_two_pools()
    m = DepressionModel(z)
    depth, info = m.flood(3.5, A)
    assert info["spilled_m3"] > 0
    cells_by_dep = {int(len(c)): d for d, c in enumerate(m._cells)}
    for l in info["levels"]:
        d = cells_by_dep[l["cells"]]
        assert l["W_m"] <= m._spill[d] + 1e-6, (l, m._spill[d])


def test_small_pit_passthrough():
    """默认 min_pool_cells=3: 1 格微坑视为排水通畅不蓄水, 来水经级联下泄不丢失。"""
    z = _terrain_with_two_pools()
    z[7, 1] = 1.0                       # 高原上的 1 格深坑(四周皆 12)
    m = DepressionModel(z)
    assert m.n_dep == 3
    depth, info = m.flood(0.5, A)
    assert float(depth[7, 1]) == 0.0            # 微坑自身不蓄水
    assert float(depth[2, 2]) > 0 and float(depth[2, 6]) > 0   # ≥3 格洼地正常蓄水
    # 微坑来水进入守恒(计入 spilled 或下游洼地)
    stored = float(depth.sum()) * A
    target = 0.5 * float(m.catchment.sum()) * A
    assert abs(stored + info["spilled_m3"] - target) < max(1.0, 1e-3 * target)


if __name__ == "__main__":
    test_perched_pool_independent_levels()
    test_volume_conservation_with_spill()
    test_monotonic_in_q()
    test_no_depression_no_flood()
    test_full_pool_capped_at_spill()
    test_small_pit_passthrough()
    print("flood_core 多洼地算法 6 项不变量 OK")

# -*- coding: utf-8 -*-
"""
uncertainty_bands.py — 重现期情景不确定性带(降雨敏感性分析)

参考 Fathom/JBA 概率化洪水图与 NOAA FIM 精度评估的"结果是一个区间"表达:
设计暴雨外推(样本仅 5 年)不确定性大, 对设计暴雨量取 ±20% 敏感性包络,
分别用浴缸法反演水面高程, 得到每个重现期的 low/central/high 三套 W 与淹没面积。

输出: flood_out/uncertainty.json
  {"note": ..., "band_pct": 0.2, "scenarios": {"2": {"rain_low_mm":..., "W": {"low":..., "central":..., "high":...},
   "area_km2": {"low":..., "central":..., "high":...}, "max_depth_m": {...}}, ...}}

运行: python tools/uncertainty_bands.py   (依赖 dem/study_dtm.tif, 数秒级)
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proj_fix  # noqa: F401  PROJ 冲突修复(须在 import rasterio 之前, 同其他栅格脚本)
import numpy as np
import rasterio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline_config import (RUNOFF_COEF, DEPTH_THRESH, CELL_LAT, cell_area_m2,
                             RETURNS_FALLBACK as returns_fallback)  # 唯一权威常量
from flood_core import DepressionModel   # 多洼地浴缸法唯一实现(对标§四1, 替代本地副本)
DTM_PATH = os.path.join(ROOT, "dem", "study_dtm.tif")
STORM_JSON = os.path.join(ROOT, "flood_out", "design_storm_24h.json")
OUT_JSON = os.path.join(ROOT, "flood_out", "uncertainty.json")
BAND = 0.20          # 降雨敏感性 ±20%
BANDS = (1.0 - BAND, 1.0, 1.0 + BAND)
KEYS = ("low", "central", "high")


def main():
    if os.path.exists(STORM_JSON):
        returns = {int(k): float(v) for k, v in json.load(open(STORM_JSON, encoding="utf-8")).items()}
    else:
        returns = dict(returns_fallback)

    with rasterio.open(DTM_PATH) as src:
        z = src.read(1).astype("float32")
        transform = src.transform
    z[np.isnan(z)] = 0.0
    z = np.clip(z, -15.0, None)
    land = z > 0
    area_m2 = cell_area_m2(transform, CELL_LAT)
    model = DepressionModel(z)      # 填洼/洼地/汇水预计算一次, 三档复用

    out = {"note": "设计暴雨 ±20%% 降雨敏感性包络(多洼地独立水面同口径反演); 样本仅5年, 区间供参考",
           "band_pct": BAND, "scenarios": {}}
    for T in sorted(returns):
        R = returns[T]
        entry = {"rain_mm": R, "W": {}, "area_km2": {}, "max_depth_m": {}}
        for key, f in zip(KEYS, BANDS):
            q = R * f / 1000.0 * RUNOFF_COEF
            depth, finfo = model.flood(q, area_m2)
            flooded = (depth > DEPTH_THRESH) & land
            W = finfo["water_level_m"]
            entry["W"][key] = round(float(W), 2)
            entry["area_km2"][key] = round(float(flooded.sum() * area_m2) / 1e6, 3)
            entry["max_depth_m"][key] = round(float(depth[flooded].max()), 2) if flooded.any() else 0.0
            print("T=%3dy x%.2f -> W=%.2f area=%.3fkm2" % (T, f, W, entry["area_km2"][key]))
        out["scenarios"][str(T)] = entry

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved ->", OUT_JSON)


if __name__ == "__main__":
    main()

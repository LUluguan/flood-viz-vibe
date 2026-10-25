# -*- coding: utf-8 -*-
"""
pipeline_config.py — 全仓共用的研究区与算法常量(唯一权威定义)。

背景(2026-10 审计): 研究区 bbox 在 7 个文件各写一份、DEPTH_CAP/RUNOFF_COEF 等在
4+ 个文件重复, 改一处漏一处即产生口径分叉。本文件零第三方依赖(不 import numpy/rasterio,
cell_area_m2 只用 math + affine 属性), 可在任何模块任何时机安全导入。
"""
import math
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

# ===== 研究区(珠江新城/广州塔) =====
LON_MIN, LON_MAX = 113.30, 113.34
LAT_MIN, LAT_MAX = 23.09, 23.13
STUDY_BOUNDS = {"west": LON_MIN, "south": LAT_MIN, "east": LON_MAX, "north": LAT_MAX}
STUDY_WS = (LON_MIN, LAT_MIN, LON_MAX, LAT_MAX)    # west, south, east, north
STUDY_LL = (LON_MIN, LON_MAX, LAT_MIN, LAT_MAX)    # lon_min, lon_max, lat_min, lat_max

# ===== 浴缸法 / 色带 / 情景 =====
DEPTH_CAP = 6.0         # 水深色带上限(m), 超出按最深色
DEPTH_THRESH = 0.05     # 淹没判定阈值(m)
RUNOFF_COEF = 0.50      # 综合径流系数(高城市化, 可被请求参数覆盖)

# ===== 重现期档位(全仓唯一权威列表) + 24h 设计暴雨后备值 =====
RETURNS = [2, 5, 10, 50, 100]

# ===== 2/5/10/50/100 年 24h 设计暴雨后备值(prep_design_storm.py 产物缺失时) =====
RETURNS_FALLBACK = {2: 118.5, 5: 166.3, 10: 198.9, 50: 270.6, 100: 300.6}

# ===== 面积/距离换算(唯一权威; NUM-01: 纬度取研究区中心, 不同纬度取值差异 ≤0.7%) =====
M_PER_DEG_LON_EQUATOR = 111320.0   # 赤道每经度米数
M_PER_DEG_LAT = 110574.0           # 每纬度米数(中纬度近似)
CELL_LAT = 23.11                   # 面积换算固定纬度(研究区中心)


def cell_area_m2(transform, lat=CELL_LAT):
    """单像元实地面积(m²)。transform 为 affine 对象(a=经度像元宽, e=纬度像元高)。"""
    return (transform.a * M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))) * (
        abs(transform.e) * M_PER_DEG_LAT)


# ===== 水深分级直方分界(唯一权威; depth_hist/export_dashboard 共用, 下缘=淹没阈值) =====
DEPTH_BINS = ((DEPTH_THRESH, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 1e9))

# ===== 广州市暴雨强度公式(中心城区/一区, 2022-12 版; 对标§四4 设计暴雨标准化) =====
# 出处: 广州市水务局《关于公开广州市暴雨强度公式编制与设计暴雨雨型研究技术报告简本的公告》
# (2023-03-07, swj.gz.gov.cn/gkmlpt/content/8/8835/post_8835843.html);
# 参数转录自该公告技术报告汇总(短历时 5–180min)。q 单位 L/(s·ha), t=历时(min), P=重现期(年)。
# 总公式: q = 13290.630 × (1 + 0.607·lgP) / (t + 39.126)^0.956
GZ_IDF_TOTAL = {"A": 13290.630, "C": 0.607, "b": 39.126, "n": 0.956}
# 分重现期区间参数公式(精度优于总公式, 官方推荐查用):
#   P∈[1,10):  q = 167·A(P)/(t+b(P))^n(P),  A/P/b/n 如下
#   P∈[10,100]: 同式(Ⅲ区参数)
GZ_IDF_REGION_II = {   # P = 1–10 年
    "A": lambda P: 56.414 + 21.398 * math.log(P - 0.509),
    "b": lambda P: 24.898 + 4.496 * math.log(P - 0.836),
    "n": lambda P: 0.934 - 0.004 * math.log(P - 0.099),
}
GZ_IDF_REGION_III = {  # P = 10–100 年
    "A": lambda P: 65.165 + 21.450 * math.log(P - 4.527),
    "b": lambda P: 32.014 + 3.805 * math.log(P - 7.842),
    "n": lambda P: 0.921 - 0.002 * math.log(P - 0.103),
}


def gz_rainfall_intensity(t_min, return_period):
    """广州中心城区暴雨强度 i(mm/min); t∈[5,180], P∈[1,100]。
    用分区间参数公式(官方推荐), 短历时换算 i = q/167。"""
    P = float(return_period)
    if P < 1 or P > 100 or t_min <= 0:
        raise ValueError("P 须在 1-100 年, t>0")
    reg = GZ_IDF_REGION_II if P < 10 else GZ_IDF_REGION_III
    a = reg["A"](P)
    b = reg["b"](P)
    n = reg["n"](P)
    q = 167.0 * a / (t_min + b) ** n      # L/(s·ha)
    return q / 167.0                       # mm/min

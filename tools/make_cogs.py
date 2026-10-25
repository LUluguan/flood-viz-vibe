# -*- coding: utf-8 -*-
"""
make_cogs.py — 水深栅格转 COG(云优化 GeoTIFF, 对标§四8 互操作)。

对 flood_out/flood_depth_{T}y.tif 重写为 COG(内建金字塔 + 128×128 分块),
供 QGIS/ArcGIS/GDAL 经 HTTP 直读(/api/cog/{T} 端点), 亦为未来瓦片化/COG-baseline
的资产底座。运行: python tools/make_cogs.py(依赖已生成的 flood_depth tif)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proj_fix  # noqa: F401  PROJ 冲突修复(须在 import rasterio 之前)

import rasterio
import rasterio.shutil   # CreateCopy 路径(COG 驱动只支持复制创建, 不支持直接 Create)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "flood_out")
DST_DIR = os.path.join(ROOT, "flood_out", "cog")
OVERVIEWS = [2, 4, 8]


def cogify(src_path, dst_path):
    """单文件转 COG: 读源 → 写临时 GTiff → build overviews → 以 COG 驱动重写。"""
    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        data = src.read()
    tmp = dst_path + ".tmp.tif"
    profile.update(driver="GTiff", tiled=True, blockxsize=128, blockysize=128,
                   compress="deflate")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data)
        dst.build_overviews(OVERVIEWS, rasterio.enums.Resampling.nearest)
        dst.update_tags(ns="rio_overview", resampling="nearest")
    with rasterio.open(tmp) as src:
        rasterio.shutil.copy(src, dst_path, driver="COG",
                             compress="deflate", overviews="auto",
                             blocksize=128, overview_resampling="nearest")
    os.remove(tmp)
    return dst_path


def main():
    os.makedirs(DST_DIR, exist_ok=True)
    n = 0
    for T in (2, 5, 10, 50, 100):
        src = os.path.join(SRC_DIR, "flood_depth_%dy.tif" % T)
        dst = os.path.join(DST_DIR, "flood_depth_%dy_cog.tif" % T)
        if not os.path.exists(src):
            print("MISS", src)
            continue
        cogify(src, dst)
        n += 1
        print("COG ->", dst, "%.1f KB" % (os.path.getsize(dst) / 1024))
    print("done: %d 个 COG" % n)


if __name__ == "__main__":
    main()

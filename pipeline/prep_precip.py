# -*- coding: utf-8 -*-
"""
prep_precip.py — 将广东降雨 NetCDF(pre_YYYY.nc)裁剪到 DEM 覆盖范围并写出 12 波段 GeoTIFF。
输出: precip_tif/precip_YYYY.tif (12 波段 = 1-12 月, 单位 0.1mm, 值×0.1=mm, nodata=-32768)
用 tifffile 写出,确保 raster2pgsql/GDAL 可读。
"""
import os
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # pipeline/ 子目录 -> 仓库根
try:
    import netCDF4
except ImportError:
    netCDF4 = None  # 缺测转换纯函数可单测；仅运行实际NC预处理时要求该依赖。
import tifffile

LON_MIN, LON_MAX = 109.0, 119.0
LAT_MIN, LAT_MAX = 20.0, 26.0
NODATA = -32768


def to_int16_precip(values, missing_value=NODATA):
    """纯转换：先保留原值/MaskedArray 缺测信息，仅转换可表示的有效值。

    非有限值、缺测哨兵及 int16 越界值统一为 NODATA；有效小数仍按旧约定截断。
    不修改输入，也不让无效数值进入 astype，避免溢出/转换警告。
    """
    raw = np.asanyarray(values)
    data = np.ma.getdata(raw)
    invalid = np.ma.getmaskarray(raw) | ~np.isfinite(data)
    if missing_value is not None:
        invalid |= np.isin(data, missing_value)
    limits = np.iinfo(np.int16)
    invalid |= (data < limits.min) | (data > limits.max)
    out = np.full(data.shape, NODATA, dtype=np.int16)
    out[~invalid] = data[~invalid].astype(np.int16)
    return out


def write_geotiff(path, bands, geo):
    """bands: (nbands, nrows, ncols) int16. geo=(x0,dx,0,y0,0,dy)。"""
    x0, dx, _, y0, _, dy = geo
    extratags = [
        (33550, 'd', 3, (dx, abs(dy), 0.0)),                    # ModelPixelScale
        (33922, 'd', 6, (0, 0, 0, x0, y0, 0)),                  # ModelTiepoint
        (34735, 'H', 16, (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)),  # WGS84
        (42113, 's', 1, str(NODATA)),                           # GDAL_NODATA: ASCII 类型(旧版 'd' double 导致 GDAL 读不到 nodata)
    ]
    with tifffile.TiffWriter(path, bigtiff=False) as tw:
        tw.write(bands, photometric='minisblack', planarconfig='separate',
                 extratags=extratags)
    print("wrote %s  bands=%d %dx%d" % (path, bands.shape[0], bands.shape[2], bands.shape[1]))


def main():
    if netCDF4 is None:
        raise SystemExit("缺少 netCDF4：请在支持该库的Python环境安装后运行NC预处理；Web和其他管线不受影响")
    with netCDF4.Dataset(os.path.join(ROOT, "pre_2021.nc")) as ds:
        lon = ds.variables["lon"][:]
        lat = ds.variables["lat"][:]
    li = np.where((lon >= LON_MIN) & (lon <= LON_MAX))[0]
    ai = np.where((lat >= LAT_MIN) & (lat <= LAT_MAX))[0]
    x0 = lon[li[0]]; dx = lon[li[1]] - lon[li[0]]
    y0 = lat[ai[0]]; dy = lat[ai[1]] - lat[ai[0]]
    print("clip lon %d cols %.4f..%.4f | lat %d rows %.4f..%.4f" % (
        len(li), lon[li[0]], lon[li[-1]], len(ai), lat[ai[0]], lat[ai[-1]]))
    geo = (x0, dx, 0.0, y0, 0.0, dy)

    os.makedirs(os.path.join(ROOT, "precip_tif"), exist_ok=True)
    for year in [2021, 2022, 2023, 2024, 2025]:
        nc = os.path.join(ROOT, "pre_%d.nc" % year)
        if not os.path.exists(nc):
            print("skip", nc); continue
        with netCDF4.Dataset(nc) as ds:
            pv = ds.variables["pre"]
            mv = pv.missing_value if "missing_value" in pv.ncattrs() else NODATA
            sub = to_int16_precip(pv[:, ai, li], missing_value=mv)
        out = os.path.join(ROOT, "precip_tif", "precip_%d.tif" % year)
        write_geotiff(out, sub, geo)
        print("  year %d: min=%d max=%d nodata=%d" % (year, int(sub.min()), int(sub.max()), int((sub==NODATA).sum())))


if __name__ == "__main__":
    main()

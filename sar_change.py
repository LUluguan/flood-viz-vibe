# -*- coding: utf-8 -*-
"""sar_change.py — Sentinel-1 RTC VV 双时相变化检测: 后向散射骤降 = 洪水新增水面。"""
import numpy as np


def to_db(arr, linear_thresh=1.0):
    """线性功率 → dB(10*log10)；含负值的数据按已经是 dB 原样返回。
    判定依据: 功率域非负(S1 RTC gamma0 典型中位 ≈0.18), dB 域典型 -30..0 且含负值。
    这是基于 S1 RTC 的启发式, 并非所有非负 dB 数据都能自动识别；不用于线性幅度。
    linear_thresh 已弃用且不生效, 仅保留位置/关键字调用兼容；不再以中位数阈值区分单位。
    旧版以"中位数>1"判定线性, 对 RTC 功率永不触发 → drop_db 阈值失效、变化掩膜近似为空。"""
    v = arr[np.isfinite(arr)]
    if v.size == 0:
        return arr
    if float(np.nanmin(v)) < 0:
        return arr
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(np.clip(arr, 1e-8, None))


def change_mask(vv_flood, vv_base, drop_db=-8.0):
    """vv_flood 与 vv_base 同为 RTC VV(线性或dB)。返回 True=新增水面(后向散射骤降)。"""
    f = to_db(vv_flood.astype("float32"))
    b = to_db(vv_base.astype("float32"))
    valid = np.isfinite(f) & np.isfinite(b)
    diff = np.full(f.shape, np.nan, dtype="float32")
    diff[valid] = f[valid] - b[valid]
    return (diff < drop_db) & valid

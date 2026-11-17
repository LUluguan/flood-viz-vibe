# -*- coding: utf-8 -*-
"""
unet_metrics.py — UNet 水体提取精度评估(演示样本集)

参考 Google Flood Hub 公开方法论 / NOAA FIM 精度评估文档的做法:
把模型精度指标写成常驻 JSON, 前端(unet.html / dashboard.html)展示"模型精度卡"。

口径: 优先读取 unet_out/samples/ 下 *_pred_mask.npy / *_gt_mask.npy 原始二值掩膜。
旧产物缺原掩膜时才从 *_pred.png / *_gt.png 估算，并显式标记降级；非完整验证集。
逐样本再宏平均。指标: IoU(Jaccard) / F1(Dice) / Precision / Recall / 混淆四格。
输出: unet_out/eval_metrics.json

运行: python tools/unet_metrics.py
"""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "unet_out", "samples")
OUT_JSON = os.path.join(ROOT, "unet_out", "eval_metrics.json")


def decode(im, kind):
    """从演示样本渲染图解出水体掩膜: pred=蓝色覆盖[0,140,255], gt=绿色覆盖[0,255,140]。"""
    a = np.asarray(im.convert("RGB")).astype("int16")
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    if kind == "pred":
        return (r < 80) & (g > 90) & (g < 200) & (b > 200)
    return (r < 80) & (g > 200) & (b > 90) & (b < 200)


def main():
    stems = {p[:-len("_pred.png")] for p in glob.glob(os.path.join(SAMPLES, "*_pred.png"))}
    stems.update(p[:-len("_pred_mask.npy")] for p in glob.glob(os.path.join(SAMPLES, "*_pred_mask.npy")))
    per, tp_all = [], dict(tp=0, fp=0, fn=0, tn=0)
    for stem in sorted(stems):
        pp, gt_p = stem + "_pred.png", stem + "_gt.png"
        pred_raw, gt_raw = stem + "_pred_mask.npy", stem + "_gt_mask.npy"
        if os.path.exists(pred_raw) and os.path.exists(gt_raw):
            pred = np.load(pred_raw, allow_pickle=False)
            gt = np.load(gt_raw, allow_pickle=False)
            if pred.ndim != 2 or gt.ndim != 2 or pred.shape != gt.shape:
                raise ValueError("原始掩膜尺寸不一致: %s" % pp)
            if not np.isin(pred, (0, 1, 255)).all() or not np.isin(gt, (0, 1, 255)).all():
                raise ValueError("原始掩膜必须为二值: %s" % pp)
            pred, gt = pred > 0, gt > 0
            mask_source = "raw_masks"
        else:
            if not (os.path.exists(pp) and os.path.exists(gt_p)):
                print("跳过: %s 缺少成对原始掩膜及 RGB 图，无法评估" % os.path.basename(stem))
                continue
            print("降级: %s 缺少成对原始掩膜，RGB 颜色估算并非精确指标" % os.path.basename(pp))
            with Image.open(pp) as im:
                pred = decode(im, "pred")
            with Image.open(gt_p) as im:
                gt = decode(im, "gt")
            mask_source = "rgb_estimate"
        if pred.shape != gt.shape:
            raise ValueError("掩膜尺寸不一致: %s" % pp)
        tp = int((pred & gt).sum())
        fp = int((pred & ~gt).sum())
        fn = int((~pred & gt).sum())
        tn = int((~pred & ~gt).sum())
        iou = tp / (tp + fp + fn) if (tp + fp + fn) else None
        f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else None
        prec = tp / (tp + fp) if (tp + fp) else None
        rec = tp / (tp + fn) if (tp + fn) else None
        name = os.path.basename(pp).replace("_pred.png", "")
        per.append({"sample": name, "mask_source": mask_source,
                    "iou": round(iou, 4) if iou is not None else None,
                    "f1": round(f1, 4) if f1 is not None else None,
                    "precision": round(prec, 4) if prec is not None else None,
                    "recall": round(rec, 4) if rec is not None else None})
        for k, v in zip(("tp", "fp", "fn", "tn"), (tp, fp, fn, tn)):
            tp_all[k] += v
    n = len(per)
    def mean(key):
        vals = [p[key] for p in per if p[key] is not None]
        return round(float(np.mean(vals)), 4) if vals else None
    by_region = {}
    for pnt in per:
        reg = pnt["sample"].split("_")[0]
        vals = by_region.setdefault(reg, [])
        if pnt["iou"] is not None:
            vals.append(pnt["iou"])
    regions = {r: round(sum(v) / len(v), 4) if v else None for r, v in sorted(by_region.items())}
    sources = {p["mask_source"] for p in per}
    approximate = "rgb_estimate" in sources
    mask_source = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else "no_samples")
    out = {
        "dataset": "GF-FloodNet 导出演示样本(非完整测试集；划分来源见 unet_samples.js 元数据)",
        "n_samples": n,
        "mask_source": mask_source,
        "approximate": approximate,
        "by_region": regions,
        "macro": {"iou": mean("iou"), "f1": mean("f1"),
                  "precision": mean("precision"), "recall": mean("recall")},
        "confusion": tp_all,
        "per_sample": per,
        "note": ("指标在导出演示样本集上计算(非完整测试集); 原始掩膜由 export_unet_demo.py 同步导出。"
                 + ("本次无有效样本，未计算指标。" if not n else
                    "含缺少原始掩膜的降级样本：RGB 颜色估算可能把地物误判为水体，不是精确指标。"
                    if approximate else "本次按原始二值掩膜计算。")),
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("samples:", n, "macro IoU:", out["macro"]["iou"], "F1:", out["macro"]["f1"])
    print("saved ->", OUT_JSON)


if __name__ == "__main__":
    main()

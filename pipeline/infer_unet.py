# -*- coding: utf-8 -*-
"""
infer_unet.py — UNet 水体提取推理: 输入多光谱影像(5波段 .tif) -> 输出水体二值掩膜 PNG。
用法:
  python infer_unet.py --img <5波段.tif> --ckpt unet_out/unet_water.pt --out out_mask.png
"""
import argparse, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # pipeline/ 子目录 -> 仓库根
import numpy as np
import tifffile
from PIL import Image
import torch
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # pipeline/ 子目录: 仓库根入 path(共享模块 pipeline_config/proj_fix/...)
from unet_model import UNet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", required=True)
    ap.add_argument("--ckpt", default=os.path.join(ROOT, "unet_out", "unet_water.pt"))
    ap.add_argument("--out", default=os.path.join(ROOT, "unet_out", "infer_mask.png"))
    ap.add_argument("--size", type=int, default=256)
    args = ap.parse_args()
    if args.size <= 0 or args.size % 16:
        raise ValueError("size 必须是正的 16 倍数(UNet 含四级下采样)")

    ck = torch.load(args.ckpt, map_location="cpu", weights_only=True)
    mean = np.asarray(ck["mean"], dtype=np.float32)
    std = np.asarray(ck["std"], dtype=np.float32)
    if mean.shape != (5,) or not np.isfinite(mean).all():
        raise ValueError("checkpoint mean 必须是 5 个有限数值")
    if std.shape != (5,) or not np.isfinite(std).all() or (std <= 0).any():
        raise ValueError("checkpoint std 必须是 5 个有限正数")

    I = tifffile.imread(args.img).astype(np.float32)   # (H, W, 5)
    if I.ndim != 3 or I.shape[2] != 5:
        raise ValueError("输入影像必须为 (H, W, 5) 的 5 波段数据")
    h, w = I.shape[:2]
    if h != args.size or w != args.size:
        I = np.stack([np.array(Image.fromarray(I[..., b]).resize(
            (args.size, args.size), Image.Resampling.BILINEAR)) for b in range(5)], axis=2)
    X = I.transpose(2, 0, 1)                          # (5, H, W)
    for b in range(5):
        X[b] = (X[b] - mean[b]) / std[b]
    Xt = torch.from_numpy(X[None])

    model = UNet(5, 1, base=ck["base"]); model.load_state_dict(ck["state_dict"]); model.eval()
    with torch.no_grad():
        prob = torch.sigmoid(model(Xt))[0, 0].numpy()  # (H, W)
    mask = (prob > 0.5).astype(np.uint8) * 255

    outdir = os.path.dirname(args.out)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    Image.fromarray(mask, "L").save(args.out)
    print("water fraction = %.3f%%  -> %s" % (100.0 * mask.mean() / 255.0, args.out))


if __name__ == "__main__":
    main()

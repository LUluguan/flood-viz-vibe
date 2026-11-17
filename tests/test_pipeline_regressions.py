# -*- coding: utf-8 -*-
"""离线管线回归：仅用 tmp_path 合成输入/输出，不重训、不访问真实 PG。"""
import importlib.util
import json
from pathlib import Path
import runpy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest
import tifffile
import torch

from pipeline import eval_unet, export_unet_demo, infer_unet, load_flood_pg, train_unet
from tools import tile_flood, unet_metrics
import unet_apply
from unet_model import UNet

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkpoint(tmp_path):
    """小型真实 UNet 权重，仅保存未训练的确定性参数。"""
    model = UNet(5, 1, base=1)
    with torch.no_grad():
        for param in model.parameters():
            param.zero_()
        model.out.bias.fill_(1)
    ck = dict(state_dict=model.state_dict(), mean=list(range(5)), std=[2.0] * 5,
              base=1, size=32, meta={"val_scenes": ["China_001"]})
    path = tmp_path / "model.pt"
    torch.save(ck, path)
    return path, ck


def write_pair(tmp_path, shape=(32, 32), name="China_001_0_0"):
    data = tmp_path / "data"
    for folder in ("images", "annotations"):
        (data / folder).mkdir(parents=True, exist_ok=True)
    image = np.zeros((*shape, 5), dtype=np.uint16)
    image[:] = np.arange(5) + 10
    label = np.full(shape, 255, dtype=np.uint8)
    label[:, :shape[1] // 2] = 0
    ip = data / "images" / (name + ".tif")
    ap = data / "annotations" / (name + ".tif")
    tifffile.imwrite(ip, image, photometric="minisblack")
    tifffile.imwrite(ap, label)
    return data, ip, ap


def set_infer_args(monkeypatch, ip, ckpt, out, size=256):
    monkeypatch.setattr(sys, "argv", ["infer_unet", "--img", str(ip), "--ckpt", str(ckpt),
                                     "--out", str(out), "--size", str(size)])


@pytest.mark.parametrize("shape,relative", [((256, 256), False), ((128, 320), True)])
def test_infer_native_and_nonsquare_output(tmp_path, monkeypatch, checkpoint, shape, relative):
    _, ip, _ = write_pair(tmp_path, shape)
    out = "mask.png" if relative else tmp_path / "nested" / "mask.png"
    monkeypatch.chdir(tmp_path)
    set_infer_args(monkeypatch, ip, checkpoint[0], out)
    seen = []
    model = UNet(5, 1, base=1)
    model.register_forward_pre_hook(lambda _, args: seen.append(args[0].clone()))
    monkeypatch.setattr(infer_unet, "UNet", lambda *a, **k: model)
    infer_unet.main()
    with Image.open(out) as im:
        assert im.size == (256, 256)
        assert im.mode == "L"
        assert np.all(np.asarray(im) == 255)
    assert seen[0].shape == (1, 5, 256, 256)
    assert torch.all(seen[0] == 5)  # 每波段 (10+b-b)/2


def test_infer_load_is_explicitly_weights_only(tmp_path, monkeypatch, checkpoint):
    _, ip, _ = write_pair(tmp_path, (16, 24))
    set_infer_args(monkeypatch, ip, checkpoint[0], tmp_path / "mask.png", size=32)
    real_load = torch.load

    def checked_load(*args, **kwargs):
        assert kwargs.get("weights_only") is True
        return real_load(*args, **kwargs)

    monkeypatch.setattr(torch, "load", checked_load)
    infer_unet.main()


@pytest.mark.parametrize("std", [[0] * 5, [-1] * 5, [float("nan")] * 5,
                                 [float("inf")] * 5, [1] * 4])
def test_infer_rejects_invalid_std(tmp_path, monkeypatch, checkpoint, std):
    path, ck = checkpoint
    ck["std"] = std
    torch.save(ck, path)
    _, ip, _ = write_pair(tmp_path, (16, 24))
    out = tmp_path / "mask.png"
    set_infer_args(monkeypatch, ip, path, out, size=32)
    with pytest.raises(ValueError, match="std"):
        infer_unet.main()
    assert not out.exists()


@pytest.mark.parametrize("shape", [(32, 32), (32, 32, 4)])
def test_infer_rejects_wrong_band_shape(tmp_path, monkeypatch, checkpoint, shape):
    ip = tmp_path / "bad.tif"
    tifffile.imwrite(ip, np.zeros(shape, dtype=np.uint16), photometric="minisblack")
    set_infer_args(monkeypatch, ip, checkpoint[0], tmp_path / "mask.png", size=32)
    with pytest.raises(ValueError, match="5"):
        infer_unet.main()


@pytest.mark.parametrize("water_label", [0, 1])
def test_preload_nearest_labels_do_not_expand(tmp_path, water_label):
    _, ip, ap = write_pair(tmp_path, (4, 4))
    labels = np.full((4, 4), 255, dtype=np.uint8)
    labels[1, 1] = water_label
    tifffile.imwrite(ap, labels)
    _, masks = train_unet.preload([(str(ip), str(ap))], 8)
    expected = np.zeros((8, 8), dtype=np.float32)
    expected[2:4, 2:4] = 1
    np.testing.assert_array_equal(masks[0, 0], expected)


def test_preload_checks_width_and_uses_bilinear_image(tmp_path):
    _, ip, ap = write_pair(tmp_path, (4, 2))
    image = np.zeros((4, 2, 5), dtype=np.uint16)
    image[:, 1] = 100
    tifffile.imwrite(ip, image, photometric="minisblack")
    imgs, masks = train_unet.preload([(str(ip), str(ap))], 4)
    assert imgs.shape == (1, 5, 4, 4)
    assert masks.shape == (1, 1, 4, 4)
    np.testing.assert_allclose(imgs[0, 0, 0], [0, 25, 75, 100])
    np.testing.assert_array_equal(masks[0, 0, 0], [1, 1, 0, 0])


def test_eval_posix_paths_and_nonsquare_images(tmp_path, monkeypatch, checkpoint, capsys):
    data, _, ap = write_pair(tmp_path, (32, 64))
    monkeypatch.setattr(eval_unet, "DATA", str(data))
    monkeypatch.setattr(eval_unet, "CKPT", str(checkpoint[0]))
    monkeypatch.setattr(eval_unet, "OUT", str(tmp_path))
    monkeypatch.setattr(eval_unet.glob, "glob", lambda _: [ap.as_posix()])
    eval_unet.main(n=1)
    assert "IoU = 0.5000" in capsys.readouterr().out
    with Image.open(tmp_path / "demo_pred.png") as im:
        assert im.size == (32, 32)


@pytest.mark.parametrize("n", [1, 0, -1])
def test_eval_rejects_empty_validation(tmp_path, monkeypatch, checkpoint, capsys, n):
    monkeypatch.setattr(eval_unet, "DATA", str(tmp_path))
    monkeypatch.setattr(eval_unet, "CKPT", str(checkpoint[0]))
    monkeypatch.setattr(eval_unet, "OUT", str(tmp_path))
    with pytest.raises(ValueError):
        eval_unet.main(n=n)
    assert "IoU" not in capsys.readouterr().out
    assert not list(tmp_path.glob("demo_*.png"))


def load_precip(monkeypatch):
    # 转换逻辑只需 numpy；不要求本机安装 netCDF4，也不打开 .nc。
    monkeypatch.setitem(sys.modules, "netCDF4", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("precip_regression", ROOT / "pipeline/prep_precip.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_precip_masks_original_values_before_int16_conversion(monkeypatch):
    mod = load_precip(monkeypatch)
    raw = np.ma.array([12.9, -9999.9, 1e30, np.nan, np.inf, -np.inf, 5, 40000, -40000],
                      mask=[False] * 6 + [True, False, False])
    original = raw.copy()
    with np.errstate(all="raise"):
        result = mod.to_int16_precip(raw, missing_value=[-9999.9, 1e30])
    assert result.dtype == np.int16
    np.testing.assert_array_equal(result, [12] + [-32768] * 8)
    np.testing.assert_array_equal(raw.data, original.data)
    np.testing.assert_array_equal(raw.mask, original.mask)


def test_precip_normal_values_and_no_hardcoded_path(monkeypatch):
    before = list(sys.path)
    mod = load_precip(monkeypatch)
    assert sys.path == before
    result = mod.to_int16_precip(np.array([[0, 1.9, 32767, -32768]]))
    np.testing.assert_array_equal(result, [[0, 1, 32767, -32768]])


def test_precip_missing_dependency_keeps_actionable_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "netCDF4", None)
    with pytest.raises(SystemExit, match="缺少 netCDF4"):
        runpy.run_path(str(ROOT / "pipeline/prep_precip.py"), run_name="__main__")


def test_real_event_shares_colors_but_keeps_finite_alpha(monkeypatch):
    from pipeline import realevent_beijiang as event
    from flood_render import colorize_depth
    from pipeline_config import DEPTH_CAP, DEPTH_THRESH

    def changed_palette(depth, land=None):
        rgba = colorize_depth(depth, land)
        rgba[..., 0] = 17
        return rgba

    monkeypatch.setattr(event, "colorize_depth", changed_palette, raising=False)
    depth = np.array([[np.nan, np.inf, -np.inf, -1, 0, DEPTH_THRESH,
                       DEPTH_THRESH + 0.01, DEPTH_CAP, DEPTH_CAP * 2]], dtype=np.float32)
    with np.errstate(all="raise"):
        rgba = event.depth_rgba(depth)
    assert np.all(rgba[..., 0] == 17), "事件图必须跟随共享色带变更"
    np.testing.assert_array_equal(rgba[0, :, 3], [0, 0, 0, 0, 0, 0, 200, 200, 200])


@pytest.mark.parametrize("returns", [(2, 5), (3, 7)])
def test_tile_loads_dtm_and_converts_land_once(tmp_path, monkeypatch, returns):
    import rasterio
    from rasterio import Affine

    (tmp_path / "dem").mkdir()
    (tmp_path / "flood_out").mkdir()
    dtm = tmp_path / "dem/study_dtm.tif"
    transform = Affine(90, 0, -180, 0, -42.5, 85)
    for path, value in [(dtm, 1)] + [(tmp_path / ("flood_out/flood_depth_%dy.tif" % t), 2) for t in returns]:
        with rasterio.open(path, "w", driver="GTiff", width=4, height=4, count=1,
                           dtype="float32", crs="EPSG:4326", transform=transform) as dst:
            dst.write(np.full((4, 4), value, dtype="float32"), 1)
    out = tmp_path / "tiles"
    monkeypatch.setattr(tile_flood, "ROOT", str(tmp_path))
    monkeypatch.setattr(tile_flood, "OUTBASE", str(out))
    monkeypatch.setattr(tile_flood, "RETURNS", returns, raising=False)
    monkeypatch.setattr(tile_flood, "ZOOMS", [1])
    monkeypatch.setattr(tile_flood, "BOUNDS", (-1, -1, 1, 1))
    monkeypatch.setattr(tile_flood, "TILE", 4)
    real_open, real_sample = rasterio.open, tile_flood.sample_tile
    reads, land_arrays = [], []

    def tracked_open(path, *args, **kwargs):
        reads.append(Path(path))
        return real_open(path, *args, **kwargs)

    def tracked_sample(src, *args):
        if np.all(src == 1):
            land_arrays.append(src)
        return real_sample(src, *args)

    monkeypatch.setattr(rasterio, "open", tracked_open)
    monkeypatch.setattr(tile_flood, "sample_tile", tracked_sample)
    tile_flood.main()
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert set(meta["scenarios"]) == {str(t) for t in returns}
    assert reads.count(dtm) == 1
    assert len(land_arrays) == 8
    assert all(a is land_arrays[0] for a in land_arrays)
    assert land_arrays[0].dtype == np.float32
    assert len(list(out.rglob("*.png"))) == 8
    for path in out.rglob("*.png"):
        with Image.open(path) as im:
            assert np.all(np.asarray(im)[..., 3] == 200)


def test_tile_without_scenarios_does_not_require_dtm(tmp_path, monkeypatch):
    out = tmp_path / "tiles"
    monkeypatch.setattr(tile_flood, "ROOT", str(tmp_path))
    monkeypatch.setattr(tile_flood, "OUTBASE", str(out))
    tile_flood.main()
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["scenarios"] == {}
    assert not list(out.rglob("*.png"))


@pytest.mark.parametrize("value,expected", [(None, "NULL"), (True, "TRUE"), (2, "2"),
                                           (1.5, "1.5"), ("O'Reilly\\河", "E'O''Reilly\\\\河'")])
def test_offline_sql_literal(value, expected):
    assert load_flood_pg.sql_literal(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "bad\x00text", {}])
def test_offline_sql_rejects_unrepresentable_literals(value):
    with pytest.raises((TypeError, ValueError)):
        load_flood_pg.sql_literal(value)


def test_offline_sql_quotes_scenario_and_geojson(tmp_path, monkeypatch):
    folder = tmp_path / "flood_out"
    folder.mkdir()
    fields = ["return_period_y", "rain_mm", "runoff_depth_m", "water_level_m", "max_depth_m",
              "mean_depth_m", "flooded_area_km2", "flooded_cells", "river_cells"]
    scene = dict.fromkeys(fields, None)
    scene.update(return_period_y=2, rain_mm="0'); DROP TABLE flood_scenarios; --")
    scen = folder / "scenarios.json"
    scen.write_text(json.dumps([scene]), encoding="utf-8")
    geometry = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0]]],
                "name": "O'Reilly\\河"}
    (folder / "flood_extent_2y.geojson").write_text(json.dumps({"features": [{"geometry": geometry}]}), encoding="utf-8")
    out = folder / "result.sql"
    monkeypatch.setattr(load_flood_pg, "ROOT", str(tmp_path))
    monkeypatch.setattr(load_flood_pg, "SCEN", str(scen))
    monkeypatch.setattr(load_flood_pg, "OUT", str(out))
    monkeypatch.setattr(load_flood_pg, "RETURNS", [2])
    load_flood_pg.main()
    sql = out.read_text(encoding="utf-8")
    assert "VALUES (2, E'0''); DROP TABLE flood_scenarios; --', NULL, NULL, NULL, NULL, NULL, NULL, NULL);" in sql
    assert "ST_GeomFromGeoJSON(E'" in sql
    assert "O''Reilly" in sql
    assert "None" not in sql


def test_model_cache_concurrent_first_load_once(tmp_path, monkeypatch, checkpoint):
    monkeypatch.setattr(unet_apply, "_model_cache", None)
    monkeypatch.setattr(unet_apply, "_ckpt_path_used", None)
    start = threading.Barrier(10)
    loading = threading.Barrier(10)
    calls = []
    real_load = torch.load

    def load(*args, **kwargs):
        calls.append(args[0])
        # 无锁实现中所有 loader 会会合；加锁后只有首个 loader 等到超时。
        try:
            loading.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return real_load(*args, **kwargs)

    def worker():
        start.wait(timeout=5)
        return unet_apply.load_model(str(checkpoint[0]))

    monkeypatch.setattr(torch, "load", load)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: worker(), range(10)))
    assert len(calls) == 1
    assert all(result is results[0] for result in results)
    assert not results[0][1].training


def test_model_cache_failure_retry_and_path_change(tmp_path, monkeypatch, checkpoint):
    monkeypatch.setattr(unet_apply, "_model_cache", None)
    monkeypatch.setattr(unet_apply, "_ckpt_path_used", None)
    with pytest.raises(FileNotFoundError):
        unet_apply.load_model(str(tmp_path / "missing.pt"))
    first = unet_apply.load_model(str(checkpoint[0]))
    other = tmp_path / "other.pt"
    torch.save(checkpoint[1], other)
    second = unet_apply.load_model(str(other))
    assert second is not first
    assert unet_apply.load_model(str(other)) is second


def test_invert_depth_does_not_require_pipeline_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p).name.lower() != "pipeline"])
    monkeypatch.delitem(sys.modules, "water_level_inversion", raising=False)
    mask = np.ones((2, 2), dtype=bool)
    level, depth = unet_apply.invert_depth(mask, np.array([[1, 2], [3, 4]], dtype=np.float32))
    assert level == 2.5
    np.testing.assert_array_equal(depth, [[1.5, 0.5], [0, 0]])


@pytest.mark.parametrize("shape", [(32, 32), (32, 64)])
def test_export_demo_saves_raw_masks(tmp_path, monkeypatch, checkpoint, shape):
    data, _, _ = write_pair(tmp_path, shape)
    (tmp_path / "web/data").mkdir(parents=True)
    out = tmp_path / "samples"
    monkeypatch.setattr(export_unet_demo, "ROOT", str(tmp_path))
    monkeypatch.setattr(export_unet_demo, "DATA", str(data))
    monkeypatch.setattr(export_unet_demo, "OUT", str(out))
    monkeypatch.setattr(export_unet_demo, "CKPT", str(checkpoint[0]))
    export_unet_demo.main(n=1)
    pred = np.load(out / "China_001_0_0_pred_mask.npy", allow_pickle=False)
    gt = np.load(out / "China_001_0_0_gt_mask.npy", allow_pickle=False)
    assert pred.shape == gt.shape == (32, 32)
    assert np.all(pred == 1)
    assert np.all(gt[:, :16] == 1) and np.all(gt[:, 16:] == 0)


def write_metric_sample(folder, raw=True):
    # RGB 地物恰好命中覆盖色，不能用于精确掩膜恢复。
    Image.fromarray(np.tile(np.uint8([0, 140, 255]), (2, 2, 1))).save(folder / "China_001_pred.png")
    Image.fromarray(np.tile(np.uint8([0, 255, 140]), (2, 2, 1))).save(folder / "China_001_gt.png")
    if raw:
        np.save(folder / "China_001_pred_mask.npy", np.array([[1, 0], [0, 0]], dtype=np.uint8))
        np.save(folder / "China_001_gt_mask.npy", np.array([[1, 1], [0, 0]], dtype=np.uint8))


def test_metrics_prefers_exact_raw_masks(tmp_path, monkeypatch):
    write_metric_sample(tmp_path)
    out = tmp_path / "metrics.json"
    monkeypatch.setattr(unet_metrics, "SAMPLES", str(tmp_path))
    monkeypatch.setattr(unet_metrics, "OUT_JSON", str(out))
    unet_metrics.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["confusion"] == dict(tp=1, fp=0, fn=1, tn=2)
    assert data["macro"]["iou"] == 0.5
    assert data["mask_source"] == "raw_masks"
    assert data["approximate"] is False


@pytest.mark.parametrize("partial", [False, True])
def test_metrics_missing_raw_masks_marks_approximation(tmp_path, monkeypatch, capsys, partial):
    write_metric_sample(tmp_path, raw=False)
    if partial:
        np.save(tmp_path / "China_001_pred_mask.npy", np.zeros((2, 2), dtype=np.uint8))
    out = tmp_path / "metrics.json"
    monkeypatch.setattr(unet_metrics, "SAMPLES", str(tmp_path))
    monkeypatch.setattr(unet_metrics, "OUT_JSON", str(out))
    unet_metrics.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["approximate"] is True
    assert data["mask_source"] == "rgb_estimate"
    assert data["per_sample"][0]["mask_source"] == "rgb_estimate"
    assert "估算" in data["note"] and "降级" in capsys.readouterr().out


@pytest.mark.parametrize("size", [0, 17])
def test_infer_rejects_incompatible_model_size(tmp_path, monkeypatch, checkpoint, size):
    _, ip, _ = write_pair(tmp_path)
    set_infer_args(monkeypatch, ip, checkpoint[0], tmp_path / "mask.png", size=size)
    with pytest.raises(ValueError, match="size"):
        infer_unet.main()


def test_metrics_raw_masks_work_without_rendered_png(tmp_path, monkeypatch):
    np.save(tmp_path / "China_001_pred_mask.npy", np.array([[1, 0]], dtype=np.uint8))
    np.save(tmp_path / "China_001_gt_mask.npy", np.array([[1, 1]], dtype=np.uint8))
    out = tmp_path / "metrics.json"
    monkeypatch.setattr(unet_metrics, "SAMPLES", str(tmp_path))
    monkeypatch.setattr(unet_metrics, "OUT_JSON", str(out))
    unet_metrics.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["n_samples"] == 1
    assert data["macro"]["iou"] == 0.5
    assert data["mask_source"] == "raw_masks"


def test_metrics_undefined_region_iou_is_not_zero(tmp_path, monkeypatch):
    write_metric_sample(tmp_path)
    for kind in ("pred", "gt"):
        np.save(tmp_path / ("China_001_%s_mask.npy" % kind), np.zeros((2, 2), dtype=np.uint8))
    out = tmp_path / "metrics.json"
    monkeypatch.setattr(unet_metrics, "SAMPLES", str(tmp_path))
    monkeypatch.setattr(unet_metrics, "OUT_JSON", str(out))
    unet_metrics.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["macro"]["iou"] is None
    assert data["by_region"]["China"] is None


def test_metrics_no_samples_does_not_claim_raw_evaluation(tmp_path, monkeypatch):
    out = tmp_path / "metrics.json"
    monkeypatch.setattr(unet_metrics, "SAMPLES", str(tmp_path))
    monkeypatch.setattr(unet_metrics, "OUT_JSON", str(out))
    unet_metrics.main()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mask_source"] == "no_samples"
    assert data["macro"]["iou"] is None
    assert "未计算" in data["note"]


def test_metrics_rejects_misaligned_raw_masks(tmp_path, monkeypatch):
    write_metric_sample(tmp_path)
    np.save(tmp_path / "China_001_gt_mask.npy", np.ones((3, 2), dtype=np.uint8))
    monkeypatch.setattr(unet_metrics, "SAMPLES", str(tmp_path))
    monkeypatch.setattr(unet_metrics, "OUT_JSON", str(tmp_path / "metrics.json"))
    with pytest.raises(ValueError, match="shape|尺寸"):
        unet_metrics.main()
    assert not (tmp_path / "metrics.json").exists()

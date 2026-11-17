# -*- coding: utf-8 -*-
"""Read-only guards for real deliveries; imports/builds run in tmp_path only.

The legacy ZIP timestamp failure remains a real delivery blocker, not an xfail.
CI has no historical copy: build a temporary copy there, never a real release.
"""
import os
import sys
import importlib.util
from pathlib import Path

import pytest

from tools.sync_delivery import build_manifest, check_delivery, forbidden_entries, sync_delivery
from tools.package_delivery import validate_package

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate_stores():
    # No app/store import in the parent process. Subprocesses use disposable copies.
    yield


@pytest.fixture
def delivery_dir(tmp_path):
    historical = Path(ROOT) / "C2132_开发源码"
    if historical.is_dir():
        return str(historical)
    destination = tmp_path / "delivery"
    sync_delivery(ROOT, destination)
    return str(destination)


@pytest.fixture
def isolated_delivery(delivery_dir, tmp_path):
    # Even old store.py versions that ignore FLOOD_DATA_DIR may only write to tmp.
    destination = tmp_path / "importable"
    sync_delivery(delivery_dir, destination, build_manifest(delivery_dir, tracked_files=[]))
    return str(destination)


def _subprocess_env(copy_dir, state):
    return dict(os.environ, PYTHONPATH=copy_dir, PYTHONDONTWRITEBYTECODE="1",
                FLOOD_DATA_DIR=str(state), FLOOD_DEMO="0", FLOOD_ADMIN_PASSWORD="",
                FLOOD_DB_PASSWORD="", LLM_API_KEY="", WECOM_WEBHOOK_URL="",
                SMTP_HOST="", SMTP_PASSWORD="")


def test_deploy_files():
    for f in ["setup.bat", "run.bat", "Dockerfile", "docker-compose.yml", ".dockerignore"]:
        assert os.path.exists(os.path.join(ROOT, f)), "缺部署文件 %s" % f
    req = Path(ROOT, "requirements-runtime.txt").read_text(encoding="utf-8")
    assert "requests" in req, "requirements 缺 requests"
    assert "torchvision" not in req, "requirements 不应含 torchvision"
    # python-multipart: UploadFile 路由注册的硬依赖, 缺失则 app 导入即崩(2026-10 审计 P0)
    assert "python-multipart" in req, "requirements 缺 python-multipart"
    if importlib.util.find_spec("multipart") is None and importlib.util.find_spec("python_multipart") is None:
        print("WARN: 本机未安装 python-multipart(requirements 已声明, setup 时会装上)")
    run = open(os.path.join(ROOT, "run.bat"), encoding="utf-8").read()
    assert "FLOOD_PORT" in run, "run.bat 应支持 FLOOD_PORT"
    dk = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    assert "uvicorn" in dk and "EXPOSE 8001" in dk, "Dockerfile 应含启动与端口"
    assert "fonts-wqy-microhei" in dk, "Dockerfile 应安装中文字体(专题图制图需要)"
    print("部署文件清单 OK(含 python-multipart 与中文字体)")


def test_delivery_copy_importable(isolated_delivery, tmp_path):
    """交付副本必须能独立 import app(2026-10 审计 P0: store.py 漏同步)。"""
    import subprocess
    copy_dir = isolated_delivery
    r = subprocess.run([sys.executable, "-c", "import app"],
                       capture_output=True, cwd=copy_dir, timeout=60,
                       env=_subprocess_env(copy_dir, tmp_path / "state"))
    assert r.returncode == 0, "交付副本 import app 失败: %s" % r.stderr.decode("utf-8", "replace")[:200]
    print("交付副本 import app OK")


def test_delivery_copy_no_missing_local_imports(delivery_dir):
    """解析 app.py 的 from X import / import X 本地模块名, 断言副本里 X.py 存在。"""
    copy_dir = delivery_dir
    copy_app = os.path.join(copy_dir, "app.py")
    src = open(copy_app, encoding="utf-8").read()
    local = set()
    for m in __import__("re").finditer(r"from (\w+) import|import (\w+)", src):
        mod = m.group(1) or m.group(2)
        if os.path.exists(os.path.join(ROOT, mod + ".py")):
            local.add(mod)
    for mod in sorted(local):
        assert os.path.exists(os.path.join(copy_dir, mod + ".py")), \
            "交付副本缺 %s.py (app.py 依赖它)" % mod
    print("本地依赖完整性 OK:", sorted(local))


def test_delivery_copy_no_credentials(delivery_dir):
    """Reject nested secrets too; never clean or print their contents."""
    denied = forbidden_entries(delivery_dir)
    assert not denied, f"交付副本含禁止路径，请人工确认或使用新目录（未删除）: {denied}"


def test_delivery_copy_serves_data(isolated_delivery, tmp_path):
    """MISS-02(2026-10 审计 DEL-02): 副本必须能出数而不只 import——
    旧版 10 个核心端点 6 个 404(数据在平级的 C2132_相关数据 目录, 未随代码走)。"""
    import subprocess
    copy_dir = isolated_delivery
    if not os.path.exists(os.path.join(ROOT, "flood_out", "scenarios.json")):
        pytest.skip("根目录无情景数据(未同步), 副本出数无从验证")
    code = ("import app;from fastapi.testclient import TestClient;"
            "c=TestClient(app.app);r=c.get('/api/scenarios');print(r.status_code)")
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, cwd=copy_dir, timeout=60,
                       env=_subprocess_env(copy_dir, tmp_path / "state"))
    assert r.returncode == 0, "副本内起服务失败: %s" % r.stderr.decode("utf-8", "replace")[:300]
    assert r.stdout.strip().endswith(b"200"), \
        "交付副本缺运行数据(/api/scenarios=%s): 先运行 tools/sync_delivery.py" % \
        r.stdout.strip().decode("utf-8", "replace")
    print("交付副本 /api/scenarios 200 OK(可独立出数)")


def test_delivery_copy_tracked_complete(delivery_dir):
    """Allowlisted tracked/new/runtime files must match SHA-256, not just exist."""
    differences = check_delivery(ROOT, delivery_dir)
    assert not any(differences.values()), f"交付副本未同步或含禁止路径（只读检查）: {differences}"


def test_delivery_zip_not_stale():
    """MISS-03(2026-10 审计 DEL-03): 作品 zip 的修改时间必须晚于最后一次提交。
    旧版 zip 落后 7 周且为分卷包(Windows 资源管理器双击必失败), 防止旧包随交。"""
    zp = os.path.join(ROOT, "C2132_作品.zip")
    if not os.path.exists(zp):
        pytest.skip("C2132_作品.zip 不存在(提交前需重打单一 zip)")
    import subprocess
    r = subprocess.run(["git", "log", "-1", "--format=%ct"], capture_output=True,
                       cwd=ROOT)
    if r.returncode != 0:
        pytest.skip("非 git 环境, 跳过 zip 新鲜度检查")
    head_ts = int(r.stdout.strip() or 0)
    zip_ts = int(os.path.getmtime(zp))
    assert zip_ts >= head_ts, (
        "C2132_作品.zip 落后于仓库(打包于 %s, 最后提交于 %s)——"
        "提交前必须重打包(单一 zip, 不含 web/config.local.js 与 web_users.json, 附数据摆放说明)" % (
            __import__("datetime").datetime.fromtimestamp(zip_ts).strftime("%Y-%m-%d %H:%M"),
            __import__("datetime").datetime.fromtimestamp(head_ts).strftime("%Y-%m-%d %H:%M")))


def test_delivery_zip_integrity():
    """Changing mtime cannot turn a split/corrupt ZIP into a valid delivery."""
    path = Path(ROOT) / "C2132_作品.zip"
    if not path.exists():
        pytest.skip("C2132_作品.zip 不存在(正式交付前必须生成并验证新包)")
    assert validate_package(path) > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""Delivery builders only run on tmp_path; never import app or touch real deliveries."""
import ast
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_stores():
    # Override the business autouse fixture: these tests do not import app/store.
    yield


def _load_tool(name):
    path = ROOT / "tools" / (name + ".py")
    assert path.is_file(), f"missing delivery builder: {name}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert any(isinstance(node, ast.If) and "__name__" in ast.unparse(node.test)
               for node in tree.body), "delivery tools must have a main guard before safe import"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_has_main_guard_before_import():
    _load_tool("sync_delivery")


@pytest.fixture
def sync():
    return _load_tool("sync_delivery")


@pytest.fixture
def pack():
    return _load_tool("package_delivery")


def _put(root, rel, content=b"sample"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for rel in ("app.py", "new_first_party.py", "pipeline/new_stage.py",
                "deploy/watchdog.bat", "tools/helper.py", "tests/test_small.py",
                "docs/部署说明.md", "web/echarts.min.js", "web/config.example.js",
                "web/data/flood_depth_100y.png", "flood_out/flood_tiles/9/2/3.png",
                "flood_out/scenarios.json", "dem/study_dtm.tif", "unet_out/best.pt",
                "requirements-runtime.txt", ".env.example"):
        _put(root, rel)
    return root


BAD_PATHS = (
    ".env", "web/nested/.env.prod", "web/config.local.js", "web/config.local.js.bak",
    "web/data/web_users.json", "web/data/reports/user.json", "web/data/state/auth.json",
    "web/data/secrets/key.json", "pipeline/credentials/private.py", "web/.git/config",
    "web/data/.cache/map.png", "tools/__pycache__/x.pyc", "web/.claude/config.json",
    "web/data/private/key.json", "web/data/id_rsa", "web/data/server.pem",
    "flood_out/reports/person.png", "unet_out/_cache.npz", "GF-FloodNet/image.tif",
    "C2132_开发源码/app.py", "C2132_作品.zip", "misc/tracked_secret.txt",
)


def test_import_is_read_only_including_cwd(sync, pack, monkeypatch):
    before = Path.cwd()
    def forbidden(*args, **kwargs):
        pytest.fail("import performed filesystem/process work")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "chdir", forbidden)
    _load_tool("sync_delivery")
    _load_tool("package_delivery")
    assert Path.cwd() == before


def test_manifest_whitelist_has_runtime_tiles_and_new_modules(sync, source):
    for rel in BAD_PATHS:
        _put(source, rel, b"private")
    tracked = [*BAD_PATHS, "docs/部署说明.md"]
    manifest = sync.build_manifest(source, tracked_files=tracked)
    assert set(manifest) == {
        "app.py", "new_first_party.py", "pipeline/new_stage.py", "deploy/watchdog.bat",
        "tools/helper.py", "tests/test_small.py", "docs/部署说明.md", "web/echarts.min.js",
        "web/config.example.js", "web/data/flood_depth_100y.png",
        "flood_out/flood_tiles/9/2/3.png", "flood_out/scenarios.json", "dem/study_dtm.tif",
        "unet_out/best.pt", "requirements-runtime.txt", ".env.example",
    }
    assert manifest["app.py"] == hashlib.sha256(b"sample").hexdigest()


def test_git_unicode_is_nul_delimited_utf8(sync, source):
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "add", "docs/部署说明.md"], check=True)
    assert sync.git_files(source) == ["docs/部署说明.md"]


@pytest.mark.parametrize("rel", ["../app.py", "/app.py", "C:/app.py", "web/../app.py",
                                  "web\\config.local.js", "web/data/bad\nname.json"])
def test_manifest_rejects_unsafe_supplied_paths(sync, source, rel):
    with pytest.raises(ValueError):
        sync.build_manifest(source, tracked_files=[rel])


def test_check_is_read_only_and_detects_content_not_mtime(sync, source, tmp_path):
    dest = tmp_path / "delivery"
    manifest = sync.build_manifest(source, tracked_files=[])
    differences = sync.check_delivery(source, dest, manifest)
    assert differences["missing"] and not dest.exists()
    sync.sync_delivery(source, dest, manifest)
    assert not any(sync.check_delivery(source, dest, manifest).values())
    _put(dest, "app.py", b"changed")
    os.utime(dest / "app.py", ns=(source.joinpath("app.py").stat().st_atime_ns,
                                  source.joinpath("app.py").stat().st_mtime_ns))
    before = (dest / "app.py").stat().st_mtime_ns
    assert sync.check_delivery(source, dest, manifest)["changed"] == ["app.py"]
    assert (dest / "app.py").read_bytes() == b"changed"
    assert (dest / "app.py").stat().st_mtime_ns == before


def test_sync_refuses_legacy_credentials_without_deleting(sync, source, tmp_path):
    dest = tmp_path / "delivery"
    secret = _put(dest, "web/deep/config.local.js", b"keep me")
    _put(dest, "app.py", b"old")
    manifest = sync.build_manifest(source, tracked_files=[])
    assert sync.check_delivery(source, dest, manifest)["forbidden"] == ["web/deep/config.local.js"]
    with pytest.raises(ValueError, match="config.local.js"):
        sync.sync_delivery(source, dest, manifest)
    assert secret.read_bytes() == b"keep me"
    assert (dest / "app.py").read_bytes() == b"old"


def test_sync_never_writes_into_source(sync, source):
    manifest = sync.build_manifest(source, tracked_files=[])
    with pytest.raises(ValueError):
        sync.sync_delivery(source, source, manifest)


def test_source_changed_after_manifest_aborts_before_copy(sync, source, tmp_path):
    manifest = sync.build_manifest(source, tracked_files=[])
    _put(source, "app.py", b"changed after collection")
    dest = tmp_path / "delivery"
    with pytest.raises(ValueError, match="changed"):
        sync.sync_delivery(source, dest, manifest)
    assert not dest.exists()


def _symlink(target, link):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"OS does not permit symlinks: {exc}")


def test_symlinks_excluded_and_destination_escape_refused(sync, source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _put(outside, "private.json", b"keep")
    _symlink(outside, source / "web" / "outside")
    _symlink(outside / "private.json", source / "linked.py")
    manifest = sync.build_manifest(source, tracked_files=[])
    assert not any("outside" in name or name == "linked.py" for name in manifest)
    dest = tmp_path / "delivery"
    dest.mkdir()
    _symlink(outside, dest / "web")
    with pytest.raises(ValueError):
        sync.sync_delivery(source, dest, manifest)
    assert sorted(p.name for p in outside.iterdir()) == ["private.json"]


def test_package_is_single_zip64_with_hash_whitelist(pack, source, tmp_path):
    for rel in BAD_PATHS:
        _put(source, rel, b"do not ship")
    output = tmp_path / "新交付.zip"
    pack.package_delivery(source, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert "flood_out/flood_tiles/9/2/3.png" in archive.namelist()
        assert not set(BAD_PATHS).intersection(archive.namelist())
        listing = archive.read("MANIFEST.sha256").decode("utf-8")
        entries = dict(line.split("  ", 1)[::-1] for line in listing.splitlines())
        assert set(archive.namelist()) == set(entries) | {"MANIFEST.sha256"}
        for name, digest in entries.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert archive.getinfo("app.py").extract_version >= 45
    assert pack.validate_package(output) == len(entries)


def test_package_refuses_overwrite_even_invalid_legacy_zip(pack, source, tmp_path):
    output = _put(tmp_path, "old.zip", b"broken legacy package")
    before = output.stat().st_mtime_ns
    with pytest.raises(FileExistsError):
        pack.package_delivery(source, output)
    assert output.read_bytes() == b"broken legacy package"
    assert output.stat().st_mtime_ns == before


def test_package_validator_detects_hash_mismatch(pack, tmp_path):
    output = tmp_path / "bad.zip"
    with zipfile.ZipFile(output, "x") as archive:
        archive.writestr("app.py", b"changed")
        archive.writestr("MANIFEST.sha256", hashlib.sha256(b"original").hexdigest() + "  app.py\n")
    with pytest.raises(ValueError, match="hash"):
        pack.validate_package(output)


def test_cli_check_only_and_new_package(sync, pack, source, tmp_path):
    dest = tmp_path / "copy"
    command = [sys.executable, str(ROOT / "tools/sync_delivery.py"), "--root", str(source),
               "--destination", str(dest)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    red = subprocess.run(command + ["--check"], env=env, capture_output=True, text=True)
    assert red.returncode == 1 and not dest.exists()
    assert "missing" in json.loads(red.stdout)
    green = subprocess.run(command, env=env, capture_output=True, text=True)
    assert green.returncode == 0, green.stderr
    check = subprocess.run(command + ["--check"], env=env, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    output = tmp_path / "cli.zip"
    result = subprocess.run([sys.executable, str(ROOT / "tools/package_delivery.py"),
                             "--root", str(source), "--output", str(output)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert zipfile.is_zipfile(output)


@pytest.mark.parametrize("name", [".env", "web/config.local.js", "../outside.py", "reports/person.json"])
def test_injected_manifest_cannot_bypass_whitelist(sync, pack, source, tmp_path, name):
    manifest = {name: hashlib.sha256(b"private").hexdigest()}
    with pytest.raises(ValueError):
        sync.sync_delivery(source, tmp_path / "delivery", manifest)
    with pytest.raises(ValueError):
        pack.package_delivery(source, tmp_path / "bad.zip", manifest)
    assert not (tmp_path / "delivery").exists()
    assert not (tmp_path / "bad.zip").exists()


@pytest.mark.parametrize("extra", ["web/config.local.js", "extra.py", "APP.PY"])
def test_package_validator_rejects_unlisted_or_duplicate_members(pack, tmp_path, extra):
    output = tmp_path / "tampered.zip"
    with zipfile.ZipFile(output, "x") as archive:
        archive.writestr("app.py", b"original")
        archive.writestr("MANIFEST.sha256", hashlib.sha256(b"original").hexdigest() + "  app.py\n")
        archive.writestr(extra, b"unlisted")
    with pytest.raises(ValueError):
        pack.validate_package(output)


def test_cli_status_codes_and_readonly_error_reports(sync, pack, source, tmp_path, capsys):
    dest = tmp_path / "delivery"
    args = ["--root", str(source), "--destination", str(dest)]
    assert sync.main(args + ["--check"]) == 1
    assert not dest.exists()
    assert sync.main(args) == 0
    assert sync.main(args + ["--check"]) == 0
    _put(dest, "reports/private.json", b"untouched")
    assert sync.main(args) == 2
    assert "nothing deleted" in capsys.readouterr().err
    output = tmp_path / "new.zip"
    assert pack.main(["--root", str(source), "--output", str(output)]) == 0
    assert pack.main(["--root", str(source), "--output", str(output)]) == 2
    assert (dest / "reports/private.json").read_bytes() == b"untouched"


def test_link_detection_applies_to_ancestors_without_os_privileges(sync, source, tmp_path, monkeypatch):
    # Emulate is_symlink/is_junction at the filesystem boundary on Windows CI.
    linked = source / "web"
    monkeypatch.setattr(sync, "is_link", lambda path: path == linked)
    manifest = sync.build_manifest(source, tracked_files=["web/echarts.min.js"])
    assert not any(name.startswith("web/") for name in manifest)
    with pytest.raises(ValueError, match="Symlink/junction"):
        sync.sync_delivery(source, linked / "out")


def test_sync_rejects_hardlinked_destination_before_any_write(sync, source, tmp_path):
    original = _put(tmp_path, "keep.py", b"untouched")
    dest = tmp_path / "delivery"
    dest.mkdir()
    os.link(original, dest / "app.py")
    with pytest.raises(ValueError):
        sync.sync_delivery(source, dest)
    assert original.read_bytes() == b"untouched"
    assert list(dest.iterdir()) == [dest / "app.py"]


def test_empty_source_and_nested_destination_are_refused(sync, source, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No deliverable"):
        sync.build_manifest(empty, tracked_files=[])
    with pytest.raises(ValueError, match="overlaps"):
        sync.sync_delivery(source, source / "web" / "output")
    assert not (source / "web" / "output").exists()


def test_compose_defaults_local_safe_and_persistent():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["floodviz"]
    assert all(port.startswith("127.0.0.1:") for port in service["ports"])
    env = service["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    assert env["FLOOD_DB_PASSWORD"] == "${FLOOD_DB_PASSWORD:-}"
    assert env["FLOOD_ADMIN_PASSWORD"] == "${FLOOD_ADMIN_PASSWORD:-}"
    assert env["FLOOD_DEMO"] == "${FLOOD_DEMO:-0}"
    assert env["FLOOD_DATA_DIR"] == "/app/state"
    assert "flood-state:/app/state" in service["volumes"]
    assert "flood-state" in compose["volumes"]


def test_docker_context_excludes_nested_credentials_and_training_sources():
    patterns = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
    for path in (".env", "nested/.env", "web/deep/config.local.js", "nested/web_users.json",
                 "nested/reports", "C2132_作品.zip", "GF-FloodNet", "pre_2025.nc"):
        assert any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns), path


def test_docker_nonroot_state_and_cpu_dependency_order():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in docker
    assert "FLOOD_DATA_DIR=/app/state" in docker
    assert "HEALTHCHECK" in docker and "/api/health" in docker
    assert "FLOOD_ADMIN_PASSWORD" not in docker
    assert docker.index("-r requirements-cpu.txt") < docker.index("-r requirements-runtime.txt")
    assert "pip check" in docker


def test_dependency_layers_and_ci_gate():
    runtime = ROOT / "requirements-runtime.txt"
    assert runtime.is_file(), "runtime and pipeline dependencies must be separated"
    text = runtime.read_text(encoding="utf-8")
    assert not any(line.startswith(("torch", "netCDF4", "python-docx")) for line in text.splitlines())
    cpu = (ROOT / "requirements-cpu.txt").read_text(encoding="utf-8")
    assert "--index-url https://download.pytorch.org/whl/cpu" in cpu and "torch>=" in cpu
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert set(ci["jobs"]["test"]["strategy"]["matrix"]["os"]) == {"windows-latest", "ubuntu-latest"}
    steps = ci["jobs"]["test"]["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "--cov=app" in commands and "--cov=store" in commands and "--cov-fail-under=80" in commands
    assert "bandit" in commands and "pip_audit" in commands and "pip check" in commands
    assert commands.index("-r requirements-cpu.txt") < commands.index("-r requirements-dev.txt")
    assert "secrets." not in json.dumps(ci)
    assert "schedule" not in ci.get("on", ci.get(True, {}))


def test_windows_setup_has_optional_dev_and_watchdog_uses_project_venv():
    setup = (ROOT / "setup.bat").read_text(encoding="utf-8")
    watchdog = (ROOT / "deploy/watchdog.bat").read_text(encoding="utf-8")
    assert "--dev" in setup and "requirements-dev.txt" in setup
    assert setup.index("-r requirements-cpu.txt") < setup.index("-r requirements.txt")
    assert "D:\\Lib" not in watchdog
    assert "%~dp0.." in watchdog and ".venv\\Scripts\\python.exe" in watchdog
    assert "FLOOD_PORT" in watchdog

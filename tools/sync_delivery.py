# -*- coding: utf-8 -*-
"""Build a SHA-256 allowlist, then sync/check a delivery without deleting files.

Usage: python tools/sync_delivery.py --check
       python tools/sync_delivery.py --destination <new-directory>
Imports are inert. Legacy credentials cause refusal, not automatic cleanup.
A Git tracked path is a candidate, NEVER permission to ship the whole repository.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

ROOT = Path(__file__).absolute().parents[1]
DEFAULT_DESTINATION = "C2132_开发源码"
MANIFEST_NAME = "MANIFEST.sha256"

# Explicit scopes. Root *.py includes new first-party modules before commit.
FILES = frozenset({
    "README.md", "CHANGELOG.md", "LICENSE", "Dockerfile", "docker-compose.yml",
    ".dockerignore", ".gitignore", ".gitattributes", ".env.example", "pytest.ini",
    "setup.bat", "run.bat", "realevent_events.json", "gz_tower_buildings.geojson",
    ".github/workflows/ci.yml",
})
TREE_SUFFIXES = {
    "pipeline": {".py"}, "deploy": {".bat", ".sql"}, "tools": {".py"},
    "tests": {".py"}, "docs": {".md"},
    "web": {".js", ".css", ".html", ".json", ".geojson", ".png", ".jpg", ".jpeg",
            ".svg", ".webp", ".woff", ".woff2", ".ttf", ".ico"},
    "dem": {".tif"},
    "flood_out": {".tif", ".json", ".geojson", ".png"},  # actual XYZ tiles
    "unet_out": {".pt", ".json", ".png"},
    "realevent_out": {".tif", ".json", ".geojson", ".png"},
    "geoscene_out": {".json", ".geojson", ".tif"},
}
DENIED_PARTS = frozenset({
    "reports", "state", "secrets", "credentials", "private", "__pycache__",
    "node_modules", "venv", "env", "cache", "caches", "gf-floodnet", "precip_tif",
})
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".npz", ".pyc", ".zip", ".log"}


def relative_name(name):
    """Reject traversal, Windows drive/ADS paths and ambiguous manifest lines."""
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe delivery path: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts):
        raise ValueError(f"Unsafe delivery path: {name!r}")
    if any(ord(c) < 32 or c in '<>"|?*' for c in name):
        raise ValueError(f"Unsafe delivery path: {name!r}")
    return PurePosixPath(name)


def forbidden(name):
    path = relative_name(name)
    for index, part in enumerate(path.parts):
        low = part.lower()
        if low in DENIED_PARTS or low.startswith(("c2132_", "nasa", "_cache")):
            return True
        if low.startswith("."):
            if str(path) in FILES or (index == 0 and low == ".github"):
                continue
            return True
        if low.startswith(("config.local.", "web_users.", "subscribers.", "notify_state.",
                           "credentials.", "secrets.", "id_rsa", "id_ed25519")):
            return True
    return path.suffix.lower() in DENIED_SUFFIXES


def allowed(name):
    path = relative_name(name)
    if forbidden(name):
        return False
    if name in FILES:
        return True
    if len(path.parts) == 1:
        return path.suffix in {".py", ".html"} or (
            path.name.startswith("requirements") and path.suffix == ".txt")
    return path.suffix.lower() in TREE_SUFFIXES.get(path.parts[0], set())


def is_link(path):
    # Junctions escape directories on Windows even though is_symlink is False.
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def safe_absolute(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if is_link(part):
            raise ValueError(f"Symlink/junction is not permitted: {part}")
    return path


def safe_path(root, name):
    relative_name(name)
    root = safe_absolute(root)
    path = safe_absolute(root / name)
    if not path.is_relative_to(root):
        raise ValueError(f"Path escapes root: {name}")
    return path


def sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_files(root):
    """NUL separation disables Git's quoted/octal rendering of Chinese names."""
    try:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                                capture_output=True, timeout=30, check=False)
    except FileNotFoundError:
        return []  # standalone source can work without Git
    if result.returncode:
        if (Path(root) / ".git").exists():
            raise ValueError("git ls-files failed; refusing an incomplete tracked manifest")
        return []
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def _walk_scope(root, scope):
    base = root / scope
    if not base.is_dir() or is_link(base):
        return
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not is_link(Path(directory) / d)
                         and not forbidden((Path(directory) / d).relative_to(root).as_posix()))
        for filename in sorted(files):
            path = Path(directory) / filename
            if not is_link(path):
                yield path.relative_to(root).as_posix()


def build_manifest(root, tracked_files=None):
    """Return sorted {safe relative path: SHA-256}, never trusting Git alone."""
    root = safe_absolute(root)
    if not root.is_dir():
        raise ValueError(f"Missing source directory: {root}")
    candidates = set(FILES)
    candidates.update(p.name for p in root.iterdir() if p.is_file() and not is_link(p))
    candidates.update(git_files(root) if tracked_files is None else tracked_files)
    for scope in TREE_SUFFIXES:
        candidates.update(_walk_scope(root, scope))
    manifest = {}
    seen = set()
    for name in sorted(candidates):
        if not allowed(name):
            continue
        try:
            path = safe_path(root, name)
        except ValueError:  # do not follow an otherwise permitted linked path
            continue
        if not path.is_file():
            continue
        if name.casefold() in seen:
            raise ValueError(f"Case-insensitive delivery path collision: {name}")
        seen.add(name.casefold())
        manifest[name] = sha256(path)
    if not manifest:
        raise ValueError("No deliverable files found")
    return manifest


def validate_sources(root, manifest):
    if not manifest:
        raise ValueError("Empty delivery manifest")
    seen = set()
    for name, digest in manifest.items():
        if not allowed(name) or name.casefold() in seen:
            raise ValueError(f"Unapproved/duplicate manifest path: {name}")
        seen.add(name.casefold())
        path = safe_path(root, name)
        if not path.is_file() or sha256(path) != digest:
            raise ValueError(f"Source missing or changed after manifest creation: {name}")


def forbidden_entries(destination):
    """Inspect names only; never read credentials or follow linked directories."""
    destination = safe_absolute(destination)
    result = []
    if not destination.exists():
        return result
    if not destination.is_dir():
        raise ValueError(f"Destination is not a directory: {destination}")
    for directory, dirs, files in os.walk(destination, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(destination).as_posix()
            if is_link(path) or forbidden(relative):
                result.append(relative)
                if name in dirs:
                    dirs.remove(name)
    return sorted(result)


def check_delivery(root, destination, manifest=None):
    """Read-only hash comparison. Extras never expand the packaging whitelist."""
    root, destination = safe_absolute(root), safe_absolute(destination)
    manifest = build_manifest(root) if manifest is None else manifest
    validate_sources(root, manifest)
    result = {"missing": [], "changed": [], "forbidden": forbidden_entries(destination)}
    for name, digest in manifest.items():
        try:
            path = safe_path(destination, name)
        except ValueError:
            if name not in result["forbidden"]:
                result["forbidden"].append(name)
            continue
        if not path.is_file():
            result["missing"].append(name)
        elif sha256(path) != digest:
            result["changed"].append(name)
    return result


def sync_delivery(root, destination, manifest=None):
    """Copy only manifest files; refuse legacy secrets before making any writes."""
    root, destination = safe_absolute(root), safe_absolute(destination)
    if root == destination or root.is_relative_to(destination):
        raise ValueError("Destination must not be the source or its parent")
    # Nested output must be outside enumerated runtime/source scopes.
    if destination.is_relative_to(root):
        top = destination.relative_to(root).parts[0]
        if top in TREE_SUFFIXES or top == ".git":
            raise ValueError("Destination overlaps an included source scope")
    manifest = build_manifest(root) if manifest is None else manifest
    differences = check_delivery(root, destination, manifest)
    if differences["forbidden"]:
        raise ValueError("Legacy forbidden files; use a NEW destination or review manually (nothing deleted): "
                         + ", ".join(differences["forbidden"]))
    pending = sorted(differences["missing"] + differences["changed"])
    for name in pending:
        target = safe_path(destination, name)
        if target.exists() and (not target.is_file() or target.stat().st_nlink > 1):
            raise ValueError(f"Destination is not an ordinary unlinked file: {name}")
    for name in pending:
        source, target = safe_path(root, name), safe_path(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha256(target) != manifest[name]:
            raise ValueError(f"Source changed while copying: {name}")
    return {"copied": len(pending), "identical": len(manifest) - len(pending)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--check", action="store_true", help="read-only SHA-256 comparison")
    args = parser.parse_args(argv)
    destination = args.destination or args.root / DEFAULT_DESTINATION
    try:
        manifest = build_manifest(args.root)
        if args.check:
            result = check_delivery(args.root, destination, manifest)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return int(any(result.values()))
        print(json.dumps(sync_delivery(args.root, destination, manifest), ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Delivery refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

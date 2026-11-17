# -*- coding: utf-8 -*-
"""Create a NEW single-volume ZIP64 directly from the source allowlist.

This is independent of sync: it never walks an old delivery copy to fill gaps.
Usage: python tools/package_delivery.py --output <new-path.zip>
Existing output is never replaced; failed new outputs remain for investigation.
"""
import argparse
import hashlib
from pathlib import Path
import stat
import sys
import zipfile

try:
    from tools.sync_delivery import (ROOT, MANIFEST_NAME, allowed, build_manifest,
                                     safe_absolute, safe_path, sha256, validate_sources)
except ModuleNotFoundError:  # direct `python tools/package_delivery.py`
    from sync_delivery import (ROOT, MANIFEST_NAME, allowed, build_manifest,
                               safe_absolute, safe_path, sha256, validate_sources)


def validate_package(output):
    """Validate CRC, allowlisted members, duplicates, and the embedded hash list."""
    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate ZIP member")
        if MANIFEST_NAME not in names:
            raise ValueError("ZIP has no hash manifest")
        for info in infos:
            if (info.filename != MANIFEST_NAME and not allowed(info.filename)) or (
                    stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError(f"Forbidden ZIP member: {info.filename}")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"ZIP CRC failure: {bad}")
        entries = {}
        for line in archive.read(MANIFEST_NAME).decode("utf-8").splitlines():
            digest, name = line.split("  ", 1)
            if name in entries or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid hash manifest")
            entries[name] = digest
        if not entries or set(names) != set(entries) | {MANIFEST_NAME}:
            raise ValueError("ZIP members differ from hash manifest")
        for name, digest in entries.items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError(f"ZIP hash mismatch: {name}")
    return len(entries)


def package_delivery(root, output, manifest=None):
    """Only entries in this source manifest may become ZIP members."""
    root, output = safe_absolute(root), safe_absolute(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing delivery: {output}")
    if output.suffix.lower() != ".zip" or not output.parent.is_dir():
        raise ValueError("Output must be a NEW .zip in an existing directory")
    manifest = build_manifest(root) if manifest is None else manifest
    validate_sources(root, manifest)
    # Exclusive creation also protects against a competing writer after exists().
    with output.open("xb") as target:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            for name, expected in sorted(manifest.items()):
                source = safe_path(root, name)
                digest = hashlib.sha256()
                with source.open("rb") as stream, archive.open(name, "w", force_zip64=True) as member:
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        digest.update(chunk)
                        member.write(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f"Source changed while packaging: {name}")
            listing = "".join(f"{digest}  {name}\n" for name, digest in sorted(manifest.items()))
            archive.writestr(MANIFEST_NAME, listing.encode("utf-8"))
    validate_package(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True,
                        help="new filename; no default and no overwrite option")
    args = parser.parse_args(argv)
    try:
        output = package_delivery(args.root, args.output)
        print(f"ZIP validated: {output}\nSHA256: {sha256(output)}")
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Packaging refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

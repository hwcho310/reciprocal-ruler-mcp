#!/usr/bin/env python3
"""Build the MCPB bundle for reciprocal-ruler-mcp.

The server is standard-library-only Python, so the bundle is just the manifest,
the server source, and the licence. An .mcpb file is a zip archive.

    python3 scripts/build_mcpb.py                 # -> dist/reciprocal-ruler-mcp.mcpb
    python3 scripts/build_mcpb.py --sha256        # also print the SHA-256 (for server.json)

The manifest lives in mcpb/manifest.json. Keep its version in step with
pyproject.toml before cutting a release: the registry entry (server.json) points at
https://github.com/hwcho310/reciprocal-ruler-mcp/releases/download/v<version>/reciprocal-ruler-mcp.mcpb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "mcpb" / "manifest.json"
PACKAGE_DIR = ROOT / "src" / "reciprocal_ruler_mcp"
OUT_DIR = ROOT / "dist"

# Files that go into the bundle, as (source, archive name) pairs.
EXTRA_FILES = [
    (ROOT / "LICENSE", "LICENSE"),
    (ROOT / "README.md", "README.md"),
]


def manifest_version() -> str:
    with MANIFEST.open(encoding="utf-8") as handle:
        return json.load(handle)["version"]


def collect_sources() -> list[tuple[Path, str]]:
    if not MANIFEST.exists():
        raise SystemExit(f"manifest not found: {MANIFEST}")
    if not PACKAGE_DIR.is_dir():
        raise SystemExit(f"package not found: {PACKAGE_DIR}")

    files: list[tuple[Path, str]] = [(MANIFEST, "manifest.json")]
    for source in sorted(PACKAGE_DIR.rglob("*.py")):
        files.append((source, source.relative_to(ROOT).as_posix()))
    for source, name in EXTRA_FILES:
        if source.exists():
            files.append((source, name))
    return files


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, name in collect_sources():
            archive.write(source, name)
    return output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None, help="output .mcpb path")
    parser.add_argument("--sha256", action="store_true", help="print the SHA-256 of the bundle")
    args = parser.parse_args()

    version = manifest_version()
    output = args.output or OUT_DIR / "reciprocal-ruler-mcp.mcpb"
    built = build(output)
    size = built.stat().st_size
    print(f"built {built.relative_to(ROOT) if built.is_relative_to(ROOT) else built} "
          f"(v{version}, {size} bytes)")
    if args.sha256:
        print(sha256(built))
    return 0


if __name__ == "__main__":
    sys.exit(main())

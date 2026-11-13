"""Prepare a hash-pinned optional overlay for the installed Nexent v2.8.3 SDK.

This command never edits the SDK input and never starts or recreates a service.
The output contains only the two patched modules and the optional startup hook.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "observation-manifest.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Patch paths must remain relative to the SDK source tree")
    return path


def apply_unified_patch(originals: dict[str, bytes], patch: str) -> dict[str, bytes]:
    """Apply exact unified hunks; reject fuzz, unknown files and incomplete hunks."""
    lines = patch.splitlines(keepends=True)
    result = {}
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- a/"):
            raise ValueError("Expected a unified patch file header")
        name = lines[index][6:].strip()
        _safe_relative(name)
        index += 1
        if index >= len(lines) or lines[index].strip() != "+++ b/" + name:
            raise ValueError("Renames and mismatched patch headers are not supported")
        if name not in originals or name in result:
            raise ValueError("Patch contains an unknown or duplicate SDK file")
        source = originals[name].decode("utf-8").splitlines(keepends=True)
        output = []
        cursor = 0
        index += 1
        saw_hunk = False
        while index < len(lines) and not lines[index].startswith("--- a/"):
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", lines[index])
            if not match:
                raise ValueError("Invalid unified hunk header")
            old_start, old_count = int(match[1]), int(match[2] or 1)
            new_start, new_count = int(match[3]), int(match[4] or 1)
            target = old_start - 1 if old_start else 0
            if target < cursor or target > len(source):
                raise ValueError("Overlapping or out-of-range patch hunk")
            output.extend(source[cursor:target])
            cursor = target
            if new_start - 1 != len(output):
                raise ValueError("Patched hunk position does not match")
            index += 1
            removed = added = 0
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- a/")):
                line = lines[index]
                if not line or line[0] not in " +-":
                    raise ValueError("Unsupported unified patch line")
                marker, content = line[0], line[1:]
                if marker in " -":
                    if cursor >= len(source) or source[cursor] != content:
                        raise ValueError("SDK source context differs from the pinned patch")
                    cursor += 1
                    removed += 1
                if marker in " +":
                    output.append(content)
                    added += 1
                index += 1
            if (removed, added) != (old_count, new_count):
                raise ValueError("Unified patch hunk counts do not match")
            saw_hunk = True
        if not saw_hunk:
            raise ValueError("SDK file has no patch hunks")
        output.extend(source[cursor:])
        result[name] = "".join(output).encode("utf-8")
    if set(result) != set(originals):
        raise ValueError("Patch did not cover exactly the pinned SDK modules")
    return result


def prepare(sdk_package: Path, output: Path) -> dict:
    """Validate a clean package copy and create a separate private overlay."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    patch_bytes = (HERE / manifest["patch_file"]).read_bytes()
    if sha256(patch_bytes) != manifest["patch_sha256"]:
        raise ValueError("Packaged SDK patch SHA256 does not match its manifest")
    package = sdk_package.resolve(strict=True)
    if not package.is_dir():
        raise ValueError("SDK package input must be the installed nexent package directory")
    originals = {}
    for entry in manifest["sdk_files"]:
        relative = _safe_relative(entry["path"])
        if relative.parts[:2] != ("sdk", "nexent"):
            raise ValueError("Unexpected pinned SDK package path")
        source = package.joinpath(*relative.parts[2:])
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(package):
            raise ValueError("SDK source file escaped the supplied package directory")
        data = source.read_bytes()
        if sha256(data) != entry["baseline_sha256"]:
            raise ValueError("SDK module differs from the clean v2.8.3 baseline; refusing to patch")
        originals[entry["path"]] = data
    patched = apply_unified_patch(originals, patch_bytes.decode("utf-8"))
    prepared = {}
    for entry in manifest["sdk_files"]:
        data = patched[entry["path"]]
        if sha256(data) != entry["candidate_sha256"]:
            raise ValueError("Prepared SDK module SHA256 differs from the live-smoke candidate")
        ast.parse(data.decode("utf-8"), filename=entry["path"])
        prepared[entry["path"]] = data
    for entry in manifest["startup_files"]:
        relative = _safe_relative(entry["path"])
        data = (HERE / Path(*relative.parts)).read_bytes()
        if sha256(data) != entry["sha256"]:
            raise ValueError("Packaged startup hook SHA256 does not match its manifest")
        ast.parse(data.decode("utf-8"), filename=entry["path"])
        prepared[entry["path"]] = data
    destination = output.resolve()
    if destination.is_relative_to(package):
        raise ValueError("Overlay output must stay outside the SDK input package")
    if destination.exists():
        raise ValueError("Overlay destination already exists; use a new private output directory")
    destination.mkdir(mode=0o700, parents=True)
    inventory = []
    for relative, data in prepared.items():
        path = destination / Path(*_safe_relative(relative).parts)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        inventory.append({"path": relative, "sha256": sha256(data)})
    report = {
        "schema": "topic16-prepared-observation-overlay-v1",
        "target_version": manifest["target_version"],
        "upstream_tag_commit": manifest["upstream_tag_commit"],
        "patch_sha256": manifest["patch_sha256"],
        "files": inventory,
        "services_started": False,
        "sdk_input_modified": False,
    }
    inventory_file = destination / "overlay-manifest.json"
    inventory_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    inventory_file.chmod(0o600)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-package", type=Path, required=True,
                        help="A clean copy of the installed nexent package, not the whole platform repository")
    parser.add_argument("--output", type=Path, required=True,
                        help="A new private directory outside Git for the prepared overlay")
    args = parser.parse_args()
    report = prepare(args.sdk_package, args.output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

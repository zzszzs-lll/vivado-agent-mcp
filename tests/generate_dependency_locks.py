from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fully pinned hash lock files from resolved wheel directories.")
    parser.add_argument(
        "--lock",
        action="append",
        default=[],
        metavar="OUTPUT=WHEEL_DIR",
        help="Output lock path and resolved wheel directory; may be repeated.",
    )
    args = parser.parse_args(argv)
    if not args.lock:
        parser.error("at least one --lock OUTPUT=WHEEL_DIR is required")
    for item in args.lock:
        output_text, separator, wheel_dir_text = item.partition("=")
        if not separator:
            parser.error(f"invalid --lock value: {item!r}")
        output = Path(output_text).expanduser().resolve()
        wheel_dir = Path(wheel_dir_text).expanduser().resolve()
        write_lock(output, wheel_dir)
        print(f"wrote {output}")
    return 0


def write_lock(output: Path, wheel_dir: Path) -> None:
    wheels = sorted(path for path in wheel_dir.glob("*.whl") if path.is_file())
    if not wheels:
        raise ValueError(f"wheel directory contains no wheels: {wheel_dir}")
    entries: dict[str, dict[str, object]] = {}
    wheel_requirements: dict[str, list[str]] = {}
    for wheel in wheels:
        name, version, requires_dist = _wheel_metadata(wheel)
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        entry = entries.setdefault(normalized_name, {"version": version, "hashes": []})
        if entry["version"] != version:
            raise ValueError(f"multiple versions found for {normalized_name}: {entry['version']} and {version}")
        entry["hashes"].append(_sha256(wheel))
        wheel_requirements.setdefault(normalized_name, []).extend(requires_dist)
    _validate_dependency_closure(
        entries,
        wheel_requirements,
        target_python_version=_target_python_version(output),
    )
    lines = [
        "# Generated from a resolved Windows wheelhouse.",
        "# Update intentionally; CI installs this file with --require-hashes.",
        "",
    ]
    for name in sorted(entries):
        version = str(entries[name]["version"])
        hashes = sorted(set(str(value) for value in entries[name]["hashes"]))
        lines.append(f"{name}=={version} \\")
        for index, digest in enumerate(hashes):
            suffix = " \\" if index < len(hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
        lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="ascii")


def _wheel_metadata(path: Path) -> tuple[str, str, list[str]]:
    with zipfile.ZipFile(path) as archive:
        metadata_paths = [
            name
            for name in archive.namelist()
            if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError(f"expected exactly one METADATA in {path.name}")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    name = str(metadata.get("Name", "")).strip()
    version = str(metadata.get("Version", "")).strip()
    if not name or not version:
        raise ValueError(f"wheel metadata missing name/version: {path.name}")
    return name, version, [str(value) for value in metadata.get_all("Requires-Dist", [])]


def _target_python_version(output: Path) -> str:
    match = re.search(r"(?:^|-)py(?P<major>\d)(?P<minor>\d+)(?:-|\.)", output.name.lower())
    if not match:
        return ""
    return f"{match.group('major')}.{match.group('minor')}"


def _validate_dependency_closure(
    entries: dict[str, dict[str, object]],
    wheel_requirements: dict[str, list[str]],
    *,
    target_python_version: str,
) -> None:
    environment = default_environment()
    environment.update(
        {
            "extra": "",
            "platform_machine": "AMD64",
            "platform_system": "Windows",
            "python_version": target_python_version or environment["python_version"],
            "python_full_version": f"{target_python_version}.0" if target_python_version else environment["python_full_version"],
            "sys_platform": "win32",
        }
    )
    missing: list[str] = []
    for owner, requirement_texts in wheel_requirements.items():
        for requirement_text in requirement_texts:
            requirement = Requirement(requirement_text)
            if requirement.marker is not None and not requirement.marker.evaluate(environment=environment):
                continue
            dependency = re.sub(r"[-_.]+", "-", requirement.name).lower()
            if dependency not in entries:
                missing.append(f"{owner} requires {requirement}")
    if missing:
        target = target_python_version or environment["python_version"]
        raise ValueError(
            f"wheel directory is not dependency-closed for Windows Python {target}: " + "; ".join(sorted(missing))
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from generate_dependency_locks import write_lock


LOCK_ROOT = Path(__file__).resolve().parents[1] / "requirements"


def test_dependency_locks_are_exact_versioned_and_hash_pinned() -> None:
    lock_paths = sorted(LOCK_ROOT.glob("*.txt"))

    assert {path.name for path in lock_paths} == {
        "build-windows.txt",
        "dev-py311-windows.txt",
        "dev-py312-windows.txt",
        "runtime-py311-windows.txt",
        "runtime-py312-windows.txt",
    }
    for path in lock_paths:
        entries = _parse_lock(path)
        assert entries, path
        assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in entries), path
        assert all(re.fullmatch(r"[^\s;]+", item["version"]) for item in entries.values()), path
        assert all(item["hashes"] and all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in item["hashes"]) for item in entries.values()), path


def test_runtime_and_dev_lock_roles_are_consistent() -> None:
    for version in ("py311", "py312"):
        runtime = _parse_lock(LOCK_ROOT / f"runtime-{version}-windows.txt")
        dev = _parse_lock(LOCK_ROOT / f"dev-{version}-windows.txt")

        assert "mcp" in runtime
        assert {"pytest", "build", "twine"}.isdisjoint(runtime)
        assert set(runtime) <= set(dev)
        assert {"mcp", "pytest", "build", "twine"} <= set(dev)
        assert all(runtime[name]["version"] == dev[name]["version"] for name in runtime)

    build = _parse_lock(LOCK_ROOT / "build-windows.txt")
    assert {"build", "setuptools", "wheel", "packaging", "pyproject-hooks"} <= set(build)

    py311_dev = _parse_lock(LOCK_ROOT / "dev-py311-windows.txt")
    py312_dev = _parse_lock(LOCK_ROOT / "dev-py312-windows.txt")
    assert {"backports-tarfile", "importlib-metadata", "zipp"} <= set(py311_dev)
    assert {"backports-tarfile", "importlib-metadata", "zipp"}.isdisjoint(py312_dev)


def test_lock_generator_checks_target_python_marker_dependency_closure(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_metadata_wheel(
        wheelhouse / "parent-1.0-py3-none-any.whl",
        name="parent",
        version="1.0",
        requires_dist=['backports.tarfile; python_version < "3.12"'],
    )

    with pytest.raises(ValueError, match="backports.tarfile"):
        write_lock(tmp_path / "dev-py311-windows.txt", wheelhouse)

    write_lock(tmp_path / "dev-py312-windows.txt", wheelhouse)
    _write_metadata_wheel(
        wheelhouse / "backports.tarfile-1.2.0-py3-none-any.whl",
        name="backports.tarfile",
        version="1.2.0",
    )
    py311_lock = tmp_path / "dev-py311-windows.txt"
    write_lock(py311_lock, wheelhouse)
    assert "backports-tarfile==1.2.0" in py311_lock.read_text(encoding="ascii")


def test_ci_consumes_pre_resolved_locks_and_uploads_exact_artifacts() -> None:
    workflow = (LOCK_ROOT.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pip install --require-hashes -r requirements/build-windows.txt" in workflow
    assert 'requirements/dev-$pythonTag-windows.txt' in workflow
    assert "--runtime-lock requirements/runtime-py311-windows.txt" in workflow
    assert "--runtime-lock requirements/runtime-py312-windows.txt" in workflow
    assert workflow.count("--source-provenance-manifest dist/source-provenance.json") == 2
    assert "tests/build_distributions.py" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1" in workflow
    assert "dist/source-provenance.json" in workflow


def _parse_lock(path: Path) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    current_name = ""
    for raw_line in path.read_text(encoding="ascii").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("--hash"):
            match = re.fullmatch(r"([a-z0-9][a-z0-9-]*)==([^\s]+) \\", line)
            assert match, (path, line)
            current_name = match.group(1)
            assert current_name not in entries
            entries[current_name] = {"version": match.group(2), "hashes": []}
            continue
        assert current_name
        match = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})(?: \\)?", line)
        assert match, (path, line)
        entries[current_name]["hashes"].append(match.group(1))
    return entries


def _write_metadata_wheel(
    path: Path,
    *,
    name: str,
    version: str,
    requires_dist: list[str] | None = None,
) -> None:
    metadata_lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata_lines.extend(f"Requires-Dist: {item}" for item in requires_dist or [])
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata_lines) + "\n")

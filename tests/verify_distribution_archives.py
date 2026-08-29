from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    dist_dir = Path(args[0] if args else "dist").resolve()
    wheels = sorted(dist_dir.glob("vivado_agent_mcp-*.whl"))
    sdists = sorted(dist_dir.glob("vivado_agent_mcp-*.tar.gz"))
    errors: list[str] = []
    if len(wheels) != 1:
        errors.append(f"expected one wheel, found {len(wheels)}")
    if len(sdists) != 1:
        errors.append(f"expected one sdist, found {len(sdists)}")

    if wheels:
        with zipfile.ZipFile(wheels[0]) as archive:
            names = [PurePosixPath(name) for name in archive.namelist()]
            if not any(path.parts[-2:] == ("licenses", "LICENSE") for path in names if len(path.parts) >= 2):
                errors.append("wheel does not contain dist-info/licenses/LICENSE")
            if any(path.name in {"release_bundle.py", "source_cleanup.py"} for path in names):
                errors.append("wheel contains maintainer-only release or source-cleanup modules")
            fixture_names = {
                "vivado_agent_mcp/qualification_fixture/qualification_counter.sv",
                "vivado_agent_mcp/qualification_fixture/tb_qualification_counter.sv",
                "vivado_agent_mcp/qualification_fixture/qualification_counter.xdc",
            }
            archived_names = {path.as_posix() for path in names}
            if not fixture_names <= archived_names:
                errors.append("wheel does not contain the complete deterministic qualification fixture")

    if sdists:
        with tarfile.open(sdists[0], mode="r:gz") as archive:
            names = [PurePosixPath(name) for name in archive.getnames()]
            if not any(path.name == "LICENSE" and len(path.parts) == 2 for path in names):
                errors.append("sdist does not contain a top-level LICENSE")
            relative_names = {
                "/".join(path.parts[1:])
                for path in names
                if len(path.parts) >= 2
            }
            if "qualification/qualification-record.schema.json" not in relative_names:
                errors.append("sdist does not contain the public qualification record schema")
            if "qualification/matrix.json" not in relative_names:
                errors.append("sdist does not contain the public qualification matrix")

    if errors:
        print("distribution archive verification: BLOCK", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2
    print("distribution archive verification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

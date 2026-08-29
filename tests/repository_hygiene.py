from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


FORBIDDEN_TOP_LEVEL = {
    ".omx",
    ".pip_tmp",
    ".pytest_cache",
    ".vivado_agent_mcp",
    ".workspace",
    "build",
    "dist",
    "test_use",
}
FORBIDDEN_FILENAMES = {"AGENTS.md", ".flexlmrc", "attestation.key"}
FORBIDDEN_PATH_COMPONENTS = {".Xil", "xsim.dir"}
FORBIDDEN_DIRECTORY_SUFFIXES = {".cache", ".gen", ".hw", ".ip_user_files", ".runs", ".sim", ".srcs"}
FORBIDDEN_SUFFIXES = {
    ".bin",
    ".bmm",
    ".bit",
    ".dcp",
    ".hwdef",
    ".jou",
    ".lic",
    ".log",
    ".ltx",
    ".mcs",
    ".mmi",
    ".pb",
    ".prm",
    ".rpt",
    ".sysdef",
    ".vcd",
    ".wdb",
    ".whl",
    ".xsa",
    ".xpr",
    ".zip",
}
SENSITIVE_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"(?:github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,})"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "slack_token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "personal_home": re.compile(rb"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s]+|/(?:home|Users)/[^/\s]+)"),
}
MAX_TRACKED_FILE_BYTES = 20 * 1024 * 1024


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    tracked = _tracked_files(workspace)
    violations: list[str] = []
    for relative in tracked:
        posix = PurePosixPath(relative)
        path = workspace / relative
        if posix.parts and posix.parts[0] in FORBIDDEN_TOP_LEVEL:
            violations.append(f"forbidden tracked path: {relative}")
            continue
        if any(
            part in FORBIDDEN_PATH_COMPONENTS or any(part.endswith(suffix) for suffix in FORBIDDEN_DIRECTORY_SUFFIXES)
            for part in posix.parts[:-1]
        ):
            violations.append(f"forbidden generated directory: {relative}")
            continue
        if posix.name in FORBIDDEN_FILENAMES or posix.suffix.lower() in FORBIDDEN_SUFFIXES:
            violations.append(f"forbidden tracked file: {relative}")
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_TRACKED_FILE_BYTES:
            violations.append(f"tracked file exceeds {MAX_TRACKED_FILE_BYTES} bytes: {relative} ({size})")
            continue
        content = path.read_bytes()
        if b"\x00" in content:
            continue
        for pattern_name, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(content):
                violations.append(f"sensitive pattern {pattern_name}: {relative}")

    if violations:
        print("repository hygiene: BLOCK", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 2
    print(f"repository hygiene: PASS ({len(tracked)} tracked files checked)")
    return 0


def _tracked_files(workspace: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip())
    return [item.decode("utf-8", errors="strict") for item in completed.stdout.split(b"\0") if item]


if __name__ == "__main__":
    raise SystemExit(main())

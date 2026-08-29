from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    ensure_pytest_basetemp_parent(config)


def ensure_pytest_basetemp_parent(config: pytest.Config, *, workspace: Path | None = None) -> None:
    root = workspace.resolve() if workspace is not None else Path(__file__).resolve().parents[1]
    expected = (root / "test_use" / "pytest_tmp").resolve()
    configured = config.getoption("basetemp")
    if configured is None:
        return
    candidate = Path(str(configured)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.resolve() != expected:
        return
    expected.parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def isolate_local_attestation_trust() -> Iterator[None]:
    workspace = Path(__file__).resolve().parents[1]
    trust_dir = workspace / "test_use" / "pytest_attestation_trust" / uuid.uuid4().hex
    previous = os.environ.get("VIVADO_AGENT_MCP_TRUST_DIR")
    os.environ["VIVADO_AGENT_MCP_TRUST_DIR"] = str(trust_dir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VIVADO_AGENT_MCP_TRUST_DIR", None)
        else:
            os.environ["VIVADO_AGENT_MCP_TRUST_DIR"] = previous
        shutil.rmtree(trust_dir, ignore_errors=True)

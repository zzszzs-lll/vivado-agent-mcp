from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


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

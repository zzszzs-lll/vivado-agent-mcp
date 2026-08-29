from __future__ import annotations

from pathlib import Path


def test_live_qualification_workflow_is_manual_self_hosted_and_fail_closed() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "live-qualification.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "runs-on: [self-hosted, Windows, vivado-2021.2]" in workflow
    assert "VIVADO_PATH: ${{ vars.VIVADO_2021_2_PATH }}" in workflow
    assert "tests/build_distributions.py" in workflow
    assert "tests/clean_install_smoke.py" in workflow
    assert "tests/live_qualification_runner.py" in workflow
    assert "--release-wheel" in workflow
    assert "--source-provenance-manifest" in workflow
    assert "--include-live-vivado" in workflow
    assert "--require-qualified" in workflow
    assert "qualification-record.json" in workflow
    assert "qualification-public-summary.json" in workflow
    assert "**/qualification-summary.json" not in workflow
    assert "test_use/live_qualification/**/public-evidence/*.json" in workflow
    assert "if: ${{ always() }}" in workflow
    assert "if-no-files-found: warn" in workflow
    assert "hardware status remains NOT_VALIDATED" in workflow

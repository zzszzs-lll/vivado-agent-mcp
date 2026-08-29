from __future__ import annotations

import tomllib
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]


def test_apache_license_is_complete_and_declared_for_distributions() -> None:
    license_text = (WORKSPACE / "LICENSE").read_text(encoding="utf-8")
    config = tomllib.loads((WORKSPACE / "pyproject.toml").read_text(encoding="utf-8"))

    assert "APPENDIX: How to apply the Apache License to your work." in license_text
    assert config["project"]["license"] == "Apache-2.0"
    assert config["project"]["license-files"] == ["LICENSE"]
    assert "setuptools>=77.0.3" in config["build-system"]["requires"]


def test_public_package_exposes_only_end_user_console_script() -> None:
    config = tomllib.loads((WORKSPACE / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["scripts"] == {
        "vivado-agent-mcp": "vivado_agent_mcp.__main__:main",
    }
    assert set(config["project"]["optional-dependencies"]["dev"]) >= {"build>=1.2", "pytest>=8", "twine>=6"}


def test_distribution_ci_installs_the_exact_verified_wheel() -> None:
    workflow = (WORKSPACE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6" in workflow
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" not in workflow
    assert "--wheel-path $wheel.FullName" in workflow
    assert "--expected-wheel-sha256 $wheelSha256" in workflow
    assert "ci_clean_install_py311" in workflow
    assert "ci_clean_install_py312" in workflow
    assert "ci_sdist_install" in workflow
    assert "- name: Build distributions" in workflow
    assert "- name: Check distribution metadata" in workflow
    assert "- name: Verify distribution archives" in workflow
    assert "Run exact wheel clean-install smoke on Python 3.11 and 3.12" not in workflow
    assert "Require Tcl integration runtime on Python 3.12" in workflow
    assert "if: matrix.python-version == '3.12'" in workflow
    assert "interpreter = tkinter.Tcl()" in workflow

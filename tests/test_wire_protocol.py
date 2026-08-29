from __future__ import annotations

import pytest

from vivado_agent_mcp.vivado.project import parse_fileset_files, parse_project_state
from vivado_agent_mcp.vivado.wire import (
    HEX_LIST_PREFIX,
    HEX_ROW_PREFIX,
    decode_wire_list,
    decode_wire_row,
    encode_wire_list,
    encode_wire_row,
    tcl_wire_prelude,
)


def test_wire_list_round_trips_delimiters_unicode_and_tcl_syntax() -> None:
    values = [
        "D:/project;variant/rtl/top.sv",
        "name|with=delimiters",
        "}; exec cmd /c whoami; #",
        "中文/{braces}/$value/[command]\\\r\n",
        "",
    ]

    encoded = encode_wire_list(values)

    assert encoded.startswith(HEX_LIST_PREFIX)
    assert decode_wire_list(encoded) == values


def test_wire_row_round_trips_path_with_legacy_delimiters() -> None:
    values = {
        "path": "D:/project;variant/wave|capture.vcd",
        "type": "SystemVerilog=Source",
        "exists": "1",
    }

    encoded = encode_wire_row(values)

    assert encoded.startswith(HEX_ROW_PREFIX)
    assert decode_wire_row(encoded) == values


def test_tcl_wire_helpers_match_python_decoder() -> None:
    tkinter = pytest.importorskip("tkinter")
    try:
        interpreter = tkinter.Tcl()
    except tkinter.TclError as exc:
        pytest.skip(f"A usable Tcl runtime is unavailable: {exc}")
    interpreter.eval(tcl_wire_prelude())
    values = (
        "D:/project;variant/rtl/top.sv",
        "name|with=delimiters",
        "}; set ::vmcp_pwned 1; #",
        "中文/{braces}/$value/[command]\\",
        "",
    )

    encoded_list = str(interpreter.call("::vivado_agent_mcp_wire_list", values))
    encoded_row = str(
        interpreter.call(
            "::vivado_agent_mcp_wire_row",
            ("path", values[0], "description", values[2], "unicode", values[3]),
        )
    )

    assert decode_wire_list(encoded_list) == list(values)
    assert decode_wire_row(encoded_row) == {
        "path": values[0],
        "description": values[2],
        "unicode": values[3],
    }
    assert interpreter.eval("info exists ::vmcp_pwned") == "0"


def test_wire_decoder_rejects_malformed_versioned_payloads() -> None:
    with pytest.raises(ValueError, match="Malformed"):
        decode_wire_list(f"{HEX_LIST_PREFIX}abc")
    with pytest.raises(ValueError, match="Malformed"):
        decode_wire_row(f"{HEX_ROW_PREFIX}path")


def test_wire_decoder_can_fail_closed_in_security_sensitive_paths() -> None:
    with pytest.raises(ValueError, match="Unversioned wire list"):
        decode_wire_list("sources_1;sim_1", allow_legacy=False)
    with pytest.raises(ValueError, match="Unversioned wire row"):
        decode_wire_row("path=D:/demo/top.sv|exists=1", allow_legacy=False)


def test_project_parsers_accept_versioned_wire_values_and_legacy_values() -> None:
    project_raw = "\n".join(
        [
            "project_name=demo",
            "project_dir=D:/demo",
            f"filesets={encode_wire_list(['sources;1', 'sim_1'])}",
            f"runs={encode_wire_list(['impl|1'])}",
            f"bitstream_files={encode_wire_list(['D:/demo;variant/top.bit'])}",
        ]
    )
    file_row = encode_wire_row(
        {
            "path": "D:/demo;variant/rtl/top.sv",
            "type": "SystemVerilog|Source",
            "exists": "1",
            "managed": "0",
        }
    )

    project = parse_project_state(project_raw)
    files = parse_fileset_files(file_row)

    assert project["filesets"] == ["sources;1", "sim_1"]
    assert project["runs"] == ["impl|1"]
    assert project["artifacts"]["bitstream_files"] == ["D:/demo;variant/top.bit"]
    assert files["files"][0]["path"] == "D:/demo;variant/rtl/top.sv"
    assert files["files"][0]["file_type"] == "SystemVerilog|Source"
    assert decode_wire_list("sources_1;sim_1", allow_legacy=True) == ["sources_1", "sim_1"]

import pytest

from vivado_agent_mcp.vivado.tcl import safe_tcl, tcl_list_quote


def test_tcl_list_quote_wraps_paths_with_spaces_chinese_and_dollar() -> None:
    value = r"D:\工程 文件\top$module.xpr"

    assert tcl_list_quote(value) == r"{D:\工程 文件\top$module.xpr}"


def test_safe_tcl_substitutes_arguments_as_tcl_lists() -> None:
    command = safe_tcl(
        "open_project {project}; set_property top {top} [current_fileset]",
        {"project": r"D:\fpga demo\demo.xpr", "top": "top$rtl"},
    )

    assert command == (
        r"open_project {D:\fpga demo\demo.xpr}; "
        r"set_property top {top$rtl} [current_fileset]"
    )


def test_safe_tcl_preserves_tcl_braces_and_list_templates() -> None:
    command = safe_tcl(
        "foreach f [list {file_a} {file_b}] {{lappend rows $f}}; puts [join $rows {{;}}]",
        {"file_a": r"D:\demo\rtl top.sv", "file_b": r"D:\demo\tb.sv"},
    )

    assert command == (
        r"foreach f [list {D:\demo\rtl top.sv} {D:\demo\tb.sv}] {lappend rows $f}; "
        r"puts [join $rows {;}]"
    )


def test_safe_tcl_allows_literal_tcl_script_braces() -> None:
    command = safe_tcl(
        "set rows [list]; foreach p [get_ports -quiet *] {lappend rows [get_property NAME $p]}; puts [join $rows {;}]",
        {},
    )

    assert command == "set rows [list]; foreach p [get_ports -quiet *] {lappend rows [get_property NAME $p]}; puts [join $rows {;}]"


def test_safe_tcl_allows_uppercase_literal_tcl_braces() -> None:
    command = safe_tcl("puts {READY}; open_project {project}", {"project": r"D:\demo\demo.xpr"})

    assert command == r"puts {READY}; open_project {D:\demo\demo.xpr}"


def test_safe_tcl_rejects_missing_template_argument() -> None:
    try:
        safe_tcl("open_project {project}", {})
    except KeyError as exc:
        assert "project" in str(exc)
    else:
        raise AssertionError("safe_tcl should reject missing template arguments")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"\}; set pwned 1; #", r'"\\}; set pwned 1; #"'),
        ("balanced {braces}", r'"balanced {braces}"'),
        ('quote " dollar $value command [set pwned 1]', '{quote " dollar $value command [set pwned 1]}'),
        ("backslash-newline\\\ncontinued", r'"backslash-newline\\\ncontinued"'),
        ("D:/工程 文件/top.sv", "{D:/工程 文件/top.sv}"),
        ("tabs\tand\rnewlines\n", "{tabs\tand\rnewlines\n}"),
        ("trailing\\", r'"trailing\\"'),
    ],
)
def test_tcl_list_quote_has_deterministic_safe_encoding(value: str, expected: str) -> None:
    assert tcl_list_quote(value) == expected


def test_tcl_list_quote_round_trips_adversarial_values_when_tcl_runtime_is_available() -> None:
    try:
        import tkinter
    except ImportError as exc:
        pytest.skip(f"Python Tcl bindings are unavailable: {exc}")

    try:
        interpreter = tkinter.Tcl()
    except tkinter.TclError as exc:
        pytest.skip(f"A usable Tcl runtime is unavailable: {exc}")
    values = [
        r"\}; set pwned 1; #",
        "balanced {braces}",
        'quote " dollar $value command [set pwned 1]',
        "backslash-newline\\\ncontinued",
        "D:/工程 文件/top.sv",
    ]

    for value in values:
        interpreter.eval("unset -nocomplain pwned")
        result = interpreter.eval(f"set vmcp_value {tcl_list_quote(value)}; set vmcp_value")
        assert result == value
        assert interpreter.eval("info exists pwned") == "0"


def test_tcl_list_quote_rejects_nul_bytes() -> None:
    try:
        tcl_list_quote("bad\x00value")
    except ValueError as exc:
        assert "NUL" in str(exc)
    else:
        raise AssertionError("NUL bytes must not enter generated Tcl commands")

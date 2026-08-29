from vivado_agent_mcp.vivado.block_design import (
    add_bd_ip_cell_command,
    connect_bd_net_command,
    create_bd_port_command,
    create_block_design_command,
    generate_block_design_wrapper_command,
    open_block_design_command,
    parse_block_design_validation,
    validate_block_design_command,
)
from vivado_agent_mcp.vivado.ip import (
    configure_ip_command,
    create_ip_command,
    generate_ip_targets_command,
    ip_status_command,
    parse_ip_status,
)
from vivado_agent_mcp.vivado.wire import encode_wire_row


def test_create_ip_command_builds_vivado_ip_with_properties() -> None:
    command = create_ip_command(
        vlnv="xilinx.com:ip:xlconstant:1.1",
        module_name="const 中文",
        ip_dir=r"D:\Vivado_Mcp\test_use\ip dir",
        properties={"CONST_WIDTH": "1", "CONFIG.CONST_VAL": "1"},
    )

    assert "create_ip -vlnv {xilinx.com:ip:xlconstant:1.1}" in command
    assert "-module_name {const 中文}" in command
    assert r"-dir {D:\Vivado_Mcp\test_use\ip dir}" in command
    assert "set_property -dict [list {CONFIG.CONST_WIDTH} {1} {CONFIG.CONST_VAL} {1}]" in command
    assert "get_ips {const 中文}" in command
    assert "xci_path $xci_path" in command


def test_configure_ip_command_sets_property_dict() -> None:
    command = configure_ip_command(
        ip_name="const_0",
        properties={"CONFIG.CONST_WIDTH": "1", "CONFIG.CONST_VAL": "0"},
    )

    assert command.startswith("set ip_obj [get_ips -quiet {const_0}]")
    assert "set_property -dict [list {CONFIG.CONST_WIDTH} {1} {CONFIG.CONST_VAL} {0}]" in command
    assert "report_ip_status" in command


def test_generate_ip_targets_command_defaults_to_all() -> None:
    command = generate_ip_targets_command(ip_name="const_0", targets=None)

    assert "generate_target {all} $ip_files" in command
    assert "get_ips -quiet {const_0}" in command


def test_ip_status_parser_extracts_key_values_and_messages() -> None:
    metadata = encode_wire_row(
        {
            'name': 'const_0',
            'xci_path': 'D:/Vivado_Mcp/test_use/project/project.srcs/sources_1/ip/const_0/const_0.xci',
            'locked': '0',
            'upgrade_available': '0',
        }
    )
    raw = f"{metadata}\nreport_begin=__VMCP_IP_STATUS_BEGIN__\nCRITICAL WARNING: [IP_Flow 19-123] Example"

    result = parse_ip_status(raw)

    assert result["name"] == "const_0"
    assert result["locked"] is False
    assert result["upgrade_available"] is False
    assert result["xci_path"].endswith("const_0.xci")
    assert result["messages"]["counts"]["CRITICAL WARNING"] == 1
    assert result["wire_trust"] == "VERSIONED"


def test_ip_status_parser_marks_versioned_metadata() -> None:
    metadata = encode_wire_row(
        {
            "name": "const|0",
            "xci_path": "D:/project=variant/const|0.xci",
            "locked": "0",
            "upgrade_available": "0",
        }
    )
    raw = f"{metadata}\nreport_begin=__VMCP_IP_STATUS_BEGIN__\n"

    result = parse_ip_status(raw)

    assert result["name"] == "const|0"
    assert result["xci_path"] == "D:/project=variant/const|0.xci"
    assert result["wire_trust"] == "VERSIONED"


def test_create_block_design_command_handles_force() -> None:
    command = create_block_design_command(name="design_1", force=True)

    assert "get_bd_designs -quiet $bd_name" in command
    assert "if {[llength $open_bds] > 0} {close_bd_design $bd_name}" in command
    assert "if {[llength $old_bd_files] > 0} {remove_files $old_bd_files}" in command
    assert "create_bd_design {design_1}" in command
    assert "save_bd_design" in command


def test_block_design_name_is_tcl_quoted_in_file_lookup_and_summary() -> None:
    name = 'evil[exec calc]"; file delete -force -- C:/'
    create_command = create_block_design_command(name=name, force=True)
    open_command = open_block_design_command(name=name)

    assert f"set bd_name {{{name}}}" in create_command
    assert "set old_bd_files [get_files -quiet [format {*/%s.bd} $bd_name]]" in create_command
    assert "get_files -quiet */${bd_name}.bd" not in create_command
    assert "get_files -quiet [format {*/%s.bd} ${bd_name}]" in open_command
    assert f"get_files -quiet */{name}.bd" not in open_command
    assert f'"name={name}' not in create_command


def test_add_bd_ip_cell_command_sets_config_properties() -> None:
    command = add_bd_ip_cell_command(
        vlnv="xilinx.com:ip:xlconstant:1.1",
        cell_name="const_0",
        properties={"CONST_WIDTH": "1", "CONFIG.CONST_VAL": "1"},
    )

    assert "create_bd_cell -type ip -vlnv {xilinx.com:ip:xlconstant:1.1} {const_0}" in command
    assert "set_property -dict [list {CONFIG.CONST_WIDTH} {1} {CONFIG.CONST_VAL} {1}]" in command
    assert "[get_bd_cells {const_0}]" in command


def test_create_bd_port_command_supports_vector_ports() -> None:
    command = create_bd_port_command(
        name="led",
        direction="O",
        port_type=None,
        from_index=0,
        to_index=0,
        properties={"CONFIG.POLARITY": "ACTIVE_HIGH"},
    )

    assert "create_bd_port -dir {O} -from {0} -to {0} {led}" in command
    assert "set_property -dict [list {CONFIG.POLARITY} {ACTIVE_HIGH}] [get_bd_ports {led}]" in command


def test_connect_bd_net_command_resolves_pins_and_ports() -> None:
    command = connect_bd_net_command(source="const_0/dout", targets=["led"])

    assert "proc ::vmcp_resolve_bd_obj" in command
    assert "set source_obj [::vmcp_resolve_bd_obj {const_0/dout}]" in command
    assert "connect_bd_net $source_obj $target_objs" in command


def test_validate_block_design_parser_classifies_errors() -> None:
    raw = """
status=INVALID
raw_begin=__VMCP_BD_VALIDATE_BEGIN__
ERROR: [BD 41-758] The following clock pins are not connected
""".strip()

    result = parse_block_design_validation(raw)

    assert result["status"] == "INVALID"
    assert result["messages"]["counts"]["ERROR"] == 1


def test_validate_block_design_command_returns_keyed_payload() -> None:
    command = validate_block_design_command(bd_name="design_1")

    assert "open_bd_design $bd_file" in command
    assert "current_bd_design $bd_name" in command
    assert "Expected exactly one Block Design file" in command
    assert "validate_bd_design" in command
    assert "bd_name=$bd_name" in command
    assert "bd_file=$bd_file" in command
    assert "status=$status" in command
    assert "__VMCP_BD_VALIDATE_BEGIN__" in command


def test_generate_block_design_wrapper_command_adds_wrapper_and_sets_top() -> None:
    command = generate_block_design_wrapper_command(
        bd_name="design_1",
        wrapper_top="design_1_wrapper",
        set_top=True,
    )

    assert "make_wrapper -files $bd_file -top" in command
    assert "add_files -norecurse $wrapper_files" in command
    assert "set_property top {design_1_wrapper} [current_fileset]" in command
    assert "wrapper_files=" in command

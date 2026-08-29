from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


class BootstrapPermissionError(RuntimeError):
    """Raised when the secret-bearing bootstrap cannot be made user-private."""


_BOOTSTRAP_TEMPLATE = r"""
set ::vivado_agent_mcp_server_host __VMCP_HOST__
set ::vivado_agent_mcp_server_port __VMCP_PORT__
set ::vivado_agent_mcp_auth_secret {__VMCP_SECRET__}
set ::vivado_agent_mcp_last_sequence 0
set ::vivado_agent_mcp_protocol_version VMCP2
set ::vivado_agent_mcp_max_response_body_bytes 8388608

proc ::vivado_agent_mcp_rotr {value count} {
    set value [expr {$value & 0xffffffff}]
    return [expr {(($value >> $count) | (($value << (32 - $count)) & 0xffffffff)) & 0xffffffff}]
}

proc ::vivado_agent_mcp_sha256_hex {data} {
    set constants {
        0x428a2f98 0x71374491 0xb5c0fbcf 0xe9b5dba5 0x3956c25b 0x59f111f1 0x923f82a4 0xab1c5ed5
        0xd807aa98 0x12835b01 0x243185be 0x550c7dc3 0x72be5d74 0x80deb1fe 0x9bdc06a7 0xc19bf174
        0xe49b69c1 0xefbe4786 0x0fc19dc6 0x240ca1cc 0x2de92c6f 0x4a7484aa 0x5cb0a9dc 0x76f988da
        0x983e5152 0xa831c66d 0xb00327c8 0xbf597fc7 0xc6e00bf3 0xd5a79147 0x06ca6351 0x14292967
        0x27b70a85 0x2e1b2138 0x4d2c6dfc 0x53380d13 0x650a7354 0x766a0abb 0x81c2c92e 0x92722c85
        0xa2bfe8a1 0xa81a664b 0xc24b8b70 0xc76c51a3 0xd192e819 0xd6990624 0xf40e3585 0x106aa070
        0x19a4c116 0x1e376c08 0x2748774c 0x34b0bcb5 0x391c0cb3 0x4ed8aa4a 0x5b9cca4f 0x682e6ff3
        0x748f82ee 0x78a5636f 0x84c87814 0x8cc70208 0x90befffa 0xa4506ceb 0xbef9a3f7 0xc67178f2
    }
    set hash_values {0x6a09e667 0xbb67ae85 0x3c6ef372 0xa54ff53a 0x510e527f 0x9b05688c 0x1f83d9ab 0x5be0cd19}

    binary scan $data c* signed_bytes
    set bytes [list]
    foreach value $signed_bytes {
        lappend bytes [expr {$value & 0xff}]
    }
    set bit_length [expr {[llength $bytes] * 8}]
    lappend bytes 128
    while {([llength $bytes] % 64) != 56} {
        lappend bytes 0
    }
    for {set shift 56} {$shift >= 0} {incr shift -8} {
        lappend bytes [expr {($bit_length >> $shift) & 0xff}]
    }

    for {set offset 0} {$offset < [llength $bytes]} {incr offset 64} {
        set words [list]
        for {set index 0} {$index < 16} {incr index} {
            set byte_index [expr {$offset + ($index * 4)}]
            set word [expr {
                ([lindex $bytes $byte_index] << 24) |
                ([lindex $bytes [expr {$byte_index + 1}]] << 16) |
                ([lindex $bytes [expr {$byte_index + 2}]] << 8) |
                [lindex $bytes [expr {$byte_index + 3}]]
            }]
            lappend words [expr {$word & 0xffffffff}]
        }
        for {set index 16} {$index < 64} {incr index} {
            set previous_15 [lindex $words [expr {$index - 15}]]
            set previous_2 [lindex $words [expr {$index - 2}]]
            set sigma0 [expr {
                [::vivado_agent_mcp_rotr $previous_15 7] ^
                [::vivado_agent_mcp_rotr $previous_15 18] ^
                ($previous_15 >> 3)
            }]
            set sigma1 [expr {
                [::vivado_agent_mcp_rotr $previous_2 17] ^
                [::vivado_agent_mcp_rotr $previous_2 19] ^
                ($previous_2 >> 10)
            }]
            lappend words [expr {
                ([lindex $words [expr {$index - 16}]] + $sigma0 +
                 [lindex $words [expr {$index - 7}]] + $sigma1) & 0xffffffff
            }]
        }

        lassign $hash_values a b c d e f g h
        for {set index 0} {$index < 64} {incr index} {
            set sum1 [expr {
                [::vivado_agent_mcp_rotr $e 6] ^
                [::vivado_agent_mcp_rotr $e 11] ^
                [::vivado_agent_mcp_rotr $e 25]
            }]
            set choose [expr {($e & $f) ^ ((~$e) & $g)}]
            set temporary1 [expr {
                ($h + $sum1 + $choose + [lindex $constants $index] + [lindex $words $index]) & 0xffffffff
            }]
            set sum0 [expr {
                [::vivado_agent_mcp_rotr $a 2] ^
                [::vivado_agent_mcp_rotr $a 13] ^
                [::vivado_agent_mcp_rotr $a 22]
            }]
            set majority [expr {($a & $b) ^ ($a & $c) ^ ($b & $c)}]
            set temporary2 [expr {($sum0 + $majority) & 0xffffffff}]
            set h $g
            set g $f
            set f $e
            set e [expr {($d + $temporary1) & 0xffffffff}]
            set d $c
            set c $b
            set b $a
            set a [expr {($temporary1 + $temporary2) & 0xffffffff}]
        }

        set working_values [list $a $b $c $d $e $f $g $h]
        set updated_values [list]
        foreach current $hash_values working $working_values {
            lappend updated_values [expr {($current + $working) & 0xffffffff}]
        }
        set hash_values $updated_values
    }

    set digest ""
    foreach value $hash_values {
        append digest [format %08x [expr {$value & 0xffffffff}]]
    }
    return $digest
}

proc ::vivado_agent_mcp_hmac_sha256 {secret_hex payload} {
    binary scan [binary format H* $secret_hex] c* signed_key
    set key [list]
    foreach value $signed_key {
        lappend key [expr {$value & 0xff}]
    }
    if {[llength $key] > 64} {
        binary scan [binary format H* [::vivado_agent_mcp_sha256_hex [binary format c* $key]]] c* signed_key
        set key [list]
        foreach value $signed_key {
            lappend key [expr {$value & 0xff}]
        }
    }
    while {[llength $key] < 64} {
        lappend key 0
    }

    set inner_pad_hex ""
    set outer_pad_hex ""
    foreach value $key {
        append inner_pad_hex [format %02x [expr {($value ^ 0x36) & 0xff}]]
        append outer_pad_hex [format %02x [expr {($value ^ 0x5c) & 0xff}]]
    }
    set inner_digest [::vivado_agent_mcp_sha256_hex "[binary format H* $inner_pad_hex]$payload"]
    return [::vivado_agent_mcp_sha256_hex "[binary format H* $outer_pad_hex][binary format H* $inner_digest]"]
}

proc ::vivado_agent_mcp_secure_equal {left right} {
    if {[string length $left] != [string length $right]} {
        return 0
    }
    binary scan $left c* left_bytes
    binary scan $right c* right_bytes
    set difference 0
    foreach left_value $left_bytes right_value $right_bytes {
        set difference [expr {$difference | (($left_value & 0xff) ^ ($right_value & 0xff))}]
    }
    return [expr {$difference == 0}]
}

proc ::vivado_agent_mcp_handle {sock addr client_port} {
    fconfigure $sock -encoding utf-8 -translation lf -buffering line -blocking 1
    set request_id unknown
    set sequence 0
    set authenticated 0
    set status 1
    set result ""
    if {[catch {
        if {[eof $sock]} {
            error "empty socket"
        }
        set line [gets $sock]
        if {$line eq ""} {
            error "empty request"
        }
        set fields [split $line " "]
        if {[llength $fields] != 5} {
            error "invalid request envelope"
        }
        lassign $fields presented_protocol request_id sequence presented_hmac encoded
        if {$presented_protocol ne $::vivado_agent_mcp_protocol_version} {
            error "unsupported protocol version"
        }
        if {![regexp {^vmcp_[0-9a-f]{32}$} $request_id]} {
            error "invalid request id"
        }
        if {![string is integer -strict $sequence]} {
            error "invalid request sequence"
        }
        if {![regexp {^[0-9a-fA-F]{64}$} $presented_hmac]} {
            error "invalid request hmac"
        }
        if {([string length $encoded] % 2) != 0 || ![regexp {^[0-9a-fA-F]*$} $encoded]} {
            error "invalid encoded command"
        }
        set signed_payload "$::vivado_agent_mcp_protocol_version\n$request_id\n$sequence\n$encoded"
        set expected_hmac [::vivado_agent_mcp_hmac_sha256 $::vivado_agent_mcp_auth_secret $signed_payload]
        if {![::vivado_agent_mcp_secure_equal [string tolower $presented_hmac] $expected_hmac]} {
            error "authentication failed"
        }
        set expected_sequence [expr {$::vivado_agent_mcp_last_sequence + 1}]
        if {$sequence != $expected_sequence} {
            error "request replay or sequence mismatch"
        }
        set ::vivado_agent_mcp_last_sequence $sequence
        set authenticated 1
        set command [encoding convertfrom utf-8 [binary format H* $encoded]]
        set status [catch {uplevel #0 $command} result options]
    } handler_error]} {
        set result $handler_error
        set status 1
    }
    if {[catch {
        set result_bytes [encoding convertto utf-8 $result]
        if {[string length $result_bytes] > $::vivado_agent_mcp_max_response_body_bytes} {
            set result "VMCP_RESPONSE_BODY_TOO_LARGE: Tcl result exceeded the 8 MiB response limit"
            set status 1
            set result_bytes [encoding convertto utf-8 $result]
        }
        binary scan $result_bytes H* body_hex
        set body_length [expr {[string length $body_hex] / 2}]
        set body_sha256 [::vivado_agent_mcp_sha256_hex $result_bytes]
        set response_payload "$::vivado_agent_mcp_protocol_version\n$request_id\n$sequence\n$status\n$authenticated\n$body_length\n$body_sha256\n$body_hex"
        set response_hmac [::vivado_agent_mcp_hmac_sha256 $::vivado_agent_mcp_auth_secret $response_payload]
        puts $sock "$::vivado_agent_mcp_protocol_version $request_id STATUS $status SEQUENCE $sequence ACCEPTED $authenticated LENGTH $body_length SHA256 $body_sha256 HMAC $response_hmac"
        puts $sock $body_hex
        puts $sock "$::vivado_agent_mcp_protocol_version $request_id END"
        flush $sock
    } write_error]} {
        close $sock
        return
    }
    close $sock
}

socket -server ::vivado_agent_mcp_handle -myaddr $::vivado_agent_mcp_server_host $::vivado_agent_mcp_server_port
puts "Vivado Agent MCP Tcl server listening on $::vivado_agent_mcp_server_host:$::vivado_agent_mcp_server_port"
""".strip()


def write_bootstrap(path: Path, host: str, port: int, auth_secret: str) -> None:
    """Write a temporary authenticated Tcl TCP server bootstrap for a visible Vivado GUI."""

    if len(auth_secret) != 64 or any(character not in "0123456789abcdefABCDEF" for character in auth_secret):
        raise ValueError("auth_secret must be a 256-bit hexadecimal value")
    script = (
        _BOOTSTRAP_TEMPLATE.replace("__VMCP_HOST__", host)
        .replace("__VMCP_PORT__", str(port))
        .replace("__VMCP_SECRET__", auth_secret.lower())
    )
    path.write_text(script, encoding="utf-8")
    try:
        _restrict_bootstrap_permissions(path)
    except (BootstrapPermissionError, OSError):
        path.unlink(missing_ok=True)
        raise


def _restrict_bootstrap_permissions(path: Path, *, platform_name: str | None = None) -> None:
    platform_name = platform_name or os.name
    if platform_name == "nt":
        sid = _current_windows_sid()
        return_code = _run_icacls(path, sid)
        if return_code != 0:
            raise BootstrapPermissionError(f"Failed to restrict bootstrap ACL: icacls exit code {return_code}")
        return

    os.chmod(path, 0o600)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise BootstrapPermissionError(f"Bootstrap mode is {mode:o}; expected 600")


def _current_windows_sid() -> str:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user = 1
    error_insufficient_buffer = 122
    token = wintypes.HANDLE()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    try:
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        required_size = wintypes.DWORD()
        ctypes.set_last_error(0)
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(required_size))
        if ctypes.get_last_error() != error_insufficient_buffer or required_size.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required_size.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user,
            buffer,
            required_size.value,
            ctypes.byref(required_size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        sid_string = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = sid_string.value or ""
        finally:
            kernel32.LocalFree(sid_string)
    except OSError as exc:
        raise BootstrapPermissionError(f"Failed to resolve current Windows SID: {exc}") from exc
    finally:
        if token:
            kernel32.CloseHandle(token)
    if not sid.startswith("S-"):
        raise BootstrapPermissionError("Current Windows SID has an unexpected format")
    return sid


def _run_icacls(path: Path, sid: str) -> int:
    icacls = _windows_system_executable("icacls.exe")
    arguments = [str(icacls), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"]
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode


def _windows_system_executable(name: str) -> Path | str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        system_root = r"C:\Windows"
    candidate = Path(system_root) / "System32" / name
    return candidate if candidate.is_file() else name

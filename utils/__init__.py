import re as _re
import sys as _sys
from typing import Optional as _Optional


def device_name_to_path(device_name: str) -> str:
    return device_name.replace('-', '_')


def canon_port(port: _Optional[str]) -> _Optional[str]:
    """Canonical form of a serial port name.

    Windows: ``COM3``/``com3``/``Com3`` are equivalent — normalise to upper case.
    POSIX: pass through stripped string. ``None`` and empty input map to themselves.
    """
    if port is None:
        return None
    p = str(port).strip()
    if not p:
        return p
    if _sys.platform == "win32" or _sys.platform.startswith("win"):
        if _re.match(r"(?i)^com\d+$", p):
            return p.upper()
    return p


from .device_info import (
    parse_device_banner,
    get_root_fs_for_core,
    get_core_profile,
    CORE_PROFILES,
    SUPPORT_CORE_DEVICE_TYPES,
    CORE_ROOT_FS,
    DEFAULT_ROOT_FS,
)
from .constants import (
    CTRL_A, CTRL_B, CTRL_C, CTRL_D, CTRL_E,
    REPL_PROMPT, EOF_MARKER,
    RAW_REPL_PROMPT, SOFT_REBOOT_MSG, OK_RESPONSE, ERROR_HEADER,
    RAW_PASTE_INIT, RAW_PASTE_SUPPORTED, RAW_PASTE_NOT_SUPPORTED,
    RAW_PASTE_FALLBACK, RAW_PASTE_WINDOW_INC, RAW_PASTE_END_DATA,
    PORT_SCAN_TIMEOUT, REPL_READ_TIMEOUT,
    SERIAL_TIMEOUT, SERIAL_WRITE_TIMEOUT,
    REPL_BUFSIZE, RAW_PASTE_DEFAULT_WINDOW_SIZE,
    DEVICE_CHUNK_SIZE_DEFAULT, DEVICE_CHUNK_SIZE_EFR32MG,
    PUT_BATCH_BYTES_DEFAULT, PUT_BATCH_BYTES_EFR32MG,
    RAW_MODE_DELAY_DEFAULT, RAW_MODE_DELAY_EFR32MG,
    DEFAULT_AGENT_PORT, MAX_AGENT_PORT, AGENT_HOST,
    MAX_CONNECTIONS, AGENT_SOCKET_TIMEOUT,
    MAX_PAYLOAD_SIZE,
    HEARTBEAT_INTERVAL, ZOMBIE_CHECK_INTERVAL, IDLE_COMMAND_THRESHOLD,
    GC_THRESHOLD,
)
from .exceptions import (
    ReplxException,
    TransportError,
    ProtocolError,
    FileSystemError,
    ValidationError,
    CompilationError,
)

__all__ = [
    "device_name_to_path",
    "canon_port",
    "parse_device_banner",
    "get_root_fs_for_core",
    "get_core_profile",
    "CORE_PROFILES",
    "SUPPORT_CORE_DEVICE_TYPES",
    "CORE_ROOT_FS",
    "DEFAULT_ROOT_FS",
    "CTRL_A", "CTRL_B", "CTRL_C", "CTRL_D", "CTRL_E",
    "REPL_PROMPT", "EOF_MARKER",
    "RAW_REPL_PROMPT", "SOFT_REBOOT_MSG", "OK_RESPONSE", "ERROR_HEADER",
    "RAW_PASTE_INIT", "RAW_PASTE_SUPPORTED", "RAW_PASTE_NOT_SUPPORTED",
    "RAW_PASTE_FALLBACK", "RAW_PASTE_WINDOW_INC", "RAW_PASTE_END_DATA",
    "PORT_SCAN_TIMEOUT", "REPL_READ_TIMEOUT",
    "SERIAL_TIMEOUT", "SERIAL_WRITE_TIMEOUT",
    "REPL_BUFSIZE", "RAW_PASTE_DEFAULT_WINDOW_SIZE",
    "DEVICE_CHUNK_SIZE_DEFAULT", "DEVICE_CHUNK_SIZE_EFR32MG",
    "PUT_BATCH_BYTES_DEFAULT", "PUT_BATCH_BYTES_EFR32MG",
    "RAW_MODE_DELAY_DEFAULT", "RAW_MODE_DELAY_EFR32MG",
    "DEFAULT_AGENT_PORT", "MAX_AGENT_PORT", "AGENT_HOST",
    "MAX_CONNECTIONS", "AGENT_SOCKET_TIMEOUT",
    "MAX_PAYLOAD_SIZE",
    "HEARTBEAT_INTERVAL", "ZOMBIE_CHECK_INTERVAL", "IDLE_COMMAND_THRESHOLD",
    "GC_THRESHOLD",
    "ReplxException",
    "TransportError",
    "ProtocolError",
    "FileSystemError",
    "ValidationError",
    "CompilationError",
]
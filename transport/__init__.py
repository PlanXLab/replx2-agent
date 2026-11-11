import os

from .serial import SerialTransport
from replx_agent.utils.constants import REPL_BAUD_CANDIDATES


def repl_baud_candidates() -> list[int]:
    """Baud rates to try for a REPL: REPLX_BAUD (comma-separated) first, then the defaults."""
    rates: list[int] = []
    for item in os.environ.get("REPLX_BAUD", "").replace(";", ",").split(","):
        item = item.strip()
        if item.isdigit() and int(item) > 0:
            rates.append(int(item))
    for rate in REPL_BAUD_CANDIDATES:
        if rate not in rates:
            rates.append(rate)
    return rates


def create_transport(connection_string: str, baudrate: int = 115200, timeout: float = 1.0) -> SerialTransport:
    port = connection_string
    if connection_string.startswith("serial:"):
        port = connection_string[7:]
    
    return SerialTransport(port=port, baudrate=baudrate, timeout=timeout)


__all__ = [
    'SerialTransport',
    'create_transport',
    'repl_baud_candidates',
]

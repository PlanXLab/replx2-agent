import gc
import sys
import time
from replx_agent.utils.exceptions import TransportError

try:
    import serial
except ImportError:
    serial = None


def _require_serial() -> None:
    if serial is None:
        raise ImportError("pyserial is required. Install with: pip install pyserial")


_PORT_PRESENCE_TTL = 0.5
_port_presence_cache: dict = {"ts": 0.0, "ports": None}


def _enumerate_ports() -> set | None:
    try:
        from serial.tools.list_ports import comports
        ports = {p.device for p in comports()}
    except Exception:
        return None
    _port_presence_cache["ports"] = ports
    _port_presence_cache["ts"] = time.time()
    return ports


def _port_in(port: str, ports: set) -> bool:
    if not port:
        return False
    if port in ports:
        return True
    if sys.platform == "win32":
        from replx_agent.utils.constants import canon_port
        needle = canon_port(str(port))
        return any(canon_port(str(p)) == needle for p in ports)
    try:
        import os
        real_needle = os.path.realpath(port)
        for p in ports:
            if p == real_needle or os.path.realpath(p) == real_needle:
                return True
    except Exception:
        pass
    return False


def _port_currently_present(port: str) -> bool | None:
    """Return True/False if the OS reports ``port`` present, or None if the
    check itself failed (caller should skip/ignore in that case)."""
    now = time.time()
    ports = _port_presence_cache["ports"]
    fresh = False
    if ports is None or (now - _port_presence_cache["ts"]) > _PORT_PRESENCE_TTL:
        ports = _enumerate_ports()
        if ports is None:
            return None
        fresh = True

    if _port_in(port, ports):
        return True
    # A cached snapshot can predate a re-enumeration; confirm absence live.
    if not fresh:
        ports = _enumerate_ports()
        if ports is None:
            return None
        return _port_in(port, ports)
    return False


class SerialTransport:
    
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        _require_serial()
        self.port = port
        self.baudrate = baudrate
        if sys.platform != "win32" and timeout > 0.6:
            timeout = 0.6
        self._default_timeout = timeout
        self._serial = None
        
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=timeout,
                write_timeout=timeout,
                inter_byte_timeout=0.1
            )
            try:
                self._serial.set_buffer_size(rx_size=262144, tx_size=65536)
            except Exception:
                pass
        except serial.SerialException as e:
            raise TransportError(f"Failed to open serial port {port}: {e}") from e

    def set_baudrate(self, baudrate: int) -> None:
        try:
            self._serial.baudrate = baudrate
        except (serial.SerialException, ValueError) as e:
            raise TransportError(f"Failed to set baud rate {baudrate}: {e}") from e
        self.baudrate = baudrate
    
    def write(self, data: bytes) -> int:
        try:
            return self._serial.write(data)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Serial write error: {e}") from e
    
    def read(self, size: int = 1) -> bytes:
        try:
            return self._serial.read(size)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Serial read error: {e}") from e
    
    def read_byte(self, timeout: float = None) -> bytes:
        try:
            if timeout is not None:
                old_timeout = self._serial.timeout
                self._serial.timeout = timeout
                try:
                    return self._serial.read(1)
                finally:
                    self._serial.timeout = old_timeout
            return self._serial.read(1)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Serial read_byte error: {e}") from e
    
    def read_available(self) -> bytes:
        try:
            waiting = self._serial.in_waiting
            if waiting > 0:
                return self._serial.read(waiting)
            return b""
        except (serial.SerialException, OSError) as e:
            error_msg = str(e).lower()
            if "clearcommerror" in error_msg or "not exist" in error_msg or "cannot find" in error_msg:
                raise TransportError("Serial port disconnected (device removed or cable unplugged)") from e
            raise TransportError(f"Serial read_available error: {e}") from e

    def in_waiting(self) -> int:
        try:
            return self._serial.in_waiting
        except (serial.SerialException, OSError) as e:
            error_msg = str(e).lower()
            raise TransportError(f"Serial input status error: {error_msg}") from e
    
    def close(self) -> None:
        if self._serial:
            serial_obj = self._serial
            self._serial = None
            try:
                if serial_obj.is_open:
                    try:
                        serial_obj.cancel_read()
                    except Exception:
                        pass
                    try:
                        serial_obj.cancel_write()
                    except Exception:
                        pass
                    try:
                        serial_obj.reset_input_buffer()
                    except Exception:
                        pass
                    try:
                        serial_obj.reset_output_buffer()
                    except Exception:
                        pass
                    serial_obj.close()
            except Exception:
                pass
            finally:
                del serial_obj
                if sys.platform == 'win32':
                    gc.collect()
    
    def reset_input_buffer(self) -> None:
        if self._serial:
            self._serial.reset_input_buffer()
    
    def reset_output_buffer(self) -> None:
        if self._serial:
            self._serial.reset_output_buffer()
    
    def check_connection(self) -> bool:
        try:
            if not self._serial or not self._serial.is_open:
                return False
            if _port_currently_present(self.port) is False:
                return False
            _ = self._serial.in_waiting
            return True
        except serial.SerialException as e:
            error_msg = str(e).lower()
            if "clearcommerror" in error_msg or "not exist" in error_msg or "cannot find" in error_msg or "access is denied" in error_msg:
                return False
            return False
        except (OSError, IOError):
            return False
        except Exception:
            return False
    
    def keep_alive(self) -> None:
        if not self._serial or not self._serial.is_open:
            raise TransportError("Serial port disconnected (device removed or cable unplugged)")
        if _port_currently_present(self.port) is False:
            raise TransportError("Serial port disconnected (device removed or cable unplugged)")
        try:
            _ = self._serial.in_waiting
        except (serial.SerialException, OSError, IOError) as e:
            raise TransportError(f"Serial keep-alive failed: {e}") from e
    
    @property
    def is_open(self) -> bool:
        return self._serial.is_open if self._serial else False

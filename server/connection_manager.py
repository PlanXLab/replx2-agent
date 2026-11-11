import logging
import re
import sys
import time
import threading
from collections import deque
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Deque, List, Tuple, Hashable

from replx_agent.protocol import ReplProtocol, create_storage
from replx_agent.utils import parse_device_banner, canon_port as _shared_canon_port
from replx_agent.utils.constants import (
    CTRL_B, CTRL_C, CONNECT_DETECT_ROUNDS, CONNECT_DETECT_RETRY_DELAY,
)
from replx_agent.utils.exceptions import TransportError
from replx_agent.utils.device_info import get_root_fs_for_core

logger = logging.getLogger(__name__)


def _detect_device_info(transport, core: str, device: str = None) -> Tuple[str, str, str, str]:
    delay1 = 0.05 if sys.platform != "win32" else 0.1
    delay2 = 0.1 if sys.platform != "win32" else 0.2
    delay3 = 0.1 if sys.platform != "win32" else 0.2
    
    try:
        transport.write(b'\r' + CTRL_C)
        time.sleep(delay1)
        transport.reset_input_buffer()

        transport.write(b'\r' + CTRL_B)
        time.sleep(delay2)

        res = transport.read_available()
        if res:
            res_str = res.decode('utf-8', errors='replace') if isinstance(res, bytes) else res
            result = parse_device_banner(res_str)
            if result:
                return result

        transport.write(b'import sys; print(sys.version.split()[0])\r\n')
        time.sleep(delay3)
        ver_res = transport.read_available()
        version = "?"
        if ver_res:
            ver_str = ver_res.decode('utf-8', errors='replace') if isinstance(ver_res, bytes) else ver_res
            match = re.search(r'(\d+\.\d+(?:\.\d+)?)', ver_str)
            if match:
                version = match.group(1)

        return (version, core, device or core, "Unknown")

    except TransportError as e:
        logger.warning("Transport error during device detection: %s", e)
        return ("?", core, device or core, "Unknown")
    except (OSError, IOError) as e:
        logger.warning("I/O error during device detection: %s", e)
        return ("?", core, device or core, "Unknown")

@dataclass
class InteractiveSessionState:
    active: bool = False
    ppid: Optional[Hashable] = None
    seq: int = 0
    client_addr: Optional[tuple] = None
    echo: bool = True
    input_queue: Deque[bytes] = field(default_factory=deque)
    stop_requested: bool = False
    thread: Optional[threading.Thread] = None
    error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, ppid: Hashable, seq: int, client_addr: tuple, echo: bool = True):
        with self.lock:
            self.active = True
            self.ppid = ppid
            self.seq = seq
            self.client_addr = client_addr
            self.echo = echo
            self.input_queue.clear()
            self.stop_requested = False
            self.error = None

    def stop(self):
        thread_to_join = None
        with self.lock:
            self.active = False
            self.ppid = None
            self.seq = 0
            self.client_addr = None
            self.input_queue.clear()
            self.stop_requested = False
            thread_to_join = self.thread
            self.thread = None

        if thread_to_join and thread_to_join.is_alive() and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=1)

    def is_owner(self, ppid: Hashable) -> bool:
        with self.lock:
            if ppid is None:
                return False
            if self.ppid is None:
                return True
            return self.ppid == ppid

@dataclass
class ReplSessionState:
    active: bool = False
    ppid: Optional[Hashable] = None
    reader_future: Optional[ConcurrentFuture] = None
    reader_thread: Optional[threading.Thread] = None
    output_buffer: bytearray = field(default_factory=bytearray)
    buffer_lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, ppid: Hashable):
        self.active = True
        self.ppid = ppid
        with self.buffer_lock:
            self.output_buffer.clear()

    def stop(self):
        self.active = False
        self.ppid = None
        if self.reader_future is not None:
            self.reader_future.cancel()
            self.reader_future = None
        if self.reader_thread:
            self.reader_thread.join(timeout=1)
            self.reader_thread = None
        with self.buffer_lock:
            self.output_buffer.clear()

    def is_owner(self, ppid: Hashable) -> bool:
        if ppid is None:
            return False
        if self.ppid is None:
            return True
        return self.ppid == ppid

    def append_output(self, data: bytes):
        with self.buffer_lock:
            self.output_buffer.extend(data)

    def read_output(self) -> bytes:
        with self.buffer_lock:
            data = self.output_buffer
            self.output_buffer = bytearray()
        return bytes(data)

@dataclass
class BoardConnection:
    port: str
    repl_protocol: Optional[ReplProtocol] = None
    file_system: Any = None

    core: str = ""
    device: str = ""
    manufacturer: str = "?"
    version: str = "?"
    device_root_fs: str = "/"
    
    board_id: Optional[str] = None

    busy: bool = False
    busy_command: Optional[str] = None
    busy_session: Optional[Hashable] = None
    busy_client: Optional[tuple] = None
    last_command_time: float = field(default_factory=time.time)
    # Single state lock guards busy_* and detached_running together so that
    # callers can atomically check "not detached AND not busy" without races.
    _busy_lock: threading.Lock = field(default_factory=threading.Lock)

    detached_running: bool = False
    _drain_thread: Optional[threading.Thread] = field(default=None, repr=False)

    # Counter incremented each heartbeat when the transport fails a liveness
    # check; reset to 0 on a successful check. The heartbeat disconnects the
    # port after this reaches a threshold (see AgentServer._heartbeat_tick).
    _liveness_fail_count: int = 0
    _logical_liveness_fail_count: int = 0
    _last_logical_probe_time: float = field(default_factory=time.monotonic)

    interactive: InteractiveSessionState = field(default_factory=InteractiveSessionState)
    repl: ReplSessionState = field(default_factory=ReplSessionState)

    # --- Sentinel session ids -------------------------------------------------
    # Real client sessions are OS PIDs (>= 1). Negative ids are reserved for
    # internal callers so they can never alias with a real client.
    HEARTBEAT_SESSION_ID = -1

    # Force-release a busy that has been held longer than this without any
    # observable progress. Heartbeat applies this only when no REPL/interactive
    # session and no detached script holds the connection.
    BUSY_TIMEOUT_S = 60.0

    def is_connected(self) -> bool:
        return self.repl_protocol is not None

    def busy_snapshot(self) -> tuple[bool, Optional[str], Optional[Hashable], Optional[tuple], bool]:
        with self._busy_lock:
            return (
                self.busy,
                self.busy_command,
                self.busy_session,
                self.busy_client,
                self.detached_running,
            )
    
    def is_detached(self) -> bool:
        with self._busy_lock:
            return self.detached_running
    
    def set_detached(self, running: bool):
        with self._busy_lock:
            self.detached_running = running

    def acquire_for_command(self, session_id: Hashable, command: str,
                            client_addr: tuple = None,
                            allow_when_detached: bool = False) -> bool:
        """Try to mark the board as busy for ``session_id``.

        Concurrent acquires from the same session are rejected so that two
        in-flight commands cannot believe they each own the board (the previous
        re-entrant behaviour caused premature release when the inner command
        finished first). Returns ``True`` only if this call took the lock.

        When ``allow_when_detached`` is True (e.g. for ``reset``), the command
        may take over ownership even when the busy flag is currently held by a
        detached script, superseding that script's ownership.
        """
        with self._busy_lock:
            if self.detached_running:
                if not allow_when_detached:
                    return False
                # Detached-aware commands (e.g. reset) are allowed to take over
                # even while the detached-script holds busy=True.
            elif self.busy:
                return False
            self.busy = True
            self.busy_session = session_id
            self.busy_command = command
            self.busy_client = client_addr
            self.last_command_time = time.time()
            return True

    def acquire_for_heartbeat(self) -> bool:
        with self._busy_lock:
            if self.busy or self.detached_running or self.repl.active or self.interactive.active:
                return False
            self.busy = True
            self.busy_session = self.HEARTBEAT_SESSION_ID
            self.busy_command = 'heartbeat'
            self.busy_client = None
            return True

    def release(self, *, reset_liveness: bool = True, update_last_command_time: bool = True):
        with self._busy_lock:
            self.busy = False
            self.busy_session = None
            self.busy_command = None
            self.busy_client = None
            if update_last_command_time:
                self.last_command_time = time.time()
            if reset_liveness:
                self._liveness_fail_count = 0
                self._logical_liveness_fail_count = 0
                self._last_logical_probe_time = time.monotonic()

    def release_if_owned(self, session_id: Hashable) -> bool:
        with self._busy_lock:
            if not self.busy or self.busy_session != session_id:
                return False
            self.busy = False
            self.busy_session = None
            self.busy_command = None
            self.busy_client = None
            self.last_command_time = time.time()
            self._liveness_fail_count = 0
            self._logical_liveness_fail_count = 0
            self._last_logical_probe_time = time.monotonic()
            return True

    def set_busy_state(self, busy: bool, command: str = None, session: Hashable = None,
                       client_addr: tuple = None) -> None:
        with self._busy_lock:
            self.busy = busy
            self.busy_command = command if busy else None
            self.busy_session = session if busy else None
            self.busy_client = client_addr if busy else None
            if busy:
                self.last_command_time = time.time()

    def force_release_if_stale(self, timeout_s: float = None) -> bool:
        """Forcefully release a busy lock that has been held too long.

        Skips REPL / interactive / detached owners since those have separate
        lifecycles managed elsewhere. Returns ``True`` if a stale lock was
        cleared.
        """
        if timeout_s is None:
            timeout_s = self.BUSY_TIMEOUT_S
        with self._busy_lock:
            if not self.busy:
                return False
            if self.detached_running:
                return False
            if self.repl.active or self.interactive.active:
                return False
            if time.time() - self.last_command_time < timeout_s:
                return False
            self.busy = False
            self.busy_session = None
            self.busy_command = None
            self.busy_client = None
            return True

    def stop_detached(self):
        with self._busy_lock:
            self.detached_running = False

        if self._drain_thread and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=0.2)
        self._drain_thread = None

class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, BoardConnection] = {}
        self._connections_lock = threading.RLock()
        # Per-port events used to serialize concurrent create_serial_connection
        # calls for the same port (prevents double serial-open race).
        self._port_creating: Dict[str, threading.Event] = {}
        self._scan_lock = threading.Lock()
        self._default_port: Optional[str] = None

    @property
    def default_port(self) -> Optional[str]:
        return self._default_port

    @default_port.setter
    def default_port(self, value: Optional[str]):
        self._default_port = self._canon_port(value) if value else None

    @staticmethod
    def _is_windows() -> bool:
        return sys.platform == "win32" or sys.platform.startswith("win")

    @staticmethod
    def _canon_port(port: Optional[str]) -> Optional[str]:
        return _shared_canon_port(port)

    def _resolve_existing_key(self, port: str) -> Optional[str]:
        if port is None:
            return None
        p = self._canon_port(port)
        if p in self._connections:
            return p
        if self._is_windows() and p and re.match(r"(?i)^com\d+$", p):
            needle = p.lower()
            for k in self._connections.keys():
                if isinstance(k, str) and k.lower() == needle:
                    return k
        return None

    def get_connection(self, port: str) -> Optional[BoardConnection]:
        with self._connections_lock:
            key = self._resolve_existing_key(port)
            return self._connections.get(key) if key else None

    def get_all_connections(self) -> Dict[str, BoardConnection]:
        with self._connections_lock:
            return dict(self._connections)

    def get_connected_ports(self) -> List[str]:
        with self._connections_lock:
            return list(self._connections.keys())

    def has_connection(self, port: str) -> bool:
        with self._connections_lock:
            return self._resolve_existing_key(port) is not None

    def add_connection(self, port: str, connection: BoardConnection) -> None:
        with self._connections_lock:
            key = self._canon_port(port)
            connection.port = key
            self._connections[key] = connection

    def remove_connection(self, port: str) -> Optional[BoardConnection]:
        with self._connections_lock:
            key = self._resolve_existing_key(port)
            return self._connections.pop(key, None) if key else None

    def connection_count(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    @property
    def scan_lock(self) -> threading.Lock:
        return self._scan_lock

    def reserve_probe_port(self, port: str) -> Optional[Tuple[str, threading.Event]]:
        port_key = self._canon_port(port)
        if not port_key:
            return None
        with self._connections_lock:
            if port_key in self._connections or port_key in self._port_creating:
                return None
            event = threading.Event()
            self._port_creating[port_key] = event
            return port_key, event

    def release_probe_port(self, reservation: Tuple[str, threading.Event]) -> None:
        port_key, event = reservation
        with self._connections_lock:
            if self._port_creating.get(port_key) is event:
                self._port_creating.pop(port_key, None)
        event.set()

    def create_serial_connection(
        self,
        port: str,
        core: str = "RP2350",
        device: str = None,
        baudrate: Optional[int] = None
    ) -> Tuple[BoardConnection, Optional[str]]:
        original_port = str(port).strip() if port is not None else ""
        port_key = self._canon_port(original_port)

        # --- Phase 1: serialize opens and scan probes for this port ---
        deadline = time.monotonic() + 30.0
        transport = None
        while True:
            with self._connections_lock:
                conn = self._connections.get(port_key)
                if conn and conn.is_connected():
                    return conn, None
                wait_event = self._port_creating.get(port_key)
                if wait_event is None:
                    our_event = threading.Event()
                    self._port_creating[port_key] = our_event
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0 or not wait_event.wait(timeout=remaining):
                return None, "Connection creation timed out (concurrent attempt)"

        # --- Phase 2: we are the creator ---
        try:
            from replx_agent.transport import create_transport, repl_baud_candidates

            rates = [baudrate] if baudrate else repl_baud_candidates()
            transport = create_transport(f"serial:{original_port}", baudrate=rates[0])

            # A board that was just reset (e.g. by esptool or a USB-UART bridge's
            # DTR/RTS lines) may still be booting, so a failed sweep is retried.
            version = "?"
            current_rate = rates[0]
            for attempt in range(CONNECT_DETECT_ROUNDS):
                if attempt:
                    time.sleep(CONNECT_DETECT_RETRY_DELAY)
                for rate in rates:
                    if rate != current_rate:
                        transport.set_baudrate(rate)
                        current_rate = rate
                    version, detected_core, detected_device, manufacturer = _detect_device_info(
                        transport, core, device
                    )
                    if version != "?":
                        break
                if version != "?":
                    break
            else:
                # No rate produced a recognisable REPL; keep the primary rate as before.
                if current_rate != rates[0]:
                    transport.set_baudrate(rates[0])

            device_root_fs = get_root_fs_for_core(detected_core)
            repl_protocol = ReplProtocol(
                transport, core=detected_core, device_root_fs=device_root_fs
            )
            if version != "?":
                try:
                    result = repl_protocol.exec("import os; print(os.getcwd())")
                    if result:
                        if isinstance(result, bytes):
                            result = result.decode('utf-8', errors='ignore')
                        cwd = result.strip() if result else "/"
                    else:
                        cwd = "/"
                    if cwd == '/flash' or cwd.startswith('/flash/'):
                        device_root_fs = '/flash/'
                except Exception as exc:
                    logger.warning(
                        "Could not query filesystem root on %s; using %s from the %s profile: %s",
                        original_port,
                        device_root_fs,
                        detected_core,
                        exc,
                    )

            repl_protocol.device_root_fs = device_root_fs

            file_system = create_storage(
                repl_protocol,
                core=detected_core,
                device=detected_device,
                device_root_fs=device_root_fs
            )

            conn = BoardConnection(
                port=port_key,
                repl_protocol=repl_protocol,
                file_system=file_system,
                core=detected_core,
                device=detected_device,
                manufacturer=manufacturer,
                version=version,
                device_root_fs=device_root_fs
            )

            with self._connections_lock:
                self._connections[port_key] = conn
                self._port_creating.pop(port_key, None)

            our_event.set()  # wake any threads that raced with us
            return conn, None

        except Exception as e:
            if transport is not None:
                try:
                    transport.close()
                except Exception as close_error:
                    logger.warning(
                        "Failed to close serial port %s after connection failure: %s",
                        original_port,
                        close_error,
                    )
            with self._connections_lock:
                self._port_creating.pop(port_key, None)
            our_event.set()  # unblock waiting threads so they get the error
            return None, str(e)

    def disconnect(self, port: str) -> bool:
        key = None
        with self._connections_lock:
            key = self._resolve_existing_key(port)
            if not key:
                return False

            conn = self._connections.pop(key)

        was_detached = conn.is_detached()
        conn.stop_detached()

        # Signal _read_ex() loops to exit immediately (checked every ~1ms).
        # Without this, interactive.stop()'s join() waits the full 1s timeout
        # because the thread has no way to know it should exit.
        if conn.repl_protocol:
            try:
                conn.repl_protocol._stop_event.set()
                conn.repl_protocol.interrupt()
            except Exception as exc:
                logger.warning("Failed to interrupt REPL on %s: %s", conn.port, exc)

        if conn.repl.active:
            conn.repl.stop()
        if conn.interactive.active:
            conn.interactive.stop()

        conn.release()

        if conn.repl_protocol:
            try:
                transport = conn.repl_protocol.transport
                if transport:
                    try:
                        if not was_detached:
                            transport.write(CTRL_B)
                    except Exception as exc:
                        logger.warning("Failed to send disconnect signal to %s: %s", conn.port, exc)
                    try:
                        transport.close()
                    except Exception as exc:
                        logger.warning("Failed to close serial transport for %s: %s", conn.port, exc)
            except Exception as exc:
                logger.warning("Failed to disconnect transport for %s: %s", conn.port, exc)
            finally:
                conn.repl_protocol = None

        return True

    def disconnect_all(self):
        with self._connections_lock:
            ports = list(self._connections.keys())

        for port in ports:
            self.disconnect(port)

    def get_connection_info(self, port: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection(port)
        if not conn:
            return None

        busy, busy_command, _busy_session, _busy_client, _detached = conn.busy_snapshot()
        return {
            'port': conn.port,
            'connected': conn.is_connected(),
            'core': conn.core,
            'device': conn.device,
            'manufacturer': conn.manufacturer,
            'version': conn.version,
            'device_root_fs': conn.device_root_fs,
            'busy': busy,
            'busy_command': busy_command,
            'board_id': conn.board_id
        }

    def get_all_connection_info(self) -> List[Dict[str, Any]]:
        result = []
        with self._connections_lock:
            for port in self._connections:
                info = self.get_connection_info(port)
                if info:
                    result.append(info)
        return result

    def set_busy(self, port: str, busy: bool, command: str = None, session: int = None):
        conn = self.get_connection(port)
        if conn:
            conn.set_busy_state(busy, command=command, session=session)

    def is_busy(self, port: str) -> bool:
        conn = self.get_connection(port)
        return conn.busy_snapshot()[0] if conn else False

    def check_health(self, port: str) -> bool:
        conn = self.get_connection(port)
        if not conn or not conn.repl_protocol:
            return False

        try:
            transport = conn.repl_protocol.transport
            if not transport:
                return False
            
            if hasattr(transport, 'check_connection'):
                return transport.check_connection()
            
            return transport.is_open
        except TransportError:
            return False
        except Exception:
            return False

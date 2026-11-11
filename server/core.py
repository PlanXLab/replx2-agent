import asyncio
import contextlib
import gc
import hmac
import ipaddress
import logging
import os
import posixpath
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, Hashable

from replx_agent.utils.constants import (
    DEFAULT_AGENT_PORT, AGENT_HOST, MAX_CONNECTIONS, HEARTBEAT_INTERVAL,
    ZOMBIE_CHECK_INTERVAL, GC_THRESHOLD,
    GC_COLLECT_INTERVAL,
)
from replx_agent.utils.exceptions import TransportError
from replx_agent.commands import CmdGroups
from replx_agent.rpc.protocol import AgentProtocol
from replx_agent import __version__
from .connection_manager import ConnectionManager, BoardConnection
from .session_manager import SessionManager, Session
from .tcp import AgentTCPServer, TcpReliabilityMixin
from .command_dispatcher import (
    CommandContext,
    NON_REPL_COMMANDS,
    READ_ONLY_COMMANDS,
    SESSION_COMMANDS,
    PERSISTENT_BUSY_COMMANDS,
    STREAMING_COMMANDS,
)

from .handlers import (
    SessionCommandsMixin,
    ExecCommandsMixin,
    FilesystemCommandsMixin,
    TransferCommandsMixin,
    ReplCommandsMixin,
    I2cCommandsMixin,
    UartCommandsMixin,
    SpiCommandsMixin,
    DisconnectedError,
    FirmwareCommandsMixin,
    PackageCommandsMixin,
)

gc.set_threshold(*GC_THRESHOLD)

logger = logging.getLogger(__name__)

_FAST_POOL_WORKERS = 4
_SLOW_POOL_WORKERS = 16
_LOGICAL_PROBE_INTERVAL_S = 5.0
_LOGICAL_PROBE_TIMEOUT_S = 1.0
_LOGICAL_PROBE_FAILURE_THRESHOLD = 2

class AgentServer(
    TcpReliabilityMixin,
    SessionCommandsMixin,
    ExecCommandsMixin,
    FilesystemCommandsMixin,
    TransferCommandsMixin,
    ReplCommandsMixin,
    I2cCommandsMixin,
    UartCommandsMixin,
    SpiCommandsMixin,
    FirmwareCommandsMixin,
    PackageCommandsMixin,
):
    def __init__(self, port: int = None, host: str = None, auth_token: str = None):
        self.agent_port = port or DEFAULT_AGENT_PORT
        self.agent_host = host or AGENT_HOST
        self.auth_token = (auth_token or os.environ.get("REPLX_AGENT_TOKEN") or "").strip()
        try:
            bind_address = ipaddress.ip_address(self.agent_host.split('%', 1)[0])
        except ValueError as exc:
            raise ValueError(f"Agent bind host must be an IP address: {self.agent_host}") from exc
        if not bind_address.is_loopback and not self.auth_token:
            raise RuntimeError(
                "Binding replx-agent to a network interface requires REPLX_AGENT_TOKEN."
            )
        self.running = False

        self.connection_manager = ConnectionManager()
        self.session_manager = SessionManager()
        self._notification_lock = threading.Lock()
        self._pending_notifications: dict[Any, list[str]] = {}
        self._i2c_bus: dict = {}
        self._uart_bus: dict = {}
        self._spi_bus: dict = {}
        self._init_transfer_state()

        self._command_handlers = self._build_command_handlers()

        self._fast_executor = ThreadPoolExecutor(
            max_workers=_FAST_POOL_WORKERS,
            thread_name_prefix='agent-fast',
        )
        self._slow_executor = ThreadPoolExecutor(
            max_workers=_SLOW_POOL_WORKERS,
            thread_name_prefix='agent-slow',
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._tcp_server: AgentTCPServer | None = None
        self._tcp_thread: threading.Thread | None = None
        self._init_tcp_reliability(response_cache_max=512, last_seq_max=256)
        self._cleaned_up: bool = False
    
    @property
    def _default_port(self) -> Optional[str]:
        return self.connection_manager.default_port
    
    @_default_port.setter
    def _default_port(self, value: Optional[str]):
        self.connection_manager.default_port = value
    
    def _to_real_path(self, virtual_path: str, conn: BoardConnection = None) -> str:
        device_root_fs = posixpath.normpath(
            (conn.device_root_fs if conn else "/") or "/"
        )
        virtual_path = (virtual_path or "/").replace("\\", "/")

        if device_root_fs == "/":
            return posixpath.normpath("/" + virtual_path.lstrip("/"))

        if virtual_path == device_root_fs or virtual_path.startswith(device_root_fs + "/"):
            real_path = posixpath.normpath(virtual_path)
        else:
            real_path = posixpath.normpath(
                posixpath.join(device_root_fs, virtual_path.lstrip("/"))
            )

        if real_path != device_root_fs and not real_path.startswith(device_root_fs + "/"):
            raise ValueError("Remote path is outside the device filesystem root")
        return real_path
    
    def _to_virtual_path(self, real_path: str, conn: BoardConnection = None) -> str:
        device_root_fs = conn.device_root_fs if conn else "/"
        
        if device_root_fs == '/':
            return real_path
        
        root = device_root_fs.rstrip('/')
        if real_path == root:
            return '/'
        elif real_path.startswith(root + '/'):
            return real_path[len(root):]
        else:
            return real_path
    
    def _get_or_create_session(self, ppid: Hashable) -> Session:
        return self.session_manager.get_or_create_session(ppid)

    @staticmethod
    def _client_ip(client_addr: tuple = None) -> str:
        address = getattr(client_addr, 'address', client_addr)
        return address[0] if address else AGENT_HOST

    def _session_key(self, ppid: int, client_addr: tuple = None) -> Optional[Hashable]:
        return self.session_manager.session_key(ppid, self._client_ip(client_addr))
    
    def _get_session(self, ppid: Hashable) -> Optional[Session]:
        return self.session_manager.get_session(ppid)
    
    def _resolve_connection_for_session(self, ppid: Hashable, explicit_port: str = None) -> Optional[str]:
        return self.session_manager.resolve_port(ppid, explicit_port, self._default_port)

    def _effective_default_port(self, session=None) -> Optional[str]:
        """Return the effective default port for a session: per-session
        default takes precedence, falling back to the global default (M4).
        """
        if session is not None and getattr(session, 'default_port', None):
            return session.default_port
        return self._default_port
    
    def _cleanup_zombie_sessions(self):
        all_conns = self.connection_manager.get_all_connections()
        connected_ports = set(all_conns.keys())
        zombie_ppids = self.session_manager.cleanup_zombie_sessions(connected_ports=connected_ports)
        
        if not zombie_ppids:
            return
        
        for conn in all_conns.values():
            if conn.repl.active and conn.repl.ppid in zombie_ppids:
                conn.repl.stop()
                conn.release()
            
            if conn.interactive.active and conn.interactive.ppid in zombie_ppids:
                # Signal the blocking _read_ex loop to exit before stopping so
                # the interactive thread unblocks quickly even when the board
                # has frozen without disconnecting the USB port.
                if conn.repl_protocol:
                    try:
                        conn.repl_protocol._stop_event.set()
                        conn.repl_protocol.interrupt()
                    except Exception as exc:
                        logger.warning(
                            "Could not interrupt zombie interactive session on %s: %s",
                            conn.port,
                            exc,
                        )
                conn.interactive.stop()
                conn.release()

            busy, _busy_command, busy_session, _busy_client, _detached = conn.busy_snapshot()
            if busy and not _detached and busy_session in zombie_ppids:
                conn.release()
    
    def _get_session_info(self, current_session_id=None) -> Dict[str, Any]:
        if current_session_id is not None:
            self.session_manager.get_session(current_session_id)
        sessions_info = []
        sessions = self.session_manager.get_all_sessions()
        connections = self.connection_manager.get_all_connections()
        
        for session_id, session in sessions.items():
            session_info = {
            'ppid': session.ppid,
            'sid': session.ppid,
            'client_ip': session.client_ip,
            'session_key': f'{session.client_ip}/{session.ppid}',
            'is_current': session_id == current_session_id,
                'foreground': session.foreground,
                'backgrounds': list(session.backgrounds),
                'last_access': session.last_access,
                'default_port': session.default_port
            }
            sessions_info.append(session_info)
        
        connections_info = []
        any_detached = False
        for port, conn in connections.items():
            referencing_sessions = self.session_manager.find_sessions_using_port(port)
            busy, busy_command, _busy_session, _busy_client, is_detached_port = conn.busy_snapshot()
            if is_detached_port:
                any_detached = True
            conn_info = {
                'port': port,
                'device': conn.device,
                'core': conn.core,
                'version': conn.version,
                'manufacturer': getattr(conn, 'manufacturer', ''),
                'busy': busy or is_detached_port,
                'busy_command': busy_command or ("detached_script" if is_detached_port else None),
                'connected': conn.is_connected(),
                'sessions': referencing_sessions
            }
            connections_info.append(conn_info)
        
        return {
            'sessions': sessions_info,
            'connections': connections_info,
            'default_port': self._default_port,
            'detached_running': any_detached
        }
    
    def _set_default_port(self, port: str):
        self.connection_manager.default_port = port if port else None
    
    def _get_connection(self, port: str) -> Optional[BoardConnection]:
        return self.connection_manager.get_connection(port)
    
    def _get_active_connection(self, ppid: Hashable = None, explicit_port: str = None) -> Optional[BoardConnection]:
        if explicit_port:
            conn = self.connection_manager.get_connection(explicit_port)
            if conn:
                return conn
        
        if ppid:
            session = self.session_manager.get_session(ppid)
            if session and session.foreground:
                conn = self.connection_manager.get_connection(session.foreground)
                if conn:
                    return conn
        
        return None
    
    def _add_connection(self, port: str, connection: BoardConnection) -> None:
        current_count = self.connection_manager.connection_count()
        if current_count >= MAX_CONNECTIONS:
            raise ValueError(f"Maximum connections ({MAX_CONNECTIONS}) reached. Free some connections first.")
        self.connection_manager.add_connection(port, connection)
    
    def _remove_connection(self, port: str) -> Optional[BoardConnection]:
        return self.connection_manager.remove_connection(port)
    
    def _get_all_connections(self) -> Dict[str, BoardConnection]:
        return self.connection_manager.get_all_connections()
    
    def _create_board_connection(self, port: str, 
                                  core: str = "RP2350", device: str = None,
                                  device_root_fs: str = "/") -> BoardConnection:
        if not port:
            raise ValueError("Port is required")
        
        board_conn, error = self.connection_manager.create_serial_connection(
            port=port,
            core=core,
            device=device
        )
        
        if error:
            raise RuntimeError(f"Failed to create connection: {error}")
        
        if not board_conn:
            raise RuntimeError("Failed to create connection: unknown error")
        
        return board_conn
    
    def start(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cleaned_up = False
        logger.info("replx agent starting (PID %s)", os.getpid())
        logger.info("TCP %s:%s", self.agent_host, self.agent_port)

        try:
            loop.run_until_complete(self._async_main())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
        finally:
            self._do_cleanup_resources()
            if not loop.is_closed():
                loop.close()
            self._loop = None
            self._stop_event = None

    async def _async_main(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        self._tcp_server = AgentTCPServer((self.agent_host, self.agent_port), self)
        self.running = True
        self._tcp_thread = threading.Thread(
            target=self._tcp_server.serve_forever,
            name='replx-agent-tcp',
            daemon=True,
        )
        self._tcp_thread.start()
        logger.info("replx agent started - listening on %s:%s (TCP)", self.agent_host, self.agent_port)

        heartbeat_task = asyncio.create_task(
            self._heartbeat_coro(), name='replx-heartbeat'
        )
        try:
            await self._stop_event.wait()
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(heartbeat_task), timeout=2.0)
            self._do_cleanup_resources()

    async def _heartbeat_coro(self) -> None:
        loop = asyncio.get_running_loop()
        zombie_counter = 0
        gc_counter = 0
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            zombie_counter, gc_counter = await loop.run_in_executor(
                self._slow_executor,
                self._heartbeat_tick,
                zombie_counter,
                gc_counter,
            )

    def _heartbeat_tick(self, zombie_counter: int, gc_counter: int) -> tuple[int, int]:
        all_conns = self.connection_manager.get_all_connections()
        zombie_counter += 1
        gc_counter += 1

        if zombie_counter >= ZOMBIE_CHECK_INTERVAL:
            self._cleanup_zombie_sessions()
            zombie_counter = 0

        if gc_counter >= GC_COLLECT_INTERVAL:
            gc.collect()
            gc_counter = 0

        dead_ports = []
        for port, conn in all_conns.items():
            transport = getattr(
                getattr(conn, 'repl_protocol', None), 'transport', None
            )
            if transport is None:
                continue
            # The snapshot may be stale: an intentional disconnect can close
            # this transport (and a new connection can take the port) while we
            # iterate. That is not a device removal.
            if self.connection_manager.get_connection(port) is not conn:
                continue

            if not conn.acquire_for_heartbeat():
                continue

            should_disconnect = False
            try:
                if self.connection_manager.get_connection(port) is not conn:
                    continue
                try:
                    if hasattr(transport, 'check_connection'):
                        is_alive = bool(transport.check_connection())
                    else:
                        is_alive = bool(transport.is_open)
                except Exception:
                    is_alive = False

                if self.connection_manager.get_connection(port) is not conn:
                    continue
                if is_alive:
                    conn._liveness_fail_count = 0
                    now = time.monotonic()
                    if now - conn._last_logical_probe_time >= _LOGICAL_PROBE_INTERVAL_S:
                        conn._last_logical_probe_time = now
                        try:
                            conn.repl_protocol.probe(timeout=_LOGICAL_PROBE_TIMEOUT_S)
                            conn._logical_liveness_fail_count = 0
                        except Exception as exc:
                            conn._logical_liveness_fail_count += 1
                            should_disconnect = (
                                conn._logical_liveness_fail_count
                                >= _LOGICAL_PROBE_FAILURE_THRESHOLD
                            )
                            if should_disconnect:
                                logger.warning("REPL health probe failed on %s: %s", port, exc)
                else:
                    conn._liveness_fail_count += 1
                    should_disconnect = conn._liveness_fail_count >= 3
            finally:
                if not should_disconnect or self.connection_manager.get_connection(port) is not conn:
                    conn.release(reset_liveness=False, update_last_command_time=False)

            if should_disconnect and self.connection_manager.get_connection(port) is conn:
                dead_ports.append((port, conn))

        for port, conn in dead_ports:
            try:
                self._handle_port_disconnect(port, conn)
            except Exception:
                logger.exception("Failed to disconnect stale port %s", port)
            finally:
                conn.release(reset_liveness=False, update_last_command_time=False)

        return zombie_counter, gc_counter
    
    def _start_drain_thread(self, conn: BoardConnection):
        conn.set_detached(True)
        
        def drain_loop():
            tail_buffer = b""
            # Brief startup delay so the script can begin executing before we poll.
            for _ in range(50):
                if not conn.is_detached():
                    return
                time.sleep(0.01)

            # Maximum wall-clock time before we give up and force-release.
            DRAIN_HARD_TIMEOUT_S = 120.0
            # How long to wait without any serial data before running a
            # liveness check (transport still open? board still alive?).
            SILENCE_LIVENESS_CHECK_S = 1.0

            start_time = time.time()
            last_data_time = time.time()
            last_liveness_check_time = time.time()

            while conn.is_detached() and self.running:
                now = time.time()

                # Hard timeout: the script has been running too long without
                # returning to the REPL.  Force-release so the board manager
                # becomes usable again (user can run `replx reset` to interrupt
                # the script on the board itself).
                if now - start_time > DRAIN_HARD_TIMEOUT_S:
                    logger.warning(
                        "replx agent: drain timeout for %s (%.0fs), force-releasing detached state",
                        conn.port,
                        DRAIN_HARD_TIMEOUT_S,
                    )
                    conn.set_detached(False)
                    conn.release()
                    return

                try:
                    if conn.repl_protocol:
                        data = conn.repl_protocol.drain()
                        if data:
                            last_data_time = now
                            tail_buffer = (tail_buffer + data)[-128:]

                            # Normal completion: board returned to REPL prompt.
                            if b'\r\n>>> ' in tail_buffer or tail_buffer.endswith(b'>>> '):
                                conn.set_detached(False)
                                conn.release()
                                return

                            # Board reboot detected (hard-fault, watchdog, or
                            # explicit machine.reset()).  The MicroPython banner
                            # appears before the REPL prompt, so catching it here
                            # lets us release immediately rather than waiting for
                            # the trailing `>>> `.
                            if b'MicroPython v' in data or b'soft reboot' in data:
                                conn.set_detached(False)
                                conn.release()
                                return
                        else:
                            # No data arriving.  Periodically check whether the
                            # serial transport itself is still alive so that a USB
                            # cable pull (or hard board freeze) is detected even
                            # when no TransportError is raised proactively.
                            if (
                                now - last_data_time > SILENCE_LIVENESS_CHECK_S
                                and now - last_liveness_check_time > SILENCE_LIVENESS_CHECK_S
                            ):
                                last_liveness_check_time = now
                                transport = getattr(conn.repl_protocol, 'transport', None)
                                if transport is not None and hasattr(transport, 'check_connection'):
                                    if not transport.check_connection():
                                        conn.set_detached(False)
                                        conn.release()
                                        try:
                                            self._handle_port_disconnect(conn.port, conn)
                                        except Exception:
                                            logger.exception("Failed to disconnect stale detached port %s", conn.port)
                                        return

                except Exception:
                    conn.set_detached(False)
                    conn.release()
                    transport = getattr(conn.repl_protocol, 'transport', None) if conn.repl_protocol else None
                    still_alive = False
                    try:
                        still_alive = bool(transport and transport.check_connection())
                    except Exception:
                        still_alive = False
                    if not still_alive:
                        try:
                            self._handle_port_disconnect(conn.port, conn)
                        except Exception as exc:
                            logger.warning("Failed to disconnect stale detached port %s: %s", conn.port, exc)
                    return

                time.sleep(0.01)

        drain_thread = threading.Thread(target=drain_loop, daemon=True)
        conn._drain_thread = drain_thread
        drain_thread.start()
    
    def _stop_detached_script(self, conn: BoardConnection = None):
        if conn:
            if not conn.is_detached():
                return

            conn.stop_detached()

            if conn.repl_protocol:
                try:
                    conn.release()
                except Exception as exc:
                    logger.warning("Failed to release busy state for detached script on %s: %s", conn.port, exc)
                try:
                    conn.repl_protocol.interrupt()
                    time.sleep(0.05)
                    conn.repl_protocol.interrupt()
                    time.sleep(0.1)
                except Exception as exc:
                    logger.warning("Failed to interrupt detached script on %s: %s", conn.port, exc)
                try:
                    conn.repl_protocol.drain()
                except Exception as exc:
                    logger.warning("Failed to drain detached script output on %s: %s", conn.port, exc)
        else:
            all_conns = self.connection_manager.get_all_connections()
            for port, c in all_conns.items():
                if c.is_detached():
                    self._stop_detached_script(c)

    def _queue_disconnect_notification(self, ppids: list[Hashable], port: str) -> None:
        message = f"Serial port {port} disconnected; the connection was closed."
        with self._notification_lock:
            for ppid in ppids:
                if ppid is None:
                    continue
                notifications = self._pending_notifications.setdefault(ppid, [])
                if message not in notifications:
                    notifications.append(message)
                    if len(notifications) > 16:
                        del notifications[:-16]

    def _attach_pending_notifications(self, response: dict, ppid: Hashable) -> dict:
        if not isinstance(response, dict) or ppid is None:
            return response
        with self._notification_lock:
            notifications = self._pending_notifications.pop(ppid, [])
        if notifications:
            response['notifications'] = notifications
        return response

    def _handle_port_disconnect(
        self, port: str, conn: BoardConnection = None, *, notify_ppid: Hashable = None
    ) -> None:
        """Handle a detected physical port disconnection (USB cable pull, board reset, etc.).

        Stops all active sessions, releases all locks, disconnects the serial
        transport, and removes the connection from both the connection manager
        and every session that referenced it.  This is idempotent — calling it
        on an already-removed port is a no-op.
        """
        if conn is None:
            conn = self.connection_manager.get_connection(port)
        if conn is None:
            return
        interactive_ppid = conn.interactive.ppid if conn.interactive.active else None
        # Never tear down a newer connection that reused this port name.
        if self.connection_manager.get_connection(port) is not conn:
            return

        # Stop drain / interactive / REPL sub-threads before closing the port.
        if conn.is_detached():
            conn.stop_detached()
        if conn.repl.active:
            conn.repl.stop()
        if conn.interactive.active:
            # Signal the blocking _read_ex loop to exit so the interactive
            # thread can finish quickly before the transport is closed.
            if conn.repl_protocol:
                try:
                    conn.repl_protocol._stop_event.set()
                    conn.repl_protocol.interrupt()
                except Exception:
                    pass
            conn.interactive.stop()
        conn.release()

        # Close the serial port and remove the connection object.
        self.connection_manager.disconnect(port)
        # Unlink the port from every session so sessions don't hold stale refs.
        affected_sessions = self.session_manager.remove_connection_from_all_sessions(port)
        excluded_ppid = notify_ppid if notify_ppid is not None else interactive_ppid
        self._queue_disconnect_notification(
            [ppid for ppid in affected_sessions if ppid != excluded_ppid], port
        )

        logger.warning(
            "replx agent: port %s disconnected (device removed or cable unplugged)",
            port,
        )

    def _encode_response_safe(self, response: dict) -> bytes:
        """Encode a response, truncating overly long error/result strings.

        ``encode_message`` raises ``ValueError`` if the JSON payload exceeds
        ``MAX_PAYLOAD_SIZE``. Long errors (eg. huge tracebacks or recursive
        listings) would otherwise propagate up and silently swallow the
        response (M2 fix).
        """
        from replx_agent.utils.constants import MAX_PAYLOAD_SIZE
        try:
            return AgentProtocol.encode_message(response)
        except ValueError:
            seq = response.get('seq', 0) if isinstance(response, dict) else 0
            if isinstance(response, dict):
                err = response.get('error') or 'response too large'
                err_str = str(err)
                limit = max(256, MAX_PAYLOAD_SIZE - 1024)
                if len(err_str) > limit:
                    err_str = err_str[:limit] + '\n... [truncated]'
            else:
                err_str = 'response too large'
            fallback = AgentProtocol.create_response(seq=seq, error=err_str)
            try:
                return AgentProtocol.encode_message(fallback)
            except Exception:
                return AgentProtocol.encode_message(
                    AgentProtocol.create_response(seq=seq, error='response too large')
                )

    def _encode_ack(self, seq: int) -> bytes:
        return AgentProtocol.encode_message(AgentProtocol.create_ack(seq))

    def _send_duplicate_reply(self, client_addr: tuple, seq: int) -> None:
        """Reply to a duplicate request: replay cached response if we have
        one, otherwise just re-ACK so the client doesn't give up while the
        original handler is still running."""
        cached = self._lookup_response(client_addr, seq)
        if cached is not None:
            self._safe_send(cached, client_addr)
            return
        self._safe_send(self._encode_ack(seq), client_addr)

    def _authorize_request(self, msg: dict, client_addr: tuple) -> bool:
        if not self.auth_token:
            return True
        if msg.get('type', 'request') != 'request':
            return bool(getattr(client_addr, 'authenticated', False))
        supplied = msg.get('auth_token')
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode('utf-8'), self.auth_token.encode('utf-8')
        ):
            return False
        client_addr.authenticated = True
        return True

    def _send_auth_error(self, msg: dict, client_addr: tuple) -> None:
        seq = msg.get('seq', 0)
        self._safe_send(self._encode_ack(seq), client_addr)
        response = AgentProtocol.create_response(
            seq=seq,
            error="Authentication failed; set the matching REPLX_AGENT_TOKEN.",
        )
        self._safe_send(self._encode_response_safe(response), client_addr)

    def _handle_direct(self, msg: dict, client_addr: tuple) -> None:
        seq = msg.get('seq', 0)
        command = msg.get('command')

        if not self._authorize_request(msg, client_addr):
            self._send_auth_error(msg, client_addr)
            return

        if not self._check_and_record_seq(client_addr, seq):
            self._send_duplicate_reply(client_addr, seq)
            return

        self._safe_send(self._encode_ack(seq), client_addr)

        try:
            sid = msg.get('ppid')
            ppid = self._session_key(sid, client_addr)
            explicit_port = msg.get('port')
            args = msg.get('args', {})
            client_ip = self._client_ip(client_addr)
            ctx = CommandContext(
                connection=None,
                ppid=ppid,
                sid=sid,
                client_ip=client_ip,
                explicit_port=explicit_port,
                seq=seq,
                client_addr=client_addr,
            )
            handler, use_args = self._command_handlers[command]
            result = handler(ctx, **args) if use_args else handler(ctx)
            response = AgentProtocol.create_response(seq=seq, result=result)
        except Exception as exc:
            response = AgentProtocol.create_response(seq=seq, error=str(exc))

        response = self._attach_pending_notifications(response, self._session_key(msg.get('ppid'), client_addr))
        encoded = self._encode_response_safe(response)
        self._cache_response(client_addr, seq, encoded)
        self._safe_send(encoded, client_addr)

    def _handle_request(self, request: bytes | dict, client_addr: tuple):
        msg = None
        try:
            msg = (
                AgentProtocol.decode_message(request)
                if isinstance(request, bytes)
                else request
            )
            if not msg:
                logger.warning("Invalid message from %s", client_addr)
                return

            seq = msg.get('seq', 0)
            msg_type = msg.get('type', 'request')

            if not self._authorize_request(msg, client_addr):
                if msg_type == 'request':
                    self._send_auth_error(msg, client_addr)
                return

            if msg_type == 'input':
                ppid = msg.get('ppid')
                port = msg.get('port')
                self._handle_input(msg, client_addr, ppid, port)
                return

            if msg_type == 'stream_ack':
                self._ack_stream_completion(client_addr, seq)
                return
            if msg_type == 'transfer_end':
                self._handle_transfer_end(client_addr, msg)
                return
            if msg_type == 'transfer_abort':
                self._handle_transfer_abort(client_addr, msg)
                return

            if msg_type == 'request':
                if not self._check_and_record_seq(client_addr, seq):
                    self._send_duplicate_reply(client_addr, seq)
                    return

            defer_ack = (
                msg_type == 'request'
                and msg.get('command') in {'transfer_start', 'firmware_stage'}
            )
            if not defer_ack:
                self._safe_send(self._encode_ack(seq), client_addr)

            response = self._handle_message(msg, client_addr)

            if defer_ack and response is None:
                self._safe_send(self._encode_ack(seq), client_addr)

            if response is not None:
                response = self._attach_pending_notifications(response, self._session_key(msg.get('ppid'), client_addr))
                response_data = self._encode_response_safe(response)
                self._cache_response(client_addr, seq, response_data)
                if msg.get('command') in STREAMING_COMMANDS:
                    self.send_completion(response_data, client_addr, seq)
                else:
                    self._safe_send(response_data, client_addr)

        except Exception as e:
            try:
                seq = msg.get('seq', 0) if isinstance(msg, dict) else 0
                error_response = AgentProtocol.create_response(seq=seq, error=str(e))
                ppid = self._session_key(msg.get('ppid'), client_addr) if isinstance(msg, dict) else None
                error_response = self._attach_pending_notifications(error_response, ppid)
                error_data = self._encode_response_safe(error_response)
                self._cache_response(client_addr, seq, error_data)
                if isinstance(msg, dict) and msg.get('command') in STREAMING_COMMANDS:
                    self.send_completion(error_data, client_addr, seq)
                else:
                    self._safe_send(error_data, client_addr)
            except Exception:
                pass
    
    def _build_command_handlers(self) -> dict:
        return {
            'release': (self._cmd_release, False),
            'disconnect_port': (self._cmd_disconnect_port, True),
            'exec': (self._cmd_exec, True),
            'status': (self._cmd_status, False),
            'scan': (self._cmd_scan, True),
            'shutdown': (self._cmd_shutdown, False),
            'ping': (lambda ctx: {
                "pong": True,
                "protocol_version": 2,
                "agent_version": __version__,
                "listen_host": self.agent_host,
            }, False),
            'reset': (self._cmd_reset, False),
            'run': (self._cmd_run, True),
            'run_stop': (self._cmd_run_stop, False),
            'ls': (self._cmd_ls, True),
            'ls_recursive': (self._cmd_ls_recursive, True),
            'cat': (self._cmd_cat, True),
            'rm': (self._cmd_rm, True),
            'rmdir': (self._cmd_rmdir, True),
            'mkdir': (self._cmd_mkdir, True),
            'is_dir': (self._cmd_is_dir, True),
            'mem': (self._cmd_mem, False),
            'cp': (self._cmd_cp, True),
            'mv': (self._cmd_mv, True),
            'df': (self._cmd_df, False),
            'touch': (self._cmd_touch, True),
            'format': (self._cmd_format, False),
            'get_file': (self._cmd_get_file, True),
            'get_file_streaming': (self._cmd_get_file_streaming, True),
            'put_file': (self._cmd_put_file, True),
            'put_file_batch': (self._cmd_put_file_batch, True),
            'get_file_batch': (self._cmd_get_file_batch, True),
            'transfer_start': (self._cmd_transfer_start, True),
            'firmware_stage': (self._cmd_firmware_stage, True),
            'firmware_info': (self._cmd_firmware_info, True),
            'firmware_flash': (self._cmd_firmware_flash, True),
            'firmware_discard': (self._cmd_firmware_discard, True),
            'stat': (self._cmd_stat, True),
            'repl_enter': (self._cmd_repl_enter, False),
            'repl_exit': (self._cmd_repl_exit, False),
            'repl_write': (self._cmd_repl_write, True),
            'repl_read': (self._cmd_repl_read, False),
            'session_info': (lambda ctx: self._get_session_info(ctx.ppid), False),
            'client_disconnect': (self._cmd_client_disconnect, True),
            'session_setup': (self._cmd_session_setup, True),
            'session_disconnect': (self._cmd_session_disconnect, True),
            'session_switch_fg': (self._cmd_session_switch_fg, False),
            'run_interactive': (self._cmd_run_interactive, True),
            'i2c_bus_set': (self._cmd_i2c_bus_set, True),
            'i2c_bus_get': (self._cmd_i2c_bus_get, False),
            'i2c_bus_clear': (self._cmd_i2c_bus_clear, False),
            'uart_bus_set': (self._cmd_uart_bus_set, True),
            'uart_bus_get': (self._cmd_uart_bus_get, False),
            'uart_bus_clear': (self._cmd_uart_bus_clear, False),
            'spi_bus_set': (self._cmd_spi_bus_set, True),
            'spi_bus_get': (self._cmd_spi_bus_get, False),
            'spi_bus_clear': (self._cmd_spi_bus_clear, False),
            'pkg_search': (self._cmd_pkg_search, True),
            'pkg_download': (self._cmd_pkg_download, True),
            'pkg_install': (self._cmd_pkg_install, True),
            'pkg_clean': (self._cmd_pkg_clean, False),
            'pkg_store_status': (self._cmd_pkg_store_status, False),
            'pkg_typehints_list': (self._cmd_pkg_typehints_list, False),
            'pkg_typehints_get': (self._cmd_pkg_typehints_get, True),
        }

    def _cmd_scan(self, ctx: CommandContext, max_workers: int = 10) -> dict:
        from serial.tools.list_ports import comports
        from replx_agent.helpers.scanner import DeviceScanner

        with self.connection_manager.scan_lock:
            port_infos = list(comports())
            scanned, permission_denied = DeviceScanner.scan_serial_ports(
                max_workers=max_workers,
                exclude_ports=self.connection_manager.get_connected_ports(),
                port_infos=port_infos,
                reserve_port=self.connection_manager.reserve_probe_port,
                release_port=self.connection_manager.release_probe_port,
            )
            present_ports = {
                DeviceScanner.port_compare_key(port_info.device)
                for port_info in port_infos
            }
            connections = [
                connection
                for connection in self.connection_manager.get_all_connection_info()
                if connection.get('connected')
                and DeviceScanner.port_compare_key(connection['port']) in present_ports
            ]

        return {
            'scanned': scanned,
            'permission_denied_ports': permission_denied,
            'connections': connections,
        }

    def _cmd_disconnect_port(self, ctx: CommandContext, port: str = None) -> dict:
        target_port = port or ctx.explicit_port
        if not target_port:
            raise ValueError("Port required")

        conn = self.connection_manager.get_connection(target_port)
        if conn:
            self.connection_manager.disconnect(target_port)
        # Always remove from sessions regardless of whether the connection
        # object still existed — prevents stale foreground references when the
        # port was already removed by a concurrent _handle_port_disconnect().
        self.session_manager.remove_connection_from_all_sessions(target_port)

        return {"disconnected": target_port}

    def _update_session_connections(self, ppid: Hashable, explicit_port: str) -> None:
        session = self._get_or_create_session(ppid)
        if explicit_port and explicit_port not in [c for c in session.get_all_connections()]:
            conn_exists = self._get_connection(explicit_port) is not None
            
            if conn_exists:
                if session.foreground is None:
                    session.add_connection(explicit_port, as_foreground=True)
                else:
                    session.add_connection(explicit_port, as_foreground=False)

    def _validate_connection(self, ppid: Hashable, explicit_port: str, seq: int) -> tuple:
        active_conn = self._get_active_connection(ppid, explicit_port)
        
        if explicit_port and not active_conn:
            return None, AgentProtocol.create_response(
                seq=seq,
                error=f"Connection {explicit_port} not found. Run 'replx --port {explicit_port} setup' first."
            )
        
        if explicit_port and active_conn and not active_conn.repl_protocol:
            return None, AgentProtocol.create_response(
                seq=seq,
                error=f"Connection {explicit_port} is not properly connected."
            )
        
        return active_conn, None

    def _handle_message(self, msg: dict, client_addr: tuple = None) -> dict:
        command = msg.get('command')
        args = msg.get('args', {})
        seq = msg.get('seq', 0)
        sid = msg.get('ppid')
        client_ip = self._client_ip(client_addr)
        ppid = self.session_manager.session_key(sid, client_ip)
        explicit_port = msg.get('port')

        if any(key in args for key in ('local_path', 'script_path')):
            return AgentProtocol.create_response(
                seq=seq,
                error="Agent RPC does not accept CLI-local filesystem paths",
            )
        
        if ppid and command not in READ_ONLY_COMMANDS and command not in SESSION_COMMANDS:
            self._update_session_connections(ppid, explicit_port)
        
        if command not in NON_REPL_COMMANDS and command not in CmdGroups.DETACHED_ALLOW:
            target_port = None
            if explicit_port:
                target_port = explicit_port
            elif ppid:
                session = self.session_manager.get_session(ppid)
                if session and session.foreground:
                    target_port = session.foreground
            
            if target_port:
                target_conn = self.connection_manager.get_connection(target_port)
                if target_conn and target_conn.is_detached():
                    return AgentProtocol.create_response(
                        seq=seq,
                        error=f"Connection {target_port} is busy. A detached script is running. Use 'replx reset' to stop it first."
                    )
        
        active_conn = None
        if command not in NON_REPL_COMMANDS and ppid:
            active_conn, error_response = self._validate_connection(ppid, explicit_port, seq)
            if error_response:
                return error_response
        
        ctx = CommandContext(
            connection=active_conn,
            ppid=ppid,
            sid=sid,
            client_ip=client_ip,
            explicit_port=explicit_port,
            seq=seq,
            client_addr=client_addr
        )

        if command not in NON_REPL_COMMANDS and active_conn:
            if active_conn.repl.active and command not in CmdGroups.REPL:
                if not active_conn.repl.is_owner(ppid):
                    return AgentProtocol.create_response(
                        seq=seq,
                        error=f"Connection {active_conn.port} is busy. REPL session is active from another terminal. Exit REPL first with exit() or Ctrl+D."
                    )

        _acquired = False
        if command not in NON_REPL_COMMANDS and active_conn:
            if not active_conn.is_connected():
                return AgentProtocol.create_response(
                    seq=seq,
                    error=f"Connection {active_conn.port} was disconnected."
                )
            # REPL sub-commands (write/read/exit) bypass the busy-lock.
            # repl_enter already holds it; ownership is enforced inside each handler.
            _is_repl_subcmd = command in ('repl_write', 'repl_read', 'repl_exit')
            if _is_repl_subcmd and active_conn.repl.active and active_conn.repl.is_owner(ppid):
                pass  # bypass acquire — repl_enter holds the lock
            else:
                allow_when_detached = command in CmdGroups.DETACHED_ALLOW
                if not active_conn.acquire_for_command(
                    ppid, command, client_addr,
                    allow_when_detached=allow_when_detached,
                ):
                    _busy, busy_command, _busy_session, _busy_client, _detached = active_conn.busy_snapshot()
                    return AgentProtocol.create_response(
                        seq=seq,
                        error=f"Connection {active_conn.port} is busy. Another command ({busy_command}) is currently running. Please wait for it to complete or press Ctrl+C to cancel."
                    )
                # Track acquisition only for commands that should auto-release in finally.
                if command not in PERSISTENT_BUSY_COMMANDS and command not in STREAMING_COMMANDS:
                    _acquired = True
        
        try:
            if command == 'connect':
                connect_port = args.get('port') or explicit_port
                result = self._cmd_session_setup(
                    ctx,
                    port=connect_port,
                    core=args.get('core', 'RP2350'),
                    device=args.get('device'),
                    as_foreground=True
                )
            elif command == 'set_default':
                port_arg = args.get('port') or explicit_port
                result = self._cmd_set_default(ctx, port=port_arg)
            elif command in self._command_handlers:
                handler, use_args = self._command_handlers[command]
                if command in STREAMING_COMMANDS:
                    handler(ctx, **args) if use_args else handler(ctx)
                    return None
                result = handler(ctx, **args) if use_args else handler(ctx)
            else:
                raise ValueError(f"Unknown command: {command}")
            
            return AgentProtocol.create_response(seq=seq, result=result)
        
        except (TransportError, DisconnectedError, ConnectionError, OSError, BrokenPipeError) as e:
            if active_conn:
                failed_port = active_conn.port
                if isinstance(e, (TransportError, DisconnectedError)) or not self.connection_manager.check_health(failed_port):
                    self._handle_port_disconnect(failed_port, active_conn, notify_ppid=ppid)
                    return AgentProtocol.create_response(seq=seq, error=f"Connection {failed_port} lost: {str(e)}. Connection removed.")
                return AgentProtocol.create_response(seq=seq, error=str(e))
            else:
                return AgentProtocol.create_response(seq=seq, error=f"Connection error: {str(e)}")
        
        except Exception as e:
            return AgentProtocol.create_response(seq=seq, error=str(e))
        
        finally:
            if _acquired and active_conn:
                active_conn.release()

    def _cmd_client_disconnect(self, ctx: CommandContext, ip: str) -> dict:
        try:
            client_ip = ipaddress.ip_address(str(ip).strip()).compressed
        except ValueError as exc:
            raise ValueError(f"Invalid client IP: {ip}") from exc

        removed_sessions = self.session_manager.remove_client(client_ip)
        removed_session_ids = {session_id for session_id, _session in removed_sessions}
        for conn in self.connection_manager.get_all_connections().values():
            busy, _command, busy_session, _client, detached = conn.busy_snapshot()
            if not busy or busy_session not in removed_session_ids:
                continue
            if detached:
                self._stop_detached_script(conn)
                continue
            if conn.interactive.active and conn.interactive.ppid == busy_session:
                if conn.repl_protocol:
                    try:
                        conn.repl_protocol._stop_event.set()
                        conn.repl_protocol.interrupt()
                    except Exception as exc:
                        logger.warning(
                            "Could not interrupt disconnected client's interactive session on %s: %s",
                            conn.port,
                            exc,
                        )
                conn.interactive.stop()
            if conn.repl.active and conn.repl.ppid == busy_session:
                conn.repl.stop()
            conn.release_if_owned(busy_session)

        affected_ports = {
            port
            for _session_id, session in removed_sessions
            for port in session.get_all_connections()
        }
        released_ports = []
        for port in sorted(affected_ports):
            if self.session_manager.find_sessions_using_port(port):
                continue
            self._cmd_disconnect_port(ctx, port=port)
            released_ports.append(port)

        return {
            "client_ip": client_ip,
            "sessions_disconnected": len(removed_sessions),
            "released_ports": released_ports,
        }
    
    def cleanup(self) -> None:
        self.running = False
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and not loop.is_closed() and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        else:
            self._do_cleanup_resources()

    def _do_cleanup_resources(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True

        for conn in self.connection_manager.get_all_connections().values():
            if conn.is_detached():
                conn.stop_detached()

        for conn in self.connection_manager.get_all_connections().values():
            if conn.repl.active:
                conn.repl.stop()
            if conn.interactive.active:
                conn.interactive.stop()

        self.connection_manager.disconnect_all()
        self.session_manager.clear_all_sessions()
        self._cleanup_transfer_state()

        tcp_server = self._tcp_server
        if tcp_server is not None:
            try:
                tcp_server.shutdown()
                tcp_server.server_close()
            except Exception:
                pass
            self._tcp_server = None

        try:
            self._fast_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self._slow_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        logger.info("replx agent stopped")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run the replx board-management agent.")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_AGENT_PORT)
    parser.add_argument("--host", default=AGENT_HOST, help="IP address to bind (default: loopback).")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    server = AgentServer(port=args.port, host=args.host)
    try:
        server.start() 
    except Exception as e:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == '__main__':
    main()

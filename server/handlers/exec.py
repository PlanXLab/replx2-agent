import codecs
import logging
import os
import time
import threading
import sys
from typing import NoReturn, Optional

from replx_agent.utils import canon_port
from replx_agent.utils.constants import CTRL_C, CTRL_D, EOF_MARKER, MAX_PAYLOAD_SIZE
from replx_agent.utils.exceptions import ProtocolError, TransportError
from replx_agent.rpc.protocol import AgentProtocol
from ..command_dispatcher import CommandContext
from ..connection_manager import BoardConnection

logger = logging.getLogger(__name__)


def _find_connection_by_port(connection_manager, port: str) -> Optional[BoardConnection]:
    if not port:
        return None

    conn = connection_manager.get_connection(port)
    if conn:
        return conn

    if sys.platform.startswith("win"):
        normalized = canon_port(port)
        conn = connection_manager.get_connection(normalized)
        if conn:
            return conn

        port_lower = port.lower()
        for key, candidate in connection_manager.get_all_connections().items():
            if isinstance(key, str) and key.lower() == port_lower:
                return candidate

    return None


class ExecCommandsMixin:
    def _cmd_exec(self, ctx: CommandContext, code: str, interactive: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")
        
        result = conn.repl_protocol.exec(code)
        result_str = result.decode('utf-8', errors='replace') if isinstance(result, bytes) else result
        
        if len(result_str) > MAX_PAYLOAD_SIZE - 1000:
            result_str = result_str[:MAX_PAYLOAD_SIZE - 1100] + "\n... [output truncated]"
        
        return {"output": result_str}
    
    def _cmd_status(self, ctx: CommandContext) -> dict:
        conn = None
        if ctx.explicit_port:
            conn = _find_connection_by_port(self.connection_manager, ctx.explicit_port)
        elif ctx.ppid:
            conn = self._get_active_connection(ctx.ppid)
        
        all_connections = self.connection_manager.get_all_connections()
        any_detached = any(c.is_detached() for c in all_connections.values())

        if conn and conn.repl_protocol:
            busy, busy_command, _busy_session, _busy_client, is_this_detached = conn.busy_snapshot()
            return {
                "running": True,
                "connected": True,
                "port": conn.port,
                "device": conn.device,
                "core": conn.core,
                "manufacturer": conn.manufacturer,
                "version": conn.version,
                "device_root_fs": conn.device_root_fs,
                "in_raw_repl": conn.repl_protocol._in_raw_repl if conn.repl_protocol else False,
                "pid": os.getpid(),
                "busy": busy or is_this_detached,
                "busy_command": busy_command or ("detached_script" if is_this_detached else None),
                "detached_running": any_detached,
                "board_id": conn.board_id
            }
        
        if ctx.explicit_port:
            return {
                "running": True,
                "connected": False,
                "port": ctx.explicit_port,
                "device": "",
                "core": "",
                "manufacturer": "",
                "version": "",
                "in_raw_repl": False,
                "pid": os.getpid(),
                "busy": False,
                "busy_command": None,
                "detached_running": any_detached
            }

        if all_connections:
            default_port = getattr(self, '_default_port', None)
            if default_port and default_port in all_connections:
                first_port, first_conn = default_port, all_connections[default_port]
            else:
                first_port, first_conn = next(iter(all_connections.items()))
            busy, busy_command, _busy_session, _busy_client, is_this_detached = first_conn.busy_snapshot()
            return {
                "running": True,
                "connected": True,
                "port": first_conn.port,
                "device": first_conn.device,
                "core": first_conn.core,
                "manufacturer": first_conn.manufacturer,
                "version": first_conn.version,
                "device_root_fs": first_conn.device_root_fs,
                "in_raw_repl": first_conn.repl_protocol._in_raw_repl if first_conn.repl_protocol else False,
                "pid": os.getpid(),
                "busy": busy or is_this_detached,
                "busy_command": busy_command or ("detached_script" if is_this_detached else None),
                "detached_running": any_detached,
                "board_id": first_conn.board_id
            }
        
        return {
            "running": True,
            "connected": False,
            "port": "",
            "device": "",
            "core": "",
            "manufacturer": "",
            "version": "",
            "in_raw_repl": False,
            "pid": os.getpid(),
            "busy": False,
            "busy_command": None,
            "detached_running": False
        }
    
    def _cmd_shutdown(self, ctx: CommandContext = None) -> dict:
        self.running = False
        self.connection_manager.disconnect_all()        
        self.session_manager.clear_all_sessions()
        
        # Defer cleanup so the shutdown response is sent before the TCP listener closes.
        def _deferred_cleanup():
            time.sleep(0.2)
            self.cleanup()
        
        threading.Thread(target=_deferred_cleanup, daemon=True).start()
        
        return {"shutdown": True}
    
    def _cmd_reset(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")
        
        if conn.is_detached():
            self._stop_detached_script(conn)
        
        conn.repl_protocol.reset()
        return {"reset": True}
    
    def _handle_input(self, msg: dict, client_addr: tuple, ppid: int = None, port: str = None):
        conn = None
        if port:
            conn = self.connection_manager.get_connection(port)
        else:
            for c in self.connection_manager.get_all_connections().values():
                if c.interactive.active and c.interactive.client_addr == client_addr:
                    conn = c
                    break
            if not conn and self._default_port:
                conn = self.connection_manager.get_connection(self._default_port)
        
        if not conn:
            return  
        
        with conn.interactive.lock:
            if not conn.interactive.active:
                return  
            
            if conn.interactive.client_addr != client_addr:
                return
            
            input_data = AgentProtocol.decode_stream_data(msg)
            if input_data:
                conn.interactive.input_queue.append(input_data)
    
    def _cmd_run_interactive(self, ctx: CommandContext, script_path: str = None, 
                              script_content: str = None, echo: bool = False,
                              reset: bool = True):
        conn = ctx.connection
        seq = ctx.seq
        client_addr = ctx.client_addr
        
        def send_error(msg: str) -> NoReturn:
            if conn:
                conn.release()
            raise RuntimeError(msg)
        
        if not conn or not conn.repl_protocol:
            send_error("Not connected")

        with conn.interactive.lock:
            if conn.interactive.active:
                thread = conn.interactive.thread
                if thread is not None and thread.is_alive():
                    conn.interactive.stop_requested = True
                    try:
                        conn.repl_protocol.interrupt()
                    except Exception:
                        pass
                else:
                    conn.interactive.active = False
                    conn.interactive.thread = None

        for _ in range(20):
            with conn.interactive.lock:
                if not conn.interactive.active:
                    break
            time.sleep(0.05)

        with conn.interactive.lock:
            if conn.interactive.active:
                thread = conn.interactive.thread
                if thread is None or not thread.is_alive():
                    conn.interactive.active = False
                    conn.interactive.thread = None
                else:
                    send_error("Interactive session already active on this connection")
        
        repl = conn.repl_protocol
        if conn.is_detached():
            self._stop_detached_script(conn)
        
        if script_path:
            if not os.path.exists(script_path):
                send_error(f"Script not found: {script_path}")
            with open(script_path, 'rb') as f:
                script_data = f.read()
        elif script_content is not None:
            script_data = script_content.encode('utf-8') if isinstance(script_content, str) else script_content
        else:
            send_error("Either script_path or script_content required")
        
        conn.interactive.start(ctx.ppid, seq, client_addr, echo)
        
        thread = threading.Thread(
            target=self._run_interactive_thread,
            args=(conn, script_data, reset),
            daemon=True,
            name=f'Interactive-{conn.port}'
        )
        conn.interactive.thread = thread
        thread.start()
    
    def _safe_reset_repl(self, repl):
        repl._in_raw_repl = False
        repl.interrupt()
        time.sleep(0.05)
        repl.interrupt()
        repl._rx_pushback.clear()
        if not repl._resync_repl():
            raise ProtocolError("REPL recovery failed")
        repl._interrupt_requested = False
        repl._in_raw_repl = True
            
    def _run_interactive_thread(self, conn: BoardConnection, script_data: bytes,
                                reset: bool = True):
        client_addr = conn.interactive.client_addr
        seq = conn.interactive.seq
        
        if client_addr is None:
            return
        
        output_buffer = bytearray()
        output_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        output_finalized = False
        buffer_lock = threading.Lock()
        last_flush_time = [time.time()]
        last_stream_send_time = [time.time()]
        flush_timer_running = [True]
        BUFFER_FLUSH_SIZE = 4096
        FLUSH_INTERVAL = 0.05
        KEEPALIVE_INTERVAL = 1.0
        
        def send_stream(output: str = '', completed: bool = False, error: str = None):
            try:
                msg = {'type': 'stream', 'seq': seq, 'output': output}
                if completed:
                    msg['completed'] = True
                    msg['error'] = error
                    encoded = AgentProtocol.encode_message(msg)
                    self.send_completion(encoded, client_addr, seq)
                else:
                    self._safe_send(AgentProtocol.encode_message(msg), client_addr)
                last_stream_send_time[0] = time.time()
            except Exception:
                pass
        
        def flush_buffer(final: bool = False):
            nonlocal output_finalized
            with buffer_lock:
                if output_finalized or (not output_buffer and not final):
                    return
                data_to_send = bytes(output_buffer)
                output_buffer.clear()
                last_flush_time[0] = time.time()
                # Timer and producer flushes must decode and send in byte order.
                output = output_decoder.decode(data_to_send, final=final)
                output_finalized = final
                if output:
                    send_stream(output)
        
        first_chunk = [True]
        
        def data_consumer(chunk: bytes):
            if not chunk:
                return
            filtered = chunk.replace(EOF_MARKER, b'').replace(b'\r', b'')
            if first_chunk[0] and filtered.startswith(b'>'):
                filtered = filtered[1:]
                first_chunk[0] = False
            elif first_chunk[0]:
                first_chunk[0] = False

            if filtered.endswith(b'>'):
                filtered = filtered[:-1]
            if not filtered:
                return
            with buffer_lock:
                output_buffer.extend(filtered)
                should_flush = len(output_buffer) >= BUFFER_FLUSH_SIZE or \
                               time.time() - last_flush_time[0] >= FLUSH_INTERVAL
            if should_flush:
                flush_buffer()
        
        def flush_timer():
            while flush_timer_running[0]:
                time.sleep(FLUSH_INTERVAL)
                flush_buffer()
                # Keep interactive session alive for client-side timeout logic,
                # even when the script is running silently with no output.
                if time.time() - last_stream_send_time[0] >= KEEPALIVE_INTERVAL:
                    send_stream('')
        
        flush_thread = threading.Thread(target=flush_timer, daemon=True)
        flush_thread.start()
        input_thread = None
        input_thread_running = [False]
        needs_recovery = False
        
        try:
            if not conn or not conn.repl_protocol:
                raise RuntimeError("Connection lost")
            repl = conn.repl_protocol
            
            input_thread_running = [True]
            
            def input_handler():
                while input_thread_running[0] and not conn.interactive.stop_requested:
                    input_data = None
                    with conn.interactive.lock:
                        if conn.interactive.input_queue:
                            input_data = conn.interactive.input_queue.popleft()
                    
                    if input_data:
                        try:
                            if input_data == CTRL_C:
                                repl.interrupt()
                                repl._interrupt_requested = True
                            elif input_data == CTRL_D:
                                repl.send_eof()
                            elif input_data in (b'\n', b'\r'):
                                repl.send_raw(b'\r')
                            else:
                                repl.send_raw(input_data)
                        except Exception:
                            pass
                    time.sleep(0.01)
            
            input_thread = threading.Thread(target=input_handler, daemon=True)
            input_thread.start()
            
            try:
                if reset:
                    repl.reset()
                elif not repl._in_raw_repl:
                    repl._enter_repl()
                repl._exec(script_data, interactive=False, echo=False, detach=False, 
                          data_consumer=data_consumer)
            except ProtocolError as e:
                conn.interactive.error = str(e)
                needs_recovery = True
            finally:
                input_thread_running[0] = False
                flush_timer_running[0] = False
                flush_buffer(final=True)
            
        except Exception as e:
            input_thread_running[0] = False
            flush_timer_running[0] = False
            conn.interactive.error = str(e)
            needs_recovery = True
            flush_buffer(final=True)
        finally:
            input_thread_running[0] = False
            flush_timer_running[0] = False
            if input_thread is not None and input_thread.is_alive():
                input_thread.join(timeout=0.5)
            flush_thread.join(timeout=0.5)
            if needs_recovery and conn.repl_protocol and not conn.repl_protocol._stop_event.is_set():
                try:
                    self._safe_reset_repl(conn.repl_protocol)
                except (ProtocolError, TransportError, OSError, ConnectionError) as exc:
                    if not self.connection_manager.check_health(conn.port):
                        conn.interactive.error = f"{conn.interactive.error}; REPL recovery failed: {exc}. Connection removed."
                        self._handle_port_disconnect(conn.port, conn)
            error = conn.interactive.error
            conn.interactive.stop()
            conn.release()
            self._last_command_time = time.time()
            send_stream(completed=True, error=error)
    
    def _cmd_run_stop(self, ctx: CommandContext) -> dict:
        conn = ctx.connection

        # RUN_STOP is in NON_REPL_COMMANDS so ctx.connection is always None.
        # Resolve the connection directly via explicit_port or active interactive session.
        if conn is None:
            if ctx.explicit_port:
                conn = _find_connection_by_port(self.connection_manager, ctx.explicit_port)
            if conn is None:
                # Find any connection that has an active interactive session owned by this ppid
                for c in self.connection_manager.get_all_connections().values():
                    if c.interactive.active and c.interactive.is_owner(ctx.ppid):
                        conn = c
                        break

        if not conn:
            return {"stopped": False, "reason": "Not connected"}
        
        with conn.interactive.lock:
            if not conn.interactive.active:
                return {"stopped": False, "reason": "No active session on this connection"}
            
            if ctx.ppid is None or (
                conn.interactive.ppid is not None and conn.interactive.ppid != ctx.ppid
            ):
                return {"stopped": False, "reason": "Not the session owner"}
            
            conn.interactive.stop_requested = True
            
            if conn.repl_protocol:
                try:
                    conn.repl_protocol._interrupt_requested = True
                    conn.repl_protocol._stop_event.set()
                    conn.repl_protocol.interrupt()
                    time.sleep(0.05)
                except Exception as exc:
                    logger.warning("Failed to interrupt interactive session on %s: %s", conn.port, exc)
                try:
                    conn.repl_protocol.interrupt()
                except Exception as exc:
                    logger.warning("Failed to retry interrupt for interactive session on %s: %s", conn.port, exc)
        
        for _ in range(40):
            time.sleep(0.05)
            with conn.interactive.lock:
                if not conn.interactive.active:
                    return {"stopped": True}
        
        conn.interactive.stop()
        conn.release()
        if not self.connection_manager.check_health(conn.port):
            self._handle_port_disconnect(conn.port, conn)
            return {"stopped": True, "disconnected": True,
                    "reason": "Device disconnected during stop"}
        return {"stopped": True}
    
    def _cmd_run(self, ctx: CommandContext, script_path: str = None, script_content: str = None, detach: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")
        
        repl = conn.repl_protocol
        
        if conn.is_detached():
            self._stop_detached_script(conn)
        
        repl.reset()
        
        time.sleep(0.1)
        
        if script_path:
            if not os.path.exists(script_path):
                raise RuntimeError(f"Script not found: {script_path}")
            with open(script_path, 'rb') as f:
                script_data = f.read()
            display_name = script_path
        elif script_content is not None:
            script_data = script_content.encode('utf-8') if isinstance(script_content, str) else script_content
            display_name = "<inline>"
        else:
            raise RuntimeError("Either script_path or script_content required")
        
        if detach:
            conn.set_busy_state(True, command='detached_script', session=ctx.ppid, client_addr=ctx.client_addr)

            try:
                try:
                    repl._leave_repl()
                except Exception:
                    pass
                repl._in_raw_repl = False
                
                repl.interrupt()
                time.sleep(0.05)
                repl.interrupt()
                time.sleep(0.05)
                repl.exit_paste_mode()
                time.sleep(0.1)
                repl.drain()
                
                repl.enter_paste_mode()
                time.sleep(0.2)
                repl.drain()
                
                script_str = script_data.decode('utf-8', errors='replace')
                for line in script_str.split('\n'):
                    repl.send_raw(line.encode('utf-8') + b'\r')
                    time.sleep(0.01)
                
                time.sleep(0.1)
                repl.send_eof()
                
                self._start_drain_thread(conn)
                return {"run": True, "script": display_name, "detached": True}
                
            except Exception as e:
                conn.set_detached(False)
                conn.release()
                raise RuntimeError(f"Script send failed: {e}")
        else:
            try:
                script_code = script_data.decode('utf-8', errors='replace')
                output = repl.exec(script_code)
                if isinstance(output, bytes):
                    output = output.decode('utf-8', errors='replace')
                return {"run": True, "script": display_name, "output": output}
            except Exception as e:
                raise RuntimeError(f"Script execution failed: {e}")

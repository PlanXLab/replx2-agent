import sys
import time
import struct
import textwrap
import threading
from contextlib import contextmanager
from typing import Callable, Optional

from replx_agent.utils.exceptions import ProtocolError, TransportError
from replx_agent.utils.constants import (
    CTRL_A, CTRL_B, CTRL_C, CTRL_D, CTRL_E,
    EOF_MARKER,
    RAW_REPL_PROMPT, SOFT_REBOOT_MSG, OK_RESPONSE,
    RAW_PASTE_INIT, RAW_PASTE_SUPPORTED, RAW_PASTE_NOT_SUPPORTED,
    RAW_PASTE_WINDOW_INC, RAW_PASTE_END_DATA,
    RAW_PASTE_DEFAULT_WINDOW_SIZE,
    RAW_PASTE_SAFE_BAUD, RAW_PASTE_HIGH_BAUD_MIN_COMMAND_SIZE,
    ERROR_HEADER,
)
from replx_agent.utils.device_info import get_core_profile, normalize_core
from replx_agent.transport import create_transport
from replx_agent.terminal import (
    IS_WINDOWS, CR, LF,
    flush_outbuf as _flush_outbuf,
    stdout_write_bytes as _stdout_write_bytes,
    getch, putch, getch_nonblock,
)

_RAW_PASTE_MIN_COMMAND_SIZE = 4 * 1024


class ReplProtocol:
    _active_instance = None
    _sigint_handler_installed = False
        
    def interrupt(self) -> None:
        self.transport.write(CTRL_C)
    
    def send_eof(self) -> None:
        self.transport.write(CTRL_D)
    
    def send_raw(self, data: bytes) -> None:
        self.transport.write(data)
    
    def drain(self) -> bytes:
        return self.transport.read_available()
    
    def enter_paste_mode(self) -> None:
        self.transport.write(CTRL_E)
    
    def exit_paste_mode(self) -> None:
        self.transport.write(CTRL_B)
    
    def in_waiting(self) -> int:
        return self.transport.in_waiting()
    
    def read_bytes(self, n: int) -> bytes:
        return self.transport.read(n)
        
    def __init__(self, connection_string, baudrate:int=115200, core:str="RP2350", device_root_fs:str="/"):
        from replx_agent.transport import SerialTransport
        
        try:
            if isinstance(connection_string, SerialTransport):
                self.transport = connection_string
            else:
                self.transport = create_transport(connection_string, baudrate=baudrate)
            self._stop_event = threading.Event()
            self._follow_thread = None
        except Exception as e:
            raise ProtocolError(f"failed to open {connection_string} ({e})")
        
        self._rx_pushback = bytearray()
        self._raw_paste_supported = None
        self._raw_paste_window_size = RAW_PASTE_DEFAULT_WINDOW_SIZE
        
        self._skip_error_output = False
        self._error_header_buf = b""
        
        self.core = normalize_core(core)
        self.device_root_fs = device_root_fs
        
        _profile = get_core_profile(core)
        self._DEVICE_CHUNK_SIZES = _profile['chunk_size']
        self._RAW_MODE_DELAY = _profile['raw_mode_delay']
        self._PUT_BATCH_BYTES = _profile['put_batch_bytes']

        baud = getattr(self.transport, 'baudrate', None)
        if not isinstance(baud, int) or baud <= 0:
            baud = RAW_PASTE_SAFE_BAUD
        self._raw_paste_min_command_size = (
            _RAW_PASTE_MIN_COMMAND_SIZE if baud <= RAW_PASTE_SAFE_BAUD
            else RAW_PASTE_HIGH_BAUD_MIN_COMMAND_SIZE
        )
        
        self._init_repl()
        self._install_sigint_handler()

    @classmethod
    def _install_sigint_handler(cls):
        if IS_WINDOWS and not cls._sigint_handler_installed:
            import signal
            
            def win_sigint_handler(signum, frame):
                if cls._active_instance is None:
                    raise KeyboardInterrupt
                cls._active_instance.request_interrupt()
            
            try:
                signal.signal(signal.SIGINT, win_sigint_handler)
                cls._sigint_handler_installed = True
            except Exception:
                pass
    
    def _reset_error_filter(self):
        self._skip_error_output = False
        self._error_header_buf = b""

    def _init_repl(self):
        self._interrupt_requested = False
        self._session_depth = 0
        self._in_raw_repl = False
        self._repl_prompt_detected = False

    def _read(self, n:int=1) -> bytes:
        if self._rx_pushback:
            if len(self._rx_pushback) <= n:
                b = bytes(self._rx_pushback)
                self._rx_pushback.clear()
                rem = n - len(b)
                if rem > 0:
                    b += self.transport.read(rem)
            else:
                b = bytes(self._rx_pushback[:n])
                del self._rx_pushback[:n]
        else:
            b = self.transport.read(n)

        return b
    
    def _read_ex(self, min_num_bytes:int, ending:bytes, timeout:float=5.0,
                data_consumer:Optional[Callable[[bytes], None]]=None) -> bytes:
        start = time.monotonic()
        deadline = (start + timeout) if timeout > 0 else None
        
        streaming_mode = (timeout == 0 and data_consumer is not None)
        
        pat = ending
        m = len(pat)
        pi = [0] * m
        k = 0
        for i in range(1, m):
            while k > 0 and pat[k] != pat[i]:
                k = pi[k-1]
            if pat[k] == pat[i]:
                k += 1
            pi[i] = k

        matched = 0
        crash_tail = b''
        
        tail_size = m + 8
        tail_buffer = bytearray(tail_size)
        tail_len = 0 
        data = bytearray()
        
        def _call_consumer(chunk: bytes):
            if data_consumer and chunk:
                data_consumer(chunk)

        def _update_tail(chunk: bytes):
            nonlocal tail_len
            chunk_len = len(chunk)
            if chunk_len >= tail_size:
                tail_buffer[:] = chunk[-tail_size:]
                tail_len = tail_size
            elif tail_len + chunk_len <= tail_size:
                tail_buffer[tail_len:tail_len + chunk_len] = chunk
                tail_len += chunk_len
            else:
                keep = tail_size - chunk_len
                tail_buffer[:keep] = tail_buffer[tail_len - keep:tail_len]
                tail_buffer[keep:keep + chunk_len] = chunk
                tail_len = tail_size

        def _feed_chunk(chunk: bytes) -> int:
            nonlocal matched, crash_tail

            if not chunk:
                return -1
            if ending == EOF_MARKER and self.core.startswith('ESP32'):
                crash_tail = (crash_tail + chunk)[-8192:]
                if (b'\r\nGuru Meditation Error:' in crash_tail
                        or b'\r\nA fatal error occurred. The crash dump printed below' in crash_tail):
                    raise ProtocolError(
                        "ESP32 firmware crashed (native panic/core dump), not a REPL timeout. "
                        "Power-cycle the board. On P4/C6 check the MicroPython C6_WIFI build, "
                        "chip revision and ESP-Hosted coprocessor firmware; Python retries cannot fix this."
                    )

            clen = len(chunk)
            end_at = -1

            if matched == 0:
                pos = chunk.find(pat)
                if pos >= 0:
                    end_at = pos + m - 1
                    matched = m
                else:
                    tail_start = max(0, clen - m + 1)
                    for idx in range(tail_start, clen):
                        b = chunk[idx]
                        while matched > 0 and pat[matched] != b:
                            matched = pi[matched - 1]
                        if pat[matched] == b:
                            matched += 1
                            if matched == m:
                                end_at = idx
                                break
            else:
                remaining = m - matched
                if clen >= remaining and chunk[:remaining] == pat[matched:]:
                    end_at = remaining - 1
                    matched = m
                else:
                    for idx in range(clen):
                        b = chunk[idx]
                        while matched > 0 and pat[matched] != b:
                            matched = pi[matched - 1]
                        if pat[matched] == b:
                            matched += 1
                            if matched == m:
                                end_at = idx
                                break

            if streaming_mode:
                chunk_to_process = chunk[:end_at+1] if end_at >= 0 else chunk
                _call_consumer(chunk_to_process)
                _update_tail(chunk_to_process)
                return end_at + 1 if end_at >= 0 else -1
            else:
                if end_at >= 0:
                    data.extend(chunk[:end_at+1])
                    _call_consumer(chunk[:end_at+1])
                    return end_at + 1
                
                data.extend(chunk)
                _call_consumer(chunk)
                return -1

        def _read_some(max_n: int) -> bytes:
            if self._rx_pushback:
                # Return buffered bytes without blocking for the rest of max_n;
                # self._read() would wait a full serial timeout for them.
                b = bytes(self._rx_pushback[:max(1, max_n)])
                del self._rx_pushback[:len(b)]
                return b
            try:
                avail = self.transport.read_available()
                if avail:
                    return avail
            except TransportError:
                raise

            if streaming_mode:
                return b''
            return self._read(1)

        def _get_result() -> bytes:
            if streaming_mode:
                return bytes(tail_buffer[:tail_len])
            else:
                return bytes(data)

        try:
            if min_num_bytes > 0:
                chunk = _read_some(min_num_bytes)
                if chunk:
                    pos = _feed_chunk(chunk)
                    if pos >= 0:
                        tail = chunk[pos:]
                        if tail:
                            self._rx_pushback[:] = tail + self._rx_pushback
                        return _get_result()

            last_activity = time.monotonic()
            last_keepalive = time.monotonic()
            keepalive_interval = 30.0

            while True:
                if self._stop_event.is_set():
                    break
                
                if deadline is not None and time.monotonic() >= deadline:
                    break
                
                now = time.monotonic()
                if streaming_mode and (now - last_keepalive) >= keepalive_interval:
                    if hasattr(self.transport, 'keep_alive'):
                        self.transport.keep_alive()
                    last_keepalive = now

                waiting = self.transport.in_waiting()
                if waiting <= 0 and len(self._rx_pushback) > 0:
                    waiting = 1

                if waiting > 0:
                    want = min(4096, max(256, waiting))
                    chunk = _read_some(want)
                    if chunk:
                        pos = _feed_chunk(chunk)
                        last_activity = time.monotonic()
                        if pos >= 0:
                            tail = chunk[pos:]
                            if tail:
                                self._rx_pushback[:] = tail + self._rx_pushback
                            return _get_result()
                    else:
                        time.sleep(0.001)
                else:
                    time.sleep(0.001)

                    if timeout > 0:
                        idle_limit = max(timeout * 2, 10)
                        if (time.monotonic() - last_activity) > idle_limit:
                            break

            return _get_result()

        except TransportError:
            if not self._stop_event.is_set():
                raise
            return _get_result()
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"REPL read failed while waiting for {ending!r}: {exc}") from exc


    def _enter_repl(self, timeout: float = 5.0, attempts: int = 4):
        self._in_raw_repl = False
        self._rx_pushback.clear()
        delay = 0.1 
        
        for attempt in range(1, attempts + 1):
            try:
                self.transport.reset_input_buffer()
                time.sleep(0.05)
                self.transport.reset_output_buffer()
                time.sleep(0.05)
            except Exception:
                pass

            self.transport.write(b'\r' + CTRL_C + CTRL_C)
            time.sleep(delay)
            
            try:
                self.transport.reset_input_buffer()
                time.sleep(0.05)
            except Exception:
                pass
            
            self.transport.write(b'\r' + CTRL_A)

            try:
                data = self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=timeout)
                if not data.endswith(RAW_REPL_PROMPT[:-1]):
                    raise ProtocolError('could not enter raw repl')
                self._in_raw_repl = True
                return
            except ProtocolError:
                try:
                    self.transport.write(b'\r' + CTRL_B) 
                    time.sleep(0.15)
                    try:
                        self.transport.reset_input_buffer()
                    except Exception:
                        pass
                except Exception:
                    pass
                
                time.sleep(0.2 * attempt) 
                continue
        
        raise ProtocolError('could not enter raw repl')

    def probe(self, timeout: float = 1.0) -> bool:
        if self._session_depth:
            raise ProtocolError("Cannot probe the REPL during an active session")
        if self._stop_event.is_set():
            raise TransportError("REPL is stopping")
        if self._rx_pushback or self.transport.in_waiting() > 0:
            return True

        try:
            self._enter_repl(timeout=timeout, attempts=1)
            self._leave_repl()
            return True
        except (ProtocolError, TransportError, OSError):
            self._in_raw_repl = False
            self._rx_pushback.clear()
            raise

    def _soft_reset(self):
        timeout = 3
        try:
            self.transport.write(CTRL_C)
            time.sleep(0.1)
            self.transport.write(CTRL_C)
            time.sleep(0.1)
            self.transport.reset_input_buffer()
            
            self.transport.write(CTRL_A)
            time.sleep(0.1)
            self.transport.reset_input_buffer()
            
            self.transport.write(CTRL_D)
            data = self._read_ex(1, SOFT_REBOOT_MSG, timeout=timeout)
            if not data.endswith(SOFT_REBOOT_MSG):
                raise ProtocolError('soft reset failed')
            
            data = self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=timeout)
            if not data.endswith(RAW_REPL_PROMPT[:-1]):
                raise ProtocolError('soft reset failed')
        except Exception:
            raise

    def _resync_repl(self):
        try:
            self.transport.write(b'\r' + CTRL_B)
            time.sleep(0.04)
            self.transport.reset_input_buffer()
        except Exception:
            pass

        try:
            self.transport.write(b'\r' + CTRL_A)
            got = self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=1)
            if got.endswith(RAW_REPL_PROMPT[:-1]):
                return True
        except Exception:
            pass
        return False

    def _leave_repl(self):
        self.transport.write(b'\r' + CTRL_B) 
        self._in_raw_repl = False

    @contextmanager
    def session(self):
        need_enter = self._session_depth == 0
        if need_enter:
            self._enter_repl()
            self._raw_paste_supported = None
        self._session_depth += 1
        try:
            yield
        finally:
            self._session_depth = max(0, self._session_depth - 1)
            if need_enter:
                self._leave_repl()

    def _enter_raw_paste_mode(self) -> bool:
        if not get_core_profile(self.core)['raw_paste']:
            return False
        # Only a negative result may be cached: every raw-paste exec must
        # start with its own RAW_PASTE_INIT handshake.
        if self._raw_paste_supported is False:
            return False
        
        self.transport.write(RAW_PASTE_INIT)

        start = time.monotonic()
        timeout = 0.5
        response = b''
        while len(response) < 2 and (time.monotonic() - start) < timeout:
            waiting = self.transport.in_waiting()
            if waiting > 0:
                response += self._read(min(2 - len(response), waiting))
            else:
                time.sleep(0.01)

        if len(response) < 2:
            self._raw_paste_supported = False
            if not self._resync_repl():
                raise ProtocolError("Timed out negotiating raw-paste mode and could not restore the REPL")
            raise ProtocolError("Timed out negotiating raw-paste mode")

        if response == RAW_PASTE_SUPPORTED:
            window_bytes = self._read(2)
            if len(window_bytes) != 2:
                self.transport.write(CTRL_D)
                self._read_ex(1, b'>', timeout=1)
                self._raw_paste_supported = False
                raise ProtocolError("Incomplete raw-paste window-size response")

            window_size = struct.unpack('<H', window_bytes)[0]
            if window_size == 0:
                self.transport.write(CTRL_D)
                self._read_ex(1, b'>', timeout=1)
                self._raw_paste_supported = False
                raise ProtocolError("Invalid raw-paste window size: 0")

            self._raw_paste_window_size = window_size
            self._raw_paste_supported = True
            return True

        if response == RAW_PASTE_NOT_SUPPORTED:
            self._raw_paste_supported = False
            return False

        if response == b'ra':
            self._read_ex(1, b'>', timeout=1)
            self._raw_paste_supported = False
            return False

        self._raw_paste_supported = False
        if not self._resync_repl():
            raise ProtocolError(f"Unexpected raw-paste response {response!r}; could not restore the REPL")
        raise ProtocolError(f"Unexpected raw-paste response {response!r}")

    def _follow_task(self, echo: bool):
        try:
            while not self._stop_event.is_set():
                try:
                    ch = getch_nonblock()
                    if ch is None:
                        time.sleep(0.005)
                        continue
                    
                    if not ch:
                        continue
                    
                    if ch == CTRL_C:
                        self.request_interrupt()
                        time.sleep(0.1)
                        return
                    
                    if ch == CTRL_D:
                        try:
                            self.transport.write(CTRL_D)
                        except Exception:
                            pass
                        time.sleep(0.08)
                        return
                    
                    if echo:
                        putch(ch)
                    
                    self.transport.write(CR if ch == LF else ch)
                except Exception:
                    pass
        finally:
            pass

    def _exec_raw_paste(self, command: bytes, data_consumer: Optional[Callable[[bytes], None]] = None) -> tuple[bytes, bytes]:
        remaining_window = self._raw_paste_window_size
        bytes_sent = 0
        command_len = len(command)
        aborted = False
        
        while bytes_sent < command_len:
            while remaining_window <= 0 or self.transport.in_waiting() > 0:
                fc_byte = self._read(1)
                if fc_byte == RAW_PASTE_WINDOW_INC:
                    remaining_window += self._raw_paste_window_size
                elif fc_byte == RAW_PASTE_END_DATA:
                    self.transport.write(CTRL_D)
                    aborted = True
                    break
                elif fc_byte:
                    raise ProtocolError(
                        f"Unexpected raw-paste flow-control byte: {fc_byte!r}"
                    )

            if aborted:
                break

            chunk_size = min(remaining_window, command_len - bytes_sent)
            chunk = command[bytes_sent:bytes_sent + chunk_size]
            self.transport.write(chunk)
            bytes_sent += chunk_size
            remaining_window -= chunk_size
        
        if not aborted and bytes_sent == command_len:
            self.transport.write(CTRL_D)
        
        ack = self._read_ex(1, EOF_MARKER, timeout=5)
        if not ack.endswith(EOF_MARKER):
            raise ProtocolError("Raw-paste compilation acknowledgment timeout")
        
        stdout_data = self._read_ex(1, EOF_MARKER, timeout=0, data_consumer=data_consumer)
        if stdout_data.endswith(EOF_MARKER):
            stdout_data = stdout_data[:-1]
        
        stderr_data = self._read_ex(1, EOF_MARKER, timeout=5)
        if stderr_data.endswith(EOF_MARKER):
            stderr_data = stderr_data[:-1]

        # Leave the trailing raw-REPL '>' for the next command's prompt check.
        return (stdout_data, stderr_data)

    def _exec(self, command:str|bytes, interactive:bool=False, echo:bool=False, detach:bool=False, 
              data_consumer:Optional[Callable[[bytes], None]]=None,
              force_raw_paste:Optional[bool]=None) -> bytes:
        self._stop_event.clear()
        self._reset_error_filter()
        
        if isinstance(command, str):
            command = command.encode('utf-8')
        
        if not command or len(command) == 0:
            return b''
        
        if interactive:
            ReplProtocol._active_instance = self

        try:
            return self._exec_command(command, interactive, echo, detach, data_consumer, force_raw_paste)
        except ProtocolError:
            self._in_raw_repl = False
            self._rx_pushback.clear()
            raise
        finally:
            if interactive:
                ReplProtocol._active_instance = None

    def _exec_command(self, command: bytes, interactive: bool, echo: bool, detach: bool,
                      data_consumer: Optional[Callable[[bytes], None]],
                      force_raw_paste: Optional[bool]) -> bytes:
        data_err = b''
        if data_consumer is None and interactive:
            data_consumer = self._create_data_consumer()
        follow_thread = None

        data = self._read_ex(1, b'>')
        if not data.endswith(b'>'):
            raise ProtocolError('could not enter raw repl')

        command_len = len(command)

        prefer_raw_paste = force_raw_paste
        if prefer_raw_paste is None:
            prefer_raw_paste = command_len >= self._raw_paste_min_command_size
        use_raw_paste = (
            prefer_raw_paste
            and not interactive
            and not detach
            and self._enter_raw_paste_mode()
        )
        
        if use_raw_paste:
            try:
                data, data_err = self._exec_raw_paste(command, data_consumer)
            except TransportError:
                raise
            except Exception as e:
                self._raw_paste_supported = False
                if not self._resync_repl():
                    try:
                        self._leave_repl()
                        time.sleep(0.05)
                        self._enter_repl()
                        prompt = self._read_ex(1, b'>')
                        if not prompt.endswith(b'>'):
                            raise ProtocolError('could not recover after raw-paste failure')
                    except Exception as recovery_error:
                        raise ProtocolError(
                            f"Raw-paste transfer failed: {e}; REPL recovery failed: {recovery_error}"
                        ) from e
                if isinstance(e, ProtocolError):
                    raise
                raise ProtocolError(f"Raw-paste transfer failed: {e}") from e

            if data_err and not self._interrupt_requested:
                raise ProtocolError(data_err.decode('utf-8', errors='replace'))

            self._interrupt_requested = False
            return data
        
        current_buffer_size = self._DEVICE_CHUNK_SIZES
        bytes_sent = 0
        
        start_time = time.monotonic()
        
        while bytes_sent < command_len:
            chunk_end = min(bytes_sent + current_buffer_size, command_len)
            chunk = command[bytes_sent:chunk_end]
            
            self.transport.write(chunk)
            bytes_sent += len(chunk)
            
            if self._RAW_MODE_DELAY > 0 and bytes_sent < command_len:
                time.sleep(self._RAW_MODE_DELAY)
        
        self.transport.write(EOF_MARKER)
        
        transfer_time = time.monotonic() - start_time
        timeout = max(5, int(transfer_time * 2))
        
        data = self._read_ex(1, OK_RESPONSE, timeout=timeout)
        if not data.endswith(OK_RESPONSE):
            raise ProtocolError('could not execute command (response: %r)' % data)

        if detach:
            return b''
        
        if interactive:
            self._stop_event.clear()
            self._interrupt_requested = False
            follow_thread = threading.Thread(target=self._follow_task, args=(echo,), daemon=True)
            follow_thread.start()
            
        try:
            data = self._read_ex(1, EOF_MARKER, 0, data_consumer)
            if data.endswith(EOF_MARKER):
                data = data[:-1]
            
            data_err = self._read_ex(1, EOF_MARKER, 0, None)
            if data_err.endswith(EOF_MARKER):
                data_err = data_err[:-1]
            elif data_err and not self._interrupt_requested:
                raise ProtocolError(data_err.decode('utf-8', errors='replace'))
        finally:            
            def _drain_consumer(b: bytes, _carry=[False]):
                if len(b) == 1 and b == b'>':
                    return
                if data_consumer:
                    data_consumer(b)
                else:
                    _stdout_write_bytes(b)
    
            if follow_thread and follow_thread.is_alive():
                self._stop_event.set()
                try: 
                    follow_thread.join(timeout=0.05)
                except Exception:
                    pass
                
                self._stop_event.clear()
                
                try:
                    self._read_ex(1, EOF_MARKER, timeout=0.1, data_consumer=_drain_consumer)
                except Exception:
                    pass

                if not data_consumer:
                    try:
                        _flush_outbuf()
                    except Exception:
                        pass

                try:
                    if self._interrupt_requested and self.core != "EFR32MG":
                        self.transport.write(b'\r' + CTRL_B)
                        time.sleep(0.08)
                        self.transport.write(b'\r' + CTRL_A)
                        self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=1)
                    else:
                        self.transport.write(CTRL_D)
                        got = self._read_ex(1, SOFT_REBOOT_MSG, timeout=2)
                        if not got.endswith(SOFT_REBOOT_MSG):
                            self.transport.write(b'\r' + CTRL_B)
                            time.sleep(0.08)
                            self.transport.write(b'\r' + CTRL_A)
                            self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=1)
                except Exception:
                    pass
        
        if data_err:
            if self._interrupt_requested:
                data_err = b""
            else:
                raise ProtocolError(data_err.decode('utf-8', errors='replace'))

        self._interrupt_requested = False
        return data
    
    def _create_data_consumer(self):
        def data_consumer(chunk):
            if not chunk:
                return
            
            self._error_header_buf = (self._error_header_buf + chunk)[-len(ERROR_HEADER):]
            if self._error_header_buf == ERROR_HEADER:
                self._skip_error_output = True
                return
            
            if not self._skip_error_output:
                _stdout_write_bytes(chunk)
                _flush_outbuf()
        
        return data_consumer

    def _drain_eof(self, max_ms:int=200):
        deadline = time.monotonic() + max_ms / 1000
        while time.monotonic() < deadline:
            waiting = self.transport.in_waiting()
            if waiting:
                _ = self._read(waiting) 
            else:
                time.sleep(0.01)

    def _repl_serial_to_stdout(self):
        PROMPT_COLOR = b"\033[92m"
        CONT_COLOR = b"\033[93m"
        RESET_COLOR = b"\033[0m"
        read_errors = 0
        
        try:
            while self.serial_reader_running:
                try:
                    count = self.transport.in_waiting()
                except Exception:
                    break

                if not count:
                    read_errors = 0
                    time.sleep(0.01)
                    continue

                try:
                    data = self.transport.read(count)
                except Exception:
                    break

                if not data:
                    continue

                read_errors = 0

                if b">>> " in data:
                    self._repl_prompt_detected = True

                if self.serial_out_put_enable and self.serial_out_put_count > 0:
                    data = data.replace(b">>> ", PROMPT_COLOR + b">>> " + RESET_COLOR)
                    data = data.replace(b"... ", CONT_COLOR + b"... " + RESET_COLOR)
                    
                    if IS_WINDOWS:
                        sys.stdout.buffer.write(data.replace(b"\r", b""))
                    else:
                        sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

                self.serial_out_put_count += 1
        except KeyboardInterrupt:
            try:
                self.transport.close()
            except Exception:
                pass

    def _reset(self):
        command = """
            import machine
            machine.soft_reset()  # Ctrl+D
        """
        self.exec(command)

    def request_interrupt(self):
        self._interrupt_requested = True
        try:
            self.transport.write(CTRL_C)
        except Exception:
            pass

    def exec(self, command:str=None):
        if not self._in_raw_repl:
            self._enter_repl()
        
        try:
            command = textwrap.dedent(command)
            return self._exec(command)
        except ProtocolError:
            raise
        finally:
            pass
    
    def run(self, local, interactive:bool=False, echo:bool=False):
        if not interactive and echo:
            import typer

            raise typer.BadParameter("Option chaining error: -n and -e can only be used once, not multiple times.")
        
        with open(local, "rb") as f:
            data = f.read()
        
        if not data or len(data) == 0:
            return
        
        if not self._in_raw_repl:
            self._enter_repl()
        
        try:
            self._soft_reset()
            self._exec(data, interactive, echo, detach=not interactive)
            if interactive:
                self._drain_eof(max_ms=200)
        except Exception:
            self._leave_repl()
            raise

    def close(self):
        self.transport.close()

    def reset(self):
        self._in_raw_repl = False
        self._enter_repl()
        
        try:
            self._soft_reset()
        except Exception:
            self._leave_repl()
            raise
 
    def repl(self):
        import time

        def _write_with_timeout(data: bytes, timeout_sec: float = 1.5) -> bool:
            result = {"ok": False, "err": None}

            def _writer():
                try:
                    self.transport.write(data)
                    result["ok"] = True
                except Exception as e:
                    result["err"] = e

            t = threading.Thread(target=_writer, daemon=True)
            t.start()
            t.join(timeout=timeout_sec)
            if t.is_alive():
                return False
            return bool(result["ok"]) and result["err"] is None
        
        self.serial_reader_running = True
        self.serial_out_put_enable = False 
        self.serial_out_put_count = 0
        self._repl_prompt_detected = False

        repl_thread = threading.Thread(target=self._repl_serial_to_stdout, daemon=True, name='REPL')
        repl_thread.start()
        
        time.sleep(0.05)

        if not _write_with_timeout(CTRL_B):
            self.serial_reader_running = False
            print('')
            return
        time.sleep(0.05)
        if not _write_with_timeout(CTRL_C):
            self.serial_reader_running = False
            print('')
            return
        time.sleep(0.1)
        
        drain_timeout = time.monotonic() + 0.5
        while time.monotonic() < drain_timeout:
            if self.transport.in_waiting() > 0:
                self.transport.read(self.transport.in_waiting())
                time.sleep(0.02)
            else:
                time.sleep(0.05)
                if self.transport.in_waiting() == 0:
                    break
        
        self.serial_out_put_enable = True
        self.serial_out_put_count = 1
        if not _write_with_timeout(b'\r'):
            self.serial_reader_running = False
            print('')
            return
        
        prompt_timeout = time.monotonic() + 1.0
        while time.monotonic() < prompt_timeout:
            time.sleep(0.05)
            if self._repl_prompt_detected:
                break
        
        recent_chars = bytearray(6)
        recent_len = 0
        
        while True:
            if self.serial_reader_running and not repl_thread.is_alive():
                break

            char = getch()
            if not char:
                continue

            if char == b'\x07': 
                self.serial_out_put_enable = False
                continue
            elif char == b'\x0F': 
                self.serial_out_put_enable = True
                self.serial_out_put_count = 0
                continue
            elif char == b'\x00' or not char:
                continue
            
            if char == b'\r' or char == b'\n':
                if recent_len >= 4:
                    cmd = bytes(recent_chars[:recent_len]).strip().lower()
                    if cmd in (b'exit', b'exit()'):
                        break
                recent_len = 0
            elif char == b'\x7f' or char == b'\x08':
                if recent_len > 0:
                    recent_len -= 1
            elif char >= b' ' and len(char) == 1: 
                if recent_len < 6:
                    recent_chars[recent_len] = char[0]
                    recent_len += 1
                else:
                    recent_chars[:-1] = recent_chars[1:]
                    recent_chars[5] = char[0]
            
            try:
                if not _write_with_timeout(b'\r' if char == b'\n' else char):
                    break
            except Exception:
                break
            
        self.serial_reader_running = False
        if repl_thread.is_alive():
            try:
                repl_thread.join(timeout=0.2)
            except Exception:
                pass
        print('')

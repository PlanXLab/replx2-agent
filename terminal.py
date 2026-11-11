import os
import sys
import platform
import time
import threading
import shutil
from typing import Callable
import select

from .utils.constants import EOF_MARKER

IS_WINDOWS: bool = platform.system() == "Windows"
CR, LF = b"\r", b"\n"

_stdout_lock = threading.Lock()
_thread_local = threading.local()
_OUTBUF_MAX = 8192


def _get_thread_state():
    if not hasattr(_thread_local, 'buffer'):
        _thread_local.buffer = b''
        _thread_local.expected_bytes = 0
        _thread_local.outbuf = bytearray()
    return _thread_local


def flush_outbuf():
    state = _get_thread_state()
    if state.outbuf:
        with _stdout_lock:
            sys.stdout.buffer.write(state.outbuf)
            sys.stdout.buffer.flush()
        state.outbuf.clear()


def stdout_write_bytes(b, skip_error_filter=False):
    if not b:
        return

    if EOF_MARKER in b:
        b = b.replace(EOF_MARKER, b'')
        if not b:
            return

    state = _get_thread_state()

    mv = memoryview(b)
    i = 0
    while i < len(mv):
        ch = mv[i]
        if state.expected_bytes:
            take = min(state.expected_bytes, len(mv) - i)
            state.buffer += mv[i:i+take].tobytes()
            state.expected_bytes -= take
            i += take
            if state.expected_bytes == 0:
                state.outbuf.extend(state.buffer)
                state.buffer = b''
                if state.outbuf.endswith(b'\n') or len(state.outbuf) >= _OUTBUF_MAX:
                    flush_outbuf()
            continue

        if ch <= 0x7F:
            j = i + 1
            ln = len(mv)
            while j < ln and mv[j] <= 0x7F:
                j += 1
            state.outbuf.extend(mv[i:j])
            i = j
            if state.outbuf.endswith(b'\n') or len(state.outbuf) >= _OUTBUF_MAX:
                flush_outbuf()
            continue

        hdr = ch
        if   (hdr & 0xF8) == 0xF0: need = 3
        elif (hdr & 0xF0) == 0xE0: need = 2
        elif (hdr & 0xE0) == 0xC0: need = 1
        else:
            state.outbuf.extend(bytes([hdr]).hex().encode())
            if len(state.outbuf) >= _OUTBUF_MAX:
                flush_outbuf()
            i += 1
            continue

        state.buffer = bytes([hdr])
        i += 1
        state.expected_bytes = need


_EXTMAP: dict[str, bytes] = {
    "H": b"\x1b[A",   # Up
    "P": b"\x1b[B",   # Down
    "M": b"\x1b[C",   # Right
    "K": b"\x1b[D",   # Left
    "G": b"\x1b[H",   # Home
    "O": b"\x1b[F",   # End
    "R": b"\x1b[2~",  # Ins
    "S": b"\x1b[3~",  # Del
}


def utf8_need_follow(b0: int) -> int:
    if b0 & 0b1000_0000 == 0:
        return 0
    if b0 & 0b1110_0000 == 0b1100_0000:
        return 1
    if b0 & 0b1111_0000 == 0b1110_0000:
        return 2
    if b0 & 0b1111_1000 == 0b1111_0000:
        return 3
    return 0


def read_escape_sequence(fd: int, first: bytes, timeout: float = 0.02) -> bytes:
    if first != b"\x1b":
        return first

    deadline = time.monotonic() + timeout
    sequence = bytearray(first)
    while len(sequence) < 32:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([sys.stdin], [], [], remaining)
        if not ready:
            break
        char = os.read(fd, 1)
        if not char:
            break
        sequence.extend(char)
        if len(sequence) == 2 and sequence[1] not in (ord("["), ord("O")):
            break
        if len(sequence) >= 3 and 0x40 <= sequence[-1] <= 0x7E:
            break

    return bytes(sequence)


if IS_WINDOWS:
    import msvcrt
    import ctypes
    import atexit as _atexit

    _ENABLE_QUICK_EDIT    = 0x0040
    _ENABLE_EXTENDED_FLAGS = 0x0080
    _STD_INPUT_HANDLE     = -10

    def disable_quick_edit_mode() -> int | None:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
            if handle == ctypes.c_void_p(-1).value:
                return None
            old_mode = ctypes.c_ulong(0)
            if not kernel32.GetConsoleMode(handle, ctypes.byref(old_mode)):
                return None
            new_mode = (old_mode.value & ~_ENABLE_QUICK_EDIT) | _ENABLE_EXTENDED_FLAGS
            kernel32.SetConsoleMode(handle, new_mode)
            return old_mode.value
        except Exception:
            return None

    def restore_console_mode(old_mode: int | None) -> None:
        if old_mode is None:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
            if handle == ctypes.c_void_p(-1).value:
                return
            kernel32.SetConsoleMode(handle, old_mode)
        except Exception:
            pass

    def kbhit() -> bool:
        return msvcrt.kbhit()

    def getch_nonblock() -> bytes | None:
        if not msvcrt.kbhit():
            return None
        w = msvcrt.getwch()
        if w in ("\x00", "\xe0"):
            ext_key = msvcrt.getwch()
            return _EXTMAP.get(ext_key, b"")
        return w.encode("utf-8")

    def getch() -> bytes:
        w = msvcrt.getwch()
        if w in ("\x00", "\xe0"):
            return _EXTMAP.get(msvcrt.getwch(), b"")
        return w.encode("utf-8")

    _PUTB: Callable[[bytes], None] = msvcrt.putch
    _PUTW: Callable[[str], None] = msvcrt.putwch

    def write_bytes(data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def putch(data: bytes) -> None:
        if data == CR:
            _PUTB(LF)
            return

        if len(data) > 1 and data.startswith(b"\x1b["):
            write_bytes(data)
        elif len(data) == 1 and data < b"\x80":
            _PUTB(data)
        else:
            _PUTW(data.decode("utf-8", "strict"))

else:
    def disable_quick_edit_mode() -> None:
        return None

    def restore_console_mode(old_mode) -> None:
        pass

    import tty
    import termios
    import atexit
    import signal

    _FD = sys.stdin.fileno()

    _terminal_state = threading.local()
    _terminal_lock = threading.RLock()

    def _get_terminal_state():
        if not hasattr(_terminal_state, 'old_settings'):
            _terminal_state.old_settings = None
            _terminal_state.raw_mode_active = False
        return _terminal_state

    def initialize_terminal():
        state = _get_terminal_state()
        if state.old_settings is None:
            try:
                with _terminal_lock:
                    state.old_settings = termios.tcgetattr(_FD)
            except Exception:
                pass

    def raw_mode(on: bool):
        state = _get_terminal_state()
        try:
            if on:
                with _terminal_lock:
                    initialize_terminal()
                    tty.setraw(_FD)
                state.raw_mode_active = True
            else:
                with _terminal_lock:
                    if state.old_settings is not None:
                        termios.tcsetattr(_FD, termios.TCSADRAIN, state.old_settings)
                state.raw_mode_active = False
        except Exception:
            pass

    def restore_terminal():
        state = _get_terminal_state()
        if state.raw_mode_active:
            raw_mode(False)

    def signal_handler(signum, frame):
        restore_terminal()
        raise KeyboardInterrupt()

    atexit.register(restore_terminal)
    signal.signal(signal.SIGTERM, signal_handler)

    def kbhit() -> bool:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(r)

    def getch_nonblock() -> bytes | None:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
        try:
            raw_mode(True)
            first = os.read(_FD, 1)
            if not first:
                return None
            need = utf8_need_follow(first[0])
            return first + (os.read(_FD, need) if need else b"")
        except (OSError, IOError):
            return None
        finally:
            raw_mode(False)

    def getch() -> bytes:
        try:
            raw_mode(True)
            first = os.read(_FD, 1)
            need = utf8_need_follow(first[0])
            return first + (os.read(_FD, need) if need else b"")
        except Exception:
            return b""
        finally:
            raw_mode(False)

    def putch(data: bytes) -> None:
        if data != CR:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()


def lmt_write(s: str | bytes) -> None:
    if isinstance(s, str):
        s = s.encode()
    sys.stdout.buffer.write(s)
    sys.stdout.buffer.flush()


def lmt_terminal_size() -> tuple[int, int]:
    sz = shutil.get_terminal_size(fallback=(80, 24))
    return sz.columns, sz.lines


def lmt_set_scroll_region(top: int, bottom: int) -> None:
    lmt_write(f"\x1b[{top};{bottom}r")


def lmt_clear_scroll_region() -> None:
    lmt_write("\x1b[r")


def lmt_move(row: int, col: int = 1) -> None:
    lmt_write(f"\x1b[{row};{col}H")


def lmt_erase_line() -> None:
    lmt_write("\x1b[2K")


def lmt_clear_screen() -> None:
    lmt_write("\x1b[2J")
    lmt_move(1, 1)


def enable_vt_mode() -> bool:
    global _windows_vt_state
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        old_mode = ctypes.c_ulong(0)
        if not kernel32.GetConsoleMode(handle, ctypes.byref(old_mode)):
            return False
        captured_mode = _windows_vt_state is None
        if captured_mode:
            _windows_vt_state = (kernel32.SetConsoleMode, handle, old_mode.value)
        new_mode = old_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
        enabled = bool(kernel32.SetConsoleMode(handle, new_mode))
        if not enabled and captured_mode:
            _windows_vt_state = None
        return enabled
    except Exception:
        return False


_EOL_MODES = ("cr", "lf", "crlf")
_EOL_BYTES = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}
_windows_vt_state: tuple[Callable[..., int], object, int] | None = None


def restore_vt_mode() -> None:
    global _windows_vt_state
    state = _windows_vt_state
    _windows_vt_state = None
    if state is None:
        return

    set_console_mode, handle, old_mode = state
    try:
        set_console_mode(handle, old_mode)
    except Exception:
        pass


class LineModeTerminal:
    PROMPT = "> "

    def __init__(self, hex_mode: bool = False):
        self.hex_mode = hex_mode
        self._eol: str = "cr" 
        self._buf: list[str] = []
        self._cols, self._rows = lmt_terminal_size()
        self._input_row = self._rows
        self._scroll_top = 1
        self._scroll_bottom = self._rows - 1
        self._error_msg: str = ""
        self._history: list[str] = []
        self._hist_idx: int = -1
        self._hist_draft: str = ""
        self._old_resize_handler = None
        self._resize_handler = None

    def setup(self) -> None:
        if self._rows < 3:
            raise RuntimeError(f"Terminal too small (rows={self._rows}, min 3 required)")
        enable_vt_mode()
        lmt_clear_screen()
        lmt_set_scroll_region(self._scroll_top, self._scroll_bottom)
        lmt_move(self._scroll_bottom, 1)
        self._draw_input()
        if not IS_WINDOWS:
            if self._resize_handler is None:
                import signal
                self._old_resize_handler = signal.getsignal(signal.SIGWINCH)
                self._resize_handler = self._on_resize
                signal.signal(signal.SIGWINCH, self._resize_handler)

    def restore(self) -> None:
        try:
            lmt_clear_scroll_region()
            lmt_move(self._input_row, 1)
            lmt_erase_line()
            lmt_write("\r\n")
        finally:
            if not IS_WINDOWS and self._resize_handler is not None:
                import signal
                if signal.getsignal(signal.SIGWINCH) == self._resize_handler:
                    signal.signal(signal.SIGWINCH, self._old_resize_handler)
                self._resize_handler = None
                self._old_resize_handler = None
            restore_vt_mode()

    def write_output(self, data: bytes) -> None:
        if not data:
            return
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if not IS_WINDOWS:
            data = data.replace(b"\n", b"\r\n")
        lmt_move(self._scroll_bottom, 1)
        lmt_write(data)
        self._draw_input()

    def handle_key(self, ch: bytes) -> bytes | None:
        if ch in (b"\r", b"\n"):
            return self._commit()
        if ch in (b"\x7f", b"\x08"):
            return self._backspace()
        if ch == b"\x14": 
            if not self.hex_mode:
                self._toggle_eol()
            return None
        if ch == b"\x15":
            self._buf.clear()
            self._hist_idx = -1
            self._hist_draft = ""
            self._error_msg = ""
            self._draw_input()
            return None
        if ch == b"\x1b[A":  
            return self._history_up()
        if ch == b"\x1b[B": 
            return self._history_down()
        if len(ch) == 1 and 0x20 <= ch[0] <= 0x7E:
            self._buf.append(ch.decode("ascii"))
            self._hist_idx = -1
            self._error_msg = ""
            self._draw_input()
        return None

    def _draw_input(self) -> None:
        lmt_move(self._input_row, 1)
        lmt_erase_line()
        if self.hex_mode:
            mode_tag = "[hex] "
        else:
            mode_tag = f"[{self._eol.upper()}] "
        err = f"  \x1b[31m{self._error_msg}\x1b[0m" if self._error_msg else ""
        content = "".join(self._buf)
        lmt_write(f"{self.PROMPT}{mode_tag}{content}{err}")

    def _backspace(self) -> None:
        if self._buf:
            self._buf.pop()
        self._error_msg = ""
        self._draw_input()
        return None

    def _history_up(self) -> None:
        if not self._history:
            return None
        if self._hist_idx == -1:
            self._hist_draft = "".join(self._buf)
        new_idx = self._hist_idx + 1
        if new_idx >= len(self._history):
            return None
        self._hist_idx = new_idx
        self._buf = list(self._history[-(self._hist_idx + 1)])
        self._error_msg = ""
        self._draw_input()
        return None

    def _history_down(self) -> None:
        if self._hist_idx == -1:
            return None
        self._hist_idx -= 1
        if self._hist_idx == -1:
            self._buf = list(self._hist_draft)
        else:
            self._buf = list(self._history[-(self._hist_idx + 1)])
        self._error_msg = ""
        self._draw_input()
        return None

    def _toggle_eol(self) -> None:
        idx = _EOL_MODES.index(self._eol)
        self._eol = _EOL_MODES[(idx + 1) % len(_EOL_MODES)]
        self._draw_input()

    def _commit(self) -> bytes | None:
        raw = "".join(self._buf).strip()
        self._buf.clear()
        self._hist_idx = -1
        self._hist_draft = ""
        if not raw:
            self._draw_input()
            return None
        if self.hex_mode:
            payload, err = self._parse_hex(raw)
            if err:
                self._error_msg = err
                self._draw_input()
                return None
        else:
            payload = raw.encode("utf-8")

        if not self._history or self._history[-1] != raw:
            self._history.append(raw)
            if len(self._history) > 100:
                self._history.pop(0)
        self._error_msg = ""
        self._draw_input()
        return payload if self.hex_mode else payload + _EOL_BYTES[self._eol]

    @staticmethod
    def _parse_hex(s: str) -> tuple[bytes | None, str | None]:
        s = s.replace(" ", "").replace("_", "")
        valid = set("0123456789abcdefABCDEF")
        for ch in s:
            if ch not in valid:
                return None, f"invalid hex char: {ch!r}"
        if len(s) % 2:
            return None, f"odd length: {s!r}"
        try:
            return bytes.fromhex(s), None
        except ValueError as e:
            return None, f"conversion error: {e}"

    def _on_resize(self, signum, frame) -> None:
        self._cols, self._rows = lmt_terminal_size()
        self._input_row = self._rows
        self._scroll_bottom = self._rows - 1
        lmt_set_scroll_region(self._scroll_top, self._scroll_bottom)
        self._draw_input()

import asyncio
import time
import threading

from replx_agent.utils.constants import REPL_PROMPT
from replx_agent.utils.exceptions import TransportError
from ..command_dispatcher import CommandContext
from ..connection_manager import BoardConnection

class ReplCommandsMixin:
    @staticmethod
    def _read_repl_chunk(repl) -> bytes:
        count = repl.in_waiting()
        if count > 0:
            return repl.read_bytes(count)

        transport = getattr(repl, 'transport', None)
        if transport is None or not hasattr(transport, 'read_byte'):
            return b''

        first = transport.read_byte(timeout=0.02)
        if not first:
            return b''

        extra = repl.in_waiting()
        if extra > 0:
            return first + repl.read_bytes(extra)
        return first

    async def _repl_reader_task(self, conn: BoardConnection) -> None:
        loop = asyncio.get_running_loop()
        repl = conn.repl_protocol
        consecutive_transient = 0
        try:
            while conn.repl.active and conn.repl_protocol:
                try:
                    data = await loop.run_in_executor(
                        self._slow_executor, self._read_repl_chunk, repl
                    )
                    if data:
                        conn.repl.append_output(data)
                        consecutive_transient = 0
                    else:
                        consecutive_transient = 0
                        await asyncio.sleep(0.01)
                except TransportError:
                    consecutive_transient += 1
                    if consecutive_transient >= 20:
                        break
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _repl_reader_loop(self, conn: BoardConnection):
        if not conn or not conn.repl_protocol:
            return
        repl = conn.repl_protocol
        consecutive_transient = 0
        while conn.repl.active and conn.repl_protocol:
            try:
                data = self._read_repl_chunk(repl)
                if data:
                    conn.repl.append_output(data)
                    consecutive_transient = 0
                else:
                    consecutive_transient = 0
                    time.sleep(0.01)
            except TransportError:
                consecutive_transient += 1
                if consecutive_transient >= 20:
                    break
                time.sleep(0.005)
            except Exception:
                break

    def _cmd_repl_enter(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")

        if conn.repl.active:
            conn.repl.stop()

        repl = conn.repl_protocol

        if repl._in_raw_repl:
            try:
                repl._leave_repl()
            except Exception:
                pass

        repl.exit_paste_mode()
        time.sleep(0.05)
        repl.interrupt()
        time.sleep(0.1)

        drain_start = time.time()
        while time.time() - drain_start < 0.3:
            if repl.in_waiting() > 0:
                repl.read_bytes(repl.in_waiting())
                time.sleep(0.02)
            else:
                break

        repl.send_raw(b'\r')
        time.sleep(0.15)

        response = b""
        read_start = time.time()
        while time.time() - read_start < 0.5:
            if repl.in_waiting() > 0:
                response += repl.read_bytes(repl.in_waiting())
                if REPL_PROMPT in response:
                    break
            time.sleep(0.02)

        response_str = response.decode('utf-8', errors='replace')
        prompt_found = '>>>' in response_str

        if prompt_found:
            conn.repl.start(ctx.ppid)
            loop = getattr(self, '_loop', None)
            if loop is not None and not loop.is_closed():
                conn.repl.reader_future = asyncio.run_coroutine_threadsafe(
                    self._repl_reader_task(conn), loop
                )
            else:
                conn.repl.reader_thread = threading.Thread(
                    target=self._repl_reader_loop,
                    args=(conn,),
                    daemon=True,
                    name=f'REPL-Reader-{conn.port}'
                )
                conn.repl.reader_thread.start()
        else:
            conn.release()

        return {
            "entered": prompt_found,
            "output": response_str
        }

    def _cmd_repl_exit(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn:
            return {"exited": False, "reason": "Not connected"}

        if not conn.repl.is_owner(ctx.ppid):
            return {"exited": False, "reason": "Not the REPL session owner"}

        conn.repl.stop()
        try:
            conn.repl_protocol.reset()
        except Exception:
            pass
        conn.release()

        return {"exited": True}

    def _cmd_repl_write(self, ctx: CommandContext, data: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")

        if not conn.repl.is_owner(ctx.ppid):
            raise RuntimeError("Not the REPL session owner")

        if isinstance(data, str):
            data = data.encode('utf-8')

        conn.repl_protocol.send_raw(data)
        return {"written": len(data)}

    def _cmd_repl_read(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")

        if not conn.repl.is_owner(ctx.ppid):
            raise RuntimeError("Not the REPL session owner")

        data = conn.repl.read_output()

        return {
            "output": data.decode('utf-8', errors='replace')
        }

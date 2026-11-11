import base64
import hashlib
import logging
import os
import posixpath
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from replx_agent.rpc.protocol import AgentProtocol
from replx_agent.utils.constants import MAX_PAYLOAD_SIZE
from ..command_dispatcher import CommandContext

logger = logging.getLogger(__name__)

_MAX_TRANSFER_SIZE = 512 * 1024 * 1024
_TRANSFER_PROGRESS_INTERVAL = 0.1
_FILE_CHUNK_SIZE = 64 * 1024


@dataclass
class _IncomingTransfer:
    context: CommandContext
    purpose: str
    file_name: str
    expected_size: int
    temp_path: str
    file_handle: Any
    remote_path: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)
    received_size: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    last_progress_time: float = field(default_factory=time.monotonic)


@dataclass
class _FirmwareStage:
    file_name: str
    size: int
    digest: str
    path: str
    created_at: float = field(default_factory=time.monotonic)


class TransferCommandsMixin:
    def _init_transfer_state(self) -> None:
        self._incoming_transfers: dict[tuple[Any, int], _IncomingTransfer] = {}
        self._incoming_transfers_lock = threading.Lock()
        self._firmware_stages: dict[str, _FirmwareStage] = {}
        self._firmware_stages_lock = threading.Lock()

    def _send_stream_completion(self, response: dict, client_addr: Any) -> None:
        encoded = AgentProtocol.encode_message(response)
        seq = response.get("seq", 0)
        self._cache_response(client_addr, seq, encoded)
        if not self.send_completion(encoded, client_addr, seq):
            logger.warning("Could not send streaming response %s to %s", seq, client_addr)

    def _cmd_get_file(self, ctx: CommandContext, remote_path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            if conn:
                conn.release()
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(remote_path, conn)
            content = conn.file_system.get(real_path)
            max_content_size = ((MAX_PAYLOAD_SIZE - 1024) * 3) // 4
            if len(content) > max_content_size:
                raise RuntimeError(
                    f"File is too large for an inline response: {len(content)} bytes"
                )
            return {
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
                "path": remote_path,
            }
        except Exception as exc:
            raise RuntimeError(f"get_file failed: {exc}") from exc

    def _cmd_put_file(self, ctx: CommandContext, remote_path: str, content: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        temp_path = None
        try:
            real_path = self._to_real_path(remote_path, conn)
            content_bytes = base64.b64decode(content, validate=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, suffix=".tmp", prefix="replx-agent-"
            ) as file:
                file.write(content_bytes)
                temp_path = file.name
            conn.file_system.put(temp_path, real_path)
            return {"uploaded": remote_path, "bytes": len(content_bytes)}
        except Exception as exc:
            raise RuntimeError(f"put_file failed: {exc}") from exc
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _cmd_put_file_batch(self, ctx: CommandContext, file_specs: list) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        results = []
        for spec in file_specs:
            remote_path = spec.get("remote_path")
            content_b64 = spec.get("content")
            if "local_path" in spec:
                results.append({
                    "path": remote_path,
                    "success": False,
                    "error": "Agent RPC does not accept CLI-local paths",
                })
                continue
            if not remote_path or not content_b64:
                results.append({
                    "path": remote_path,
                    "success": False,
                    "error": "Remote path and content are required",
                })
                continue

            temp_path = None
            try:
                content = base64.b64decode(content_b64, validate=True)
                real_path = self._to_real_path(remote_path, conn)
                with tempfile.NamedTemporaryFile(
                    mode="wb", delete=False, suffix=".tmp", prefix="replx-agent-"
                ) as file:
                    file.write(content)
                    temp_path = file.name
                conn.file_system.put(temp_path, real_path)
                results.append({"path": remote_path, "success": True})
            except Exception as exc:
                results.append({
                    "path": remote_path,
                    "success": False,
                    "error": str(exc),
                })
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except FileNotFoundError:
                        pass

        return {
            "results": results,
            "success_count": sum(1 for result in results if result["success"]),
            "total": len(file_specs),
        }

    def _cmd_get_file_batch(self, ctx: CommandContext, remote_paths: list) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        results = []
        for remote_path in remote_paths:
            try:
                real_path = self._to_real_path(remote_path, conn)
                content = conn.file_system.get(real_path)
                if isinstance(content, str):
                    content = content.encode("utf-8")
                results.append({
                    "path": remote_path,
                    "content": base64.b64encode(content).decode("ascii"),
                    "success": True,
                })
            except Exception as exc:
                results.append({
                    "path": remote_path,
                    "success": False,
                    "error": str(exc),
                })

        return {
            "results": results,
            "success_count": sum(1 for result in results if result["success"]),
            "total": len(remote_paths),
        }

    def _start_incoming_transfer(
        self,
        ctx: CommandContext,
        *,
        purpose: str,
        file_name: str,
        size: int,
        remote_path: str = None,
        options: Optional[dict] = None,
    ) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_TRANSFER_SIZE:
            raise ValueError(f"Invalid transfer size: {size!r}")
        if purpose not in {"put", "script_run", "script_interactive", "firmware"}:
            raise ValueError(f"Unsupported transfer purpose: {purpose}")
        if purpose == "put" and not remote_path:
            raise ValueError("Remote file path is required")
        if purpose in {"put", "script_run", "script_interactive"}:
            if not ctx.connection or not ctx.connection.file_system:
                raise RuntimeError("Not connected")
            if purpose == "put":
                self._to_real_path(remote_path, ctx.connection)

        file = tempfile.NamedTemporaryFile(
            mode="wb", delete=False, suffix=".tmp", prefix="replx-agent-"
        )

        key = (ctx.client_addr, ctx.seq)
        state = _IncomingTransfer(
            context=ctx,
            purpose=purpose,
            file_name=os.path.basename(file_name or "transfer"),
            expected_size=size,
            temp_path=file.name,
            file_handle=file,
            remote_path=remote_path,
            options=options or {},
        )
        with self._incoming_transfers_lock:
            if key in self._incoming_transfers:
                file.close()
                os.unlink(file.name)
                raise RuntimeError("Transfer ID is already active")
            self._incoming_transfers[key] = state

    def _cmd_transfer_start(
        self,
        ctx: CommandContext,
        purpose: str,
        file_name: str,
        size: int,
        remote_path: str = None,
        echo: bool = False,
        reset: bool = True,
        detach: bool = False,
    ) -> None:
        try:
            self._start_incoming_transfer(
                ctx,
                purpose=purpose,
                file_name=file_name,
                size=size,
                remote_path=remote_path,
                options={"echo": echo, "reset": reset, "detach": detach},
            )
        except Exception:
            key = (ctx.client_addr, ctx.seq)
            with self._incoming_transfers_lock:
                transfer_exists = key in self._incoming_transfers
            if ctx.connection and not transfer_exists:
                ctx.connection.release()
            raise

    def _cmd_firmware_stage(
        self, ctx: CommandContext, file_name: str, size: int
    ) -> None:
        self._start_incoming_transfer(
            ctx,
            purpose="firmware",
            file_name=file_name,
            size=size,
        )

    def _handle_binary_frame(self, client_addr: Any, seq: int, data: bytes) -> None:
        key = (client_addr, seq)
        with self._incoming_transfers_lock:
            state = self._incoming_transfers.get(key)

        if state is None:
            self._safe_send(
                AgentProtocol.encode_message(AgentProtocol.create_response(
                    seq, error="No active transfer for binary frame"
                )),
                client_addr,
            )
            return

        try:
            if state.received_size + len(data) > state.expected_size:
                raise ValueError("Transfer contains more data than declared")
            state.file_handle.write(data)
            state.digest.update(data)
            state.received_size += len(data)

            now = time.monotonic()
            if now - state.last_progress_time >= _TRANSFER_PROGRESS_INTERVAL:
                state.last_progress_time = now
                progress = AgentProtocol.create_progress_stream(seq, {
                    "current": state.received_size,
                    "total": state.expected_size,
                    "file": state.file_name,
                    "status": "receiving",
                })
                self._safe_send(AgentProtocol.encode_message(progress), client_addr)
        except Exception as exc:
            self._abort_incoming_transfer(client_addr, seq, str(exc), notify=True)

    def _handle_transfer_end(self, client_addr: Any, msg: dict) -> None:
        seq = msg.get("seq", 0)
        key = (client_addr, seq)
        with self._incoming_transfers_lock:
            state = self._incoming_transfers.get(key)

        if state is None:
            self._send_stream_completion(
                AgentProtocol.create_response(seq, error="No active transfer to finish"),
                client_addr,
            )
            return

        try:
            state.file_handle.flush()
            state.file_handle.close()
            state.file_handle = None
        except OSError as exc:
            self._abort_incoming_transfer(
                client_addr, seq, f"Could not stage transfer: {exc}", notify=True
            )
            return

        expected_digest = str(msg.get("sha256") or "").lower()
        if (
            msg.get("size") != state.expected_size
            or state.received_size != state.expected_size
            or expected_digest != state.digest.hexdigest()
        ):
            self._abort_incoming_transfer(
                client_addr, seq, "Transfer size or SHA-256 verification failed", notify=True
            )
            return

        with self._incoming_transfers_lock:
            self._incoming_transfers.pop(key, None)

        if state.purpose == "firmware":
            stage_id = os.urandom(16).hex()
            stage = _FirmwareStage(
                file_name=state.file_name,
                size=state.expected_size,
                digest=state.digest.hexdigest(),
                path=state.temp_path,
            )
            with self._firmware_stages_lock:
                self._firmware_stages[stage_id] = stage
            self._send_stream_completion(
                AgentProtocol.create_response(seq, result={
                    "stage_id": stage_id,
                    "file_name": stage.file_name,
                    "size": stage.size,
                    "sha256": stage.digest,
                }),
                client_addr,
            )
            return

        threading.Thread(
            target=self._complete_incoming_transfer,
            args=(state,),
            daemon=True,
            name=f"replx-transfer-{seq}",
        ).start()

    def _complete_incoming_transfer(self, state: _IncomingTransfer) -> None:
        ctx = state.context
        seq = ctx.seq
        client_addr = ctx.client_addr
        conn = ctx.connection
        keep_connection = False

        try:
            if state.purpose == "put":
                real_path = self._to_real_path(state.remote_path, conn)
                with conn.file_system.repl.session():
                    conn.file_system.put(state.temp_path, real_path)
                result = {
                    "uploaded": state.remote_path,
                    "bytes": state.received_size,
                    "sha256": state.digest.hexdigest(),
                    "success": True,
                }
            elif state.purpose == "script_run":
                result = self._cmd_run(
                    ctx,
                    script_path=state.temp_path,
                    detach=bool(state.options.get("detach")),
                )
                result["script"] = state.file_name
            elif state.purpose == "script_interactive":
                self._cmd_run_interactive(
                    ctx,
                    script_path=state.temp_path,
                    echo=bool(state.options.get("echo")),
                    reset=bool(state.options.get("reset", True)),
                )
                keep_connection = bool(conn and conn.interactive.active)
                result = {"run": True, "started": True}
            else:
                raise ValueError(f"Unsupported transfer purpose: {state.purpose}")

            response = AgentProtocol.create_response(seq, result=result)
        except Exception as exc:
            response = AgentProtocol.create_response(seq, error=str(exc))
        finally:
            if conn and not keep_connection:
                conn.release()
            try:
                os.unlink(state.temp_path)
            except FileNotFoundError:
                pass

        self._send_stream_completion(response, client_addr)

    def _abort_incoming_transfer(
        self, client_addr: Any, seq: int, reason: str, *, notify: bool
    ) -> None:
        key = (client_addr, seq)
        with self._incoming_transfers_lock:
            state = self._incoming_transfers.pop(key, None)
        if state is None:
            return
        if state.context.connection:
            state.context.connection.release()
        if state.file_handle:
            state.file_handle.close()
        try:
            os.unlink(state.temp_path)
        except FileNotFoundError:
            pass
        if notify:
            self._send_stream_completion(
                AgentProtocol.create_response(seq, error=reason), client_addr
            )

    def _handle_transfer_abort(self, client_addr: Any, msg: dict) -> None:
        self._abort_incoming_transfer(
            client_addr,
            msg.get("seq", 0),
            str(msg.get("error") or "Transfer was cancelled"),
            notify=False,
        )

    def _cmd_get_file_streaming(
        self, ctx: CommandContext, remote_path: str
    ) -> None:
        conn = ctx.connection
        if not conn or not conn.file_system:
            if conn:
                conn.release()
            raise RuntimeError("Not connected")

        seq = ctx.seq
        client_addr = ctx.client_addr
        try:
            real_path = self._to_real_path(remote_path, conn)
        except Exception:
            conn.release()
            raise

        def download() -> None:
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", delete=False, suffix=".tmp", prefix="replx-agent-"
                ) as file:
                    temp_path = file.name
                with conn.file_system.repl.session():
                    conn.file_system.get(real_path, temp_path)

                size = os.path.getsize(temp_path)
                digest = hashlib.sha256()
                sent = 0
                last_progress_time = time.monotonic()
                if not self._safe_send(AgentProtocol.encode_message(
                    AgentProtocol.create_progress_stream(seq, {
                        "current": 0,
                        "total": size,
                        "file": posixpath.basename(remote_path),
                        "status": "starting",
                    })
                ), client_addr):
                    raise ConnectionError("Client disconnected during file download")

                with open(temp_path, "rb") as file:
                    while chunk := file.read(_FILE_CHUNK_SIZE):
                        digest.update(chunk)
                        if not self._safe_send(
                            AgentProtocol.encode_binary(seq, chunk), client_addr
                        ):
                            raise ConnectionError("Client disconnected during file download")
                        sent += len(chunk)
                        now = time.monotonic()
                        if (
                            now - last_progress_time >= _TRANSFER_PROGRESS_INTERVAL
                            or sent == size
                        ):
                            last_progress_time = now
                            self._safe_send(AgentProtocol.encode_message(
                                AgentProtocol.create_progress_stream(seq, {
                                    "current": sent,
                                    "total": size,
                                    "file": posixpath.basename(remote_path),
                                    "status": "downloading",
                                })
                            ), client_addr)

                response = AgentProtocol.create_response(seq, result={
                    "downloaded": remote_path,
                    "size": sent,
                    "sha256": digest.hexdigest(),
                    "success": True,
                })
            except Exception as exc:
                response = AgentProtocol.create_response(
                    seq, error=f"get_file failed: {exc}"
                )
            finally:
                conn.release()
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except FileNotFoundError:
                        pass
            self._send_stream_completion(response, client_addr)

        threading.Thread(
            target=download,
            daemon=True,
            name=f"replx-download-{seq}",
        ).start()

    def _cleanup_transfer_state(self) -> None:
        with self._incoming_transfers_lock:
            incoming = list(self._incoming_transfers.values())
            self._incoming_transfers.clear()
        for state in incoming:
            if state.context.connection:
                state.context.connection.release()
            if state.file_handle:
                state.file_handle.close()
            try:
                os.unlink(state.temp_path)
            except FileNotFoundError:
                pass

        with self._firmware_stages_lock:
            stages = list(self._firmware_stages.values())
            self._firmware_stages.clear()
        for stage in stages:
            try:
                os.unlink(stage.path)
            except FileNotFoundError:
                pass

    def _handle_peer_disconnect(self, client_addr: Any) -> None:
        with self._incoming_transfers_lock:
            keys = [
                key for key in self._incoming_transfers
                if key[0] is client_addr
            ]
            transfers = [
                self._incoming_transfers.pop(key)
                for key in keys
            ]
        for state in transfers:
            if state.file_handle:
                try:
                    state.file_handle.close()
                except OSError as exc:
                    logger.warning("Could not close interrupted transfer %s: %s", state.temp_path, exc)
            if state.context.connection:
                state.context.connection.release()
            try:
                os.unlink(state.temp_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not remove interrupted transfer %s: %s", state.temp_path, exc)

        for conn in self.connection_manager.get_all_connections().values():
            if conn.interactive.active and conn.interactive.client_addr is client_addr:
                with conn.interactive.lock:
                    conn.interactive.stop_requested = True
                if conn.repl_protocol:
                    try:
                        conn.repl_protocol.interrupt()
                    except Exception as exc:
                        logger.warning("Could not interrupt disconnected interactive session on %s: %s", conn.port, exc)
        self._forget_peer(client_addr)

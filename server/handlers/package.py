import base64
import logging
import posixpath
import threading

from replx_agent.rpc.protocol import AgentProtocol
from replx_agent.pkg import service as pkg_service
from replx_agent.utils.exceptions import PkgError
from ..command_dispatcher import CommandContext

logger = logging.getLogger(__name__)

# Raw bytes per pkg_typehints_get response; base64 of this fits MAX_PAYLOAD_SIZE.
_TYPEHINTS_CHUNK_SIZE = 32768


class PackageCommandsMixin:
    """Agent-side package management: the package store (~/.replx), GitHub
    downloads and mpy-cross compilation live on the agent host, so a remote
    client only exchanges commands/progress events over the agent protocol."""

    @staticmethod
    def _pkg_require_context(ctx: CommandContext) -> None:
        if not ctx.connection or not ctx.core:
            raise RuntimeError("Not connected")

    def _cmd_pkg_search(self, ctx: CommandContext, owner: str = "PlanXLab",
                        repo: str = "replx_libs", ref: str = "main",
                        query: str = None) -> dict:
        self._pkg_require_context(ctx)
        return pkg_service.compute_search_rows(
            ctx.core, ctx.device, owner, repo, ref, query=query or None
        )

    def _cmd_pkg_store_status(self, ctx: CommandContext) -> dict:
        self._pkg_require_context(ctx)
        return pkg_service.store_status(ctx.core, ctx.device)

    def _cmd_pkg_clean(self, ctx: CommandContext) -> dict:
        self._pkg_require_context(ctx)
        return pkg_service.clean(ctx.core, ctx.device)

    def _cmd_pkg_typehints_list(self, ctx: CommandContext) -> dict:
        self._pkg_require_context(ctx)
        return pkg_service.typehints_list(ctx.core, ctx.device)

    def _cmd_pkg_typehints_get(self, ctx: CommandContext, scope: str,
                               relpath: str, offset: int = 0,
                               length: int = _TYPEHINTS_CHUNK_SIZE) -> dict:
        self._pkg_require_context(ctx)
        data = pkg_service.typehints_read(scope, relpath, ctx.core, ctx.device)
        offset = max(0, int(offset))
        length = min(max(1, int(length)), _TYPEHINTS_CHUNK_SIZE)
        chunk = data[offset:offset + length]
        return {
            "scope": scope,
            "relpath": relpath,
            "offset": offset,
            "size": len(data),
            "eof": offset + len(chunk) >= len(data),
            "content": base64.b64encode(chunk).decode("ascii"),
        }

    def _cmd_pkg_download(self, ctx: CommandContext, owner: str = "PlanXLab",
                          repo: str = "replx_libs", ref: str = "main",
                          scope_filter: str = None, file_filter: str = None) -> None:
        conn = ctx.connection
        if not conn or not ctx.core:
            if conn:
                conn.release()
            raise RuntimeError("Not connected")

        seq = ctx.seq
        client_addr = ctx.client_addr
        core = ctx.core
        device = ctx.device

        def emit(data: dict) -> None:
            self._safe_send(AgentProtocol.encode_message(
                AgentProtocol.create_progress_stream(seq, data)
            ), client_addr)

        def run() -> None:
            try:
                result = pkg_service.download(
                    core, device, owner, repo, ref,
                    scope_filter=scope_filter,
                    file_filter=file_filter,
                    progress=emit,
                )
                result["success"] = True
                response = AgentProtocol.create_response(seq, result=result)
            except Exception as exc:
                response = AgentProtocol.create_response(seq, error=str(exc))
            finally:
                conn.release()
            self._send_stream_completion(response, client_addr)

        threading.Thread(
            target=run,
            daemon=True,
            name=f"replx-pkg-download-{seq}",
        ).start()

    def _cmd_pkg_install(self, ctx: CommandContext, spec: str) -> None:
        conn = ctx.connection
        if not conn or not conn.file_system:
            if conn:
                conn.release()
            raise RuntimeError("Not connected")

        seq = ctx.seq
        client_addr = ctx.client_addr
        core = ctx.core
        device = ctx.device
        version = ctx.version if ctx.version not in (None, "?") else ""

        def emit(data: dict) -> None:
            self._safe_send(AgentProtocol.encode_message(
                AgentProtocol.create_progress_stream(seq, data)
            ), client_addr)

        def run() -> None:
            try:
                batch = pkg_service.build_install_batch(
                    spec, core, device, version, progress=emit
                )

                for path in batch["dirs"]:
                    real_path = self._to_real_path(path, conn)
                    try:
                        conn.file_system.mkdir(real_path)
                    except Exception:
                        pass

                files_total = batch["files"]
                total_bytes = batch["total_bytes"]
                sent = 0
                for idx, ((local_path, remote_path), size) in enumerate(
                        zip(batch["specs"], batch["sizes"])):
                    emit({
                        "phase": "install",
                        "file": posixpath.basename(remote_path),
                        "file_index": idx,
                        "files_total": files_total,
                        "file_bytes": size,
                        "current": sent,
                        "total": total_bytes,
                    })
                    real_path = self._to_real_path(remote_path, conn)
                    with conn.file_system.repl.session():
                        conn.file_system.put(local_path, real_path)
                    sent += size

                emit({
                    "phase": "install",
                    "file": "",
                    "file_index": files_total,
                    "files_total": files_total,
                    "file_bytes": 0,
                    "current": sent,
                    "total": total_bytes,
                })
                response = AgentProtocol.create_response(seq, result={
                    "success": True,
                    "files": files_total,
                    "bytes": total_bytes,
                })
            except PkgError as exc:
                response = AgentProtocol.create_response(seq, error=str(exc))
            except Exception as exc:
                response = AgentProtocol.create_response(
                    seq, error=f"pkg_install failed: {exc}")
            finally:
                conn.release()
            self._send_stream_completion(response, client_addr)

        threading.Thread(
            target=run,
            daemon=True,
            name=f"replx-pkg-install-{seq}",
        ).start()

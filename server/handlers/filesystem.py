import logging
import posixpath

from replx_agent.utils.constants import MAX_PAYLOAD_SIZE
from replx_agent.utils.exceptions import TransportError, FileSystemError
from ..command_dispatcher import CommandContext

logger = logging.getLogger(__name__)


class DisconnectedError(RuntimeError):
    pass


class FilesystemCommandsMixin:
    def _cmd_ls(self, ctx: CommandContext, path: str = "/", detailed: bool = False, recursive: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)

            if detailed or recursive:
                items = conn.file_system.ls_detailed(real_path)
                if recursive:
                    all_items = []
                    def recurse(p):
                        try:
                            items = conn.file_system.ls_detailed(p)
                            for name, size, is_dir in items:
                                full_path = f"{p.rstrip('/')}/{name}"
                                all_items.append((full_path, size, is_dir))
                                if is_dir:
                                    recurse(full_path)
                        except TransportError:
                            raise
                        except FileSystemError as e:
                            msg = str(e).lower()
                            if "enoent" in msg or "enotdir" in msg or "not found" in msg:
                                pass
                            else:
                                logger.warning("Failed to list %s: %s", p, e)
                        except Exception as e:
                            logger.warning("Unexpected error listing %s: %s", p, e)
                    recurse(real_path)
                    items = all_items

                return {
                    "items": [{"name": self._to_virtual_path(n, conn), "size": s, "is_dir": d} for n, s, d in items]
                }
            else:
                items = conn.file_system.ls(real_path)
                return {"items": items}
        except FileSystemError as e:
            msg = str(e)
            lower_msg = msg.lower()
            if "enoent" in lower_msg or "no such file" in lower_msg or "not found" in lower_msg:
                raise RuntimeError(f"Path does not exist: {path}")
            if "enotdir" in lower_msg or "not a directory" in lower_msg or "ls failed: 20" in lower_msg:
                if not recursive:
                    is_dir = False
                    file_exists = None
                    if hasattr(conn.file_system, "_file_exists"):
                        try:
                            file_exists = conn.file_system._file_exists(real_path)
                        except Exception:
                            file_exists = None
                    if file_exists is False:
                        raise RuntimeError(f"Path does not exist: {path}")
                    size = conn.file_system.state(real_path)
                    name = path.rsplit('/', 1)[-1] if '/' in path else path
                    return {
                        "items": [{"name": name, "size": size, "is_dir": is_dir}]
                    }
                raise RuntimeError(f"Path is not a directory: {path}")
            raise RuntimeError(f"ls failed: {msg}")
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"ls failed: {e}")

    def _cmd_ls_recursive(self, ctx: CommandContext, path: str = "/") -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            items = conn.file_system.ls_recursive(real_path)
            return {
                "items": [{"name": self._to_virtual_path(n, conn), "size": s, "is_dir": d} for n, s, d in items],
                "path": path
            }
        except FileSystemError as e:
            msg = str(e)
            lower_msg = msg.lower()
            if "enoent" in lower_msg or "no such file" in lower_msg or "not found" in lower_msg:
                raise RuntimeError(f"Path does not exist: {path}")
            if "enotdir" in lower_msg or "not a directory" in lower_msg or "ls failed: 20" in lower_msg:
                raise RuntimeError(f"Path is not a directory: {path}")
            raise RuntimeError(f"ls_recursive failed: {msg}")
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"ls_recursive failed: {e}")

    def _cmd_cat(self, ctx: CommandContext, path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            content = conn.file_system.get(real_path)

            if not isinstance(content, (bytes, bytearray)):
                content = content.encode('utf-8') if isinstance(content, str) else bytes(content)

            if b'\x00' in content:
                text_content = None
            else:
                try:
                    text_content = content.decode('utf-8')
                except ValueError:
                    text_content = None

            if text_content is None:
                return {
                    "content": content.hex(),
                    "is_binary": True,
                    "size": len(content)
                }

            if len(text_content) > MAX_PAYLOAD_SIZE - 1000:
                text_content = text_content[:MAX_PAYLOAD_SIZE - 1100] + "\n... [truncated]"

            return {"content": text_content, "is_binary": False}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"cat failed: {e}")

    def _cmd_rm(self, ctx: CommandContext, path: str, recursive: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            if recursive:
                conn.file_system.rmdir(real_path)
            else:
                conn.file_system.rm(real_path)
            return {"removed": path}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"rm failed: {e}")

    def _cmd_rmdir(self, ctx: CommandContext, path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            conn.file_system.rmdir(real_path)
            return {"removed": path}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"rmdir failed: {e}")

    def _cmd_mkdir(self, ctx: CommandContext, path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            if not conn.file_system.mkdir(real_path):
                raise RuntimeError(f"Could not create directory: {path}")
            return {"created": path}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"mkdir failed: {e}")

    def _cmd_is_dir(self, ctx: CommandContext, path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            result = conn.file_system.is_dir(real_path)
            return {"path": path, "is_dir": result}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"is_dir failed: {e}")

    def _cmd_touch(self, ctx: CommandContext, path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            conn.file_system.touch(real_path, core=conn.core)
            return {"created": path}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"touch failed: {e}")

    def _cmd_cp(self, ctx: CommandContext, source: str, dest: str, recursive: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_source = self._to_real_path(source, conn)
            real_dest = self._to_real_path(dest, conn)

            is_source_dir = conn.file_system.is_dir(real_source)

            if is_source_dir and not recursive:
                raise RuntimeError(f"cp: {source} is a directory (use -r to copy recursively)")

            if is_source_dir:
                dest_exists = False
                try:
                    dest_exists = conn.file_system.is_dir(real_dest)
                except TransportError:
                    raise
                except Exception:
                    pass

                if dest_exists:
                    source_name = posixpath.basename(real_source.rstrip('/'))
                    real_dest = posixpath.join(real_dest, source_name)

                try:
                    conn.file_system.mkdir(real_dest)
                except TransportError:
                    raise
                except Exception:
                    pass

                success_count = 0

                def copy_recursive(src_dir, dst_dir):
                    nonlocal success_count
                    items = conn.file_system.ls_detailed(src_dir)

                    for name, size, is_dir in items:
                        src_path = src_dir.rstrip('/') + '/' + name
                        dst_path = dst_dir.rstrip('/') + '/' + name

                        if is_dir:
                            try:
                                conn.file_system.mkdir(dst_path)
                            except TransportError:
                                raise
                            except Exception:
                                pass
                            copy_recursive(src_path, dst_path)
                        else:
                            conn.file_system.cp(src_path, dst_path)
                            success_count += 1

                copy_recursive(real_source, real_dest)

                return {"copied": True, "source": source, "dest": dest, "files_count": success_count}
            else:
                try:
                    if conn.file_system.is_dir(real_dest):
                        real_dest = posixpath.join(real_dest, posixpath.basename(real_source))
                except TransportError:
                    raise
                except Exception:
                    pass

                conn.file_system.cp(real_source, real_dest)
                return {"copied": True, "source": source, "dest": dest}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"cp failed: {e}")

    def _cmd_mv(self, ctx: CommandContext, source: str, dest: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_source = self._to_real_path(source, conn)
            real_dest = self._to_real_path(dest, conn)

            dest_is_dir = False
            try:
                dest_is_dir = conn.file_system.is_dir(real_dest)
            except TransportError:
                raise
            except Exception:
                pass

            if dest_is_dir:
                source_name = posixpath.basename(real_source.rstrip('/'))
                real_dest = posixpath.join(real_dest, source_name)

            conn.file_system.mv(real_source, real_dest)
            return {"moved": True, "source": source, "dest": dest}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"mv failed: {e}")

    def _cmd_stat(self, ctx: CommandContext, path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(path, conn)
            size = conn.file_system.state(real_path)
            is_dir = conn.file_system.is_dir(real_path)
            return {
                "path": path,
                "size": size,
                "is_dir": is_dir
            }
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"stat failed: {e}")

    def _cmd_mem(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            result = conn.file_system.mem()
            return {"mem": result}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"mem failed: {e}")

    def _cmd_df(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            result = conn.file_system.df()
            return {
                "total": result[0],
                "used": result[1],
                "free": result[2],
                "percent": result[3]
            }
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            raise RuntimeError(f"df failed: {e}")

    def _cmd_format(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            result = conn.file_system.format()
            return {"formatted": result}
        except TransportError as e:
            raise DisconnectedError(f"Serial port disconnected: {e}")
        except Exception as e:
            import traceback
            raise RuntimeError(f"format failed: {e}\n{traceback.format_exc()}")

import time
import threading
import sys

from replx_agent.utils import canon_port as _canon_port
from ..command_dispatcher import CommandContext


class SessionCommandsMixin:
    @staticmethod
    def _canon_port(port: str) -> str:
        return _canon_port(port)

    def _cmd_session_setup(self, ctx: CommandContext, port: str = None,
                           core: str = "RP2350", device: str = None,
                           as_foreground: bool = True, set_default: bool = False,
                           local_default: str = None) -> dict:
        ppid = ctx.ppid
        if not ppid:
            raise ValueError("Session PPID required")

        if not port and ctx.explicit_port:
            port = ctx.explicit_port

        if not port:
            raise ValueError("Port is required")
        
        port = self._canon_port(port)
        conn_key = port

        session = self._get_or_create_session(ppid)
        
        if local_default:
            session.default_port = self._canon_port(local_default)

        existing_conn = self._get_connection(conn_key)

        if existing_conn:
            self.session_manager.add_connection_to_session(
                ppid, port,
                as_foreground=as_foreground,
                default_port=self._effective_default_port(session)
            )

            if set_default:
                self._set_default_port(conn_key)

            return {
                "connected": True,
                "device": existing_conn.device,
                "core": existing_conn.core,
                "version": existing_conn.version,
                "manufacturer": existing_conn.manufacturer,
                "device_root_fs": existing_conn.device_root_fs,
                "port": port,
                "is_foreground": session.foreground == conn_key if session.foreground else False,
                "existing": True,
                "switched_from": None
            }

        new_conn = self._create_board_connection(
            port=port,
            core=core,
            device=device
        )

        self._add_connection(conn_key, new_conn)

        self.session_manager.add_connection_to_session(
            ppid, port,
            as_foreground=as_foreground,
            default_port=self._effective_default_port(session)
        )

        if set_default:
            self._set_default_port(conn_key)

        return {
            "connected": True,
            "device": new_conn.device,
            "core": new_conn.core,
            "version": new_conn.version,
            "manufacturer": new_conn.manufacturer,
            "device_root_fs": new_conn.device_root_fs,
            "port": port,
            "is_foreground": session.foreground == conn_key if session.foreground else False,
            "existing": False,
            "switched_from": None
        }

    def _cmd_session_disconnect(self, ctx: CommandContext, port: str = None, all_ports: bool = False) -> dict:
        ppid = ctx.ppid
        if not port and ctx.explicit_port:
            port = ctx.explicit_port
        port = self._canon_port(port) if port else None

        ports_to_close = []
        session = self._get_session(ppid) if ppid else None
        old_foreground = session.foreground if session else None

        if all_ports:
            if not ppid:
                raise ValueError("Session PPID required")
            if not session:
                raise ValueError("No session found for this terminal")
            ports_to_close = list(session.get_all_connections())
            freed_port = "all"
        elif port:
            ports_to_close = [port]
            freed_port = port
        else:
            if not ppid:
                raise ValueError("Session PPID required")
            if not session:
                raise ValueError("No session found for this terminal")
            if not session.foreground:
                raise ValueError("No foreground connection to free")
            ports_to_close = [session.foreground]
            freed_port = session.foreground

        for conn_port in ports_to_close:
            # A user-requested disconnect is global: close the physical port
            # even if another session references it or the board is busy, then
            # remove it from every session. Session.remove_connection promotes
            # the most recent background to foreground for each affected
            # session.
            conn = self.connection_manager.get_connection(conn_port)
            if conn:
                self.connection_manager.disconnect(conn_port)
            # Always clean up session references even if the connection was
            # already removed (e.g., by a concurrent _handle_port_disconnect).
            self.session_manager.remove_connection_from_all_sessions(conn_port)

        self.session_manager.cleanup_empty_sessions()

        session = self._get_session(ppid) if ppid else None
        remaining_connections = len(session.get_all_connections()) if session and not session.is_empty() else 0
        new_foreground = session.foreground if session and not session.is_empty() else None
        fg_changed = old_foreground != new_foreground

        return {
            "freed_port": freed_port,
            "new_foreground": new_foreground,
            "fg_changed": fg_changed,
            "remaining_connections": remaining_connections
        }

    def _cmd_session_switch_fg(self, ctx: CommandContext) -> dict:
        ppid = ctx.ppid
        if not ppid:
            return {"success": False, "error": "Session PPID required"}

        port = self._canon_port(ctx.explicit_port)
        if not port:
            return {"success": False, "error": "Port required"}

        conn = self.connection_manager.get_connection(port)
        if not conn:
            return {"success": False, "error": f"Connection {port} not found"}

        session = self._get_session(ppid)
        if not session:
            session = self._get_or_create_session(ppid)

        old_fg = session.foreground

        session.add_connection(port, as_foreground=True)

        return {
            "success": True,
            "old_foreground": old_fg,
            "new_foreground": port,
            "session_created": old_fg is None
        }

    def _cmd_set_default(self, ctx: CommandContext, port: str = None, update_session: bool = False) -> dict:
        port = port or ctx.explicit_port
        if port:
            port = self._canon_port(port)
            self._set_default_port(port)
            
            if update_session and ctx.ppid:
                session = self.session_manager.add_connection_to_session(
                    ctx.ppid,
                    port,
                    as_foreground=True,
                    default_port=port,
                )
                session.default_port = port
            
            return {"default": port, "set": True}
        else:
            self._default_port = None
            return {"default": None, "set": False}

    def _cmd_release(self, ctx: CommandContext) -> dict:
        if ctx.ppid:
            if ctx.explicit_port:
                return self._cmd_session_disconnect(ctx, port=ctx.explicit_port)
            else:
                return self._cmd_session_disconnect(ctx, all_ports=False)

        if ctx.explicit_port:
            return self._cmd_disconnect_port(ctx, port=ctx.explicit_port)
        return {"released": False, "port": ""}

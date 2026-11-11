from typing import Optional, Hashable

from .connection_manager import BoardConnection
from replx_agent.commands import CmdGroups

class CommandContext:
    def __init__(
        self,
        connection: Optional[BoardConnection] = None,
        ppid: Optional[Hashable] = None,
        sid: Optional[int] = None,
        client_ip: Optional[str] = None,
        explicit_port: Optional[str] = None,
        seq: int = 0,
        client_addr: tuple = None
    ):
        self.connection = connection
        self.ppid = ppid
        self.sid = sid if sid is not None else (ppid[1] if isinstance(ppid, tuple) else ppid)
        address = getattr(client_addr, 'address', client_addr)
        self.client_ip = client_ip or (address[0] if address else None)
        self.explicit_port = explicit_port
        self.seq = seq
        self.client_addr = client_addr

        if connection:
            self.repl_protocol = connection.repl_protocol
            self.file_system = connection.file_system
            self.port = connection.port
            self.core = connection.core
            self.device = connection.device
            self.manufacturer = connection.manufacturer
            self.version = connection.version
            self.device_root_fs = connection.device_root_fs
        else:
            self.repl_protocol = None
            self.file_system = None
            self.port = ""
            self.core = ""
            self.device = ""
            self.manufacturer = "?"
            self.version = "?"
            self.device_root_fs = "/"

NON_REPL_COMMANDS = CmdGroups.NON_REPL
READ_ONLY_COMMANDS = CmdGroups.READ_ONLY
SESSION_COMMANDS = CmdGroups.SESSION
PERSISTENT_BUSY_COMMANDS = CmdGroups.PERSISTENT_BUSY
STREAMING_COMMANDS = CmdGroups.STREAMING

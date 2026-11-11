import os
import sys
import time
import threading
import ipaddress
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Set, Tuple, Hashable

from replx_agent.utils import canon_port as _canon_port


@dataclass
class Session:
    ppid: int
    client_ip: str = '127.0.0.1'
    foreground: Optional[str] = None
    # Ordered list (most-recently added last). When the foreground is removed,
    # the most recent background is promoted, giving deterministic behaviour.
    backgrounds: List[str] = field(default_factory=list)
    last_access: float = field(default_factory=time.time)
    default_port: Optional[str] = None 

    def _remove_bg(self, port: str) -> bool:
        try:
            self.backgrounds.remove(port)
            return True
        except ValueError:
            return False

    def add_connection(self, port: str, as_foreground: bool = False):
        port = _canon_port(port)
        if as_foreground:
            if self.foreground and self.foreground != port:
                # Demote previous foreground to most-recent background.
                self._remove_bg(self.foreground)
                self.backgrounds.append(self.foreground)
            self.foreground = port
            self._remove_bg(port)
        else:
            if not self.foreground or self.foreground != port:
                # Move-to-end semantics so promoted bg is the most recent.
                self._remove_bg(port)
                self.backgrounds.append(port)
        self.last_access = time.time()

    def remove_connection(self, port: str) -> bool:
        port = _canon_port(port)
        was_foreground = self.foreground and self.foreground == port
        if was_foreground:
            # Promote the most recently used background.
            self.foreground = self.backgrounds.pop() if self.backgrounds else None
        else:
            self._remove_bg(port)
        self.last_access = time.time()
        return was_foreground

    def get_all_connections(self) -> List[str]:
        result = list(self.backgrounds)
        if self.foreground:
            result.insert(0, self.foreground)
        return result

    def has_connection(self, port: str) -> bool:
        port = _canon_port(port)
        if self.foreground and self.foreground == port:
            return True
        return port in self.backgrounds

    def is_empty(self) -> bool:
        return self.foreground is None and not self.backgrounds

    def switch_foreground(self, port: str) -> bool:
        port = _canon_port(port)
        if self.foreground and self.foreground == port:
            return True

        if port not in self.backgrounds:
            return False

        if self.foreground:
            self._remove_bg(self.foreground)
            self.backgrounds.append(self.foreground)

        self._remove_bg(port)
        self.foreground = port
        self.last_access = time.time()

        return True

class SessionManager:
    REMOTE_SESSION_TTL = 300.0

    def __init__(self):
        self._sessions: Dict[Hashable, Session] = {}
        self._sessions_lock = threading.RLock()
        self._last_zombie_check = time.time()
        self._zombie_check_interval = 60

    @staticmethod
    def session_key(ppid: int, client_ip: str = None) -> Optional[Hashable]:
        if ppid is None:
            return None
        normalized_ip = str(client_ip or '127.0.0.1').split('%', 1)[0]
        try:
            normalized_ip = ipaddress.ip_address(normalized_ip).compressed
        except ValueError:
            pass
        if normalized_ip in {'127.0.0.1', '::1'}:
            return ppid
        return normalized_ip, ppid

    @staticmethod
    def _session_identity(session_id: Hashable) -> tuple[int, str]:
        if isinstance(session_id, tuple) and len(session_id) == 2:
            return session_id[1], str(session_id[0])
        return session_id, '127.0.0.1'

    def get_session(self, ppid: Hashable) -> Optional[Session]:
        with self._sessions_lock:
            session = self._sessions.get(ppid)
            if session:
                session.last_access = time.time()
            return session

    def get_or_create_session(self, ppid: Hashable) -> Session:
        with self._sessions_lock:
            if ppid not in self._sessions:
                terminal_id, client_ip = self._session_identity(ppid)
                self._sessions[ppid] = Session(ppid=terminal_id, client_ip=client_ip)
            session = self._sessions[ppid]
            session.last_access = time.time()
            return session

    def get_all_sessions(self) -> Dict[Hashable, Session]:
        with self._sessions_lock:
            return dict(self._sessions)

    def remove_session(self, ppid: Hashable) -> Optional[Session]:
        with self._sessions_lock:
            return self._sessions.pop(ppid, None)

    def add_connection_to_session(
        self,
        ppid: Hashable,
        port: str,
        as_foreground: bool = False,
        default_port: str = None
    ) -> Session:
        port = _canon_port(port)
        default_port = _canon_port(default_port) if default_port else None
        
        with self._sessions_lock:
            if ppid not in self._sessions:
                terminal_id, client_ip = self._session_identity(ppid)
                self._sessions[ppid] = Session(ppid=terminal_id, client_ip=client_ip)
            session = self._sessions[ppid]
            session.last_access = time.time()
            
            if as_foreground:
                session.add_connection(port, as_foreground=True)
            elif session.foreground is None:
                if default_port and default_port != port:
                    session.add_connection(default_port, as_foreground=True)
                    session.add_connection(port, as_foreground=False)
                else:
                    session.add_connection(port, as_foreground=True)
            else:
                session.add_connection(port, as_foreground=False)

            for other_ppid in list(self._sessions.keys()):
                if other_ppid == ppid:
                    continue
                other_sess = self._sessions.get(other_ppid)
                if other_sess and other_sess.has_connection(port) and not self.is_session_alive(other_sess):
                    other_sess.remove_connection(port)
                    if other_sess.is_empty():
                        self._sessions.pop(other_ppid, None)

        return session

    def remove_connection_from_session(self, ppid: Hashable, port: str) -> Tuple[bool, bool]:
        port = _canon_port(port)
        session = self.get_session(ppid)
        if not session:
            return False, False

        with self._sessions_lock:
            was_foreground = session.remove_connection(port)
            if session.is_empty():
                self._sessions.pop(ppid, None)
            return True, was_foreground

    def remove_connection_from_all_sessions(self, port: str) -> List[Hashable]:
        port = _canon_port(port)
        affected_sessions = []

        with self._sessions_lock:
            for ppid, session in list(self._sessions.items()):
                if session.has_connection(port):
                    session.remove_connection(port)
                    affected_sessions.append(ppid)
                    if session.is_empty():
                        self._sessions.pop(ppid, None)

        return affected_sessions

    def get_foreground(self, ppid: Hashable) -> Optional[str]:
        session = self.get_session(ppid)
        return session.foreground if session else None

    def switch_foreground(self, ppid: Hashable, port: str) -> bool:
        port = _canon_port(port)
        session = self.get_session(ppid)
        if not session:
            return False

        with self._sessions_lock:
            return session.switch_foreground(port)

    def resolve_port(self, ppid: Hashable, explicit_port: str = None, default_port: str = None) -> Optional[str]:
        if explicit_port:
            return _canon_port(explicit_port)

        session = self.get_session(ppid)
        if session and session.foreground:
            return session.foreground

        if default_port:
            return _canon_port(default_port)

        return None

    def is_process_alive(self, pid: int) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return False
            proc = psutil.Process(pid)
            return proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            pass
        try:
            if sys.platform == 'win32':
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                STILL_ACTIVE = 259
                
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    try:
                        exit_code = wintypes.DWORD()
                        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                            return exit_code.value == STILL_ACTIVE
                        return False
                    finally:
                        kernel32.CloseHandle(handle)
                return False
            else:
                os.kill(pid, 0)
                return True
        except PermissionError:
            # Process exists but we lack permission to signal it — treat as alive.
            return True
        except OSError:
            return False

    @staticmethod
    def _is_local_ip(host: str) -> bool:
        try:
            address = ipaddress.ip_address(str(host).split('%', 1)[0])
        except ValueError:
            return False
        if address.is_loopback:
            return True
        try:
            import psutil
            for interface_addresses in psutil.net_if_addrs().values():
                for interface_address in interface_addresses:
                    candidate = interface_address.address.split('%', 1)[0]
                    if candidate == str(address):
                        return True
        except Exception:
            pass
        return False

    def is_session_alive(self, session: Session) -> bool:
        if self._is_local_ip(session.client_ip):
            return self.is_process_alive(session.ppid)
        return time.time() - session.last_access <= self.REMOTE_SESSION_TTL

    def cleanup_zombie_sessions(self, connected_ports: Optional[Set[str]] = None) -> List[Hashable]:
        with self._sessions_lock:
            zombie_sessions = []
            live_ports: Set[str] = set()
            canon_connected = {_canon_port(p) for p in connected_ports} if connected_ports is not None else None

            for session_id, sess in list(self._sessions.items()):
                if not self.is_session_alive(sess):
                    zombie_sessions.append(session_id)
                else:
                    for p in sess.get_all_connections():
                        live_ports.add(_canon_port(p))

            for session_id in zombie_sessions:
                sess = self._sessions.get(session_id)
                if not sess:
                    continue
                for p in list(sess.get_all_connections()):
                    cp = _canon_port(p)
                    if (canon_connected is not None and cp not in canon_connected) or (cp in live_ports):
                        sess.remove_connection(cp)
                    else:
                        live_ports.add(cp)
                if sess.is_empty():
                    self._sessions.pop(session_id, None)

            self._last_zombie_check = time.time()
            return zombie_sessions

    def get_session_info(self) -> Dict[str, Any]:
        sessions_info = []

        with self._sessions_lock:
            for session_id, session in self._sessions.items():
                sessions_info.append({
                    'ppid': session.ppid,
                    'sid': session.ppid,
                    'client_ip': session.client_ip,
                    'session_key': f'{session.client_ip}/{session.ppid}',
                    'foreground': session.foreground,
                    'backgrounds': list(session.backgrounds),
                    'last_access': session.last_access
                })

        return {'sessions': sessions_info}

    def find_sessions_using_port(self, port: str) -> List[Dict[str, Any]]:
        result = []

        with self._sessions_lock:
            for session_id, session in self._sessions.items():
                if session.has_connection(port):
                    is_fg = session.foreground and session.foreground == port
                    result.append({
                        'ppid': session.ppid,
                        'sid': session.ppid,
                        'client_ip': session.client_ip,
                        'session_key': f'{session.client_ip}/{session.ppid}',
                        'is_foreground': is_fg
                    })

        return result

    def remove_client(self, client_ip: str) -> list[tuple[Hashable, Session]]:
        normalized_ip = str(ipaddress.ip_address(str(client_ip).split('%', 1)[0]))
        with self._sessions_lock:
            session_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session.client_ip == normalized_ip
            ]
            return [
                (session_id, self._sessions.pop(session_id))
                for session_id in session_ids
            ]

    def clear_all_sessions(self):
        with self._sessions_lock:
            self._sessions.clear()

    def cleanup_empty_sessions(self) -> List[Hashable]:
        with self._sessions_lock:
            empty = [ppid for ppid, sess in self._sessions.items() if sess.is_empty()]
            for ppid in empty:
                self._sessions.pop(ppid, None)
            return empty

    def has_sessions(self) -> bool:
        with self._sessions_lock:
            return len(self._sessions) > 0

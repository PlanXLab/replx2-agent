import json
import logging
import socket
import socketserver
import sys
import threading
from typing import Optional

from replx_agent.rpc.protocol import AgentProtocol, FrameType, FrameReader

logger = logging.getLogger(__name__)

LOOP_COMMANDS: frozenset[str] = frozenset({
    "ping",
    "status",
    "session_info",
})

FAST_COMMANDS: frozenset[str] = frozenset({
    "repl_read",
})

_RESPONSE_CACHE_MAX_BYTES = 16 * 1024 * 1024


class AgentTcpPeer:
    def __init__(self, sock: socket.socket, address: tuple):
        self.sock = sock
        self.address = address
        self._send_lock = threading.Lock()
        self.closed = False
        self.authenticated = False
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, data: bytes) -> None:
        with self._send_lock:
            if self.closed:
                raise ConnectionError("Client connection is closed")
            self.sock.sendall(data)

    def close(self) -> None:
        with self._send_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def __repr__(self) -> str:
        return f"AgentTcpPeer({self.address!r})"


class _AgentRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        peer = AgentTcpPeer(self.request, self.client_address)
        reader = FrameReader(self.request)
        server = self.server.agent_server
        try:
            while server.running:
                try:
                    frame = reader.receive()
                    if frame is None:
                        continue
                    frame_type, seq, payload = AgentProtocol.decode_frame(frame)
                except ConnectionError:
                    break
                except (OSError, ValueError) as exc:
                    logger.warning("Invalid TCP frame from %s: %s", peer, exc)
                    break

                if frame_type is FrameType.BINARY:
                    if server.auth_token and not peer.authenticated:
                        logger.warning("Unauthenticated binary frame from %s", peer)
                        break
                    server._handle_binary_frame(peer, seq, payload)
                    continue

                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid JSON message from %s: %s", peer, exc)
                    break
                if not isinstance(msg, dict):
                    logger.warning("Invalid message from %s", peer)
                    break

                msg_type = msg.get("type")
                if msg_type in {"input", "stream_ack", "transfer_end", "transfer_abort"}:
                    server._handle_request(msg, peer)
                    continue

                command = msg.get("command")
                if command in {"transfer_start", "firmware_stage"}:
                    server._handle_request(msg, peer)
                    continue
                if command == "shutdown":
                    server._handle_request(msg, peer)
                    continue
                if command in LOOP_COMMANDS:
                    server._handle_direct(msg, peer)
                    continue

                executor = (
                    server._fast_executor
                    if command in FAST_COMMANDS
                    else server._slow_executor
                )
                executor.submit(server._handle_request, msg, peer)
        finally:
            server._handle_peer_disconnect(peer)
            peer.close()


class AgentTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], agent_server):
        self.agent_server = agent_server
        self.address_family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        super().__init__(address, _AgentRequestHandler)


class TcpReliabilityMixin:
    def _init_tcp_reliability(
        self,
        *,
        response_cache_max: int = 512,
        response_cache_max_bytes: int = _RESPONSE_CACHE_MAX_BYTES,
        last_seq_max: int = 256,
    ) -> None:
        self.last_seq: dict[AgentTcpPeer, int] = {}
        self._last_seq_lock = threading.Lock()
        self._MAX_LAST_SEQ = last_seq_max
        self._response_cache: dict[tuple[AgentTcpPeer, int], bytes] = {}
        self._response_cache_bytes = 0
        self._response_cache_lock = threading.Lock()
        self._RESPONSE_CACHE_MAX = response_cache_max
        self._RESPONSE_CACHE_MAX_BYTES = response_cache_max_bytes

    def _safe_send(self, data: bytes, peer: AgentTcpPeer) -> bool:
        try:
            peer.send(data)
            return True
        except (ConnectionError, OSError) as exc:
            if not peer.closed:
                logger.warning("TCP response to %s failed: %s", peer, exc)
            return False

    def _cache_response(self, peer: AgentTcpPeer, seq: int, data: bytes) -> None:
        key = (peer, seq)
        with self._response_cache_lock:
            previous = self._response_cache.pop(key, None)
            if previous is not None:
                self._response_cache_bytes -= len(previous)
            self._response_cache[key] = data
            self._response_cache_bytes += len(data)

            while (
                len(self._response_cache) > self._RESPONSE_CACHE_MAX
                or self._response_cache_bytes > self._RESPONSE_CACHE_MAX_BYTES
            ):
                oldest = next(iter(self._response_cache))
                evicted = self._response_cache.pop(oldest)
                self._response_cache_bytes -= len(evicted)

    def _lookup_response(self, peer: AgentTcpPeer, seq: int) -> Optional[bytes]:
        with self._response_cache_lock:
            return self._response_cache.get((peer, seq))

    def _forget_peer(self, peer: AgentTcpPeer) -> None:
        with self._last_seq_lock:
            self.last_seq.pop(peer, None)
        with self._response_cache_lock:
            for key in [key for key in self._response_cache if key[0] is peer]:
                response = self._response_cache.pop(key)
                self._response_cache_bytes -= len(response)

    def _check_and_record_seq(self, peer: AgentTcpPeer, seq: int) -> bool:
        with self._last_seq_lock:
            if peer in self.last_seq and seq == self.last_seq[peer]:
                return False
            self.last_seq[peer] = seq
            if len(self.last_seq) > self._MAX_LAST_SEQ:
                oldest = next(iter(self.last_seq))
                del self.last_seq[oldest]
            return True

    def send_completion(self, encoded: bytes, peer: AgentTcpPeer, seq: int) -> bool:
        return self._safe_send(encoded, peer)

    def _ack_stream_completion(self, peer: AgentTcpPeer, seq: int) -> None:
        return None

    def _send_duplicate_reply(self, peer: AgentTcpPeer, seq: int) -> None:
        cached = self._lookup_response(peer, seq)
        self._safe_send(cached or AgentProtocol.encode_message(
            AgentProtocol.create_ack(seq)
        ), peer)

import base64
import json
import socket
import struct
import threading
import time
from enum import IntEnum
from typing import Any, Dict, List, Optional

from replx_agent.utils.constants import MAX_PAYLOAD_SIZE

_MAGIC = b"RPLX"
_VERSION = 2
_HEADER = struct.Struct("!4sBBII")
_MAX_BINARY_FRAME_SIZE = 256 * 1024
_READ_SIZE = 64 * 1024


class FrameType(IntEnum):
    JSON = 1
    BINARY = 2


_seq_lock = threading.Lock()
_seq_counter = int(time.time() * 1000000) & 0xFFFFFFFE


def _next_seq() -> int:
    global _seq_counter
    with _seq_lock:
        _seq_counter = (_seq_counter + 1) & 0xFFFFFFFF
        return _seq_counter


class FrameReader:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = bytearray()

    def _extract_frame(self) -> Optional[bytes]:
        if len(self._buffer) < _HEADER.size:
            return None

        magic, version, frame_type, _seq, length = _HEADER.unpack_from(self._buffer)
        if magic != _MAGIC or version != _VERSION:
            raise ValueError("Invalid agent frame header")
        try:
            kind = FrameType(frame_type)
        except ValueError as exc:
            raise ValueError(f"Unknown agent frame type: {frame_type}") from exc

        maximum = MAX_PAYLOAD_SIZE if kind is FrameType.JSON else _MAX_BINARY_FRAME_SIZE
        if length > maximum:
            raise ValueError(f"Agent frame exceeds {maximum} bytes")

        frame_size = _HEADER.size + length
        if len(self._buffer) < frame_size:
            return None

        frame = bytes(self._buffer[:frame_size])
        del self._buffer[:frame_size]
        return frame

    def receive(self, timeout: Optional[float] = None) -> Optional[bytes]:
        self._sock.settimeout(timeout)
        while True:
            frame = self._extract_frame()
            if frame is not None:
                return frame

            try:
                chunk = self._sock.recv(_READ_SIZE)
            except socket.timeout:
                return None

            if not chunk:
                if self._buffer:
                    raise ConnectionError("Agent closed the connection mid-frame")
                raise ConnectionError("Agent closed the connection")
            self._buffer.extend(chunk)


class AgentProtocol:
    @staticmethod
    def _encode_frame(frame_type: FrameType, seq: int, payload: bytes) -> bytes:
        maximum = MAX_PAYLOAD_SIZE if frame_type is FrameType.JSON else _MAX_BINARY_FRAME_SIZE
        if len(payload) > maximum:
            raise ValueError(f"Agent frame too large: {len(payload)} bytes")
        return _HEADER.pack(_MAGIC, _VERSION, int(frame_type), seq & 0xFFFFFFFF, len(payload)) + payload

    @staticmethod
    def encode_message(msg: Dict[str, Any]) -> bytes:
        payload = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        return AgentProtocol._encode_frame(
            FrameType.JSON, int(msg.get("seq", 0)), payload
        )

    @staticmethod
    def encode_binary(seq: int, payload: bytes) -> bytes:
        return AgentProtocol._encode_frame(FrameType.BINARY, seq, payload)

    @staticmethod
    def decode_frame(data: bytes) -> tuple[FrameType, int, bytes]:
        if len(data) < _HEADER.size:
            raise ValueError("Agent frame header is incomplete")

        magic, version, frame_type, seq, length = _HEADER.unpack_from(data)
        if magic != _MAGIC or version != _VERSION:
            raise ValueError("Invalid agent frame header")
        try:
            kind = FrameType(frame_type)
        except ValueError as exc:
            raise ValueError(f"Unknown agent frame type: {frame_type}") from exc

        maximum = MAX_PAYLOAD_SIZE if kind is FrameType.JSON else _MAX_BINARY_FRAME_SIZE
        if length > maximum:
            raise ValueError(f"Agent frame exceeds {maximum} bytes")
        if len(data) != _HEADER.size + length:
            raise ValueError("Agent frame length does not match its header")

        return kind, seq, data[_HEADER.size:]

    @staticmethod
    def decode_message(data: bytes) -> Optional[Dict[str, Any]]:
        try:
            frame_type, _seq, payload = AgentProtocol.decode_frame(data)
            if frame_type is not FrameType.JSON:
                return None
            message = json.loads(payload.decode("utf-8"))
            return message if isinstance(message, dict) else None
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def create_request(
        command: str,
        seq: int = None,
        ppid: int = None,
        port: str = None,
        auth_token: str = None,
        **args,
    ) -> Dict[str, Any]:
        if seq is None:
            seq = _next_seq()

        request = {
            "seq": seq,
            "type": "request",
            "command": command,
            "args": args,
        }
        if ppid is not None:
            request["ppid"] = ppid
        if port is not None:
            request["port"] = port
        if auth_token:
            request["auth_token"] = auth_token
        return request

    @staticmethod
    def create_response(seq: int, result: Any = None, error: str = None) -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "response",
            "result": result,
            "error": error,
        }

    @staticmethod
    def create_ack(seq: int) -> Dict[str, Any]:
        return {"seq": seq, "type": "ack"}

    @staticmethod
    def create_stream(seq: int, data: bytes, stream_type: str = "stdout") -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "stream",
            "data": base64.b64encode(data).decode("ascii"),
            "stream_type": stream_type,
        }

    @staticmethod
    def create_progress_stream(seq: int, progress_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "stream",
            "data": progress_data,
            "stream_type": "progress",
        }

    @staticmethod
    def create_input(seq: int, data: bytes, ppid: int = None, port: str = None) -> Dict[str, Any]:
        message = {
            "seq": seq,
            "type": "input",
            "data": base64.b64encode(data).decode("ascii"),
        }
        if ppid is not None:
            message["ppid"] = ppid
        if port is not None:
            message["port"] = port
        return message

    @staticmethod
    def create_stream_ack(seq: int) -> Dict[str, Any]:
        return {"seq": seq, "type": "stream_ack"}

    @staticmethod
    def decode_stream_data(msg: Dict[str, Any]) -> bytes:
        data_b64 = msg.get("data", "")
        return base64.b64decode(data_b64) if data_b64 else b""

    @staticmethod
    def chunk_large_data(data: str, max_size: int = MAX_PAYLOAD_SIZE) -> List[str]:
        data_bytes = data.encode("utf-8")
        chunk_size = max_size - 100
        return [
            data_bytes[offset:offset + chunk_size].decode("utf-8", errors="ignore")
            for offset in range(0, len(data_bytes), chunk_size)
        ]

from ..command_dispatcher import CommandContext


class UartCommandsMixin:
    def _uart_resolve_port(self, ctx: CommandContext) -> str:
        port = self._resolve_connection_for_session(ctx.ppid, ctx.explicit_port)
        if not port:
            raise ValueError("No active connection. Connect to a board first.")
        return port

    def _cmd_uart_bus_set(self, ctx: CommandContext,
                          tx, rx, ch: int,
                          baud: int, bits: int, parity: str,
                          stop: int, timeout_ms: int,
                          family: str = 'other', id_mode: str = '') -> dict:
        port = self._uart_resolve_port(ctx)
        self._uart_bus[port] = {
            'tx': tx, 'rx': rx, 'ch': ch,
            'baud': baud, 'bits': bits, 'parity': parity,
            'stop': stop, 'timeout_ms': timeout_ms,
            'family': family, 'id_mode': id_mode,
        }
        return self._uart_bus[port]

    def _cmd_uart_bus_get(self, ctx: CommandContext) -> dict:
        port = self._uart_resolve_port(ctx)
        return self._uart_bus.get(port) or {}

    def _cmd_uart_bus_clear(self, ctx: CommandContext) -> dict:
        port = self._uart_resolve_port(ctx)
        self._uart_bus.pop(port, None)
        return {}

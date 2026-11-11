from ..command_dispatcher import CommandContext


class SpiCommandsMixin:
    def _spi_resolve_port(self, ctx: CommandContext) -> str:
        port = self._resolve_connection_for_session(ctx.ppid, ctx.explicit_port)
        if not port:
            raise ValueError("No active connection. Connect to a board first.")
        return port

    def _cmd_spi_bus_set(self, ctx: CommandContext, **kwargs) -> dict:
        port = self._spi_resolve_port(ctx)
        self._spi_bus[port] = dict(kwargs)
        return self._spi_bus[port]

    def _cmd_spi_bus_get(self, ctx: CommandContext) -> dict:
        port = self._spi_resolve_port(ctx)
        return self._spi_bus.get(port) or {}

    def _cmd_spi_bus_clear(self, ctx: CommandContext) -> dict:
        port = self._spi_resolve_port(ctx)
        self._spi_bus.pop(port, None)
        return {}

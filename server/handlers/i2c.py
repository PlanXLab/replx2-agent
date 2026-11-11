from ..command_dispatcher import CommandContext


class I2cCommandsMixin:
    def _i2c_resolve_port(self, ctx: CommandContext) -> str:
        port = self._resolve_connection_for_session(ctx.ppid, ctx.explicit_port)
        if not port:
            raise ValueError("No active connection. Connect to a board first.")
        return port

    def _cmd_i2c_bus_set(self, ctx: CommandContext, **kwargs) -> dict:
        port = self._i2c_resolve_port(ctx)
        self._i2c_bus[port] = dict(kwargs)
        return self._i2c_bus[port]

    def _cmd_i2c_bus_get(self, ctx: CommandContext) -> dict:
        port = self._i2c_resolve_port(ctx)
        return self._i2c_bus.get(port) or {}

    def _cmd_i2c_bus_clear(self, ctx: CommandContext) -> dict:
        port = self._i2c_resolve_port(ctx)
        self._i2c_bus.pop(port, None)
        return {}

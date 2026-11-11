
class Cmd:
    CONNECT = 'connect'
    DISCONNECT_PORT = 'disconnect_port'
    SESSION_SETUP = 'session_setup'
    SESSION_DISCONNECT = 'session_disconnect'
    SESSION_SWITCH_FG = 'session_switch_fg'
    SESSION_INFO = 'session_info'
    CLIENT_DISCONNECT = 'client_disconnect'
    SET_DEFAULT = 'set_default'
    RELEASE = 'release'

    STATUS = 'status'
    SCAN = 'scan'
    SHUTDOWN = 'shutdown'
    PING = 'ping'
    RESET = 'reset'

    EXEC = 'exec'
    RUN = 'run'
    RUN_STOP = 'run_stop'
    RUN_INTERACTIVE = 'run_interactive'

    REPL_ENTER = 'repl_enter'
    REPL_EXIT = 'repl_exit'
    REPL_WRITE = 'repl_write'
    REPL_READ = 'repl_read'

    LS = 'ls'
    LS_RECURSIVE = 'ls_recursive'
    CAT = 'cat'
    STAT = 'stat'
    IS_DIR = 'is_dir'

    RM = 'rm'
    RMDIR = 'rmdir'
    MKDIR = 'mkdir'
    CP = 'cp'
    MV = 'mv'
    TOUCH = 'touch'
    FORMAT = 'format'

    GET_FILE = 'get_file'
    GET_FILE_STREAMING = 'get_file_streaming'
    GET_FILE_BATCH = 'get_file_batch'
    PUT_FILE = 'put_file'
    PUT_FILE_BATCH = 'put_file_batch'
    TRANSFER_START = 'transfer_start'
    FIRMWARE_STAGE = 'firmware_stage'
    FIRMWARE_INFO = 'firmware_info'
    FIRMWARE_FLASH = 'firmware_flash'
    FIRMWARE_DISCARD = 'firmware_discard'

    MEM = 'mem'
    DF = 'df'

    I2C_BUS_SET = 'i2c_bus_set'
    I2C_BUS_GET = 'i2c_bus_get'
    I2C_BUS_CLEAR = 'i2c_bus_clear'

    UART_BUS_SET = 'uart_bus_set'
    UART_BUS_GET = 'uart_bus_get'
    UART_BUS_CLEAR = 'uart_bus_clear'

    SPI_BUS_SET = 'spi_bus_set'
    SPI_BUS_GET = 'spi_bus_get'
    SPI_BUS_CLEAR = 'spi_bus_clear'

    PKG_SEARCH = 'pkg_search'
    PKG_DOWNLOAD = 'pkg_download'
    PKG_INSTALL = 'pkg_install'
    PKG_CLEAN = 'pkg_clean'
    PKG_STORE_STATUS = 'pkg_store_status'
    PKG_TYPEHINTS_LIST = 'pkg_typehints_list'
    PKG_TYPEHINTS_GET = 'pkg_typehints_get'

class CmdGroups:
    NON_REPL = frozenset({
        Cmd.CONNECT, Cmd.RELEASE, Cmd.DISCONNECT_PORT,
        Cmd.STATUS, Cmd.SCAN, Cmd.SHUTDOWN, Cmd.PING, Cmd.RUN_STOP,
        Cmd.SESSION_INFO, Cmd.CLIENT_DISCONNECT, Cmd.SESSION_SETUP, Cmd.SESSION_DISCONNECT,
        Cmd.SESSION_SWITCH_FG, Cmd.SET_DEFAULT,
        Cmd.I2C_BUS_SET, Cmd.I2C_BUS_GET, Cmd.I2C_BUS_CLEAR,
        Cmd.UART_BUS_SET, Cmd.UART_BUS_GET, Cmd.UART_BUS_CLEAR,
        Cmd.SPI_BUS_SET, Cmd.SPI_BUS_GET, Cmd.SPI_BUS_CLEAR,
        Cmd.FIRMWARE_STAGE, Cmd.FIRMWARE_INFO, Cmd.FIRMWARE_FLASH,
        Cmd.FIRMWARE_DISCARD,
    })

    READ_ONLY = frozenset({
        Cmd.SHUTDOWN, Cmd.PING, Cmd.SESSION_INFO, Cmd.STATUS, Cmd.SCAN,
        Cmd.EXEC, Cmd.LS, Cmd.LS_RECURSIVE, Cmd.CAT, Cmd.STAT,
        Cmd.IS_DIR, Cmd.MEM, Cmd.DF,
        Cmd.GET_FILE,
        Cmd.GET_FILE_BATCH,
        Cmd.GET_FILE_STREAMING, Cmd.FIRMWARE_INFO,
        Cmd.I2C_BUS_GET,
        Cmd.UART_BUS_GET,
        Cmd.SPI_BUS_GET,
        Cmd.PKG_SEARCH, Cmd.PKG_STORE_STATUS,
        Cmd.PKG_TYPEHINTS_LIST, Cmd.PKG_TYPEHINTS_GET,
    })

    SESSION = frozenset({
        Cmd.SESSION_SETUP, Cmd.SESSION_DISCONNECT, Cmd.SESSION_SWITCH_FG,
        Cmd.CONNECT, Cmd.DISCONNECT_PORT, Cmd.CLIENT_DISCONNECT
    })

    PERSISTENT_BUSY = frozenset({
        Cmd.RUN_INTERACTIVE, Cmd.REPL_ENTER
    })

    STREAMING = frozenset({
        Cmd.GET_FILE_STREAMING, Cmd.TRANSFER_START,
        Cmd.FIRMWARE_STAGE,
        Cmd.RUN_INTERACTIVE, Cmd.RUN_STOP,
        Cmd.PKG_DOWNLOAD, Cmd.PKG_INSTALL
    })

    REPL = frozenset({
        Cmd.REPL_ENTER, Cmd.REPL_EXIT, Cmd.REPL_WRITE, Cmd.REPL_READ
    })

    DETACHED_ALLOW = frozenset({
        Cmd.RUN_STOP, Cmd.RESET, Cmd.STATUS, Cmd.PING,
        Cmd.SHUTDOWN, Cmd.SESSION_INFO,
        Cmd.SESSION_DISCONNECT, Cmd.DISCONNECT_PORT, Cmd.RELEASE
    })

NON_REPL_COMMANDS = CmdGroups.NON_REPL
READ_ONLY_COMMANDS = CmdGroups.READ_ONLY

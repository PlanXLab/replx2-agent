CTRL_A = b'\x01'  # Raw REPL mode / Paste mode
CTRL_B = b'\x02'  # Normal REPL mode / Exit paste mode
CTRL_C = b'\x03'  # Keyboard interrupt
CTRL_D = b'\x04'  # EOF / Soft reboot
CTRL_E = b'\x05'  # Enter paste mode
REPL_PROMPT = b'>>>'

EOF_MARKER = b'\x04'

RAW_REPL_PROMPT = b'raw REPL; CTRL-B to exit\r\n>'
SOFT_REBOOT_MSG = b'soft reboot\r\n'
OK_RESPONSE = b'OK'

ERROR_HEADER = b"Traceback (most recent call last):"

RAW_PASTE_INIT = b'\x05A\x01'
RAW_PASTE_SUPPORTED = b'R\x01'
RAW_PASTE_NOT_SUPPORTED = b'R\x00'
RAW_PASTE_FALLBACK = b'raw REPL; CTRL-B'
RAW_PASTE_WINDOW_INC = b'\x01'
RAW_PASTE_END_DATA = b'\x04'

PORT_SCAN_TIMEOUT = 0.05
REPL_READ_TIMEOUT = 0.5
SERIAL_TIMEOUT = 0.5
SERIAL_WRITE_TIMEOUT = 0.3

REPL_BUFSIZE = 2048
RAW_PASTE_DEFAULT_WINDOW_SIZE = 128

DEVICE_CHUNK_SIZE_DEFAULT = 4096
DEVICE_CHUNK_SIZE_EFR32MG = 256

PUT_BATCH_BYTES_DEFAULT = 16 * 1024
PUT_BATCH_BYTES_EFR32MG = 2 * 1024

RAW_MODE_DELAY_DEFAULT = 0.0
RAW_MODE_DELAY_EFR32MG = 0.01

# REPL baud rates tried in order when opening a serial port. Native USB CDC
# ignores the value; USB-UART bridges (e.g. the CH343 on ESP32-P4 boards) need
# the rate the firmware was built with. REPLX_BAUD prepends extra candidates.
REPL_BAUD_DEFAULT = 115200
REPL_BAUD_CANDIDATES = (115200, 921600)

# Above this baud rate a firmware with a small stdin buffer can be overrun by
# plain raw-REPL writes, so longer commands use flow-controlled raw-paste.
RAW_PASTE_SAFE_BAUD = 115200
RAW_PASTE_HIGH_BAUD_MIN_COMMAND_SIZE = 256
# Baud/REPL detection sweeps on connect (covers a board still booting after reset)
CONNECT_DETECT_ROUNDS = 3
CONNECT_DETECT_RETRY_DELAY = 0.5

DEFAULT_AGENT_PORT = 49153
MIN_AGENT_PORT = 49153
MAX_AGENT_PORT = 65535
AGENT_HOST = '127.0.0.1'

MAX_CONNECTIONS = 20
AGENT_SOCKET_TIMEOUT = 1.0
HTTP_REQUEST_TIMEOUT = 15

MAX_PAYLOAD_SIZE = 60000

PUTDIR_EXCLUDED_DIRS = frozenset({
    '__pycache__', '.git', '.svn', '.hg', 'node_modules',
    '.venv', 'venv', '__MACOSX',
})
PUTDIR_EXCLUDED_EXTENSIONS = frozenset({
    '.pyc', '.pyo', '.pyd', '.so', '.dll', '.dylib',
})

HEARTBEAT_INTERVAL = 1
ZOMBIE_CHECK_INTERVAL = 10
IDLE_COMMAND_THRESHOLD = 5
GC_COLLECT_INTERVAL = 60

GC_THRESHOLD = (1500, 15, 15)

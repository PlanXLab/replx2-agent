import os
import ast
import base64
import json
import textwrap
import posixpath
import tempfile
import zlib
from typing import Tuple, List

from replx_agent.utils.exceptions import ProtocolError, TransportError, FileSystemError
from replx_agent.utils.constants import PUTDIR_EXCLUDED_DIRS, PUTDIR_EXCLUDED_EXTENSIONS
from replx_agent.utils.device_info import normalize_core, get_core_profile


_DEFLATE_WBITS = 12


def _python_string_literal(value: str) -> str:
    return repr(value)


class DeviceStorage:

    def __init__(self, repl_protocol, core: str = "RP2350", device: str = "", device_root_fs: str = "/"):
        self.repl = repl_protocol
        self.core = normalize_core(core)
        self.device = device
        self.device_root_fs = device_root_fs
        
        _profile = get_core_profile(self.core)
        self._DEVICE_CHUNK_SIZES = _profile['chunk_size']
        self._PUT_BATCH_BYTES = _profile['put_batch_bytes']
        self._GET_CHUNK_SIZE = _profile['get_chunk_size']
        
        if self.core == "EFR32MG":
            self._ls_detailed_func = self._ls_detailed_xbee
            self._ls_recursive_func = self._ls_recursive_xbee
        else:
            self._ls_detailed_func = self._ls_detailed_standard
            self._ls_recursive_func = self._ls_recursive_standard

    def ls(self, path: str = "/") -> List[str]:
        path_literal = _python_string_literal(path)
        command = f"""
import os
try:
    print(os.listdir({path_literal}))
except Exception as e:
    print('__ERROR__:' + str(e))
"""
        result = self.repl.exec(command).decode('utf-8').strip()
        if result.startswith("__ERROR__:"):
            raise FileSystemError(f"ls failed: {result[len('__ERROR__:'):].strip()}")
        try:
            items = ast.literal_eval(result)
        except (ValueError, SyntaxError) as e:
            raise FileSystemError(f"Invalid ls response: {e}") from e
        if not isinstance(items, list):
            raise FileSystemError("Invalid ls response: expected a list")
        return items

    def ls_detailed(self, path: str = "/") -> List[list]:
        return self._ls_detailed_func(path)

    def ls_recursive(self, path: str = "/") -> List[list]:
        return self._ls_recursive_func(path)

    def _ls_detailed_standard(self, dir: str = "/") -> List[list]:
        dir = self._normalize_remote_path(dir)
        if not dir.startswith("/"):
            dir = "/" + dir
        if dir != '/' and dir.endswith('/'):
            dir = dir.rstrip('/')
        dir_literal = _python_string_literal(dir)
        
        command = f"""
import os
import json
def get_detailed_listing(path):
    try:
        items = []
        for item in os.listdir(path):
            full_path = path + ('/' + item if path != '/' else item)
            try:
                stat_info = os.stat(full_path)
                is_dir = stat_info[0] & 0x4000 != 0
                size = 0 if is_dir else stat_info[6]
                items.append([item, size, is_dir])
            except Exception:
                try:
                    os.listdir(full_path)
                    items.append([item, 0, True])
                except Exception:
                    items.append([item, 0, False])
        return sorted(items, key=lambda x: (not x[2], x[0].lower()))
    except Exception as e:
        return {{'__error__': str(e)}}
print(json.dumps(get_detailed_listing({dir_literal})))
"""
        try:
            out = self.repl.exec(command)
            result_str = out.decode("utf-8", errors='replace').strip()
            result = json.loads(result_str)
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls failed: {result['__error__']}")
            return result
        except TransportError:
            raise
        except json.JSONDecodeError as e:
            raise FileSystemError(f"Invalid directory listing response: {e}") from e

    def _ls_detailed_xbee(self, dir: str = "/") -> List[list]:
        dir = self._normalize_remote_path(dir)
        if not dir.startswith("/"):
            dir = "/" + dir
        dir_literal = _python_string_literal(dir)
        
        command = f"""
import os
import json
def xbee_is_dir(path):
    try:
        os.listdir(path)
        return True
    except OSError:
        return False

def xbee_get_file_size(path):
    size = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(256)
            if not chunk:
                break
            size += len(chunk)
    return size

def get_detailed_listing(path):
    try:
        items = []
        for item in os.listdir(path):
            full_path = path + ('/' + item if path != '/' else item)
            is_dir = xbee_is_dir(full_path)
            if is_dir:
                size = 0
            else:
                size = xbee_get_file_size(full_path)
            items.append([item, size, is_dir])
        return sorted(items, key=lambda x: (not x[2], x[0].lower()))
    except Exception as e:
        return {{'__error__': str(e)}}
print(json.dumps(get_detailed_listing({dir_literal})))
"""
        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls failed: {result['__error__']}")
            return result
        except TransportError:
            raise
        except json.JSONDecodeError as e:
            raise FileSystemError(f"Invalid directory listing response: {e}") from e

    def _ls_recursive_standard(self, dir: str = "/") -> List[list]:
        dir = self._normalize_remote_path(dir)
        if not dir.startswith("/"):
            dir = "/" + dir
        safe_dir = _python_string_literal(dir)
        
        command = f"""
import os
import json

def get_recursive_listing(path, base_path=None):
    if base_path is None:
        base_path = path
    items = []
    try:
        entries = os.listdir(path)
        for item in entries:
            full_path = path + ('/' + item if path != '/' else item)
            
            # Calculate relative path from base
            if base_path == '/':
                rel_path = full_path
            else:
                rel_path = full_path[len(base_path):].lstrip('/')
            
            try:
                stat_info = os.stat(full_path)
                is_dir = stat_info[0] & 0x4000 != 0
                size = 0 if is_dir else stat_info[6]
            except Exception:
                try:
                    os.listdir(full_path)
                    is_dir = True
                    size = 0
                except Exception:
                    is_dir = False
                    size = 0
            
            if is_dir:
                items.append([rel_path, 0, True])
                child = get_recursive_listing(full_path, base_path)
                if isinstance(child, dict) and '__error__' in child:
                    return child
                items.extend(child)
            else:
                items.append([rel_path, size, False])
        
    except Exception as e:
        return {{'__error__': str(e)}}
    
    return sorted(items, key=lambda x: x[0].lower())

print(json.dumps(get_recursive_listing({safe_dir})))
"""
        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls_recursive failed: {result['__error__']}")
            return result
        except TransportError:
            raise
        except json.JSONDecodeError:
            return self._ls_recursive_fallback(dir)

    def _ls_recursive_xbee(self, dir: str = "/") -> List[list]:
        dir = self._normalize_remote_path(dir)
        if not dir.startswith("/"):
            dir = "/" + dir
        safe_dir = _python_string_literal(dir)
        
        command = f"""
import os
import json

def xbee_is_dir(path):
    try:
        os.listdir(path)
        return True
    except OSError:
        return False

def xbee_get_file_size(path):
    size = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(256)
            if not chunk:
                break
            size += len(chunk)
    return size

def get_recursive_listing(path, base_path=None):
    if base_path is None:
        base_path = path
    items = []
    try:
        entries = os.listdir(path)
        for item in entries:
            full_path = path + ('/' + item if path != '/' else item)
            
            # Calculate relative path from base
            if base_path == '/':
                rel_path = full_path
            else:
                rel_path = full_path[len(base_path):].lstrip('/')
            
            is_dir = xbee_is_dir(full_path)
            
            if is_dir:
                items.append([rel_path, 0, True])
                child = get_recursive_listing(full_path, base_path)
                if isinstance(child, dict) and '__error__' in child:
                    return child
                items.extend(child)
            else:
                size = xbee_get_file_size(full_path)
                items.append([rel_path, size, is_dir])
        
    except Exception as e:
        return {{'__error__': str(e)}}
    
    return sorted(items, key=lambda x: x[0].lower())

print(json.dumps(get_recursive_listing({safe_dir})))
"""
        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls_recursive failed: {result['__error__']}")
            return result
        except TransportError:
            raise
        except json.JSONDecodeError:
            return self._ls_recursive_fallback(dir)

    def _ls_recursive_fallback(self, dir: str = "/") -> List[list]:
        result = []
        
        def walk_dir(current_dir, base_dir):
            items = self.ls_detailed(current_dir)
            for name, size, is_dir in items:
                full_path = posixpath.join(current_dir, name)
                if base_dir == '/':
                    rel_path = full_path
                else:
                    rel_path = full_path[len(base_dir):].lstrip('/')
                
                if is_dir:
                    result.append([rel_path, 0, True])
                    walk_dir(full_path, base_dir)
                else:
                    result.append([rel_path, size, False])
        
        walk_dir(dir, dir)
        
        return sorted(result, key=lambda x: x[0].lower())

    def is_dir(self, path: str) -> bool:
        path = self._normalize_remote_path(path)
        path_literal = _python_string_literal(path)
        
        if self.core == "EFR32MG":
            command = f"""
import os
try:
    os.listdir({path_literal})
    result = True
except OSError:
    result = False
print(result)
"""
        else:
            command = f"""
import os
try:
    result = os.stat({path_literal})[0] & 0x4000 != 0
except Exception:
    try:
        os.listdir({path_literal})
        result = True
    except OSError:
        result = False
print(result)
"""
        out = self.repl.exec(command)
        try:
            result = ast.literal_eval(out.decode("utf-8").strip())
        except (ValueError, SyntaxError) as e:
            raise FileSystemError(f"Invalid is_dir response: {e}") from e
        if not isinstance(result, bool):
            raise FileSystemError("Invalid is_dir response: expected a boolean")
        return result

    def state(self, path: str) -> int:
        path = self._normalize_remote_path(path)
        path_literal = _python_string_literal(path)
        
        if self.core == "EFR32MG":
            command = f"""
try:
    with open({path_literal}, 'rb') as f:
        size = 0
        while True:
            chunk = f.read(256)
            if not chunk:
                break
            size += len(chunk)
    print(size)
except OSError as e:
    print('__ERROR__:' + str(e))
"""
        else:
            command = f"""
import os
try:
    st = os.stat({path_literal})
    print(st[6])
except Exception:
    print(0)
"""
        out = self.repl.exec(command)
        result = out.decode('utf-8').strip()
        if result.startswith('__ERROR__:'):
            raise FileSystemError(f"File-size query failed: {result[10:].strip()}")
        try:
            return int(result)
        except ValueError as e:
            raise FileSystemError(f"Invalid file-size response: {result!r}") from e

    def _file_exists(self, path: str) -> bool:
        path_literal = _python_string_literal(path)
        
        if self.core == "EFR32MG":
            command = f"""
try:
    f = open({path_literal}, 'rb')
    f.close()
    print('1')
except Exception:
    print('0')
"""
        else:
            command = f"""
import os
try:
    os.stat({path_literal})
    print('1')
except Exception:
    print('0')
"""
        result = self.repl.exec(command).decode('utf-8').strip()
        if result == '1':
            return True
        if result == '0':
            return False
        raise FileSystemError(f"Invalid file-exists response: {result!r}")

    def mkdir(self, dir: str) -> bool:
        dir = dir.replace('\\', '/')
        if not dir:
            return False
        dir_literal = _python_string_literal(dir)
        command = f"""
import os
def mkdir(path):
    current = '/' if path.startswith('/') else ''
    for part in path.split('/'):
        if not part:
            continue
        if current == '/':
            current += part
        elif current:
            current += '/' + part
        else:
            current = part
        try:
            os.mkdir(current)
        except OSError:
            try:
                os.listdir(current)
            except OSError:
                return False
    return True
print(mkdir({dir_literal}))
"""
        out = self.repl.exec(command)
        try:
            result = ast.literal_eval(out.decode("utf-8").strip())
        except (ValueError, SyntaxError) as e:
            raise FileSystemError(f"Invalid mkdir response: {e}") from e
        if not isinstance(result, bool):
            raise FileSystemError("Invalid mkdir response: expected a boolean")
        return result

    def rm(self, filename: str):
        filename_literal = _python_string_literal(filename)
        command = f"""
import os
os.remove({filename_literal})
"""
        self.repl.exec(command)

    def touch(self, filename: str, core: str = None):
        core = core or self.core
        filename_literal = _python_string_literal(filename)
        if core == "EFR32MG":
            command = f"""
import os
try:
    # Try to open file to check if it exists
    f = open({filename_literal}, 'rb')
    f.close()
except Exception:
    # File doesn't exist, create it
    f = open({filename_literal}, 'wb')
    f.close()
"""
        else:
            command = f"f = open({filename_literal}, 'a'); f.close()"
        self.repl.exec(command)

    def rmdir(self, dir: str):
        dir_literal = _python_string_literal(dir)
        if self.core == "EFR32MG":
            command = f"""
import os
def rmdir(p):
    for f in os.listdir(p):
        fp = p + '/' + f
        try:
            os.remove(fp)
        except OSError:
            rmdir(fp)
    os.rmdir(p)
rmdir({dir_literal})
"""
        else:
            command = f"""
import os
def rmdir(p):
    for name in os.listdir(p):
        fp = p + '/' + name if p != '/' else '/' + name
        try:
            if os.stat(fp)[0] & 0x4000:
                rmdir(fp)
            else:
                os.remove(fp)
        except OSError:
            try:
                rmdir(fp)
            except Exception:
                pass
    os.rmdir(p)
rmdir({dir_literal})
"""
        self.repl.exec(command)

    def cp(self, source: str, dest: str):
        source_literal = _python_string_literal(source)
        dest_literal = _python_string_literal(dest)
        command = f"""
with open({source_literal}, 'rb') as src:
    with open({dest_literal}, 'wb') as dst:
        while True:
            chunk = src.read(512)
            if not chunk:
                break
            dst.write(chunk)
"""
        self.repl.exec(command)

    def mv(self, source: str, dest: str):
        source_literal = _python_string_literal(source)
        dest_literal = _python_string_literal(dest)
        if self.core == "EFR32MG":
            command = f"""
import os
try:
    os.listdir({source_literal})
    is_dir = True
except Exception:
    is_dir = False

if is_dir:
    # For directories: create dest, copy contents recursively, remove source
    def copy_dir(src, dst):
        try:
            os.mkdir(dst)
        except Exception:
            pass
        for f in os.listdir(src):
            sf = src + '/' + f
            df = dst + '/' + f
            try:
                os.listdir(sf)
                copy_dir(sf, df)
            except Exception:
                with open(sf, 'rb') as s:
                    with open(df, 'wb') as d:
                        while True:
                            c = s.read(512)
                            if not c:
                                break
                            d.write(c)
    def rm_dir(p):
        for f in os.listdir(p):
            fp = p + '/' + f
            try:
                os.remove(fp)
            except OSError:
                rm_dir(fp)
        os.rmdir(p)
    copy_dir({source_literal}, {dest_literal})
    rm_dir({source_literal})
else:
    # For files: simple copy and remove
    with open({source_literal}, 'rb') as src:
        with open({dest_literal}, 'wb') as dst:
            while True:
                chunk = src.read(512)
                if not chunk:
                    break
                dst.write(chunk)
    os.remove({source_literal})
"""
        else:
            command = f"""
import os
os.rename({source_literal}, {dest_literal})
"""
        self.repl.exec(command)

    def mem(self) -> Tuple[int, int, int, float]:
        command = """
import gc
gc.collect()
free = gc.mem_free()
alloc = gc.mem_alloc()
total = free + alloc
usage_pct = round(alloc / total * 100, 2)
print(free, alloc, total, usage_pct)
"""
        out = self.repl.exec(command)
        free_str, alloc_str, total_str, usage_pct_str = out.decode("utf-8").strip().split()
        return int(free_str), int(alloc_str), int(total_str), float(usage_pct_str)

    def df(self) -> Tuple[int, int, int, float]:
        command = """
import os
def get_fs_info(path='/'):
    stats = os.statvfs(path)
    block_size = stats[0]
    total_blocks = stats[2]
    free_blocks = stats[3]
    total = block_size * total_blocks
    free = block_size * free_blocks
    used = total - free
    usage_pct = round(used / total * 100, 2) if total > 0 else 0.0
    return total, used, free, usage_pct
print(get_fs_info())
"""
        out = self.repl.exec(command)
        return ast.literal_eval(out.decode("utf-8").strip())

    def format(self) -> bool:
        normalized_core = normalize_core(self.core)
        
        if normalized_core in ("ESP32S3", "ESP32C6", "ESP32C5", "ESP32P4"):
            command = """
import os
from flashbdev import bdev
os.umount('/')
os.VfsLfs2.mkfs(bdev)
os.mount(bdev, '/')
"""
        elif normalized_core == "EFR32MG":
            command = """
import os
os.format()
"""
        elif normalized_core == "RP2350":
            command = """
import os, rp2
try:
    os.umount('/')
except Exception:
    pass
bdev = rp2.Flash()
os.VfsLfs2.mkfs(bdev, progsize=256)
fs = os.VfsLfs2(bdev, progsize=256)
os.mount(fs, '/')
"""
        elif normalized_core == "MIMXRT1062DVJ6A":
            command = """
import os, mimxrt
try:
    os.umount('/flash')
except Exception:
    pass
bdev = mimxrt.Flash()
os.VfsLfs2.mkfs(bdev)
fs = os.VfsLfs2(bdev)
os.mount(fs, '/flash')
os.chdir('/flash')
"""
        else:
            return False
        
        try:
            self.repl.exec(command)
        except ProtocolError:
            return False
        return True

    def get(self, remote: str, local: str = None) -> bytes:
        import binascii as binascii_module
        
        local_file = None
        local_target = None
        temp_path = None
        content_parts = []
        
        if local:
            if os.path.isdir(local):
                local = os.path.join(local, posixpath.basename(remote.rstrip('/')))
            local_target = os.path.abspath(local)
        
        bytes_read = 0

        try:
            file_size = self.state(remote)
            
            if file_size == 0:
                if not self._file_exists(remote):
                    raise ProtocolError(f"File not found: {remote}")
                if local_target:
                    local_dir = os.path.dirname(local_target) or os.curdir
                    os.makedirs(local_dir, exist_ok=True)
                    fd, temp_path = tempfile.mkstemp(
                        prefix=f".{os.path.basename(local_target)}.",
                        suffix=".tmp",
                        dir=local_dir,
                    )
                    os.close(fd)
                    os.replace(temp_path, local_target)
                    temp_path = None
                    return None
                return b""

            if local_target:
                local_dir = os.path.dirname(local_target) or os.curdir
                os.makedirs(local_dir, exist_ok=True)
                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(local_target)}.",
                    suffix=".tmp",
                    dir=local_dir,
                )
                local_file = os.fdopen(fd, "wb")

            with self.repl.session():
                init_command = f"""
import sys
import binascii
f = open({_python_string_literal(remote)}, 'rb')
"""
                self.repl._exec(textwrap.dedent(init_command))
                
                CHUNK_SIZE = self._GET_CHUNK_SIZE
                
                try:
                    while bytes_read < file_size:
                        remaining = min(CHUNK_SIZE, file_size - bytes_read)

                        read_cmd = f"""
chunk = f.read({remaining})
if chunk:
    encoded = binascii.b2a_base64(chunk)
    sys.stdout.write(encoded.decode('ascii'))
"""
                        encoded_data = self.repl._exec(textwrap.dedent(read_cmd))

                        if not encoded_data:
                            break
                        try:
                            cleaned_data = encoded_data.strip().replace(b'\r', b'').replace(b'\n', b'')
                            chunk_data = binascii_module.a2b_base64(cleaned_data)
                        except Exception as e:
                            raise ProtocolError(f"Failed to decode base64 data: {e}")

                        if not chunk_data:
                            break
                        if local_file is not None:
                            local_file.write(chunk_data)
                        else:
                            content_parts.append(chunk_data)

                        bytes_read += len(chunk_data)
                finally:
                    self.repl._exec("f.close()")

            if bytes_read != file_size:
                raise ProtocolError(f"Download incomplete: got {bytes_read}/{file_size} bytes")

            if local_file is not None:
                local_file.flush()
                os.fsync(local_file.fileno())
                local_file.close()
                local_file = None
                os.replace(temp_path, local_target)
                temp_path = None
        
        except ProtocolError:
            raise
        except Exception as e:
            raise ProtocolError(f"Download failed: {e}") from e
        finally:
            if local_file is not None:
                local_file.close()
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass

        if not local_target:
            return b''.join(content_parts)

    def put(self, local: str, remote: str, progress_callback=None):
        sent = 0
        needs_retry = False
        file_opened = False

        use_deflate = False
        with self.repl.session():
            try:
                if self.core != "EFR32MG":
                    # Probe for the deflate module and define a helper that inflates
                    # raw-deflate (4 KiB window) chunks sent as base64.
                    out = self.repl._exec(
                        "import binascii\n"
                        "try:\n"
                        " import deflate,io\n"
                        f" def _w(d):f.write(deflate.DeflateIO(io.BytesIO(binascii.a2b_base64(d)),deflate.RAW,{_DEFLATE_WBITS}).read())\n"
                        " print('Z1')\n"
                        "except ImportError:\n"
                        " print('Z0')\n"
                        f"f = open({_python_string_literal(remote)}, 'wb')"
                    )
                    use_deflate = b'Z1' in out
                else:
                    self.repl._exec(f"f = open({_python_string_literal(remote)}, 'wb')")
                file_opened = True
            except ProtocolError as e:
                if "EEXIST" in str(e):
                    needs_retry = True
                else:
                    raise
            else:
                try:
                    with open(local, "rb") as f:
                        total = os.fstat(f.fileno()).st_size
                        
                        if progress_callback:
                            progress_callback(0, total)

                        batch_src_lines = []
                        batch_bytes = 0
                        DEVICE_CHUNK = self._DEVICE_CHUNK_SIZES
                        BATCH_LIMIT = max(1, int(self._PUT_BATCH_BYTES))

                        def _flush_batch():
                            nonlocal batch_src_lines, batch_bytes
                            if not batch_src_lines:
                                return

                            code = ";\n".join(batch_src_lines)
                            self.repl._exec(code)
                            batch_src_lines = []
                            batch_bytes = 0
                            
                            if progress_callback:
                                progress_callback(sent, total)

                        while True:
                            chunk = f.read(DEVICE_CHUNK)
                            if not chunk:
                                _flush_batch()
                                break

                            line = f"f.write({repr(chunk)})"
                            if self.core != "EFR32MG":
                                encoded_line = f"f.write(binascii.a2b_base64({base64.b64encode(chunk)!r}))"
                                if len(encoded_line) < len(line):
                                    line = encoded_line
                                if use_deflate:
                                    z = zlib.compressobj(9, zlib.DEFLATED, -_DEFLATE_WBITS)
                                    packed = z.compress(chunk) + z.flush()
                                    deflate_line = f"_w({base64.b64encode(packed)!r})"
                                    if len(deflate_line) < len(line):
                                        line = deflate_line
                            batch_src_lines.append(line)
                            batch_bytes += len(line)
                            sent += len(chunk)

                            if batch_bytes >= BATCH_LIMIT:
                                _flush_batch()

                    self.repl._exec("f.close()")
                    file_opened = False
                    
                    if progress_callback:
                        progress_callback(total, total)
                        
                except Exception:
                    if file_opened:
                        try:
                            self.repl._exec("try:\n  f.close()\nexcept:\n  pass")
                        except Exception:
                            pass
                    raise

        if needs_retry:
            self.rm(remote)
            self.put(local, remote, progress_callback)

    def getdir_batch(self, remote: str, local: str, progress_callback=None):
        base_remote = remote.replace("\\", "/")
        base_local = os.path.abspath(local)
        os.makedirs(base_local, exist_ok=True)
        
        try:
            with self.repl.session():
                try:
                    items = self.ls_recursive(base_remote)
                except Exception as e:
                    raise ProtocolError(f"Failed to list remote directory: {e}") from e
                return self._getdir_from_items(items, base_remote, base_local, progress_callback)
        except ProtocolError:
            raise
        except Exception as e:
            raise ProtocolError(f"Failed to download remote directory: {e}") from e

    def _getdir_from_items(self, items, base_remote: str, base_local: str, progress_callback):
        if not items:
            return
        
        file_specs = []
        for item in items:
            rel_path = item[0]
            is_dir = item[2]
            relative_path = rel_path.lstrip('/')
            local_path = os.path.join(base_local, *relative_path.split('/'))
            if is_dir:
                os.makedirs(local_path, exist_ok=True)
                continue
            
            local_dir = os.path.dirname(local_path)
            if local_dir and not os.path.exists(local_dir):
                os.makedirs(local_dir, exist_ok=True)
            
            if rel_path.startswith('/'):
                full_remote = rel_path
            elif base_remote == '/':
                full_remote = '/' + rel_path
            else:
                full_remote = base_remote + ('/' if not base_remote.endswith('/') else '') + rel_path
            
            file_specs.append((full_remote, local_path))
        
        if not file_specs:
            return
        
        total = len(file_specs)
        for idx, (remote_file, local_file) in enumerate(file_specs):
            if progress_callback:
                progress_callback(idx, total, os.path.basename(remote_file))
            
            try:
                self.get(remote_file, local_file)
            except Exception as e:
                raise ProtocolError(f"Failed to download {remote_file}: {e}")
        
        if progress_callback:
            progress_callback(total, total, "Complete")

    def putdir(self, local: str, remote: str):
        import posixpath
        
        base_local = os.path.abspath(local)
        base_remote = remote.replace("\\", "/")
        
        with self.repl.session():
            for parent, child_dirs, child_files in os.walk(base_local, followlinks=False):
                child_dirs[:] = [
                    name for name in child_dirs if name.lower() not in PUTDIR_EXCLUDED_DIRS
                ]
                rel = os.path.relpath(parent, base_local).replace("\\", "/")
                remote_parent = posixpath.normpath(
                    posixpath.join(base_remote, "" if rel == "." else rel)
                )
                if not self.mkdir(remote_parent):
                    raise FileSystemError(f"Could not create remote directory: {remote_parent}")

                for filename in child_files:
                    if os.path.splitext(filename)[1].lower() in PUTDIR_EXCLUDED_EXTENSIONS:
                        continue
                    local_path = os.path.join(parent, filename)
                    remote_path = posixpath.join(remote_parent, filename)
                    self.put(local_path, remote_path)

    def _normalize_remote_path(self, path: str) -> str:
        root = posixpath.normpath(self.device_root_fs or '/')
        path = (path or '/').replace('\\', '/')

        if root == '/':
            return posixpath.normpath('/' + path.lstrip('/'))

        if path == root or path.startswith(root + '/'):
            real_path = posixpath.normpath(path)
        else:
            real_path = posixpath.normpath(posixpath.join(root, path.lstrip('/')))

        if real_path != root and not real_path.startswith(root + '/'):
            raise FileSystemError("Remote path is outside the device filesystem root")
        return real_path

    def _print_progress_bar(self, current: int, total: int, bar_length: int = 40):
        pct = 0 if total == 0 else min(1.0, current / total)
        block = min(bar_length, int(round(bar_length * pct)))
        bar = "#" * block + "-" * (bar_length - block)
        percent = int(pct * 100)
        print(f"\r[{bar}] {percent}% ({current}/{total})", end="", flush=True)


def create_storage(repl_protocol, core: str = "RP2350", device: str = "", device_root_fs: str = "/") -> DeviceStorage:
    return DeviceStorage(
        repl_protocol,
        core=core,
        device=device,
        device_root_fs=device_root_fs
    )


SerialStorage = DeviceStorage


__all__ = [
    'DeviceStorage',
    'SerialStorage',
    'create_storage',
]

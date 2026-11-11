import os
import hashlib
import subprocess

from replx_agent.utils.device_info import normalize_core
from replx_agent.utils.exceptions import CompilationError, ValidationError, PkgError
from .store import StoreManager


class CompilerHelper:
    _compile_cache = {}

    @staticmethod
    def _run_mpy_cross(args: list[str], source_path: str) -> None:
        try:
            import mpy_cross
        except ImportError as e:
            raise CompilationError("mpy-cross is not installed") from e

        try:
            proc = mpy_cross.run(
                *args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            raise CompilationError(f"MPY compilation failed for {source_path}: {e}") from e

        stdout = ""
        stderr = ""
        returncode = 0

        if hasattr(proc, "communicate"):
            stdout, stderr = proc.communicate()
            returncode = proc.returncode or 0

        if returncode != 0:
            details = (stderr or stdout or f"mpy-cross exited with code {returncode}").strip()
            raise CompilationError(f"MPY compilation failed for {source_path}: {details}")

    @staticmethod
    def compile_file(abs_py: str, out_mpy: str, core: str, version: str) -> str:
        if not os.path.exists(abs_py):
            raise ValidationError(f"Source file not found: {abs_py}")

        if normalize_core(core) == "EFR32MG":
            raise CompilationError(
                "Digi XBee3 uses a legacy/vendor .mpy ABI. The bundled mpy-cross "
                "cannot certify compatibility; upload .py source with put, "
                "or use mip install --no-compile."
            )

        out_dir = os.path.dirname(out_mpy)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        args = [abs_py, '-o', out_mpy]
        args.extend(CompilerHelper._march_for_core(core, version or "1.29.0"))
        CompilerHelper._run_mpy_cross(args, abs_py)

        if os.path.exists(out_mpy) and os.path.getsize(out_mpy) > 0:
            return out_mpy

        raise CompilationError(f"Compilation failed: {out_mpy} not found or empty")

    @staticmethod
    def mpy_arch_tag(core: str) -> str:
        if not core:
            return "unknown"
        if "/" in core:
            return core.split("/", 1)[0]
        return core

    @staticmethod
    def staging_out_for(abs_py: str, base: str, arch_tag: str) -> str:
        rel = os.path.relpath(abs_py, base).replace("\\", "/")
        rel_mpy = os.path.splitext(rel)[0] + ".mpy"
        out_path = StoreManager.HOME_STAGING / arch_tag / rel_mpy
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return str(out_path)

    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    @staticmethod
    def _march_for_core(core: str, version: str) -> list[str]:
        if not core:
            raise PkgError("The core is unknown")

        if "/" in core:
            core = core.split("/", 1)[0]

        args: list[str] = ['-msmall-int-bits=31']

        if core == "EFR32MG":
            try:
                ver_parts = version.split('.')
                ver_float = float(f"{ver_parts[0]}.{ver_parts[1]}" if len(ver_parts) >= 2 else version)
            except (ValueError, IndexError):
                ver_float = 0.0
            if ver_float < 1.19:
                args.append('-mno-unicode')
            return args

        if core in ("ESP32", "ESP32S2"):
            args.append('-march=xtensa')
            return args

        if core == "ESP32S3":
            args.append('-march=xtensawin')
            return args

        if core.startswith("ESP32C"):
            args.append('-march=rv32imc')
            return args

        if core.startswith("ESP32P"):
            args.append('-march=rv32imc')
            return args

        if core == "RP2350":
            args.append('-march=armv7emsp')
            return args

        raise PkgError(f"The {core} is not supported")

    @staticmethod
    def compile_to_staging(abs_py: str, base: str, core: str, version: str) -> str:
        if not os.path.exists(abs_py):
            raise ValidationError(f"Source file not found: {abs_py}")

        arch_tag = CompilerHelper.mpy_arch_tag(core)
        out_mpy = CompilerHelper.staging_out_for(abs_py, base, arch_tag)

        cache_key = (abs_py, arch_tag)
        current_hash = CompilerHelper._compute_file_hash(abs_py)

        if cache_key in CompilerHelper._compile_cache:
            cached_hash, cached_out = CompilerHelper._compile_cache[cache_key]
            if cached_hash == current_hash and os.path.exists(cached_out) and os.path.getsize(cached_out) > 0:
                return cached_out

        CompilerHelper.compile_file(abs_py, out_mpy, core, version or "1.29.0")
        CompilerHelper._compile_cache[cache_key] = (current_hash, out_mpy)
        return out_mpy

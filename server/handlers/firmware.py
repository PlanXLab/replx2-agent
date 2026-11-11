import os
import subprocess
import sys
import time

from ..command_dispatcher import CommandContext

_FIRMWARE_STAGE_TTL_S = 30 * 60


class FirmwareCommandsMixin:
    def _get_firmware_stage(self, stage_id: str):
        if not stage_id:
            raise ValueError("Firmware stage ID is required")
        with self._firmware_stages_lock:
            stage = self._firmware_stages.get(stage_id)
            if stage and time.monotonic() - stage.created_at > _FIRMWARE_STAGE_TTL_S:
                self._firmware_stages.pop(stage_id, None)
                expired = stage
                stage = None
            else:
                expired = None
        if expired:
            try:
                os.unlink(expired.path)
            except FileNotFoundError:
                pass
        if stage is None:
            raise ValueError("Firmware image staging expired or was not found")
        return stage

    @staticmethod
    def _run_esptool(arguments: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "esptool", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )

    def _cmd_firmware_info(self, ctx: CommandContext, stage_id: str) -> dict:
        stage = self._get_firmware_stage(stage_id)
        result = self._run_esptool([
            "--chip", "esp32p4", "image-info", stage.path,
        ])
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode:
            raise RuntimeError(
                f"esptool image-info exited with code {result.returncode}.\n{output}"
            )
        return {
            "file_name": stage.file_name,
            "size": stage.size,
            "sha256": stage.digest,
            "output": output,
        }

    def _cmd_firmware_flash(
        self,
        ctx: CommandContext,
        stage_id: str,
        port: str,
        baud: int = 460800,
        erase: bool = False,
    ) -> dict:
        if not port:
            raise ValueError("Flash port is required")
        if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
            raise ValueError("Baud rate must be a positive integer")

        stage = self._get_firmware_stage(stage_id)
        self._cmd_disconnect_port(ctx, port=port)

        arguments = [
            "--chip", "esp32p4", "--port", port, "--baud", str(baud),
            "write-flash",
        ]
        if erase:
            arguments.append("--erase-all")
        arguments.extend(["0x2000", stage.path])
        result = self._run_esptool(arguments)
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode:
            raise RuntimeError(
                f"esptool exited with code {result.returncode}. Check the image's chip "
                f"revision, C6 variant and port.\n{output}"
            )

        with self._firmware_stages_lock:
            self._firmware_stages.pop(stage_id, None)
        try:
            os.unlink(stage.path)
        except FileNotFoundError:
            pass

        return {
            "flashed": stage.file_name,
            "port": port,
            "baud": baud,
            "erase": erase,
            "output": output,
        }

    def _cmd_firmware_discard(
        self, ctx: CommandContext, stage_id: str
    ) -> dict:
        with self._firmware_stages_lock:
            stage = self._firmware_stages.pop(stage_id, None)
        if stage:
            try:
                os.unlink(stage.path)
            except FileNotFoundError:
                pass
        return {"discarded": stage is not None}

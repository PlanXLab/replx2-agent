import os
import urllib.request
from typing import Optional, Tuple

from replx_agent.utils import device_name_to_path
from replx_agent.utils.constants import HTTP_REQUEST_TIMEOUT
from replx_agent.utils.exceptions import PkgError
from .store import StoreManager


class InstallHelper:

    @staticmethod
    def resolve_spec(spec: str) -> Tuple[str, str]:
        if spec in ("core/", "device/", "core", "device"):
            raise PkgError(
                f"Invalid spec: {spec} (deprecated; use core.all/device.all/core.<file>/device.<file>)"
            )

        if spec == "core.all":
            return "core", ""
        if spec == "device.all":
            return "device", ""

        if spec.startswith("core."):
            rest = spec[len("core."):]
            if not rest:
                raise PkgError("Invalid spec: core.<file> requires a filename")
            if rest.count(".") > 1:
                raise PkgError(f"Invalid spec: {spec} (nested dotted filename is not supported)")
            return "core", rest

        if spec.startswith("device."):
            rest = spec[len("device."):]
            if not rest:
                raise PkgError("Invalid spec: device.<file> requires a filename")
            if rest.count(".") > 1:
                raise PkgError(f"Invalid spec: {spec} (nested dotted filename is not supported)")
            return "device", rest

        raise PkgError(
            f"Invalid spec: {spec} (expect core.all/device.all/core.<file>/device.<file>)"
        )

    @staticmethod
    def download_raw_file(owner: str, repo: str, ref_: str, path: str, out_path: str) -> str:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_}/{path}"
        req = urllib.request.Request(url, headers=StoreManager.gh_headers())
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as r, open(out_path, "wb") as f:
            f.write(r.read())
        return out_path

    @staticmethod
    def remote_dir_for(scope: str, rel_dir: str, device: str) -> str:
        if scope == "core":
            return "lib/" + (rel_dir + "/" if rel_dir else "")
        return f"lib/{device_name_to_path(device)}/" + (rel_dir + "/" if rel_dir else "")

    @staticmethod
    def list_local_py_targets(scope: str, rest: str, core: str, device: str) -> Tuple[str, list]:
        if scope == "core":
            base = os.path.join(StoreManager.pkg_root(), "core", core, "src")
        else:
            base = os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(device), "src")

        target_path = os.path.join(base, rest)
        if os.path.isfile(target_path):
            rel = os.path.relpath(target_path, base).replace("\\", "/")
            return base, [(target_path, rel)]

        if os.path.isdir(target_path):
            out = []
            for dp, _, fns in os.walk(target_path):
                for fn in fns:
                    ap = os.path.join(dp, fn)
                    rel = os.path.relpath(ap, base).replace("\\", "/")
                    out.append((ap, rel))
            return base, out
        return base, []

    @staticmethod
    def local_store_ready_for_full_install(core: str, device: str) -> Tuple[bool, str]:
        StoreManager.ensure_home_store()
        meta_path = StoreManager.local_meta_path()
        if not os.path.isfile(meta_path):
            return False, "meta-missing"

        try:
            _ = StoreManager.load_local_meta()
        except Exception:
            return False, "meta-broken"

        req_dirs = [
            os.path.join(StoreManager.pkg_root(), "core", core, "src"),
            os.path.join(StoreManager.pkg_root(), "core", core, "typehints"),
            os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(device), "src"),
            os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(device), "typehints"),
        ]
        missing = [p for p in req_dirs if not os.path.isdir(p)]
        if missing:
            return False, "dirs-missing"

        return True, "ok"


class SearchHelper:

    @staticmethod
    def fmt_ver_with_star(remote_ver: float, local_ver: float, missing_local: bool) -> str:
        star = "*" if missing_local or (remote_ver > (local_ver or 0.0)) else ""
        return f"{remote_ver:.1f}{star}"

    @staticmethod
    def key_ci(items: set | dict, name: str) -> Optional[str]:
        if not name:
            return None

        if isinstance(items, set):
            if name in items:
                return name
            n = name.lower()
            for k in items:
                if isinstance(k, str) and k.lower() == n:
                    return k
            return None

        if isinstance(items, dict):
            if name in items:
                return name
            n = name.lower()
            for k in items.keys():
                if isinstance(k, str) and k.lower() == n:
                    return k
            return None

        return None


class RegistryHelper:

    @staticmethod
    def root_sections(reg: dict):
        platform_cores = reg.get("platform_cores", {})
        device_configs = reg.get("device_configs", {})

        cores = set(platform_cores.keys()) if platform_cores else set()
        devices = set(device_configs.keys()) if device_configs else set()

        return cores, devices

    @staticmethod
    def get_packages_for_platform(reg: dict, platform: str) -> list[str]:
        platform_cores = reg.get("platform_cores", {})
        core_info = platform_cores.get(platform, {})
        return core_info.get("includes", [])

    @staticmethod
    def get_packages_for_device(reg: dict, device: str) -> list[str]:
        device_configs = reg.get("device_configs", {})
        device_info = device_configs.get(device, {})
        return device_info.get("includes", [])

    @staticmethod
    def match_pattern(pkg_name: str, pattern: str) -> bool:
        if "*" in pattern:
            # Wildcard pattern
            prefix = pattern.rstrip("/*")
            return pkg_name.startswith(prefix)
        else:
            pkg_base = pkg_name
            for ext in (".py", ".pyi", ".json"):
                if pkg_base.endswith(ext):
                    pkg_base = pkg_base[:-len(ext)]
                    break
            return pkg_base == pattern or pkg_name == pattern

    @staticmethod
    def get_packages_matching(reg: dict, patterns: list[str], package_type: str = None) -> dict:
        packages = reg.get("packages", {})
        result = {}

        for pkg_name, pkg_meta in packages.items():
            if package_type:
                pkg_type = pkg_meta.get("type", "")
                if package_type == "core" and pkg_type != "core":
                    continue
                elif package_type == "device" and not pkg_type.startswith("device-"):
                    continue

            source = pkg_meta.get("source", "")
            for pattern in patterns:
                if RegistryHelper.match_pattern(source, pattern):
                    result[pkg_name] = pkg_meta
                    break

        return result

    @staticmethod
    def get_version(pkg_meta: dict) -> float:
        v = pkg_meta.get("version", "0.0")
        try:
            if isinstance(v, str) and "." in v:
                parts = v.split(".")
                major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
                minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                return float(f"{major}.{minor}")
            return float(v)
        except Exception:
            return 0.0

    @staticmethod
    def effective_version(reg: dict, scope: str, target: str, part: str, relpath: str) -> float:
        packages = reg.get("packages", {})

        if scope == "core":
            source_path = f"core/{target}/{part}/{relpath}"
        else:
            source_path = f"device/{target}/{part}/{relpath}"

        for pkg_name, pkg_meta in packages.items():
            pkg_source = pkg_meta.get("source", "")
            if pkg_source == source_path:
                return RegistryHelper.get_version(pkg_meta)

        return 0.0

    @staticmethod
    def walk_files_for_core(reg: dict, core_name: str, part: str = "src"):
        packages = reg.get("packages", {})
        patterns = RegistryHelper.get_packages_for_platform(reg, core_name)

        for pkg_name, pkg_meta in packages.items():
            if pkg_meta.get("type") != "core":
                continue

            source = pkg_meta.get("source", "")

            match_found = False
            for pattern in patterns:
                if "*" in pattern:
                    pattern_prefix = pattern.rstrip("/*")
                    if f"/{pattern_prefix}/" in source or source.endswith(f"/{pattern_prefix}"):
                        match_found = True
                        break
                else:
                    if RegistryHelper.match_pattern(source, pattern):
                        match_found = True
                        break

            if not match_found:
                continue

            if part == "src":
                file_path = source
            elif part == "typehints":
                file_path = pkg_meta.get("typehint", "")
                if not file_path:
                    continue
            else:
                continue

            path_parts = file_path.split("/")
            if len(path_parts) >= 3 and path_parts[2] == part:
                relpath = "/".join(path_parts[3:])
                yield (relpath, pkg_meta)

    @staticmethod
    def walk_files_for_device(reg: dict, device_name: str, part: str = "src", include_submodules: bool = False):
        packages = reg.get("packages", {})
        patterns = RegistryHelper.get_packages_for_device(reg, device_name)

        for pkg_name, pkg_meta in packages.items():
            variants = pkg_meta.get("variants", {})
            for variant_name, variant_meta in variants.items():
                variant_deploy = variant_meta.get("deploy_path", "")

                match_found = False
                for pattern in patterns:
                    module_name = variant_deploy.replace("/__init__.py", "").replace(".py", "")
                    variant_full_path = f"{variant_name}/{module_name}"

                    if RegistryHelper.match_pattern(variant_full_path, pattern):
                        match_found = True
                        break
                    if "/" in pattern and variant_full_path.startswith(pattern + "/"):
                        match_found = True
                        break

                if not match_found:
                    continue

                if part == "src":
                    file_path = variant_meta.get("source", "")
                elif part == "typehints":
                    file_path = variant_meta.get("typehint", "")
                    if not file_path or not file_path.endswith(".pyi"):
                        continue
                else:
                    continue

                if not file_path:
                    continue

                if part == "typehints":
                    relpath = variant_deploy.replace(".py", ".pyi")
                else:
                    relpath = variant_deploy
                merged_meta = pkg_meta.copy()
                merged_meta.update(variant_meta)
                yield (relpath, merged_meta)

            pkg_type = pkg_meta.get("type", "")
            if not pkg_type.startswith("device-"):
                continue

            source = pkg_meta.get("source", "")
            deploy_path = pkg_meta.get("deploy_path", "")

            match_found = False
            for pattern in patterns:
                if "@" in pkg_name:
                    parts = pkg_name.split("@")
                    module_name = parts[0]
                    device_prefix = parts[1]
                    variant_full_path = f"{device_prefix}/{module_name}"
                    if RegistryHelper.match_pattern(variant_full_path, pattern):
                        match_found = True
                        break

                if source.startswith("device/"):
                    source_parts = source.split("/")
                    if len(source_parts) >= 4:
                        category = source_parts[1]
                        file_parts = source_parts[3:]

                        file_path = "/".join(file_parts)
                        if file_path.endswith("/__init__.py"):
                            file_path = file_path[:-12]
                        elif file_path.endswith(".py") or file_path.endswith(".pyi"):
                            file_path = file_path[:-3]

                        source_pattern_path = f"{category}/{file_path}"

                        if RegistryHelper.match_pattern(source_pattern_path, pattern):
                            match_found = True
                            break

                if RegistryHelper.match_pattern(deploy_path, pattern):
                    match_found = True
                    break
                if "/" in pattern and deploy_path.startswith(pattern + "/"):
                    match_found = True
                    break

            if not match_found:
                continue

            if part == "src":
                file_path = source
            elif part == "typehints":
                file_path = pkg_meta.get("typehint", "")
                if not file_path or not file_path.endswith(".pyi"):
                    continue
            else:
                continue

            if part == "typehints":
                relpath = deploy_path.replace(".py", ".pyi")
            else:
                relpath = deploy_path
            yield (relpath, pkg_meta)

            if include_submodules and part == "src":
                deploy_dir = "/".join(deploy_path.split("/")[:-1])
                sub_base = {k: v for k, v in pkg_meta.items() if k not in ("submodules", "submodules_typehints")}
                sub_base["submodules"] = []
                sub_base["submodules_typehints"] = []
                sub_base["_parent_source"] = pkg_meta.get("source", "")
                for sub_src in pkg_meta.get("submodules", []):
                    sub_filename = sub_src.split("/")[-1]
                    if sub_filename.endswith(".pyi"):
                        continue
                    sub_relpath = (deploy_dir + "/" + sub_filename) if deploy_dir else sub_filename
                    sub_meta = sub_base.copy()
                    sub_meta["source"] = sub_src
                    yield (sub_relpath, sub_meta)

"""Package store orchestration for the agent.

Pure logic (no UI, no board I/O): download planning/execution against the
GitHub registry, install batch preparation (compile to staging), store
cleanup, store status and typehints enumeration. Board writes are done by
the command handler using the prepared batch.
"""

import os
import hashlib
import shutil
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from replx_agent.utils import device_name_to_path
from replx_agent.utils.exceptions import PkgError
from .store import StoreManager
from .registry import InstallHelper, SearchHelper, RegistryHelper
from .compiler import CompilerHelper

ProgressFn = Callable[[dict], None]


def find_local_pkg_version(local_meta: dict, *, scope: str, target: str,
                           source_path: str, pkg_name: str) -> tuple[float, bool]:
    if not local_meta or not isinstance(local_meta, dict):
        return 0.0, True

    local_packages = local_meta.get("packages", {})
    if not local_packages or not isinstance(local_packages, dict):
        return 0.0, True

    if not scope or not target or not source_path or not pkg_name:
        return 0.0, True

    base_key = f"{scope}:{target}:{pkg_name}"

    matching_versions = []
    for key, meta in local_packages.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        if key == base_key or key.startswith(base_key + "@"):
            meta_source = meta.get("source") or meta.get("typehint") or ""
            if meta_source == source_path:
                matching_versions.append(RegistryHelper.get_version(meta))
    if matching_versions:
        return max(matching_versions), False

    if base_key in local_packages:
        meta = local_packages[base_key]
        if isinstance(meta, dict):
            meta_source = meta.get("source") or meta.get("typehint") or ""
            if not source_path or not meta_source or meta_source == source_path:
                return RegistryHelper.get_version(meta), False

    if scope == "device":
        best = None
        for key, meta in local_packages.items():
            if not isinstance(key, str):
                continue
            if key.startswith(base_key + "@"):
                v = RegistryHelper.get_version(meta)
                best = v if best is None else max(best, v)
        if best is not None:
            return best, False

    prefix_lower = f"{scope}:{target}:".lower()
    for key, meta in local_packages.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        if key.lower().startswith(prefix_lower) and meta.get("source") == source_path:
            return RegistryHelper.get_version(meta), False

    return 0.0, True


def compute_search_rows(core: str, device: str, owner: str, repo: str, ref: str,
                        query: Optional[str] = None) -> dict:
    try:
        remote = StoreManager.load_remote_meta(owner, repo, ref)
    except Exception as e:
        raise PkgError(f"Failed to load remote registry: {e}")

    try:
        local = StoreManager.load_local_meta()
    except Exception:
        local = {}

    cores, devices = RegistryHelper.root_sections(remote)

    def status_label(remote_ver: float, local_ver_: float, missing_local: bool) -> str:
        if missing_local:
            return "NEW"
        if remote_ver > (local_ver_ or 0.0):
            return "UPD"
        return ""

    def local_ver(source_path: str, target: str, scope: str) -> tuple[float, bool]:
        if not source_path or not target:
            return 0.0, True

        filename = source_path.split("/")[-1] if "/" in source_path else source_path
        name = filename.replace(".py", "").replace(".pyi", "").replace("__init__", "")
        if not name and "/" in source_path:
            parts = source_path.split("/")
            name = parts[-2] if len(parts) >= 2 else ""

        return find_local_pkg_version(local, scope=scope, target=target,
                                      source_path=source_path, pkg_name=name)

    def add_core_rows(core_name: str, rows: list):
        for relpath, pkg_meta in RegistryHelper.walk_files_for_core(remote, core_name, "src"):
            if not relpath.endswith(".py"):
                continue
            rver = RegistryHelper.get_version(pkg_meta)
            source = pkg_meta.get("source", "")
            lver, missing = local_ver(source, core_name, "core")
            stat = status_label(rver, lver, missing)
            pkg_name = pkg_meta.get("source", "").split("/")[-1].replace(".py", "")
            display_path = f"src/{relpath}"
            if relpath.endswith("/__init__.py"):
                display_path = display_path.replace("/__init__.py", "/")
            rows.append(("core", core_name, stat, f"{rver:.1f}", display_path, pkg_name))

    def add_device_rows(dev_name: str, rows: list):
        for relpath, pkg_meta in RegistryHelper.walk_files_for_device(remote, dev_name, "src", include_submodules=True):
            if not (relpath.endswith(".py") or relpath.endswith(".bin")):
                continue
            rver = RegistryHelper.get_version(pkg_meta)
            source = pkg_meta.get("_parent_source") or pkg_meta.get("source", "")
            lver, missing = local_ver(source, dev_name, "device")
            stat = status_label(rver, lver, missing)
            pkg_name = pkg_meta.get("source", "").split("/")[-1].replace(".py", "")
            display_path = f"src/{relpath}"
            if relpath.endswith("/__init__.py"):
                display_path = display_path.replace("/__init__.py", "/")
            elif relpath == "__init__.py":
                src_parts = source.split("/")
                if len(src_parts) >= 2 and src_parts[0] == "device":
                    pkg_folder = src_parts[1].lstrip("_")
                    display_path = f"src/{pkg_folder}/"
            rows.append(("device", dev_name, stat, f"{rver:.1f}", display_path, pkg_name))

    cur_core = (core or "").strip()
    cur_dev = (device or "").strip()
    cur_dev_key = SearchHelper.key_ci(devices, device_name_to_path(cur_dev)) if cur_dev else None
    cur_core_key = SearchHelper.key_ci(cores, cur_core) if cur_core else None
    if not cur_core_key:
        cur_dev_key = None

    rows: list[tuple[str, str, str, str, str, str]] = []

    if query:
        dkey = SearchHelper.key_ci(devices, device_name_to_path(query))
        ckey = SearchHelper.key_ci(cores, query)

        if dkey and cur_dev_key and dkey == cur_dev_key:
            add_device_rows(dkey, rows)
        elif ckey and cur_core_key and ckey == cur_core_key:
            add_core_rows(ckey, rows)
        else:
            q = query.lower()
            temp_rows = []

            if cur_core_key:
                add_core_rows(cur_core_key, temp_rows)
                if cur_dev_key and cur_dev_key != cur_core_key:
                    add_device_rows(cur_dev_key, temp_rows)

            for scope, target, stat, ver_str, shown_path, pkg_name in temp_rows:
                if (q in pkg_name.lower()) or (q in shown_path.lower()):
                    rows.append((scope, target, stat, ver_str, shown_path, pkg_name))
    else:
        if cur_core_key:
            add_core_rows(cur_core_key, rows)
            if cur_dev_key and cur_dev_key != cur_core_key:
                add_device_rows(cur_dev_key, rows)

    def row_key(r):
        scope_order = 0 if r[0] == "core" else 1
        return (scope_order, r[1].lower(), r[4].lower())

    rows.sort(key=row_key)

    return {"rows": [list(r) for r in rows]}


def download(core: str, device: str, owner: str, repo: str, ref: str,
             scope_filter: Optional[str] = None,
             file_filter: Optional[str] = None,
             progress: Optional[ProgressFn] = None) -> dict:
    StoreManager.ensure_home_store()

    dev = (device or "").strip()
    core = (core or "").strip()

    if not core:
        raise PkgError("Failed to get device info from connected board.")

    try:
        remote = StoreManager.load_remote_meta(owner, repo, ref)
    except Exception as e:
        raise PkgError(f"Failed to load remote meta: {e}")

    cores, devices = RegistryHelper.root_sections(remote)

    if scope_filter not in (None, "core", "device"):
        raise PkgError(f"Invalid scope_filter: {scope_filter}")

    include_core = scope_filter in (None, "core")
    include_device = scope_filter in (None, "device")

    core_exists_remote = core in cores if core else False
    device_exists_remote = (device_name_to_path(dev) in devices if dev else False) and (dev != core)

    if not core_exists_remote:
        return {
            "core": core, "device": dev,
            "core_in_registry": False,
            "total": 0, "new": 0, "upd": 0,
            "errors": [], "target": "",
        }

    with StoreManager.LOCK:
        try:
            local = StoreManager.load_local_meta()
            if not isinstance(local, dict):
                local = {}
        except Exception:
            local = {}

        local_packages = local.setdefault("packages", {})

        if "platform_cores" in remote and core:
            remote_platform_cores = remote.get("platform_cores", {})
            if core in remote_platform_cores:
                if "platform_cores" not in local:
                    local["platform_cores"] = {}
                local["platform_cores"][core] = remote_platform_cores[core]

        if "device_configs" in remote and dev and dev != core:
            remote_device_configs = remote.get("device_configs", {})
            device_key = device_name_to_path(dev)
            if device_key in remote_device_configs:
                if "device_configs" not in local:
                    local["device_configs"] = {}
                local["device_configs"][device_key] = remote_device_configs[device_key]

        if "schema_version" in remote:
            local["schema_version"] = remote.get("schema_version")

        def _local_touch_package(pkg_name: str, pkg_meta: dict) -> None:
            local_packages[pkg_name] = pkg_meta.copy()

        exts = (".py", ".pyi", ".json")

        def _plan_for(scope: str, target_name: str, part: str) -> list:
            todo = []
            remote_packages = remote.get("packages", {})

            if scope == "core":
                walker = RegistryHelper.walk_files_for_core(remote, target_name, part)
            else:
                walker = RegistryHelper.walk_files_for_device(remote, target_name, part)

            for relpath, pkg_meta in walker:
                if not relpath.endswith(exts):
                    continue

                source = pkg_meta.get("source", "")
                rver = RegistryHelper.get_version(pkg_meta)

                orig_pkg_name = None
                orig_pkg_meta = None

                for name, meta in remote_packages.items():
                    if meta.get("source") == source:
                        orig_pkg_name = name
                        orig_pkg_meta = meta
                        break
                    variants = meta.get("variants", {})
                    for var_name, var_meta in variants.items():
                        if var_meta.get("source") == source:
                            orig_pkg_name = name
                            orig_pkg_meta = meta.copy()
                            orig_pkg_meta["source"] = source
                            break
                    if orig_pkg_name:
                        break

                if not orig_pkg_name or not orig_pkg_meta:
                    pkg_name = source.split("/")[-1].replace(".py", "").replace(".pyi", "").replace("__init__", "")
                    if not pkg_name or pkg_name == "":
                        pkg_name = source.split("/")[-2] if len(source.split("/")) >= 2 else "unknown"
                    orig_pkg_name = pkg_name
                    orig_pkg_meta = pkg_meta

                source_for_match = (
                    source
                    or orig_pkg_meta.get("source", "")
                    or orig_pkg_meta.get("typehint", "")
                    or ""
                )
                lver, missing = find_local_pkg_version(
                    local,
                    scope=scope,
                    target=target_name,
                    source_path=source_for_match,
                    pkg_name=orig_pkg_name,
                )

                change_type = "NEW" if missing else ("UPD" if float(lver) < float(rver) else "")

                if float(lver) < float(rver):
                    display_name = source.split("/")[-1].replace(".py", "").replace(".pyi", "")
                    todo.append((relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))

            return todo

        plan = []
        new_count = 0
        upd_count = 0
        download_targets = []

        def _append_plan(scope: str, target_name: str, part: str, entries: list):
            nonlocal new_count, upd_count
            for relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type in entries:
                plan.append((scope, target_name, part, relpath, pkg_meta, display_name,
                             orig_pkg_name, orig_pkg_meta, change_type))
                if change_type == "NEW":
                    new_count += 1
                elif change_type == "UPD":
                    upd_count += 1

        if core_exists_remote and include_core:
            core_src_files = _plan_for("core", core, "src")
            core_hints_files = _plan_for("core", core, "typehints")

            if file_filter:
                core_src_files = [
                    item for item in core_src_files
                    if os.path.basename(item[0]) == file_filter
                ]
                core_hints_files = []

            _append_plan("core", core, "src", core_src_files)
            _append_plan("core", core, "typehints", core_hints_files)

            if core_src_files or core_hints_files:
                download_targets.append(f"core/{core}")

        if device_exists_remote and include_device:
            device_src_files = _plan_for("device", device_name_to_path(dev), "src")
            device_hints_files = _plan_for("device", device_name_to_path(dev), "typehints")

            if file_filter:
                device_src_files = [
                    item for item in device_src_files
                    if os.path.basename(item[0]) == file_filter
                ]
                device_hints_files = []

            _append_plan("device", device_name_to_path(dev), "src", device_src_files)
            _append_plan("device", device_name_to_path(dev), "typehints", device_hints_files)

            if device_src_files or device_hints_files:
                download_targets.append(f"device/{dev}")

        download_target = " + ".join(download_targets) if download_targets else ""

        total = len(plan)
        errors: list[str] = []

        if total > 0:
            def download_file(task):
                scope, target, part, relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, _change_type = task

                if part == "typehints":
                    source = pkg_meta.get("typehint", "")
                    if not source:
                        return (False, relpath, "No typehint path in metadata")
                else:
                    source = pkg_meta.get("source", "")

                if scope == "device" and part == "typehints":
                    out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, target, relpath.replace("/", os.sep))
                else:
                    out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, relpath.replace("/", os.sep))

                try:
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    InstallHelper.download_raw_file(owner, repo, ref, source, out_path)

                    if part == "typehints":
                        submodules = pkg_meta.get("submodules_typehints", [])
                    else:
                        submodules = pkg_meta.get("submodules", [])

                    for submodule_path in submodules:
                        relpath_dir = os.path.dirname(relpath) if "/" in relpath or "\\" in relpath else ""
                        submodule_filename = os.path.basename(submodule_path)
                        sub_relpath = os.path.join(relpath_dir, submodule_filename) if relpath_dir else submodule_filename

                        if scope == "device" and part == "typehints":
                            sub_out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, target, sub_relpath.replace("/", os.sep))
                        else:
                            sub_out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, sub_relpath.replace("/", os.sep))

                        os.makedirs(os.path.dirname(sub_out_path), exist_ok=True)
                        InstallHelper.download_raw_file(owner, repo, ref, submodule_path, sub_out_path)

                    unique_pkg_name = f"{scope}:{target}:{orig_pkg_name}"
                    source_key = source.replace("\\", "/") if source else ""
                    if source_key:
                        _local_touch_package(f"{unique_pkg_name}@{source_key}", orig_pkg_meta)
                    else:
                        _local_touch_package(unique_pkg_name, orig_pkg_meta)
                    return (True, relpath, None)
                except urllib.error.HTTPError:
                    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{source}"
                    return (False, relpath, f"404 Not Found - URL: {url}")
                except OSError as e:
                    return (False, relpath, f"Local: {e}")
                except Exception as e:
                    return (False, relpath, str(e))

            default_workers = 4
            max_workers = min(
                int(os.environ.get("REPLX_DOWNLOAD_THREADS", str(default_workers))),
                total,
                8
            )

            done = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_task = {executor.submit(download_file, task): task for task in plan}

                for future in as_completed(future_to_task):
                    success, relpath, error = future.result()
                    done += 1
                    if not success:
                        errors.append(f"{relpath}: {error}")
                    if progress:
                        progress({
                            "phase": "download",
                            "current": done,
                            "total": total,
                            "file": relpath,
                            "target": download_target,
                        })

        if device_exists_remote and dev:
            device_typehints_pkg_dir = os.path.join(
                StoreManager.pkg_root(), "device", device_name_to_path(dev),
                "typehints", device_name_to_path(dev))
            init_pyi_path = os.path.join(device_typehints_pkg_dir, "__init__.pyi")

            if os.path.isdir(device_typehints_pkg_dir) and not os.path.exists(init_pyi_path):
                try:
                    with open(init_pyi_path, "w", encoding="utf-8") as f:
                        f.write("# Type hints for device package\n")
                except Exception:
                    pass

        StoreManager.save_local_meta(local)

    return {
        "core": core,
        "device": dev,
        "core_in_registry": True,
        "device_in_registry": device_exists_remote,
        "total": total,
        "new": new_count,
        "upd": upd_count,
        "errors": errors,
        "target": download_target,
    }


def build_install_batch(spec: str, core: str, device: str, version: str,
                        progress: Optional[ProgressFn] = None) -> dict:
    if not (spec.startswith("core.") or spec.startswith("device.")):
        raise PkgError(f"Invalid spec format: {spec}")

    scope, rest = InstallHelper.resolve_spec(spec)
    base, local_list = InstallHelper.list_local_py_targets(scope, rest, core, device)
    if not local_list:
        raise PkgError("No local files to install. Run 'replx pkg download' first.")

    total = len(local_list)
    arch_tag = CompilerHelper.mpy_arch_tag(core)

    batch_specs: list[tuple[str, str]] = []
    unique_dirs: set[str] = set()

    for idx, (abs_file, rel) in enumerate(local_list):
        if progress:
            progress({
                "phase": "compile",
                "current": idx,
                "total": total,
                "file": os.path.basename(rel),
            })

        rel_dir = os.path.dirname(rel)
        is_python = abs_file.endswith(".py")

        if rel.startswith("ext/") and "/" in rel[4:]:
            parts = rel.split("/")
            if len(parts) >= 3:
                rel_dir = "ext"
                remote_dir = InstallHelper.remote_dir_for(scope, rel_dir, device)

                if is_python:
                    CompilerHelper.compile_to_staging(abs_file, base, core, version)
                    out_file = CompilerHelper.staging_out_for(abs_file, base, arch_tag)
                    remote_path = ("/" + remote_dir + os.path.splitext(parts[-1])[0] + ".mpy").replace("//", "/")
                else:
                    out_file = abs_file
                    remote_path = ("/" + remote_dir + parts[-1]).replace("//", "/")

                batch_specs.append((out_file, remote_path))
                unique_dirs.add(remote_dir)
                continue

        remote_dir = InstallHelper.remote_dir_for(scope, rel_dir, device)

        if is_python:
            CompilerHelper.compile_to_staging(abs_file, base, core, version)
            out_file = CompilerHelper.staging_out_for(abs_file, base, arch_tag)
            remote_path = ("/" + remote_dir + os.path.splitext(os.path.basename(rel))[0] + ".mpy").replace("//", "/")
        else:
            out_file = abs_file
            remote_path = ("/" + remote_dir + os.path.basename(rel)).replace("//", "/")

        batch_specs.append((out_file, remote_path))

        if remote_dir:
            unique_dirs.add(remote_dir)

    all_paths = set()
    for remote_dir in sorted(unique_dirs):
        parts = [p for p in remote_dir.replace("\\", "/").strip("/").split("/") if p]
        path = ""
        for p in parts:
            path = path + "/" + p
            all_paths.add(path)

    file_sizes = [os.path.getsize(lp) if os.path.exists(lp) else 0 for lp, _ in batch_specs]

    return {
        "specs": batch_specs,
        "sizes": file_sizes,
        "dirs": sorted(all_paths),
        "files": total,
        "total_bytes": sum(file_sizes),
    }


def clean(core: str, device: str) -> dict:
    pkg_root = StoreManager.pkg_root()
    device_key = device_name_to_path(device)
    core_path = os.path.join(pkg_root, "core", core)
    device_path = os.path.join(pkg_root, "device", device_key)
    meta_path = StoreManager.local_meta_path()

    core_exists = os.path.isdir(core_path)
    device_exists = os.path.isdir(device_path)
    meta_exists = os.path.isfile(meta_path)

    if not core_exists and not device_exists:
        return {"removed": [], "total_size": 0, "found": False}

    def get_dir_size(path: str) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    removed_items = []
    total_size = 0

    with StoreManager.LOCK:
        if core_exists:
            size = get_dir_size(core_path)
            shutil.rmtree(core_path)
            removed_items.append({"item": f"core/{core}/", "size": size})
            total_size += size

        if device_exists:
            size = get_dir_size(device_path)
            shutil.rmtree(device_path)
            removed_items.append({"item": f"device/{device}/", "size": size})
            total_size += size

        if meta_exists:
            local_meta = StoreManager.load_local_meta()
            local_packages = local_meta.get("packages", {})

            core_prefix = f"core:{core}:"
            device_prefix = f"device:{device_key}:"

            keys_to_remove = [k for k in local_packages.keys()
                              if k.startswith(core_prefix) or k.startswith(device_prefix)]

            entries_removed = 0
            if keys_to_remove:
                for key in keys_to_remove:
                    del local_packages[key]
                entries_removed += len(keys_to_remove)

            if core_exists and "platform_cores" in local_meta:
                if core in local_meta["platform_cores"]:
                    del local_meta["platform_cores"][core]
                    entries_removed += 1

            if device_exists and "device_configs" in local_meta:
                if device_key in local_meta["device_configs"]:
                    del local_meta["device_configs"][device_key]
                    entries_removed += 1

            if entries_removed > 0:
                StoreManager.save_local_meta(local_meta)
                removed_items.append({"item": "registry.json", "entries": entries_removed})

    return {"removed": removed_items, "total_size": total_size, "found": True}


def store_status(core: str, device: str) -> dict:
    StoreManager.ensure_home_store()
    ready, reason = InstallHelper.local_store_ready_for_full_install(core, device)
    return {
        "ready": ready,
        "reason": reason,
        "meta": os.path.isfile(StoreManager.local_meta_path()),
        "core_src": os.path.isdir(os.path.join(StoreManager.pkg_root(), "core", core, "src")),
        "device_src": os.path.isdir(os.path.join(
            StoreManager.pkg_root(), "device", device_name_to_path(device), "src")),
    }


def _typehints_dir(scope: str, core: str, device: str) -> str:
    if scope == "core":
        return os.path.join(StoreManager.pkg_root(), "core", core, "typehints")
    return os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(device), "typehints")


def typehints_list(core: str, device: str) -> dict:
    files = []
    for scope in ("core", "device"):
        base = _typehints_dir(scope, core, device)
        if not os.path.isdir(base):
            continue
        for dp, _dirs, fns in os.walk(base):
            for fn in fns:
                ap = os.path.join(dp, fn)
                rel = os.path.relpath(ap, base).replace("\\", "/")
                digest = hashlib.sha256()
                try:
                    with open(ap, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            digest.update(chunk)
                    files.append({
                        "scope": scope,
                        "relpath": rel,
                        "size": os.path.getsize(ap),
                        "sha256": digest.hexdigest(),
                    })
                except OSError:
                    continue
    return {"core": core, "device": device, "files": files}


def typehints_read(scope: str, relpath: str, core: str, device: str) -> bytes:
    if scope not in ("core", "device"):
        raise PkgError(f"Invalid typehints scope: {scope}")
    base = os.path.realpath(_typehints_dir(scope, core, device))
    target = os.path.realpath(os.path.join(base, relpath.replace("/", os.sep)))
    if not (target == base or target.startswith(base + os.sep)):
        raise PkgError(f"Invalid typehints path: {relpath}")
    if not os.path.isfile(target):
        raise PkgError(f"Typehint file not found: {relpath}")
    with open(target, "rb") as f:
        return f.read()

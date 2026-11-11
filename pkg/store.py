import os
import json
import base64
import threading
import urllib.request
from pathlib import Path

from replx_agent.utils.constants import HTTP_REQUEST_TIMEOUT
from replx_agent.utils.exceptions import PkgError


class StoreManager:
    HOME_STORE = Path.home() / ".replx"
    HOME_STAGING = HOME_STORE / ".staging"
    META_NAME = "registry.json"
    # Serializes store mutations (meta writes, clean) across agent threads.
    LOCK = threading.RLock()

    @staticmethod
    def ensure_home_store():
        StoreManager.HOME_STORE.mkdir(parents=True, exist_ok=True)
        (StoreManager.HOME_STORE / "core").mkdir(exist_ok=True)
        (StoreManager.HOME_STORE / "device").mkdir(exist_ok=True)
        StoreManager.HOME_STAGING.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def pkg_root() -> str:
        StoreManager.ensure_home_store()
        return str(StoreManager.HOME_STORE)

    @staticmethod
    def local_meta_path() -> str:
        return os.path.join(StoreManager.pkg_root(), StoreManager.META_NAME)

    @staticmethod
    def gh_headers() -> dict:
        hdrs = {"User-Agent": "replx"}
        tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if tok:
            hdrs["Authorization"] = f"******"
        return hdrs

    @staticmethod
    def load_local_meta() -> dict:
        p = StoreManager.local_meta_path()
        if not os.path.exists(p):
            return {"targets": {}, "items": {}}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"targets": {}, "items": {}}

    @staticmethod
    def save_local_meta(meta: dict):
        p = StoreManager.local_meta_path()
        tmp = p + ".tmp"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    @staticmethod
    def load_remote_meta(owner: str, repo: str, ref_: str) -> dict:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{StoreManager.META_NAME}?ref={ref_}"
        req = urllib.request.Request(url, headers=StoreManager.gh_headers())
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as r:
            data = json.load(r)
        b64 = (data.get("content") or "").replace("\n", "")
        if not b64:
            raise PkgError("Remote meta has no content.")
        txt = base64.b64decode(b64.encode("utf-8")).decode("utf-8")
        return json.loads(txt)

    @staticmethod
    def refresh_meta_if_online(owner: str, repo: str, ref_: str) -> bool:
        try:
            remote = StoreManager.load_remote_meta(owner, repo, ref_)
            with StoreManager.LOCK:
                StoreManager.save_local_meta(remote)
            return True
        except Exception:
            return False

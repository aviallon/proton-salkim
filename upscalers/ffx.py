"""Provision the pinned FidelityFX SDK; Wine selects compatible replacements."""

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import URLError
from urllib.parse import urlparse

from .storage import ComponentError, atomic_json, data_home, download_file, locked, matches


_NAMES = frozenset((
    "amd_fidelityfx_dx12.dll", "amd_fidelityfx_vk.dll",
    "amd_fidelityfx_loader_dx12.dll", "amd_fidelityfx_upscaler_dx12.dll",
    "amd_fidelityfx_framegeneration_dx12.dll", "amd_fidelityfx_denoiser_dx12.dll",
    "amd_fidelityfx_radiancecache_dx12.dll", "amd_fidelityfx_loader_vk.dll",
    "amd_fidelityfx_upscaler_vk.dll", "amd_fidelityfx_framegeneration_vk.dll",
))


def catalog():
    data = json.loads((Path(__file__).parent / "releases/ffx.json").read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 1 or
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", data.get("version", "")) or
            set(data.get("files", {})) != _NAMES):
        raise ComponentError("Invalid FidelityFX SDK manifest")
    for name, item in data["files"].items():
        if not isinstance(item, dict):
            raise ComponentError("Invalid FidelityFX SDK file")
        url = urlparse(item.get("url", ""))
        if (url.scheme != "https" or url.hostname != "raw.githubusercontent.com" or
                not re.fullmatch(r"/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/[0-9a-f]{40}/.+/" + re.escape(name), url.path) or
                not isinstance(item.get("size"), int) or item["size"] <= 0 or
                not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))):
            raise ComponentError("Invalid FidelityFX SDK file")
    return data


def prepare(env, progress=None):
    """Return one complete SDK directory, shared by games and OptiScaler."""
    if env.get("WINE_FFX_PROVIDER"):
        path = Path(env["WINE_FFX_PROVIDER"])
        if not path.is_absolute() or not path.is_dir():
            raise ComponentError("WINE_FFX_PROVIDER must name an absolute SDK directory")
        return path
    data = catalog()
    root = data_home(env) / "proton/components/ffx"
    target = root / data["version"]
    with locked(root / ".lock"):
        if target.is_symlink():
            raise ComponentError("The FidelityFX SDK directory must not be a symlink")
        if all(matches(target / name, item["size"], item["sha256"]) for name, item in data["files"].items()):
            return target.resolve()
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as directory:
            stage = Path(directory) / "sdk"
            stage.mkdir()
            total = sum(item["size"] for item in data["files"].values())
            completed = 0
            for name, item in data["files"].items():
                def update(stage, done, _total):
                    return progress(stage, completed + done, total) if progress else None
                download_file(item, stage / name, update, hosts=("raw.githubusercontent.com",))
                completed += item["size"]
            atomic_json(stage / "metadata.json", data)
            previous = Path(directory) / "previous"
            if target.exists():
                os.replace(target, previous)
            try:
                os.replace(stage, target)
            except OSError:
                if previous.exists():
                    os.replace(previous, target)
                raise
        return target.resolve()


def setup(env, *, progress=None, log=print):
    if env.get("WINE_OPTISCALER_NAME"):
        return False
    try:
        directory = prepare(env, progress)
        env["WINE_FFX_PROVIDER"] = str(directory)
        log("FidelityFX SDK ready: " + str(directory))
        return True
    except (ComponentError, OSError, ValueError, TypeError, URLError) as error:
        log("FidelityFX SDK upgrade unavailable: " + str(error))
        return False

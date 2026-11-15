"""Independent upscaler component setup for the Proton launcher."""

import platform

from . import ffx, fsr, progress


_HELPERS = frozenset((
    "iscriptevaluator.exe", "wineboot.exe", "winecfg.exe", "regedit.exe",
    "reg.exe", "rundll32.exe", "explorer.exe", "services.exe", "winedbg.exe",
))


def eligible_amd(gpus):
    """Use Vulkan capabilities for provisioning; Wine checks the actual device."""
    gpus = tuple(gpus)
    # Use an iGPU automatically only when no discrete or unknown GPU is visible.
    allow_integrated = all(
        getattr(getattr(gpu, "deviceType", None), "name", "") in ("INTEGRATED_GPU", "CPU")
        for gpu in gpus)
    for gpu in gpus:
        kind = getattr(getattr(gpu, "deviceType", None), "name", "")
        if gpu.vendorID != 0x1002 or kind not in (
                ("DISCRETE_GPU", "INTEGRATED_GPU") if allow_integrated else ("DISCRETE_GPU",)):
            continue
        names = {getattr(extension, "extensionName", extension) for extension in gpu.extensions}
        if "VK_KHR_fragment_shading_rate" in names or "VK_EXT_shader_float8" in names:
            return True
    return False


def setup(env, *, mode, command, gpu_query, log=print):
    if mode not in ("run", "waitforexitandrun") or not command:
        return False
    executable = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable in _HELPERS:
        return False
    if env.get("WINEARCH") == "win32" or platform.machine() not in ("x86_64", "amd64"):
        return False
    try:
        with progress.Progress(log=log) as reporter:
            provider_ready = fsr.setup(env, gpu_eligible=lambda: eligible_amd(gpu_query()),
                                       progress=reporter.update, log=log)
        if provider_ready or env.get("WINE_FFX_PROVIDER"):
            with progress.Progress(log=log) as reporter:
                ffx.setup(env, progress=reporter.update, log=log)
        return provider_ready
    except Exception as error:
        log("FSR component setup failed; keeping the current game configuration: " + str(error))
        return False

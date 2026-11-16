"""Install the WireGuard command line port into a prefix that runs Settlers United.

Settlers United brings a WireGuard tunnel up by running the wireguard-windows
command line ("wireguard.exe /installtunnelservice <conf>"). WireGuard for
Windows needs a kernel-mode NDIS driver, which Wine cannot load, so this Proton
build ships a port in <proton>/wireguard/wireguard.exe that keeps that command
line and lets the host's wg-quick create the tunnel.

The application finds wireguard.exe by walking the registry Path and taking the
first "<entry>\\wireguard.exe" that exists, so the port is installed into
C:\\Program Files\\WireGuard\\ and that directory is added to the Path, which is
exactly what the official MSI would have done. Both steps are idempotent and run
from the proton script on every launch, the way the upscalers are installed.

Nothing here touches a prefix that does not contain Settlers United.
"""

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

PORT_DIR = "Program Files/WireGuard"
PORT_NAME = "wireguard.exe"

# Where Settlers United ends up, depending on how it was installed: the
# launcher's own installer, a GOG/Ubisoft prefix, or the per-user Programs dir.
MARKERS = (
    "Program Files (x86)/Settlers United",
    "Program Files/Settlers United",
    "users/*/AppData/Local/Programs/settlers-united",
)

PATH_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
PORT_DOS_DIR = "C:\\Program Files\\WireGuard\\"


def has_settlers_united(prefix_dir: Path) -> bool:
    """Whether this prefix has a Settlers United installation."""
    drive_c = prefix_dir / "drive_c"
    for marker in MARKERS:
        pattern = (
            drive_c / marker if "*" not in marker else Path(str(drive_c) + "/" + marker)
        )
        if glob.glob(str(pattern)):
            return True
    return False


def _registry_path(wine, env) -> str | None:
    """Current value of the system Path, or None if it cannot be read."""
    try:
        result = subprocess.run(
            [str(wine), "reg", "query", PATH_KEY, "/v", "Path"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.replace("\r", "").splitlines():
        fields = line.split("    ")
        if len(fields) >= 4 and fields[1].lower() == "path":
            return fields[3]
    return None


def ensure_path_entry(wine, env, log, dry_run: bool = False) -> bool:
    """Add the port directory to the system Path, as the official MSI does."""
    current = _registry_path(wine, env)
    if current is None:
        log.warning("could not read the system Path, leaving it alone")
        return False
    entries = [entry.strip() for entry in current.split(";")]
    if PORT_DOS_DIR.rstrip("\\") in [entry.rstrip("\\") for entry in entries]:
        return True

    value = current.rstrip(";") + ";" + PORT_DOS_DIR
    if dry_run:
        log.info(f"would set the system Path to {value}")
        return True

    try:
        result = subprocess.run(
            [
                str(wine),
                "reg",
                "add",
                PATH_KEY,
                "/v",
                "Path",
                "/t",
                "REG_EXPAND_SZ",
                "/d",
                value,
                "/f",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(f"could not extend the system Path: {exc}")
        return False
    if result.returncode != 0:
        log.warning(f"could not extend the system Path: {result.stderr.strip()}")
        return False
    log.info("added C:\\Program Files\\WireGuard to the system Path")
    return True


def install(
    env, proton_dir: Path, prefix_dir: Path, wine, log, dry_run: bool = False
) -> None:
    """Put the port where Settlers United looks for it, if it is installed here."""
    if not has_settlers_united(prefix_dir):
        return

    source = Path(proton_dir) / "wireguard" / PORT_NAME
    if not source.is_file():
        log.warning(
            f"{source} is missing from this Proton build, cannot install the WireGuard port"
        )
        return

    target = prefix_dir / "drive_c" / PORT_DIR / PORT_NAME
    if target.is_file() and target.read_bytes() == source.read_bytes():
        log.debug(f"{target} is up to date")
    elif dry_run:
        log.info(f"would install {source} -> {target}")
    else:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError as exc:
            log.warning(f"could not install the WireGuard port: {exc}")
            return
        log.info(f"installed the WireGuard port into {target.parent}")

    ensure_path_entry(wine, env, log, dry_run=dry_run)


def _main(argv: list[str]) -> int:
    """Dry run against a prefix, for testing: wireguard_port.py <proton> <prefix>"""
    import logging

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    log = logging.getLogger("wireguard_port")
    if len(argv) != 3:
        print(f"usage: {argv[0]} <proton-dir> <prefix-dir>", file=sys.stderr)
        return 64
    wine = os.environ.get("WINE_BIN", "wine")
    install(os.environ, Path(argv[1]), Path(argv[2]), wine, log)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))

"""Download and extract pinned components without running their installers."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlparse

from . import sevenzip


class ComponentError(Exception):
    """A component could not be prepared; the game can use its own files."""


def data_home(env):
    value = env.get("XDG_DATA_HOME", "")
    return Path(value) if value and os.path.isabs(value) else Path.home() / ".local/share"


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def matches(path, size, digest):
    path = Path(path)
    return (not path.is_symlink() and path.is_file() and
            path.stat().st_size == size and checksum(path) == digest)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _metadata(release):
    provider = {key: value for key, value in release["provider"].items()
                if key != "effect_version" and not key.startswith("model_")}
    return {**release, "provider": provider}


def report(progress, stage, done, total):
    if progress is not None and progress(stage, done, total) is False:
        raise ComponentError("FSR component preparation cancelled")


def download_file(item, destination, progress=None, *, hosts, referer=None):
    address = urlparse(item["url"])
    if address.scheme != "https" or address.hostname not in hosts:
        raise ComponentError("Unrecognised component download source")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Proton component manager)"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(item["url"], headers=headers)
    digest = hashlib.sha256()
    received = 0
    report(progress, "download", 0, item["size"])
    with urllib.request.urlopen(request, timeout=30) as response:
        address = urlparse(response.geturl())
        if address.scheme != "https" or address.hostname not in hosts:
            raise ComponentError("Unrecognised component download source")
        with Path(destination).open("wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                received += len(block)
                if received > item["size"]:
                    raise ComponentError("Component download exceeds its recorded size")
                output.write(block)
                digest.update(block)
                report(progress, "download", received, item["size"])
    if received != item["size"] or digest.hexdigest() != item["sha256"]:
        raise ComponentError("Component download does not match the selected release")


def download(release, destination, progress=None):
    download_file(release["archive"], destination, progress,
                  hosts=("drivers.amd.com", "download.amd.com"), referer=release["release_notes"])


def extract(archive, release, destination, progress=None, *, program):
    payload = release["provider"]
    received = 0
    digest = hashlib.sha256()
    report(progress, "extract", 0, payload["size"])
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [program, "e", "-so", "--", str(archive), payload["member"]],
            stdout=subprocess.PIPE, stderr=errors)
        try:
            with Path(destination).open("wb") as output:
                while True:
                    block = process.stdout.read(1024 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > payload["size"]:
                        raise ComponentError("Extracted FSR provider exceeds its recorded size")
                    output.write(block)
                    digest.update(block)
                    report(progress, "extract", received, payload["size"])
            if process.wait(timeout=30):
                errors.seek(0)
                detail = errors.read(4096).decode("utf-8", "replace").strip()
                raise ComponentError("Cannot extract the FSR provider: " + detail)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    if received != payload["size"] or digest.hexdigest() != payload["sha256"]:
        raise ComponentError("Extracted FSR provider does not match the selected release")


class Store:
    def __init__(self, env):
        self.env = env
        self.root = data_home(env) / "proton/components/fsr/driver"

    def lock(self):
        return locked(self.root / ".lock")

    def installed(self, release):
        directory = self.root / release["version"]
        provider = directory / "amdxcffx64.dll"
        try:
            metadata_path = directory / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or not isinstance(metadata.get("provider"), dict):
                return None
            if (metadata.get("version") != release["version"] or
                    metadata.get("provider", {}).get("sha256") != release["provider"]["sha256"] or
                    not matches(provider, release["provider"]["size"], release["provider"]["sha256"])):
                return None
            cleaned = _metadata(metadata)
            if cleaned != metadata:
                try:
                    atomic_json(metadata_path, cleaned)
                except OSError:
                    pass
            return provider
        except (OSError, ValueError, TypeError):
            return None

    def prepare(self, release, progress=None, archive=None):
        """Called under lock; publish only a complete, verified installation."""
        target = self.root / release["version"]
        if target.is_symlink():
            raise ComponentError("The FSR component directory must not be a symlink")
        current = self.installed(release)
        if current:
            return current
        program = sevenzip.extractor(env=self.env)
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.root) as name:
            temporary = Path(name)
            if archive is None:
                archive = temporary / "driver.exe"
                download(release, archive, progress)
            else:
                archive = Path(archive)
                if not matches(archive, release["archive"]["size"], release["archive"]["sha256"]):
                    raise ComponentError("The imported AMD driver does not match the selected release")
            prepared = temporary / "component"
            prepared.mkdir()
            extract(archive, release, prepared / "amdxcffx64.dll", progress, program=program)
            atomic_json(prepared / "metadata.json", _metadata(release))
            previous = temporary / "previous"
            if target.exists():
                os.replace(target, previous)
            try:
                os.replace(prepared, target)
            except OSError:
                if previous.exists():
                    os.replace(previous, target)
                raise
        return target / "amdxcffx64.dll"

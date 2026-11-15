"""Shared SDK transactions, compatibility inputs and launcher policy."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from upscalers import ffx, storage


class FfxComponents(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"XDG_DATA_HOME": str(self.root)}
        self.data = deepcopy(ffx.catalog())
        self.payloads = {name: name.encode() for name in self.data["files"]}
        for name, item in self.data["files"].items():
            item.update(size=len(self.payloads[name]), sha256=hashlib.sha256(self.payloads[name]).hexdigest())
        patch = mock.patch.object(ffx, "catalog", return_value=self.data)
        patch.start()
        self.addCleanup(patch.stop)
        download = mock.patch.object(ffx, "download_file", side_effect=self.download)
        self.downloader = download.start()
        self.addCleanup(download.stop)

    def download(self, item, path, progress=None, **options):
        name = Path(path).name
        Path(path).write_bytes(self.payloads[name])
        if progress:
            progress("download", item["size"], item["size"])

    def test_atomic_directory_and_reuse_without_prefix_copies(self):
        self.assertTrue(ffx.setup(self.env, log=lambda _: None))
        directory = Path(self.env["WINE_FFX_PROVIDER"])
        self.assertTrue(directory.is_absolute())
        self.assertEqual(set(p.name for p in directory.iterdir()), set(self.data["files"]) | {"metadata.json"})
        self.assertFalse(any(p.is_symlink() for p in directory.iterdir()))
        self.assertEqual(self.env, {"XDG_DATA_HOME": str(self.root), "WINE_FFX_PROVIDER": str(directory)})
        self.env.pop("WINE_FFX_PROVIDER")
        self.downloader.reset_mock()
        self.assertEqual(ffx.prepare(self.env), directory)
        self.downloader.assert_not_called()

    def test_failed_download_never_publishes_partial_sdk(self):
        self.downloader.side_effect = [None, OSError("offline")]
        self.assertFalse(ffx.setup(self.env, log=lambda _: None))
        self.assertNotIn("WINE_FFX_PROVIDER", self.env)
        self.assertFalse((self.root / "proton/components/ffx" / self.data["version"]).exists())

    def test_damaged_cache_is_replaced_as_a_set(self):
        directory = ffx.prepare(self.env)
        first = next(iter(self.payloads))
        (directory / first).write_bytes(b"damaged")
        self.assertEqual(ffx.prepare(self.env), directory)
        self.assertEqual((directory / first).read_bytes(), self.payloads[first])

    def test_rollback_preserves_previous_directory(self):
        directory = ffx.prepare(self.env)
        first = next(iter(self.payloads))
        (directory / first).write_bytes(b"old")
        replace = ffx.os.replace
        def fail_publish(source, target):
            if Path(source).name == "sdk":
                raise OSError("rename failed")
            return replace(source, target)
        with mock.patch.object(ffx.os, "replace", side_effect=fail_publish):
            self.assertFalse(ffx.setup(self.env, log=lambda _: None))
        self.assertEqual((directory / first).read_bytes(), b"old")
        self.assertNotIn("WINE_FFX_PROVIDER", self.env)

    def test_changed_manifest_preserves_previous_release(self):
        old = ffx.prepare(self.env)
        self.data["version"] = "next-release"
        new = ffx.prepare(self.env)
        self.assertNotEqual(old, new)
        self.assertTrue(old.is_dir())
        self.assertTrue(new.is_dir())

    def test_symlink_target_is_not_followed(self):
        directory = self.root / "proton/components/ffx" / self.data["version"]
        directory.parent.mkdir(parents=True)
        directory.symlink_to(self.root / "elsewhere")
        self.assertFalse(ffx.setup(self.env, log=lambda _: None))
        self.downloader.assert_not_called()

    def test_concurrent_launches_share_one_download(self):
        with ThreadPoolExecutor(2) as executor:
            directories = list(executor.map(lambda _: ffx.prepare(self.env), range(2)))
        self.assertEqual(directories[0], directories[1])
        self.assertEqual(self.downloader.call_count, len(self.payloads))

    def test_explicit_path_is_preserved(self):
        directory = self.root / "manual SDK"
        directory.mkdir()
        self.env["WINE_FFX_PROVIDER"] = str(directory)
        self.assertTrue(ffx.setup(self.env, log=lambda _: None))
        self.assertEqual(self.env["WINE_FFX_PROVIDER"], str(directory))
        self.downloader.assert_not_called()

    def test_invalid_explicit_path_is_nonfatal(self):
        self.env["WINE_FFX_PROVIDER"] = "relative/sdk"
        self.assertFalse(ffx.setup(self.env, log=lambda _: None))
        self.downloader.assert_not_called()

    def test_optiscaler_does_not_enable_game_sdk_replacement(self):
        self.env["WINE_OPTISCALER_NAME"] = "dxgi.dll"
        before = dict(self.env)
        self.assertFalse(ffx.setup(self.env))
        self.assertEqual(self.env, before)
        self.downloader.assert_not_called()

    def test_progress_covers_the_whole_set(self):
        events = []
        ffx.prepare(self.env, lambda *args: events.append(args))
        self.assertEqual(events[-1][1], sum(len(p) for p in self.payloads.values()))
        self.assertTrue(all(e[1] <= e[2] == events[-1][1] for e in events))
        self.assertEqual(sorted(e[1] for e in events), [e[1] for e in events])


class FfxDownload(unittest.TestCase):
    def test_download_checks_size_hash_and_final_host(self):
        content = b"verified DLL bytes"
        item = {"url": "https://raw.githubusercontent.com/amd/test.dll", "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sdk.dll"
            for payload, url, valid in [(content, item["url"], True),
                                        (content[:-1], item["url"], False),
                                        (content + b"x", item["url"], False),
                                        (b"x" * len(content), item["url"], False),
                                        (content, "https://example.com/sdk.dll", False)]:
                response = io.BytesIO(payload)
                response.geturl = lambda: url
                with mock.patch.object(storage.urllib.request, "urlopen", return_value=response):
                    if valid:
                        storage.download_file(item, path, hosts=("raw.githubusercontent.com",))
                        self.assertEqual(path.read_bytes(), content)
                    else:
                        with self.assertRaises(storage.ComponentError):
                            storage.download_file(item, path, hosts=("raw.githubusercontent.com",))

    def test_manifest_uses_pinned_official_files_and_both_layouts(self):
        data = ffx.catalog()
        self.assertIn("amd_fidelityfx_dx12.dll", data["files"])
        self.assertIn("amd_fidelityfx_vk.dll", data["files"])
        self.assertIn("amd_fidelityfx_loader_dx12.dll", data["files"])
        self.assertIn("amd_fidelityfx_loader_vk.dll", data["files"])
        for item in data["files"].values():
            self.assertRegex(item["url"], r"/FidelityFX-SDK/[0-9a-f]{40}/")


if __name__ == "__main__":
    unittest.main()

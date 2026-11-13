import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from archive.services.paths import open_root, open_regular_beneath, UnsafePath
from archive.tests.fixtures import jpeg


class PathSafetyTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="birds-paths-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_parent_replacement_cannot_redirect_open_file(self):
        (self.root / "nested").mkdir()
        jpeg(self.root / "nested/photo.jpg")
        wanted = (self.root / "nested/photo.jpg").read_bytes()
        fd = open_root(self.root)
        self.addCleanup(os.close, fd)
        real_open = os.open

        def replace(path, flags, *args, **kwargs):
            result = real_open(path, flags, *args, **kwargs)
            if path == "nested":
                (self.root / "nested").rename(self.root / "old")
                (self.root / "nested").symlink_to(self.root / "outside")
            return result

        (self.root / "outside").mkdir()
        jpeg(self.root / "outside/photo.jpg", color=(20, 30, 40))
        with mock.patch("os.open", side_effect=replace):
            with open_regular_beneath(fd, "nested/photo.jpg") as stream:
                self.assertEqual(stream.read(), wanted)
        with self.assertRaises((UnsafePath, OSError)):
            open_regular_beneath(fd, "nested/photo.jpg")

    def test_fifo_is_rejected_in_bounded_process(self):
        os.mkfifo(self.root / "pipe.jpg")
        code = "from archive.services.paths import *; import sys; f=open_root(sys.argv[1]); open_regular_beneath(f,'pipe.jpg')"
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True,
                                text=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("仅接受普通文件", result.stderr)

    def test_rejects_symlinks_special_files_and_invalid_paths(self):
        jpeg(self.root / "real.jpg")
        (self.root / "link.jpg").symlink_to(self.root / "real.jpg")
        (self.root / "dir").symlink_to(self.root, target_is_directory=True)
        fd = open_root(self.root)
        self.addCleanup(os.close, fd)
        for relative in ("link.jpg", "dir/real.jpg", "../real.jpg", "/real.jpg", "x//real.jpg", "x/./real.jpg", "x\\real.jpg", ""):
            with self.subTest(relative=relative), self.assertRaises((UnsafePath, OSError)):
                open_regular_beneath(fd, relative)

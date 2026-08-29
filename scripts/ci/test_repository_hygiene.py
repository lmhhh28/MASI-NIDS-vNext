from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().with_name("check_repository_hygiene.py")


def load_module():
    spec = importlib.util.spec_from_file_location("repository_hygiene", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load repository hygiene module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryHygieneTests(unittest.TestCase):
    def test_deleted_tracked_file_is_not_a_candidate(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            tracked = root / "renamed-away.txt"
            tracked.write_text("safe\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", tracked.name], check=True)
            tracked.unlink()
            paths = module.git_paths(root, include_untracked=True)
            self.assertIn(tracked, paths)
            self.assertFalse(tracked.exists())
            with (
                patch.object(
                    sys,
                    "argv",
                    [str(SCRIPT), "--root", str(root), "--include-untracked"],
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(0, module.main())

    def test_dangling_symlink_remains_detectable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary) / "dangling"
            os.symlink("missing", link)
            self.assertTrue(link.is_symlink())
            self.assertFalse(link.exists())


if __name__ == "__main__":
    unittest.main()

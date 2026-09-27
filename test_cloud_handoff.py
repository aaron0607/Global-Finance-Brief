"""cloud_handoff：推送到 briefs 分支、拒绝覆盖已就绪成稿、失败标记。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cloud_handoff

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        run("git", "init", "-q", "--bare", str(self.remote), cwd=root)
        seed = root / "seed"
        run("git", "init", "-q", str(seed), cwd=root)
        run("git", "checkout", "-q", "--orphan", "briefs", cwd=seed)
        (seed / "README.md").write_text("x")
        run("git", "add", ".", cwd=seed)
        run("git", "commit", "-qm", "init", cwd=seed)
        run("git", "remote", "add", "origin", str(self.remote), cwd=seed)
        run("git", "push", "-q", "origin", "briefs", cwd=seed)
        self.project = root / "project"
        run("git", "clone", "-q", "-b", "briefs", str(self.remote), str(self.project), cwd=root)
        self.patches = [mock.patch.object(cloud_handoff, "BASE", self.project),
                        mock.patch.dict(os.environ, GIT_ENV)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def remote_file(self, path):
        return run("git", "--git-dir", str(self.remote), "show", f"briefs:{path}", cwd=self.project)

    def test_ready_is_pushed_and_cannot_be_overwritten(self):
        src = self.project / "content_20260928.json"
        src.write_text('{"as_of": "x"}', encoding="utf-8")
        cloud_handoff.commit_and_push("20260928", {
            "content_20260928.json": src,
            "READY.json": json.dumps({"day": "20260928"}),
        }, "ready")
        self.assertEqual(self.remote_file("briefs/20260928/content_20260928.json"), '{"as_of": "x"}')
        self.assertIn("20260928", self.remote_file("briefs/20260928/READY.json"))
        with self.assertRaises(SystemExit):
            cloud_handoff.commit_and_push("20260928", {"READY.json": "{}"}, "again")
        worktrees = run("git", "worktree", "list", cwd=self.project).strip().splitlines()
        self.assertEqual(len(worktrees), 1)  # 临时 worktree 已清理

    def test_failed_marker(self):
        with mock.patch("sys.argv", ["cloud_handoff.py", "20260929", "--failed", "行情获取失败"]):
            cloud_handoff.main()
        self.assertIn("行情获取失败", self.remote_file("briefs/20260929/FAILED.txt"))

    def test_ready_requires_built_files(self):
        with self.assertRaises(SystemExit):
            cloud_handoff.preflight("20260930")


if __name__ == "__main__":
    unittest.main()

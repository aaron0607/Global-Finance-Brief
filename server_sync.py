#!/usr/bin/env python3
"""腾讯云服务器从 GitHub main 同步代码（经 REST API；服务器连不上 github.com，但 api.github.com 可达）。

只覆盖仓库里跟踪的文件；运行数据（content_*.json、日志、.venv、.secrets.env 等不在仓库里的文件）不动。
被覆盖前内容不同的旧文件先备份到 backups/sync_<时间>/。

用法（服务器上）：
    python3 server_sync.py            # 同步到本脚本所在目录
    python3 server_sync.py --dry-run  # 只列出会变化的文件

令牌：环境变量或本目录 .secrets.env 里的 BRIEF_GITHUB_TOKEN（只读 fine-grained token，Contents: Read-only）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = os.environ.get("BRIEF_REPO", "aaron0607/Global-Finance-Brief")
API = os.environ.get("BRIEF_GITHUB_API", "https://api.github.com")
REF = os.environ.get("BRIEF_SYNC_REF", "main")


def load_token() -> str:
    token = os.environ.get("BRIEF_GITHUB_TOKEN", "").strip()
    secrets = BASE / ".secrets.env"
    if not token and secrets.exists():
        for line in secrets.read_text(encoding="utf-8").splitlines():
            if line.startswith("BRIEF_GITHUB_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        sys.exit("✗ 缺 BRIEF_GITHUB_TOKEN（写入 .secrets.env 后重试）")
    return token


def api_get(path: str, token: str, raw: bool = False) -> bytes:
    request = urllib.request.Request(f"{API}{path}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "finance-brief-sync",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"✗ GitHub API {path} 返回 HTTP {exc.code}（401/403=令牌无效或权限不足，404=仓库/分支不存在或令牌未授权此仓库）")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    token = load_token()

    commit = json.loads(api_get(f"/repos/{REPO}/commits/{REF}", token))["sha"]
    tree = json.loads(api_get(f"/repos/{REPO}/git/trees/{commit}?recursive=1", token))
    if tree.get("truncated"):
        sys.exit("✗ 仓库文件树被截断，无法完整同步")
    blobs = [item for item in tree["tree"] if item["type"] == "blob"]

    changed = []
    for item in blobs:
        data = api_get(f"/repos/{REPO}/contents/{item['path']}?ref={commit}", token, raw=True)
        target = BASE / item["path"]
        if not target.exists() or target.read_bytes() != data:
            changed.append((item, data))

    print(f"GitHub {REF} @ {commit[:7]}：共 {len(blobs)} 个文件，{len(changed)} 个有变化")
    for item, _ in changed:
        print("  ~", item["path"])
    if args.dry_run or not changed:
        return

    backup = BASE / "backups" / ("sync_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    for item, data in changed:
        target = BASE / item["path"]
        if target.exists():
            (backup / item["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup / item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".sync_tmp")
        tmp.write_bytes(data)
        if item["mode"] == "100755":
            tmp.chmod(0o755)
        elif target.exists():
            tmp.chmod(target.stat().st_mode & 0o777)
        tmp.replace(target)
    (BASE / ".synced_commit").write_text(commit + "\n", encoding="utf-8")
    print(f"✓ 已同步到 {commit[:7]}" + (f"，旧文件备份在 {backup.relative_to(BASE)}" if backup.exists() else ""))


if __name__ == "__main__":
    main()

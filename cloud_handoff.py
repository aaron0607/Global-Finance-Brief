#!/usr/bin/env python3
"""把云端当日成稿交接到 GitHub `briefs` 分支，供腾讯云服务器 server_publish.sh 拉取发布。

分支结构（孤儿分支，只放运行产物，不放代码）：
    briefs/YYYYMMDD/content_YYYYMMDD.json
    briefs/YYYYMMDD/wechat_article_YYYYMMDD.html
    briefs/YYYYMMDD/READY.json        成稿就绪标记（as_of、标题、代码版本、交接时间）
    briefs/YYYYMMDD/FAILED.txt        失败标记（原因），服务器收到后立即飞书告警

用法：
    .venv/bin/python cloud_handoff.py YYYYMMDD                 # 交接成稿（先做与发布入口相同的终检）
    .venv/bin/python cloud_handoff.py YYYYMMDD --failed "原因"  # 交接失败标记

同一天已有 READY 时拒绝覆盖（服务器可能已发布）；需要修订已发布草稿时走 publish_wechat.py --update。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
BRANCH = "briefs"
REMOTE = "origin"
PUSH_ATTEMPTS = 4


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd or BASE, check=check, text=True, capture_output=True)


def preflight(day: str) -> dict:
    """与 publish_wechat.py 相同的发布前终检，保证交接出去的稿服务器端一定能过校验。"""
    from build_brief import validate_final_render
    from content_validation import validate_calendar, validate_emphasis, validate_no_structure_leaks

    content_path = BASE / f"content_{day}.json"
    html_path = BASE / f"wechat_article_{day}.html"
    for path in (content_path, html_path):
        if not path.exists():
            sys.exit(f"✗ 缺 {path.name}，先完成写稿与 build_brief.py 构建")
    content = json.loads(content_path.read_text(encoding="utf-8"))
    html = html_path.read_text(encoding="utf-8")
    errors = (validate_no_structure_leaks(content, html) + validate_emphasis(content)
              + validate_calendar(content))
    if errors:
        sys.exit("✗ 交接前终检失败：\n  " + "\n  ".join(errors))
    validate_final_render(content, html)
    return content


def commit_and_push(day: str, files: dict[str, Path | str], message: str) -> None:
    """在临时 worktree 里基于最新 origin/briefs 提交 briefs/DAY/ 下的文件并推送；非快进时重试。"""
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        git("fetch", "-q", REMOTE, BRANCH)
        with tempfile.TemporaryDirectory(prefix="briefs-") as tmp:
            wt = Path(tmp) / "wt"
            git("worktree", "add", "-q", "--detach", str(wt), f"{REMOTE}/{BRANCH}")
            try:
                target = wt / "briefs" / day
                ready = target / "READY.json"
                if ready.exists() and ready.read_text(encoding="utf-8") != files.get("READY.json"):
                    sys.exit(f"✗ briefs/{day}/READY.json 已存在（服务器可能已发布），拒绝覆盖；"
                             "修订请用 publish_wechat.py --update")
                target.mkdir(parents=True, exist_ok=True)
                for name, src in files.items():
                    if isinstance(src, Path):
                        shutil.copyfile(src, target / name)
                    else:
                        (target / name).write_text(src, encoding="utf-8")
                git("add", "-A", "briefs", cwd=wt)
                if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
                    # 远端已是同一份内容：上次推送返回失败但实际已落地，视为成功
                    print(f"✓ origin/{BRANCH} 已含相同的 briefs/{day}/，无需重复推送")
                    return
                git("commit", "-q", "-m", message, cwd=wt)
                push = git("push", REMOTE, f"HEAD:{BRANCH}", cwd=wt, check=False)
            finally:
                git("worktree", "remove", "--force", str(wt), check=False)
        if push.returncode == 0:
            print(f"✓ 已推送 {REMOTE}/{BRANCH}: briefs/{day}/ ({', '.join(files)})")
            return
        print(f"! 第 {attempt} 次推送失败：{push.stderr.strip()}", file=sys.stderr)
        time.sleep(2 ** attempt)
    sys.exit("✗ 推送 briefs 分支失败，服务器将在截止时间后告警")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day")
    parser.add_argument("--failed", metavar="REASON")
    args = parser.parse_args()
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    head = git("rev-parse", "--short", "HEAD").stdout.strip()

    if args.failed:
        commit_and_push(args.day, {"FAILED.txt": f"{now}\n{args.failed.strip()}\n"},
                        f"briefs {args.day}: 云端失败")
        return

    content = preflight(args.day)
    ready = {
        "day": args.day,
        "as_of": content.get("as_of"),
        "main_title": content.get("main_title"),
        "code_version": head,
        "handed_off_at": now,
    }
    commit_and_push(args.day, {
        f"content_{args.day}.json": BASE / f"content_{args.day}.json",
        f"wechat_article_{args.day}.html": BASE / f"wechat_article_{args.day}.html",
        "READY.json": json.dumps(ready, ensure_ascii=False, indent=2) + "\n",
    }, f"briefs {args.day}: 成稿就绪")


if __name__ == "__main__":
    main()

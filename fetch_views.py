#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一条命令拉取飞书「股市马斯克文章发表」最近 3 篇文章的 docx 正文（投资观点对齐用）。

两条路径：
1. OpenAPI（服务器友好）：读环境变量 LARK_APP_ID / LARK_APP_SECRET → tenant_access_token
   → drive files list → docx raw_content。前提是飞书后台把该文件夹/文档共享给对应应用
   （已实测 wb 应用 cli_aab8e4e5d8791bc9 可列文件夹；正文读取需文档共享给该应用）。
2. 回退 lark-cli（本机）：`lark-cli drive files list --as user` + docs fetch。

用法:
    .venv/bin/python fetch_views.py            # 最近 3 篇文章（按日期倒序）
    .venv/bin/python fetch_views.py 20260710   # 指定日期

本脚本失败不影响简报主体（run_brief.sh 将 fetch_views 视为非致命）。
"""
import json
import os
import re
import subprocess
import sys

import requests

FOLDER_TOKEN = "YKT1fTVr6lsGehdD1MZcknaEnre"
MAX_CHARS_PER_DOC = 12000
MAX_VIEWS = 3  # 取用户最近 3 篇文章（按日期倒序），反映其最新观点
FEISHU = "https://open.feishu.cn/open-apis"
LARK_APP_ID = os.environ.get("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()


def run(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        sys.exit("✗ lark-cli 失败: %s\n%s" % (" ".join(args[:4]), p.stderr[:500]))
    return json.loads(p.stdout)


def _openapi_tenant_token() -> str:
    resp = requests.post(
        FEISHU + "/auth/v3/tenant_access_token/internal",
        json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET},
        timeout=15,
    )
    data = resp.json()
    token = data.get("tenant_access_token")
    if not token:
        raise RuntimeError("飞书 tenant token 获取失败: %s" % data.get("msg"))
    return token


def _openapi_list(token: str) -> list[dict]:
    resp = requests.get(
        FEISHU + "/drive/v1/files",
        params={"folder_token": FOLDER_TOKEN, "order_by": "EditedTime", "direction": "DESC", "page_size": 20},
        headers={"Authorization": "Bearer " + token},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError("飞书文件列表失败: %s" % data.get("msg"))
    return data.get("data", {}).get("files") or []


def _openapi_fetch(token: str, document_id: str) -> str:
    resp = requests.get(
        FEISHU + "/docx/v1/documents/%s/raw_content" % document_id,
        headers={"Authorization": "Bearer " + token},
        timeout=20,
    )
    data = resp.json()
    if data.get("code") != 0:
        # 未共享给应用时常见 1770002 not found；提示后跳过该篇。
        raise RuntimeError("读取文档 %s 失败(code=%s): %s" % (document_id, data.get("code"), data.get("msg")))
    return (data.get("data", {}).get("content") or "").strip()


def fetch_via_openapi() -> list[tuple[str, str, str]]:
    """返回最近 3 篇 [(date, name, content)]，date 为文件名前 8 位，按日期倒序。"""
    token = _openapi_tenant_token()
    files = _openapi_list(token)
    docs = []
    for f in files:
        m = re.match(r"^(\d{8})", (f.get("name") or "").strip())
        if f.get("type") == "docx" and m:
            docs.append((m.group(1), f["name"].strip(), f.get("token")))
    if not docs:
        print("✗ 文件夹内未找到 YYYYMMDD 开头的 docx 文章", file=sys.stderr)
        return []
    docs.sort(key=lambda x: x[0], reverse=True)
    picked = docs[:MAX_VIEWS]
    results = []
    for _, name, doc_token in picked:
        try:
            content = _openapi_fetch(token, doc_token)
        except RuntimeError as exc:
            print("⚠️ %s" % exc, file=sys.stderr)
            continue
        content = re.sub(r"!\[[^\]]*\]\([^)]*\)\n?", "", content)
        if len(content) > MAX_CHARS_PER_DOC:
            content = content[:MAX_CHARS_PER_DOC] + "\n…[已截断]"
        results.append((_, name, content))
    return results


def fetch_via_larkcli(want_date=None) -> list[tuple[str, str, str]]:
    lst = run(["lark-cli", "drive", "files", "list", "--params",
               json.dumps({"folder_token": FOLDER_TOKEN, "order_by": "EditedTime",
                           "direction": "DESC", "page_size": 20}),
               "--as", "user"])
    docs = []
    for f in lst["data"]["files"]:
        m = re.match(r"^(\d{8})", f["name"].strip())
        if f["type"] == "docx" and m:
            docs.append((m.group(1), f["name"].strip(), f["token"]))
    if not docs:
        sys.exit("✗ 文件夹内未找到 YYYYMMDD 开头的 docx 文章")
    docs.sort(key=lambda x: x[0], reverse=True)
    if want_date:
        picked = [d for d in docs if d[0] == want_date]
        if not picked:
            sys.exit("✗ 未找到 %s 的文章，最近有: %s" % (want_date, sorted({d for d, _, _ in docs}, reverse=True)[:5]))
    else:
        picked = docs[:MAX_VIEWS]
    results = []
    for _, name, token in picked:
        doc = run(["lark-cli", "docs", "+fetch", "--doc", token,
                   "--doc-format", "markdown", "--json"])
        content = doc["data"]["document"]["content"]
        content = re.sub(r"!\[[^\]]*\]\([^)]*\)\n?", "", content)
        if len(content) > MAX_CHARS_PER_DOC:
            content = content[:MAX_CHARS_PER_DOC] + "\n…[已截断]"
        results.append((_, name, content))
    return results


def main():
    want_date = sys.argv[1] if len(sys.argv) > 1 else None
    if LARK_APP_ID and LARK_APP_SECRET:
        try:
            picked = fetch_via_openapi()
        except Exception as exc:  # noqa: BLE001 - OpenAPI 失败回退 lark-cli
            print("⚠️ OpenAPI 路径失败(%s)，回退 lark-cli" % exc, file=sys.stderr)
            picked = fetch_via_larkcli(want_date)
    else:
        picked = fetch_via_larkcli(want_date)
    if not picked:
        sys.exit("✗ 未能读取飞书文章（可能文档未共享给应用或 lark-cli 未授权）")
    dates = sorted({d for d, _, _ in picked}, reverse=True)
    print("观点参考文章（共 %d 篇，日期 %s，最新优先）" % (len(picked), " / ".join(dates)))
    for _, name, content in picked:
        print("\n" + "=" * 60)
        print("📄 %s" % name)
        print("=" * 60)
        print(content)


if __name__ == "__main__":
    main()

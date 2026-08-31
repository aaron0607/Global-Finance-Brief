#!/usr/bin/env python3
"""把当日 wechat_article_YYYYMMDD.html 自动发布到微信公众号草稿箱。

流程：提取 <div id="article"> 正文 → 前拼头图、后拼公众号名片（wechat_fragments/）
→ 在发布机执行 ~/wenyan-work/publish-brief.js 调 draft/add 建草稿。

运行位置自适应：
- 在云服务器 115.159.56.153 上（存在 ~/wenyan-work/publish-brief.js 与 .env）：本机直接执行 node，
  不再 ssh 回环；这是每日定时任务在服务器上运行时的路径。
- 在本机（无 ~/wenyan-work）：scp 到云服务器后远程执行 node。

用法：
    .venv/bin/python publish_wechat.py                         # 默认今天，创建草稿
    .venv/bin/python publish_wechat.py 20260718                # 指定日期，创建草稿
    .venv/bin/python publish_wechat.py --update 20260718 ID    # 更新已有草稿，避免重复创建

发布后用户仍需在公众号后台手动：勾选原创声明、创作来源提示选"投资观点仅供参考"、点发表。
（这两项微信未开放 API，无法自动化。）
"""
import json
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from build_brief import validate_final_render
from content_validation import (calendar_summary, emphasis_summary, validate_calendar,
                                validate_emphasis, validate_no_structure_leaks)

BASE = Path(__file__).parent
SSH = ["ssh", "-i", str(Path.home() / ".ssh/lct_cloud"), "ubuntu@115.159.56.153"]
SCP = ["scp", "-i", str(Path.home() / ".ssh/lct_cloud")]
REMOTE = "ubuntu@115.159.56.153:~/wenyan-work/incoming/"

# 本机就是发布服务器时（存在 node 发布脚本与微信 .env），改为本地执行。
ON_SERVER = (
    (Path.home() / "wenyan-work" / "publish-brief.js").exists()
    and (Path.home() / "wenyan-work" / ".env").exists()
)
LOCAL_INCOMING = Path.home() / "wenyan-work" / "incoming"


UPDATE_SCRIPT = r'''const fs=require("fs");const {execFileSync}=require("child_process");const h=process.env.HOME;
const e=Object.fromEntries(fs.readFileSync(h+"/wenyan-work/.env","utf8").trim().split("\n").filter(Boolean).map(x=>{const i=x.indexOf("=");return [x.slice(0,i),x.slice(i+1)]}));
const c=fs.readFileSync(h+"/wenyan-work/incoming/content.html","utf8");const m=JSON.parse(fs.readFileSync(h+"/wenyan-work/incoming/meta.json","utf8"));
const t=fs.readFileSync(h+"/wenyan-work/brief-cover-media-id.txt","utf8").trim();const id=process.argv[1];
(async()=>{const k=await (await fetch(`https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=${e.WECHAT_APP_ID}&secret=${e.WECHAT_APP_SECRET}`)).json();if(!k.access_token)throw Error("token failed");
const r=await (await fetch(`https://api.weixin.qq.com/cgi-bin/draft/update?access_token=${k.access_token}`,{method:"POST",body:JSON.stringify({media_id:id,index:0,articles:{title:m.title,author:"股市马斯克",digest:m.digest,content:c,content_source_url:"https://fundgz.ndxspx.cn/fund_estimator.html",thumb_media_id:t,pic_crop_235_1:"0_0_1_1",pic_crop_1_1:"0.024_0_0.449_1"}})})).json();if(r.errcode&&r.errcode!==0)throw Error(JSON.stringify(r));
console.log("草稿已更新 ✅ "+m.title);try{execFileSync("lark-cli",["im","+messages-send","--as","bot","--user-id","ou_1a823bbae5d58fe27620c8a6add2d891","--text",`📰 公众号草稿已更新：${m.title}\\n\\n摘要：${m.digest}\\n\\n请到公众号后台确认后发表`,`--idempotency-key`,`brief-update-${m.title}`],{env:{...process.env,HOME:"/home/ubuntu/lark-bot-qsmp"},stdio:"pipe"});console.log("QSMP 更新通知已发送 ✅")}catch(x){console.error("⚠️ 草稿已更新，但通知失败")}})().catch(x=>{console.error("草稿更新失败："+x.message);process.exit(1)});'''


def main():
    args = sys.argv[1:]
    update_id = None
    if args and args[0] == "--update":
        if len(args) != 3:
            sys.exit("用法：python3 publish_wechat.py --update YYYYMMDD MEDIA_ID")
        day, update_id = args[1], args[2]
    else:
        day = args[0] if args else date.today().strftime("%Y%m%d")
    src = BASE / f"wechat_article_{day}.html"
    if not src.exists():
        sys.exit(f"找不到 {src}，先运行 build_brief.py 生成当日文章")
    html = src.read_text()
    content_path = BASE / f"content_{day}.json"
    if not content_path.exists():
        sys.exit(f"找不到 {content_path}，无法执行发布前成稿终检")
    content_json = json.loads(content_path.read_text(encoding="utf-8"))
    structure_errors = validate_no_structure_leaks(content_json, html)
    if structure_errors:
        sys.exit("✗ 发布前正文结构泄漏校验失败：\n- " + "\n- ".join(structure_errors))
    emphasis_errors = validate_emphasis(content_json)
    if emphasis_errors:
        sys.exit("✗ 发布前强调色校验失败：\n- " + "\n- ".join(emphasis_errors))
    print(f"发布前强调色校验 ✅ {emphasis_summary(content_json)}")
    calendar_errors = validate_calendar(content_json)
    if calendar_errors:
        sys.exit("✗ 发布前本周财经日历校验失败：\n- " + "\n- ".join(calendar_errors))
    print(f"发布前本周财经日历 ✅ {calendar_summary(content_json)}")
    final_qa_status = validate_final_render(content_json, html)
    print(f"发布前成稿终检 ✅ {final_qa_status}")

    try:
        article = html.split('<div id="article">', 1)[1].rsplit("</div>", 1)[0]
    except IndexError:
        sys.exit('文章里没有 <div id="article">，检查 build_brief.py 输出格式是否变了')

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", article, re.S)
    if not h1:
        sys.exit("正文里没有 <h1> 两行标题，无法生成摘要")
    digest = "\n".join(
        t.strip() for t in re.sub(r"<[^>]+>", "\n", h1.group(1)).splitlines() if t.strip()
    )

    content = (
        (BASE / "wechat_fragments/header.html").read_text().strip()
        + article
        + (BASE / "wechat_fragments/footer_card.html").read_text().strip()
    )
    meta = {"title": f"全球财经简报｜{day}", "digest": digest}

    if ON_SERVER:
        # 本机即发布服务器：直接写 incoming 目录，本地执行 node，不做 ssh 回环。
        LOCAL_INCOMING.mkdir(parents=True, exist_ok=True)
        (LOCAL_INCOMING / "content.html").write_text(content, encoding="utf-8")
        (LOCAL_INCOMING / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "content.html").write_text(content)
            (Path(tmp) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
            subprocess.run(SSH + ["mkdir -p ~/wenyan-work/incoming"], check=True)
            subprocess.run(SCP + [f"{tmp}/content.html", f"{tmp}/meta.json", REMOTE], check=True)

    remote_cmd = ("node ~/wenyan-work/publish-brief.js" if not update_id
                  else "node -e %s %s" % (shlex.quote(UPDATE_SCRIPT), shlex.quote(update_id)))
    if ON_SERVER:
        r = subprocess.run(remote_cmd, shell=True, capture_output=True, text=True)
    else:
        r = subprocess.run(SSH + [remote_cmd], capture_output=True, text=True)
    print(r.stdout.strip())
    if r.returncode != 0:
        sys.exit(f"建稿失败：{r.stderr.strip()}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把抓取结果推到「你自己的静态托管」，得到一个前端可以直接 GET 的公开地址。

只跑在服务端，凭据只存在环境变量里，不写进 HTML、不写进仓库。

支持三种通道（由 PROMAX_PUSH 选择）：

1) PROMAX_PUSH=github   —— 推到 GitHub 仓库，前端读 raw.githubusercontent.com（该域返回 CORS: *）
   PROMAX_GH_TOKEN    必填，细粒度 PAT，只需 Contents: Read and write
   PROMAX_GH_REPO     必填，形如 owner/repo
   PROMAX_GH_PATH     选填，默认 cloud_draws.json
   PROMAX_GH_BRANCH   选填，默认 main
   读取地址：https://raw.githubusercontent.com/<repo>/<branch>/<path>

2) PROMAX_PUSH=http     —— 直接 PUT 到任意可写地址（对象存储预签名 URL、自建 Nginx WebDAV 等）
   PROMAX_PUSH_URL    必填，PUT 目标
   PROMAX_PUSH_TOKEN  选填，带 Authorization: Bearer <token>
   PROMAX_PUSH_HEADER 选填，自定义鉴权头，形如 "X-Auth: abc"
   PROMAX_PUSH_READ_URL 选填，读取地址（不填时沿用 PROMAX_PUSH_URL 去掉查询串）

3) PROMAX_PUSH=none（默认）—— 只落盘，不推送

用法：
  python push_cloud.py --file cloud_draws.json
  python push_cloud.py --file cloud_draws.json --dry-run
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "promax-cloud-sync/1.0"


def log(msg):
    sys.stderr.write("[push] %s\n" % msg)
    sys.stderr.flush()


def request(method, url, body=None, headers=None, timeout=25):
    data = None
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def push_github(payload, dry):
    token = os.environ.get("PROMAX_GH_TOKEN", "").strip()
    repo = os.environ.get("PROMAX_GH_REPO", "").strip()
    path = os.environ.get("PROMAX_GH_PATH", "cloud_draws.json").strip()
    branch = os.environ.get("PROMAX_GH_BRANCH", "main").strip()
    if not repo:
        raise SystemExit("缺少 PROMAX_GH_REPO（形如 owner/repo）")
    api = "https://api.github.com/repos/%s/contents/%s" % (repo, urllib.parse.quote(path))
    read_url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, path)

    if dry:
        log("[dry-run] 将 PUT %s?ref=%s（目标分支 %s）" % (api, branch, branch))
        log("[dry-run] 前端读取地址 %s" % read_url)
        return True, read_url

    if not token:
        raise SystemExit("缺少 PROMAX_GH_TOKEN（细粒度 PAT，Contents: Read and write）")

    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    status, body = request("GET", "%s?ref=%s" % (api, branch), headers=headers)
    sha = None
    if status == 200:
        try:
            sha = json.loads(body).get("sha")
        except Exception:  # noqa: BLE001
            sha = None
    elif status == 404:
        log("远端尚无该文件，将新建")
    else:
        log("读取远端文件失败（%s）：%s" % (status, body[:200]))

    commit = {
        "message": "chore(cloud_draws): sync %s" % payload.get("updated_at", ""),
        "content": base64.b64encode(
            (json.dumps(payload, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        ).decode("ascii"),
        "branch": branch,
    }
    if sha:
        commit["sha"] = sha
    status, body = request("PUT", api, body=commit, headers=headers, timeout=40)
    if status in (200, 201):
        log("已写入 GitHub：%s" % read_url)
        return True, read_url
    log("写入 GitHub 失败（%s）：%s" % (status, body[:300]))
    return False, read_url


def push_http(payload, dry):
    url = os.environ.get("PROMAX_PUSH_URL", "").strip()
    if not url:
        raise SystemExit("缺少 PROMAX_PUSH_URL")
    read_url = os.environ.get("PROMAX_PUSH_READ_URL", "").strip() or url.split("?")[0]
    token = os.environ.get("PROMAX_PUSH_TOKEN", "").strip()
    custom = os.environ.get("PROMAX_PUSH_HEADER", "").strip()

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if custom and ":" in custom:
        k, v = custom.split(":", 1)
        headers[k.strip()] = v.strip()

    if dry:
        log("[dry-run] 将 PUT %s（%d 字节）" % (url, len(json.dumps(payload, ensure_ascii=False))))
        log("[dry-run] 前端读取地址 %s" % read_url)
        return True, read_url

    body = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    status, text = request("PUT", url, body=body, headers=headers, timeout=40)
    if status in (200, 201, 202, 204):
        log("已推送：%s" % read_url)
        return True, read_url
    log("推送失败（%s）：%s" % (status, text[:300]))
    return False, read_url


def main():
    ap = argparse.ArgumentParser(description="把 cloud_draws.json 推到静态托管")
    ap.add_argument("--file", default="cloud_draws.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        log("找不到数据文件：%s" % args.file)
        return 1
    with open(args.file, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    mode = os.environ.get("PROMAX_PUSH", "none").strip().lower() or "none"
    if mode == "none":
        log("PROMAX_PUSH=none：只落盘，不推送。本地文件 %s（%d 期）"
            % (args.file, payload.get("count", 0)))
        return 0
    if mode == "github":
        ok, url = push_github(payload, args.dry_run)
    elif mode == "http":
        ok, url = push_http(payload, args.dry_run)
    else:
        log("未知 PROMAX_PUSH=%r（可选 github / http / none）" % mode)
        return 1

    print(json.dumps({"push": mode, "ok": ok, "read_url": url,
                      "count": payload.get("count"), "latest": payload.get("latest")},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
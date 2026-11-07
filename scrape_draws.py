#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""猪半仙 ProMax 云端开奖抓取器

只跑在服务端（定时任务 / 云主机 / 本地 cron），不进入 HTML 单文件，因此不受浏览器 CORS 限制。

抓取策略
  1) 首选源 A：https://www.777778.net/kjjson/livexam.json
     结构 {status, info, data:{period, numberList:[{number,color,shengXiao,wuXing}...], title}}
     numberList 前 6 位为平码，第 7 位为特码。
  2) 源 A 超时 / 报错 / 结构异常时，降级源 B：
     https://m.55128.cn/kjh/history_newam6hc.aspx（近 15 期）
     https://m.55128.cn/kjh/newam6hc-history-120.htm（近 120 期，用于补齐断档）
     结构：div.item > p.title-left > strong(2026311) 期号；div.kjh-wxs > span.kj-red|green|blue + p(生肖/五行)；
     平码与特码之间用 div.jiahaos("+") 分隔。
  3) 每个源失败自动重试 2 次（首次 + 2 次重试 = 3 次尝试），指数退避 2s / 4s。
     两源全失败时保留本地已有数据、状态记为 error、退出码 0，不阻断次日任务。

输出 standard JSON（前端只读这个文件，不做任何抓取）：
{
  "schema": "promax.draws/1",
  "updated_at": "2026-11-08T02:40:00+08:00",
  "status": "ok" | "partial" | "error",
  "source": "A" | "B" | "none",
  "latest": 2026311,
  "count": 311,
  "draws": [{"period": 2026311, "balls": [32,40,49,14,19,16], "special": 34, "recorded_at": "...", "source": "A"}]
}

用法：
  python scrape_draws.py                        # 抓最新一期，合并进 ./promax_draws.json
  python scrape_draws.py --out promax_draws.json # 指定输出
  python scrape_draws.py --backfill 50          # 顺带从源 B 的 120 期页补最多 50 期缺失
  python scrape_draws.py --source b             # 强制走源 B（自检降级通道用）
  python scrape_draws.py --print                # 只打印不落盘
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
# 可用环境变量覆盖（自检降级通道、或日后换源时不必改代码）
SRC_A = os.environ.get("PROMAX_SRC_A", "https://www.777778.net/kjjson/livexam.json")
SRC_B_LIST = os.environ.get("PROMAX_SRC_B", "https://m.55128.cn/kjh/history_newam6hc.aspx")
SRC_B_120 = os.environ.get("PROMAX_SRC_B_HIST", "https://m.55128.cn/kjh/newam6hc-history-120.htm")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UNIVERSE = 49
MAX_RETRY = 2
SCHEMA = "promax.draws/1"    # 只写开奖文件；字典 / 生肖 / 公式三文件由前端按 updated_at 缓存，脚本绝不触碰


def full_period(tail, year=None):
    """期号统一为 7 位纯数字：尾巴 311 + 年份 2026 -> 2026311。

    已经是 7 位（>= 1000000）时原样返回，避免重复拼接。
    年份优先取数据源自带的年份，缺失时取当前北京时间年份。
    """
    p = int(tail)
    if p >= 1000000:
        return p
    y = int(year) if year else datetime.now(CST).year
    return y * 1000 + p


def now_iso():
    return datetime.now(CST).replace(microsecond=0).isoformat()


def log(msg):
    sys.stderr.write("[scrape] %s\n" % msg)
    sys.stderr.flush()


def http_get(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def with_retry(fn, label):
    """首次 + MAX_RETRY 次重试；返回 (结果, 最后一次异常)。"""
    last = None
    for attempt in range(MAX_RETRY + 1):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 网络/解析异常统一降级
            last = exc
            if attempt < MAX_RETRY:
                backoff = 2 * (attempt + 1)
                log("%s 第 %d 次尝试失败：%s；%ds 后重试" % (label, attempt + 1, exc, backoff))
                time.sleep(backoff)
            else:
                log("%s 重试耗尽：%s" % (label, exc))
    return None, last


TITLE_RE = re.compile(
    r"第\s*(\d+)\s*期\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*(\d{1,2}):(\d{2})")


def draw_time_from_title(title, period, year=None):
    """源 A 的 title 形如「第312期 2026/11/08 21:30 星期日」。

    title 声明的是下一期时，本期开奖日 = 该日期前 1 天；声明的是本期时直接采用。
    期号按 7 位的尾三位比较，跨年（999 -> 001）同样成立。
    解析不出或期号对不上则返回 None（调用方回落为抓取时间，不做猜测）。
    """
    m = TITLE_RE.search(title or "")
    if not m:
        return None
    declared, year, month, day, hour, minute = m.groups()
    declared = int(declared)
    try:
        stamp = datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=CST)
    except ValueError:
        return None
    tail = int(period) % 1000
    if declared == (tail % 999) + 1:
        return (stamp - timedelta(days=1)).replace(microsecond=0).isoformat()
    if declared == tail:
        return stamp.replace(microsecond=0).isoformat()
    return None


def normalize(period, balls, special, source, recorded_at=None):
    """校验并组装一条标准开奖。"""
    period = int(period)
    balls = [int(b) for b in balls]
    special = int(special)
    if period < 1000000:
        raise ValueError("期号必须为 7 位纯数字（如 2026311）：%r" % period)
    if len(balls) != 6:
        raise ValueError("平码数量应为 6，实际 %d" % len(balls))
    pool = balls + [special]
    if len(set(pool)) != 7:
        raise ValueError("7 个号码存在重复：%r" % pool)
    if any(n < 1 or n > UNIVERSE for n in pool):
        raise ValueError("号码越界(01-%02d)：%r" % (UNIVERSE, pool))
    return {
        "period": period,
        "balls": balls,
        "special": special,
        "recorded_at": recorded_at or now_iso(),
        "source": source,
    }


def fetch_a():
    doc = json.loads(http_get(SRC_A, timeout=15))
    if str(doc.get("status")) != "0":
        raise ValueError("源 A 状态异常：status=%r info=%r" % (doc.get("status"), doc.get("info")))
    data = doc.get("data") or {}
    raw_period = data.get("period")
    year = data.get("year")
    rows = data.get("numberList") or []
    nums = [int(r.get("number")) for r in rows if r.get("number") is not None]
    if raw_period is None or len(nums) != 7:
        raise ValueError("源 A 结构不符：period=%r 号码数=%d" % (raw_period, len(nums)))
    period = full_period(raw_period, year)
    stamp = draw_time_from_title(data.get("title"), period, year)
    return normalize(period, nums[:6], nums[6], "A", stamp)


ITEM_SPLIT = re.compile(r'<div class="item">')
PERIOD_RE = re.compile(r"<strong>\s*(\d{5,9})\s*</strong>")
NUM_RE = re.compile(r'kj-(?:green|red|blue)"\s*>\s*(\d{1,2})\s*<')


def parse_b_html(html):
    """解析源 B 列表页，返回按期号从新到旧的开奖列表。"""
    if "kj-detailss" not in html:
        raise ValueError("源 B 页面结构不符（缺少 kj-detailss）")
    chunks = ITEM_SPLIT.split(html)
    draws = []
    for chunk in chunks[1:]:
        m = PERIOD_RE.search(chunk)
        if not m:
            continue
        raw_period = int(m.group(1))
        period = full_period(raw_period)
        body = chunk[chunk.find("kj-detailss"):]
        head, sep, tail = body.partition("jiahaos")
        balls = [int(x) for x in NUM_RE.findall(head)]
        specials = [int(x) for x in NUM_RE.findall(tail)] if sep else []
        if len(balls) != 6 or len(specials) != 1:
            log("跳过期号 %s：平码 %d 个、特码 %d 个" % (period, len(balls), len(specials)))
            continue
        try:
            draws.append(normalize(period, balls, specials[0], "B"))
        except ValueError as exc:
            log("跳过期号 %s：%s" % (period, exc))
    if not draws:
        raise ValueError("源 B 未解析出任何开奖")
    draws.sort(key=lambda d: d["period"], reverse=True)
    return draws


def fetch_b(url=SRC_B_LIST):
    return parse_b_html(http_get(url, timeout=25))


def fetch_seed(url):
    """拉取云端现值的 draws 列表（接受信封或裸数组）。"""
    doc = json.loads(http_get(url, timeout=25))
    rows = doc.get("draws") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise ValueError("云端现值结构不符")
    out = []
    for row in rows:
        try:
            out.append({
                "period": int(row["period"]),
                "balls": [int(b) for b in row["balls"]],
                "special": int(row["special"]),
                "recorded_at": row.get("recorded_at") or "",
                "source": row.get("source") or "seed",
            })
        except Exception:  # noqa: BLE001
            continue
    if not out:
        raise ValueError("云端现值无有效记录")
    return out


def load_existing(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:  # noqa: BLE001 坏文件不应阻断抓取
        log("本地文件无法解析，按空库处理：%s" % exc)
        return {}
    rows = doc.get("draws") if isinstance(doc, dict) else doc
    out = {}
    for row in rows or []:
        try:
            out[int(row["period"])] = {
                "period": int(row["period"]),
                "balls": [int(b) for b in row["balls"]],
                "special": int(row["special"]),
                "recorded_at": row.get("recorded_at") or "",
                "source": row.get("source") or "local",
            }
        except Exception:  # noqa: BLE001 单条坏数据跳过
            continue
    return out


def merge(store, incoming):
    """按期号去重：已存在的期号不覆盖（ON CONFLICT DO NOTHING），返回新增列表。"""
    added = []
    for draw in incoming:
        p = draw["period"]
        if p in store:
            continue
        store[p] = draw
        added.append(p)
    return added


def write_output(path, store, status, source):
    periods = sorted(store)
    doc = {
        "schema": SCHEMA,
        "updated_at": now_iso(),
        "status": status,
        "source": source,
        "latest": periods[-1] if periods else None,
        "count": len(periods),
        "draws": [store[p] for p in periods],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    return doc


def main():
    ap = argparse.ArgumentParser(description="猪半仙 ProMax 开奖抓取器")
    ap.add_argument("--out", default="promax_draws.json")
    ap.add_argument("--seed", default=os.environ.get("PROMAX_SEED_URL", ""),
                    help="空库时先拉取的云端现值 URL（避免把完整历史覆盖成单期）")
    ap.add_argument("--backfill", type=int, default=0, help="从源 B 的 120 期页补最多 N 期缺失")
    ap.add_argument("--source", choices=["auto", "a", "b"], default="auto")
    ap.add_argument("--print", action="store_true", dest="dry", help="只打印不落盘")
    args = ap.parse_args()

    store = load_existing(args.out)
    if store:
        log("本地已有 %d 期，最新 %d" % (len(store), max(store)))
    elif args.seed:
        # 云端每次都是全新容器：先取仓库现值当基线，再增量合并
        seed_url = args.seed + ("&" if "?" in args.seed else "?") + "cb=" + str(int(time.time()))
        log("本地无基线，先拉云端现值：%s" % args.seed)
        seeded, err = with_retry(lambda: fetch_seed(seed_url), "云端现值")
        if seeded:
            for d in seeded:
                store[d["period"]] = d
            log("云端现值 %d 期，最新 %d" % (len(store), max(store)))
        else:
            log("云端现值不可用：%s（将以空库起步，只写入本期）" % err)

    newest = None
    used = "none"

    if args.source in ("auto", "a"):
        log("请求源 A：%s" % SRC_A)
        got, err = with_retry(fetch_a, "源 A")
        if got:
            newest = got
            used = "A"
            log("源 A 命中：%d 期 · %s + %02d" % (got["period"], got["balls"], got["special"]))
        else:
            log("源 A 不可用，降级源 B：%s" % err)

    if newest is None:
        log("请求源 B：%s" % SRC_B_LIST)
        got, err = with_retry(fetch_b, "源 B")
        if got:
            newest = got[0]
            used = "B"
            log("源 B 命中：%d 期 · %s + %02d（列表共 %d 期）"
                % (newest["period"], newest["balls"], newest["special"], len(got)))
        else:
            log("源 B 也不可用：%s" % err)

    incoming = [newest] if newest else []

    if args.backfill > 0:
        log("补齐断档：请求源 B 120 期页（上限 %d 期）" % args.backfill)
        hist, err = with_retry(lambda: fetch_b(SRC_B_120), "源 B 历史")
        if hist:
            missing = [d for d in hist if d["period"] not in store]
            missing.sort(key=lambda d: d["period"], reverse=True)
            incoming.extend(missing[: args.backfill])
            log("历史页 %d 期，其中缺失 %d 期，本次补 %d 期"
                % (len(hist), len(missing), min(len(missing), args.backfill)))
        else:
            log("历史页不可用，跳过补齐：%s" % err)

    added = merge(store, incoming)

    if newest:
        status = "ok" if added else "no-new"
    else:
        status = "error"

    if args.dry:
        print(json.dumps({"status": status, "source": used, "added": added,
                          "count": len(store), "latest": max(store) if store else None},
                         ensure_ascii=False))
        return 0

    doc = write_output(args.out, store, status, used)
    print(json.dumps({"status": doc["status"], "source": doc["source"], "added": added,
                      "count": doc["count"], "latest": doc["latest"],
                      "updated_at": doc["updated_at"]}, ensure_ascii=False))

    if status == "error":
        log("两源全部失败：保留本地 %d 期数据，本次不更新，次日任务照常执行" % doc["count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env node
/**
 * 香港六合彩开奖同步任务（独立数据流）
 * 源：https://m.55128.cn/kjh/hk6-history-15.htm
 * 目标：promax_hk_draws.json（只写这个文件，绝不触碰 promax_draws.json）
 *
 * 用法：
 *   node hk-draws-sync.js                               # 正常抓取并推送
 *   node hk-draws-sync.js --dry-run                     # 只打印，不写 GitHub
 *   node hk-draws-sync.js --offline a.html --dry-run    # 用本地 HTML 调试
 *   node hk-draws-sync.js --offline a.html --existing b.json --dry-run  # 用本地文件当基线
 *
 * 环境变量：GH_DATA_TOKEN（推荐 Fine-grained PAT，仅 Contents: RW）或 GITHUB_TOKEN
 */
'use strict';

const fs = require('fs');

const OWNER = process.env.GH_OWNER || 'zjz520cc';
const REPO = process.env.GH_REPO || 'zhubanxian-data';
const BRANCH = process.env.GH_BRANCH || 'main';
const PATH_HK = 'promax_hk_draws.json';
const PATH_MO = 'promax_draws.json';               // 仅用于断言：本脚本永不读写它
const SOURCE = 'https://m.55128.cn/kjh/hk6-history-15.htm';
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

const args = process.argv.slice(2);
const DRY = args.includes('--dry-run');
const OFFLINE = (() => { const i = args.indexOf('--offline'); return i >= 0 ? args[i + 1] : ''; })();
const EXISTING = (() => { const i = args.indexOf('--existing'); return i >= 0 ? args[i + 1] : ''; })();
const TOKEN = process.env.GH_DATA_TOKEN || process.env.GITHUB_TOKEN || '';

/* ===== 隔离红线：目标文件必须带 _hk_ 且不是澳门文件 ===== */
if (!PATH_HK.endsWith('_hk_draws.json')) throw new Error('隔离红线：目标文件必须是 *_hk_draws.json');
if (PATH_HK === PATH_MO) throw new Error('隔离红线：禁止写入澳门 promax_draws.json');

/* ---------- 工具 ---------- */
const pad2 = (n) => (n < 10 ? '0' + n : String(n));
const fmt = (d) => d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
const isDrawDay = (d) => [2, 4, 6].includes(d.getUTCDay());   // 周二/四/六
function parseYmd(s) {
  const m = /(\d{4})-(\d{1,2})-(\d{1,2})/.exec(s || '');
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
}
const nextDraw = (d) => { const x = new Date(d.getTime()); for (let i = 1; i <= 10; i++) { x.setUTCDate(x.getUTCDate() + 1); if (isDrawDay(x)) return new Date(x.getTime()); } return x; };
const prevDraw = (d) => { const x = new Date(d.getTime()); for (let i = 1; i <= 10; i++) { x.setUTCDate(x.getUTCDate() - 1); if (isDrawDay(x)) return new Date(x.getTime()); } return x; };
const lastDrawOnOrBefore = (d) => (isDrawDay(d) ? new Date(d.getTime()) : prevDraw(d));

const ZOD = ['马', '蛇', '龙', '兔', '虎', '牛', '鼠', '猪', '狗', '鸡', '猴', '羊'];
const YEAR_ZOD = { 2020: '鼠', 2021: '牛', 2022: '虎', 2023: '兔', 2024: '龙', 2025: '蛇', 2026: '马', 2027: '羊' };
function zodiacOf(n, year) {
  const base = ZOD.indexOf(YEAR_ZOD[year] || '');
  return base < 0 ? '' : ZOD[(base + ((n - 1) % 12)) % 12];
}

/* ---------- 解析 55128 页面 ---------- */
function parse55128(html) {
  const out = [];
  const chunks = String(html || '').split('<div class="item">').slice(1);
  for (const chunk of chunks) {
    const pm = /<strong>(\d{7})<\/strong>/.exec(chunk);
    if (!pm) continue;
    const period = Number(pm[1]);
    const year = Math.floor(period / 1000);
    const balls = [];
    const re = /class="kj-(red|blue|green)"\s*>\s*(\d{1,2})\s*<\/span>\s*<p>(<sup><</sup>*)<\/p>/g;
    let m;
    while ((m = re.exec(chunk))) {
      const n = Number(m[2]);
      if (n >= 1 && n <= 49) balls.push({ n, color: m[1][0].toUpperCase(), label: (m[3] || '').split('/')[0].trim() });
    }
    if (balls.length !== 7) continue;                    // 6 平 + 1 特
    out.push({
      period,
      balls: balls.slice(0, 6).map((b) => b.n),
      special: balls[6].n,
      colors: balls.map((b) => b.color),
      zodiacs: balls.map((b) => zodiacOf(b.n, year)),
      source_zodiacs: balls.map((b) => b.label),
    });
  }
  const map = new Map();
  out.forEach((r) => map.set(r.period, r));
  return [...map.values()].sort((a, b) => b.period - a.period);
}

/* ---------- GitHub Contents API ---------- */
function ghHeaders() {
  if (!TOKEN) throw new Error('缺少 GH_DATA_TOKEN / GITHUB_TOKEN');
  return {
    Authorization: 'Bearer ' + TOKEN,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'hk-draws-sync',
  };
}
const api = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${PATH_HK}`;

async function readRemote() {
  const res = await fetch(api + '?ref=' + BRANCH + '&_=' + Date.now(), { headers: ghHeaders(), cache: 'no-store' });
  if (res.status === 404) return { exists: false, sha: null, doc: null };
  if (!res.ok) throw new Error('读取远端失败 HTTP ' + res.status);
  const j = await res.json();
  let doc = null;
  try { doc = JSON.parse(Buffer.from(j.content || '', 'base64').toString('utf8')); } catch (e) { doc = null; }
  return { exists: true, sha: j.sha, doc };
}
async function writeRemote(doc, sha, message) {
  const body = {
    message,
    content: Buffer.from(JSON.stringify(doc, null, 2) + '\n', 'utf8').toString('base64'),
    branch: BRANCH,
    committer: { name: 'hk-draws-bot', email: 'hk-draws-bot@users.noreply.github.com' },
  };
  if (sha) body.sha = sha;
  return fetch(api, {
    method: 'PUT',
    headers: { ...ghHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/* ---------- 权威日期补全（可选，失败不影响主流程） ---------- */
const DATE_SRC = 'https://2026kj.zkclhb.com:2026/hk.html';
async function fetchDateMap() {
  try {
    const res = await fetch(DATE_SRC + '?_=' + Date.now(), { headers: { 'User-Agent': UA } });
    if (!res.ok) return {};
    const html = await res.text();
    const map = {}, re = /<dt><b>(\d+)<\/b><sup><</sup>*\((?:开奖时间:)?(\d{4}-\d{1,2}-\d{1,2})\)<\/dt>/g;
    let m;
    while ((m = re.exec(html))) map[Number(m[1])] = m[2];
    return map;
  } catch (e) { return {}; }
}
let DATE_MAP = {};

/* ---------- 合并 ---------- */
function mergeDraws(existing, parsed) {
  const byPeriod = new Map();
  (existing || []).forEach((r) => { if (r && r.period) byPeriod.set(Number(r.period), r); });
  const maxExisting = Math.max(0, ...byPeriod.keys());
  const fresh = parsed.filter((r) => r.period > maxExisting).sort((a, b) => a.period - b.period);
  if (!fresh.length) return { draws: [...byPeriod.values()].sort((a, b) => b.period - a.period), added: 0 };

  const known = [...byPeriod.values()].filter((r) => r.date).sort((a, b) => a.period - b.period);
  let anchor = known.length ? parseYmd(known[known.length - 1].date) : null;
  let out = [];

  if (anchor) {
    let d = anchor;
    for (const r of fresh) {
      const real = DATE_MAP[r.period % 1000] || DATE_MAP[r.period];
      if (real) { d = parseYmd(real) || d; out.push({ ...r, date: real, date_estimated: false }); }
      else { d = nextDraw(d); out.push({ ...r, date: fmt(d), date_estimated: true }); }
    }
  } else {
    // 首次运行：最新一期定位到“今天或之前最近的二/四/六”，其余向前回推
    let d = lastDrawOnOrBefore(new Date());
    out = [{ ...fresh[fresh.length - 1], date: fmt(d), date_estimated: true }];
    for (let i = fresh.length - 2; i >= 0; i--) { d = prevDraw(d); out.unshift({ ...fresh[i], date: fmt(d), date_estimated: true }); }
  }
  out.forEach((r) => byPeriod.set(r.period, r));
  return { draws: [...byPeriod.values()].sort((a, b) => b.period - a.period), added: out.length };
}

/* ---------- 主流程 ---------- */
(async () => {
  let html;
  if (OFFLINE) { html = fs.readFileSync(OFFLINE, 'utf8'); console.log('[offline] ' + OFFLINE); }
  else {
    const res = await fetch(SOURCE + '?_=' + Date.now(), { headers: { 'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9' } });
    if (!res.ok) throw new Error('抓取源页失败 HTTP ' + res.status);
    html = await res.text();
  }

  const parsed = parse55128(html);
  if (!parsed.length) throw new Error('解析不到任何一期，页面结构可能已变，请检查选择器');
  console.log(`解析到 ${parsed.length} 期，最新 ${parsed[0].period}（${parsed[0].balls.join(',')} + ${parsed[0].special}）`);

  let remote = { exists: false, sha: null, doc: null };
  if (EXISTING) {
    remote = { exists: true, sha: null, doc: JSON.parse(fs.readFileSync(EXISTING, 'utf8')) };
    console.log('[dry] 基线=' + EXISTING);
  } else {
    try { remote = await readRemote(); } catch (e) { console.warn('读取远端失败（按新建处理）：' + e.message); }
  }
  const existing = remote.doc ? (Array.isArray(remote.doc) ? remote.doc : remote.doc.draws || []) : [];

  DATE_MAP = OFFLINE ? {} : await fetchDateMap();
  console.log('权威日期表：' + Object.keys(DATE_MAP).length + ' 条' + (Object.keys(DATE_MAP).length ? '' : '（不可用，按日程推定）'));

  const { draws, added } = mergeDraws(existing, parsed);
  const doc = { version: 1, lottery: 'hk', updated_at: new Date().toISOString(), count: draws.length, latest: draws.length ? draws[0].period : null, draws };
  console.log(`合并结果：新增 ${added} 期，总 ${draws.length} 期，最新 ${doc.latest}`);

  if (DRY) { console.log('dry-run 不推送；抽样 ' + JSON.stringify(doc.draws[0])); return; }
  if (!added) { console.log('无新增期数，跳过提交（不产生空提交）'); return; }

  const message = `chore(hk): 更新香港开奖 +${added} 期 · 最新 ${doc.latest}`;
  let res = await writeRemote(doc, remote.sha, message);
  if (res.status === 409 || res.status === 422) {                     // 并发冲突：重取 SHA 再写一次
    const again = await readRemote();
    res = await writeRemote(doc, again.sha, message);
  }
  if (!res.ok) throw new Error('推送失败 HTTP ' + res.status + ' ' + (await res.text()).slice(0, 240));
  console.log('已推送 promax_hk_draws.json：' + message);
})();

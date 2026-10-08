/**
 * 用网易云客户端的真实搜索，抓取候选歌曲，生成 UI 演示 fixture。
 *
 * 前置：客户端需带调试参数运行（--remote-debugging-port=9222）
 *
 * 抓取方式与设计文档 §7.2 一致：
 *   1. 往搜索框写关键词（原生 setter + input 事件）+ 真实 Enter
 *   2. 从结果行的 React fiber props 里读结构化 track 对象
 *   3. 艺人 / 标签 / 番剧标注 从 DOM 补充（fiber 的 track 里没有艺人）
 *
 * 用法： node tools/fetch_mock_candidates.js
 */

const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = 9222;
const WS_ROOT = path.resolve(__dirname, "..");
const OUT = path.join(WS_ROOT, "data", "mock", "netease_candidates.json");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 每个查询：关键词 + 我们期望匹配到的目标（用于演示打分） */
const QUERIES = [
  { anime: "冰之城墙 第二季", kind: "OP", query: "Escort THE BEAT GARDEN" },
  { anime: "冰之城墙 第二季", kind: "ED", query: "Hanakage Superfly" },
  { anime: "FX战士久留美", kind: "OP", query: "FX Senshi Kurumi-chan Aina Suzuki" },
  { anime: "FX战士久留美", kind: "ED", query: "Aina Suzuki FX" },
  { anime: "名侦探光之美少女！", kind: "OP", query: "名探偵プリキュア OP" },
];

function getJSON(p) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port: PORT, path: p }, (res) => {
        let b = "";
        res.on("data", (c) => (b += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(b));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject);
  });
}

const EXTRACT_JS = `
(function () {
  function propsOf(el) {
    if (!el) return null;
    var k = Object.keys(el).find(function (k) { return k.indexOf('__reactEventHandlers$') === 0; });
    return k ? el[k] : null;
  }
  function instOf(el) {
    if (!el) return null;
    var k = Object.keys(el).find(function (k) { return k.indexOf('__reactInternalInstance$') === 0; });
    return k ? el[k] : null;
  }
  function trackOf(row) {
    var f = instOf(row), n = 0;
    while (f && n < 12) {
      if (f.memoizedProps && f.memoizedProps.track) return f.memoizedProps.track;
      f = f.return; n++;
    }
    return null;
  }
  var rows = [].slice.call(document.querySelectorAll('[data-log*="cell_pc_common_songcard"]'));
  return JSON.stringify(rows.slice(0, 8).map(function (row) {
    var t = trackOf(row) || {};
    var songId = '';
    try { songId = JSON.parse(row.getAttribute('data-log') || '{}').params.s_songId || ''; } catch (e) {}
    var artists = [].slice.call(row.querySelectorAll('.cmd-typography.artist'))
      .map(function (e) { return (e.textContent || '').trim(); })
      .filter(Boolean).join(' / ');
    var tags = [].slice.call(row.querySelectorAll('[aria-label^="Tag: "]'))
      .map(function (e) { return (e.getAttribute('aria-label') || '').replace('Tag: ', '').trim(); })
      .filter(Boolean);
    var cover = row.querySelector('img.cmd-image');
    return {
      netease_song_id: String(t.id || songId || ''),
      name: t.name || (row.querySelector('.cmd-typography.header') || {}).textContent || '',
      artists: artists,
      duration_ms: t.duration || null,
      tags: tags,
      anime_alias: (t.alias && t.alias[0]) || null,
      fee: t.fee,
      cover: cover ? cover.getAttribute('src') : null,
    };
  }).filter(function (x) { return x.netease_song_id; }));
})()
`;

(async () => {
  const targets = await getJSON("/json/list");
  const tab = targets.find((t) => t.type === "page");
  if (!tab) {
    console.error("找不到客户端页面，请确认客户端已带 --remote-debugging-port=9222 运行");
    process.exit(1);
  }

  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  const call = (method, params = {}) =>
    new Promise((r) => {
      const id = ++seq;
      pending.set(id, r);
      ws.send(JSON.stringify({ id, method, params }));
    });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m);
      pending.delete(m.id);
    }
  };
  await new Promise((r) => (ws.onopen = r));

  const ev = async (expr) => {
    const r = await call("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) return null;
    return r.result && r.result.result ? r.result.result.value : null;
  };

  await call("Page.enable");
  const out = [];

  for (const q of QUERIES) {
    process.stdout.write(`搜索「${q.query}」... `);
    await ev(`(function(){
      var i = document.querySelector('input[type=search]');
      if (!i) return 'no input';
      i.focus(); i.click();
      var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(i, ${JSON.stringify(q.query)});
      i.dispatchEvent(new Event('input', { bubbles: true }));
      return i.value;
    })()`);
    await sleep(500);
    for (const type of ["rawKeyDown", "char", "keyUp"]) {
      await call("Input.dispatchKeyEvent", {
        type, key: "Enter", code: "Enter",
        windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
        text: type === "char" ? "\r" : undefined,
      });
    }
    await sleep(3200);

    const raw = await ev(EXTRACT_JS);
    let cands = [];
    try {
      cands = JSON.parse(raw || "[]");
    } catch (e) {}
    console.log(`${cands.length} 条候选`);

    out.push({ ...q, candidates: cands });
  }

  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(out, null, 2), "utf-8");
  console.log(`\n已保存 ${out.length} 组候选到 ${OUT}`);

  for (const g of out) {
    console.log(`\n【${g.anime} ${g.kind}】query="${g.query}"`);
    for (const c of g.candidates.slice(0, 5)) {
      console.log(
        `   ${String(c.netease_song_id).padEnd(12)} ${String(c.name).slice(0, 34).padEnd(36)} ` +
          `${String(c.artists).slice(0, 20).padEnd(22)} ${c.duration_ms || 0}ms  [${c.tags.join(",")}]  ${c.anime_alias || ""}`
      );
    }
  }

  ws.close();
  process.exit(0);
})().catch((e) => {
  console.error("异常:", e.message);
  process.exit(1);
});

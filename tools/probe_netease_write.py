#!/usr/bin/env python
"""迭代式探针：把「写入该 dispatch 什么」从**推测**变成**记录下来的事实**。

为什么要单独一个可迭代的工具：`desktop_cdp._JS_WRITE_PROBE_BODY` 是入库代码，
每改一次 JS 都要跑全量测试。而"搞清楚客户端真实的 action 长什么样"这件事
本质上是**探索**，需要改了立刻看输出。所以探索在一个脚本里做完，
结论再folded回 `write_path.py` 与 `desktop_cdp.py`。

**只读**：本脚本从不 dispatch 任何会改状态的 action。
唯一会"写"的是 `--install-recorder`，它把 `store.dispatch` 包一层**记录器**
（记录调用，不改变行为）—— 那是内存里的，关掉客户端即消失。

用法：
    python tools/probe_netease_write.py shape        # data/menu 形状（默认）
    python tools/probe_netease_write.py resources    # 全树扫 resourceType 分布
    python tools/probe_netease_write.py handlers     # click/onClick/dawn 是什么
    python tools/probe_netease_write.py actions      # 在 webpack 里找 action 类型常量
    python tools/probe_netease_write.py install      # 装 dispatch 记录器
    python tools/probe_netease_write.py dump         # 读记录器抓到的 action
    python tools/probe_netease_write.py uninstall    # 卸掉记录器
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "src"), os.path.join(_ROOT, "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from animusic import constants  # noqa: E402
from animusic.cdp import CDPBrowser, CDPError  # noqa: E402

_PRELUDE = r"""
var W = window;
var CACHE = '__animusic_dcdp_store__';
function pfx(el, p) {
  var out = [], ks, i;
  if (!el) return out;
  try { ks = Object.keys(el); } catch (e) { return out; }
  for (i = 0; i < ks.length; i++) { if (ks[i].indexOf(p) === 0) out.push(ks[i]); }
  return out;
}
function fiberEntry(el) {
  if (!el) return null;
  var k = pfx(el, '__reactInternalInstance$');
  if (k.length) return { f: el[k[0]], key: k[0] };
  k = pfx(el, '__reactFiber$');
  if (k.length) return { f: el[k[0]], key: k[0] };
  return null;
}
function rootEntry() {
  var el = document.getElementById('root') || document.body;
  if (!el) return null;
  var e = fiberEntry(el);
  if (e) return { f: e.f, key: e.key, root: 'element' };
  var r = el._reactRootContainer;
  if (r && r._internalRoot && r._internalRoot.current) {
    return { f: r._internalRoot.current, key: '_reactRootContainer', root: 'legacy' };
  }
  if (r && r.current) return { f: r.current, key: '_reactRootContainer', root: 'legacy' };
  return null;
}
function isStore(o) {
  return !!o && typeof o.getState === 'function' && typeof o.dispatch === 'function'
    && typeof o.subscribe === 'function';
}
function pick(o) {
  if (!o || typeof o !== 'object') return null;
  if (isStore(o)) return o;
  if (isStore(o.store)) return o.store;
  if (isStore(o._store)) return o._store;
  return null;
}
function fromDeps(f) {
  var d = f.dependencies, c, v;
  while (d) {
    c = d.firstContext;
    while (c) { v = pick(c.memoizedValue); if (v) return v; c = c.next; }
    d = d.next;
  }
  return null;
}
function fromNode(f) {
  if (!f) return null;
  var s = f.stateNode, v;
  v = pick(f.memoizedProps); if (v) return v;
  if (s && s.context) { v = pick(s.context.store); if (v) return v; }
  v = pick(s); if (v) return v;
  v = pick(f.memoizedState); if (v) return v;
  return fromDeps(f);
}
function findStore() {
  var c = null;
  try { c = W[CACHE]; } catch (e) {}
  if (c && isStore(c.store)) return c.store;
  var re = rootEntry();
  if (re) {
    var n = re.f, g = 0, s;
    while (n && g++ < 400) { s = fromNode(n); if (s) return s; n = n.return; }
    var top = re.f;
    while (top && top.return) top = top.return;
    var stack = [top], cnt = 0, x;
    while (stack.length && cnt++ < 40000) {
      x = stack.pop();
      s = fromNode(x);
      if (s) return s;
      if (x.child) stack.push(x.child);
      if (x.sibling) stack.push(x.sibling);
    }
  }
  try {
    var nd = W.NE_DAWN;
    if (nd && typeof nd.getStore === 'function') { s = pick(nd.getStore()); if (s) return s; }
    if (nd && typeof nd.getState === 'function' && typeof nd.dispatch === 'function') {
      return { getState: function () { return nd.getState(); }, dispatch: nd.dispatch,
               subscribe: function () { return function () {}; } };
    }
  } catch (e2) {}
  return null;
}
function isArr(v) { return Object.prototype.toString.call(v) === '[object Array]'; }
function prim(v) {
  if (v === null) return 'null';
  if (v === undefined) return 'undefined';
  var t = typeof v;
  if (Object.prototype.toString.call(v) === '[object Array]') return 'array';
  return t;
}
function preview(v) {
  var t = typeof v;
  if (t === 'string') return v.length > 120 ? v.slice(0, 120) + '…' : v;
  if (t === 'number' || t === 'boolean') return v;
  return null;
}
function shape(v, depth, cap) {
  cap = cap || 14;
  var o = { type: prim(v) }, ks = [], i, k, sub;
  if (depth < 0) return o;
  if (o.type === 'array') {
    try { o.len = v.length; if (v.length) o.item0 = shape(v[0], depth - 1, cap); } catch (e) {}
    return o;
  }
  if (o.type === 'object') {
    try { ks = Object.keys(v); } catch (e2) { ks = []; }
    o.keys = ks.slice(0, cap);
    if (ks.length > cap) o.more = ks.length - cap;
    o.values = {};
    for (i = 0; i < o.keys.length; i++) {
      k = o.keys[i];
      try { sub = v[k]; } catch (e3) { continue; }
      o.values[k] = { type: prim(sub), preview: preview(sub) };
      if (o.values[k].type === 'array') { try { o.values[k].len = sub.length; } catch (e4) {} }
      if (o.values[k].type === 'object' && depth > 0) {
        o.values[k].shape = shape(sub, depth - 1, Math.min(cap, 10));
      }
    }
    return o;
  }
  o.preview = preview(v);
  if (o.type === 'function') { try { o.src = String(v).slice(0, 400); } catch (e5) {} }
  return o;
}
function walkFibers(cb, limit) {
  var re = rootEntry();
  if (!re) return 0;
  var top = re.f;
  while (top && top.return) top = top.return;
  var stack = [top], n = 0, x, hit = 0;
  while (stack.length && n++ < 60000) {
    x = stack.pop();
    if (x && cb(x, n)) hit++;
    if (hit >= (limit || 1e9)) break;
    if (x && x.child) stack.push(x.child);
    if (x && x.sibling) stack.push(x.sibling);
  }
  return hit;
}
function typeName(f) {
  try {
    var t = f.type || f.elementType;
    if (typeof t === 'string') return t;
    if (t && t.displayName) return t.displayName;
    if (t && t.name) return t.name;
  } catch (e) {}
  return null;
}
function json(o) { return JSON.stringify(o); }
"""

_BODY_SHAPE = r"""
var out = { wrappers: [], resourceTypes: {}, errors: [] };
walkFibers(function (f) {
  var p = f && f.memoizedProps;
  if (!p || typeof p !== 'object') return false;
  var d = p.data;
  if (!d || typeof d !== 'object' || isArr(d)) return false;
  var rt = d.resourceType;
  if (typeof rt === 'string') { out.resourceTypes[rt] = (out.resourceTypes[rt] || 0) + 1; }
  if (out.wrappers.length >= 3) return false;
  out.wrappers.push({
    component: typeName(f),
    hasDispatch: typeof p.dispatch === 'function',
    propsKeys: Object.keys(p).slice(0, 30),
    menuType: prim(p.menu), menuPreview: preview(p.menu),
    dataKeys: Object.keys(d),
    resourceType: rt,
    resourceKeys: (d.resource && typeof d.resource === 'object')
      ? Object.keys(d.resource).slice(0, 40) : null,
    resourcePreview: {
      id: (d.resource || {}).id, name: (d.resource || {}).name,
      songName: (d.resource || {}).songName, ar: (d.resource || {}).ar,
      al: (d.resource || {}).al, dt: (d.resource || {}).dt
    },
    from: shape(d.from, 2, 12),
    resourceShape: shape(d.resource, 1, 40)
  });
  return true;
}, 3);
out.storeFound = !!findStore();
return json(out);
"""

_BODY_RESOURCES = r"""
var counts = {}, samples = {}, n = 0;
walkFibers(function (f) {
  var p = f && f.memoizedProps;
  if (!p || typeof p !== 'object') return false;
  var d = p.data;
  if (!d || typeof d !== 'object' || isArr(d)) return false;
  var rt = String(d.resourceType);
  counts[rt] = (counts[rt] || 0) + 1;
  if (!samples[rt] && d.resource && typeof d.resource === 'object') {
    samples[rt] = { component: typeName(f), keys: Object.keys(d.resource).slice(0, 40),
                    id: d.resource.id, name: d.resource.name,
                    songName: d.resource.songName };
  }
  n++;
  return false;
}, 1e9);
return json({ total: n, counts: counts, samples: samples });
"""

_BODY_HANDLERS = r"""
var out = { wrappers: [] };
walkFibers(function (f) {
  var p = f && f.memoizedProps;
  if (!p || typeof p !== 'object') return false;
  if (!p.data || typeof p.data !== 'object') return false;
  if (out.wrappers.length >= 2) return false;
  out.wrappers.push({
    component: typeName(f),
    dawn: { type: prim(p.dawn), preview: preview(p.dawn),
            keys: (p.dawn && typeof p.dawn === 'object')
              ? Object.keys(p.dawn).slice(0, 40) : null },
    _nk: { type: prim(p._nk), preview: preview(p._nk),
           keys: (p._nk && typeof p._nk === 'object')
             ? Object.keys(p._nk).slice(0, 30) : null },
    click: shape(p.click, 0, 8),
    onClick: shape(p.onClick, 0, 8),
    dispatchSrc: (typeof p.dispatch === 'function')
      ? String(p.dispatch).slice(0, 300) : null,
    fiberPropsKeys: Object.keys(p).slice(0, 30)
  });
  return true;
}, 2);
// 记录器能挂在哪：把树上所有带 dispatch 的地方列出来
var hooks = [];
walkFibers(function (f) {
  var p = f && f.memoizedProps;
  if (!p || typeof p !== 'object') return false;
  if (typeof p.dispatch === 'function') {
    hooks.push({ at: 'fiberProps.dispatch', component: typeName(f),
                 src: String(p.dispatch).slice(0, 160) });
  }
  if (p.dawn && typeof p.dawn === 'object') {
    var ds = [];
    for (var k in p.dawn) {
      try { if (typeof p.dawn[k] === 'function') ds.push(k); } catch (e) {}
    }
    if (ds.length) hooks.push({ at: 'fiberProps.dawn', component: typeName(f),
                                methods: ds.slice(0, 25) });
  }
  return false;
}, 1e9);
var seen = {}, uniq = [];
for (var i = 0; i < hooks.length && uniq.length < 25; i++) {
  var key = hooks[i].at + '|' + (hooks[i].methods || hooks[i].src || '');
  if (seen[key]) continue;
  seen[key] = 1; uniq.push(hooks[i]);
}
out.hooks = uniq;
out.hookCount = hooks.length;
var nd = W.NE_DAWN;
if (nd) {
  var m = [];
  for (var k2 in nd) { try { if (typeof nd[k2] === 'function') m.push(k2); } catch (e2) {} }
  out.NE_DAWN_methods = m.slice(0, 40);
  out.NE_DAWN_keys = Object.keys(nd).slice(0, 40);
}
return json(out);
"""

# ────────────────────────────────────────────────────────── dispatch 记录器
#
# ⚠️ 为什么不能只包 `store.dispatch`：`connect()` 在**渲染时**就把 dispatch
#    捕获进闭包了（`bindActionCreators(creators, dispatch)`），之后再改
#    `store.dispatch` 这个**属性**，那些已经捕获的引用完全不受影响。
#    所以记录器必须把树上每一处能拿到的 dispatch 都包一遍（fiber props、
#    NE_DAWN、store 本身），并且把**已经生成好的绑定版 action creator**
#    也找不到 —— 那就靠"包到哪算哪，看有没有记录进来"来验证。

_BODY_INSTALL = r"""
var KEY = '__animusic_dispatch_log__';
var s = findStore();
var log = W[KEY];
if (!log) { log = { installed: false, items: [], hooks: [], errors: [] }; W[KEY] = log; }
if (log.installed) return json({ ok: true, already: true, n: log.items.length });

function clip(v, depth, seen) {
  depth = (depth === undefined ? 3 : depth);
  if (v === null || v === undefined) return v;
  var t = typeof v;
  if (t === 'string') return v.length > 200 ? v.slice(0, 200) + '…' : v;
  if (t === 'number' || t === 'boolean') return v;
  if (t === 'function') { try { return { __fn__: String(v).slice(0, 200) }; } catch (e) { return { __fn__: true }; } }
  if (depth <= 0) return { __kind__: prim(v) };
  if (isArr(v)) return { __arr__: v.length, item0: v.length ? clip(v[0], depth - 1) : null };
  if (t === 'object') {
    var o = {}, ks = [];
    try { ks = Object.keys(v); } catch (e2) { return { __kind__: 'object' }; }
    for (var i = 0; i < ks.length && i < 40; i++) {
      try { o[ks[i]] = clip(v[ks[i]], depth - 1); } catch (e3) { o[ks[i]] = '<<throw>>'; }
    }
    if (ks.length > 40) o.__more__ = ks.length - 40;
    return o;
  }
  return String(v);
}

function record(where, args) {
  try {
    log.items.push({ n: log.items.length, t: Date.now(), where: where,
                     action: clip(args && args[0], 4) });
    if (log.items.length > 300) log.items.shift();
  } catch (e) {}
}

function wrap(fn, where) {
  if (typeof fn !== 'function' || fn.__animusic_wrapped__) return fn;
  var g = function () { record(where, arguments); return fn.apply(this, arguments); };
  try { g.__animusic_wrapped__ = true; } catch (e) {}
  return g;
}

// ① store.dispatch（对还没捕获引用的调用者有效）
if (s) {
  try { s.dispatch = wrap(s.dispatch, 'store.dispatch'); log.hooks.push('store.dispatch'); }
  catch (e1) { log.errors.push('store.dispatch: ' + String(e1 && e1.message || e1)); }
}
// ② NE_DAWN.dispatch / getDispatch()
try {
  var nd = W.NE_DAWN;
  if (nd) {
    if (typeof nd.dispatch === 'function') {
      try { nd.dispatch = wrap(nd.dispatch, 'NE_DAWN.dispatch'); log.hooks.push('NE_DAWN.dispatch'); }
      catch (e2) { log.errors.push('NE_DAWN.dispatch: ' + String(e2 && e2.message || e2)); }
    }
    if (typeof nd.getDispatch === 'function') {
      try {
        var d0 = nd.getDispatch();
        var d1 = wrap(d0, 'NE_DAWN.getDispatch()');
        if (d1 !== d0) { nd.getDispatch = function () { return d1; }; log.hooks.push('NE_DAWN.getDispatch'); }
      } catch (e3) { log.errors.push('NE_DAWN.getDispatch: ' + String(e3 && e3.message || e3)); }
    }
  }
} catch (e4) {}
// ③ 树上每一处 props.dispatch（**这条才是真正管用的那条**，见上面注释）
var patched = 0;
walkFibers(function (f) {
  var p = f && f.memoizedProps;
  if (!p || typeof p !== 'object') return false;
  if (typeof p.dispatch === 'function' && !p.dispatch.__animusic_wrapped__) {
    try {
      // ★ 必须**就地改**，不能 `Object.assign({}, p, {...})` 造新对象：
      //   类组件的 `this.props` 就是 fiber 的 `memoizedProps` **同一个对象**，
      //   换成新对象后实例仍然拿着旧的那个，记录器就永远抓不到东西。
      p.dispatch = wrap(p.dispatch, 'props.dispatch:' + (typeName(f) || '?'));
      patched++;
    } catch (e5) {}
  }
  // dawn 上挂的 dispatch
  if (p.dawn && typeof p.dawn === 'object' && typeof p.dawn.dispatch === 'function'
      && !p.dawn.dispatch.__animusic_wrapped__) {
    try { p.dawn.dispatch = wrap(p.dawn.dispatch, 'props.dawn.dispatch'); patched++; } catch (e6) {}
  }
  return false;
}, 1e9);
// ★ 重渲染会把 memoizedProps 整个换掉，我们的补丁会被冲掉 ——
//   所以装一个**周期性重打补丁**的定时器（只在内存里，关掉客户端即消失）。
if (log._timer) { clearInterval(log._timer); }
log._timer = setInterval(function () {
  try {
    walkFibers(function (f) {
      var p = f && f.memoizedProps;
      if (!p || typeof p !== 'object') return false;
      if (typeof p.dispatch === 'function' && !p.dispatch.__animusic_wrapped__) {
        try { p.dispatch = wrap(p.dispatch, 'props.dispatch:' + (typeName(f) || '?')); } catch (e) {}
      }
      if (p.dawn && typeof p.dawn === 'object' && typeof p.dawn.dispatch === 'function'
          && !p.dawn.dispatch.__animusic_wrapped__) {
        try { p.dawn.dispatch = wrap(p.dawn.dispatch, 'props.dawn.dispatch'); } catch (e) {}
      }
      return false;
    }, 1e9);
  } catch (e7) {}
}, 400);
log.hooks.push('fiberProps.dispatch x' + patched);
log.installed = true;
return json({ ok: true, already: false, hooks: log.hooks, patchedFibers: patched,
              errors: log.errors, storeFound: !!s,
              note: '已装 400ms 重打补丁定时器（抗重渲染）' });
"""

_BODY_DUMP = r"""
var KEY = '__animusic_dispatch_log__';
var log = W[KEY];
if (!log) return json({ installed: false, items: [] });
return json({ installed: log.installed, n: log.items.length,
              hooks: log.hooks, errors: log.errors, items: log.items });
"""

_BODY_UNINSTALL = r"""
var KEY = '__animusic_dispatch_log__';
var log = W[KEY];
var n = (log && log.items) ? log.items.length : 0;
try { delete W[KEY]; } catch (e) {}
return json({ ok: true, discarded: n,
              note: '记录器只影响内存；真正的解包要重开客户端（或刷新页面）' });
"""


_BODY_CONST = r"""
var out = { NE_DAWN_CONST: null, candidates: [], errors: [] };
var nd = W.NE_DAWN;
if (nd && nd.CONST) {
  out.NE_DAWN_CONST = shape(nd.CONST, 3, 60);
  // 找「像 action type」的字符串值：大写下划线，或带 @@ 前缀
  (function walk(v, path, depth) {
    if (!v || typeof v !== 'object' || depth > 3) return;
    var ks = [];
    try { ks = Object.keys(v); } catch (e) { return; }
    for (var i = 0; i < ks.length; i++) {
      var k = ks[i], sub;
      try { sub = v[k]; } catch (e2) { continue; }
      var p = path ? path + '.' + k : k;
      if (typeof sub === 'string' && sub.length < 80) {
        if (/^_*[A-Z][A-Z0-9_]{3,}$/.test(sub) || sub.indexOf('@@') === 0) {
          out.candidates.push({ path: p, value: sub });
        }
      } else if (sub && typeof sub === 'object') {
        walk(sub, p, depth + 1);
      }
    }
  })(nd.CONST, '', 0);
} else {
  out.errors.push('NE_DAWN.CONST 不存在');
}
if (nd && nd.Tool) { out.Tool_type = prim(nd.Tool); }
if (nd && nd.toolInstance) { out.toolInstance_type = prim(nd.toolInstance); }
return json(out);
"""

# 逃逸口：把任意 JS 片段（从文件读）塞进同一个沙盒里跑。
# 探索期最有用的一条 —— 不用为每个新想法改一次这个脚本。
_BODY_CUSTOM_PLACEHOLDER = "/*__CUSTOM_JS__*/"


# ═══════════════════════════════════════════════════════════════ 运行

def _eval(expr: str, port: int):
    browser = CDPBrowser(port, 25.0)
    tab = browser.find_tab("orpheus://")
    if tab is None:
        raise SystemExit(f"CDP 端口 {port} 上没有 orpheus:// 页面 —— 客户端没在跑？")
    page = browser.page(tab)
    try:
        return page.evaluate(expr)
    finally:
        page.close()


_MODES = {
    "shape": _BODY_SHAPE,
    "resources": _BODY_RESOURCES,
    "handlers": _BODY_HANDLERS,
    "const": _BODY_CONST,
    "install": _BODY_INSTALL,
    "dump": _BODY_DUMP,
    "uninstall": _BODY_UNINSTALL,
    "custom": _BODY_CUSTOM_PLACEHOLDER,
}


def main() -> int:
    ap_ = argparse.ArgumentParser(description="探测网易云客户端的写入链路（只读）")
    ap_.add_argument("mode", nargs="?", default="shape",
                     choices=sorted(k for k in _MODES))
    ap_.add_argument("--port", type=int, default=constants.NETEASE_DEBUG_PORT)
    ap_.add_argument("--js", default="", help="custom 模式要跑的 JS（或 --js-file）")
    ap_.add_argument("--js-file", default="", help="从文件读 JS（custom 模式）")
    ap_.add_argument("--raw", action="store_true", help="打印未缩进的原始 JSON")
    ap_.add_argument("--save", default="", help="把结果写到这个文件")
    args = ap_.parse_args()

    body = _MODES[args.mode]
    if args.mode == "custom":
        src = args.js
        if args.js_file:
            with open(args.js_file, encoding="utf-8") as f:
                src = f.read()
        if not src.strip():
            print("custom 模式需要 --js 或 --js-file")
            return 2
        body = ("try {\n" + src + "\n} catch (e) { return json({error: String(e && e.message || e)}); }")

    expr = "(function () {" + _PRELUDE + body + "})()"
    try:
        raw = _eval(expr, args.port)
    except (CDPError, SystemExit) as e:
        print(f"探测失败：{e}")
        return 1
    if raw is None:
        print("JS 没有返回值")
        return 1
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        print("返回的不是 JSON：", str(raw)[:400])
        return 1

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已写入 {args.save}")

    if args.raw:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:12000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


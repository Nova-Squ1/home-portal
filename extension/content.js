// 内容脚本：把页面切成「段」（同一父元素下连续的文字和行内元素），段滚到眼前才送去翻译。
// 原地替换模式复用原元素（链接、事件都还在），双语模式在段后插一份译文副本；再按一次全部还原。
// 后台每次 executeScript 都会注入本文件，所以整体包在函数里、只初始化一次。
(() => {
  if (window.__xt) return;

  // 整块不碰：看不见的、能输入的、代码块
  const IGNORE = new Set(["script", "style", "noscript", "template", "textarea", "select", "input",
    "option", "iframe", "canvas", "video", "audio", "object", "pre"]);
  // 段内原样保留、不翻译的行内元素
  const ATOM = new Set(["code", "kbd", "samp", "var", "img", "br", "wbr", "svg", "math"]);
  const BATCH_ITEMS = 30, BATCH_CHARS = 4000, PARALLEL = 4;

  let on = false, mode = "replace", gen = 0, err = "";
  let units = [], queue = [], active = 0, timer = 0;
  let seen = new WeakSet();          // 已经归进某段的原节点
  const mine = new WeakSet();        // 插件插进页面的节点
  const byParent = new Map();        // 父元素 -> 等它进视口再翻的段
  const dirty = new Set();           // 页面新加了内容、待重扫的元素

  const badge = document.createElement("div");
  badge.style.cssText = "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647;" +
    "max-width:360px;padding:6px 10px;border-radius:6px;background:#222;color:#fff;" +
    "font:12px/1.5 system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3)";
  function say(text, ms) {
    clearTimeout(say.t);
    if (!text) return badge.remove();
    badge.textContent = text;
    document.documentElement.append(badge);
    if (ms) say.t = setTimeout(() => badge.remove(), ms);
  }

  const keep = (el) => ATOM.has(el.localName) || el.translate === false ||
    el.classList.contains("notranslate");
  const dead = (el) => IGNORE.has(el.localName) || mine.has(el) || seen.has(el) ||
    el.isContentEditable || getComputedStyle(el).display === "none";

  // 能整个放进段里：行内显示，且里面没有块级或不能碰的东西
  function inline(el) {
    const d = getComputedStyle(el).display;
    if (keep(el)) return d.startsWith("inline");
    if (d !== "inline") return false;
    for (const c of el.children) if (dead(c) || !inline(c)) return false;
    return true;
  }

  function scan(el, out) {
    let run = [];
    const flush = () => { if (run.length) out.push(run); run = []; };
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        if (seen.has(n) || mine.has(n)) flush(); else run.push(n);
      } else if (n.nodeType === 1) {
        if (dead(n)) flush();
        else if (inline(n)) run.push(n);
        else { flush(); if (!keep(n)) scan(n, out); }
      }
    }
    flush();
  }

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const unesc = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

  // 行内元素换成 <gN>…</gN>，不翻译的换成 <gN/>；elems[N] 是对应原元素
  function serialize(nodes, elems) {
    let s = "";
    for (const n of nodes) {
      if (n.nodeType === 3) s += esc(n.data);
      else if (n.nodeType === 1) {
        const i = elems.push(n) - 1;
        s += keep(n) ? `<g${i}/>` : `<g${i}>${serialize(n.childNodes, elems)}</g${i}>`;
      }
    }
    return s;
  }

  // 至少两个字母，且不是以中文为主（夹几个英文词的中文不翻；日文照翻）
  function worth(src) {
    const t = src.replace(/<\/?g\d+\/?>/g, "");
    const letters = (t.match(/\p{L}/gu) || []).length;
    if (letters < 2) return false;
    if (/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(t)) return true;
    const han = (t.match(/\p{Script=Han}/gu) || []).length;
    return han * 4 < letters - han;
  }

  // 译文 -> DOM。reuse：第一次出现的元素直接挪原元素（原地替换），否则浅拷贝（双语）。只建文本节点，不碰 innerHTML。
  function build(tr, elems, reuse) {
    const frag = document.createDocumentFragment();
    const stack = [{ node: frag, i: -1 }];
    const used = new Set();
    for (const [, close, num, self, text] of tr.matchAll(/<(\/?)g(\d+)(\/?)>|([^<]+|<)/g)) {
      const top = stack[stack.length - 1].node;
      if (text !== undefined) { top.append(unesc(text)); continue; }
      const i = +num, el = elems[i];
      if (!el) continue;
      if (close) {
        const k = stack.findLastIndex((f) => f.i === i);
        if (k > 0) stack.length = k;
        continue;
      }
      const atom = self || keep(el);
      let node;
      if (reuse && !used.has(el)) {
        used.add(el);
        node = el;
        if (!atom) node.replaceChildren();
      } else {
        node = el.cloneNode(atom);
        if (node.removeAttribute) node.removeAttribute("id");
      }
      top.append(node);
      // 不翻译的元素写成了成对标签：里面的字丢进临时片段
      if (!self) stack.push({ node: atom ? document.createDocumentFragment() : node, i });
    }
    return frag;
  }

  function render(u) {
    const { parent, nodes } = u;
    const last = nodes[nodes.length - 1];
    if (last.parentNode !== parent) return; // 页面自己把这段改掉了
    u.how = mode;
    u.mark = document.createTextNode("");
    mine.add(u.mark);
    last.after(u.mark);
    if (mode === "bilingual") {
      const span = document.createElement("span");
      span.className = "xt-bi";
      span.style.cssText = "display:block;margin:.25em 0";
      span.append(build(u.tr, u.elems, false));
      u.out = [span];
    } else {
      u.kids = u.elems.map((e) => [...e.childNodes]);
      const frag = build(u.tr, u.elems, true);
      nodes.forEach((n) => n.parentNode === parent && n.remove());
      u.out = [...frag.childNodes];
    }
    u.out.forEach((n) => mine.add(n));
    u.mark.before(...u.out);
  }

  function restore(u) {
    if (!u.out) return;
    u.out.forEach((n) => n.remove());
    if (u.how === "replace") {
      u.elems.forEach((e) => e.remove());
      u.elems.forEach((e, i) => e.replaceChildren(...u.kids[i]));
      u.mark.before(...u.nodes);
    }
    u.mark.remove();
    u.out = null;
  }

  async function send(batch, g) {
    const r = await chrome.runtime.sendMessage({ type: "translate", texts: batch.map((u) => u.src) })
      .catch((e) => ({ error: e.message }));
    if (g !== gen) return;
    if (!r || r.error) { err = r ? r.error : "后台没响应"; return; }
    err = "";
    batch.forEach((u, i) => {
      u.tr = r.texts[i];
      try { render(u); } catch (e) { console.warn("Codex 翻译：", e); }
    });
  }

  function pump() {
    while (on && active < PARALLEL && queue.length) {
      const batch = [];
      let chars = 0;
      while (queue.length && batch.length < BATCH_ITEMS && chars < BATCH_CHARS) {
        const u = queue.shift();
        batch.push(u);
        chars += u.src.length;
      }
      active++;
      send(batch, gen).finally(() => { active--; pump(); });
    }
    if (!on) return;
    if (active) say("翻译中…");
    else if (err) say("翻译失败：" + err, 8000);
    else say("");
  }

  function enqueue(u) {
    queue.push(u);
    clearTimeout(timer);
    timer = setTimeout(pump, 50);
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      (byParent.get(e.target) || []).forEach(enqueue);
      byParent.delete(e.target);
    }
  }, { rootMargin: "600px 0px" });

  function collect(root) {
    const runs = [];
    scan(root, runs);
    for (const nodes of runs) {
      const elems = [];
      const src = serialize(nodes, elems).replace(/\s+/g, " ").trim();
      if (!worth(src)) continue;
      nodes.forEach((n) => seen.add(n));
      const u = { nodes, elems, src, parent: nodes[0].parentNode, tr: null, out: null };
      units.push(u);
      if (!u.parent.getClientRects().length) { enqueue(u); continue; } // 没有盒子（display:contents）就不等视口
      const list = byParent.get(u.parent);
      if (list) list.push(u);
      else { byParent.set(u.parent, [u]); io.observe(u.parent); }
    }
  }

  const inMine = (n) => { for (; n; n = n.parentNode) if (mine.has(n)) return true; return false; };

  // 页面后来加的内容（无限滚动、单页应用切换）也翻；插件自己插的节点不算
  const mo = new MutationObserver((records) => {
    for (const r of records) {
      if ([...r.addedNodes].some((n) => !mine.has(n) && !seen.has(n))) dirty.add(r.target);
    }
    clearTimeout(mo.t);
    mo.t = setTimeout(() => {
      const roots = [...dirty].filter((el) => el.isConnected && !inMine(el));
      dirty.clear();
      roots.filter((el) => !roots.some((o) => o !== el && o.contains(el))).forEach(collect);
    }, 400);
  });

  function start() {
    on = true;
    err = "";
    gen++;
    collect(document.body);
    if (!units.length) say("这页没有要翻译的外文", 3000);
    mo.observe(document.body, { childList: true, subtree: true });
  }

  function stop() {
    on = false;
    gen++;
    mo.disconnect();
    io.disconnect();
    clearTimeout(mo.t);
    clearTimeout(timer);
    dirty.clear();
    byParent.clear();
    queue = [];
    units.forEach(restore);
    units = [];
    seen = new WeakSet();
    say("");
  }

  function setMode(m) {
    if (m === mode) return;
    units.forEach(restore);
    mode = m;
    units.forEach((u) => u.tr != null && render(u));
  }

  window.__xt = {
    get on() { return on; },
    toggle(m) { mode = m; if (on) stop(); else start(); return on; },
    setMode,
  };
})();

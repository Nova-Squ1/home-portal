// 后台：把翻译请求转给门户 /api/translate（带 Authelia 登录 cookie），处理快捷键和弹窗的「翻译 / 还原」。

async function translate(texts) {
  const { server } = await chrome.storage.sync.get("server");
  if (!server) throw new Error("先点插件图标，填门户地址并保存");
  let res;
  try {
    res = await fetch(server + "/api/translate", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts }),
    });
  } catch (e) {
    throw new Error("连不上门户：" + e.message);
  }
  // 没登录时 Authelia 会把请求转去登录页，拿到的是 HTML 不是 JSON
  if (!(res.headers.get("content-type") || "").includes("json")) {
    throw new Error("没登录门户：先在 Edge 里打开 " + server + " 登录一次");
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "门户返回 " + res.status);
  return data.texts;
}

async function toggle(tabId) {
  const { mode = "replace" } = await chrome.storage.sync.get("mode");
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  const [r] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (m) => window.__xt.toggle(m),
    args: [mode],
  });
  return r.result;
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  const job = msg.type === "translate" ? translate(msg.texts).then((texts) => ({ texts }))
    : msg.type === "toggle" ? toggle(msg.tabId).then((on) => ({ on }))
    : null;
  if (!job) return;
  job.then(reply, (e) => reply({ error: e.message }));
  return true;
});

chrome.commands.onCommand.addListener((cmd, tab) => {
  if (cmd === "toggle" && tab) toggle(tab.id).catch((e) => console.warn("Codex 翻译：", e.message));
});

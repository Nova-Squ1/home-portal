const $ = (id) => document.getElementById(id);
const say = (text) => { $("msg").textContent = text || ""; };
let tabId;

(async () => {
  const { server = "", mode = "replace" } = await chrome.storage.sync.get(["server", "mode"]);
  $("server").value = server;
  document.querySelector(`input[value="${mode}"]`).checked = true;
  if (!server) say("先填门户地址并保存");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = tab.id;
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId }, func: () => !!window.__xt?.on });
    $("go").textContent = r.result ? "还原原文" : "翻译此页";
    $("go").disabled = false;
  } catch {
    say("这个页面不能翻译（浏览器内置页、扩展商店或 PDF）");
  }
})();

$("go").onclick = async () => {
  const r = await chrome.runtime.sendMessage({ type: "toggle", tabId });
  if (r.error) return say(r.error);
  window.close();
};

for (const radio of document.querySelectorAll("input[name=mode]")) {
  radio.onchange = () => {
    chrome.storage.sync.set({ mode: radio.value });
    chrome.scripting.executeScript({
      target: { tabId },
      func: (m) => window.__xt?.setMode(m),
      args: [radio.value],
    }).catch(() => {});
  };
}

$("save").onclick = () => {
  let origin;
  try {
    origin = new URL($("server").value.trim()).origin;
  } catch {
    return say("地址格式不对，要带 https://");
  }
  $("server").value = origin;
  // 先存再要权限：权限弹框可能把这个弹窗关掉，回调就跑不到了
  chrome.storage.sync.set({ server: origin });
  chrome.permissions.request({ origins: [origin + "/*"] }, (ok) => {
    say(ok ? "已保存" : "没给访问门户的权限，翻译请求发不出去");
  });
};

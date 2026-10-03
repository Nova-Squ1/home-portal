/* home-portal 共享脚本：布局注入、⌘K 搜索面板、工具函数。 */
(function () {
  "use strict";

  // 个人链接等站点配置由后端从 config/site.json 生成（/assets/site-config.js），缺省时用 site.example.json
  var SITE = window.PORTAL_SITE || {};
  function ext(items) {
    return (items || []).map(function (item) { return Object.assign({ external: true }, item); });
  }

  // 多租户：features 为 null 表示全部开放；后端 /assets/site-config.js 带下来
  var FEATURES = Array.isArray(SITE.features) ? SITE.features : null;
  var IS_ADMIN = !!(SITE.user && SITE.user.admin);
  function can(feature) {
    return !FEATURES || !feature || FEATURES.indexOf(feature) >= 0;
  }
  function keep(items) {
    return (items || []).filter(function (it) { return can(it && it.feature) && (!(it && it.admin) || IS_ADMIN); });
  }

  var NAV = {
    workspace: keep([
      { icon: "🏠", svg: "home", label: "Home", href: "/index.html", key: "home", feature: "home" },
      { icon: "🗓️", svg: "calendar", label: "Calendar", href: "/calendar.html", key: "calendar", feature: "calendar" },
      { icon: "✅", svg: "tasks", label: "Tasks", href: "/tasks.html", key: "tasks", feature: "tasks" }
    ]).concat(ext(SITE.workspace_links)),
    surfing: keep((SITE.surfing_links || []).map(function (l) { return Object.assign({}, l); })),
    // Notebook 在 Surfing（site.json 的 surfing_links）；OneAPI 是服务器订阅/API 台账，仅管理员
    private: keep([
      { icon: "🔌", svg: "plug", label: "OneAPI", href: "/oneapi.html", key: "oneapi", admin: true },
      { icon: "💰", svg: "finance", label: "Finance", href: "/finance.html", key: "finance", feature: "finance" },
      { icon: "🔖", svg: "bookmarks", label: "Bookmarks", href: "/bookmarks.html", key: "bookmarks", feature: "bookmarks" }
    ]).concat(!can("mail") ? [] : (SITE.mail_links || []).length === 1 ? [{
      icon: "📥", svg: "mail", label: "Mail", href: SITE.mail_links[0].href, external: true
    }] : (SITE.mail_links || []).length > 1 ? [{
      icon: "📥", svg: "mail", label: "Mail", key: "inbox-mail", expandable: true, children: SITE.mail_links
    }] : []),
    system: [
      { icon: "🖥️", svg: "server", label: "Servers", href: "/server.html", key: "server" },
      { icon: "⚙️", svg: "settings", label: "Settings", href: "/settings.html", key: "settings" }
    ]
  };

  var RAIL = SITE.rail || [];

  // SF Symbols 风格线性 SVG 图标（stroke 1.8、圆头），导航 emoji 渐进替换
  var ICONS = {
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg>',
    home: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h5v-6h4v6h5V9.5"/></svg>',
    calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 10h18"/></svg>',
    tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="m8.5 12.5 2.5 2.5 5-5.5"/></svg>',
    notebook: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2z"/><path d="M9 2v20M13 7h3M13 11h3"/></svg>',
    finance: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M14.8 9.2A3.4 3.4 0 0 0 12 8c-1.9 0-3 1-3 2.2 0 3 6 1.6 6 4.6 0 1.2-1.1 2.2-3 2.2a3.4 3.4 0 0 1-2.8-1.2M12 6.5v11"/></svg>',
    bookmarks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4.5L5 21V4a1 1 0 0 1 1-1z"/></svg>',
    mail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m3.5 7 8.5 6 8.5-6"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v3M12 18.2v3M4.2 7.5l2.6 1.5M17.2 15l2.6 1.5M4.2 16.5 6.8 15M17.2 9l2.6-1.5"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.6-4.6"/></svg>',
    // 外链栏（rail）与左栏外链
    bilibili: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="19" height="12" rx="4"/><path d="M8 6 5.5 3M16 6l2.5-3"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/></svg>',
    server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>',
    bot: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="4"/><circle cx="9.5" cy="14" r="1.2"/><circle cx="14.5" cy="14" r="1.2"/><path d="M12 8V4.5M8.5 3h7"/></svg>',
    cloud: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 18a4.5 4.5 0 1 1 .9-8.9A5.5 5.5 0 0 1 18 11a3.5 3.5 0 0 1-.5 7z"/></svg>',
    lighthouse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6l-1 3h-4zM10 6h4l2 13H8zM12 9v3m0 3v3M5 21h14"/></svg>',
    chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 20.5h17M6 17V9m6 8V5m6 12v-6"/></svg>',
    github: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19c-4.5 1.5-4.5-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 4.2 4.2 0 0 0-.1-3.2s-1-.3-3.4 1.3a11.8 11.8 0 0 0-6.2 0C6.5 2.8 5.5 3.1 5.5 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/></svg>',
    droplet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3s6 6.2 6 10.5a6 6 0 1 1-12 0C6 9.2 12 3 12 3z"/></svg>',
    linux: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="13" rx="6" ry="8"/><path d="M10 9.5h.01M14 9.5h.01M10.5 12.5h3M12 15h.01M9 6c-1-1.5-1-3 0-3.5M15 6c1-1.5 1-3 0-3.5"/></svg>',
    bird: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10c2-4 6-6 10-5l3-2 .5 3.5c2 1.5 3 4 2.5 7-1 4-5 7-10 7H4l2.5-3.5C4.5 15 3.5 12.5 4 10z"/><circle cx="14" cy="10" r=".8" fill="currentColor"/></svg>',
    fire: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3c1 3-3 4.5-3 8a3 3 0 0 0 6 .2C15 9.5 17 9 17 6c2 2 3 4.5 3 7a8 8 0 1 1-16 0c0-4.5 4-6.5 8-10z"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19V5a2 2 0 0 1 2-2h14v18H6a2 2 0 0 1-2-2z"/><path d="M8 7h8M8 11h8"/></svg>',
    code: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m8 8-4.5 4L8 16M16 8l4.5 4L16 16M13 5l-2 14"/></svg>',
    face: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21a9 9 0 1 1 9-9c0 2-1.5 3-3 3h-2a2.5 2.5 0 0 0-2 4c.4.6.2 2-2 2z"/><circle cx="8.5" cy="10.5" r=".9" fill="currentColor"/><circle cx="13" cy="7.5" r=".9" fill="currentColor"/></svg>',
    doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2h8l5 5v15H6z"/><path d="M14 2v5h5M9 12h7M9 16h7"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-8 8H4l2-3.2A8 8 0 1 1 21 12z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/></svg>',
    sparkles: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.7 4.3L18 10l-4.3 1.7L12 16l-1.7-4.3L6 10l4.3-1.7zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8zM5 3l.7 1.8L7.5 5.5l-1.8.7L5 8l-.7-1.8L2.5 5.5l1.8-.7z"/></svg>',
    brain: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 3a3 3 0 0 0-3 3 3 3 0 0 0-2.4 4.8A3.2 3.2 0 0 0 5 16.5 3 3 0 0 0 9.5 21c1 0 2.5-.7 2.5-2V5c0-1.3-1.5-2-2.5-2z"/><path d="M14.5 3a3 3 0 0 1 3 3 3 3 0 0 1 2.4 4.8A3.2 3.2 0 0 1 19 16.5 3 3 0 0 1 14.5 21c-1 0-2.5-.7-2.5-2V5c0-1.3 1.5-2 2.5-2z"/></svg>',
    volcano: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 4c1 1.5 1 2.5 0 4M12 3c1 1.5 1 3 0 4.5M16 4c1 1.5 1 2.5 0 4M9 9 3 20h18L15 9z"/><path d="M9 20c1-2 2-2.5 3-2.5s2 .5 3 2.5"/></svg>',
    money: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 6.5v11M15 9c-.6-1-1.7-1.5-3-1.5-1.6 0-2.7.8-2.7 2 0 2.8 5.7 1.5 5.7 4.2 0 1.2-1.2 2-2.9 2-1.4 0-2.5-.6-3.1-1.6"/></svg>',
    grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/></svg>',
    home2: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>',
    archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/></svg>',
    grad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m2.5 9 9.5-4.5L21.5 9 12 13.5z"/><path d="M6.5 11v5c0 1.2 2.5 2.5 5.5 2.5s5.5-1.3 5.5-2.5v-5M21.5 9v5"/></svg>',
    plug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v4M15 3v4"/><path d="M6 7h12v3a6 6 0 0 1-12 0z"/><path d="M12 16v5"/></svg>',
    link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 14a4.5 4.5 0 0 0 6.4.4l3-3a4.5 4.5 0 0 0-6.4-6.4l-1.5 1.5"/><path d="M14 10a4.5 4.5 0 0 0-6.4-.4l-3 3a4.5 4.5 0 0 0 6.4 6.4l1.5-1.5"/></svg>'
  };
  function navIcon(item) {
    return item.svg ? '<span class="nav-icon nav-icon-svg">' + ICONS[item.svg] + "</span>"
                    : '<span class="nav-icon">' + item.icon + "</span>";
  }

  // 站点配置的链接按 label 自动匹配 SVG 图标；匹配不到时回退 emoji
  var LABEL_ICONS = [
    [/[Bb]ilibili/, "bilibili"],
    [/Server Panel|1[Pp]anel|面板/, "server"],
    [/AstrBot|[Bb]ot(?!ify)/, "bot"],
    [/Cloudflare|DNS/, "cloud"],
    [/Lighthouse|灯塔|腾讯/, "lighthouse"],
    [/GitHub Trending/, "github"],
    [/GitHot/, "fire"],
    [/GitHub/, "github"],
    [/水源|SJTU.*Wiki|Wiki/, "droplet"],
    [/Linux/, "linux"],
    [/IDCflare|Twitter|X(?![a-z])/, "bird"],
    [/LeetCode|Leetcode/, "code"],
    [/Hugging\s?Face|HF/, "face"],
    [/arXiv|论文|paper/i, "doc"],
    [/ChatGPT|GPT/, "chat"],
    [/Gemini|Sparkle/, "sparkles"],
    [/Micu|Model|模型/, "brain"],
    [/Volcengine|Ark|火山/, "volcano"],
    [/Price|价格|比价/i, "money"],
    [/SMIS|门户|Portal/i, "grid"],
    [/趋势|Trend|Chart|行情/i, "chart"]
  ];
  function labelIcon(label) {
    if (!label) return null;
    for (var i = 0; i < LABEL_ICONS.length; i++) {
      if (LABEL_ICONS[i][0].test(label)) return LABEL_ICONS[i][1];
    }
    return null;
  }
  function renderIcon(item) {
    var key = item.svg || labelIcon(item.label);
    return key && ICONS[key]
      ? '<span class="nav-icon nav-icon-svg">' + ICONS[key] + "</span>"
      : '<span class="nav-icon">' + (item.icon || "") + "</span>";
  }

  // 顶栏滚动后由透明渐变为玻璃条（方案6a）
  function watchTopbar() {
    var bar = document.querySelector(".topbar");
    if (!bar) return;
    bar.classList.add("topbar-auto");
    var update = function () {
      bar.classList.toggle("scrolled", window.scrollY > 8);
    };
    window.addEventListener("scroll", update, { passive: true });
    update();
  }

  // 移动端侧栏抽屉：顶栏菜单按钮 + 左缘右滑打开、左滑/点背景关闭
  function enableMobileDrawer() {
    var body = document.body;
    if (body.dataset.drawerReady) return;
    body.dataset.drawerReady = "1";

    var side = document.querySelector(".side");
    if (!side) return;

    var scrim = document.createElement("div");
    scrim.className = "drawer-scrim";

    // 菜单按钮（插入所有 .topbar 的最前面；无 topbar 的页面挂在 body 上悬浮）
    function makeBtn() {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "drawer-btn";
      btn.setAttribute("aria-label", "打开侧栏");
      btn.innerHTML = ICONS.menu || '<span style="font-size:18px;line-height:1">☰</span>';
      btn.addEventListener("click", function () {
        body.classList.contains("drawer-open") ? closeDrawer() : openDrawer();
      });
      return btn;
    }
    var bars = document.querySelectorAll(".topbar");
    if (bars.length) {
      bars.forEach(function (bar) { bar.insertBefore(makeBtn(), bar.firstChild); });
    } else {
      var f = makeBtn();
      f.classList.add("drawer-btn-float");
      body.appendChild(f);
    }

    function openDrawer() {
      body.classList.add("drawer-open");
      scrim.dataset.open = "1";
    }
    function closeDrawer() {
      body.classList.remove("drawer-open");
      delete scrim.dataset.open;
    }
    window.PortalDrawer = { open: openDrawer, close: closeDrawer,
      toggle: function () { body.classList.contains("drawer-open") ? closeDrawer() : openDrawer(); } };

    side.parentNode && side.parentNode.insertBefore(scrim, side);
    scrim.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeDrawer();
    });

    // 触摸手势：左缘 24px 内起手右滑打开；抽屉打开时左滑关闭
    var startX = 0, startY = 0, tracking = false, edge = false;
    document.addEventListener("touchstart", function (e) {
      var t = e.touches[0];
      if (e.touches.length !== 1) { tracking = false; return; }
      startX = t.clientX; startY = t.clientY;
      edge = startX < 24;
      tracking = true;
    }, { passive: true });
    document.addEventListener("touchmove", function (e) {
      if (!tracking) return;
      var t = e.touches[0];
      var dx = t.clientX - startX, dy = Math.abs(t.clientY - startY);
      if (Math.abs(dx) < 56 || dy > 44) return;
      if (!body.classList.contains("drawer-open") && edge && dx > 0) {
        openDrawer();
        tracking = false;
      } else if (body.classList.contains("drawer-open") && dx < 0) {
        closeDrawer();
        tracking = false;
      }
    }, { passive: true });
    document.addEventListener("touchend", function () { tracking = false; }, { passive: true });
  }

  // 子页标题栏 emoji 渐进替换为线性 SVG（与导航一致的图标体系）
  function hydratePageIcons() {
    var nodes = document.querySelectorAll(".page-emoji-svg[data-icon], .h-ico[data-icon]");
    for (var i = 0; i < nodes.length; i++) {
      var svg = ICONS[nodes[i].getAttribute("data-icon")];
      if (svg) nodes[i].innerHTML = svg;
    }
  }

  var ENGINES = (SITE.engines || [{ name: "DuckDuckGo", bang: null, url: "https://duckduckgo.com/?q={q}" }])
    .map(function (en, i) { return Object.assign({ placeholder: i === 0 }, en); });

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* 全局左栏可缩进：按钮在侧栏顶部（N 徽标行右侧），收起后只剩 60px 图标列，
     悬停临时展开浮层。偏好存 localStorage portal-side-min，所有页面共享，仅桌面端。 */
  var SIDE_MIN_KEY = "portal-side-min";
  function setSideMin(min, persist) {
    var app = document.querySelector(".app");
    var side = app && app.querySelector(".side");
    if (!app || !side) return;
    document.body.classList.toggle("side-min", !!min);
    side.classList.toggle("is-min", !!min);
    var btn = document.getElementById("sideMinBtn");
    if (btn) { btn.textContent = min ? "»" : "«"; btn.title = min ? "展开侧栏" : "收起侧栏（只留图标）"; btn.setAttribute("aria-label", btn.title); }
    if (persist !== false) { try { localStorage.setItem(SIDE_MIN_KEY, min ? "1" : ""); } catch (_) {} }
  }
  function initSideCollapse() {
    if (!matchMedia("(min-width: 821px)").matches) return;
    var app = document.querySelector(".app");
    var side = app && app.querySelector(".side");
    var btn = document.getElementById("sideMinBtn");
    if (!side || !btn) return;
    btn.addEventListener("click", function () { setSideMin(!document.body.classList.contains("side-min")); });
    var saved = null; try { saved = localStorage.getItem(SIDE_MIN_KEY); } catch (_) {}
    setSideMin(saved === "1", false);
  }

  function buildSide(activeKey) {
    var side = el("aside", "side");

    var top = el("div", "side-top");
    var ws = el("a", "ws");
    ws.href = SITE.workspace_url || "/";
    ws.title = "打开个人主页";
    ws.innerHTML =
      '<div class="ws-icon">N</div>' +
      '<div class="ws-name">Nova Workspace</div>' +
      '<div class="ws-caret">⌄</div>';
    top.appendChild(ws);
    var minBtn = el("button", "side-min-btn");
    minBtn.type = "button";
    minBtn.id = "sideMinBtn";
    minBtn.textContent = "«";
    minBtn.title = "收起侧栏（只留图标）";
    minBtn.setAttribute("aria-label", minBtn.title);
    top.appendChild(minBtn);
    side.appendChild(top);

    var search = el("div", "side-row");
    search.id = "openSearch";
    search.setAttribute("role", "button");
    search.tabIndex = 0;
    search.innerHTML = '<span class="nav-icon nav-icon-svg">' + ICONS.search + '</span><span>Search</span><kbd>⌘K</kbd>';
    side.appendChild(search);

    function section(title, items) {
      side.appendChild(el("div", "side-section", title));
      var nav = el("div", "nav");
      items.forEach(function (item) {
        var row = el("a", "nav-item" + (item.key === activeKey ? " active" : ""));
        if (item.href) row.href = item.href;
        if (item.external) { row.target = "_blank"; row.rel = "noopener noreferrer"; }
        row.innerHTML =
          renderIcon(item) + "<span>" + esc(item.label) + "</span>" +
          (item.expandable ? '<span class="nav-caret">›</span>' : "");
        nav.appendChild(row);
        if (item.expandable && item.children) {
          var sub = el("div", "sub-nav");
          item.children.forEach(function (ch) {
            var cr = el("a", "nav-item");
            cr.href = ch.href;
            cr.target = "_blank";
            cr.rel = "noopener noreferrer";
            cr.innerHTML =
              '<span class="sub-dot" style="background:' + ch.color + '"></span>' +
              "<span>" + esc(ch.label) + "</span>";
            sub.appendChild(cr);
          });
          nav.appendChild(sub);
          row.addEventListener("click", function (e) {
            e.preventDefault();
            row.classList.toggle("open");
          });
        }
      });
      side.appendChild(nav);
    }

    section("Workspace", NAV.workspace);
    section("Surfing", NAV.surfing);
    section("Private", NAV.private);

    side.appendChild(el("div", "side-section", "System"));
    var sysNav = el("div", "nav");
    NAV.system.forEach(function (item) {
      var row = el("a", "nav-item" + (item.key === activeKey ? " active" : ""));
      row.href = item.href;
      if (item.external) { row.target = "_blank"; row.rel = "noopener noreferrer"; }
      row.innerHTML = renderIcon(item) + "<span>" + esc(item.label) + "</span>";
      sysNav.appendChild(row);
    });
    side.appendChild(sysNav);

    return side;
  }

  function buildRail() {
    var rail = el("aside", "rail");
    RAIL.forEach(function (group) {
      var g = el("div", "rail-group");
      g.appendChild(el("h3", null, esc(group.title)));
      group.links.forEach(function (link) {
        var a = el("a", "rail-link");
        a.href = link.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.innerHTML =
          '<span class="ico">' + renderIcon(link) + "</span><span>" + esc(link.label) +
          '</span><span class="ext">↗</span>';
        g.appendChild(a);
      });
      rail.appendChild(g);
    });
    return rail;
  }

  /* ---------- Search palette ---------- */
  function buildPalette() {
    var backdrop = el("div", "palette-backdrop");
    backdrop.innerHTML =
      '<div class="palette">' +
      '  <div class="palette-input"><span>🔎</span><input type="text" id="paletteInput" placeholder="用 Nova Search 搜索，或用 !gh / !b / !so / !arxiv 跳转…" autocomplete="off"></div>' +
      '  <div class="palette-list" id="paletteList"></div>' +
      '  <div class="palette-foot"><span>↵ 跳转</span><span>esc 关闭</span><span>⌘K 随时唤起</span></div>' +
      "</div>";
    document.body.appendChild(backdrop);

    var input = backdrop.querySelector("#paletteInput");
    var list = backdrop.querySelector("#paletteList");
    var activeIdx = 0;

    function currentEngines(q) {
      var bangMatch = /^\s*(!\S+)\s+([\s\S]+)$/.exec(q);
      if (bangMatch) {
        var bang = bangMatch[1].toLowerCase();
        var query = bangMatch[2];
        return ENGINES.filter(function (en) { return en.bang === bang; })
          .map(function (en) { return { name: en.name + " · " + bang, url: en.url.replace("{q}", encodeURIComponent(query)) }; });
      }
      return ENGINES.map(function (en) {
        return { name: en.name + (en.bang ? " · " + en.bang : ""), url: en.url.replace("{q}", encodeURIComponent(q.trim())) };
      }).filter(function (en) { return q.trim(); });
    }

    function render() {
      var items = currentEngines(input.value);
      activeIdx = 0;
      list.innerHTML = "";
      if (!items.length) {
        list.innerHTML = '<div class="palette-item" style="color:var(--ink-3);cursor:default">输入关键词开始搜索</div>';
        return;
      }
      items.forEach(function (it, i) {
        var row = el("div", "palette-item" + (i === 0 ? " active" : ""));
        var bangName = it.name.split(" · ")[1];
        row.innerHTML =
          (bangName ? '<span class="bang">' + esc(bangName) + "</span>" : '<span class="bang">web</span>') +
          "<span>" + esc(it.name.split(" · ")[0]) + "</span>";
        row.addEventListener("click", function () { window.open(it.url, "_blank", "noopener"); close(); });
        list.appendChild(row);
      });
    }

    function open() {
      backdrop.classList.add("show");
      input.value = "";
      render();
      setTimeout(function () { input.focus(); }, 0);
    }
    function close() { backdrop.classList.remove("show"); }

    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      var rows = list.querySelectorAll(".palette-item");
      if (e.key === "Escape") { close(); }
      else if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIdx = Math.min(activeIdx + 1, rows.length - 1);
        rows.forEach(function (r, i) { r.classList.toggle("active", i === activeIdx); });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIdx = Math.max(activeIdx - 1, 0);
        rows.forEach(function (r, i) { r.classList.toggle("active", i === activeIdx); });
      } else if (e.key === "Enter") {
        var chosen = rows[activeIdx];
        if (chosen) chosen.click();
      }
    });
    backdrop.addEventListener("click", function (e) { if (e.target === backdrop) close(); });

    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        backdrop.classList.contains("show") ? close() : open();
      }
    });
    document.getElementById("openSearch").addEventListener("click", open);

  }

  /* ---------- Toast：portalToast(msg[, type])，type 为 success / error / info，不传时按文字推断 ---------- */
  var toastEl;
  var TOAST_FACE = { success: "✓", error: "!", info: "i" };
  window.portalToast = function (msg, type) {
    msg = String(msg == null ? "" : msg);
    type = type || (/失败|错误|无法|不能/.test(msg) ? "error" : /^已|成功|完成/.test(msg) ? "success" : "info");
    if (!toastEl) {
      toastEl = el("div", "toast");
      toastEl.setAttribute("role", "status");
      toastEl.setAttribute("aria-live", "polite");
      toastEl.innerHTML = '<span class="toast-face" aria-hidden="true"></span><span class="toast-text"></span>';
      document.body.appendChild(toastEl);
    }
    toastEl.className = "toast " + type;
    toastEl.querySelector(".toast-face").textContent = TOAST_FACE[type] || TOAST_FACE.info;
    toastEl.querySelector(".toast-text").textContent = msg;
    void toastEl.offsetWidth; // 重新触发弹入动画
    toastEl.classList.add("show");
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.classList.remove("show"); }, type === "error" ? 3600 : 2200);
  };

  /* ---------- 对话框：替代 alert / confirm，全部返回 Promise ----------
     PortalDialog.show({ tone, icon, title, message, list, buttons: [{ label, value, kind, default }], cancelValue })
       tone: info | question | warn | danger | success；kind: ghost | primary | danger
       Esc / 点背景返回 cancelValue；回车触发 default 按钮（没有则最后一个）
     PortalDialog.confirm({ title, message, confirmText, cancelText, danger }) -> Promise<boolean>
     PortalDialog.alert({ title, message, tone, okText }) -> Promise<void> */
  var DIALOG_LOOK = { info: "i", question: "?", warn: "!", danger: "!", success: "✓" };
  var dialogQueue = Promise.resolve();

  function openDialog(opts) {
    return new Promise(function (resolve) {
      var tone = DIALOG_LOOK[opts.tone] ? opts.tone : "info";
      var look = DIALOG_LOOK[tone];
      var buttons = opts.buttons && opts.buttons.length ? opts.buttons : [{ label: "好的", value: true, kind: "primary" }];
      var cancelValue = "cancelValue" in opts ? opts.cancelValue : false;
      var lastFocus = document.activeElement;
      var uid = "moe" + Date.now().toString(36);

      var backdrop = el("div", "moe-backdrop");
      backdrop.innerHTML =
        '<div class="moe-card" data-tone="' + tone + '" role="alertdialog" aria-modal="true" aria-labelledby="' + uid + 't" aria-describedby="' + uid + 'm">' +
        '<div class="moe-badge" aria-hidden="true">' + look + "</div>" +
        '<h2 class="moe-title" id="' + uid + 't">' + esc(opts.title || "提示") + "</h2>" +
        '<div class="moe-msg" id="' + uid + 'm">' + esc(opts.message || "") + "</div>" +
        (opts.list && opts.list.length ? '<ul class="moe-list">' + opts.list.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>" : "") +
        '<div class="moe-actions"></div></div>';
      var card = backdrop.querySelector(".moe-card");
      var actions = backdrop.querySelector(".moe-actions");
      var defaultBtn = null;
      buttons.forEach(function (b) {
        var btn = el("button", "moe-btn " + (b.kind || "ghost"));
        btn.type = "button";
        btn.textContent = b.label;
        btn.addEventListener("click", function () { close(b.value); });
        actions.appendChild(btn);
        if (b["default"]) defaultBtn = btn;
      });
      var allBtns = actions.querySelectorAll(".moe-btn");
      defaultBtn = defaultBtn || allBtns[allBtns.length - 1];

      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(cancelValue); }
        else if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault(); e.stopPropagation();
          (document.activeElement && document.activeElement.classList.contains("moe-btn") ? document.activeElement : defaultBtn).click();
        } else if (e.key === "Tab") {
          // 焦点只在按钮之间循环
          var list = Array.prototype.slice.call(allBtns), idx = list.indexOf(document.activeElement);
          e.preventDefault();
          list[(idx + (e.shiftKey ? -1 : 1) + list.length) % list.length].focus();
        }
      }
      var closed = false;
      function close(value) {
        if (closed) return;
        closed = true;
        document.removeEventListener("keydown", onKey, true);
        backdrop.classList.add("leaving");
        setTimeout(function () {
          backdrop.remove();
          if (lastFocus && lastFocus.focus && document.contains(lastFocus)) lastFocus.focus();
          resolve(value);
        }, 170);
      }
      backdrop.addEventListener("mousedown", function (e) {
        if (e.target !== backdrop) return;
        if (opts.dismissible === false) {
          card.classList.remove("shake"); void card.offsetWidth; card.classList.add("shake");
        } else close(cancelValue);
      });
      document.addEventListener("keydown", onKey, true);
      document.body.appendChild(backdrop);
      requestAnimationFrame(function () { backdrop.classList.add("show"); });
      (opts.focus === "cancel" ? allBtns[0] : defaultBtn).focus({ preventScroll: true });
    });
  }

  window.PortalDialog = {
    // 同一时间只显示一个，后来的排队
    show: function (opts) {
      var run = dialogQueue.then(function () { return openDialog(opts || {}); });
      dialogQueue = run.catch(function () {});
      return run;
    },
    confirm: function (opts) {
      opts = opts || {};
      return this.show({
        tone: opts.tone || (opts.danger ? "danger" : "question"), icon: opts.icon, kao: opts.kao,
        title: opts.title, message: opts.message, list: opts.list,
        focus: opts.danger ? "cancel" : undefined,
        buttons: [
          { label: opts.cancelText || "再想想", value: false, kind: "ghost" },
          { label: opts.confirmText || "确定", value: true, kind: opts.danger ? "danger" : "primary", "default": true }
        ]
      });
    },
    alert: function (opts) {
      opts = typeof opts === "string" ? { message: opts } : (opts || {});
      return this.show({
        tone: opts.tone || "info", icon: opts.icon, kao: opts.kao, title: opts.title, message: opts.message,
        cancelValue: undefined, buttons: [{ label: opts.okText || "知道啦", value: undefined, kind: "primary" }]
      }).then(function () {});
    }
  };

  /* ---------- Tasks（与 calendar 页共享 localStorage） ---------- */
  var TASKS_KEY = "nova.tasks.v1";
  var TASK_STATE_KEY = "nova.taskstate.v1";
  window.PortalTasks = {
    load: function () {
      try { return JSON.parse(localStorage.getItem(TASKS_KEY)) || []; }
      catch (e) { return []; }
    },
    save: function (tasks) { localStorage.setItem(TASKS_KEY, JSON.stringify(tasks)); },
    state: function () {
      try { return JSON.parse(localStorage.getItem(TASK_STATE_KEY)) || {}; }
      catch (e) { return {}; }
    },
    setState: function (key, patch) {
      var all = this.state();
      all[key] = Object.assign({}, all[key] || {}, patch);
      localStorage.setItem(TASK_STATE_KEY, JSON.stringify(all));
    },
    archived: function (key, done, localArchived) {
      return !!(done || localArchived || (this.state()[key] || {}).archived);
    },
    onHome: function (key, date, done, localArchived) {
      var state = this.state()[key] || {};
      return !this.archived(key, done, localArchived) &&
        (state.home || date === this.todayKey());
    },
    todayKey: function (d) {
      d = d || new Date();
      var m = String(d.getMonth() + 1).padStart(2, "0");
      var day = String(d.getDate()).padStart(2, "0");
      return d.getFullYear() + "-" + m + "-" + day;
    }
  };

  /* ---------- 课程表（独立于 Tasks/Canvas，只读静态数据） ---------- */
  var scheduleCache = { courses: [], exceptions: {}, max_week: 0 };
  var scheduleInflight = null;

  function localDate(dateKey) {
    var p = String(dateKey || "").split("-").map(Number);
    return p.length === 3 ? new Date(p[0], p[1] - 1, p[2]) : new Date(NaN);
  }

  function scheduleLoad() {
    if (scheduleCache.courses.length) return Promise.resolve(scheduleCache);
    if (scheduleInflight) return scheduleInflight;
    scheduleInflight = fetch("/assets/schedule.json?v=1", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) { scheduleCache = d; return d; })
      .catch(function (e) {
        scheduleCache.error = String(e.message || e);
        return scheduleCache;
      })
      .then(function (d) { scheduleInflight = null; return d; });
    return scheduleInflight;
  }

  function scheduleInfo(date) {
    var key = PortalTasks.todayKey(date);
    var exception = (scheduleCache.exceptions || {})[key] || null;
    var first = localDate(scheduleCache.week_one_monday);
    var day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    var week = Math.floor((day - first) / 604800000) + 1;
    var weekday = day.getDay() || 7;
    if (exception && exception.week) week = exception.week;
    if (exception && exception.weekday) weekday = exception.weekday;
    return {
      key: key,
      week: week,
      weekday: weekday,
      label: exception && exception.label || "",
      cancelled: !!(exception && exception.cancelled),
      inSemester: week >= 1 && week <= (scheduleCache.max_week || 0)
    };
  }

  window.PortalSchedule = {
    cache: function () { return scheduleCache; },
    load: scheduleLoad,
    info: scheduleInfo,
    forDate: function (date) {
      var info = scheduleInfo(date);
      if (!info.inSemester || info.cancelled) return [];
      return (scheduleCache.courses || []).filter(function (course) {
        return course.weekday === info.weekday && course.weeks.indexOf(info.week) !== -1;
      }).sort(function (a, b) {
        return parseInt(a.periods, 10) - parseInt(b.periods, 10);
      });
    },
    weekMonday: function (date) {
      var day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
      day.setDate(day.getDate() - ((day.getDay() + 6) % 7));
      return day;
    }
  };

  /* ---------- Canvas 任务，来自后端缓存 ---------- */
  var canvasCache = { tasks: [], messages: [], updated: null, configured: null, errors: [] };
  var canvasInflight = null;

  function fetchCanvas(force) {
    // 该账号没开 Canvas：直接给空缓存，不去请求（后端也会 403）
    if (!can("canvas")) {
      canvasCache = { tasks: [], messages: [], updated: null, configured: false, errors: [] };
      return Promise.resolve(canvasCache);
    }
    if (canvasInflight) return canvasInflight;
    canvasInflight = fetch("/api/oc-tasks" + (force ? "?refresh=1" : ""), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        canvasCache = d;
        return d;
      })
      .catch(function (e) {
        canvasCache.errors = [String(e.message || e)];
        return canvasCache;
      })
      .then(function (d) { canvasInflight = null; return d; });
    return canvasInflight;
  }

  window.PortalCanvas = {
    cache: function () { return canvasCache; },
    load: function (force) { return fetchCanvas(force); },
    /* 今天该显示的 Canvas 项：
       - 截止日 <= 今天 且未提交（待办/逾期/作业类）
       - planner 日期 == 今天 */
    forDate: function (tasks, dateKey) {
      return (tasks || []).filter(function (t) {
        if (t.due_local_date === dateKey) return true;
        return false;
      });
    },
    overdue: function (tasks, dateKey) {
      return (tasks || []).filter(function (t) {
        return t.due_local_date && t.due_local_date < dateKey && !t.submitted &&
          (t.kind === "missing" || t.kind === "todo" ||
           (t.kind === "planner" && t.plannable_type === "assignment"));
      });
    },
    kindLabel: function (t) {
      if (t.kind === "missing") return "逾期";
      if (t.kind === "todo") return "待办";
      if (t.kind === "assignment") return "作业";
      if (t.plannable_type === "announcement") return "公告";
      if (t.plannable_type === "planner_note") return "备忘";
      if (t.plannable_type === "assignment") return "作业";
      return "Canvas";
    },
    timeLabel: function (t) {
      if (!t.due_at) return "";
      var dt = new Date(t.due_at);
      if (isNaN(dt)) return "";
      var hm = dt.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
      var now = new Date();
      var sameDay = dt.toDateString() === now.toDateString();
      var dPart = "";
      if (!sameDay) {
        var diff = Math.round((new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()) -
                               new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
        if (diff === 1) dPart = "明天 ";
        else if (diff === 2) dPart = "后天 ";
        // 7 天内：周X + 具体日期并排（纯"星期二"无法区分本周/下周）
        else if (diff > 0 && diff <= 7) dPart = "周" + "日一二三四五六"[dt.getDay()] + " " + (dt.getMonth() + 1) + "-" + dt.getDate() + " ";
        else dPart = (dt.getMonth() + 1) + "-" + dt.getDate() + " ";
      } else {
        dPart = "今天 ";
      }
      var tPart = hm === "23:59" ? "" : hm;
      return dPart + tPart + (tPart ? " 截止" : "截止");
    }
  };

  window.PortalSite = {
    config: SITE,
    features: FEATURES,
    admin: IS_ADMIN,
    can: can,
    // data-feature="mail" / data-admin 的节点在功能未开放时整块移除
    gate: function (root) {
      (root || document).querySelectorAll("[data-feature]").forEach(function (node) {
        if (!can(node.getAttribute("data-feature"))) node.remove();
      });
      (root || document).querySelectorAll("[data-admin]").forEach(function (node) {
        if (!IS_ADMIN) node.remove();
      });
    },
    get: function (path, fallback) {
      var value = String(path).split(".").reduce(function (obj, key) { return obj && obj[key]; }, SITE);
      return value == null || value === "" ? fallback : value;
    },
    // <span data-site="canvas.name">Canvas</span>：有配置就替换文字，没有就保留默认文字
    fill: function (root) {
      (root || document).querySelectorAll("[data-site]").forEach(function (node) {
        node.textContent = PortalSite.get(node.getAttribute("data-site"), node.textContent);
      });
    }
  };

  window.PortalLayout = {
    mount: function (opts) {
      opts = opts || {};
      var app = document.querySelector(".app");
      var main = app.querySelector(".main");
      app.classList.toggle("with-rail", !!opts.rail);
      app.insertBefore(buildSide(opts.active), main);
      app.classList.add("mounted");  // 解除防 FOUC 预置宽度
      PortalSite.gate();
      PortalSite.fill();
      if (opts.rail) app.appendChild(buildRail());
      watchTopbar();
      hydratePageIcons();
      buildPalette();
      enableMobileDrawer();
      initSideCollapse();
      window.PortalHydrateIcons = hydratePageIcons;
    }
  };
})();

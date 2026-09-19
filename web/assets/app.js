/* home-portal 共享脚本：布局注入、⌘K 搜索面板、工具函数。 */
(function () {
  "use strict";

  // 个人链接等站点配置由后端从 config/site.json 生成（/assets/site-config.js），缺省时用 site.example.json
  var SITE = window.PORTAL_SITE || {};
  function ext(items) {
    return (items || []).map(function (item) { return Object.assign({ external: true }, item); });
  }

  var NAV = {
    workspace: [
      { icon: "🏠", label: "Home", href: "/index.html", key: "home" },
      { icon: "🗓️", label: "Calendar", href: "/calendar.html", key: "calendar" },
      { icon: "✅", label: "Tasks", href: "/tasks.html", key: "tasks" }
    ].concat(ext(SITE.workspace_links)),
    websites: ext(SITE.websites),
    private: [
      { icon: "📔", label: "Notebook", href: "/notebook.html", key: "notebook" },
      { icon: "💰", label: "Finance", href: "/finance.html", key: "finance" },
      { icon: "🔖", label: "Bookmarks", href: "/bookmarks.html", key: "bookmarks" }
    ].concat((SITE.mail_links || []).length ? [{
      icon: "📥", label: "Mail", key: "inbox-mail", expandable: true, children: SITE.mail_links
    }] : []),
    system: [
      { icon: "⚙️", label: "Settings", href: "/settings.html", key: "settings" }
    ]
  };

  var RAIL = SITE.rail || [];

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

  function buildSide(activeKey) {
    var side = el("aside", "side");

    var ws = el("a", "ws");
    ws.href = SITE.workspace_url || "/";
    ws.title = "打开个人主页";
    ws.innerHTML =
      '<div class="ws-icon">N</div>' +
      '<div class="ws-name">Nova Workspace</div>' +
      '<div class="ws-caret">⌄</div>';
    side.appendChild(ws);

    var search = el("div", "side-row");
    search.id = "openSearch";
    search.setAttribute("role", "button");
    search.tabIndex = 0;
    search.innerHTML = "<span>🔎</span><span>Search</span><kbd>⌘K</kbd>";
    side.appendChild(search);

    function section(title, items) {
      side.appendChild(el("div", "side-section", title));
      var nav = el("div", "nav");
      items.forEach(function (item) {
        var row = el("a", "nav-item" + (item.key === activeKey ? " active" : ""));
        if (item.href) row.href = item.href;
        if (item.external) { row.target = "_blank"; row.rel = "noopener noreferrer"; }
        row.innerHTML =
          '<span class="nav-icon">' + item.icon + "</span><span>" + esc(item.label) + "</span>" +
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
    section("Website", NAV.websites);
    section("Private", NAV.private);

    side.appendChild(el("div", "side-section", "System"));
    var sysNav = el("div", "nav");
    NAV.system.forEach(function (item) {
      var row = el("a", "nav-item" + (item.key === activeKey ? " active" : ""));
      row.href = item.href;
      if (item.external) { row.target = "_blank"; row.rel = "noopener noreferrer"; }
      row.innerHTML = '<span class="nav-icon">' + item.icon + "</span><span>" + esc(item.label) + "</span>";
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
          '<span class="ico">' + link.icon + "</span><span>" + esc(link.label) +
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
  var TOAST_FACE = { success: "✨", error: "💦", info: "🌸" };
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
  var DIALOG_LOOK = {
    info:     { icon: "🌸", kao: "(｡･ω･｡)ﾉ" },
    question: { icon: "💭", kao: "(・ω・)？" },
    warn:     { icon: "⚠️", kao: "(°ー°〃)" },
    danger:   { icon: "🗑️", kao: "(｡•́︿•̀｡)" },
    success:  { icon: "🎉", kao: "(๑•̀ㅂ•́)و✧" }
  };
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
      var petals = "";
      for (var i = 0; i < 7; i += 1) {
        petals += '<i class="moe-petal" style="--x:' + (6 + i * 14 + Math.random() * 6).toFixed(1) + "%;--s:" + (9 + Math.random() * 7).toFixed(1) +
          "px;--d:" + (7 + Math.random() * 5).toFixed(1) + "s;--delay:-" + (Math.random() * 8).toFixed(1) + 's"></i>';
      }
      backdrop.innerHTML = petals +
        '<div class="moe-card" data-tone="' + tone + '" role="alertdialog" aria-modal="true" aria-labelledby="' + uid + 't" aria-describedby="' + uid + 'm">' +
        '<div class="moe-badge" aria-hidden="true">' + esc(opts.icon || look.icon) + "</div>" +
        '<div class="moe-kao" aria-hidden="true">' + esc(opts.kao || look.kao) + "</div>" +
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
      return hm === "23:59" ? "截止" : "截止 " + hm;
    }
  };

  window.PortalSite = {
    config: SITE,
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
      PortalSite.fill();
      if (opts.rail) app.appendChild(buildRail());
      buildPalette();
    }
  };
})();

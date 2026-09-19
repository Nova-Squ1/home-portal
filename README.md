# Nova Workspace · home-portal

一个 Notion 风格的个人门户：把每天要看的东西放在一个页面里。后端只用 Python 标准库，前端是不依赖构建工具的原生 HTML/CSS/JS。

## 功能

| 页面 | 内容 |
|---|---|
| **Home** | 问候语、今日课表、今日任务、Canvas 近期作业、重要邮件（LLM 审查未读邮件）、Canvas 站内信未读、服务器 CPU/内存/磁盘/负载 |
| **Calendar** | 课表 + Canvas 作业 + 本地任务按日期汇总，默认显示本周，可展开整月 |
| **Tasks** | 本地任务与 Canvas 所有学期作业，拖拽到主页或归档 |
| **Notebook** | Markdown 笔记：自动保存与离线草稿、冲突处理、标签、置顶、归档、附件（截图粘贴/拖入）与配额 |
| **Finance** | 订阅管理（按天/周/月/年周期、价格历史、扣款日自动记账）+ 收支记账（按月/年统计、分类占比），CNY/USD/GBP/HKD 实时汇率折算 |
| **Bookmarks** | 只存链接的收藏夹：自动抓取标题/描述/图标，书签栏一键收藏（小书签） |
| **Settings** | 快捷入口与本地数据管理 |

另有 ⌘K 搜索面板（支持 `!gh` 等 bang）、自绘的弹窗与提示条（不使用浏览器原生 `alert`/`confirm`）。

## 目录

```
app.py            HTTP 服务：静态文件 + /api/*，后台线程轮询邮件、Canvas、汇率
canvas_sync.py    Canvas LMS REST API 同步（作业、待办、Planner、站内信）
llm_triage.py     用 OpenAI 兼容接口判断邮件是否重要，失败时回退关键词规则
finance.py        订阅与记账、汇率（open.er-api.com，备用 fawazahmed0/exchange-api）
bookmarks.py      收藏夹与网页元数据抓取（带 SSRF 防护）
web/              前端页面；web/assets/app.js 为共享布局、导航、弹窗等
config/*.example  配置模板（真实配置不进 git）
data/             运行数据（不进 git）
```

## 运行

需要 Python 3（在 3.12 上测试），无第三方依赖。

```bash
cp web/assets/site.example.json config/site.json      # 导航链接、右栏、搜索引擎等个人配置
cp web/assets/schedule.example.json config/schedule.json  # 课表（可选）
cp config/mail.yml.example config/mail.yml            # 邮箱（可选）
cp config/canvas.yml.example config/canvas.yml        # Canvas（可选）
cp config/llm.yml.example config/llm.yml              # 邮件审查用的 LLM（可选）
chmod 600 config/*
python3 app.py                                        # 默认监听 127.0.0.1:8080，可用 HOME_PORTAL_PORT 修改
```

`config/site.json`、`config/schedule.json` 不存在时，会自动使用 `web/assets/` 下的示例文件，所以直接 `python3 app.py` 也能跑起来看效果。

## 部署注意

- **门户本身没有登录功能**，只监听 `127.0.0.1`。放到公网时必须在前面加带认证的反向代理（例如 Caddy/Nginx + Authelia、OAuth2 Proxy），否则任何人都能读写你的笔记和账本。
- `config/` 与 `data/` 里是凭据和个人数据，权限保持 600，不要提交到任何仓库。
- 备份时带上 `data/` 整个目录（笔记、附件、账本、收藏夹、缓存）。

# BiliSurfing · B站冲浪管理器

**让 Bot 真正“看”B 站视频**的 AstrBot 插件：抽帧 + 字幕 + 热评一起注入上下文，而不是只拿到标题和简介。

除了“看视频”，它还带一套冲浪功能：刷视频挑片、发评论/楼中楼、语气记忆（可自动学习）、头像生成，以及一个可视化管理面板。

> 插件 ID：`astrbot_plugin_bili_reader` ｜ 适用 AstrBot `>=4.16,<5` ｜ 平台：`aiocqhttp`（NapCat / SnowLuma 等 OneBot v11 实现）

---

## 功能

- **看视频**：消息里出现 `bilibili.com/video/...`、`b23.tv/...` 或裸 `BV号` 时，自动抓取
  关键画面（短视频下载高清抽帧，长视频回退 B 站剧情图）、标题、简介、CC 字幕、评论区热评，
  注入本次 LLM 请求。模型不用再说“我只看到标题”。
- **刷视频**：`/刷视频 [关键词]` 按最近 N 天搜索，抓字幕片段后让模型挑 1–3 个值得看的。
- **发评论 / 楼中楼**：用户明确要求时，模型可调用 `post_video_comment`、`reply_hot_comment`
  在视频下评论或回复热评（可配置 @ 指定 UID）。
- **自动评论**（可选，默认关闭）：时段内、播放量达标、间隔满足时自动挑视频评论，并私聊通知。
- **语气记忆**：`/记住`、`/记忆`、`/忘记`；另有自动学习，定时从热评归纳 1–2 条语气规则。
- **头像生成**：`draw_avatar` 工具，接 OpenAI 兼容的 `/images/generations`（默认智谱 CogView-3-Flash）。
- **管理面板**：插件卡片里的页面入口，可看运行状态、改配置、管理记忆与浏览/评论记录。

---

## 食用方法

### 1. 准备环境

一台**能出网**的 Linux 机器即可，不必须是云服务器（家里的 NAS、小主机、软路由都行）：

- 2 核 2G 起步足够（抽帧时会有短暂 CPU 峰值）；
- 能访问 `bilibili.com`、`api.bilibili.com`、`i0.hdslb.com`（国内机器直连即可）；
- 磁盘预留 1–2 GB（抽帧缓存会自动清理）。

### 2. 装 AstrBot + OneBot 客户端

按 AstrBot 官方文档用 Docker 部署，并配好一个 `aiocqhttp` 平台（推荐 NapCat 或 SnowLuma）：

```
QQ ←→ OneBot 客户端 ←→ AstrBot ←→ 本插件
```

本插件**不额外开端口**、不需要单独的进程；它跑在 AstrBot 进程里，面板走 AstrBot 自带的 WebUI 端口。

### 3. 安装插件

把本仓库放进 AstrBot 的插件目录：

```bash
git clone https://github.com/ShiroNYami/BiliSurfing.git \
  <AstrBot数据目录>/plugins/astrbot_plugin_bili_reader
```

然后在 AstrBot WebUI 的「插件」页点「重载」，或重启 AstrBot 容器。看到 `BiliSurfing` 已加载即可。

### 4. 配置

在插件卡片点齿轮进入配置，最关键的几项：

| 配置 | 说明 |
|---|---|
| `bili_cookie` | 想读 CC 字幕 / 热评、发评论，必须填。浏览器登录 B 站后复制整段 Cookie（至少含 `SESSDATA`、`bili_jct`、`buvid3`） |
| `owner_qq` | 允许教它、让它发评论的 QQ，多个用逗号分隔；留空则以 AstrBot 管理员为准 |
| `scan_keywords` | `/刷视频` 的默认关键词，逗号分隔 |
| `min_play` | 刷视频/自动评论的最低播放量，默认 10000 |
| `auto_comment_enable` | 自动评论总开关，默认关闭 |
| `comment_hours` | 允许自动评论的时段，含结束小时（如 `8-23` 表示到 23:59） |
| `comment_target_uid` | 评论里 @ 的 B 站 UID，0 表示不 @ |
| `block_words` | 屏蔽词，命中则不评论、不学习 |
| `image_api_key` | 生图 API Key（智谱等），留空则用免费图床兜底 |

### 5. 用起来

| 场景 | 怎么做 |
|---|---|
| 看视频 | 直接发链接 / BV 号 / b23 短链；也可以引用一条含链接的消息再问 |
| 刷视频 | `/刷视频 AI` |
| 让它评论 | 「去评论区留一条，短点」 |
| 回复热评 | 「回复第 2 条：……」 |
| 教语气 | `/记住 说话短一点，别列点` |
| 看状态 / 改配置 | 插件卡片 → 页面入口（管理面板） |

面板里能改全部配置、增删语气记忆、查看浏览/评论记录，保存即时生效，不需要重启。

---

## 前置条件与依赖

| 项目 | 是否必需 | 说明 |
|---|---|---|
| AstrBot `>=4.16,<5` | 必需 | 插件 API 依赖这个版本区间的钩子与工具注册方式 |
| `aiocqhttp` 平台 | 必需 | NapCat / SnowLuma 等 OneBot v11 实现 |
| `ffmpeg` | 必需 | 抽帧用；AstrBot 官方镜像已自带，自行部署需另装 |
| `httpx`、`Pillow` | 必需 | AstrBot 自带；`requirements.txt` 也列了 |
| B 站 Cookie | 可选 | 不填只能看画面和简介；填了才有字幕/热评/评论 |
| 智谱（或其他）生图 Key | 可选 | 只用“画头像”时才需要 |
| Docker | 推荐 | AstrBot 官方部署方式；非 Docker 部署也可，只要依赖齐全 |
| 公网 IP / 域名 | 不需要 | 插件不监听端口；面板复用 AstrBot WebUI |

> 不需要 GPU、不需要本地大模型、不需要向量库。所有理解都交给 AstrBot 配置的 LLM。

---

## 数据存放

运行时数据都在 AstrBot 的数据目录下，**不在插件代码目录里**：

```
data/plugin_data/bili_reader/
├── memory.json          # 语气记忆
├── history.json         # 浏览/评论记录
├── comment_state.json   # 上次评论时间、已评论视频
├── learn_state.json     # 自动学习时间戳
├── frames/              # 抽帧缓存
└── images/              # 生成的头像
```

插件配置由 AstrBot 保存在 `data/config/astrbot_plugin_bili_reader_config.json`。
备份时把 `data/plugin_data/bili_reader/` 和该配置文件一起带走即可。

---

## 常见问题

**Q：为什么评论区“看不到我刚发的评论”？**
B 站对新号、低等级账号的评论会先审核，期间只有自己可见，公开列表和时间倒序都刷不到；等审核通过才公开。

**Q：必须用云服务器吗？**
不必。任何能跑 Docker、能访问 B 站的 Linux 机器都行；家里的小主机同理，只要长期在线。

**Q：抽帧很慢 / 很占流量？**
把 `frame_mode` 改成 `storyboard`，直接用 B 站官方剧情图，通常 1 秒内完成，画质低一些。

**Q：需要给插件单独开端口吗？**
不需要。面板是 AstrBot 进程内的页面与接口，复用 WebUI 端口。

**Q：面板里点视频标题，为什么有时候不开新标签？**
AstrBot 给插件页 iframe 的 `sandbox` 默认不含 `allow-popups`，浏览器会拦掉 `target="_blank"`。
插件会先尝试开新标签，被拦时自动退回复制链接。想直接开新标签，可在宿主机执行一次：

```bash
bash scripts/patch-dashboard-popups.sh astrbot
```

它会给 AstrBot dashboard 的 iframe 补上 `allow-popups`（属于对宿主静态资源的修改，升级/重建容器后需重新执行）。

**Q：支持抖音 / YouTube 吗？**
暂不支持。YouTube 在国内机器通常也不通，插件只做 B 站。

---

## 注意

- **Cookie 是登录凭证**，带写权限时可在你账号下发评论。请只放在自己的服务器上，不要提交到仓库、不要发给别人；泄露后在 B 站退出登录即可作废。
- 自动评论属于写操作，B 站有风控与限流，评论可能“发出但公开列表不可见”。建议先用小号、低频使用，并保留 `block_words` 与时段限制。
- 字幕/热评/画面都来自公开接口，内容版权归原作者；请勿用于批量搬运。

---

## 开发

- 页面只有一份源文件：`pages/panel/index.html`；`panel.py` 只负责读取它。
- 目录：
  - `main.py` 插件入口、命令、工具、调度、Web API
  - `bili.py` B 站接口封装
  - `frames.py` 抽帧与清理
  - `panel.py` 页面加载
- 版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT


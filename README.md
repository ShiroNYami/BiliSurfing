# astrbot_plugin_bili_reader

让 Bot 真正“看”B 站视频的 AstrBot 插件：**抽帧 + 字幕 + 热评**一起注入上下文，而不是只拿标题和简介。

> 适用 AstrBot `>=4.16,<5`，平台 `aiocqhttp`（NapCat / SnowLuma 等 OneBot v11 实现）。

## 功能

- **看视频**：消息里出现 `bilibili.com/video/...`、`b23.tv/...` 或裸 `BV号` 时，自动抓取
  关键画面（短视频下载高清抽帧，长视频回退 B 站剧情图）、标题、简介、CC 字幕、评论区热评，
  注入本次 LLM 请求，模型不用再说“我只看到标题”。
- **刷视频**：`/刷视频 [关键词]` 按最近 N 天搜索，抓字幕片段后让模型挑 1–3 个值得看的。
- **发评论 / 楼中楼**：用户明确要求时，模型可调用 `post_video_comment`、`reply_hot_comment`
  在视频下评论或回复热评（可配置 @ 指定 UID）。
- **自动评论**（可选，默认关闭）：时段内、播放量达标、间隔满足时自动挑视频评论，并私聊通知。
- **语气记忆**：`/记住`、`/记忆`、`/忘记`；另有自动学习，定时从热评归纳 1–2 条语气规则。
- **头像生成**：`draw_avatar` 工具，接 OpenAI 兼容的 `/images/generations`（默认智谱 CogView-3-Flash）。
- **管理面板**：插件卡片里的页面入口，可看状态、改配置、管理记忆与浏览/评论记录。

## 安装

1. 把本目录放到 `<AstrBot数据目录>/plugins/astrbot_plugin_bili_reader/`；
2. 重启 AstrBot（或在插件页点“重载”）；
3. 在插件配置里按需填写 B 站 Cookie、关键词、@ 的 UID 等；
4. 需要评论/字幕时，`bili_cookie` 至少包含 `SESSDATA`、`bili_jct`、`buvid3`。

依赖：`httpx`、`Pillow`（AstrBot 镜像已自带）；抽帧依赖 `ffmpeg`（官方镜像已包含）。

## 使用

| 场景 | 做法 |
|---|---|
| 看视频 | 发链接 / BV号 / b23 短链，或引用一条含链接的消息 |
| 刷视频 | `/刷视频 AI` |
| 让它评论 | 「去评论区留一条，短点」→ 模型调用评论工具 |
| 回复热评 | 「回复第 2 条：……」→ 楼中楼 |
| 教语气 | `/记住 说话短一点，别列点` |
| 管理 | 插件卡片 → 页面入口（管理面板） |

## 配置

主要项（完整说明见插件配置页）：

| 键 | 说明 |
|---|---|
| `bili_cookie` | B 站 Cookie，读字幕/热评、发评论都依赖它；只读用途可只填 `SESSDATA` |
| `frame_mode` | `auto`（默认）/ `storyboard`（快、省流量）/ `video`（清晰、较慢） |
| `max_frames` | 每次附几张画面，默认 6 |
| `min_play` | 刷视频/自动评论的播放量门槛，默认 10000 |
| `scan_keywords` | 默认关键词，逗号分隔 |
| `auto_comment_enable` | 自动评论开关，默认关 |
| `comment_hours` | 允许自动评论的时段，如 `8-22` |
| `comment_target_uid` | 评论里 @ 的 B 站 UID，0 表示不 @ |
| `block_words` | 屏蔽词，命中则不评论/不学 |
| `owner_qq` | 允许教它、让它评论的 QQ；留空以 AstrBot 管理员为准 |
| `image_api_key` | 生图 API Key（智谱等），留空则用免费图床兜底 |

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

## 注意

- **Cookie 是登录凭证**，带写权限时可在你账号下发评论。请只放在自己的服务器上，
  不要提交到仓库、不要发给别人；泄露后在 B 站退出登录即可作废。
- 自动评论属于写操作，B 站对新号/低等级账号有审核与限流，评论可能“发出但公开列表不可见”。
  建议先用小号、低频使用，并保留 `block_words` 与时段限制。
- 字幕/热评/画面都来自公开接口，内容版权归原作者；请勿用于批量搬运。

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

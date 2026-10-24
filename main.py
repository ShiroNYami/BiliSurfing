"""B 站视频阅读插件：把视频画面与字幕注入 LLM 请求，让 Bot 真的“看过”再回答。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

from starlette.responses import HTMLResponse, JSONResponse

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .bili import (
    BARE_BV_RE,
    BiliClient,
    BiliError,
    VideoInfo,
    extract_video_urls,
    fmt_ts,
    fmt_wan,
    looks_like_bili_video,
)
from .frames import crop_storyboard, extract_video_frames, prune_frames
from .panel import page_html


PLUGIN: "BiliReaderPlugin | None" = None


_MD_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)"), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r"\1（\2）"),
]


def strip_markdown(text: str) -> str:
    """把 QQ 不渲染的 Markdown 降级成纯文本，保留内容与换行。"""
    if not text:
        return text
    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?", "", text)
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "· ", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"^\s*[-*_]{3,}\s*$", "", line)
        lines.append(line)
    text = "\n".join(lines)
    for pattern, repl in _MD_INLINE:
        text = pattern.sub(repl, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class VideoContext:
    info: VideoInfo
    frames: list[tuple[int, str]]
    subtitles: list[tuple[float, str]]
    mode: str
    comments: list[dict] = field(default_factory=list)

    def frames_alive(self) -> bool:
        return all(os.path.exists(path) for _ts, path in self.frames)

    def build_prompt(self, max_subtitle_chars: int = 3000, max_desc_chars: int = 400) -> str:
        info = self.info
        lines = ["【视频读取结果（插件实时抓取，可视为你已经看过的内容）】"]
        lines.append(f"标题：{info.page_title}")
        meta = [f"UP 主：{info.owner}"] if info.owner else []
        meta.append(f"时长：{fmt_ts(info.duration)}")
        if info.view:
            meta.append(f"播放：{fmt_wan(info.view)}")
        if info.like:
            meta.append(f"点赞：{fmt_wan(info.like)}")
        if info.pubdate:
            meta.append(time.strftime("%Y-%m-%d", time.localtime(info.pubdate)))
        lines.append(" ｜ ".join(meta))
        lines.append(f"链接：{info.url}")

        desc = (info.desc or "").strip()
        if desc:
            if len(desc) > max_desc_chars:
                desc = desc[:max_desc_chars] + "…"
            lines.append(f"简介：{desc}")

        if self.frames:
            stamps = "、".join(fmt_ts(ts) for ts, _p in self.frames)
            source = "按需下载后高清抽帧" if self.mode == "video" else "B 站剧情图"
            lines.append(
                f"画面：已随本次请求附上 {len(self.frames)} 张按时间顺序排列的截图（{source}），"
                f"时间点依次为 {stamps}。请直接查看这些图片。"
            )
        else:
            lines.append("画面：本次没能取到截图，只能依据标题、简介和字幕判断。")

        if self.subtitles:
            text = "\n".join(f"[{fmt_ts(ts)}] {content}" for ts, content in self.subtitles)
            if len(text) > max_subtitle_chars:
                text = text[:max_subtitle_chars] + "\n…（字幕过长已截断）"
            lines.append("字幕（B 站 CC / UP 主字幕，可能有错字）：")
            lines.append(text)
        else:
            lines.append("字幕：该视频没有可用字幕或未登录拿不到字幕，请主要依据画面判断。")

        if self.comments:
            lines.append("评论区热评（编号可用于回复，不要照抄）：")
            for i, item in enumerate(self.comments, 1):
                lines.append(
                    f"{i}. [{item.get('like', 0)} 赞] {item.get('text', '')}"
                )

        lines.append(
            "请基于以上真实内容回答用户；画面与字幕冲突时以画面为准。"
            "不要再声称自己看不到视频。如果用户只是丢了个链接没提问，就自然地聊聊这个视频。"
            "如果用户想让你参考这些画面画/设计一张头像，可以调用 draw_avatar 工具生成并发给他。"
            "如果用户要你「去评论区留一条评论」，可以调用 post_video_comment 工具，直接发到这条视频下。"
            "如果要回复某条热评（楼中楼），调用 reply_hot_comment 工具，参数用上面的编号。"
        )
        return "\n".join(lines)


@register(
    "astrbot_plugin_bili_reader",
    "dafiyu",
    "发 B 站视频链接时自动抓取画面与字幕，让 Bot 真的“看过”视频再回答。",
    "v0.1.0",
    "https://github.com/",
)
class BiliReaderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        base = os.path.join(get_astrbot_data_path(), "plugin_data", "bili_reader")
        self._cache_dir = os.path.join(base, "cache")
        self._frame_root = os.path.join(base, "frames")
        self._image_dir = os.path.join(base, "images")
        os.makedirs(self._cache_dir, exist_ok=True)
        os.makedirs(self._frame_root, exist_ok=True)
        os.makedirs(self._image_dir, exist_ok=True)
        prune_frames(self._frame_root)

        self._cache: dict[str, tuple[float, VideoContext]] = {}
        self._semaphore = asyncio.Semaphore(2)
        self._locks: dict[str, asyncio.Lock] = {}
        self._draw_last: dict[str, float] = {}
        self._data_dir = base
        self._memories: list[dict] = self._load_memories()
        self._owner_umo: str = self._load_owner_umo()
        self._comment_state: dict = self._load_comment_state()
        self._comment_task: asyncio.Task | None = None
        self._history: list[dict] = self._load_history()
        self._learn_state: dict = self._load_learn_state()
        self._last_video: dict[str, VideoInfo] = {}
        self._last_comments: dict[str, list[dict]] = {}
        self._last_manual_comment: float = 0.0
        self._last_check: dict = {}
        self._log_prune_ts: float = 0.0
        self._at_state: dict = self._load_at_state()
        self._at_last_poll: float = 0.0
        self._register_webui()
        try:
            self._maybe_prune_logs()
        except Exception:  # noqa: BLE001
            pass
        if self._owner_umo:
            try:
                self._ensure_comment_task()
            except Exception:  # noqa: BLE001
                pass
        global PLUGIN
        PLUGIN = self

    # ---------- 配置读取 ----------

    def _get(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self._get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def _get_str(self, key: str, default: str = "") -> str:
        value = self._get(key, default)
        return str(value).strip() if value is not None else default

    # ---------- 长期记忆 ----------

    def _memory_file(self) -> str:
        return os.path.join(self._data_dir, "memory.json")

    def _load_memories(self) -> list[dict]:
        try:
            with open(self._memory_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            return [
                m for m in data if isinstance(m, dict) and str(m.get("text") or "").strip()
            ]
        except Exception:  # noqa: BLE001
            return []

    def _save_memories(self) -> None:
        try:
            tmp = self._memory_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._memories, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._memory_file())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 保存记忆失败：{exc}")

    def _add_memory(self, text: str, source: str = "chat") -> dict | None:
        text = " ".join(str(text).split()).strip()
        if not text:
            return None
        next_id = max([int(m.get("id") or 0) for m in self._memories] or [0]) + 1
        item = {
            "id": next_id,
            "text": text[:400],
            "source": source,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        self._memories.append(item)
        if len(self._memories) > 40:
            self._memories = self._memories[-40:]
        self._save_memories()
        logger.info(f"[bili_reader] 记住 #{item['id']}：{item['text'][:40]}")
        return item

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        owner = self._get_str("owner_qq", "")
        ids = {x.strip() for x in owner.replace("，", ",").split(",") if x.strip()}
        try:
            return str(event.get_sender_id()) in ids
        except Exception:  # noqa: BLE001
            return False

    @filter.on_llm_request()
    async def inject_memories(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        # 语气规则默认只用于「插件自己发评论」，不再注入日常聊天。
        # B 站热评学来的腔调是实验性的，混进 QQ 聊天会把人格带偏。
        if not self._get_bool("inject_memory_in_chat", False):
            return
        if not self._get_bool("memory_enable", True) or not self._memories:
            return
        marker = "【长期记忆·用户教过你的规则】"
        if marker in (req.prompt or ""):
            return
        block = "\n".join(f"- {m['text']}" for m in self._memories[-30:])
        if len(block) > 1600:
            block = block[-1600:]
        req.prompt = ((req.prompt or "") + f"\n\n{marker}\n以下规则请优先遵守：\n{block}").strip()

    @filter.command("刷视频", alias={"刷b站", "刷B站"}, desc="按关键词刷最近的 B 站视频并挑出值得看的（仅主人）")
    async def scan_cmd(self, event: AstrMessageEvent, text: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        keyword = str(text).strip() or self._get_str("scan_keywords", "AI拟人,手绘,整活")
        days = max(1, self._get_int("scan_days", 3))
        limit = max(3, min(self._get_int("scan_limit", 8), 20))

        items: list[dict] = []
        error = ""
        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )

        async def enrich(item: dict) -> None:
            """给候选补一段字幕，便于判断值不值得看。"""
            try:
                info = await client.video_info(
                    f"https://www.bilibili.com/video/{item['bvid']}"
                )
                subs = await client.subtitles(info)
                if subs:
                    text = " ".join(content for _ts, content in subs[:10])
                    item["subtitle"] = text[:220]
            except Exception:  # noqa: BLE001
                item["subtitle"] = ""

        try:
            async with self._semaphore:
                items = await client.search_videos(
                    keyword,
                    limit=limit,
                    days=days,
                    max_seconds=self._get_int("max_video_seconds", 900),
                    min_play=self._get_int("min_play", 10000),
                )
                if items:
                    await asyncio.gather(*(enrich(it) for it in items[:6]))
        except BiliError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            error = f"搜索出错：{exc}"
        finally:
            await client.close()

        if error:
            logger.warning(f"[bili_reader] 刷视频失败：{error}")
            yield event.plain_result(f"没刷成：{error}")
            return

        blocked = [
            w.strip()
            for w in self._get_str("block_words", "").replace("，", ",").split(",")
            if w.strip()
        ]
        if blocked:
            items = [
                it
                for it in items
                if not any(w in (it["title"] + it["desc"]) for w in blocked)
            ]

        if not items:
            yield event.plain_result(f"「{keyword}」最近 {days} 天没刷到合适的视频。")
            return

        candidates = []
        for i, it in enumerate(items, 1):
            self._record_history(
                "scan",
                it["bvid"],
                it["title"],
                int(it.get("play") or 0),
                "",
                f"https://www.bilibili.com/video/{it['bvid']}",
            )
            line = (
                f"{i}. {it['title']}｜UP {it['author']}｜{fmt_ts(it['duration'])}"
                f"｜播放 {fmt_wan(it['play'])}"
            )
            if it["desc"]:
                line += f"｜简介：{it['desc']}"
            if it.get("subtitle"):
                line += f"｜字幕片段：{it['subtitle']}"
            line += f"\n   链接：https://www.bilibili.com/video/{it['bvid']}"
            candidates.append(line)

        prompt = (
            f"我刚用「{keyword}」扫了 B 站最近 {days} 天的新视频，候选如下：\n\n"
            + "\n".join(candidates)
            + "\n\n请从里面挑 1-3 个最值得看的，每个一句话说理由，附上链接；宁缺毋滥。"
            "不要选标题党、广告、政治敏感或低俗内容；如果都不值得，就直说没有。"
        )
        yield event.request_llm(prompt=prompt)

    @filter.on_llm_request()
    async def track_owner_session(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._is_owner(event):
            return
        try:
            new_umo = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            return
        # 通知只走私聊：群里说话不记录会话
        if "FriendMessage" not in new_umo:
            self._ensure_comment_task()
            return
        self._owner_umo = new_umo
        self._save_owner_umo()
        self._ensure_comment_task()

    @filter.command("记住", alias={"记一下"}, desc="让 Bot 记住一条长期规则（仅主人）")
    async def remember_cmd(self, event: AstrMessageEvent, text: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        item = self._add_memory(str(text), "command")
        if not item:
            yield event.plain_result("要记什么？用法：/记住 回复别列点，最多两句")
            return
        yield event.plain_result(f"记住了（#{item['id']}）：{item['text']}")

    @filter.command("忘记", desc="删除一条长期记忆（仅主人）")
    async def forget_cmd(self, event: AstrMessageEvent, text: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        arg = str(text).strip()
        if not self._memories:
            yield event.plain_result("目前没有记忆。")
            return
        if arg in {"全部", "all", "*"}:
            self._memories = []
            self._save_memories()
            yield event.plain_result("记忆已清空。")
            return
        try:
            mid = int(arg.lstrip("#"))
        except ValueError:
            yield event.plain_result("用法：/忘记 3（编号）或 /忘记 全部")
            return
        kept = [m for m in self._memories if int(m.get("id") or 0) != mid]
        if len(kept) == len(self._memories):
            yield event.plain_result(f"没找到 #{mid}。")
            return
        self._memories = kept
        self._save_memories()
        yield event.plain_result(f"已忘记 #{mid}。")

    @filter.command("记忆", desc="查看当前长期记忆")
    async def list_memories_cmd(self, event: AstrMessageEvent):
        if not self._memories:
            yield event.plain_result("还没有记忆。用 /记住 内容 来教我。")
            return
        lines = [f"#{m['id']} {m['text']}" for m in self._memories]
        yield event.plain_result("现在的长期记忆：\n" + "\n".join(lines))

    @filter.command("测试通知", desc="测试自动评论通知能否发到私聊（仅主人）")
    async def test_notify_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这条只认主人哦。")
            return
        sent = await self._send_owner("测试通知：自动评论结果会私聊发到这里")
        yield event.plain_result(
            "已私聊发送" if sent else f"私聊发送失败，当前会话：{self._owner_umo or '未记录'}"
        )

    async def _tool_remember(self, event: AstrMessageEvent, text: str) -> str:
        """当用户明确要求你「记住」某条长期偏好或规则时调用，把它保存下来。

        Args:
            text(string): 要记住的规则，精炼成一句祈使句，例如「回复不要列点，最多两句话」。
        Returns:
            保存结果说明。
        """
        if not self._is_owner(event):
            return "只有主人可以修改长期记忆，这次没有保存。"
        item = self._add_memory(text, "tool")
        if not item:
            return "内容为空，没有保存。"
        return f"已记住（#{item['id']}）：{item['text']}"

    # ---------- 管理面板 ----------

    _PANEL_KEYS = (
        "scan_keywords",
        "comment_hours",
        "comment_interval_minutes",
        "min_play",
        "comment_target_uid",
        "comment_target_name",
        "block_words",
        "auto_comment_enable",
    )

    def _history_file(self) -> str:
        return os.path.join(self._data_dir, "history.json")

    def _load_history(self) -> list[dict]:
        try:
            with open(self._history_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            return [x for x in data if isinstance(x, dict)]
        except Exception:  # noqa: BLE001
            return []

    def _save_history(self) -> None:
        try:
            tmp = self._history_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._history, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._history_file())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 保存历史失败：{exc}")

    def _record_history(
        self,
        kind: str,
        bvid: str,
        title: str,
        play: int = 0,
        comment: str = "",
        url: str = "",
    ) -> None:
        self._history.append(
            {
                "kind": kind,
                "bvid": bvid,
                "title": title,
                "play": play,
                "comment": comment,
                "url": url or f"https://www.bilibili.com/video/{bvid}",
                "time": time.strftime("%Y-%m-%d %H:%M"),
            }
        )
        self._history = self._history[-300:]
        self._save_history()

    def _config_file(self) -> str:
        return os.path.join(
            get_astrbot_data_path(),
            "config",
            "astrbot_plugin_bili_reader_config.json",
        )

    def _public_config(self) -> dict:
        disk: dict = {}
        try:
            with open(self._config_file(), encoding="utf-8-sig") as fp:
                disk = json.load(fp)
        except Exception:  # noqa: BLE001
            disk = {}
        out = {}
        for key in self._PANEL_KEYS:
            out[key] = disk[key] if key in disk else self._get(key, "")
        return out

    def _save_panel_config(self, updates: dict) -> None:
        try:
            self.config.update(updates)
        except Exception:  # noqa: BLE001
            pass
        path = self._config_file()
        saved = False
        # 优先用 AstrBot 自己的保存方法，避免它之后用旧副本覆盖我们的文件
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] save_config 失败：{exc}")
        try:
            with open(path, encoding="utf-8-sig") as fp:
                cfg = json.load(fp)
        except Exception:  # noqa: BLE001
            cfg = dict(self.config) if isinstance(self.config, dict) else {}
        cfg.update(updates)
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(cfg, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
            saved = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 写配置文件失败：{exc}")
        logger.info(f"[bili_reader] 面板保存 {sorted(updates)} -> {path} ok={saved}")

    def _register_webui(self) -> None:
        base = "/astrbot_plugin_bili_reader"
        routes = (
            (f"{base}/panel", self._api_panel, ["GET"], "B站冲浪管理面板"),
            (f"{base}/state", self._api_state, ["GET"], "读取配置与记录"),
            (f"{base}/config", self._api_config, ["POST"], "保存配置"),
            (f"{base}/config/<path:payload>", self._api_config, ["GET"], "保存配置"),
            (f"{base}/memory", self._api_memory, ["POST"], "语气记忆管理"),
            (f"{base}/memory/<path:payload>", self._api_memory, ["GET"], "语气记忆管理"),
            (f"{base}/history", self._api_history, ["POST"], "浏览记录管理"),
            (f"{base}/history/<path:payload>", self._api_history, ["GET"], "浏览记录管理"),
        )
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 注册 Web API 失败 {route}: {exc}")

    async def _api_panel(self, req=None, **kwargs):
        return HTMLResponse(page_html())

    async def _api_state(self, req=None, **kwargs):
        start_hour, end_hour = self._comment_window()
        last = float(self._comment_state.get("last_ts") or 0)
        interval = max(15, self._get_int("comment_interval_minutes", 60)) * 60
        return JSONResponse(
            {
                "config": self._public_config(),
                "status": {
                    "now": time.strftime("%Y-%m-%d %H:%M"),
                    "window": (
                        f"{start_hour}:00-{end_hour}:59"
                        if start_hour <= end_hour
                        else f"{start_hour}:00-次日{end_hour}:59"
                    ),
                    "in_window": self._hour_in_window(
                        start_hour, end_hour, time.localtime().tm_hour
                    ),
                    "last_comment": time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
                    if last
                    else "",
                    "next_allowed": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(last + interval)
                    )
                    if last
                    else "",
                    "commented_count": len(self._comment_state.get("commented") or []),
                    "last_check": self._last_check.get("result", ""),
                    "last_check_at": self._last_check.get("time", ""),
                    "last_check_detail": self._last_check.get("detail", ""),
                },
                "history": list(reversed(self._history[-100:])),
                "memories": self._memories,
            }
        )

    async def _read_payload(self, req, payload: str | None = None) -> dict:
        """读取面板提交的数据：优先路径里的 base64，其次 body，最后 query。"""
        data = {}
        if payload:
            try:
                pad = "=" * (-len(payload) % 4)
                data = json.loads(
                    base64.urlsafe_b64decode(payload + pad).decode("utf-8")
                )
            except Exception:  # noqa: BLE001
                data = {}
        if not data and req is not None and hasattr(req, "json"):
            try:
                data = await req.json(default={})
            except Exception:  # noqa: BLE001
                data = {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:  # noqa: BLE001
                data = {}
        if not data:
            query = getattr(req, "query", None)
            raw = ""
            try:
                raw = query.get("payload") if query is not None else ""
            except Exception:  # noqa: BLE001
                raw = ""
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:  # noqa: BLE001
                    data = {}
        return data if isinstance(data, dict) else {}

    async def _api_config(self, req=None, payload=None, **kwargs):
        data = await self._read_payload(req, payload)
        logger.info(f"[bili_reader] 面板保存请求：{data}")
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)
        if isinstance(data.get("config"), dict):
            data = data["config"]
        updates: dict = {}
        for key in self._PANEL_KEYS:
            if key not in data:
                continue
            value = data[key]
            if key in {"comment_interval_minutes", "min_play", "comment_target_uid"}:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif key == "auto_comment_enable":
                value = bool(value)
            else:
                value = str(value).strip()
            updates[key] = value
        if updates:
            self._save_panel_config(updates)
        return JSONResponse({"ok": True, "config": self._public_config()})

    async def _api_memory(self, req=None, payload=None, **kwargs):
        data = await self._read_payload(req, payload)
        action = str(data.get("action") or "")
        if action == "add":
            item = self._add_memory(str((data or {}).get("text") or ""), "panel")
            return JSONResponse({"ok": bool(item), "memories": self._memories})
        if action == "delete":
            try:
                mid = int((data or {}).get("id"))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
            self._memories = [m for m in self._memories if int(m.get("id") or 0) != mid]
            self._save_memories()
            return JSONResponse({"ok": True, "memories": self._memories})
        if action == "clear":
            self._memories = []
            self._save_memories()
            return JSONResponse({"ok": True, "memories": []})
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)

    async def _api_history(self, req=None, payload=None, **kwargs):
        data = await self._read_payload(req, payload)
        if data.get("action") == "clear":
            self._history = []
            self._save_history()
        return JSONResponse({"ok": True, "history": list(reversed(self._history[-100:]))})

    # ---------- 自动评论 ----------

    def _comment_state_file(self) -> str:
        return os.path.join(self._data_dir, "comment_state.json")

    def _load_comment_state(self) -> dict:
        try:
            with open(self._comment_state_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_comment_state(self) -> None:
        try:
            tmp = self._comment_state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._comment_state, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._comment_state_file())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 保存评论状态失败：{exc}")

    def _comment_window(self) -> tuple[int, int]:
        raw = (
            self._get_str("comment_hours", "8-23")
            .replace("：", "-")
            .replace("~", "-")
            .replace("—", "-")
        )
        try:
            start, end = raw.split("-")
            return max(0, int(start)), min(24, int(end))
        except Exception:  # noqa: BLE001
            return 8, 23

    @staticmethod
    def _hour_in_window(start: int, end: int, hour: int) -> bool:
        """判断当前小时是否在时段内；start > end 表示跨凌晨。"""
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    def _blocked_words(self) -> list[str]:
        raw = self._get_str("block_words", "").replace("，", ",")
        return [w.strip() for w in raw.split(",") if w.strip()]

    def _maybe_prune_logs(self) -> None:
        """每天最多一次，清理 AstrBot 日志目录里超过保留期的文件。"""
        now = time.time()
        if now - float(getattr(self, "_log_prune_ts", 0) or 0) < 86400:
            return
        self._log_prune_ts = now
        days = max(1, self._get_int("log_retention_days", 15))
        root = os.path.join(get_astrbot_data_path(), "logs")
        cutoff = now - days * 86400
        removed = 0
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    continue
        if removed:
            logger.info(
                f"[bili_reader] 清理了 {removed} 个超过 {days} 天的日志文件（{root}）"
            )

    def _owner_file(self) -> str:
        return os.path.join(self._data_dir, "owner.json")

    def _load_owner_umo(self) -> str:
        try:
            with open(self._owner_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            return str(data.get("umo") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _save_owner_umo(self) -> None:
        try:
            with open(self._owner_file(), "w", encoding="utf-8") as fp:
                json.dump({"umo": self._owner_umo}, fp, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    def _note_check(self, result: str, detail: str = "") -> None:
        """记录一次自动评论检查的结果，便于在面板/日志里看“卡在哪一步”。"""
        now = time.time()
        prev = self._last_check or {}
        changed = prev.get("result") != result
        self._last_check = {
            "ts": now,
            "time": time.strftime("%m-%d %H:%M", time.localtime(now)),
            "result": result,
            "detail": detail,
        }
        # 结果变化时立即记一条；否则每 30 分钟记一次，避免刷屏
        if changed or now - float(prev.get("logged_at") or 0) >= 1800:
            logger.info(f"[bili_reader] 自动评论检查：{result}{('｜' + detail) if detail else ''}")
            self._last_check["logged_at"] = now

    async def _send_owner(self, text: str) -> bool:
        """只发私聊；没有私聊会话就不发，绝不退群。"""
        if not self._owner_umo or "FriendMessage" not in self._owner_umo:
            logger.info(
                "[bili_reader] 没有私聊会话，通知跳过（私聊 Bot 发一条消息即可建立）"
            )
            return False
        try:
            return bool(
                await self.context.send_message(
                    self._owner_umo, MessageChain([Plain(text)])
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 私聊通知发送失败：{exc}")
            return False

    def _self_mid(self) -> str:
        cookie = self._get_str("bili_cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "DedeUserID":
                return value.strip()
        return ""

    async def _verify_comment(self, info: VideoInfo, rpid: str) -> None:
        """发完 10 分钟回查一次公开列表，只查一次，不轮询。"""
        await asyncio.sleep(600)
        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )
        try:
            found = await client.find_comment(
                info, rpid=str(rpid), mid=self._self_mid()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 评论回查失败：{exc}")
            return
        finally:
            await client.close()
        if found:
            await self._send_owner(f"评论已公开：{info.title}\n{info.url}")
        else:
            await self._send_owner(
                "评论发出 10 分钟后仍不在公开列表，大概率在审核（仅自己可见）："
                f"{info.title}\n{info.url}"
            )

    def _at_state_file(self) -> str:
        return os.path.join(self._data_dir, "at_state.json")

    def _load_at_state(self) -> dict:
        try:
            with open(self._at_state_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_at_state(self) -> None:
        try:
            with open(self._at_state_file(), "w", encoding="utf-8") as fp:
                json.dump(self._at_state, fp, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    async def _maybe_at_reply(self) -> None:
        """轮询「@我的」，只回复指定 UID 的 @，每天有上限。"""
        if not self._get_bool("at_reply_enable", False):
            return
        target = self._get_int("at_target_uid", 0)
        if not target:
            return
        now = time.time()
        if now - self._at_last_poll < max(1, self._get_int("at_poll_minutes", 5)) * 60:
            return
        self._at_last_poll = now

        today = time.strftime("%Y-%m-%d")
        if self._at_state.get("day") != today:
            self._at_state["day"] = today
            self._at_state["count"] = 0
        if int(self._at_state.get("count") or 0) >= max(1, self._get_int("at_daily_max", 10)):
            return

        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )
        try:
            items = await client.at_items()
            last_id = int(self._at_state.get("last_id") or 0)
            if last_id == 0 and items:
                # 首次运行只标记当前位置，避免把历史 @ 全部回复一遍
                self._at_state["last_id"] = max(int(x.get("id") or 0) for x in items)
                self._save_at_state()
                logger.info("[bili_reader] 首次记录 @ 游标，跳过历史消息")
                return
            fresh = [
                it
                for it in items
                if int(it.get("id") or 0) > last_id
                and str((it.get("user") or {}).get("mid")) == str(target)
            ]
            if not fresh:
                return
            fresh.sort(key=lambda x: int(x.get("id") or 0))
            for item in fresh:
                self._at_state["last_id"] = int(item.get("id") or 0)
                await self._handle_at(item, client)
                self._save_at_state()
        finally:
            await client.close()

    async def _handle_at(self, item: dict, client: BiliClient) -> None:
        info = item.get("item") or {}
        oid = info.get("subject_id") or info.get("oid")
        # B 站 @ 消息里，被 @ 的那条评论 rpid 在 source_id；root_id/target_id 为 0
        rpid = info.get("source_id") or info.get("target_id") or info.get("root_id")
        message = (
            info.get("source_content")
            or ((item.get("reply") or {}).get("content") or {}).get("message")
            or ""
        )
        if not oid or not rpid:
            logger.info(
                "[bili_reader] @记录字段不全，跳过："
                + json.dumps(item, ensure_ascii=False)[:300]
            )
            return

        provider = await self.context.get_using_provider_async(self._owner_umo or None)
        reply = ""
        if provider:
            prompt = (
                f"有人在 B 站视频的评论区 @ 了你，说：{message[:200]}\n"
                "请写一句不超过 30 字的回复：口语、友好，可以带一点轻松调侃；"
                "不要提 AI、机器人、自动、插件；不要涉及政治、色情、引战、广告。"
                "只输出回复本身，不要引号。"
            )
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是 B 站上的普通用户，回复只有一句话。",
                )
                reply = (resp.completion_text or "").strip().strip('"“”')
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 生成 @ 回复失败：{exc}")
        if not reply:
            return
        if any(w in reply for w in self._blocked_words()):
            return
        try:
            data = await client.reply_comment_aid(int(oid), str(rpid), str(rpid), reply[:500])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 回复 @ 失败：{exc}")
            return
        self._at_state["count"] = int(self._at_state.get("count") or 0) + 1
        await self._send_owner(
            f"有人在视频评论区 @ 你，已回复：{reply}\n"
            f"https://www.bilibili.com/video/av{oid}#reply{rpid}"
        )
        logger.info(f"[bili_reader] 已回复 @，rpid={data.get('rpid')}")

    def _ensure_comment_task(self) -> None:
        if self._comment_task is None or self._comment_task.done():
            self._comment_task = asyncio.create_task(self._comment_loop())

    async def _comment_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await self._maybe_comment()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 自动评论异常：{exc}")
            try:
                await self._maybe_at_reply()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] @回复异常：{exc}")
            try:
                await self._maybe_learn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 语气学习异常：{exc}")
            try:
                self._maybe_prune_logs()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 清理日志失败：{exc}")
            await asyncio.sleep(300)

    async def _maybe_comment(self) -> None:
        if not self._get_bool("auto_comment_enable", False):
            self._note_check("开关关闭", "在面板/配置里打开「自动评论」后才会跑")
            return
        if not self._owner_umo:
            self._note_check("没有会话", "主人先在 QQ 里发一条消息，才能把通知推给你")
            return
        start_hour, end_hour = self._comment_window()
        hour = time.localtime().tm_hour
        if not self._hour_in_window(start_hour, end_hour, hour):
            self._note_check("不在时段", f"当前 {hour} 点，时段 {start_hour}-{end_hour}")
            return
        interval = max(15, self._get_int("comment_interval_minutes", 60)) * 60
        gap = time.time() - float(self._comment_state.get("last_ts") or 0)
        if gap < interval:
            nxt = time.strftime(
                "%H:%M", time.localtime(time.time() + (interval - gap))
            )
            self._note_check("距上次评论太近", f"最早 {nxt} 才会再发")
            return

        keywords = [
            k.strip()
            for k in self._get_str("scan_keywords", "AI拟人,手绘,整活")
            .replace("，", ",")
            .split(",")
            if k.strip()
        ]
        keyword = random.choice(keywords or ["AI"])
        blocked = self._blocked_words()
        done = set(self._comment_state.get("commented") or [])
        min_play = self._get_int("min_play", 10000)
        days = max(1, self._get_int("scan_days", 3))

        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )
        try:
            items: list[dict] = []
            tried: list[str] = []
            # 一次最多试 3 个关键词，避免抽到冷门词就整轮放弃
            for kw in random.sample(keywords or ["AI"], min(3, max(1, len(keywords)))):
                tried.append(kw)
                items = await client.search_videos(
                    kw,
                    limit=8,
                    days=days,
                    max_seconds=900,
                    min_play=min_play,
                )
                if items:
                    keyword = kw
                    break
            if not items:
                self._note_check(
                    "没有候选视频",
                    f"试了 {'/'.join(tried)}，近 {days} 天播放≥{min_play} 的视频 0 个",
                )
                return
            items = [
                it
                for it in items
                if it["bvid"] not in done
                and not any(w in (it["title"] + it["desc"]) for w in blocked)
            ]
            if not items:
                self._note_check("候选都被过滤", "都评论过，或命中屏蔽词")
                return

            no_sub = 0
            no_comment = 0
            for it in items[:6]:
                info = await client.video_info(
                    f"https://www.bilibili.com/video/{it['bvid']}"
                )
                subs = await client.subtitles(info)
                if not subs:
                    no_sub += 1
                    continue
                comment = await self._make_comment(info, subs)
                if not comment:
                    no_comment += 1
                    continue
                target = self._get_str("comment_target_name", "") or await client.user_name(
                    self._get_int("comment_target_uid", 0)
                )
                message = (f"@{target} {comment}" if target else comment)[:900]
                data = await client.post_comment(info, message)
                rpid = data.get("rpid") or ""
                members = ((data.get("reply") or {}).get("content") or {}).get("members") or []
                self._comment_state["last_ts"] = time.time()
                self._comment_state["commented"] = (list(done) + [info.bvid])[-200:]
                self._save_comment_state()
                self._record_history(
                    "comment", info.bvid, info.title, int(it.get("play") or 0), comment, info.url
                )
                self._note_check("已评论", f"{info.bvid}《{info.title[:20]}》rpid={rpid}")
                if rpid:
                    asyncio.create_task(self._verify_comment(info, rpid))
                logger.info(
                    f"[bili_reader] 已评论 {info.bvid} rpid={rpid} 提到={len(members)} 条"
                )
                text = f"在《{info.title}》评论了：{comment}\n{info.url}"
                await self._send_owner(text)
                return

            self._note_check(
                "候选不合适",
                f"共 {len(items[:6])} 个候选：无字幕 {no_sub} 个，评论生成失败 {no_comment} 个",
            )
        except Exception as exc:  # noqa: BLE001
            self._note_check("搜索或发送出错", str(exc)[:100])
        finally:
            await client.close()

    async def _make_comment(self, info: VideoInfo, subs: list[tuple[float, str]]) -> str:
        provider = await self.context.get_using_provider_async(self._owner_umo)
        if not provider:
            return ""
        snippet = " ".join(text for _ts, text in subs[:12])[:300]
        prompt = (
            f"你在 B 站刷到一个视频：{info.title}（UP 主：{info.owner}）。\n"
            f"字幕片段：{snippet}\n\n"
            "请以普通网友的身份写一条不超过 30 字的评论：口语、具体、有信息量，"
            "可以夸一句或提一个细节。不要提 AI、机器人、自动、插件，不要用 @，"
            "不要涉及政治、色情、暴力、引战、广告、抽奖。只输出评论本身，不要引号。"
        )
        if self._get_bool("memory_enable", True) and self._memories:
            rules = "\n".join(f"- {m['text']}" for m in self._memories[-20:])
            prompt += f"\n\n【平时攒下的说话习惯，参考语气，别照念】\n{rules}"
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是 B 站上说话简短自然的普通用户，回复只有一句评论。",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 生成评论失败：{exc}")
            return ""
        text = (resp.completion_text or "").strip().strip('"“”')
        text = re.sub(r"@\S+\s*", "", text).strip()
        if not text or len(text) > 60:
            return ""
        if any(w in text for w in self._blocked_words()):
            return ""
        return text

    # ---------- 语气自动学习 ----------

    def _learn_state_file(self) -> str:
        return os.path.join(self._data_dir, "learn_state.json")

    def _load_learn_state(self) -> dict:
        try:
            with open(self._learn_state_file(), encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_learn_state(self) -> None:
        try:
            tmp = self._learn_state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._learn_state, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._learn_state_file())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 保存学习状态失败：{exc}")

    def _is_duplicate_rule(self, text: str) -> bool:
        for item in self._memories:
            old = str(item.get("text") or "")
            if not old:
                continue
            if text in old or old in text:
                return True
        return False

    async def _maybe_learn(self) -> None:
        if not self._get_bool("auto_learn_enable", True):
            return
        interval = max(1, self._get_int("auto_learn_interval_hours", 12)) * 3600
        if time.time() - float(self._learn_state.get("last_ts") or 0) < interval:
            return
        auto_count = sum(1 for m in self._memories if m.get("source") == "auto")
        if auto_count >= max(1, self._get_int("auto_learn_max", 8)):
            return

        seen: list[str] = []
        for item in self._history[-20:]:
            bvid = str(item.get("bvid") or "")
            if bvid and bvid not in seen:
                seen.append(bvid)
        bvids = seen[-4:]
        if not bvids:
            return

        samples: list[str] = []
        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )
        try:
            for bvid in bvids:
                try:
                    info = await client.video_info(
                        f"https://www.bilibili.com/video/{bvid}"
                    )
                    items = await client.comments(info, 8)
                except Exception:  # noqa: BLE001
                    continue
                samples.extend(item["text"] for item in items)
        finally:
            await client.close()

        samples = [s for s in samples if 6 <= len(s) <= 80][:20]
        if len(samples) < 5:
            return

        rules = await self._distill_rules(samples)
        added = 0
        for rule in rules:
            rule = rule.strip().strip("。；;")
            if not (4 <= len(rule) <= 40):
                continue
            if any(w in rule for w in self._blocked_words()):
                continue
            if self._is_duplicate_rule(rule):
                continue
            item = self._add_memory(rule, "auto")
            if item:
                added += 1
            if added >= 2:
                break

        self._learn_state["last_ts"] = time.time()
        self._save_learn_state()
        if added:
            logger.info(f"[bili_reader] 自动学到 {added} 条语气规则")

    async def _distill_rules(self, comments: list[str]) -> list[str]:
        provider = await self.context.get_using_provider_async(self._owner_umo)
        if not provider:
            return []
        sample_text = "\n".join(f"- {c}" for c in comments)
        prompt = (
            "下面是从 B 站视频评论区摘出来的真实评论。请归纳出 1-2 条「说话语气」上的可复用规则，"
            "用来让聊天更像真人。要求：只描述语气、句式、节奏，不要具体梗、人名、事件；"
            "不鼓励攻击、阴阳怪气、脏话；每条不超过 30 字，用祈使句。\n\n"
            f"{sample_text}\n\n"
            "只输出 JSON 字符串数组，例如 [\"多用短句，少用书面语\"]，不要解释。"
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是语言风格分析器，只输出合法 JSON 数组。",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 归纳语气失败：{exc}")
            return []
        raw = (resp.completion_text or "").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except Exception:  # noqa: BLE001
            return []
        return [str(x) for x in data if isinstance(x, str)]

    # ---------- 消息解析 ----------

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        parts: list[str] = [event.get_message_str() or ""]
        try:
            for comp in event.get_messages():
                # 引用回复（QQ 的“回复”）：链接常常藏在被引用的那条消息里
                quoted = getattr(comp, "message_str", None)
                if quoted:
                    parts.append(str(quoted))
                for sub in getattr(comp, "chain", None) or []:
                    sub_text = getattr(sub, "text", None)
                    if sub_text:
                        parts.append(str(sub_text))
                payload = getattr(comp, "data", None)
                if not payload:
                    continue
                try:
                    dumped = json.dumps(payload, ensure_ascii=False, default=str)
                except Exception:  # noqa: BLE001
                    dumped = str(payload)
                # QQ 的分享卡片（小程序 json）里通常藏着 jumpUrl / qqdocurl
                parts.append(dumped[:4000])
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(p for p in parts if p)

    # ---------- 主流程 ----------

    def _normalize_reasoning_effort(self) -> None:
        """WebUI 的推理强度下拉会写入首字母大写的 Low/High，而 DeepSeek 只认小写。

        这里在每次请求前把供应商配置里的取值改回小写，避免整条请求 422。
        """
        try:
            manager = getattr(self.context, "provider_manager", None)
            insts = list(getattr(manager, "provider_insts", []) or [])
        except Exception:  # noqa: BLE001
            return
        for inst in insts:
            cfg = getattr(inst, "provider_config", None)
            if not isinstance(cfg, dict):
                continue
            extra = cfg.get("custom_extra_body")
            if not isinstance(extra, dict):
                continue
            value = extra.get("reasoning_effort")
            if isinstance(value, str) and value != value.lower():
                extra["reasoning_effort"] = value.lower()
                logger.info(
                    f"[bili_reader] reasoning_effort {value!r} 已自动纠正为 {value.lower()!r}"
                )

    @filter.on_llm_request()
    async def normalize_reasoning_effort(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        self._normalize_reasoning_effort()

    @filter.on_decorating_result()
    async def clean_markdown(self, event: AstrMessageEvent) -> None:
        """QQ 不渲染 Markdown，发送前把 **、##、反引号等降级成纯文本。"""
        if not self._get_bool("clean_markdown", True):
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        if not result or not getattr(result, "chain", None):
            return
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                cleaned = strip_markdown(comp.text)
                if cleaned != comp.text:
                    comp.text = cleaned

    @filter.on_llm_request()
    async def inject_quoted_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """把「引用回复」的内容带进上下文，避免模型看不见用户在回复什么。"""
        text = (event.get_message_str() or "").strip()
        quoted = ""
        try:
            for comp in event.get_messages():
                value = getattr(comp, "message_str", None)
                if value:
                    quoted = str(value)
                    break
        except Exception:  # noqa: BLE001
            quoted = ""
        additions: list[str] = []
        try:
            woke = bool(event.is_wake_up())
        except Exception:  # noqa: BLE001
            woke = False
        if woke:
            additions.append(
                "（这条消息 @ 的是你，用户是在问你本人，不是在转述别人的对话；"
                "即使消息里还 @ 了别人，也以你为回答对象。）"
            )
        if quoted:
            additions.append(f"（用户引用了这条消息：{quoted[:300]}）")
        if not text and quoted:
            additions.append(
                "用户这条只 @ 了你，没有写正文。请结合被引用的内容和最近几轮对话判断他想让你继续做什么，"
                "不要反问「戳我干嘛」「怎么不说话」。"
            )
        if additions:
            req.prompt = ((req.prompt or "") + "\n\n" + "\n".join(additions)).strip()

    @filter.on_llm_request()
    async def inject_video_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._get_bool("enable", True):
            return
        if event.get_extra("bili_reader_handled"):
            return
        text = self._message_text(event)
        if not looks_like_bili_video(text):
            return

        urls = extract_video_urls(text)
        if not urls:
            bv = BARE_BV_RE.search(text)
            if not bv:
                return
            urls = [f"https://www.bilibili.com/video/{bv.group(1)}"]
        url = urls[0]
        event.set_extra("bili_reader_handled", True)

        try:
            ctx = await asyncio.wait_for(self._load(url), timeout=180)
        except asyncio.TimeoutError:
            self._inject_failure(req, "解析超时")
            return
        except BiliError as exc:
            logger.warning(f"[bili_reader] 解析失败 {url}: {exc}")
            self._inject_failure(req, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[bili_reader] 未预期错误 {url}: {exc}")
            self._inject_failure(req, f"内部错误：{exc}")
            return

        req.prompt = ((req.prompt or "") + "\n\n" + ctx.build_prompt()).strip()
        self._last_video[event.unified_msg_origin] = ctx.info
        if ctx.comments:
            self._last_comments[event.unified_msg_origin] = ctx.comments
        image_paths = [path for _ts, path in ctx.frames]
        if image_paths:
            existing = list(getattr(req, "image_urls", None) or [])
            req.image_urls = existing + image_paths
        logger.info(
            f"[bili_reader] {ctx.info.bvid} 注入完成：{len(image_paths)} 张图，"
            f"{len(ctx.subtitles)} 行字幕，模式 {ctx.mode}"
        )

    @staticmethod
    def _inject_failure(req: ProviderRequest, reason: str) -> None:
        req.prompt = (
            (req.prompt or "")
            + "\n\n【视频读取失败】"
            + reason
            + "。请如实告诉用户这次没能读到视频内容，不要凭空描述画面。"
        ).strip()

    async def _load(self, url: str) -> VideoContext:
        cache_minutes = self._get_int("cache_minutes", 30)
        cached = self._cache.get(url)
        if cached and cache_minutes > 0:
            ts, ctx = cached
            if time.time() - ts < cache_minutes * 60 and ctx.frames_alive():
                return ctx

        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._cache.get(url)
            if cached and cache_minutes > 0:
                ts, ctx = cached
                if time.time() - ts < cache_minutes * 60 and ctx.frames_alive():
                    return ctx
            async with self._semaphore:
                ctx = await self._parse(url)
            if cache_minutes > 0:
                self._cache[url] = (time.time(), ctx)
            return ctx

    async def _parse(self, url: str) -> VideoContext:
        max_frames = max(1, min(self._get_int("max_frames", 6), 12))
        max_duration = self._get_int("max_duration_seconds", 3600)
        max_video_seconds = self._get_int("max_video_seconds", 900)
        max_download = max(5, self._get_int("max_download_mb", 60)) * 1024 * 1024
        mode_cfg = self._get_str("frame_mode", "auto").lower()
        include_subs = self._get_bool("include_subtitles", True)
        sessdata = self._get_str("sessdata", "")

        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=sessdata
        )
        tmp_video = os.path.join(self._cache_dir, f"src_{int(time.time() * 1000)}.mp4")
        try:
            info = await client.video_info(url)
            if max_duration and info.duration > max_duration:
                raise BiliError(f"视频太长（{fmt_ts(info.duration)}），超过解析上限")

            sub_dir = os.path.join(self._frame_root, f"{info.bvid}_p{info.page_index}")
            old_files = os.listdir(sub_dir) if os.path.isdir(sub_dir) else []
            for name in old_files:
                try:
                    os.remove(os.path.join(sub_dir, name))
                except OSError:
                    pass

            frames: list[tuple[int, str]] = []
            mode = "storyboard"
            want_video = mode_cfg == "video" or (
                mode_cfg == "auto" and 0 < info.duration <= max_video_seconds
            )

            if want_video and info.cid:
                try:
                    urls = await client.playurl(info)
                    last_error: Exception | None = None
                    for candidate in urls[:2]:
                        try:
                            await client.download(candidate, tmp_video, max_download)
                            frames = await extract_video_frames(
                                tmp_video, info.duration, max_frames, sub_dir, info.bvid
                            )
                            mode = "video"
                            break
                        except Exception as exc:  # noqa: BLE001
                            last_error = exc
                            continue
                    if not frames and last_error:
                        raise last_error
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_reader] 下载抽帧失败，回退剧情图：{exc}")
                    frames = []

            if not frames and info.cid:
                try:
                    board = await client.storyboard(info)
                    if board:
                        sprite, stamps, grid_x, grid_y = board
                        frames = crop_storyboard(
                            sprite, stamps, grid_x, grid_y, max_frames, sub_dir, info.bvid
                        )
                        mode = "storyboard"
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_reader] 剧情图抽帧失败：{exc}")

            subtitles: list[tuple[float, str]] = []
            if include_subs and info.cid:
                try:
                    subtitles = await client.subtitles(info)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_reader] 取字幕失败：{exc}")

            comments: list[dict] = []
            if self._get_bool("include_comments", True) and info.aid:
                try:
                    comments = await client.comments(
                        info, max(1, min(self._get_int("max_comments", 6), 10))
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_reader] 取评论失败：{exc}")

            if not frames and not subtitles:
                raise BiliError("没有取到任何画面或字幕")

            prune_frames(self._frame_root)
            return VideoContext(
                info=info,
                frames=frames,
                subtitles=subtitles,
                mode=mode,
                comments=comments,
            )
        finally:
            await client.close()
            if os.path.exists(tmp_video):
                try:
                    os.remove(tmp_video)
                except OSError:
                    pass

    async def _tool_draw(self, event: AstrMessageEvent, prompt: str) -> str:
        """根据画面描述生成一张卡通头像/插画，并直接发送给用户。

        当用户要求「设计一个头像」「画一张图」「照着视频里的风格画一个」时调用本工具。
        Args:
            prompt(string): 画面描述，中文即可，写清主体、表情、配色、道具和构
                图。例如「蓝色圆脑袋鱼头，半睁死鱼眼，歪戴迷你纸皇冠，旁边飘个小闪电，手举牌子」。
        Returns:
            生成结果说明。图片会由工具直接发出，不要重复贴图片或复述提示词。
        """
        if not self._get_bool("enable_draw", True):
            return "图片生成功能已在插件配置里关闭。"
        text = (prompt or "").strip()
        if not text:
            return "缺少画面描述，无法生成。"

        now = time.time()
        sender = str(event.get_sender_id())
        if now - self._draw_last.get(sender, 0) < 30:
            return "刚刚已经画过一张了，等半分钟再画。"
        self._draw_last[sender] = now

        size = max(512, min(self._get_int("draw_size", 1024), 1024))
        style = "手绘涂鸦贴纸风格，粗描边，米白纸纹背景，居中构图，干净线条，卡通头像"
        full_prompt = f"{text}，{style}"

        api_key = self._get_str("image_api_key", "")
        content: bytes | None = None
        if api_key:
            base = self._get_str(
                "image_api_base", "https://open.bigmodel.cn/api/paas/v4"
            ).rstrip("/")
            model = self._get_str("image_model", "cogview-3-flash") or "cogview-3-flash"
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        f"{base}/images/generations",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": model, "prompt": full_prompt, "size": f"{size}x{size}"},
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                item = (payload.get("data") or [{}])[0]
                if item.get("b64_json"):
                    content = base64.b64decode(item["b64_json"])
                elif item.get("url"):
                    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                        img = await client.get(item["url"])
                        img.raise_for_status()
                        content = img.content
                if not content:
                    return f"图片生成失败：接口返回异常 {str(payload)[:200]}"
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 智谱出图失败：{exc}")
                return f"图片生成失败：{exc}"
        else:
            # 没有配置生图 API 时的兜底（免费图床，可能限流）
            model = self._get_str("draw_model", "")
            style_en = (
                "crayon doodle sticker style, thick wobbly outlines, off-white paper texture, "
                "cute cartoon avatar, centered composition, simple background, clean line art"
            )
            url = (
                "https://image.pollinations.ai/prompt/"
                + urllib.parse.quote(f"{text}, {style_en}"[:900])
                + f"?width={size}&height={size}&nologo=true&seed={random.randint(1, 1_000_000)}"
            )
            if model:
                url += f"&model={urllib.parse.quote(model)}"
            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                    content = resp.content
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_reader] 生成图片失败：{exc}")
                return f"图片生成失败：{exc}"

        if not content or len(content) < 1024:
            return "图片生成失败：返回内容为空。"

        path = os.path.join(
            self._image_dir, f"avatar_{int(now)}_{random.randint(100, 999)}.jpg"
        )
        try:
            with open(path, "wb") as fp:
                fp.write(content)
            prune_frames(self._image_dir, keep_files=100, max_bytes=100 * 1024 * 1024)
            await event.send(event.image_result(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 发送图片失败：{exc}")
            return f"图片生成了，但发送失败：{exc}"

        logger.info(f"[bili_reader] 生成头像 {path} ({len(content) // 1024}KB)")
        return "图片已生成并发送给用户。请用一两句话自然点评，不要复述提示词。"

    async def _tool_comment(self, event: AstrMessageEvent, comment: str, bvid: str = "") -> str:
        """在 B 站视频下发一条评论（会自动 @ 指定的账号）。

        仅当用户明确要求「去评论」「留一条评论」「发个评论」时调用。
        Args:
            comment(string): 评论正文。30 字以内，口语、健康合规，不要带 @ 或链接。
            bvid(string): 可选，视频 BV 号；留空表示用当前对话里刚看过的那个视频。
        Returns:
            发送结果说明。
        """
        if not self._is_owner(event):
            return "只有主人可以让我在 B 站发评论。"
        text = " ".join(str(comment or "").split()).strip()
        if not text:
            return "评论内容为空，没有发送。"
        if any(w in text for w in self._blocked_words()):
            return "这条评论命中屏蔽词，没有发送。"
        now = time.time()
        if now - self._last_manual_comment < 30:
            return "刚刚发过一条了，等半分钟再发。"

        target_bvid = str(bvid or "").strip().upper()
        info = self._last_video.get(event.unified_msg_origin)
        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )
        try:
            if target_bvid and (not info or info.bvid.upper() != target_bvid):
                info = await client.video_info(
                    f"https://www.bilibili.com/video/{target_bvid}"
                )
            if not info:
                return "当前没有可评论的视频，请先把视频链接发给我。"
            name = self._get_str("comment_target_name", "")
            message = f"@{name} {text}" if name else text
            data = await client.post_comment(info, message[:900])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 手动评论失败：{exc}")
            return f"评论发送失败：{exc}"
        finally:
            await client.close()

        self._last_manual_comment = now
        self._record_history("comment", info.bvid, info.title, 0, text, info.url)
        rpid = data.get("rpid") or ""
        return f"已发到《{info.title}》的评论区（rpid {rpid}）。可以顺口告诉用户你说了什么。"

    async def _tool_reply(self, event: AstrMessageEvent, index: int, comment: str) -> str:
        """回复当前视频评论区里的某条热评（楼中楼）。

        仅当用户明确要求「回复这条评论」「在楼下接一句」时调用。
        Args:
            index(number): 热评编号，从 1 开始，对应上下文「评论区热评」里列出的编号。
            comment(string): 回复内容，30 字以内，健康合规，不要带 @。
        Returns:
            发送结果说明。
        """
        if not self._is_owner(event):
            return "只有主人可以让我在 B 站回复评论。"
        items = self._last_comments.get(event.unified_msg_origin) or []
        if not items:
            return "当前没有可回复的评论，请先发一条视频链接让我读完评论区。"
        try:
            idx = int(index) - 1
        except (TypeError, ValueError):
            return "编号不合法。"
        if not (0 <= idx < len(items)):
            return f"编号超出范围（1-{len(items)}）。"
        text = " ".join(str(comment or "").split()).strip()
        if not text:
            return "回复内容为空。"
        if any(w in text for w in self._blocked_words()):
            return "这条回复命中屏蔽词，没有发送。"
        now = time.time()
        if now - self._last_manual_comment < 30:
            return "刚刚发过一条了，等半分钟再发。"
        info = self._last_video.get(event.unified_msg_origin)
        if not info:
            return "找不到当前视频信息，请重新发一次链接。"
        target = items[idx]
        client = BiliClient(
            cookie=self._get_str("bili_cookie", ""), sessdata=self._get_str("sessdata", "")
        )
        try:
            data = await client.reply_comment(
                info, str(target.get("rpid") or ""), str(target.get("rpid") or ""), text[:500]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_reader] 楼中楼回复失败：{exc}")
            return f"回复失败：{exc}"
        finally:
            await client.close()

        self._last_manual_comment = now
        self._record_history(
            "reply",
            info.bvid,
            info.title,
            int(target.get("like") or 0),
            f"回复第 {idx + 1} 条热评：{text}",
            info.url,
        )
        return f"已在第 {idx + 1} 条热评下回复（rpid {data.get('rpid') or ''}）。"

    async def terminate(self) -> None:
        if self._comment_task and not self._comment_task.done():
            self._comment_task.cancel()
        self._cache.clear()
        self._locks.clear()
        logger.info("[bili_reader] 插件已停止")


def _split_tool_call(args: tuple, kwargs: dict):
    """兼容 AstrBot 不同的工具调用方式：event 可能按位置或关键字传入，参数也可能重复。"""
    event = None
    rest: list = []
    for value in args:
        if event is None and hasattr(value, "unified_msg_origin"):
            event = value
        else:
            rest.append(value)
    kwargs.pop("event", None)
    return event, rest, kwargs


@llm_tool(name="remember_rule")
async def remember_rule(*args, **kwargs) -> str:
    """当用户明确要求你「记住」某条长期偏好或规则时调用，把它保存下来。

    Args:
        text(string): 要记住的规则，精炼成一句祈使句，例如「回复不要列点，最多两句话」。
    Returns:
        保存结果说明。
    """
    event, rest, kw = _split_tool_call(args, kwargs)
    text = str(kw.pop("text", rest[0] if rest else "")).strip()
    if event is None or PLUGIN is None:
        return "插件未就绪，没有保存。"
    return await PLUGIN._tool_remember(event, text)


@llm_tool(name="draw_avatar")
async def draw_avatar(*args, **kwargs) -> str:
    """根据画面描述生成一张卡通头像/插画，并直接发送给用户。

    当用户要求「设计一个头像」「画一张图」「照着视频里的风格画一个」时调用本工具。
    Args:
        prompt(string): 画面描述，中文即可，写清主体、表情、配色、道具和构图。
    Returns:
        生成结果说明。图片会由工具直接发出，不要重复贴图片或复述提示词。
    """
    event, rest, kw = _split_tool_call(args, kwargs)
    prompt = str(kw.pop("prompt", rest[0] if rest else "")).strip()
    if event is None or PLUGIN is None:
        return "插件未就绪，无法生成。"
    return await PLUGIN._tool_draw(event, prompt)


@llm_tool(name="post_video_comment")
async def post_video_comment(*args, **kwargs) -> str:
    """在 B 站视频下发一条评论（会自动 @ 指定账号）。只有用户明确要求「去评论」「留一条评论」时才调用。

    Args:
        comment(string): 评论正文，30 字以内，口语、健康合规，不要带 @ 或链接。
        bvid(string): 可选，视频 BV 号；留空表示用当前对话里刚看过的那个视频。
    Returns:
        发送结果说明。
    """
    event, rest, kw = _split_tool_call(args, kwargs)
    comment = str(kw.pop("comment", rest[0] if rest else "")).strip()
    bvid = str(kw.pop("bvid", rest[1] if len(rest) > 1 else "")).strip()
    if event is None or PLUGIN is None:
        return "插件未就绪，没有发送。"
    return await PLUGIN._tool_comment(event, comment, bvid)


@llm_tool(name="reply_hot_comment")
async def reply_hot_comment(*args, **kwargs) -> str:
    """回复当前视频评论区里的某条热评（楼中楼）。只有用户明确要求「回复这条评论」时才调用。

    Args:
        index(number): 热评编号，从 1 开始，对应上下文「评论区热评」里的编号。
        comment(string): 回复内容，30 字以内，健康合规，不要带 @。
    Returns:
        发送结果说明。
    """
    event, rest, kw = _split_tool_call(args, kwargs)
    index = kw.pop("index", rest[0] if rest else 0)
    comment = str(kw.pop("comment", rest[1] if len(rest) > 1 else "")).strip()
    if event is None or PLUGIN is None:
        return "插件未就绪，没有发送。"
    return await PLUGIN._tool_reply(event, index, comment)
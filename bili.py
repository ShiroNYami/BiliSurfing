"""B 站接口的最小封装：链接解析、视频信息、剧情图、字幕、播放地址、下载。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_WBI_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

VIDEO_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)",
    re.I,
)
SHORT_RE = re.compile(r"(?:https?://)?(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]{5,}", re.I)
BARE_BV_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
HOST_HINT_RE = re.compile(r"(bilibili\.com|b23\.tv|bili2233\.cn)", re.I)


class BiliError(Exception):
    """可预期的解析失败（链接不对、视频不可见等）。"""


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    title: str
    desc: str
    duration: int
    owner: str
    pubdate: int
    view: int
    like: int
    cid: int
    page_index: int
    pages: list[dict] = field(default_factory=list)

    @property
    def url(self) -> str:
        if self.page_index > 1:
            return f"https://www.bilibili.com/video/{self.bvid}?p={self.page_index}"
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def page_title(self) -> str:
        if self.page_index > 1 and self.page_index <= len(self.pages):
            part = self.pages[self.page_index - 1].get("part") or ""
            if part:
                return f"{self.title}（P{self.page_index}：{part}）"
        return self.title


def extract_video_urls(text: str) -> list[str]:
    """从一段文本里找出 B 站视频链接，返回去重后的链接列表。"""
    if not text:
        return []
    found: list[str] = []
    for regex in (VIDEO_RE, SHORT_RE):
        for m in regex.finditer(text):
            raw = m.group(0)
            if not raw.lower().startswith("http"):
                raw = "https://" + raw
            found.append(raw)
    return list(dict.fromkeys(found))


def looks_like_bili_video(text: str) -> bool:
    return bool(text and (VIDEO_RE.search(text) or SHORT_RE.search(text) or BARE_BV_RE.search(text)))


class BiliClient:
    def __init__(self, cookie: str = "", sessdata: str = "", timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            headers=HEADERS, timeout=timeout, follow_redirects=True
        )
        self._cookie = (cookie or "").strip()
        self._sessdata = (sessdata or "").strip()
        self._prepared = False

    async def close(self) -> None:
        await self._client.aclose()

    def _apply_cookie(self) -> None:
        pairs: list[str] = []
        if self._cookie:
            pairs.extend(p.strip() for p in self._cookie.split(";") if "=" in p)
        if self._sessdata and "SESSDATA=" not in self._cookie:
            pairs.append(f"SESSDATA={self._sessdata}")
        for pair in pairs:
            name, _, value = pair.partition("=")
            try:
                self._client.cookies.set(name.strip(), value.strip(), domain=".bilibili.com")
            except Exception:  # noqa: BLE001
                pass

    async def _prepare(self) -> None:
        if self._prepared:
            return
        self._apply_cookie()
        if not self._cookie:
            try:
                # 没有登录 Cookie 时先访问一次主页拿 buvid3/buvid4，否则接口会返回 412。
                await self._client.get("https://www.bilibili.com/")
            except Exception:
                pass
        self._prepared = True

    async def _json(self, url: str, params: dict | None = None) -> dict:
        await self._prepare()
        last: Exception | None = None
        for _ in range(2):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - 统一转成 BiliError
                last = exc
                await asyncio.sleep(0.6)
        raise BiliError(f"B 站接口请求失败：{last}")

    async def resolve(self, url: str) -> str:
        """展开 b23.tv 短链，返回最终跳转地址。"""
        if not SHORT_RE.search(url):
            return url
        if not url.lower().startswith("http"):
            url = "https://" + url
        await self._prepare()
        try:
            resp = await self._client.get(url)
            return str(resp.url)
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"短链展开失败：{exc}") from exc

    async def video_info(self, url: str) -> VideoInfo:
        """解析链接（含短链）并拉取视频元信息。"""
        final_url = await self.resolve(url)
        match = VIDEO_RE.search(final_url)
        if not match:
            raise BiliError("链接里没找到 B 站视频 ID（只支持视频页，不支持直播/动态/番剧）")
        ident = match.group(1)

        params: dict[str, str] = {}
        if ident.lower().startswith("av"):
            params["aid"] = ident[2:]
        else:
            params["bvid"] = ident

        payload = await self._json("https://api.bilibili.com/x/web-interface/view", params)
        if payload.get("code") != 0:
            raise BiliError(f"取视频信息失败：{payload.get('message') or payload.get('code')}")
        data = payload["data"]

        page_index = 1
        query = parse_qs(urlparse(final_url).query)
        if query.get("p"):
            try:
                page_index = max(1, int(query["p"][0]))
            except ValueError:
                page_index = 1

        pages = data.get("pages") or []
        if page_index > len(pages):
            page_index = 1
        cid = data.get("cid")
        if pages and page_index <= len(pages):
            cid = pages[page_index - 1].get("cid") or cid

        stat = data.get("stat") or {}
        return VideoInfo(
            bvid=data.get("bvid") or ident,
            aid=int(data.get("aid") or 0),
            title=(data.get("title") or "").strip(),
            desc=(data.get("desc") or "").strip(),
            duration=int(data.get("duration") or 0),
            owner=((data.get("owner") or {}).get("name") or "").strip(),
            pubdate=int(data.get("pubdate") or 0),
            view=int(stat.get("view") or 0),
            like=int(stat.get("like") or 0),
            cid=int(cid or 0),
            page_index=page_index,
            pages=pages,
        )

    async def storyboard(self, info: VideoInfo) -> tuple[bytes, list[int], int, int] | None:
        """取 B 站剧情图（官方抽帧雪碧图）。返回 (图片字节, 每格时间点, 列数, 行数)。"""
        try:
            payload = await self._json(
                "https://api.bilibili.com/x/player/videoshot",
                {"bvid": info.bvid, "cid": info.cid, "index": 1},
            )
        except BiliError:
            return None
        if payload.get("code") != 0:
            return None
        data = payload.get("data") or {}
        images = data.get("image") or []
        if not images:
            return None
        sprite_url = images[0]
        if sprite_url.startswith("//"):
            sprite_url = "https:" + sprite_url
        try:
            resp = await self._client.get(sprite_url, headers={"Referer": "https://www.bilibili.com/"})
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            return None
        stamps = [int(x) for x in (data.get("index") or [])]
        if not stamps:
            return None
        return (
            resp.content,
            stamps,
            int(data.get("img_x_len") or 10),
            int(data.get("img_y_len") or 10),
        )

    async def playurl(self, info: VideoInfo, qn: int = 64) -> list[str]:
        """取可直接下载的 mp4 地址（带备用线路）。"""
        payload = await self._json(
            "https://api.bilibili.com/x/player/playurl",
            {"bvid": info.bvid, "cid": info.cid, "qn": qn, "fnval": 1, "fnver": 0, "fourk": 0},
        )
        if payload.get("code") != 0:
            raise BiliError(f"取播放地址失败：{payload.get('message') or payload.get('code')}")
        data = payload.get("data") or {}
        durls = data.get("durl") or []
        urls: list[str] = []
        for item in durls:
            for key in ("url", "backup_url"):
                value = item.get(key)
                if isinstance(value, list):
                    urls.extend([u for u in value if u])
                elif isinstance(value, str) and value:
                    urls.append(value)
        if not urls:
            raise BiliError("视频没有可下载的直链（可能是充电/付费视频）")
        return list(dict.fromkeys(urls))

    async def subtitles(self, info: VideoInfo) -> list[tuple[float, str]]:
        """取 UP 主/CC 字幕。需要登录 Cookie，游客身份通常为空。"""
        entries: list[dict] = []
        for path in ("x/player/wbi/v2", "x/player/v2"):
            try:
                payload = await self._json(
                    f"https://api.bilibili.com/{path}",
                    {"bvid": info.bvid, "cid": info.cid},
                )
            except BiliError:
                continue
            if payload.get("code") != 0:
                continue
            data = payload.get("data") or {}
            entries = ((data.get("subtitle") or {}).get("subtitles")) or []
            if entries:
                break
        if not entries:
            return []
        # 优先中文
        entries.sort(key=lambda e: 0 if str(e.get("lan", "")).startswith("zh") else 1)
        for entry in entries:
            sub_url = entry.get("subtitle_url") or ""
            if not sub_url:
                continue
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            try:
                resp = await self._client.get(sub_url, headers={"Referer": "https://www.bilibili.com/"})
                resp.raise_for_status()
                body = (resp.json() or {}).get("body") or []
            except Exception:  # noqa: BLE001
                continue
            lines: list[tuple[float, str]] = []
            for item in body:
                content = (item.get("content") or "").strip()
                if not content:
                    continue
                lines.append((float(item.get("from") or 0), content))
            if lines:
                return lines
        return []

    async def comments(self, info: VideoInfo, limit: int = 6) -> list[dict]:
        """取热门评论（按点赞），返回 [{rpid, like, text, uname}]。"""
        try:
            payload = await self._json(
                "https://api.bilibili.com/x/v2/reply",
                {"type": 1, "oid": info.aid, "pn": 1, "ps": 20, "sort": 2},
            )
        except BiliError:
            return []
        if payload.get("code") != 0:
            return []
        replies = (payload.get("data") or {}).get("replies") or []
        items: list[dict] = []
        for reply in replies:
            message = ((reply.get("content") or {}).get("message") or "").strip()
            message = re.sub(r"\[[^\]]{1,8}\]", "", message)  # 去掉 [打call] 这类表情
            message = re.sub(r"\s+", " ", message).strip()
            if len(message) < 4:
                continue
            items.append(
                {
                    "rpid": str(reply.get("rpid") or ""),
                    "like": int(reply.get("like") or 0),
                    "text": message[:120],
                    "uname": str((reply.get("member") or {}).get("uname") or ""),
                }
            )
        items.sort(key=lambda x: -x["like"])
        return items[:limit]

    async def reply_comment(
        self, info: VideoInfo, root: str, parent: str, message: str
    ) -> dict:
        """回复某条评论（楼中楼）。root 为楼根 rpid，parent 为被回复的 rpid。"""
        csrf = self.csrf
        if not csrf:
            raise BiliError("Cookie 里缺少 bili_jct，无法发评论")
        payload = await self._post(
            "https://api.bilibili.com/x/v2/reply/add",
            {
                "type": 1,
                "oid": info.aid,
                "root": str(root),
                "parent": str(parent),
                "message": message,
                "plat": 1,
                "csrf": csrf,
            },
        )
        code = payload.get("code")
        if code == 17:
            return {"_folded": True, "code": 17}
        if code != 0:
            raise BiliError(f"回复失败：{payload.get('message')}（code {code}）")
        return payload.get("data") or {}

    async def reply_comment_aid(self, aid: int, root: str, parent: str, message: str) -> dict:
        """按 aid 回复楼中楼。"""
        csrf = self.csrf
        if not csrf:
            raise BiliError("Cookie 里缺少 bili_jct，无法回复")
        payload = await self._post(
            "https://api.bilibili.com/x/v2/reply/add",
            {
                "type": 1,
                "oid": int(aid),
                "root": str(root),
                "parent": str(parent),
                "message": message,
                "plat": 1,
                "csrf": csrf,
            },
        )
        code = payload.get("code")
        if code == 17:
            return {"_folded": True, "code": 17}
        if code != 0:
            raise BiliError(f"回复失败：{payload.get('message')}（code {code}）")
        return payload.get("data") or {}

    async def child_comments(self, aid: int, root: str, pn: int = 1, ps: int = 20) -> list[dict]:
        """拉某条评论下的楼中楼。"""
        payload = await self._json(
            "https://api.bilibili.com/x/v2/reply/reply",
            {"type": 1, "oid": int(aid), "root": str(root), "pn": pn, "ps": ps},
        )
        if payload.get("code") != 0:
            return []
        out: list[dict] = []
        for reply in (payload.get("data") or {}).get("replies") or []:
            message = ((reply.get("content") or {}).get("message") or "").strip()
            message = re.sub(r"\[[^\]]{1,8}\]", "", message)
            message = re.sub(r"\s+", " ", message).strip()
            if not message:
                continue
            member = reply.get("member") or {}
            out.append(
                {
                    "rpid": str(reply.get("rpid") or ""),
                    "mid": str(member.get("mid") or ""),
                    "uname": str(member.get("uname") or ""),
                    "text": message,
                    "ctime": int(reply.get("ctime") or 0),
                }
            )
        return out

    async def create_dynamic(self, text: str) -> dict:
        """发一条纯文字动态。"""
        csrf = self.csrf
        if not csrf:
            raise BiliError("Cookie 里缺少 bili_jct，无法发动态")
        await self._prepare()
        nav = await self._json("https://api.bilibili.com/x/web-interface/nav")
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        if len(raw) < 64:
            raise BiliError("获取 WBI 签名失败")
        dyn_req = {
            "content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
            "scene": 1,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "attach_card": None,
        }
        resp = await self._client.post(
            "https://api.bilibili.com/x/dynamic/feed/create/dyn",
            params={"csrf": csrf},
            json={"dyn_req": dyn_req},
        )
        payload = resp.json()
        if payload.get("code") != 0:
            raise BiliError(
                f"发动态失败：{payload.get('message')}（code {payload.get('code')}）"
            )
        return payload.get("data") or {}
    async def find_comment(self, info: "VideoInfo", rpid: str = "", mid: str = "", pages: int = 3) -> bool:
        """在公开评论列表里找自己的评论，判断是否真的可见。"""
        for page in range(1, max(1, pages) + 1):
            payload = await self._json(
                "https://api.bilibili.com/x/v2/reply",
                {"type": 1, "oid": int(info.aid), "sort": 0, "ps": 20, "pn": page},
            )
            if payload.get("code") != 0:
                return False
            for reply in (payload.get("data") or {}).get("replies") or []:
                if rpid and str(reply.get("rpid")) == str(rpid):
                    return True
                member = reply.get("member") or {}
                if mid and not rpid and str(member.get("mid")) == str(mid):
                    return True
        return False
    async def search_videos(
        self,
        keyword: str,
        limit: int = 8,
        days: int = 3,
        max_seconds: int = 1800,
        min_play: int = 0,
        pages: int = 3,
    ) -> list[dict]:
        """按关键词搜最近的视频（按发布时间排序），可跨页凑够候选。"""
        deadline = time.time() - max(1, days) * 86400
        items: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max(1, pages) + 1):
            payload = await self._json(
                "https://api.bilibili.com/x/web-interface/search/type",
                {
                    "search_type": "video",
                    "keyword": keyword,
                    "page": page,
                    "order": "click",
                    "pubtime_begin_s": int(deadline),
                    "pubtime_end_s": int(time.time()),
                },
            )
            if payload.get("code") != 0:
                raise BiliError(
                    f"搜索失败：{payload.get('message') or payload.get('code')}"
                )
            rows = (payload.get("data") or {}).get("result") or []
            if not rows:
                break
            for raw in rows:
                bvid = raw.get("bvid") or ""
                if not bvid or bvid in seen:
                    continue
                pubdate = int(raw.get("pubdate") or 0)
                if pubdate and pubdate < deadline:
                    continue
                duration = _parse_duration(raw.get("duration"))
                if duration and duration > max_seconds:
                    continue
                play = int(raw.get("play") or 0)
                desc = str(raw.get("description") or "").strip()[:160]
                if play < min_play:
                    # 搜索接口有时把播放量压成 0/1，用详情接口补真实数据
                    try:
                        detail = await self.video_info(f"https://www.bilibili.com/video/{bvid}")
                        play = int(getattr(detail, "view", 0) or 0)
                        desc = (detail.desc or desc)[:160]
                    except Exception:  # noqa: BLE001
                        continue
                    if play < min_play:
                        continue
                title = re.sub(r"<[^>]+>", "", str(raw.get("title") or "")).strip()
                seen.add(bvid)
                items.append(
                    {
                        "bvid": bvid,
                        "title": title,
                        "author": str(raw.get("author") or ""),
                        "duration": duration,
                        "pubdate": pubdate,
                        "play": play,
                        "desc": desc,
                    }
                )
                if len(items) >= limit:
                    return items
        return items

    @property
    def csrf(self) -> str:
        for part in self._cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "bili_jct":
                return value
        return ""

    async def _post(self, url: str, data: dict) -> dict:
        await self._prepare()
        last: Exception | None = None
        for _ in range(2):
            try:
                resp = await self._client.post(url, data=data)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(0.8)
        raise BiliError(f"请求失败：{last}")

    async def user_name(self, mid: int) -> str:
        payload = await self._json(
            "https://api.bilibili.com/x/web-interface/card", {"mid": mid}
        )
        if payload.get("code") == 0:
            return str((payload.get("data") or {}).get("card", {}).get("name") or "")
        return ""

    async def post_comment(self, info: VideoInfo, message: str) -> dict:
        csrf = self.csrf
        if not csrf:
            raise BiliError("Cookie 里缺少 bili_jct，无法发评论")
        payload = await self._post(
            "https://api.bilibili.com/x/v2/reply/add",
            {"type": 1, "oid": info.aid, "message": message, "plat": 1, "csrf": csrf},
        )
        code = payload.get("code")
        if code == 17:
            # 17 = 发送成功但被折叠，别人看不见
            return {"_folded": True, "code": 17, "message": payload.get("message") or ""}
        if code != 0:
            raise BiliError(f"发评论失败：{payload.get('message')}（code {code}）")
        return payload.get("data") or {}

    async def delete_comment(self, info: VideoInfo, rpid: int | str) -> bool:
        csrf = self.csrf
        payload = await self._post(
            "https://api.bilibili.com/x/v2/reply/del",
            {"type": 1, "oid": info.aid, "rpid": str(rpid), "csrf": csrf},
        )
        return payload.get("code") == 0

    async def download(self, url: str, dest_path: str, max_bytes: int) -> str:
        """流式下载视频，超过上限立即中断。"""
        await self._prepare()
        total = 0
        try:
            async with self._client.stream(
                "GET", url, headers={"Referer": "https://www.bilibili.com/"}
            ) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as fp:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise BiliError(f"视频超过 {max_bytes // 1024 // 1024}MB 上限，已放弃下载")
                        fp.write(chunk)
        except BiliError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"下载视频失败：{exc}") from exc
        return dest_path


def _parse_duration(value: object) -> int:
    """把 '12:34' / '1:02:03' 或秒数转成秒。"""
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return numbers[0] if numbers else 0


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_wan(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return str(value)

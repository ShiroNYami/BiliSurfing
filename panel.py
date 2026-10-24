"""管理面板：直接读取 pages/panel/index.html，避免 HTML 在两处维护。"""

from __future__ import annotations

from pathlib import Path

_PAGE_FILE = Path(__file__).resolve().with_name("pages") / "panel" / "index.html"

_FALLBACK = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>B站管理面板</title></head><body style="font-family:sans-serif;padding:24px">
<h3>面板文件缺失</h3><p>请确认插件目录下存在 <code>pages/panel/index.html</code>。</p>
</body></html>"""


def page_html() -> str:
    try:
        return _PAGE_FILE.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return _FALLBACK

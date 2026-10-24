#!/usr/bin/env bash
# 让插件页面里的外链可以在新标签页打开。
# AstrBot 给插件页 iframe 的 sandbox 默认是 allow-scripts allow-forms allow-downloads，
# 不含 allow-popups，所以 target="_blank" 会被浏览器拦掉。
#
# 注意：这是对 AstrBot dashboard 静态资源的修改，不是插件本身的一部分；
# 升级/重建 AstrBot 容器后需要重新执行一次。
#
# 用法：bash scripts/patch-dashboard-popups.sh [容器名，默认 astrbot]

set -euo pipefail
CONTAINER="${1:-astrbot}"

docker exec "$CONTAINER" bash -lc '
set -e
f=$(ls /AstrBot/astrbot/dashboard/dist/assets/PluginViewPage-*.js | head -1)
[ -f "$f" ] || { echo "未找到 PluginViewPage 资源"; exit 1; }
[ -f "$f.bak-popups" ] || cp "$f" "$f.bak-popups"
sed -i "s/sandbox:\"allow-scripts allow-forms allow-downloads\"/sandbox:\"allow-scripts allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox\"/" "$f"
echo -n "sandbox now: "
grep -o "sandbox:\"[^\"]*\"" "$f"
'

"""抽帧：B 站剧情图裁剪 / ffmpeg 视频抽帧 / 临时文件清理。"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import time

from PIL import Image

from .bili import BiliError


def _even_timestamps(duration: int, count: int) -> list[int]:
    """在视频时长内均匀取 count 个时间点，避开片头片尾。"""
    if duration <= 0 or count <= 0:
        return []
    start = max(1, int(duration * 0.04))
    end = max(start + 1, int(duration * 0.96))
    if count == 1:
        return [(start + end) // 2]
    step = (end - start) / (count - 1)
    return [int(start + step * i) for i in range(count)]


def crop_storyboard(
    sprite: bytes,
    stamps: list[int],
    grid_x: int,
    grid_y: int,
    count: int,
    out_dir: str,
    prefix: str,
) -> list[tuple[int, str]]:
    """从官方雪碧图里裁出 count 张画面，返回 [(时间点, 文件路径)]。"""
    if grid_x <= 0 or grid_y <= 0 or not stamps:
        return []
    try:
        image = Image.open(io.BytesIO(sprite)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise BiliError(f"剧情图解码失败：{exc}") from exc

    tile_w = image.width // grid_x
    tile_h = image.height // grid_y
    if tile_w <= 0 or tile_h <= 0:
        return []

    os.makedirs(out_dir, exist_ok=True)
    duration = stamps[-1]
    wanted = _even_timestamps(duration, count)
    results: list[tuple[int, str]] = []
    used: set[int] = set()

    for ts in wanted:
        index = min(range(len(stamps)), key=lambda i: abs(stamps[i] - ts))
        if index in used:
            continue
        used.add(index)
        col, row = index % grid_x, index // grid_x
        if row >= grid_y:
            break
        tile = image.crop(
            (col * tile_w, row * tile_h, (col + 1) * tile_w, (row + 1) * tile_h)
        )
        # 剧情图单格只有 160x90 左右，适度放大便于视觉模型辨认
        tile = tile.resize((tile_w * 3, tile_h * 3), Image.LANCZOS)
        path = os.path.join(out_dir, f"{prefix}_{int(stamps[index]):05d}.jpg")
        tile.save(path, format="JPEG", quality=88)
        results.append((int(stamps[index]), path))

    return results


async def extract_video_frames(
    video_path: str,
    duration: int,
    count: int,
    out_dir: str,
    prefix: str,
    width: int = 960,
) -> list[tuple[int, str]]:
    """用 ffmpeg 在均匀时间点抽帧，返回 [(时间点, 文件路径)]。"""
    os.makedirs(out_dir, exist_ok=True)
    results: list[tuple[int, str]] = []
    for ts in _even_timestamps(duration, count):
        out_path = os.path.join(out_dir, f"{prefix}_{int(ts):05d}.jpg")
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            str(ts),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "4",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(out_path):
            continue
        results.append((int(ts), out_path))
    if not results:
        raise BiliError(f"ffmpeg 抽帧失败：{(stderr or b'').decode('utf-8', 'ignore')[:200]}")
    return results


def prune_frames(root: str, keep_files: int = 400, max_bytes: int = 300 * 1024 * 1024) -> None:
    """清理旧的抽帧文件，避免 2C2G 小机器磁盘被撑爆。"""
    if not os.path.isdir(root):
        return
    entries: list[tuple[float, int, str]] = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size

    entries.sort()
    while entries and (len(entries) > keep_files or total > max_bytes):
        _mtime, size, path = entries.pop(0)
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass

    # 顺手删掉空目录
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def cleanup_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)

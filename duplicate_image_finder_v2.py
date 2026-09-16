#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重复图片查找工具 v2 —— 面向海量文件（十万~百万级）
=====================================================

特性:
  * 交互菜单:
      [1] 开始/继续查找(断点续扫)  [2] 重新扫描主库  [3] 导出列表(CSV+Markdown)
      [4] 临时隔离扫描(不污染主库, 导出 _temp 报告)  [5] 优化与清理数据库
      [6] 切换错误重试模式  [7] 清理 Mac 垃圾文件(._* 与 .DS_Store)
  * 终端单行动态更新: 原地更新单行进度与当前文件，绝不换行滚动刷屏，保留全部历史日志
  * 智能断点续扫: 支持跳过磁盘重遍历，直达未完成阶段 (文件头/整文件/SIMD感知哈希聚类)
  * 容错与重试: 遇错暂停时可输入 'r' 重试 / 'a' 跳过本次运行所有错误, 也可用选项 6 永久切换自动跳过
  * Mac 垃圾免疫: 自动跳过 macOS 拷贝产生的 ._xxx (AppleDouble) 元数据文件, 不会误报 UnidentifiedImageError
  * 分级漏斗加速: 文件大小 -> 文件头哈希 (64KB) -> 整文件哈希 (BLAKE2b-128) -> (可选)感知哈希 (pHash)
  * NumPy SIMD 硬件级向量化 (POPCNT) 多线程块并行快速相似聚类

重复类型:
  * 内容重复:   字节级完全一致(整文件 BLAKE2b-128 哈希, 零误报, 安全去重)
  * 视觉相似(可选): pHash 64位指纹 + SIMD 向量化并行聚类,
    可识别重新压缩/改分辨率/加水印/改格式后肉眼相同的图片

依赖:
  pip install pillow imagehash tqdm numpy

用法:
  python duplicate_image_finder_v2.py                          # 交互菜单
  python duplicate_image_finder_v2.py scan <目录>              # 续扫主库
  python duplicate_image_finder_v2.py scan <目录> --temp       # 临时隔离扫描 (导出 _temp 报告)
  python duplicate_image_finder_v2.py scan <目录> --skip-walk  # 跳过磁盘遍历直达未完成阶段
  python duplicate_image_finder_v2.py scan <目录> --fresh      # 重新扫描
  python duplicate_image_finder_v2.py scan <目录> --no-visual  # 只查字节级重复
  python duplicate_image_finder_v2.py export                   # 导出报告
  python duplicate_image_finder_v2.py clean-db                 # 优化与清理数据库
  python duplicate_image_finder_v2.py clean-junk <目录>        # 清理 macOS 垃圾文件 (._* 与 .DS_Store)
"""

import argparse
import ctypes
import hashlib
import os
import shutil
import sqlite3
import sys
import time
import unicodedata
import warnings
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

# 禁用大图像素上限限制及 DecompressionBomb 告警
Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter('ignore', Image.DecompressionBombWarning)

# Windows 终端 VT 模式与 UTF-8 输出初始化
if sys.platform == 'win32':
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# ----------------------------- 常量 -----------------------------

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif'}
DB_FILENAME = 'image_dedup.db'
TEMP_DB_FILENAME = 'image_dedup_temp.db'
PREFIX_BYTES = 64 * 1024      # 文件头哈希的采样长度
CHUNK_SIZE = 50               # 每个进程任务处理的文件数 (调小以高频平滑更新当前读取文件)
BATCH = 5000                  # 数据库批量提交大小
HASH_SIZE = 8                 # pHash 8x8 = 64 位指纹
DEFAULT_THRESHOLD = 6         # 汉明距离阈值(0=完全一致, 建议 5~8)

KIND_EXACT = '内容重复'
KIND_VISUAL = '视觉相似'

# 会话级错误处理: 报错提示中输入 'a' 后, 本次运行剩余所有错误自动跳过 (仅当次运行生效, 不改动菜单选项 6)
_session_auto_skip = [False]


def _auto_skip_now(auto_skip_errors: bool) -> bool:
    """菜单选项 6 的永久开关(auto_skip_errors) 或本次运行中输入 'a' 均可触发自动跳过"""
    return auto_skip_errors or _session_auto_skip[0]

# ------------------- Windows OneDrive 占位文件检测 -------------------

if sys.platform == 'win32':
    from ctypes import wintypes

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _k32.GetFileAttributesW.restype = wintypes.DWORD
    _F_OFFLINE = 0x1000
    _F_RECALL_ON_OPEN = 0x40000
    _F_RECALL_ON_DATA = 0x400000

    def is_placeholder(path: str) -> bool:
        """OneDrive 按需文件: 读取内容会触发云端下载, 扫描时跳过"""
        p = '\\\\?\\' + os.path.abspath(path) if len(path) >= 250 else path
        attrs = _k32.GetFileAttributesW(p)
        return attrs != 0xFFFFFFFF and bool(attrs & (_F_OFFLINE | _F_RECALL_ON_OPEN | _F_RECALL_ON_DATA))
else:
    def is_placeholder(path: str) -> bool:
        return False

# ----------------------------- 终端单行动态显示 -----------------------------


def visual_len(s: str) -> int:
    """计算中英文字符串在终端中所占的实际显示列数 (全角占 2 列, 半角占 1 列)"""
    return sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1 for c in s)


def truncate_to_width(s: str, max_w: int) -> str:
    """严格按终端显示列数截断字符串, 避免超出终端宽度导致换行"""
    cur_w = 0
    res = []
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
        if cur_w + cw > max_w:
            break
        cur_w += cw
        res.append(ch)
    return ''.join(res)


class SingleLineReporter:
    """
    单行就地动态进度控制器:
    格式: [阶段] 百分比 | 当前: <文件名/路径> | 当前/总数 [已用<剩余, 速率]
    特性: 严格限制在终端单行内, 使用 \\r 覆盖, 绝不换行滚动, 绝不冲刷历史日志
    """
    def __init__(self, total: int = 0, desc: str = '处理中'):
        self.total = total
        self.desc = desc
        self.count = 0
        self.start_time = time.time()
        self.current_file = ''
        self.last_render = 0
        self.isatty = sys.stdout.isatty()

    def update(self, n: int = 1, current_file: str = ''):
        self.count += n
        if current_file:
            self.current_file = current_file
        now = time.time()
        if now - self.last_render > 0.04 or (self.total > 0 and self.count >= self.total):
            self.render()
            self.last_render = now

    def render(self):
        if not self.isatty:
            return
        try:
            cols = shutil.get_terminal_size((80, 20)).columns
        except Exception:
            cols = 80
        cols = max(40, cols)

        elapsed = time.time() - self.start_time
        rate = (self.count / elapsed) if elapsed > 0 else 0
        el_str = time.strftime('%M:%S', time.gmtime(elapsed))

        if self.total > 0:
            pct_str = f'{self.count * 100 / self.total:5.1f}%'
            if rate > 0 and self.total > self.count:
                rem_str = time.strftime('%M:%S', time.gmtime((self.total - self.count) / rate))
            else:
                rem_str = '00:00'
            stats_str = f'| {self.count:,}/{self.total:,} [{el_str}<{rem_str}, {rate:.1f}个/s]'
        else:
            pct_str = ''
            stats_str = f'| {self.count:,}个 [{el_str}, {rate:.1f}个/s]'

        prefix = f'{self.desc} {pct_str} '
        fixed_w = visual_len(prefix) + visual_len(stats_str) + 6
        avail_w = max(10, cols - fixed_w)

        fn = self.current_file
        if visual_len(fn) > avail_w:
            fn = '...' + fn[-(avail_w - 3):]
            while visual_len(fn) > avail_w and len(fn) > 4:
                fn = '...' + fn[4:]

        full_line = f'{prefix}| 当前: {fn} {stats_str}'
        full_line = truncate_to_width(full_line, cols - 1)
        pad = ' ' * max(0, cols - 1 - visual_len(full_line))

        sys.stdout.write(f'\r{full_line}{pad}')
        sys.stdout.flush()

    def clear(self):
        if self.isatty:
            try:
                cols = shutil.get_terminal_size((80, 20)).columns
            except Exception:
                cols = 80
            sys.stdout.write('\r' + ' ' * (cols - 1) + '\r')
            sys.stdout.flush()

    def close(self):
        if self.isatty:
            self.render()
            sys.stdout.write('\n')
            sys.stdout.flush()
        else:
            elapsed = time.time() - self.start_time
            print(f'{self.desc} 完成: 共 {self.count:,} 项, 耗时 {elapsed:.1f}s')

# ----------------------------- 数据库 -----------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    prefix_hash TEXT,
    full_hash   TEXT,
    phash       TEXT,
    visual_group INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_files_size ON files(size);
CREATE INDEX IF NOT EXISTS ix_files_size_prefix ON files(size, prefix_hash);
CREATE INDEX IF NOT EXISTS ix_files_full ON files(full_hash);
CREATE INDEX IF NOT EXISTS ix_files_vg   ON files(visual_group);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def db_file_count(conn):
    return conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]


def clean_database(db_path: str):
    """清理并压缩优化 SQLite 数据库 (VACUUM & ANALYZE)"""
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        print(f'数据库不存在: {db_path}')
        return
    size_before = os.path.getsize(db_path)
    conn = open_db(db_path)
    total_before = db_file_count(conn)
    err_before = conn.execute('SELECT COUNT(*) FROM files WHERE error IS NOT NULL').fetchone()[0]
    print('\n' + '=' * 50)
    print(f'正在优化整理数据库: {os.path.basename(db_path)}')
    print('=' * 50)
    print(f'当前记录: {total_before:,} 个文件 (含 {err_before:,} 个错误记录)')
    print(f'当前文件体积: {size_before / 1048576:.2f} MB')

    t0 = time.time()
    conn.execute('VACUUM')
    conn.execute('ANALYZE')
    conn.commit()
    conn.close()

    size_after = os.path.getsize(db_path)
    freed = max(0, size_before - size_after)
    print(f'\n[优化完成]')
    print(f'  耗时:         {time.time() - t0:.2f} 秒')
    print(f'  整理后体积:   {size_after / 1048576:.2f} MB')
    print(f'  释放磁盘空间: {freed / 1048576:.2f} MB')
    print('=' * 50)


def clean_mac_junk(root: str, db_path: str = None) -> int:
    """扫描并删除 macOS 拷贝到非 Mac 文件系统时产生的垃圾文件:
    ._xxx (AppleDouble 元数据文件, 会导致 PIL 报 UnidentifiedImageError) 与 .DS_Store。
    删除前列出清单并要求确认; 删除后同步移除主数据库中对应的记录。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f'错误: 目录不存在 - {root}')
        return 1

    print('\n' + '=' * 50)
    print('        清理 macOS 垃圾文件')
    print('=' * 50)
    print(f'扫描目录: {root}')
    print('目标文件: ._*.xxx (AppleDouble 元数据) 与 .DS_Store')
    print('-' * 50)

    junk = []   # (路径, 大小)
    t0 = time.time()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if is_mac_junk(fn):
                p = os.path.join(dirpath, fn)
                try:
                    junk.append((p, os.path.getsize(p)))
                except OSError:
                    junk.append((p, 0))

    if not junk:
        print(f'[完成] 未发现 macOS 垃圾文件, 目录很干净 (耗时 {time.time() - t0:.1f} 秒)。')
        return 0

    total_size = sum(s for _, s in junk)
    print(f'发现 {len(junk):,} 个垃圾文件, 共占用 {total_size / 1048576:.2f} MB (扫描耗时 {time.time() - t0:.1f} 秒)')
    for p, s in junk[:10]:
        print(f'  - {p} ({s / 1024:.1f} KB)')
    if len(junk) > 10:
        print(f'  ... 以及另外 {len(junk) - 10:,} 个文件')

    if input(f'\n确认永久删除以上 {len(junk):,} 个垃圾文件? (yes/no): ').strip().lower() != 'yes':
        print('已取消, 未删除任何文件。')
        return 0

    t1 = time.time()
    deleted, failed, freed = 0, 0, 0
    deleted_paths = []
    for p, s in junk:
        try:
            os.remove(p)
            deleted += 1
            freed += s
            deleted_paths.append(p)
        except OSError as e:
            failed += 1
            print(f'  [删除失败] {p}: {e}')
    print(f'\n[清理完成] 耗时 {time.time() - t1:.1f} 秒')
    print(f'  已删除:       {deleted:,} 个文件')
    print(f'  释放磁盘空间: {freed / 1048576:.2f} MB')
    if failed:
        print(f'  删除失败:     {failed:,} 个 (通常是文件被占用, 关闭相关程序后重试)')

    # 同步主数据库: 移除已删除文件对应的记录 (即使不同步, 下次完整扫描也会自动校正)
    if deleted_paths and db_path and os.path.exists(db_path):
        try:
            conn = open_db(os.path.abspath(db_path))
            for i in range(0, len(deleted_paths), BATCH):
                qm = ','.join('?' * len(deleted_paths[i:i + BATCH]))
                conn.execute(f'DELETE FROM files WHERE path IN ({qm})', deleted_paths[i:i + BATCH])
            conn.commit()
            conn.close()
            print(f'  主数据库已同步, 移除 {len(deleted_paths):,} 条对应记录。')
        except Exception as e:
            print(f'  [提示] 主数据库同步失败 (不影响本次清理, 下次扫描会自动校正): {e}')
    print('=' * 50)
    return 0

# ----------------------------- 目录遍历 -----------------------------


def is_mac_junk(name: str) -> bool:
    """macOS 拷贝到非 Mac 文件系统时产生的垃圾文件:
    ._xxx 为 AppleDouble 元数据文件 (不是图片, PIL 无法解码), .DS_Store 为 Finder 设置文件"""
    return name.startswith('._') or name == '.DS_Store'


def walk_images(root: str, junk_counter=None):
    """确定性(按名称排序)递归遍历, 产出图片文件路径 (自动跳过 macOS 垃圾文件)。
    junk_counter 为可选 [计数] 容器, 用于统计发现的垃圾文件数。"""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError as e:
            print(f'\n[警告] 无法进入目录 {d}: {e}')
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    if is_mac_junk(entry.name):
                        if junk_counter is not None:
                            junk_counter[0] += 1
                        continue
                    if os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS:
                        yield entry.path
            except OSError:
                continue


def enumerate_into_db(conn, root: str, db_abs: str, auto_skip_errors: bool = False):
    """遍历目录并同步到数据库。返回 (总数, 新增, 变化, 删除, 占位跳过, 出错, Mac垃圾跳过)"""
    added = changed = deleted = skipped_ph = stat_err = 0
    mac_junk = [0]
    seen = set()
    batch = []

    def flush():
        nonlocal added, changed
        if not batch:
            return
        paths = [b[0] for b in batch]
        qmarks = ','.join('?' * len(paths))
        existing = {r[1]: r for r in conn.execute(
            f'SELECT id,path,size,mtime,name FROM files WHERE path IN ({qmarks})', paths)}
        inserts, updates = [], []
        for path, name, size, mtime in batch:
            row = existing.get(path)
            if row is None:
                inserts.append((path, name, size, mtime))
                added += 1
            elif row[2] != size or row[3] != mtime or row[4] != name:
                updates.append((size, mtime, name, row[0]))
                changed += 1
            seen.add(row[0] if row else None)
        if inserts:
            conn.executemany('INSERT INTO files(path,name,size,mtime) VALUES(?,?,?,?)', inserts)
        if updates:
            conn.executemany(
                'UPDATE files SET size=?, mtime=?, name=?, prefix_hash=NULL, full_hash=NULL, '
                'phash=NULL, visual_group=NULL, error=NULL WHERE id=?', updates)
        if inserts:
            new_paths = [i[0] for i in inserts]
            qm2 = ','.join('?' * len(new_paths))
            for (fid,) in conn.execute(f'SELECT id FROM files WHERE path IN ({qm2})', new_paths):
                seen.add(fid)
        seen.discard(None)
        conn.commit()
        batch.clear()

    bar = SingleLineReporter(0, '[1/4] 遍历目录')
    try:
        for path in walk_images(root, mac_junk):
            if os.path.abspath(path) == db_abs:
                continue
            if is_placeholder(path):
                skipped_ph += 1
                continue
            st = None
            while True:
                try:
                    st = os.stat(path)
                    break
                except Exception as e:
                    if _auto_skip_now(auto_skip_errors):
                        stat_err += 1
                        break
                    bar.clear()
                    print(f'\n[文件扫描失败] 无法读取文件状态: {path}')
                    print(f'  错误原因: {type(e).__name__}: {e}')
                    choice = input("  输入 'r' 重试此文件，输入 'a' 跳过本次所有错误，按其他键或直接回车跳过: ").strip().lower()
                    if choice == 'r':
                        continue
                    elif choice == 'a':
                        _session_auto_skip[0] = True
                        stat_err += 1
                        print('  --> [已切换] 本次运行后续所有错误将自动跳过。\n')
                        break
                    else:
                        stat_err += 1
                        break
            if st is None:
                continue
            batch.append((path, os.path.basename(path), st.st_size, st.st_mtime))
            bar.update(1, current_file=path)
            if len(batch) >= BATCH:
                flush()
        flush()
    finally:
        bar.close()

    # 删除已不存在的文件记录
    conn.execute('CREATE TEMP TABLE seen_ids(id INTEGER PRIMARY KEY)')
    ids = sorted(seen)
    for i in range(0, len(ids), BATCH):
        conn.executemany('INSERT OR IGNORE INTO seen_ids(id) VALUES(?)',
                         [(x,) for x in ids[i:i + BATCH]])
    deleted = conn.execute('DELETE FROM files WHERE id NOT IN (SELECT id FROM seen_ids)').rowcount
    conn.execute('DROP TABLE seen_ids')
    conn.commit()

    total = db_file_count(conn)
    return total, added, changed, deleted or 0, skipped_ph, stat_err, mac_junk[0]

# ----------------------------- 工作进程与单文件哈希 -----------------------------


def hash_chunk(task):
    """计算文件哈希。task = (mode, [(id, path), ...]); mode: 'prefix'|'full'"""
    mode, jobs = task
    out = []
    for fid, path in jobs:
        try:
            h = hashlib.blake2b(digest_size=16)
            with open(path, 'rb') as f:
                if mode == 'prefix':
                    h.update(f.read(PREFIX_BYTES))
                else:
                    while True:
                        block = f.read(1 << 20)
                        if not block:
                            break
                        h.update(block)
            out.append((fid, path, h.hexdigest(), None))
        except Exception as e:
            out.append((fid, path, None, f'{type(e).__name__}: {e}'[:200]))
    return out


def phash_chunk(jobs):
    """计算感知哈希。jobs = [(id, path), ...]"""
    import warnings
    import imagehash
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    warnings.simplefilter('ignore', Image.DecompressionBombWarning)
    out = []
    for fid, path in jobs:
        try:
            with Image.open(path) as im:
                try:
                    im.draft('L', (64, 64))  # JPEG 快速降采样, 显著提速
                except Exception:
                    pass
                hv = imagehash.phash(im, hash_size=HASH_SIZE)
            out.append((fid, path, str(hv), None))
        except Exception as e:
            out.append((fid, path, None, f'{type(e).__name__}: {e}'[:200]))
    return out


def compute_single_hash(mode, path):
    """主进程重试计算单个文件的哈希"""
    if mode == 'prefix':
        h = hashlib.blake2b(digest_size=16)
        with open(path, 'rb') as f:
            h.update(f.read(PREFIX_BYTES))
        return h.hexdigest()
    elif mode == 'full':
        h = hashlib.blake2b(digest_size=16)
        with open(path, 'rb') as f:
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    elif mode == 'phash' or mode is None:
        import imagehash
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            try:
                im.draft('L', (64, 64))
            except Exception:
                pass
            hv = imagehash.phash(im, hash_size=HASH_SIZE)
        return str(hv)
    return None

# ----------------------------- 哈希阶段 -----------------------------


def _run_stage(conn, sql, worker_fn, mode, workers, desc, auto_skip_errors=False):
    """通用并行哈希阶段: 按 sql 选出 (id, path), 并行计算, 批量入库。
    返回 (处理数, 失败数, 结果列表)。"""
    rows = conn.execute(sql).fetchall()
    if not rows:
        print(f'{desc}: 0 个文件需计算 (已全部完成)')
        return 0, 0, []

    chunks = [rows[i:i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
    tasks = [(mode, c) for c in chunks] if mode else chunks
    failures, results = [], []
    col = {'prefix': 'prefix_hash', 'full': 'full_hash', None: 'phash'}[mode]

    bar = SingleLineReporter(len(rows), desc)
    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for chunk_res in ex.map(worker_fn, tasks):
                updates, errs = [], []
                last_path = ''
                for fid, path, digest, err in chunk_res:
                    last_path = path
                    if digest is not None:
                        updates.append((digest, fid))
                        results.append((fid, digest, None))
                    else:
                        if not _auto_skip_now(auto_skip_errors):
                            bar.clear()
                            print(f'\n[文件处理失败] 路径: {path}')
                            print(f'  错误原因: {err}')
                            retried = False
                            while True:
                                choice = input("  输入 'r' 重试此文件，输入 'a' 跳过本次所有错误，按其他键或直接回车跳过: ").strip().lower()
                                if choice == 'r':
                                    try:
                                        digest = compute_single_hash(mode, path)
                                        updates.append((digest, fid))
                                        results.append((fid, digest, None))
                                        print('  --> [重试成功！]\n')
                                        retried = True
                                        break
                                    except Exception as retry_e:
                                        print(f'  --> [重试仍失败]: {type(retry_e).__name__}: {retry_e}')
                                elif choice == 'a':
                                    _session_auto_skip[0] = True
                                    print('  --> [已切换] 本次运行后续所有错误将自动跳过。\n')
                                    break
                                else:
                                    break
                            if not retried:
                                errs.append((err, fid))
                                failures.append((fid, err))
                                results.append((fid, None, err))
                        else:
                            errs.append((err, fid))
                            failures.append((fid, err))
                            results.append((fid, None, err))

                if updates:
                    conn.executemany(f'UPDATE files SET {col}=? WHERE id=?', updates)
                if errs:
                    conn.executemany('UPDATE files SET error=? WHERE id=?', errs)
                conn.commit()
                bar.update(len(chunk_res), current_file=last_path)
    finally:
        bar.close()

    return len(rows), len(failures), results


def stage_prefix_hash(conn, workers, auto_skip_errors=False):
    conn.execute('DROP TABLE IF EXISTS dup_sizes')
    conn.execute('CREATE TEMP TABLE dup_sizes(size INTEGER PRIMARY KEY)')
    conn.execute('INSERT INTO dup_sizes SELECT size FROM files GROUP BY size HAVING COUNT(*)>1')
    n, fails, _ = _run_stage(
        conn,
        'SELECT f.id, f.path FROM files f JOIN dup_sizes d ON f.size=d.size '
        'WHERE f.prefix_hash IS NULL AND f.error IS NULL',
        hash_chunk, 'prefix', workers, '[2/4] 计算文件头哈希', auto_skip_errors=auto_skip_errors)
    return n, fails


def stage_full_hash(conn, workers, auto_skip_errors=False):
    conn.execute('DROP TABLE IF EXISTS dup_prefix')
    conn.execute('CREATE TEMP TABLE dup_prefix(size INTEGER, prefix_hash TEXT, PRIMARY KEY(size, prefix_hash))')
    conn.execute('INSERT INTO dup_prefix SELECT size, prefix_hash FROM files WHERE prefix_hash IS NOT NULL '
                 'GROUP BY size, prefix_hash HAVING COUNT(*)>1')

    rows = conn.execute(
        'SELECT f.id, f.path FROM files f JOIN dup_prefix d '
        'ON f.size=d.size AND f.prefix_hash=d.prefix_hash '
        'WHERE f.full_hash IS NULL AND f.error IS NULL').fetchall()
    if not rows:
        print('  已全部完成 (无需额外计算)')
        return 0, 0

    n, fails, _ = _run_stage(
        conn,
        'SELECT f.id, f.path FROM files f JOIN dup_prefix d '
        'ON f.size=d.size AND f.prefix_hash=d.prefix_hash '
        'WHERE f.full_hash IS NULL AND f.error IS NULL',
        hash_chunk, 'full', workers, '[3/4] 计算整文件哈希', auto_skip_errors=auto_skip_errors)
    return n, fails

# ----------------------------- 向量化并行聚类 -----------------------------


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def cluster_hashes_parallel(hashes_list, threshold, workers, progress_desc='[4/4] 相似哈希聚类'):
    """
    基于 NumPy C/SIMD 向量化 (POPCNT) 与多线程分块并行的快速汉明距离聚类。
    线程池大小严格遵循用户设定的 workers 数量。
    """
    n = len(hashes_list)
    if n < 2:
        return UnionFind(n)

    hashes_arr = np.array(hashes_list, dtype=np.uint64)
    uf = UnionFind(n)

    # 动态确定分块大小 (2000 保证 L2/L3 缓存友好与 SIMD 饱和)
    block_size = 2000 if n > 4000 else max(100, n // max(1, workers))
    num_blocks = (n + block_size - 1) // block_size

    def compare_block_pair(p):
        bi, bj = p
        s1, e1 = bi * block_size, min((bi + 1) * block_size, n)
        s2, e2 = bj * block_size, min((bj + 1) * block_size, n)
        b1 = hashes_arr[s1:e1]
        b2 = hashes_arr[s2:e2]
        diff = np.bitwise_count(b1[:, None] ^ b2[None, :])
        r, c = np.where(diff <= threshold)
        if bi == bj:
            mask = r < c
            r, c = r[mask], c[mask]
        if len(r) > 0:
            return np.column_stack((r + s1, c + s2))
        return None

    block_pairs = [(bi, bj) for bi in range(num_blocks) for bj in range(bi, num_blocks)]
    total_pairs = len(block_pairs)

    bar = SingleLineReporter(total_pairs, desc=progress_desc)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for idx, res in enumerate(ex.map(compare_block_pair, block_pairs), 1):
                if res is not None:
                    for u, v in res:
                        uf.union(int(u), int(v))
                bar.update(1, current_file=f'正在并行计算第 {idx:,}/{total_pairs:,} 块矩阵 ({workers} 线程)')
    finally:
        bar.close()

    return uf


def stage_visual(conn, workers, threshold, skip_if_clean, auto_skip_errors=False):
    """视觉相似检测: 对代表文件算 pHash, 向量化并行聚类, 展开成组。返回 (新算哈希数, 组数)。"""
    conn.execute('DROP TABLE IF EXISTS reps')
    conn.execute('CREATE TEMP TABLE reps(id INTEGER PRIMARY KEY)')
    conn.execute(
        'INSERT INTO reps '
        'SELECT MIN(id) AS id FROM files WHERE full_hash IS NOT NULL GROUP BY full_hash '
        'UNION '
        'SELECT id FROM files WHERE full_hash IS NULL AND error IS NULL')

    n_new, fails, _ = _run_stage(
        conn,
        # 过滤 macOS AppleDouble 垃圾文件 (旧版本索引过的 ._xxx 不会解码报错, 也不参与视觉分组)
        'SELECT r.id, f.path FROM reps r JOIN files f ON f.id=r.id '
        "WHERE f.phash IS NULL AND substr(f.name,1,2) != '._'",
        phash_chunk, None, workers, '[4/4] 计算感知哈希 pHash', auto_skip_errors=auto_skip_errors)
    if fails:
        print(f'[提示] {fails} 张图片无法解码, 已记录错误并跳过')

    rep_rows = conn.execute(
        'SELECT r.id, f.phash, f.full_hash FROM reps r JOIN files f ON f.id=r.id '
        'WHERE f.phash IS NOT NULL ORDER BY r.id').fetchall()

    visual_done_before = get_meta(conn, 'visual_done') == '1'
    if skip_if_clean and n_new == 0 and visual_done_before:
        print('缓存无变化, 沿用上次视觉相似分组 (如需强制重算请选 2 重新扫描)')
        return 0, conn.execute(
            'SELECT COUNT(DISTINCT visual_group) FROM files WHERE visual_group IS NOT NULL').fetchone()[0]

    conn.execute('UPDATE files SET visual_group=NULL')
    conn.commit()

    hashes = [int(h, 16) for _, h, _ in rep_rows]
    n = len(hashes)

    # 采用 NumPy SIMD 硬件级向量化 + 多线程并行计算 (使用用户指定的 workers 线程数)
    uf = cluster_hashes_parallel(hashes, threshold, workers, progress_desc='[4/4] 相似哈希聚类')

    # 聚类结果 -> 代表索引
    clusters = defaultdict(list)
    for i in range(n):
        clusters[uf.find(i)].append(i)

    hash_to_root, id_to_root = {}, {}
    for i, (fid, _, full_hash) in enumerate(rep_rows):
        root = uf.find(i)
        if full_hash is not None:
            hash_to_root[full_hash] = root
        else:
            id_to_root[fid] = root

    # 展开代表 -> 全部成员, 并按聚类归组
    members = defaultdict(list)      # root -> [(id, path)]
    cur = conn.execute('SELECT id, path, full_hash FROM files WHERE error IS NULL '
                       'ORDER BY full_hash IS NULL, full_hash, id')
    for fid, path, full_hash in cur:
        if full_hash is not None:
            root = hash_to_root.get(full_hash)
        else:
            root = id_to_root.get(fid)
        if root is None:
            continue
        members[root].append((fid, path))

    gid = 0
    groups = []
    for root, idxs in clusters.items():
        if len(idxs) < 2:
            continue
        ms = sorted(members[root], key=lambda x: x[0])
        gid += 1
        groups.append((gid, ms))

    if groups:
        for gid, ms in groups:
            conn.executemany('UPDATE files SET visual_group=? WHERE id=?',
                             [(gid, fid) for fid, _ in ms])
        conn.commit()
    set_meta(conn, 'visual_done', '1')
    set_meta(conn, 'threshold', threshold)
    conn.commit()
    return n_new, len(groups)

# ----------------------------- 分组查询与输出 -----------------------------


def show_group(kind, name, paths):
    """格式化输出单组重复图片"""
    lines = [f'[{kind}] {name} ({len(paths)} 份):']
    for i, p in enumerate(paths, 1):
        lines.append(f'  dup path{i}: {p}')
    print('\n'.join(lines) + '\n')


def iter_exact_groups(conn):
    """查询内容完全一致(整文件哈希相同)的重复组"""
    sql = """
    SELECT f.id, f.name, f.path, f.size, f.full_hash
    FROM files f
    JOIN (
        SELECT full_hash FROM files
        WHERE full_hash IS NOT NULL
        GROUP BY full_hash
        HAVING COUNT(*) > 1
    ) d ON f.full_hash = d.full_hash
    ORDER BY f.full_hash, f.id
    """
    cur = conn.execute(sql)
    group = []
    for row in cur:
        if group and row[4] != group[-1][4]:
            if len(group) > 1:
                yield group
            group = []
        group.append(row)
    if len(group) > 1:
        yield group


def iter_visual_groups(conn):
    """查询感知哈希视觉相似组"""
    sql = """
    SELECT f.id, f.name, f.path, f.size, f.visual_group
    FROM files f
    WHERE f.visual_group IS NOT NULL
    ORDER BY f.visual_group, f.id
    """
    cur = conn.execute(sql)
    group = []
    for row in cur:
        if group and row[4] != group[-1][4]:
            if len(group) > 1:
                yield group
            group = []
        group.append(row)
    if len(group) > 1:
        yield group

# ----------------------------- 导出报告 -----------------------------


def export_report(conn, out_dir, echo=True, suffix=''):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_csv_name = f'duplicates_report{suffix}.csv'
    base_md_name = f'duplicates_report{suffix}.md'
    csv_path = out_dir / base_csv_name
    md_path = out_dir / base_md_name

    sections = [
        (KIND_EXACT, list(iter_exact_groups(conn))),
        (KIND_VISUAL, list(iter_visual_groups(conn))),
    ]

    import csv as _csv
    try:
        f_csv = open(csv_path, 'w', encoding='utf-8-sig', newline='')
    except PermissionError:
        ts = time.strftime("%Y%m%d_%H%M%S")
        csv_path = out_dir / f'duplicates_report{suffix}_{ts}.csv'
        print(f'\n[提示] 原 {base_csv_name} 可能已被 Excel 打开占用，已自动写入新文件: {csv_path.name}')
        f_csv = open(csv_path, 'w', encoding='utf-8-sig', newline='')

    with f_csv:
        w = _csv.writer(f_csv)
        for kind, groups in sections:
            if not groups:
                continue
            for group in groups:
                label = group[0][1]
                paths = [row[2] for row in group]
                w.writerow([label] + paths)

    root = get_meta(conn, 'root', '?')
    total_files = db_file_count(conn)

    try:
        f_md = open(md_path, 'w', encoding='utf-8')
    except PermissionError:
        ts = time.strftime("%Y%m%d_%H%M%S")
        md_path = out_dir / f'duplicates_report{suffix}_{ts}.md'
        f_md = open(md_path, 'w', encoding='utf-8')

    with f_md:
        f_md.write('# 重复图片报告\n\n')
        f_md.write(f'- 扫描目录: `{root}`\n')
        f_md.write(f'- 生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        f_md.write(f'- 索引文件总数: {total_files:,}\n')
        for kind, groups in sections:
            n_files = sum(len(g) for g in groups)
            f_md.write(f'- {kind}: {len(groups):,} 组, 涉及 {n_files:,} 个文件\n')
        for kind, groups in sections:
            if not groups:
                continue
            f_md.write(f'\n## {kind} ({len(groups):,} 组)\n')
            for gi, group in enumerate(groups, 1):
                label = group[0][1]
                f_md.write(f'\n### 组 {gi}: `{label}` ({len(group)} 份)\n\n')
                for i, row in enumerate(group, 1):
                    f_md.write(f'- 路径 {i}: `{row[2]}` ({row[3]:,} 字节)\n')
                f_md.write('\n')

    if echo:
        print('\n' + '=' * 60)
        print('重复文件清单摘要 (完整清单见导出的 CSV / Markdown 报告)')
        print('=' * 60)
        for kind, groups in sections:
            print(f'\n===== {kind}: 共 {len(groups):,} 组 =====')
            for group in groups[:5]:
                show_group(kind, group[0][1], [r[2] for r in group])
            if len(groups) > 5:
                print(f'  ... 其余 {len(groups) - 5:,} 组已写入报告文件 ...\n')

    print(f'\n[导出完成]')
    print(f'  CSV (每行格式: filename, dup path1, dup path2...): {csv_path}')
    print(f'  Markdown 报告:                                    {md_path}')
    return csv_path, md_path

# ----------------------------- 扫描主流程 -----------------------------


def run_scan(root, db_path, fresh, visual, threshold, workers, interactive,
             auto_skip_errors=False, skip_walk=False, report_suffix=''):
    root = os.path.abspath(root)
    _session_auto_skip[0] = False   # 输入 'a' 的跳过仅本次运行生效, 每次扫描开始时重置
    if not os.path.isdir(root):
        print(f'错误: 目录不存在 - {root}')
        return 1

    db_abs = os.path.abspath(db_path)
    conn = open_db(db_abs)

    prev_root = get_meta(conn, 'root')
    if prev_root and prev_root != root:
        print(f'[注意] 缓存数据库属于另一目录: {prev_root}')
        if fresh or (interactive and input('按新目录重新扫描? (yes/no): ').strip().lower() == 'yes'):
            fresh = True
        else:
            print('已取消。')
            return 1
    if fresh:
        conn.execute('DELETE FROM files')
        conn.execute('DELETE FROM meta')
        conn.commit()
        print('已清空缓存, 开始全新扫描。')

    set_meta(conn, 'root', root)
    conn.commit()

    t0 = time.time()
    print(f'\n===== 开始扫描: {root} =====')
    if skip_walk and db_file_count(conn) > 0:
        total = db_file_count(conn)
        changed_total = 0
        print(f'[1/4] 遍历目录: 已跳过 (直接使用数据库已有的 {total:,} 个文件索引)')
    else:
        total, added, changed, deleted, ph_skip, stat_err, mac_skip = enumerate_into_db(
            conn, root, db_abs, auto_skip_errors=auto_skip_errors)
        changed_total = added + changed + deleted
        print(f'  索引统计: 共 {total:,} 个图片文件 | 新增 {added:,} | 变化 {changed:,} | 已删除 {deleted:,} '
              f'| OneDrive占位跳过 {ph_skip:,} | Mac垃圾跳过 {mac_skip:,} | 读取失败 {stat_err:,}')
    if total < 2:
        print('文件不足, 无需比较。')
        return 0

    print('\n[2/4] 计算文件头哈希 (仅大小重复的文件)')
    n, fails = stage_prefix_hash(conn, workers, auto_skip_errors=auto_skip_errors)
    if n > 0:
        print(f'  处理完成: {n:,} 个文件, 失败 {fails:,}')

    print('\n[3/4] 计算整文件哈希 (仅头部冲突的文件)')
    n, fails = stage_full_hash(conn, workers, auto_skip_errors=auto_skip_errors)
    exact_groups = list(iter_exact_groups(conn))
    if n > 0:
        print(f'  处理完成: {n:,} 个文件, 失败 {fails:,} | 内容重复: {len(exact_groups):,} 组')
    else:
        print(f'  内容重复: {len(exact_groups):,} 组')

    if visual:
        print(f'\n[4/4] 视觉相似检测 (pHash, 阈值 {threshold})')
        n_new, v_groups = stage_visual(conn, workers, threshold,
                                       skip_if_clean=(changed_total == 0),
                                       auto_skip_errors=auto_skip_errors)
        print(f'  处理完成: 新算 {n_new:,} 张图片 | 视觉相似: {v_groups:,} 组')
    else:
        print('\n[4/4] 视觉相似检测: 已跳过 (--no-visual)')

    # 汇总: 每组保留一份, 其余为冗余
    redundant_files = sum(len(g) - 1 for g in exact_groups)
    redundant_bytes = sum(g[0][3] * (len(g) - 1) for g in exact_groups)
    print('\n' + '=' * 60)
    print(f'扫描完成, 耗时 {time.time() - t0:.1f} 秒')
    print(f'  索引文件总数:  {total:,}')
    print(f'  内容重复组:    {len(exact_groups):,} 组 (冗余 {redundant_files:,} 个文件, '
          f'可释放约 {redundant_bytes / 1048576:.1f} MB)')
    if visual:
        vg = conn.execute('SELECT COUNT(DISTINCT visual_group) FROM files '
                          'WHERE visual_group IS NOT NULL').fetchone()[0]
        print(f'  视觉相似组:    {vg:,} 组')
    print('=' * 60)

    if interactive and input('\n是否现在导出报告 (CSV + Markdown)? [Y/n]: ').strip().lower() != 'n':
        export_report(conn, os.path.dirname(db_abs) or '.', echo=True, suffix=report_suffix)

    conn.close()
    return 0


def run_temp_scan(root, db_dir='.', visual=True, threshold=DEFAULT_THRESHOLD, workers=4, auto_skip_errors=False):
    """单次临时隔离扫描: 使用独立临时数据库, 导出 _temp 报告, 绝不污染主数据库"""
    temp_db_path = os.path.abspath(os.path.join(db_dir, TEMP_DB_FILENAME))
    print('\n' + '=' * 50)
    print('        启动临时隔离扫描 (One-time Temp Scan)')
    print('=' * 50)
    print(f'临时数据库: {temp_db_path}')
    print('说明: 此模式完全隔离，绝不影响主数据库文件')
    print('-' * 50)

    ret = run_scan(root, temp_db_path, fresh=True, visual=visual, threshold=threshold,
                   workers=workers, interactive=False, auto_skip_errors=auto_skip_errors,
                   skip_walk=False, report_suffix='_temp')
    if ret != 0:
        return ret

    conn = open_db(temp_db_path)
    try:
        export_report(conn, db_dir, echo=True, suffix='_temp')
    finally:
        conn.close()

    print('\n' + '-' * 50)
    choice = input(f'临时扫描完成。是否删除临时数据库 ({TEMP_DB_FILENAME})? [Y/n]: ').strip().lower()
    if choice != 'n':
        for f in [temp_db_path, temp_db_path + '-wal', temp_db_path + '-shm']:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        print('已安全清理临时数据库文件。')
    else:
        print(f'临时数据库已保留在: {temp_db_path}')
    return 0

# ----------------------------- 交互菜单 -----------------------------


def prompt(text, default):
    s = input(f'{text} [{default}]: ').strip()
    return s if s else str(default)


def menu(db_path):
    auto_skip_errors = False
    while True:
        conn = open_db(os.path.abspath(db_path))
        last_root = get_meta(conn, 'root')
        n_files = db_file_count(conn)
        conn.close()
        print('\n' + '=' * 50)
        print('        重复图片查找工具 (海量文件版)')
        print('=' * 50)
        print(f'主缓存数据库: {db_path} (已索引 {n_files:,} 个文件)')
        print(f'上次扫描目录: {last_root or "未设置"}')
        mode_label = '自动跳过错误(不暂停)' if auto_skip_errors else '遇错暂停(提示输入 r 重试)'
        print(f'错误处理模式: {mode_label}')
        print('-' * 50)
        print(' [1] 开始/继续查找重复项 (断点续扫主库)')
        print(' [2] 重新扫描主库 (清空并重新扫描)')
        print(' [3] 导出主库重复列表 (CSV + Markdown)')
        print(' [4] 临时隔离扫描 (使用临时数据库, 不污染主库, 导出 _temp 报告)')
        print(' [5] 优化与清理数据库 (VACUUM 整理碎片 & 索引优化)')
        print(f' [6] 切换错误处理模式 (当前: {"自动跳过" if auto_skip_errors else "暂停重试"})')
        print(' [7] 清理 Mac 垃圾文件 (删除扫描目录中的 ._*.xxx 与 .DS_Store, 删除前需确认)')
        print(' [0] 退出')
        choice = input('请选择: ').strip()

        if choice == '0':
            break
        elif choice == '4':
            root = prompt('临时扫描目录', last_root or '.')
            workers_default = 4
            try:
                workers = int(prompt('并行进程数', workers_default))
            except ValueError:
                workers = workers_default
            visual = prompt('检测视觉相似图片? (重新压缩/改尺寸副本, y/n)', 'y').lower() != 'n'
            try:
                threshold = int(prompt('视觉相似汉明距离阈值 (0-64, 越小越严格)', DEFAULT_THRESHOLD))
            except ValueError:
                threshold = DEFAULT_THRESHOLD
            threshold = max(0, min(64, threshold))
            db_dir = os.path.dirname(os.path.abspath(db_path)) or '.'
            try:
                run_temp_scan(root, db_dir=db_dir, visual=visual, threshold=threshold,
                              workers=workers, auto_skip_errors=auto_skip_errors)
            except KeyboardInterrupt:
                print('\n临时扫描已中断。')
        elif choice == '5':
            clean_database(db_path)
        elif choice == '6':
            auto_skip_errors = not auto_skip_errors
            if auto_skip_errors:
                print('\n[已设置] 遇到读取错误时将【自动跳过并记录】，不暂停重试。')
            else:
                print('\n[已设置] 遇到读取错误时将【暂停并提示输入 r 重试 (或输入 a 跳过本次所有错误)】。')
            continue
        elif choice == '7':
            root = prompt('要清理的目录', last_root or '.')
            try:
                clean_mac_junk(root, db_path)
            except KeyboardInterrupt:
                print('\n清理已中断。')
            continue
        elif choice in ('1', '2'):
            fresh = choice == '2'
            if fresh and n_files > 0:
                if input('确认清空缓存并重新扫描主库? (yes/no): ').strip().lower() != 'yes':
                    continue
            root = prompt('扫描目录', last_root or '.')
            workers_default = 4
            try:
                workers = int(prompt('并行进程数', workers_default))
            except ValueError:
                workers = workers_default
            visual = prompt('检测视觉相似图片? (重新压缩/改尺寸副本, y/n)', 'y').lower() != 'n'
            try:
                threshold = int(prompt('视觉相似汉明距离阈值 (0-64, 越小越严格)', DEFAULT_THRESHOLD))
            except ValueError:
                threshold = DEFAULT_THRESHOLD
            threshold = max(0, min(64, threshold))

            skip_walk = False
            if not fresh and n_files > 0:
                # 断点续扫时，询问是否跳过磁盘重新遍历，直接继续未完成的哈希/聚类阶段
                check_disk = prompt('重新检查磁盘目录变动? (y/n, 选 n 则直接继续未完成阶段)', 'n').lower()
                skip_walk = (check_disk != 'y')

            try:
                run_scan(root, db_path, fresh, visual, threshold, workers, interactive=True,
                         auto_skip_errors=auto_skip_errors, skip_walk=skip_walk)
            except KeyboardInterrupt:
                print('\n已中断。下次选择 [1] 可从断点继续, 已完成的结果保存在数据库中。')
        elif choice == '3':
            conn = open_db(os.path.abspath(db_path))
            if db_file_count(conn) == 0:
                print('数据库为空, 请先执行扫描 (选项 1 或 2)。')
                conn.close()
                continue
            try:
                export_report(conn, os.path.dirname(os.path.abspath(db_path)) or '.', echo=True)
            finally:
                conn.close()
        else:
            print('无效选项。')

# ----------------------------- 入口 -----------------------------


def main():
    parser = argparse.ArgumentParser(
        description='重复图片查找工具 (海量文件版)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('用法:', 1)[-1] if __doc__ else '')
    parser.add_argument('--db', default=str(Path(__file__).resolve().parent / DB_FILENAME),
                        help='SQLite 缓存数据库路径')
    sub = parser.add_subparsers(dest='cmd')

    p_scan = sub.add_parser('scan', help='扫描目录 (默认断点续扫)')
    p_scan.add_argument('root', help='要扫描的图片目录')
    p_scan.add_argument('--fresh', action='store_true', help='清空缓存重新扫描')
    p_scan.add_argument('--no-visual', action='store_true', help='跳过视觉相似检测')
    p_scan.add_argument('--threshold', type=int, default=DEFAULT_THRESHOLD, help='汉明距离阈值')
    p_scan.add_argument('--workers', type=int, default=4)
    p_scan.add_argument('--auto-skip-errors', action='store_true', help='遇到错误自动跳过，不暂停要求输入r重试')
    p_scan.add_argument('--skip-walk', action='store_true', help='跳过磁盘遍历，直接对数据库已有文件继续哈希/聚类')
    p_scan.add_argument('--temp', action='store_true', help='临时隔离扫描模式 (使用临时数据库，导出 _temp 报告，不污染主库)')

    sub.add_parser('export', help='导出重复列表 (CSV + Markdown)')
    sub.add_parser('clean-db', help='优化并整理数据库碎片 (VACUUM & ANALYZE)')
    p_clean = sub.add_parser('clean-junk', help='删除目录中的 macOS 垃圾文件 (._* 与 .DS_Store, 删除前需确认)')
    p_clean.add_argument('root', help='要清理的目录')

    args = parser.parse_args()

    if args.cmd == 'scan':
        if args.temp:
            db_dir = os.path.dirname(os.path.abspath(args.db)) or '.'
            try:
                sys.exit(run_temp_scan(args.root, db_dir=db_dir, visual=not args.no_visual,
                                       threshold=args.threshold, workers=args.workers,
                                       auto_skip_errors=args.auto_skip_errors))
            except KeyboardInterrupt:
                print('\n临时扫描已中断。')
                sys.exit(130)
        else:
            try:
                sys.exit(run_scan(args.root, args.db, args.fresh, not args.no_visual,
                                  args.threshold, args.workers, interactive=False,
                                  auto_skip_errors=args.auto_skip_errors,
                                  skip_walk=args.skip_walk))
            except KeyboardInterrupt:
                print('\n已中断。下次运行可断点续扫。')
                sys.exit(130)
    elif args.cmd == 'export':
        conn = open_db(os.path.abspath(args.db))
        try:
            if db_file_count(conn) == 0:
                print('数据库为空, 请先扫描。')
                sys.exit(1)
            export_report(conn, os.path.dirname(os.path.abspath(args.db)) or '.')
        finally:
            conn.close()
    elif args.cmd == 'clean-db':
        clean_database(args.db)
    elif args.cmd == 'clean-junk':
        sys.exit(clean_mac_junk(args.root, args.db))
    else:
        try:
            menu(args.db)
        except KeyboardInterrupt:
            print('\n已退出。')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重复图片查找工具 v2 —— 面向海量文件（十万~百万级）
=====================================================

特性:
  * 交互菜单: [1] 开始/继续查找(断点续扫)  [2] 重新扫描  [3] 导出列表(CSV+Markdown)
  * SQLite 缓存: 中断后可续扫; 增量扫描自动跳过 大小+mtime 未变化的文件
  * 分级漏斗加速: 文件名 -> 文件大小 -> 文件头哈希 -> 整文件哈希 -> (可选)感知哈希
  * 多进程并行计算哈希; 查找过程中实时打印已发现的重复组

重复类型:
  * 文件名重复: 基础文件名相同(不区分大小写), 与内容无关
  * 内容重复:   字节级完全一致(整文件 BLAKE2b-128 哈希)
  * 视觉相似(可选): pHash 64位指纹 + BK树 + 汉明距离聚类,
    可识别重新压缩/改分辨率/加水印/改格式后肉眼相同的图片

依赖:
  pip install pillow imagehash tqdm

用法:
  python duplicate_image_finder_v2.py                          # 交互菜单
  python duplicate_image_finder_v2.py scan <目录>              # 续扫
  python duplicate_image_finder_v2.py scan <目录> --fresh      # 重新扫描
  python duplicate_image_finder_v2.py scan <目录> --no-visual  # 只查字节级重复
  python duplicate_image_finder_v2.py export                   # 导出报告
"""

import argparse
import hashlib
import os
import sqlite3
import sys
import time
import warnings
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image
from tqdm import tqdm

# 禁用大图像素上限限制及 DecompressionBomb 告警
Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter('ignore', Image.DecompressionBombWarning)

# ----------------------------- 常量 -----------------------------

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif'}
DB_FILENAME = 'image_dedup.db'
PREFIX_BYTES = 64 * 1024      # 文件头哈希的采样长度
CHUNK_SIZE = 2000             # 每个进程任务处理的文件数
BATCH = 5000                  # 数据库批量提交大小
HASH_SIZE = 8                 # pHash 8x8 = 64 位指纹
DEFAULT_THRESHOLD = 6         # 汉明距离阈值(0=完全一致, 建议 5~8)

KIND_NAME = '文件名重复'
KIND_EXACT = '内容重复'
KIND_VISUAL = '视觉相似'

# ------------------- Windows OneDrive 占位文件检测 -------------------

if sys.platform == 'win32':
    import ctypes
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
CREATE INDEX IF NOT EXISTS ix_files_name ON files(name COLLATE NOCASE);
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

# ----------------------------- 目录遍历 -----------------------------


def walk_images(root: str):
    """确定性(按名称排序)递归遍历, 产出图片文件路径"""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError as e:
            tqdm.write(f'[警告] 无法进入目录 {d}: {e}')
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    if os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS:
                        yield entry.path
            except OSError:
                continue


def enumerate_into_db(conn, root: str, db_abs: str):
    """遍历目录并同步到数据库。返回 (总数, 新增, 变化, 删除, 占位跳过, 出错)"""
    added = changed = deleted = skipped_ph = stat_err = 0
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

    for path in walk_images(root):
        if os.path.abspath(path) == db_abs:
            continue
        if is_placeholder(path):
            skipped_ph += 1
            continue
        try:
            st = os.stat(path)
        except OSError:
            stat_err += 1
            continue
        batch.append((path, os.path.basename(path), st.st_size, st.st_mtime))
        if len(batch) >= BATCH:
            flush()
    flush()

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
    return total, added, changed, deleted or 0, skipped_ph, stat_err

# ----------------------------- 工作进程 -----------------------------


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
            out.append((fid, h.hexdigest(), None))
        except Exception as e:
            out.append((fid, None, f'{type(e).__name__}: {e}'[:200]))
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
            out.append((fid, str(hv), None))
        except Exception as e:
            out.append((fid, None, f'{type(e).__name__}: {e}'[:200]))
    return out

# ----------------------------- 哈希阶段 -----------------------------


def _run_stage(conn, sql, worker_fn, mode, workers, desc):
    """通用并行哈希阶段: 按 sql 选出 (id, path), 并行计算, 批量入库。
    返回 (处理数, 失败数, 结果迭代期间的回调由调用方处理)。"""
    rows = conn.execute(sql).fetchall()
    if not rows:
        return 0, 0, []

    chunks = [rows[i:i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
    tasks = [(mode, c) for c in chunks] if mode else chunks
    failures, results = [], []
    col = {'prefix': 'prefix_hash', 'full': 'full_hash', None: 'phash'}[mode]

    with ProcessPoolExecutor(max_workers=workers) as ex, \
            tqdm(total=len(rows), desc=desc, unit='文件') as bar:
        for chunk_res in ex.map(worker_fn, tasks):
            updates, errs = [], []
            for fid, digest, err in chunk_res:
                results.append((fid, digest, err))
                if digest is not None:
                    updates.append((digest, fid))
                else:
                    errs.append((err, fid))
                    failures.append((fid, err))
            if updates:
                conn.executemany(f'UPDATE files SET {col}=? WHERE id=?', updates)
            if errs:
                conn.executemany('UPDATE files SET error=? WHERE id=?', errs)
            conn.commit()
            bar.update(len(chunk_res))
    return len(rows), len(failures), results


def stage_prefix_hash(conn, workers):
    conn.execute('DROP TABLE IF EXISTS dup_sizes')
    conn.execute('CREATE TEMP TABLE dup_sizes AS '
                 'SELECT size FROM files GROUP BY size HAVING COUNT(*)>1')
    n, fails, _ = _run_stage(
        conn,
        'SELECT f.id, f.path FROM files f JOIN dup_sizes d ON f.size=d.size '
        'WHERE f.prefix_hash IS NULL AND f.error IS NULL',
        hash_chunk, 'prefix', workers, '文件头哈希(仅大小重复的文件)')
    return n, fails


def stage_full_hash(conn, workers, live_print=True):
    conn.execute('DROP TABLE IF EXISTS dup_prefix')
    conn.execute('CREATE TEMP TABLE dup_prefix AS '
                 'SELECT size, prefix_hash FROM files WHERE prefix_hash IS NOT NULL '
                 'GROUP BY size, prefix_hash HAVING COUNT(*)>1')

    # 已有整文件哈希的组(用于实时发现新成员)
    known = defaultdict(list)
    for h, path in conn.execute(
            'SELECT full_hash, path FROM files WHERE full_hash IS NOT NULL ORDER BY id'):
        known[h].append(path)

    rows = conn.execute(
        'SELECT f.id, f.path FROM files f JOIN dup_prefix d '
        'ON f.size=d.size AND f.prefix_hash=d.prefix_hash '
        'WHERE f.full_hash IS NULL AND f.error IS NULL').fetchall()
    if not rows:
        return 0, 0

    chunks = [rows[i:i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
    failures = 0
    with ProcessPoolExecutor(max_workers=workers) as ex, \
            tqdm(total=len(rows), desc='整文件哈希(仅头部冲突的文件)', unit='文件') as bar:
        for chunk_res in ex.map(hash_chunk, [('full', c) for c in chunks]):
            updates, errs = [], []
            for fid, digest, err in chunk_res:
                if digest is not None:
                    updates.append((digest, fid))
                else:
                    errs.append((err, fid))
                    failures += 1
            if updates:
                conn.executemany('UPDATE files SET full_hash=? WHERE id=?', updates)
            if errs:
                conn.executemany('UPDATE files SET error=? WHERE id=?', errs)
            conn.commit()

            if live_print:
                qm = ','.join('?' * len(updates))
                if updates:
                    for h, path in conn.execute(
                            f'SELECT full_hash, path FROM files WHERE id IN ({qm}) ORDER BY id',
                            [u[1] for u in updates]):
                        lst = known[h]
                        lst.append(path)
                        if len(lst) == 2:
                            show_group(KIND_EXACT, os.path.basename(lst[0]), lst)
                        elif len(lst) > 2:
                            tqdm.write(f'+ 同组新增: {path}  (组: {os.path.basename(lst[0])})\n')
            bar.update(len(chunk_res))
    conn.commit()
    return len(rows), failures

# ----------------------------- BK 树聚类 -----------------------------


class _BKNode:
    __slots__ = ('h', 'idxs', 'children')

    def __init__(self, h, idx):
        self.h = h
        self.idxs = [idx]
        self.children = {}


class BKTree:
    """迭代式 BK 树, 按汉明距离近邻查询; 相同哈希合并到一个节点"""

    def __init__(self):
        self.root = None

    def insert(self, h, idx):
        if self.root is None:
            self.root = _BKNode(h, idx)
            return
        cur = self.root
        while True:
            d = (h ^ cur.h).bit_count()
            if d == 0:
                cur.idxs.append(idx)
                return
            nxt = cur.children.get(d)
            if nxt is None:
                cur.children[d] = _BKNode(h, idx)
                return
            cur = nxt

    def query(self, h, threshold, out):
        if self.root is None:
            return
        stack = [self.root]
        while stack:
            node = stack.pop()
            d = (h ^ node.h).bit_count()
            if d <= threshold:
                out.extend(node.idxs)
            lo, hi = d - threshold, d + threshold
            for dd, child in node.children.items():
                if lo <= dd <= hi:
                    stack.append(child)


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


def stage_visual(conn, workers, threshold, skip_if_clean):
    """视觉相似检测: 对代表文件算 pHash, BK树聚类, 展开成组。
    返回 (新算哈希数, 组数)。"""
    # 代表 = 每个整文件哈希组取第一个 + 所有无整文件哈希的文件(大小唯一/头部唯一)
    conn.execute('DROP TABLE IF EXISTS reps')
    conn.execute(
        'CREATE TEMP TABLE reps AS '
        'SELECT MIN(id) AS id FROM files WHERE full_hash IS NOT NULL GROUP BY full_hash '
        'UNION '
        'SELECT id FROM files WHERE full_hash IS NULL AND error IS NULL')

    n_new, fails, _ = _run_stage(
        conn,
        'SELECT r.id, f.path FROM reps r JOIN files f ON f.id=r.id WHERE f.phash IS NULL',
        phash_chunk, None, workers, '感知哈希 pHash(视觉相似)')
    if fails:
        tqdm.write(f'[提示] {fails} 张图片无法解码, 已跳过视觉相似检测')

    rep_rows = conn.execute(
        'SELECT r.id, f.phash, f.full_hash FROM reps r JOIN files f ON f.id=r.id '
        'WHERE f.phash IS NOT NULL ORDER BY r.id').fetchall()

    visual_done_before = get_meta(conn, 'visual_done') == '1'
    if skip_if_clean and n_new == 0 and visual_done_before:
        tqdm.write('缓存无变化, 沿用上次视觉相似分组 (如需强制重算请选 2 重新扫描)')
        return 0, conn.execute(
            'SELECT COUNT(DISTINCT visual_group) FROM files WHERE visual_group IS NOT NULL').fetchone()[0]

    conn.execute('UPDATE files SET visual_group=NULL')
    conn.commit()

    hashes = [int(h, 16) for _, h, _ in rep_rows]
    n = len(hashes)
    uf = UnionFind(n)
    tree = BKTree()
    hits_buf = []
    for i in tqdm(range(n), desc='BK树聚类(汉明距离)', unit='哈希'):
        hits_buf.clear()
        tree.query(hashes[i], threshold, hits_buf)
        for j in hits_buf:
            uf.union(i, j)
        tree.insert(hashes[i], i)

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
            continue  # 单一内容的组(纯字节重复)已在"内容重复"中报告
        ms = sorted(members[root], key=lambda x: x[0])
        gid += 1
        groups.append((gid, ms))
        show_group(KIND_VISUAL, os.path.basename(ms[0][1]), [p for _, p in ms])

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
    tqdm.write('\n'.join(lines) + '\n')


def iter_name_groups(conn):
    """查询文件名重复组。使用 SQL GROUP BY HAVING 仅筛选存在重复的组"""
    sql = """
    SELECT f.id, f.name, f.path, f.size
    FROM files f
    JOIN (
        SELECT name FROM files
        GROUP BY name COLLATE NOCASE
        HAVING COUNT(*) > 1
    ) d ON f.name = d.name COLLATE NOCASE
    ORDER BY f.name COLLATE NOCASE, f.id
    """
    cur = conn.execute(sql)
    group = []
    for row in cur:
        if group and row[1].lower() != group[-1][1].lower():
            if len(group) > 1:
                yield group
            group = []
        group.append(row)
    if len(group) > 1:
        yield group


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


def export_report(conn, out_dir, echo=True):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / 'duplicates_report.csv'
    md_path = out_dir / 'duplicates_report.md'

    sections = [
        (KIND_NAME, list(iter_name_groups(conn))),
        (KIND_EXACT, list(iter_exact_groups(conn))),
        (KIND_VISUAL, list(iter_visual_groups(conn))),
    ]

    import csv as _csv
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = _csv.writer(f)
        for kind, groups in sections:
            if not groups:
                continue
            for group in groups:
                label = group[0][1]
                paths = [row[2] for row in group]
                w.writerow([label] + paths)

    root = get_meta(conn, 'root', '?')
    total_files = db_file_count(conn)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('# 重复图片报告\n\n')
        f.write(f'- 扫描目录: `{root}`\n')
        f.write(f'- 生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write(f'- 索引文件总数: {total_files}\n')
        for kind, groups in sections:
            n_files = sum(len(g) for g in groups)
            f.write(f'- {kind}: {len(groups)} 组, 涉及 {n_files} 个文件\n')
        for kind, groups in sections:
            if not groups:
                continue
            f.write(f'\n## {kind} ({len(groups)} 组)\n')
            for gi, group in enumerate(groups, 1):
                label = group[0][1]
                f.write(f'\n### 组 {gi}: `{label}` ({len(group)} 份)\n\n')
                for i, row in enumerate(group, 1):
                    f.write(f'- 路径 {i}: `{row[2]}` ({row[3]:,} 字节)\n')
                f.write('\n')

    if echo:
        print('\n' + '=' * 60)
        print('重复文件清单摘要 (完整清单见导出的 CSV / Markdown 报告)')
        print('=' * 60)
        for kind, groups in sections:
            print(f'\n===== {kind}: 共 {len(groups)} 组 =====')
            for group in groups[:5]:
                show_group(kind, group[0][1], [r[2] for r in group])
            if len(groups) > 5:
                print(f'  ... 其余 {len(groups) - 5} 组已写入报告文件 ...\n')

    print(f'\n[导出完成]')
    print(f'  CSV (每行格式: filename, dup path1, dup path2...): {csv_path}')
    print(f'  Markdown 报告:                                    {md_path}')
    return csv_path, md_path

# ----------------------------- 扫描主流程 -----------------------------


def run_scan(root, db_path, fresh, visual, threshold, workers, interactive):
    root = os.path.abspath(root)
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
    print(f'\n[1/5] 遍历目录: {root}')
    total, added, changed, deleted, ph_skip, stat_err = enumerate_into_db(conn, root, db_abs)
    changed_total = added + changed + deleted
    print(f'  共 {total} 个图片文件 | 新增 {added} | 变化 {changed} | 已删除 {deleted} '
          f'| OneDrive占位跳过 {ph_skip} | 读取失败 {stat_err}')
    if total < 2:
        print('文件不足, 无需比较。')
        return 0

    print('\n[2/5] 检查文件名重复')
    name_groups = list(iter_name_groups(conn))
    for g in name_groups:
        show_group(KIND_NAME, g[0][1], [r[2] for r in g])
    print(f'  文件名重复: {len(name_groups)} 组')

    print('\n[3/5] 计算文件头哈希 (仅大小重复的文件)')
    n, fails = stage_prefix_hash(conn, workers)
    print(f'  处理 {n} 个文件, 失败 {fails}')

    print('\n[4/5] 计算整文件哈希 (仅头部冲突的文件) -> 确认内容重复')
    n, fails = stage_full_hash(conn, workers)
    exact_groups = list(iter_exact_groups(conn))
    print(f'  处理 {n} 个文件, 失败 {fails} | 内容重复: {len(exact_groups)} 组')

    if visual:
        print(f'\n[5/5] 视觉相似检测 (pHash, 阈值 {threshold})')
        n_new, v_groups = stage_visual(conn, workers, threshold,
                                       skip_if_clean=(changed_total == 0))
        print(f'  视觉相似: {v_groups} 组')
    else:
        print('\n[5/5] 视觉相似检测: 已跳过 (--no-visual)')

    # 汇总: 每组保留一份, 其余为冗余
    redundant_files = sum(len(g) - 1 for g in exact_groups)
    redundant_bytes = sum(g[0][3] * (len(g) - 1) for g in exact_groups)
    print('\n' + '=' * 60)
    print(f'扫描完成, 耗时 {time.time() - t0:.1f} 秒')
    print(f'  索引文件:     {total}')
    print(f'  文件名重复:   {len(name_groups)} 组')
    print(f'  内容重复:     {len(exact_groups)} 组 (冗余 {redundant_files} 个文件, '
          f'可释放约 {redundant_bytes / 1048576:.1f} MB)')
    if visual:
        vg = conn.execute('SELECT COUNT(DISTINCT visual_group) FROM files '
                          'WHERE visual_group IS NOT NULL').fetchone()[0]
        print(f'  视觉相似:     {vg} 组')
    print('=' * 60)

    if interactive and input('\n是否现在导出报告 (CSV + Markdown)? [Y/n]: ').strip().lower() != 'n':
        export_report(conn, os.path.dirname(db_abs) or '.', echo=False)

    conn.close()
    return 0

# ----------------------------- 交互菜单 -----------------------------


def prompt(text, default):
    s = input(f'{text} [{default}]: ').strip()
    return s if s else str(default)


def menu(db_path):
    while True:
        conn = open_db(os.path.abspath(db_path))
        last_root = get_meta(conn, 'root')
        n_files = db_file_count(conn)
        conn.close()
        print('\n' + '=' * 46)
        print('        重复图片查找工具 (海量文件版)')
        print('=' * 46)
        print(f'缓存数据库: {db_path} (已索引 {n_files} 个文件)')
        print(f'上次扫描目录: {last_root or "未设置"}')
        print('-' * 46)
        print(' [1] 开始/继续查找重复项 (断点续扫)')
        print(' [2] 重新扫描 (清空缓存)')
        print(' [3] 导出重复文件列表 (CSV + Markdown)')
        print(' [0] 退出')
        choice = input('请选择: ').strip()

        if choice == '0':
            break
        elif choice in ('1', '2'):
            fresh = choice == '2'
            if fresh and n_files > 0:
                if input('确认清空缓存并重新扫描? (yes/no): ').strip().lower() != 'yes':
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
            try:
                run_scan(root, db_path, fresh, visual, threshold, workers, interactive=True)
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
    if sys.platform == 'win32':
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty():
                try:
                    stream.reconfigure(encoding='utf-8', errors='replace')
                except Exception:
                    pass
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

    sub.add_parser('export', help='导出重复列表 (CSV + Markdown)')

    args = parser.parse_args()

    if args.cmd == 'scan':
        try:
            sys.exit(run_scan(args.root, args.db, args.fresh, not args.no_visual,
                              args.threshold, args.workers, interactive=False))
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
    else:
        try:
            menu(args.db)
        except KeyboardInterrupt:
            print('\n已退出。')


if __name__ == '__main__':
    main()

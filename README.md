# Duplicate Image Finder (v2)

A resilient, high-performance duplicate and visually similar image detection system built in Python, designed specifically to handle massive photo collections (100,000 to 1,000,000+ files) without crashing, thrashing disk I/O, or overloading memory.

---

## Key Features & Functions

### 1. 4-Stage Hierarchical Funnel Architecture
To avoid expensive image decoding across millions of files, the pipeline filters images progressively:
- **Stage 1 — Directory Enumeration & Cache Sync**: Recursively scans folders using `os.scandir` and indexes `(path, size, mtime)` into an SQLite database.
- **Stage 2 — Prefix Hash (64 KB BLAKE2b)**: Quickly hashes only the first 64 KB of files that share identical byte sizes. Unique sizes are immediately skipped.
- **Stage 3 — Full File Hash (BLAKE2b-128)**: Streams the entire file in 1 MB blocks only for files with prefix collisions. Identifies **bit-for-bit exact duplicates** with zero false positives.
- **Stage 4 — Perceptual Hash (pHash) & SIMD Clustering (Optional)**:
  - Generates 64-bit DCT perceptual hashes (`imagehash.phash`) on unique representative files.
  - Leverages JPEG draft downsampling (`im.draft('L', (64, 64))`) for accelerated decoding.
  - Performs multi-threaded Hamming distance clustering accelerated by NumPy 2.x hardware SIMD instructions (`np.bitwise_count` / CPU `POPCNT`).
  - Identifies **visually similar duplicates** (re-compressed, resized, format-converted, or watermarked copies).

### 2. High Resilience & Scalability
- **SQLite WAL Mode & Breakpoint Resumption**: Full persistence in `image_dedup.db` using WAL mode (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`). Interrupted scans can be resumed immediately without re-doing completed work.
- **Direct Stage Resumption (`--skip-walk`)**: Allows continuing directly to uncompleted hashing or visual clustering stages without re-walking large disks.
- **Stateless Multi-Processing**: Hashing worker processes run pure stateless functions, eliminating database locking and multi-process race conditions.
- **In-Place Dynamic Terminal UI**: Custom `SingleLineReporter` dynamically truncates progress strings based on terminal column width (including full-width East Asian character support) without spamming multi-line terminal logs.
- **Cloud Storage Aware**: Automatically detects and skips Windows OneDrive cloud-only placeholder files (`FILE_ATTRIBUTE_OFFLINE` / `RECALL_ON_OPEN`) to prevent accidental massive cloud downloads.
- **macOS Metadata Protection**: Automatically ignores `.DS_Store` and `._*` AppleDouble metadata files, preventing `UnidentifiedImageError` when scanning disks copied from macOS.
- **Gigapixel Image Support**: Disables PIL pixel decompression bomb limits (`Image.MAX_IMAGE_PIXELS = None`) to safely process ultra-high-resolution images.
- **Interactive Error Recovery**: When a corrupt or unreadable file is encountered, choose to retry (`r`), skip all remaining errors in the current session (`a`), or configure permanent auto-skipping.

### 3. Built-In Utilities
- **Temporary Isolated Scanning**: Scan folders into an isolated temporary database (`image_dedup_temp.db`) and generate `_temp` reports without affecting the main database.
- **Database Maintenance (`clean-db`)**: Optimize and defragment SQLite storage via `VACUUM` and update optimizer statistics with `ANALYZE`.
- **macOS Junk Purge (`clean-junk`)**: Search and safely remove `._*` and `.DS_Store` files from any directory with interactive confirmation and automatic database cleanup.
- **Safe Duplicate Deletion (menu `[8]`)**: Remove redundant duplicates from the scan results, keeping exactly one file per group (`largest size` → `newest date` → `largest filename`). Works on either the **main database** or a kept **temporary scan database**. Deleted files go to the **OS Recycle Bin** (recoverable), the SQLite index is synced automatically, and reclaimed disk space is reported.

---

## Requirements

- **Python**: 3.10 or later (Python 3.12+ recommended).
- **Operating System**: Windows 10/11, Linux, or macOS.

### Dependencies
- `pillow >= 10.0`
- `imagehash >= 4.3`
- `numpy >= 2.0` (required for hardware SIMD `np.bitwise_count`)
- `tqdm >= 4.60`

---

## Installation

### Option A: Windows Automated Setup (One-Click)
Double-click `install_dependencies.bat` or run it from the command prompt:
```cmd
install_dependencies.bat
```
This script checks for Python, installs all packages from `requirements.txt` (falling back to the Tsinghua PyPI mirror if needed), and verifies that all dependencies import correctly.

### Option B: Manual Installation
Install dependencies via `pip`:
```bash
pip install -r requirements.txt
```

*(Optional Performance Tip)*: For 2x–4x faster JPEG/PNG image decoding, you can install [`pillow-simd`](https://github.com/uploadcare/pillow-simd) (requires uninstalling standard `pillow` first).

---

## Usage Guide

### 1. Interactive Menu Mode (Recommended for Beginners)

Simply launch the script without arguments:
```bash
python duplicate_image_finder_v2.py
```

An interactive menu will be displayed:
```text
==================================================
        重复图片查找工具 (海量文件版)
==================================================
主缓存数据库: image_dedup.db (已索引 0 个文件)
上次扫描目录: 未设置
错误处理模式: 遇错暂停(提示输入 r 重试)
--------------------------------------------------
 [1] 开始/继续查找重复项 (断点续扫主库)
 [2] 重新扫描主库 (清空并重新扫描)
 [3] 导出主库重复列表 (CSV + Markdown)
 [4] 临时隔离扫描 (使用临时数据库, 不污染主库, 导出 _temp 报告)
 [5] 优化与清理数据库 (VACUUM 整理碎片 & 索引优化)
 [6] 切换错误处理模式 (当前: 暂停重试)
 [7] 清理 Mac 垃圾文件 (删除扫描目录中的 ._*.xxx 与 .DS_Store, 删除前需确认)
 [8] 删除重复文件 (可选主库/临时库; 每组保留 1 个: 体积最大>日期最新>文件名最大; 移入回收站)
 [0] 退出
```

- **Option [1] (Start/Resume)**: Scans a target folder or resumes an existing scan. If previous file records exist, it asks whether to re-check the disk or directly resume unfinished hash/clustering stages.
- **Option [2] (Fresh Scan)**: Clears existing database records and performs a complete re-scan from scratch.
- **Option [3] (Export Reports)**: Exports duplicate reports from existing scan data without rescanning.
- **Option [4] (Temp Scan)**: Performs an isolated scan in `image_dedup_temp.db` and exports `_temp` reports. Offers to delete the temporary database upon completion.
- **Option [5] (Optimize DB)**: Runs `VACUUM` and `ANALYZE` on the SQLite database to reclaim disk space and rebuild indexes.
- **Option [6] (Toggle Error Mode)**: Switches between interactive error prompt (`r` retry / `a` skip all) and silent auto-skip.
- **Option [7] (Clean Mac Junk)**: Scans for macOS `._*` and `.DS_Store` junk files, shows a summary, and asks for confirmation before deleting.
- **Option [8] (Delete Duplicates)**: Deletes redundant duplicates from the scan results, keeping exactly one file per group. It works on **both the main database and a temporary scan database**: if a previous temporary scan was kept (`image_dedup_temp.db`), you are asked whether to process the main DB or the temp DB. It then shows the database path and the scanned root directory; if the database is empty it prompts you to scan first (main → option `1`/`2`, temp → option `4`) and returns to the menu. You then choose which category to delete (`1` Exact / `2` Visual / `3` Both). Files are sent to the **OS Recycle Bin** (recoverable), the SQLite index is synced, and reclaimed space is reported. See the retention policy below.
  - **Retention policy (keep exactly one per group)**: ① largest file size → ② newest modification date (**date only**, time-of-day ignored) → ③ largest filename within the same date (lexicographic, case-insensitive).

---

### 2. Command-Line Interface (CLI Mode)

The tool can be fully automated using CLI commands and flags:

#### Basic Scans
```bash
# Start or resume scanning on a folder (uses main database)
python duplicate_image_finder_v2.py scan "D:\Photos"

# Wipe existing cache and start a fresh scan
python duplicate_image_finder_v2.py scan "D:\Photos" --fresh

# Skip disk re-traversal and resume directly from unfinished hashing / clustering stages
python duplicate_image_finder_v2.py scan "D:\Photos" --skip-walk
```

#### Scan Options & Performance Tuning
```bash
# Disable perceptual visual similarity (exact byte duplicates only, much faster)
python duplicate_image_finder_v2.py scan "D:\Photos" --no-visual

# Customize perceptual hash Hamming distance threshold (default: 6, lower is stricter)
# and worker process count (default: 4)
python duplicate_image_finder_v2.py scan "D:\Photos" --threshold 6 --workers 8

# Automatically skip unreadable/corrupted files without interactive prompts
python duplicate_image_finder_v2.py scan "D:\Photos" --auto-skip-errors
```

#### Isolated Temporary Scan
```bash
# Run an isolated test scan (uses image_dedup_temp.db and exports duplicates_report_temp.*)
python duplicate_image_finder_v2.py scan "D:\Photos" --temp
```

#### Utility Commands
```bash
# Export CSV and Markdown reports from the existing database
python duplicate_image_finder_v2.py export

# Optimize and defragment the SQLite database
python duplicate_image_finder_v2.py clean-db

# Clean macOS metadata junk files (._* and .DS_Store) in a target folder
python duplicate_image_finder_v2.py clean-junk "D:\Photos"
```

---

## Output Reports

Every scan export generates two reports in the workspace directory:

1. **`duplicates_report.csv`** (or `duplicates_report_temp.csv`):
   - Encoded in UTF-8 with BOM (`utf-8-sig`) for native compatibility with Microsoft Excel.
   - Organized horizontally for quick review:
     ```csv
     filename,dup_path_1,dup_path_2,dup_path_3,...
     ```
2. **`duplicates_report.md`** (or `duplicates_report_temp.md`):
   - Detailed Markdown summary with scan metadata, total indexed files, and duplicate group counts.
   - Separate sections for **Exact Match (内容重复)** and **Visual Similarity (视觉相似)** with file sizes and paths.

---

## Database Architecture (`image_dedup.db`)

All scanned file states and hash results are persisted in a local SQLite database with the following schema:

```sql
CREATE TABLE files (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    prefix_hash  TEXT,              -- BLAKE2b hex of first 64 KB
    full_hash    TEXT,              -- BLAKE2b-128 hex of entire file
    phash        TEXT,              -- 64-bit perceptual hash hex
    visual_group INTEGER,           -- Group ID for visual duplicate clusters
    error        TEXT               -- Error message if read/decode failed
);
```

---

## License

This project is licensed under the MIT License.

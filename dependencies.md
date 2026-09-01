# 依赖与部署说明

`duplicate_image_finder_v2.py` 在新机器上的部署所需环境。

## 1. 运行环境

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11、Linux、macOS | Windows 上自动检测并跳过 OneDrive 占位文件 |
| Python | **3.10 及以上**（推荐 3.12） | 依赖 `int.bit_count()`（3.10+）；本机验证版本 3.12.10 |
| 磁盘 | 数据库约每 10 万文件 100~200 MB | `image_dedup.db` 生成在脚本同目录 |

## 2. Python 依赖

| 包 | 最低版本 | 用途 | 必需 |
|---|---|---|---|
| `pillow` | 10.0 | 图片解码 | 是 |
| `imagehash` | 4.3 | pHash 感知哈希（自动带入 `numpy`、`scipy`、`PyWavelets`） | 是 |
| `tqdm` | 4.60 | 进度条 | 是 |

本机验证通过的版本组合（2026-08-31）：
`pillow 12.2.0` · `imagehash 4.3.2` · `numpy 2.4.4` · `scipy 1.18.1` · `PyWavelets 1.9.0` · `tqdm 4.67.3`

## 3. 安装

**联网机器**（仓库已附 `requirements.txt`）：

```bash
pip install -r requirements.txt
```

或手动：

```bash
pip install pillow imagehash tqdm
```

**离线机器**：先在一台联网且 Python 版本/系统相同的机器上下载离线包，再拷贝安装：

```bash
# 联网机器上（注意 --platform/--python-version 需与目标机一致，Windows 64 位示例）
mkdir wheels
pip download -r requirements.txt -d wheels

# 拷贝 wheels 目录到目标机器后
pip install --no-index --find-links wheels -r requirements.txt
```

## 4. 验证安装

```bash
python -c "import PIL, imagehash, tqdm, numpy; print('依赖 OK')"
```

输出 `依赖 OK` 即可运行：

```bash
python duplicate_image_finder_v2.py
```

## 5. 可选性能优化

- **Pillow-SIMD**：比 Pillow 快约 2~4 倍的图像解码（对 65 万文件的首次视觉相似扫描提速明显）。与 Pillow **互斥**，需先卸载再装：

  ```bash
  pip uninstall pillow -y
  pip install pillow-simd
  ```

  代码无需改动（同样以 `PIL` 导入）。若安装失败（缺编译环境），换回 `pip install pillow` 即可。

## 6. 部署清单

只需要拷贝一个代码文件：

```
duplicate_image_finder_v2.py    # 主程序（其余文件均非必需）
```

运行时会自动在脚本目录生成：

| 文件 | 说明 |
|---|---|
| `image_dedup.db` | SQLite 缓存（断点续扫依赖它，迁移机器时可一并拷贝以保留进度） |
| `duplicates_report.csv` | 导出结果（UTF-8 BOM，Excel 可直接打开） |
| `duplicates_report.md` | 导出的 Markdown 报告 |

## 7. 常见问题

- **`ModuleNotFoundError: No module named 'imagehash'`**：确认 `pip` 与 `python` 属于同一环境（可用 `python -m pip install -r requirements.txt`）。
- **扫描 OneDrive 目录**：占位（未下载）文件会被自动跳过并计数，不会触发云下载。
- **Windows 长路径（>260 字符）**：若报路径错误，启用系统长路径支持：组策略「计算机配置 → 管理模板 → 系统 → 文件系统 → 启用 Win32 长路径」，或注册表 `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1`。
- **多进程启动慢/杀毒拦截**：程序使用进程池并行哈希，杀软若拦截子进程可将扫描目录加入白名单。

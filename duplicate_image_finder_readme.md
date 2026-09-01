已完成！以下是完整的脚本下载和使用说明：

## 📥 下载脚本

**duplicate_image_finder.py**

## 🚀 快速开始

### 1. 安装依赖

bash

复制

```bash
pip install pillow imagehash tqdm
```

### 2. 基础用法

bash

复制

```bash
# 基本扫描（使用默认的 pHash 算法）
python duplicate_image_finder.py /path/to/your/photos

# 使用 dHash 算法，阈值设为 8（更严格）
python duplicate_image_finder.py /path/to/photos -a dhash -t 8

# 显示详细相似度矩阵
python duplicate_image_finder.py /path/to/photos -v

# 导出结果到文件
python duplicate_image_finder.py /path/to/photos -o report.txt
```

### 3. 删除重复图片（谨慎！）

bash

复制

```bash
# 试运行模式（只预览，不实际删除）
python duplicate_image_finder.py /path/to/photos --delete

# 实际删除：保留每组最大文件，删除其余（关闭试运行）
python duplicate_image_finder.py /path/to/photos --delete --keep largest --no-dry-run

# 保留第一张（按文件名排序），删除其余
python duplicate_image_finder.py /path/to/photos --delete --keep first --no-dry-run
```

## ⚙️ 参数说明

表格

| 参数            | 说明                                     | 建议值           |
| ------------- | -------------------------------------- | ------------- |
| `-a`          | 算法选择：`phash`稳健, `ahash`快速, `dhash`结构敏感 | 默认 `phash`    |
| `-t`          | 汉明距离阈值（越小越严格）                          | `8-12`（pHash） |
| `--hash-size` | 哈希矩阵大小（默认8=64位）                        | 增大提高精度但容错降低   |
| `--keep`      | 删除时保留策略：`first`/`largest`/`resolution` | 建议 `largest`  |
| `-v`          | 显示组内相似度矩阵                              | 调试用           |

## 💡 算法选择建议

根据演示结果，不同场景选择不同算法：

- **pHash（推荐）**：基于DCT，**最稳健**，对轻微模糊/压缩容忍度高，但会正确区分亮度调整和裁剪

- **aHash**：**速度最快**，但对颜色分布敏感，可能误判不同内容为重复（如不同场景但颜色相似）

- **dHash**：对**结构变化敏感**，能识别裁剪，但对亮度变化容忍度较低

- **wHash**：基于小波，**细节保留好**，但计算量稍大

## 🔧 在Python代码中调用

Python

复制

```python
from duplicate_image_finder import DuplicateImageFinder

finder = DuplicateImageFinder(hash_size=8, algorithm='phash', threshold=10)

# 扫描目录
hash_dict = finder.scan_directory("/path/to/photos")

# 查找重复
duplicates = finder.find_duplicates(hash_dict)

# 处理结果
for group in duplicates:
    print(f"发现 {len(group)} 张相似图片")
    for path in group:
        print(f"  - {path}")
```

这个脚本已包含完整的错误处理、进度显示、结果导出和安全删除机制，可以直接用于生产环境。

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重复图片识别脚本 - 基于感知哈希 (Perceptual Hashing)
支持 aHash, dHash, pHash, wHash 算法
作者：AI Assistant
日期：2026-03-22
"""

import os
import sys
import argparse
import warnings
from pathlib import Path
from PIL import Image
import imagehash
from collections import defaultdict
from typing import List, Tuple, Dict, Optional
from tqdm import tqdm

# 禁用大图像素上限限制及 DecompressionBomb 告警
Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter('ignore', Image.DecompressionBombWarning)


class DuplicateImageFinder:
    """
    基于感知哈希的重复图片识别器

    算法说明:
    - aHash (Average Hash): 速度快，对颜色敏感，适合快速预览去重
    - dHash (Difference Hash): 基于梯度，对结构变化敏感
    - pHash (Perceptual Hash): 基于DCT，抗干扰能力最强，推荐用于生产环境
    - wHash (Wavelet Hash): 基于小波变换，保留细节但计算量稍大
    """

    def __init__(self, hash_size: int = 8, algorithm: str = 'phash', threshold: int = 10):
        """
        初始化参数

        Args:
            hash_size: 哈希大小，默认8生成64位指纹（16位十六进制字符串）
                      增大到16可生成256位指纹，精度更高但容错率降低
            algorithm: 哈希算法 'ahash'|'dhash'|'phash'|'whash'，默认'phash'
            threshold: 汉明距离阈值，小于等于此值视为相似/重复
                      建议值: aHash(8-10), dHash(10-12), pHash(8-12), wHash(10-14)
        """
        self.hash_size = hash_size
        self.algorithm = algorithm.lower()
        self.threshold = threshold
        self.hash_func = self._get_hash_function()

    def _get_hash_function(self):
        """获取对应的哈希函数"""
        algorithms = {
            'ahash': imagehash.average_hash,
            'dhash': imagehash.dhash,
            'phash': imagehash.phash,
            'whash': imagehash.whash
        }
        if self.algorithm not in algorithms:
            raise ValueError(f"不支持的算法: {self.algorithm}，可选: {list(algorithms.keys())}")
        return algorithms[self.algorithm]

    def compute_hash(self, image_path: str) -> Optional[imagehash.ImageHash]:
        """
        计算单张图片的感知哈希

        Args:
            image_path: 图片路径

        Returns:
            ImageHash 对象，失败返回 None
        """
        try:
            with Image.open(image_path) as img:
                # 统一转换为RGB，处理RGBA/灰度图/Palette等模式
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                return self.hash_func(img, hash_size=self.hash_size)
        except Exception as e:
            print(f"⚠️  处理失败 {image_path}: {e}")
            return None

    def scan_directory(self, directory: str, 
                      extensions: Tuple[str, ...] = ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff')) -> Dict[str, imagehash.ImageHash]:
        """
        扫描目录中的所有图片并计算哈希

        Args:
            directory: 目标文件夹路径
            extensions: 支持的图片扩展名

        Returns:
            字典: {图片路径: 哈希值}
        """
        directory = Path(directory)
        if not directory.exists():
            raise FileNotFoundError(f"目录不存在: {directory}")

        # 收集所有图片文件（递归子目录）
        image_files = []
        for ext in extensions:
            image_files.extend(directory.rglob(f"*{ext}"))
            image_files.extend(directory.rglob(f"*{ext.upper()}"))

        # 去重并保持排序稳定
        image_files = sorted(set(image_files))

        if not image_files:
            print(f"未在 {directory} 中发现图片文件")
            return {}

        print(f"📁 扫描目录: {directory}")
        print(f"🖼️  发现 {len(image_files)} 个图片文件")
        print(f"⚙️  使用 {self.algorithm.upper()} 算法 (哈希大小: {self.hash_size}x{self.hash_size})")

        # 计算哈希并过滤失败项
        hash_dict = {}
        failed_count = 0

        for img_path in tqdm(image_files, desc="计算哈希", unit="img"):
            img_hash = self.compute_hash(str(img_path))
            if img_hash is not None:
                hash_dict[str(img_path)] = img_hash
            else:
                failed_count += 1

        if failed_count > 0:
            print(f"⚠️  {failed_count} 张图片处理失败")

        print(f"✅ 成功建立 {len(hash_dict)} 张图片的哈希索引")
        return hash_dict

    def find_duplicates(self, hash_dict: Dict[str, imagehash.ImageHash]) -> List[List[str]]:
        """
        查找重复/相似图片组

        使用并查集(Union-Find)算法进行聚类：
        如果A相似于B，B相似于C，则A,B,C归为同一重复组

        时间复杂度: O(N^2)，适用于万级以下数据集
        对于百万级数据，建议使用BK-Tree或局部敏感哈希(LSH)优化

        Args:
            hash_dict: 路径到哈希的映射字典

        Returns:
            重复图片组列表，每组是相似图片的路径列表（按文件名排序）
        """
        paths = list(hash_dict.keys())
        hashes = [hash_dict[p] for p in paths]
        n = len(paths)

        if n < 2:
            return []

        # 并查集初始化
        parent = list(range(n))
        rank = [0] * n

        def find(x):
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            px, py = find(x), find(y)
            if px == py:
                return
            if rank[px] < rank[py]:
                px, py = py, px
            parent[py] = px
            if rank[px] == rank[py]:
                rank[px] += 1

        # 两两比较汉明距离
        comparison_count = 0
        similar_pairs = 0

        progress_bar = tqdm(total=n*(n-1)//2, desc="相似度比对", unit="pairs")

        for i in range(n):
            for j in range(i + 1, n):
                distance = hashes[i] - hashes[j]  # imagehash重载了减法为汉明距离
                comparison_count += 1
                if distance <= self.threshold:
                    union(i, j)
                    similar_pairs += 1
                if comparison_count % 100 == 0:
                    progress_bar.update(100)

        progress_bar.close()

        print(f"🔍 完成 {comparison_count} 次对比，发现 {similar_pairs} 对相似图片")

        # 收集分组结果
        groups = defaultdict(list)
        for i, path in enumerate(paths):
            groups[find(i)].append(path)

        # 只返回包含多张图片的组（真正有重复的组）
        duplicate_groups = [sorted(group) for group in groups.values() if len(group) > 1]

        # 按组大小排序，大的在前
        duplicate_groups.sort(key=lambda x: len(x), reverse=True)

        return duplicate_groups

    def print_results(self, duplicate_groups: List[List[str]], detailed: bool = False):
        """
        打印重复识别结果

        Args:
            duplicate_groups: 重复图片组列表
            detailed: 是否显示详细的哈希距离矩阵
        """
        if not duplicate_groups:
            print("\n✅ 未发现重复或相似图片")
            return

        total_dup_images = sum(len(group) for group in duplicate_groups)
        unique_groups = len(duplicate_groups)

        print(f"\n" + "="*70)
        print(f"🔍 结果汇总: 发现 {unique_groups} 组重复，涉及 {total_dup_images} 张图片")
        print("="*70)

        for idx, group in enumerate(duplicate_groups, 1):
            print(f"\n【重复组 {idx}/{unique_groups}】共 {len(group)} 张图片")
            print("-" * 50)

            if detailed and len(group) > 1:
                # 计算组内所有两两距离（对称矩阵）
                print("相似度矩阵 (汉明距离，值越小越相似):")
                hashes = [self.compute_hash(p) for p in group]

                # 打印表头
                max_name_len = max(len(os.path.basename(p)) for p in group)
                header = " " * (max_name_len + 2)
                for i in range(len(group)):
                    header += f"#{i+1:<4}"
                print(header)

                # 打印矩阵
                for i, (path_i, h_i) in enumerate(zip(group, hashes)):
                    row = f"#{i+1} {os.path.basename(path_i):<{max_name_len}} "
                    for j, h_j in enumerate(hashes):
                        if j < i:
                            dist = h_i - h_j
                            marker = "**" if dist <= self.threshold else "  "
                            row += f"{marker}{dist:>2}{marker} "
                        elif i == j:
                            row += "  0   "
                        else:
                            row += "      "
                    print(row)
                print(f"阈值: {self.threshold}，**标注为视为重复的距离")
            else:
                # 简洁模式：显示文件大小和路径
                max_size = 0
                largest_file = None

                for path in group:
                    try:
                        size = os.path.getsize(path)
                        if size > max_size:
                            max_size = size
                            largest_file = path
                    except:
                        pass

                for path in group:
                    try:
                        size = os.path.getsize(path)
                        size_str = f"{size/1024:.1f} KB"
                        marker = "[MAX] " if path == largest_file else "      "
                    except:
                        size_str = "? KB"
                        marker = "      "

                    rel_path = os.path.relpath(path)
                    print(f"  {marker}{rel_path} ({size_str})")

            print()

    def export_results(self, duplicate_groups: List[List[str]], output_file: str = "duplicates.txt"):
        """将结果导出到文本文件"""
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"重复图片识别报告\n")
            f.write(f"算法: {self.algorithm}, 阈值: {self.threshold}, 哈希大小: {self.hash_size}\n")
            f.write(f"共发现 {len(duplicate_groups)} 组重复图片\n")
            f.write("="*70 + "\n\n")

            for idx, group in enumerate(duplicate_groups, 1):
                f.write(f"【重复组 {idx}】\n")
                for path in group:
                    f.write(f"{path}\n")
                f.write("\n")

        print(f"📄 结果已导出到: {output_file}")

    def delete_duplicates(self, duplicate_groups: List[List[str]], 
                          keep_strategy: str = 'first', 
                          dry_run: bool = True):
        """
        删除重复图片（谨慎使用！）

        Args:
            duplicate_groups: 重复图片组
            keep_strategy: 保留策略
                - 'first': 保留每组第一张（按文件名排序）
                - 'largest': 保留文件最大的（通常质量最好）
                - 'resolution': 保留分辨率最高的
            dry_run: 试运行模式（只打印不实际删除）
        """
        if not duplicate_groups:
            print("没有重复图片需要删除")
            return

        if dry_run:
            print("\n⚠️  【试运行模式】以下操作仅预览，不会实际删除文件")
            print("    如需实际删除，请添加 --no-dry-run 参数\n")
        else:
            print("\n⚠️  【警告】正在执行实际删除操作！\n")
            confirm = input("确认删除? (yes/no): ")
            if confirm.lower() != 'yes':
                print("操作已取消")
                return

        total_deleted = 0
        saved_bytes = 0

        for group in duplicate_groups:
            if len(group) <= 1:
                continue

            # 确定要保留的文件
            if keep_strategy == 'first':
                keep_path = group[0]
                to_delete = group[1:]
            elif keep_strategy == 'largest':
                # 按文件大小排序，保留最大的
                sized_files = [(p, os.path.getsize(p)) for p in group if os.path.exists(p)]
                if not sized_files:
                    continue
                sized_files.sort(key=lambda x: x[1], reverse=True)
                keep_path = sized_files[0][0]
                to_delete = [p for p, _ in sized_files[1:]]
            elif keep_strategy == 'resolution':
                # 按分辨率排序
                res_files = []
                for p in group:
                    try:
                        with Image.open(p) as img:
                            res_files.append((p, img.width * img.height))
                    except:
                        continue
                if not res_files:
                    continue
                res_files.sort(key=lambda x: x[1], reverse=True)
                keep_path = res_files[0][0]
                to_delete = [p for p, _ in res_files[1:]]
            else:
                print(f"未知保留策略: {keep_strategy}")
                continue

            print(f"保留: {os.path.basename(keep_path)}")

            for del_path in to_delete:
                try:
                    size = os.path.getsize(del_path)
                    print(f"  删除: {os.path.basename(del_path)} ({size/1024:.1f} KB)")

                    if not dry_run:
                        os.remove(del_path)

                    total_deleted += 1
                    saved_bytes += size
                except Exception as e:
                    print(f"    错误: {e}")

            print()

        action_str = "试运行" if dry_run else "实际"
        print(f"✅ {action_str}删除完成: {total_deleted} 张图片，可释放空间 {saved_bytes/1024/1024:.2f} MB")


def main():
    parser = argparse.ArgumentParser(
        description='基于感知哈希的重复图片识别工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  %(prog)s /path/to/photos
  %(prog)s /path/to/photos -a phash -t 10 -v
  %(prog)s /path/to/photos --delete --keep largest --no-dry-run

算法建议:
  - phash (默认): 最稳健，适合一般去重，阈值建议8-12
  - ahash: 速度最快，对颜色敏感，适合快速筛选，阈值建议8-10  
  - dhash: 对结构敏感，适合检测裁剪，阈值建议10-12
  - whash: 精度高，适合精细比对，阈值建议10-14
        """
    )

    parser.add_argument('directory', help='要扫描的图片目录路径')
    parser.add_argument('-a', '--algorithm', default='phash', 
                        choices=['ahash', 'dhash', 'phash', 'whash'],
                        help='感知哈希算法 (默认: phash)')
    parser.add_argument('-t', '--threshold', type=int, default=10, 
                        help='汉明距离阈值，越小越严格 (默认: 10)')
    parser.add_argument('--hash-size', type=int, default=8,
                        help='哈希矩阵大小，默认8(64位)，增大提高精度但降低容错 (默认: 8)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='显示详细的相似度矩阵')
    parser.add_argument('-o', '--output', metavar='FILE',
                        help='导出结果到文本文件')
    parser.add_argument('--delete', action='store_true',
                        help='启用删除模式（默认仅标记，不删除）')
    parser.add_argument('--keep', default='first', 
                        choices=['first', 'largest', 'resolution'],
                        help='删除时保留哪张 (默认: first)')
    parser.add_argument('--no-dry-run', dest='dry_run', action='store_false', default=True,
                        help='关闭试运行，实际执行删除（需配合 --delete）')
    parser.add_argument('--min-size', type=int, metavar='KB',
                        help='忽略小于此大小的图片（KB）')

    args = parser.parse_args()

    # 验证目录
    if not os.path.isdir(args.directory):
        print(f"错误: 目录不存在 - {args.directory}")
        sys.exit(1)

    print("="*70)
    print("🔍 重复图片识别工具 (基于感知哈希)")
    print("="*70)

    # 初始化查找器
    try:
        finder = DuplicateImageFinder(
            hash_size=args.hash_size,
            algorithm=args.algorithm,
            threshold=args.threshold
        )
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    # 扫描目录
    try:
        hash_dict = finder.scan_directory(args.directory)
    except Exception as e:
        print(f"扫描失败: {e}")
        sys.exit(1)

    if len(hash_dict) < 2:
        print("图片数量不足，无需比较")
        sys.exit(0)

    # 查找重复
    duplicates = finder.find_duplicates(hash_dict)

    # 打印结果
    finder.print_results(duplicates, detailed=args.verbose)

    # 导出结果
    if args.output:
        finder.export_results(duplicates, args.output)

    # 删除操作
    if args.delete:
        if not duplicates:
            print("没有重复图片需要删除")
        else:
            finder.delete_duplicates(duplicates, 
                                     keep_strategy=args.keep, 
                                     dry_run=args.dry_run)

    print("\n" + "="*70)
    print("处理完成")
    print("="*70)


if __name__ == "__main__":
    main()

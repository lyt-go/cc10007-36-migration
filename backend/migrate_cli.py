"""命令行分批迁移入口。

用法：
    python migrate_cli.py                 # 按配置批次大小迁移到完成
    python migrate_cli.py --max-batches 1  # 只推进一个批次（部分迁移/断点核对）
    python migrate_cli.py --batch-size 1

服务重启场景下直接重新运行即可：存在 migrating 断点时自动续迁，
已 done 时重复执行按内容哈希幂等跳过。
"""
from __future__ import annotations

import argparse
import json

from app.migration import engine


def main() -> None:
    parser = argparse.ArgumentParser(description="看板视图与指标归因内存记录分批迁移入库")
    parser.add_argument("--batch-size", type=int, default=None, help="每批读取的内存行数")
    parser.add_argument("--max-batches", type=int, default=None,
                        help="本次最多推进的批次数（默认全部完成）")
    parser.add_argument("--note", default="", help="迁移运行备注")
    args = parser.parse_args()

    status = engine.start_migration(
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        note=args.note or "CLI 分批迁移",
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

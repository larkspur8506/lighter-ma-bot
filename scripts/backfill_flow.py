#!/usr/bin/env python3
"""出入金流水补录（只写 `account_flows` 表，不碰别的）。

背景：`/transfer/history` 对 UDA 桥接入金恒空；机器人靠权益跳变推断流水，但区间内
有持仓/成交时会保守放弃 —— 那笔出入金会静默混进 cum_pnl（面板记录页会给出跳变提示）。
这时用本工具补录，下一轮 tick 的 TWR 重放就会把它剔掉。

用法（容器内，DB 路径默认取环境 DB_PATH）::

    python /srv/scripts/backfill_flow.py --amount 399.0 --ts 1793198347.122463 \\
        --note "UDA deposit 2026-10-28 14:39:07 UTC"

    python /srv/scripts/backfill_flow.py --amount 399.0 \\
        --ts "2026-10-28T14:39:07Z" --note "..."      # ISO 时间也认

    python /srv/scripts/backfill_flow.py --list       # 只看现有流水

安全：`amount` 带符号（正 = 入金，负 = 出金）；`(source='manual', ts, amount)` 唯一
索引幂等 —— 重复执行同一笔只落一行，脚本会如实报告 inserted/duplicate。
"""
from __future__ import annotations

import argparse
import calendar
import os
import sys
import time


def _parse_ts(raw: str) -> float:
    raw = str(raw).strip()
    try:
        return float(raw)                            # epoch 秒
    except ValueError:
        pass
    fmts = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M")
    for fmt in fmts:
        try:
            return calendar.timegm(time.strptime(raw, fmt))   # 按 UTC 解析
        except ValueError:
            continue
    raise SystemExit(f"认不出时间格式: {raw!r}（给 epoch 秒或 ISO/UTC）")


def main() -> int:
    ap = argparse.ArgumentParser(description="出入金流水补录（manual 源，幂等）")
    ap.add_argument("--amount", type=float, help="带符号 USDC（正=入金，负=出金）")
    ap.add_argument("--ts", help="流水时刻：epoch 秒或 ISO（按 UTC）")
    ap.add_argument("--note", default="", help="备注（落库）")
    ap.add_argument("--list", action="store_true", help="只列出现有流水")
    args = ap.parse_args()

    sys.path.insert(0, "/srv")                       # 容器内布局；本地测试自行 PYTHONPATH
    from app.store import StateStore                 # noqa: PLC0415

    db_path = os.environ.get("DB_PATH") or "/srv/data/state.db"
    store = StateStore(db_path)
    try:
        if args.list:
            for row in store.flows():
                print(f'{row["ts"]:>16.3f}  {row["amount"]:>+12.4f}  {row["source"]:<9} '
                      f'{row["note"]}')
            return 0
        if args.amount is None or args.amount == 0.0 or not args.ts:
            ap.error("--amount（非 0）与 --ts 必填；只看流水用 --list")
        inserted = store.record_flow(args.amount, ts=_parse_ts(args.ts),
                                     source="manual", note=args.note)
        verb = "已补录" if inserted else "幂等命中（库里已有，未重复插入）"
        print(f"{verb}: amount={args.amount:+.4f} ts={_parse_ts(args.ts):.3f}")
        print("下一轮 tick 的 TWR 重放会自动把它剔出盈亏；若回撤峰值已被污染，"
              "还需在面板「重置回撤峰值」重新计量。")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())

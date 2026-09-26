"""命令行入口：HTTP 服务、确定性批量日结、规则追溯、投影重建。

示例：
    python -m src.benefits.cli serve --db data/benefits.sqlite --port 8080
    python -m src.benefits.cli settle --db data/benefits.sqlite --date 2026-08-31
    python -m src.benefits.cli retro  --db data/benefits.sqlite --case CASE-XXXX
    python -m src.benefits.cli rebuild --db data/benefits.sqlite
"""

from __future__ import annotations

import argparse
import json

from .api import serve
from .service import BenefitService
from .store import Store


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benefits",
                                     description="生育待遇统一结算核心")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db", default="data/benefits.sqlite",
        help="SQLite 数据库路径（默认 data/benefits.sqlite）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", parents=[common], help="启动 HTTP API 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    p_settle = sub.add_parser(
        "settle", parents=[common], help="确定性批量日结（可安全重跑）")
    p_settle.add_argument("--date", required=True, help="跑批日 YYYY-MM-DD")

    p_retro = sub.add_parser("retro", parents=[common],
                             help="按现行规则追溯重算单个案件")
    p_retro.add_argument("--case", required=True)
    p_retro.add_argument("--actor", default="policy-admin")
    p_retro.add_argument("--reason", default="规则追溯重算")

    sub.add_parser("rebuild", parents=[common],
                   help="清空投影并从事件日志重建")

    p_explain = sub.add_parser("explain", parents=[common],
                               help="输出逐案可解释账本")
    p_explain.add_argument("--case", required=True)

    p_batch = sub.add_parser("batch-status", parents=[common],
                             help="查看日结批次状态")
    p_batch.add_argument("--date", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        serve(args.db, args.host, args.port)
        return 0

    store = Store(args.db)
    svc = BenefitService(store)

    if args.cmd == "settle":
        _print(svc.daily_close(args.date))
        return 0
    if args.cmd == "retro":
        _print(svc.retro_recompute(args.case, actor=args.actor,
                                   reason=args.reason))
        return 0
    if args.cmd == "rebuild":
        _print(store.rebuild())
        return 0
    if args.cmd == "explain":
        _print(svc.explain_case(args.case))
        return 0
    if args.cmd == "batch-status":
        _print(store.batch_status(args.date))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

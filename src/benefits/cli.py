"""命令行入口：

    python -m benefits.cli init-db   --db data.db
    python -m benefits.cli seed      --db data.db
    python -m benefits.cli serve     --db data.db --host 127.0.0.1 --port 8080
    python -m benefits.cli daily     --db data.db --as-of 2026-09-26
    python -m benefits.cli explain   --db data.db --case C...
"""

from __future__ import annotations

import argparse
import json
import sys

from .app import BenefitsApp
from .batch import DailySettlement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benefits", description="生育待遇统一结算核心")
    parser.add_argument("--db", default="benefits.db", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="初始化数据库")
    sub.add_parser("seed", help="写入演示种子数据（含 init-db）")

    p_serve = sub.add_parser("serve", help="启动 HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    p_daily = sub.add_parser("daily", help="执行确定性批量日结")
    p_daily.add_argument("--as-of", required=True)
    p_daily.add_argument("--actor", default="batch")

    p_explain = sub.add_parser("explain", help="输出逐案可解释账本")
    p_explain.add_argument("--case", required=True)

    args = parser.parse_args(argv)
    app = BenefitsApp.open(args.db)

    try:
        if args.cmd == "init-db":
            print(json.dumps({"ok": True, "db": args.db}, ensure_ascii=False))
        elif args.cmd == "seed":
            from .seed import seed

            seed(app)
            print(json.dumps({"ok": True, "seeded": True}, ensure_ascii=False))
        elif args.cmd == "serve":
            from .api import serve

            server = serve(app, args.host, args.port)
            print(f"HTTP API 监听 http://{args.host}:{args.port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        elif args.cmd == "daily":
            report = DailySettlement(app.db, app.clock).run(args.as_of, actor=args.actor)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.cmd == "explain":
            print(json.dumps(app.explain(args.case), ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

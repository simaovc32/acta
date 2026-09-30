"""`acta` command line: one entry point for the things you run by hand or from cron.

    acta demo              build a demo person in ./data (see acta.demo.seed)
    acta serve             run the API + dashboard
    acta ingest            pull new strap data from the Gadgetbridge export
    acta backup            snapshot acta.db into ./data/backups
    acta report [--month]  write the monthly Markdown report
    acta prices            refresh investment prices
"""

import argparse
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="acta", description="Acta: a personal health dashboard.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="build a synthetic demo in ./data", add_help=False)
    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")
    ingest = sub.add_parser("ingest", help="ingest the Gadgetbridge export")
    ingest.add_argument("--quiet", action="store_true")
    sub.add_parser("backup", help="back up acta.db")
    sub.add_parser("report", help="write the monthly report", add_help=False)
    sub.add_parser("prices", help="refresh investment prices", add_help=False)

    if argv and argv[0] in ("demo", "report", "prices"):
        cmd, rest = argv[0], argv[1:]
    else:
        args = ap.parse_args(argv)
        cmd, rest = args.cmd, []

    if cmd == "demo":
        from acta.demo import seed
        seed.main(rest)
    elif cmd == "serve":
        import uvicorn

        from acta import config
        uvicorn.run("acta.api.app:app", host=args.host or config.HOST, port=args.port or config.PORT,
                    reload=args.reload)
    elif cmd == "ingest":
        from acta.pipeline import ingest as pipeline
        if not args.quiet:
            print("=== Acta ingest ===")
        pipeline.run_ingest(verbose=not args.quiet)
    elif cmd == "backup":
        from acta.jobs import backup
        sys.exit(backup.main())
    elif cmd == "report":
        from acta.insights import monthly_report
        sys.argv = ["acta report", *rest]
        monthly_report.main()
    elif cmd == "prices":
        from acta.finance import prices
        sys.argv = ["acta prices", *rest]
        sys.exit(prices.main())


if __name__ == "__main__":
    main()

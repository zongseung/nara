"""Independent diagnostic collection: no operational snapshot publication."""

import argparse
import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.catalog_client import (
    FixtureSource,
    NetworkSource,
    diagnostic_profile,
    require_ready,
)
from core.catalog_partitions import Partition, PriceDomain
from src.catalog_planner import CollectionPlan, make_plan
from src.catalog_provider import SQLiteProvider
from core.catalog_sources import SearchQuery, SourceError
from core.catalog_json import json_text
from src.catalog_store import CatalogStore
from src.catalog_verify import reconcile
from src.crawl_all import collect


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("inspect", "crawl", "resume", "reconcile", "query", "export"):
        sub = commands.add_parser(command)
        sub.add_argument("--db", default="catalog_v2.db")
        sub.add_argument("--scope", default="shopping-all")
        sub.add_argument("--run", default="last-reconciled")
        if command in ("inspect", "crawl", "resume"):
            sub.add_argument(
                "--fixture",
                type=Path,
                help="explicit offline rows fixture; never live evidence",
            )
            sub.add_argument("--network", action="store_true")
            sub.add_argument("--max-pages", type=int)
            sub.add_argument("--workers", type=int, default=2)
            sub.add_argument("--pending", type=int, default=4)
            sub.add_argument("--rate", type=float, default=1)
            sub.add_argument("--price-upper")
            sub.add_argument("--price-quantum", default="1")
        if command == "crawl":
            sub.add_argument("--resume")
        if command in ("query", "export"):
            sub.add_argument("--allow-incomplete", action="store_true")
        if command == "query":
            sub.add_argument("--keyword", default="")
            sub.add_argument("--limit", type=int, default=20)
            sub.add_argument("--json", action="store_true")
        if command == "export":
            sub.add_argument("--out", required=True)
    return root


def status_code(validation: str) -> int:
    return {"reconciled_observation": 0, "drift_detected": 3}.get(validation, 2)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run = None
    try:
        if args.command == "query":
            result = SQLiteProvider(
                args.db, args.run, args.scope, args.allow_incomplete
            ).search(args.keyword, limit=args.limit)
            print(json_text(asdict(result)))
            return 2 if result.metadata.incomplete else 0
        if args.command == "export":
            from src.export_parquet import export_catalog

            provider = SQLiteProvider(
                args.db, args.run, args.scope, args.allow_incomplete
            )
            count = export_catalog(
                args.db,
                args.out,
                scope=args.scope,
                run=provider.metadata.run_id,
                allow_incomplete=args.allow_incomplete,
            )
            print(
                json_text(
                    {
                        "rows": count,
                        "out": args.out,
                        "metadata": asdict(provider.metadata),
                    }
                )
            )
            return 2 if provider.metadata.incomplete else 0
        if args.command == "reconcile":
            with CatalogStore(args.db) as store:
                result = reconcile(store, args.run)
            print(json_text(asdict(result)))
            return status_code(result.validation)
        if args.max_pages is not None and args.max_pages < 1:
            raise SourceError("invalid_limit", "max_pages")
        profile = diagnostic_profile(args.scope)
        require_ready(profile)
        if args.command == "inspect" and not args.network and args.fixture is None:
            print(
                json_text(
                    {
                        "scope": profile.scope,
                        "profile": asdict(profile),
                        "profile_hash": profile.profile_hash,
                        "network_observed": False,
                        "source_snapshot_verified": False,
                    }
                )
            )
            return 0
        source = (
            FixtureSource(args.fixture, profile)
            if args.fixture
            else NetworkSource(profile, args.rate)
        )
        if args.command == "inspect":
            page = source.fetch(SearchQuery())
            print(
                json_text(
                    {
                        "scope": profile.scope,
                        "profile_hash": source.profile.profile_hash,
                        "observed_at": page.observed_at.isoformat(),
                        "total_size": page.total_size,
                        "row_total": page.row_total,
                        "received": len(page.rows),
                        "source_snapshot_verified": False,
                        "fixture": args.fixture is not None,
                    }
                )
            )
            return 0
        with CatalogStore(args.db) as store:
            run = args.run if args.command == "resume" else args.resume
            if run:
                record = store.run(run)
                if args.max_pages is not None and args.max_pages != record["max_pages"]:
                    raise SourceError("incompatible_resume", "max_pages")
                if args.price_upper is not None:
                    raise SourceError("incompatible_resume", "new_price_domain")
            else:
                first = None
                if args.max_pages is not None:
                    # This first response is both the plan observation and first persisted page.
                    first = source.fetch(SearchQuery())
                    count = (
                        first.row_total
                        if first.row_total is not None
                        else first.total_size
                    )
                    plan = CollectionPlan(
                        (Partition(None, None, count),), count, first.total_size
                    )
                else:
                    domain = (
                        None
                        if args.price_upper is None
                        else PriceDomain(
                            Decimal(0),
                            Decimal(args.price_upper),
                            Decimal(args.price_quantum),
                        )
                    )
                    plan = make_plan(source, domain)
                run = store.create_run(source.profile, plan, args.max_pages)
                if first is not None:
                    store.save_page(run, 0, 1, first)
            collect(store, source, run, workers=args.workers, pending=args.pending)
            result = reconcile(store, run)
        print(json_text(asdict(result)))
        return status_code(result.validation)
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "run_id": run}))
        return 2
    except (SourceError, OSError, sqlite3.Error, InvalidOperation) as exc:
        code = exc.code if isinstance(exc, SourceError) else "failure"
        print(
            json.dumps(
                {"error": code, "detail": str(exc), "run_id": run}, ensure_ascii=False
            )
        )
        if code in (
            "requires_credentials",
            "requires_contract_validation",
            "unsupported_scope",
            "unvalidated_scope",
        ):
            return 4
        if code in ("incomplete_run", "no_reconciled_run", "coverage_unresolved"):
            return 2
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

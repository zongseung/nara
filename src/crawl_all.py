"""Diagnostic collection entry point and retained legacy partition-test helpers."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from src import g2b_client as g2b
from src.catalog_client import Source
from src.catalog_store import CatalogStore

SAFE = 9000  # 윈도우당 상한(≈90페이지). 넘으면 반으로 분할.


def count(session, lo, hi):
    """가격구간 [lo, hi]의 총건수."""
    _, tot = g2b.fetch_page(1, page_size=1, start_prce=lo, end_prce=hi, session=session)
    return tot


def plan_windows(session, lo, hi, out):
    """[lo,hi]를 건수 SAFE 이하 리프 구간들로 적응 분할해 out에 (lo,hi,tot)로 append."""
    tot = count(session, lo, hi)
    if tot == 0:
        return
    if tot <= SAFE or lo >= hi:
        out.append((lo, hi, tot))
        return
    mid = (lo + hi) // 2
    plan_windows(session, lo, mid, out)
    plan_windows(session, mid + 1, hi, out)


def collect(
    store: CatalogStore, source: Source, run: str, *, workers: int = 2, pending: int = 4
) -> None:
    """Collect a fixed plan with bounded futures and one transactional writer."""
    import shutil
    from concurrent.futures import FIRST_COMPLETED, wait
    from pathlib import Path

    from core.catalog_sources import SearchQuery, SourceError

    if not 1 <= workers <= pending <= 4:
        raise SourceError("invalid_concurrency", "1 <= workers <= pending <= 4")
    record, plan = store.run(run), store.plan(run)
    if (
        source.profile.profile_hash != record["profile_hash"]
        or plan.version != "diagnostic-plan/1"
    ):
        raise SourceError("incompatible_resume", "profile_or_plan")
    stored = [
        tuple(row)
        for row in store.db.execute(
            "SELECT lower_price,upper_price,expected FROM partitions WHERE run_id=? ORDER BY partition_id",
            (run,),
        )
    ]
    expected = [
        (
            None if p.lower is None else str(p.lower),
            None if p.upper is None else str(p.upper),
            p.count,
        )
        for p in plan.partitions
    ]
    if stored != expected:
        raise SourceError("incompatible_resume", "partition_plan")
    if record["validation"] == "reconciled_observation":
        return
    complete = {
        tuple(row)
        for row in store.db.execute(
            "SELECT partition_id,page_no FROM pages WHERE run_id=?", (run,)
        )
    }
    cap = record["max_pages"]
    remaining = None if cap is None else max(0, cap - len(complete))

    def jobs():
        emitted = 0
        for i, part in enumerate(plan.partitions):
            for page in range(
                1,
                max(
                    1,
                    (part.count + source.profile.page_cap - 1)
                    // source.profile.page_cap,
                )
                + 1,
            ):
                if (i, page) in complete:
                    continue
                if remaining is not None and emitted >= remaining:
                    return
                emitted += 1
                yield (
                    i,
                    page,
                    SearchQuery(
                        page,
                        source.profile.page_cap,
                        start_price="" if part.lower is None else str(part.lower),
                        end_price="" if part.upper is None else str(part.upper),
                    ),
                )

    iterator, active = iter(jobs()), {}
    store.set_state(run, "running")
    try:
        estimated_rows = (
            plan.expected
            if cap is None
            else min(plan.expected, cap * source.profile.page_cap)
        )
        required = max(1024**3, estimated_rows * 8192)
        if shutil.disk_usage(Path(store.path).resolve().parent).free < required:
            raise SourceError("insufficient_disk", str(required))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            try:
                exhausted = False
                while active or not exhausted:
                    while not exhausted and len(active) < pending:
                        job = next(iterator, None)
                        if job is None:
                            exhausted = True
                        else:
                            index, number, query = job
                            active[executor.submit(source.fetch, query)] = (
                                index,
                                number,
                            )
                    if not active:
                        break
                    finished, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in finished:
                        index, number = active.pop(future)
                        store.save_page(run, index, number, future.result())
            finally:
                for future in active:
                    future.cancel()
        if cap is not None:
            store.set_state(run, "limited")
            return
        end = source.fetch(SearchQuery(page_size=1))
        with store.db:
            store.db.execute(
                "UPDATE runs SET end_count=?,end_total_size=? WHERE run_id=?",
                (
                    end.row_total if end.row_total is not None else end.total_size,
                    end.total_size,
                    run,
                ),
            )
        store.set_state(run, "collected")
    except KeyboardInterrupt:
        store.set_state(run, "interrupted", "keyboard_interrupt")
        raise
    except (SourceError, OSError, sqlite3.Error) as exc:
        store.set_state(run, "failed", str(exc))
        raise


def main() -> None:
    """Retain the script entry point while executing the new diagnostic CLI."""
    import runpy
    import sys

    sys.argv.insert(1, "crawl")
    runpy.run_module("src.catalog_cli", run_name="__main__")


if __name__ == "__main__":
    main()

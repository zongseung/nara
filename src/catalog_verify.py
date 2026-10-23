"""Reconcile an observation window; never claim a server snapshot."""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from core.catalog_json import json_text
from src.catalog_store import CatalogStore


@dataclass(frozen=True, slots=True)
class Reconciliation:
    run_id: str
    scope: str
    profile_hash: str
    observed_from: str
    observed_to: str | None
    state: str
    validation: str
    expected: int
    received: int
    inserted: int
    unique_listings: int
    unique_goods: int
    quarantined: int
    start_count: int
    end_count: int | None
    start_total_size: int
    end_total_size: int | None
    unresolved: tuple[str, ...]
    source_snapshot_verified: bool = False


def reconcile(store: CatalogStore, run: str) -> Reconciliation:
    record, plan, profile = store.run(run), store.plan(run), store.profile(run)
    db = store.db
    reasons = list(plan.unresolved)
    drift = "planning_count_drift" in reasons
    if record["state"] != "collected" or record["max_pages"] is not None:
        reasons.append("collection_not_complete")
    if record["end_count"] is None:
        reasons.append("missing_end_count")
    elif (
        record["end_count"] != record["expected"]
        or record["end_total_size"] != record["total_size"]
    ):
        reasons.append("start_end_count_drift")
        drift = True
    for index, part in enumerate(plan.partitions):
        pages = db.execute(
            "SELECT * FROM pages WHERE run_id=? AND partition_id=? ORDER BY page_no",
            (run, index),
        ).fetchall()
        needed = max(1, (part.count + profile.page_cap - 1) // profile.page_cap)
        if [p["page_no"] for p in pages] != list(range(1, needed + 1)):
            reasons.append(f"partition_{index}:missing_pages")
        if sum(p["received"] for p in pages) != part.count:
            reasons.append(f"partition_{index}:row_count")
        for page in pages:
            expected = min(
                profile.page_cap,
                max(0, part.count - (page["page_no"] - 1) * profile.page_cap),
            )
            if page["received"] != expected:
                reasons.append(f"partition_{index}:short_or_extra_page")
            count = (
                page["row_total"]
                if page["row_total"] is not None
                else page["total_size"]
            )
            if count != part.count:
                drift = True
                reasons.append(f"partition_{index}:count_drift")
        if len({p["total_size"] for p in pages}) > 1:
            drift = True
            reasons.append(f"partition_{index}:total_size_drift")
        if part.lower is not None:
            for row in db.execute(
                "SELECT raw_json FROM observations WHERE run_id=? AND partition_id=?",
                (run, index),
            ):
                price = json.loads(row[0], parse_float=Decimal).get("ctrtUprc")
                if price is None or not part.contains(
                    Decimal(str(price)), plan.quantum
                ):
                    reasons.append(f"partition_{index}:price_domain")
                    break
    repeated = db.execute(
        "SELECT fingerprint FROM pages WHERE run_id=? AND received>0 GROUP BY fingerprint HAVING count(*)>1",
        (run,),
    ).fetchone()
    if repeated:
        reasons.append("repeated_page_fingerprint")
    received = db.execute(
        "SELECT count(*) FROM observations WHERE run_id=?", (run,)
    ).fetchone()[0]
    inserted = db.execute(
        "SELECT count(*) FROM run_entities WHERE run_id=?", (run,)
    ).fetchone()[0]
    goods = db.execute(
        "SELECT count(DISTINCT goods_id) FROM run_entities WHERE run_id=? AND goods_id IS NOT NULL AND goods_id!=''",
        (run,),
    ).fetchone()[0]
    quarantine = db.execute(
        "SELECT count(*) FROM quarantine q JOIN observations o USING(observation_id) WHERE o.run_id=?",
        (run,),
    ).fetchone()[0]
    if inserted != plan.expected or received != inserted:
        reasons.append("distinct_entity_count")
    if quarantine:
        reasons.append("quarantine")
    validation = (
        "drift_detected"
        if drift
        else "incomplete"
        if reasons
        else "reconciled_observation"
    )
    result = Reconciliation(
        run,
        profile.scope,
        profile.profile_hash,
        record["started_at"],
        record["ended_at"],
        record["state"],
        validation,
        plan.expected,
        received,
        inserted,
        inserted if profile.entity_kind == "listing" else 0,
        goods,
        quarantine,
        plan.expected,
        record["end_count"],
        plan.total_size,
        record["end_total_size"],
        tuple(sorted(set(reasons))),
    )
    with db:
        db.execute(
            "UPDATE runs SET validation=?,report_json=? WHERE run_id=?",
            (validation, json_text(asdict(result)), run),
        )
        if validation == "reconciled_observation":
            db.execute(
                "INSERT INTO published_runs VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET run_id=excluded.run_id",
                (profile.scope, run),
            )
    return result

"""Bounded full-scope mandatory checks before the separate detail budget."""

import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from core.matching.constraints import aggregate_status, evaluate_constraints
from core.matching.contracts import (
    Candidate,
    ConstraintStatus,
    ContractError,
    InputItem,
    Purpose,
    RetrievalResult,
)
from src.matching.index import SearchIndex, decimal_key
from core.matching.policy import Policy


@dataclass(frozen=True, slots=True)
class RetrievalBudget:
    max_scan: int = 50_000
    max_seconds: float = 20.0
    max_detail: int = 500

    def __post_init__(self) -> None:
        if (
            type(self.max_scan) is not int
            or type(self.max_detail) is not int
            or self.max_scan < 1
            or self.max_detail < 1
        ):
            raise ContractError("retrieval_budget", "positive integer limits required")
        if not math.isfinite(self.max_seconds) or self.max_seconds <= 0:
            raise ContractError("retrieval_budget", "positive finite seconds required")


DEFAULT_BUDGET: Final = RetrievalBudget()


def _scope(item: InputItem) -> tuple[str, tuple[str, ...]]:
    clauses = ["(category_id = ? OR category_id IS NULL)"]
    parameters = [item.category_id]
    if (
        item.purpose is Purpose.EXACT_IDENTITY
        and item.goods_id
        and item.goods_id_verified
    ):
        clauses.append("(goods_id = ? OR goods_id IS NULL OR goods_verified = 0)")
        parameters.append(item.goods_id)
    if item.purpose is Purpose.SAME_PRICE_COMPETITOR:
        price = item.price
        for column, value in (
            ("price", decimal_key(price.amount)),
            ("price_kind", price.kind),
            ("currency", price.currency),
            ("tax_basis", price.tax_basis),
            ("unit", price.unit),
            ("pack_quantity", decimal_key(price.pack_quantity)),
        ):
            if value is not None:
                clauses.append(f"({column} = ? OR {column} IS NULL)")
                parameters.append(value)
        if item.seller_id:
            clauses.append("(seller_id <> ? OR seller_id IS NULL)")
            parameters.append(item.seller_id)
    for seller in item.excluded_seller_ids:
        clauses.append("(seller_id <> ? OR seller_id IS NULL)")
        parameters.append(seller)
    return " AND ".join(clauses), tuple(parameters)


def retrieve(
    item: InputItem,
    policy: Policy,
    index: SearchIndex,
    *,
    now: datetime,
    budget: RetrievalBudget = DEFAULT_BUDGET,
    clock: Callable[[], float] = time.monotonic,
) -> RetrievalResult:
    """Return only non-FAIL candidates, while explicitly accounting for UNKNOWN.

    Completion refers to the declared structured scope, never the entire market.
    Detail completion means every surviving candidate fits the returned bounded
    detail set; the engine must still execute ranking/decision evaluation.
    """
    started = clock()
    where, parameters = _scope(item)
    scope = json.dumps(
        {
            "source": "published_snapshot",
            "query": "structured_scope",
            "predicate": where,
            "parameters": parameters,
            "unknown_fields_retained": True,
            "price_fallback": False,
        },
        sort_keys=True,
    )
    if (
        not item.category_id
        or item.purpose is not policy.purpose
        or item.category_id not in policy.category_ids
    ):
        return RetrievalResult(
            (),
            scope,
            None,
            0,
            0,
            False,
            False,
            True,
            "unsupported_category",
            index.manifest.snapshot_id,
            index.manifest.generation,
        )
    total = None
    retained: list[Candidate] = []
    scanned = unknown = eligible = 0
    reason = "complete"
    sql_timed_out = False

    def progress() -> int:
        nonlocal sql_timed_out
        sql_timed_out = clock() - started >= budget.max_seconds
        return int(sql_timed_out)

    index.connection.set_progress_handler(progress, 1000)
    try:
        total = index.count(where, parameters)
        for candidate in index.candidates(where, parameters):
            if scanned >= budget.max_scan:
                reason = "scan_budget"
                break
            if clock() - started >= budget.max_seconds:
                reason = "time_budget"
                break
            status = aggregate_status(
                evaluate_constraints(item, candidate, policy, now=now)
            )
            scanned += 1
            if status is ConstraintStatus.UNKNOWN:
                unknown += 1
            if status is not ConstraintStatus.FAIL:
                eligible += 1
                if len(retained) < budget.max_detail:
                    retained.append(candidate)
    except sqlite3.OperationalError:
        if not sql_timed_out:
            raise
        reason = "time_budget"
    finally:
        index.connection.set_progress_handler(None, 0)
    scan_complete = scanned == total
    detail_complete = scan_complete and eligible <= budget.max_detail
    if scan_complete and not detail_complete:
        reason = "evaluation_budget"
    return RetrievalResult(
        tuple(retained),
        scope,
        total,
        scanned,
        unknown,
        scan_complete,
        detail_complete,
        not (scan_complete and detail_complete),
        reason,
        index.manifest.snapshot_id,
        index.manifest.generation,
    )

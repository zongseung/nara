# /// script
# requires-python = ">=3.12"
# ///
# Run: .venv/bin/python -m test.ml_surface_qa --out .omo/evidence/ml-certification/manual
"""Real local producer/reader/index/CPU-model/ledger flow; synthetic facts are explicitly marked."""

import argparse
import json
from datetime import timedelta
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.datasets import freeze_split
from src.matching.engine import evaluate_candidate, run_match
from src.matching.evaluation import EvaluationCase, evaluate_final
from src.matching.models import load_model, save_model
from src.matching.releases import DecisionConfig, certify
from src.matching.reviews import Registry, Review
from src.matching.store import MatchingStore
from src.matching.training import train_models
from test.test_matching_datasets import label
from test.test_matching_engine import NOW, OPERATOR, pipeline
from test.test_matching_models import examples
from test.updater_fixtures import row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    out = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=False)
    rows = examples()
    split = freeze_split(tuple(e.label for e in rows))
    split.write(out / "training-split.json")
    comparison = train_models(rows, split)
    artifacts = []
    for model in comparison.models:
        directory = save_model(model, out / "models")
        restored = load_model(out / "models", directory.name)
        assert restored.probability(rows[-1].pair) == model.probability(rows[-1].pair)
        artifacts.append(
            {
                "algorithm": model.artifact.algorithm,
                "hash": model.artifact_hash,
                "path": str(directory),
            }
        )
    model = load_model(out / "models", comparison.champion.artifact_hash)
    with MatchingStore(out / "matching.sqlite") as ledger:
        with pipeline(out / "actual-source", enriched=False) as (
            item,
            policy,
            release,
            index,
        ):
            actual = run_match(item, policy, index, release=release, now=NOW)
            ledger.record_job(item, actual, release)
            assert not actual.decision.candidates and actual.retrieval.unknown_count > 0
        empty = Registry()
        with pipeline(out / "annotated-fixture") as (item, policy, release, index):
            release = release.model_copy(
                update={
                    "config": DecisionConfig(reuse="certified_registry"),
                    "model_hash": model.artifact_hash,
                    "threshold": model.artifact.threshold,
                    "registry_hash": empty.content_hash,
                }
            )
            first = run_match(
                item,
                policy,
                index,
                release=release,
                now=NOW,
                scorer=model,
                registry=empty,
            )
            job = ledger.record_job(item, first, release)
            candidate_id, evidence = ledger.connection.execute(
                "SELECT candidate_id,evidence_hash FROM candidate_checks WHERE job_id=?",
                (job,),
            ).fetchone()
            review = Review(
                review_id="manual-review",
                job_id=job,
                candidate_id=candidate_id,
                evidence_hash=evidence,
                target=True,
                reason="synthetic software QA approval",
                reviewed_at=NOW,
                expires_at=NOW + timedelta(hours=24),
            )
            approval = ledger.import_review(review, OPERATOR, now=NOW)
            assert ledger.import_review(review, OPERATOR, now=NOW) == approval
            assert release.registry_hash == empty.content_hash
            assert approval is not None
            registry = Registry(approvals=(approval,))
            registry.write(out / "candidate-registry.json")
            first_price = first.candidate_checks[0].candidate.price.amount
            old_snapshot = index.catalog.manifest.snapshot_path
        with pipeline(out / "annotated-fixture", [row("A", "101", spec="20x30mm")]) as (
            item,
            policy,
            _current,
            index,
        ):
            candidate_release = release.model_copy(
                update={"registry_hash": registry.content_hash}
            )
            rerun = run_match(
                item,
                policy,
                index,
                release=candidate_release,
                now=NOW,
                scorer=model,
                registry=registry,
            )
            new_job = ledger.record_job(item, rerun, candidate_release, supersedes=job)
            assert rerun.candidate_checks[0].candidate.price.amount != first_price
            assert rerun.candidate_checks[0].route == "reuse"
            reviewed = label(
                100,
                request_id=item.input_row_id,
                candidate_id=candidate_id,
                purpose=item.purpose,
                category_id=item.category_id,
            )
            test_split = freeze_split((reviewed,))
            report = evaluate_final(
                (EvaluationCase(item, (reviewed,)),),
                candidate_release,
                lambda request: evaluate_candidate(
                    request,
                    policy,
                    index,
                    release=candidate_release,
                    now=NOW,
                    operator=OPERATOR,
                    scorer=model,
                    registry=registry,
                ),
                split=test_split,
                store=ledger,
                operator=OPERATOR,
                registry=registry,
            )
            report.write(out / "evaluation.json")
            try:
                certify(candidate_release, report)
            except ContractError:
                certification_blocked = True
            else:
                certification_blocked = False
            assert certification_blocked
            current_price = str(rerun.candidate_checks[0].candidate.price.amount)
        (out / "annotated-fixture/source" / old_snapshot).unlink()
        copied = ledger.connection.execute(
            "SELECT payload FROM candidate_checks WHERE job_id=?", (job,)
        ).fetchone()[0]
        assert "100" in copied
        ledger.backup(out / "matching-backup.sqlite")
        counts = {
            table: ledger.connection.execute(
                "SELECT count(*) FROM " + table
            ).fetchone()[0]
            for table in (
                "jobs",
                "decisions",
                "candidate_checks",
                "review_events",
                "evaluations",
            )
        }
    summary = {
        "models": artifacts,
        "champion": model.artifact.algorithm,
        "actual_source_status": actual.decision.status.value,
        "actual_source_unknown": actual.retrieval.unknown_count,
        "synthetic_annotations": "item_kind/latest_contract/seller_id only; fixture software QA, not business source facts",
        "first_price": str(first_price),
        "current_price": current_price,
        "review_import_idempotent": True,
        "frozen_registry_hash": registry.content_hash,
        "pending_import_did_not_change_active_registry": True,
        "new_candidate_registry_reuse_route": rerun.candidate_checks[0].route,
        "source_retention_copied_evidence": True,
        "first_job": job,
        "next_job": new_job,
        "counts": counts,
        "production_certificate_blocked": certification_blocked,
        "business_quality": "UNMEASURED: real independent labels unavailable",
        "cleanup": "all handles closed; old owned fixture snapshot removed; remaining artifacts retained for verification",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

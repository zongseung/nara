import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.matching.contracts import ContractError
from src.matching.engine import evaluate_candidate, run_match
from src.matching.store import MatchingStore
from test.test_matching_engine import NOW, OPERATOR, pipeline


class StoreTests(unittest.TestCase):
    def test_job_idempotence_and_copied_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with MatchingStore(root / "matching.sqlite") as ledger:
                with pipeline(root) as (item, policy, release, index):
                    result = run_match(item, policy, index, release=release, now=NOW)
                    first = ledger.record_job(item, result, release)
                    second = ledger.record_job(item, result, release)
                self.assertEqual(first, second)
                self.assertEqual(
                    ledger.connection.execute(
                        "SELECT count(*) FROM decisions"
                    ).fetchone()[0],
                    1,
                )
                self.assertIn(
                    "20x30mm",
                    ledger.connection.execute(
                        "SELECT payload FROM candidate_checks"
                    ).fetchone()[0],
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.connection.execute("DELETE FROM decisions")
                ledger.connection.rollback()
                ledger.backup(root / "backup.sqlite")
                self.assertGreater((root / "backup.sqlite").stat().st_size, 0)

    def test_evaluation_cannot_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as ledger,
                pipeline(root) as (item, policy, release, index),
            ):
                result = evaluate_candidate(
                    item, policy, index, release=release, now=NOW, operator=OPERATOR
                )
                with self.assertRaises(ContractError):
                    ledger.record_job(item, result, release)
                self.assertEqual(
                    ledger.connection.execute("SELECT count(*) FROM jobs").fetchone()[
                        0
                    ],
                    0,
                )

    def test_transaction_interrupt_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                MatchingStore(root / "matching.sqlite") as ledger,
                pipeline(root) as (item, policy, release, index),
            ):
                result = run_match(item, policy, index, release=release, now=NOW)
                ledger.connection.execute(
                    "CREATE TRIGGER interrupt BEFORE INSERT ON decisions BEGIN SELECT RAISE(ABORT,'interrupt'); END"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.record_job(item, result, release)
                self.assertEqual(
                    ledger.connection.execute("SELECT count(*) FROM jobs").fetchone()[
                        0
                    ],
                    0,
                )
                ledger.connection.execute("DROP TRIGGER interrupt")
                ledger.record_job(item, result, release)
                self.assertEqual(
                    ledger.connection.execute(
                        "SELECT count(*) FROM decisions"
                    ).fetchone()[0],
                    1,
                )

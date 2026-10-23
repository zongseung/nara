import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test.test_catalog_planner import FIXTURE


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"

    def cli(self, *args):
        env = dict(os.environ)
        env.pop("DATA_GO_KR_SERVICE_KEY", None)
        return subprocess.run(
            [sys.executable, "catalog_cli.py", *map(str, args)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_subcommands_complete_fixture_and_consume_publication(self):
        # Given the real CLI and explicit offline fixture.
        result = self.cli("crawl", "--db", self.db, "--fixture", FIXTURE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = json.loads(result.stdout)["run_id"]
        # When each read/reconcile/export/resume surface consumes the fixed run.
        for command in [
            ("inspect",),
            ("inspect", "--fixture", FIXTURE),
            ("reconcile", "--db", self.db, "--run", run),
            ("query", "--db", self.db, "--keyword", "ab 안내판", "--json"),
            ("export", "--db", self.db, "--out", self.db.with_suffix(".parquet")),
            ("resume", "--db", self.db, "--run", run, "--fixture", FIXTURE),
        ]:
            with self.subTest(command=command):
                result = self.cli(*command)
                # Then structured output and successful exit are observable.
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsInstance(json.loads(result.stdout), dict)

    def test_capped_run_requires_opt_in_for_reads_and_exports(self):
        # Given an intentionally limited run.
        result = self.cli(
            "crawl", "--db", self.db, "--fixture", FIXTURE, "--max-pages", 2
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        run = json.loads(result.stdout)["run_id"]
        # When reading/exporting with and without opt-in; then no implicit success.
        for command, extra in [
            ("query", []),
            ("export", ["--out", str(self.db.with_suffix(".parquet"))]),
        ]:
            denied = self.cli(command, "--db", self.db, "--run", run, *extra)
            self.assertEqual(json.loads(denied.stdout)["error"], "incomplete_run")
            allowed = self.cli(
                command, "--db", self.db, "--run", run, "--allow-incomplete", *extra
            )
            self.assertEqual(allowed.returncode, 2, allowed.stdout + allowed.stderr)
        resumed = self.cli(
            "crawl", "--db", self.db, "--resume", run, "--fixture", FIXTURE
        )
        self.assertEqual(json.loads(resumed.stdout)["received"], 200)
        self.assertEqual(resumed.returncode, 2)

    def test_source_gates_return_four(self):
        # Given unready scopes; when inspected; then the CLI explains the gate.
        for scope, code in [
            ("catalog-goods", "requires_credentials"),
            ("digital-main", "requires_contract_validation"),
            ("venture", "unsupported_scope"),
        ]:
            result = self.cli("inspect", "--scope", scope)
            self.assertEqual(result.returncode, 4)
            self.assertEqual(json.loads(result.stdout)["error"], code)

    def test_drift_exit_is_three(self):
        # Given a collected run altered to represent changed end totals.
        result = self.cli("crawl", "--db", self.db, "--fixture", FIXTURE)
        run = json.loads(result.stdout)["run_id"]
        import sqlite3

        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET end_count=206 WHERE run_id=?", (run,))
        # When reconciled; then exit three and explicit drift.
        result = self.cli("reconcile", "--db", self.db, "--run", run)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["validation"], "drift_detected")

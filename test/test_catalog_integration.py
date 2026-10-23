"""Real subprocess/SQLite/Parquet/XLS surface; retained evidence when CATALOG_EVIDENCE_DIR is set."""

import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import pyarrow.parquet as pq
from openpyxl import load_workbook

from test.test_catalog_planner import FIXTURE


class IntegrationTests(unittest.TestCase):
    def test_offline_cli_sqlite_parquet_and_comparison_workflow(self):
        # Given the actual command surface and a bounded, explicitly synthetic source.
        retained = os.environ.get("CATALOG_EVIDENCE_DIR")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = (
            Path(retained) / ("surface-" + uuid4().hex[:12])
            if retained
            else Path(temporary.name)
        )
        root.mkdir(parents=True, exist_ok=True)
        db = root / "catalog_v2.db"
        fixture = root / "source.json"
        body = json.loads(FIXTURE.read_text())
        for row in body["rows"]:
            row.update(itemIdnfNm="안내판, AB, 600×150mm", itemIndfNmView="안내판, AB, 600×150mm",
                       pdctAtrbCdDtlNm="재질:목재,용도:종합안내,지지대수:0개")
        fixture.write_text(json.dumps(body, ensure_ascii=False))
        records = []

        def invoke(label, arguments, expected):
            command = [sys.executable, *map(str, arguments)]
            result = subprocess.run(
                command, check=False, capture_output=True, text=True
            )
            (root / (label + ".stdout")).write_text(result.stdout)
            (root / (label + ".stderr")).write_text(result.stderr or "(empty stderr)\n")
            records.append(
                {
                    "scenario": label,
                    "invocation": command,
                    "exit_code": result.returncode,
                    "stdout": str(root / (label + ".stdout")),
                    "stderr": str(root / (label + ".stderr")),
                }
            )
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return result

        invoke("help", ["catalog_cli.py", "--help"], 0)
        invoke("inspect", ["catalog_cli.py", "inspect", "--fixture", fixture], 0)
        capped = invoke(
            "limited-crawl",
            [
                "catalog_cli.py",
                "crawl",
                "--db",
                db,
                "--fixture",
                fixture,
                "--max-pages",
                "2",
            ],
            2,
        )
        limited = json.loads(capped.stdout)["run_id"]
        invoke(
            "limited-query-denied",
            ["catalog_cli.py", "query", "--db", db, "--run", limited],
            2,
        )
        invoke(
            "limited-query-allowed",
            [
                "catalog_cli.py",
                "query",
                "--db",
                db,
                "--run",
                limited,
                "--allow-incomplete",
                "--keyword",
                "ab 안내판",
            ],
            2,
        )
        invoke(
            "limited-export-denied",
            [
                "catalog_cli.py",
                "export",
                "--db",
                db,
                "--run",
                limited,
                "--out",
                root / "denied.parquet",
            ],
            2,
        )
        invoke(
            "limited-export-allowed",
            [
                "catalog_cli.py",
                "export",
                "--db",
                db,
                "--run",
                limited,
                "--allow-incomplete",
                "--out",
                root / "limited.parquet",
            ],
            2,
        )
        invoke(
            "limited-resume",
            [
                "catalog_cli.py",
                "resume",
                "--db",
                db,
                "--run",
                limited,
                "--fixture",
                fixture,
            ],
            2,
        )
        complete = invoke(
            "complete-crawl",
            ["catalog_cli.py", "crawl", "--db", db, "--fixture", fixture],
            0,
        )
        reconciled = json.loads(complete.stdout)["run_id"]
        invoke(
            "default-query",
            ["catalog_cli.py", "query", "--db", db, "--keyword", "ab 안내판"],
            0,
        )
        invoke(
            "default-export",
            [
                "catalog_cli.py",
                "export",
                "--db",
                db,
                "--out",
                root / "complete.parquet",
            ],
            0,
        )
        invoke(
            "xlsx-export",
            ["catalog_cli.py", "export", "--db", db, "--out", root / "complete.xlsx"],
            0,
        )
        invoke(
            "catalog-gate", ["catalog_cli.py", "inspect", "--scope", "catalog-goods"], 4
        )
        invoke(
            "digital-gate", ["catalog_cli.py", "inspect", "--scope", "digital-main"], 4
        )

        # A tiny real BIFF2 XLS is enough to exercise the unmodified legacy XLS reader.
        def biff(code, data):
            return struct.pack("<HH", code, len(data)) + data

        workbook = biff(9, struct.pack("<HH", 2, 16)) + biff(
            0x42, struct.pack("<H", 949)
        )
        for i, row in enumerate(
            [
                ["물품식별번호", "한글품명", "재질", "희망가(부가세포함)", "용도", "지지대수"],
                ["ours", "안내판, 테스트, 600×150mm", "목재", "100", "종합안내", "0"],
            ]
        ):
            for j, value in enumerate(row):
                encoded = value.encode("cp949")
                workbook += biff(
                    4,
                    struct.pack("<HH", i, j)
                    + b"\0\0\0"
                    + bytes([len(encoded)])
                    + encoded,
                )
        xls = root / "input.xls"
        xls.write_bytes(workbook + biff(10, b""))
        runner = root / "offline_compare.py"
        runner.write_text(
            "import sys,runpy\nfrom pathlib import Path\nsys.path.insert(0,str(Path.cwd()))\nimport requests\ndef blocked(*args,**kwargs):\n raise AssertionError('HTTP forbidden in offline comparison')\nrequests.Session.request=blocked\nsys.argv=['compare_file.py',*sys.argv[1:]]\nrunpy.run_path('compare_file.py',run_name='__main__')\n"
        )
        # When limited data is consumed explicitly with the whole network blocked.
        invoke(
            "compare-denied",
            [
                runner,
                xls,
                "--catalog-db",
                db,
                "--catalog-run",
                limited,
                "--out",
                root / "denied.xlsx",
            ],
            1,
        )
        invoke(
            "compare-offline",
            [
                runner,
                xls,
                "--catalog-db",
                db,
                "--catalog-run",
                limited,
                "--allow-incomplete",
                "--out",
                root / "comparison.xlsx",
            ],
            0,
        )
        # Then SQL, Parquet, and workbook prove the same run and original fields.
        with sqlite3.connect(db) as con:
            metrics = {
                "integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_keys": con.execute("PRAGMA foreign_key_check").fetchall(),
            }
            counts = dict(
                con.execute("SELECT run_id,count(*) FROM run_entities GROUP BY run_id")
            )
            keys = {
                row[0]
                for row in con.execute(
                    "SELECT entity_key FROM run_entities WHERE run_id=?", (reconciled,)
                )
            }
            self.assertEqual(counts, {limited: 200, reconciled: 205})
            self.assertEqual(metrics, {"integrity": "ok", "foreign_keys": []})
        parquet = pq.read_table(root / "complete.parquet")
        self.assertEqual(set(parquet["entity_key"].to_pylist()), keys)
        self.assertEqual(
            json.loads(parquet["raw_json"][0].as_py())["extra"]["unicode"], "한글"
        )
        with (root / "comparison.xlsx").open("rb") as stream:
            book = load_workbook(stream)
            cells = [str(cell.value) for sheet in book for row in sheet for cell in row]
            self.assertTrue(any(limited in value for value in cells))
            self.assertIn("https://invalid/image.png", cells)
            self.assertIn("종합안내 / 지지대 0개", cells)
            self.assertNotIn("비교 보류: 지지대수 미확인", cells)
            book.close()
        report = {
            "environment": "fixture",
            "live_verified": False,
            "source_snapshot_verified": False,
            "limited_run": limited,
            "reconciled_run": reconciled,
            "sql_counts": counts,
            "sqlite": metrics,
            "parquet_rows": parquet.num_rows,
            "network_blocked_comparison": True,
            "commands": records,
            "artifacts": [str(p) for p in root.iterdir() if p.is_file()],
        }
        (root / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        if retained:
            print("CATALOG_SURFACE_EVIDENCE=" + str(root / "result.json"))


def live_smoke() -> int:
    """Opt-in only: one inspect page and a four-page permanently limited run."""
    if os.environ.get("CATALOG_LIVE_SMOKE") != "1":
        print("Live QA requires explicit CATALOG_LIVE_SMOKE=1; no network requested.")
        return 4
    root = Path(".omo/evidence/diagnostic-collector") / ("live-" + uuid4().hex[:12])
    root.mkdir(parents=True)
    db = root / "catalog_v2.db"
    records = []
    run = None
    commands = [
        ["inspect", "--scope", "shopping-all", "--network", "--max-pages", "1"],
        ["crawl", "--db", str(db), "--scope", "shopping-all", "--max-pages", "4"],
    ]
    for index, args in enumerate(commands):
        result = subprocess.run(
            [sys.executable, "catalog_cli.py", *args],
            check=False,
            capture_output=True,
            text=True,
        )
        (root / f"{index}.stdout").write_text(result.stdout)
        (root / f"{index}.stderr").write_text(result.stderr or "(empty stderr)\n")
        records.append({"command": args, "exit_code": result.returncode})
        expected = 0 if index == 0 else 2
        if result.returncode != expected:
            (root / "result.json").write_text(
                json.dumps(
                    {"live_verified": False, "failure": True, "commands": records}
                )
            )
            return 1
        if index == 1:
            payload = json.loads(result.stdout)
            if payload.get("state") != "limited":
                return 1
            run = payload["run_id"]
    for index, args in enumerate(
        [
            ["query", "--db", str(db), "--run", run, "--allow-incomplete"],
            [
                "export",
                "--db",
                str(db),
                "--run",
                run,
                "--allow-incomplete",
                "--out",
                str(root / "sample.parquet"),
            ],
        ],
        start=2,
    ):
        result = subprocess.run(
            [sys.executable, "catalog_cli.py", *args],
            check=False,
            capture_output=True,
            text=True,
        )
        (root / f"{index}.stdout").write_text(result.stdout)
        (root / f"{index}.stderr").write_text(result.stderr or "(empty stderr)\n")
        records.append({"command": args, "exit_code": result.returncode})
        if result.returncode != 2:
            return 1
    with sqlite3.connect(db) as con:
        count = con.execute(
            "SELECT count(*) FROM run_entities WHERE run_id=?", (run,)
        ).fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if pq.read_table(root / "sample.parquet").num_rows != count or integrity != "ok":
        return 1
    (root / "result.json").write_text(
        json.dumps(
            {
                "live_verified": True,
                "coverage_complete": False,
                "source_snapshot_verified": False,
                "pages_budget": 5,
                "run": run,
                "rows": count,
                "commands": records,
            },
            indent=2,
        )
    )
    print(root / "result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(live_smoke())

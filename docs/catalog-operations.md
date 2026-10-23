# Independent diagnostic catalog

`catalog_v2.db` is a diagnostic SQLite database for inspection, local suggestions and export. It is separate from the updater's `control/catalog.sqlite` and immutable operational snapshots. It cannot serve as an auto/shadow matching snapshot. `source_snapshot_verified` is always false: successful reconciliation describes an observation window, not a server-guaranteed point-in-time snapshot.

## Scopes and gates

- `shopping-all`: existing shopping target, empty purchase-method filter (not a hardcoded list of four item types).
- `shopping-main`: the legacy main-item filter `본010001`.
- `digital-main`: captured digital endpoint/target/main-item filter; pagination and identity contracts remain unvalidated. `digital` remains a separate unvalidated unrestricted-filter name.
- `catalog-goods` / `catalog`: official catalog API 15129417 is the intended source. Missing `DATA_GO_KR_SERVICE_KEY` returns `requires_credentials`; supplying a key still returns `requires_contract_validation`. Classification coverage, required query fields, pagination termination, usage/deletion status and independent counts are not established. No invented adapter or empty successful result is returned. Keys are never persisted or printed.
- Other names, including venture, innovation, service and contract-history, return `unsupported_scope`.

The historical shopping/digital counts and catalog last-page estimate in the plan are observations under different filters and times. They must not be added into a global goods total. Goods references and contract-listing keys are distinct; repeated goods numbers are expected across contracts. Catalog goods may have no current sale price.

## Reproducible offline example

The repository includes 205 synthetic contract records with 103 goods references. This fixture proves local mechanics, not the live API contract.

```sh
.venv/bin/python catalog_cli.py --help
.venv/bin/python catalog_cli.py inspect --scope shopping-all
.venv/bin/python catalog_cli.py inspect --fixture test/fixtures/diagnostic/rows.json
.venv/bin/python catalog_cli.py crawl --db catalog_v2.db --fixture test/fixtures/diagnostic/rows.json
.venv/bin/python catalog_cli.py query --db catalog_v2.db --keyword 'ab 안내판' --limit 20 --json
.venv/bin/python catalog_cli.py export --db catalog_v2.db --out catalog.parquet
.venv/bin/python catalog_cli.py export --db catalog_v2.db --out catalog.xlsx
```

Each crawl creates a new run. stdout supplies the run ID and separate collection/reconciliation states. With a fixture, the profile endpoint records its absolute fixture path so it cannot resume as a live profile. Query/export default to the scope's last reconciled run. Local search performs Unicode NFKC/casefold and whitespace-token AND substring matching; it does not claim equivalence with server search. Candidate totals, truncation and observation timestamps accompany query output.

A limited observation is deliberately not published, even if its page limit happened to cover a small fixture:

```sh
.venv/bin/python catalog_cli.py crawl --db catalog_v2.db --fixture test/fixtures/diagnostic/rows.json --max-pages 2
# Substitute the exact run ID printed above:
.venv/bin/python catalog_cli.py resume --db catalog_v2.db --run RUN_ID --fixture test/fixtures/diagnostic/rows.json
.venv/bin/python catalog_cli.py reconcile --db catalog_v2.db --run RUN_ID
.venv/bin/python catalog_cli.py query --db catalog_v2.db --run RUN_ID --allow-incomplete --keyword 안내판
.venv/bin/python catalog_cli.py export --db catalog_v2.db --run RUN_ID --allow-incomplete --out limited.parquet
.venv/bin/python compare_file.py INPUT.xls --catalog-db catalog_v2.db --catalog-run RUN_ID --allow-incomplete --out comparison.xlsx
```

The cap is global to that run, including its initially persisted page, and remains fixed on resume. It cannot be increased to turn a sample into a complete run. Omitting `--allow-incomplete` rejects limited/failed/unreconciled data. Approximate suggestion retries use the same selected DB/run and preserve incompleteness. Offline comparisons retain image URLs and make no external image requests. Seller/manufacturer merging remains a legacy suggestion display convention and is not a strict auto DTO.

## Live access and completion

`inspect` without `--network` does not request the website. `crawl` without `--fixture` uses the website. A bounded QA invocation must be explicitly opted into and must never exceed five pages total:

```sh
# Explicit operator opt-in; one inspect page plus at most four crawl pages.
CATALOG_LIVE_SMOKE=1 .venv/bin/python -m unittest test.test_catalog_integration -v
```

The default unittest suite never uses the live website. No full crawl, official key approval, live digital/catalog contract, or exact historical count is certified by fixture evidence. Live network failure is a failed observation, not a passing or skipped live check.

Uncapped shopping collection above 1,000 rows requires an explicit Decimal price domain using `--price-upper` and `--price-quantum`. The domain is an operator-supplied coverage hypothesis, not a hardcoded claim about maximum prices. The shared partition planner splits inclusive exact Decimal ranges; a single-price overflow is unresolved. No-price totals must agree with partition totals, start/end counts and per-page totals must agree, and observed prices must belong to the declared domain. Missing/null/out-of-domain prices, quarantined identities or conflicting aliases, repeated/short/missing pages, and count drift block reconciliation. Equal counts cannot rule out undetectable source replacements between observations.

Defaults are two workers, at most four pending pages and one request start per second. The shared transport applies the same limiter to every attempt, connect/read timeouts of 10/35 seconds, at most three HTTP attempts, 1/3-second backoff plus jitter, and Retry-After capped at 120 seconds. The diagnostic collector adds no second retry layer. Each page (at most 100 rows), all original observations and its checkpoint commit together. Source profiles, normalized-source versions and partition plans are fixed for resume; successful pages are not overwritten. Process interruption leaves a resumable run; retry failed pages with the same profile/fixture and run ID. Fresh acquisition requires a new run.

Preflight requires at least 1 GiB free and a conservative estimate of 8 KiB per planned row (limited runs use their sample budget). Actual raw row size, indexes, WAL, concurrent exports and retention can require more space; monitor disk capacity and retain additional headroom. No historical timing comment guarantees completion time. A disk/HTTP failure records a failed run and never changes the published pointer.

## Exit codes and recovery

| Code | Meaning |
| --- | --- |
| 0 | Valid inspect or reconciled operation |
| 2 | Incomplete/limited run or coverage unresolved; allowed partial query/export still returns 2 |
| 3 | Count drift detected |
| 4 | Unsupported scope, missing credentials or unvalidated source contract |
| 1 | Other failure |

`crawl_all.py` now forwards to diagnostic `crawl` with default `catalog_v2.db`. Its imported legacy price-window helpers remain for old direct tests; the CLI does not write old `items`/`done` tables. Never pass the old `shop_all.db` as a diagnostic writer destination: the schema is rejected without mutation. The old normalized rows cannot reconstruct discarded original fields or missing contract identities; legacy done flags cannot prove a new observation is current.

Keep the old DB until the new sidecar is verified. Roll back consumers by selecting a prior reconciled run explicitly or the preserved old export. Failed diagnostic runs do not replace the last published pointer or delete missing products. There is no implicit operational promotion or destructive migration.

`export_parquet.py --db shop_all.db --out legacy.parquet` still reads legacy `items` data, marking it `legacy_unverified` with `raw_original_available=false`. New exports include run/scope/profile/entity/source IDs, goods references, original JSON (including parent references and unknown keys), observation time and validation metadata. Parquet uses bounded batches; XLSX uses streaming sheets with at most 1,048,576 rows including the header and escapes formula-like strings. Output uses a temporary `.partial` file and atomic replacement only after a successful write. Incomplete exports retain their status in file metadata.

## Verification and retained artifacts

```sh
.venv/bin/python -m unittest test.test_g2b_client -v
.venv/bin/python -m unittest test.test_crawl -v
.venv/bin/python -m unittest discover -s test -t . -v
CATALOG_EVIDENCE_DIR=.omo/evidence/diagnostic-collector .venv/bin/python -m unittest test.test_catalog_integration -v
```

The surface scenario retains stdout/stderr, SQLite, Parquet, XLS/XLSX and a result JSON with the exact invocations, exit codes, keys, counts and integrity checks. Crash tests terminate actual child processes before and after page commit. Live/browser verification is separate and remains unverified unless its opt-in artifacts exist.

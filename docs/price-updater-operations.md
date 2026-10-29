# Price updater operations

The updater publishes reconciled observations, not a source-guaranteed simultaneous market snapshot. Existing legacy crawl `done` markers are never refresh checkpoints. There is no schedule; collection runs only when an operator runs `refresh`. Production source access is disabled by default.

## Running a refresh

`python -m src.updater.cli refresh --mode full` collects, reconciles and publishes in the current process. Set `NARA_DATA_DIR` for the store location and `NARA_SOURCE_ENABLED=true` to allow live source access; without it the command exits before contacting the source. It refuses with `maintenance_active` while the maintenance flag is set and prints a JSON summary of the publication on success. For recurring collection, schedule this command with the host's scheduler (for example cron); there is no built-in schedule.

Default full collection probes pages beyond the former 1,000-row safety threshold, the midpoint and the final page. It rejects short, repeated, overlapping or count-mismatched probes. It then persists one unfiltered scope and divides page work into chunks of at most ten pages, with four tasks in flight and a shared request-start budget of one per second. This avoids splitting prices and handles large groups of identically priced products. Every collected page still checks counts, unique item keys, fingerprints and expected length; final reconciliation and the 12-hour observation limit remain mandatory. An explicit `NARA_PRICE_DOMAIN_JSON` still selects the original Decimal price partition strategy using `lower`, `upper`, `quantum`, and `max_partition_rows`; its single-price overflow remains unresolved.

## Data and diagnostics

`status --json` includes source activation, run attempts, maintenance state, source observation window, information groups and last successful refresh time. When a stage exists, `collection_progress` reports expected/collected items and expected/completed pages. `current`, `history LISTING_KEY`, `history LISTING_KEY --price-only`, `catalog-manifest --out PATH`, and `export --out PATH.xlsx` read published data. Excel includes current items, field changes, and price history; timestamps identify Asia/Seoul and all source text remains literal. Warning starts after 36 hours and stale after 72 hours, measured from source observations rather than maintenance publication time.

Large original page responses are retained losslessly as gzip BLOB values in the existing stage `pages.raw_json` column; smaller responses remain text. SQLite's existing non-strict column accepts both. Decode BLOB values with `gzip.decompress(value).decode()` before parsing; row evidence and operational snapshots keep their existing JSON contracts. No previous page evidence is rewritten. See [live capacity measurements](collection-capacity.md) for initial size estimates; neither images nor future history growth is included.

Collection tasks carry bounded page chunks, persist each page with its checkpoint, and retry transient failures once. HTTP transport attempts at most three calls, so an unsuccessfully retried page has at most six HTTP attempts. Publication retries are zero. A publication event and active pointer commit in one SQLite transaction. A failure to acknowledge afterward cannot undo committed data.

## Maintenance, backup and restore

Use `src.updater.maintenance.enter_maintenance(Store())` before a maintenance operation. The persistent `control/maintenance.flag` stops new `refresh` runs and production matching readers. Existing pinned readers finish. The flag persists across restarts.

`backup(store, destination)` copies control with SQLite Online Backup and all extant event-referenced immutable snapshot files, including retained rollback versions, checking their SHA256. This is the complete backup; there is no separate orchestration database.

`restore(store, backup_path)` validates the bundle, enters maintenance, and refuses to replace files while any snapshot pin is held. It leaves the gate active. Validate published queries, history and hashes after restoration. Matching ledger, model, calibrator and vocabulary recovery is a separate coordinated operation; this module does not modify those artifacts. Only after those checks should an operator call `resume(store)`. Resume removes the business gate.

`prune_files` retains the newest three publication snapshots, every current pointer, and all pinned versions. Terminal stages expire after seven days. `prune_history` publishes a new maintenance snapshot for 365-day history retention, retaining current versions, the boundary baseline and all referenced versions/evidence. It preserves source refresh identity, observation windows, checked/changed times. Immutable files are never edited in place. Publication tombstones remain in control after snapshot deletion.

Disk preflight uses measured stage/snapshot bytes, three-copy allowance and 20% headroom with a minimum 2 GiB free space. Large production runs still need a measured sample of at most 1,000 rows and an approved full-scope estimate. Current sample measurements do not certify production capacity.

import tempfile
import unittest
from pathlib import Path
from src.updater.collector import refresh, CollectionPolicy
from src.updater.store import Store
from test.updater_fixtures import FixtureSource, row
from src.matching.catalog import open_catalog, CatalogError


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "source")
        self.manifest = refresh(
            self.store,
            FixtureSource([row("001")]),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )

    def test_real_producer_projection_preserves_unknown(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            candidate = next(catalog.candidates())
            self.assertEqual(candidate.snapshot_id, self.manifest.snapshot_id)
            self.assertIsNone(candidate.seller_id)
            self.assertIsNone(candidate.price.tax_basis)
            self.assertFalse(catalog.manifest.source_snapshot_verified)
            self.assertEqual(candidate.environment, "fixture")

    def test_fixture_cannot_enter_production(self):
        with self.assertRaises(CatalogError):
            with open_catalog(self.manifest, root=self.store.root, production=True):
                pass

    def test_untrusted_root_rejected(self):
        with self.assertRaises(CatalogError):
            with open_catalog(self.manifest, root=self.root / "other"):
                pass

    def test_stale_generation_rejected(self):
        refresh(
            self.store,
            FixtureSource([row("001", "110")]),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )
        with self.assertRaises(CatalogError):
            with open_catalog(self.manifest, root=self.store.root):
                pass

    def test_corrupt_source_hash_rejected(self):
        with (self.store.root / self.manifest.snapshot_path).open("ab") as stream:
            stream.write(b"corrupt")
        with self.assertRaises(CatalogError):
            with open_catalog(self.manifest, root=self.store.root):
                pass

    def test_manifest_path_escape_rejected(self):
        bad = self.manifest.model_copy(update={"snapshot_path": "../escape.sqlite"})
        with self.assertRaises(CatalogError):
            with open_catalog(bad, root=self.store.root):
                pass

    def test_alias_conflict_never_verified_identity(self):
        raw = row("conflict")
        raw["itemIndfNmView"] = "different name"
        manifest = refresh(
            self.store,
            FixtureSource([raw]),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )
        with open_catalog(manifest, root=self.store.root) as catalog:
            candidate = next(
                c for c in catalog.candidates() if c.listing_key.endswith(":conflict")
            )
            self.assertFalse(candidate.goods_id_verified)
            self.assertEqual(
                dict(candidate.attributes)["identity_name"].status.value, "conflicting"
            )
            self.assertIsNone(dict(candidate.group_checked_at)["identity"])

    def test_retained_old_timestamp_is_not_currently_checked(self):
        raw = row("001")
        del raw["pdctAtrbCdDtlNm"]
        manifest = refresh(
            self.store,
            FixtureSource([raw], tick=1),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )
        with open_catalog(manifest, root=self.store.root) as catalog:
            self.assertIsNone(
                dict(next(catalog.candidates()).group_checked_at)["specification"]
            )

    def test_pin_held_for_context_and_released(self):
        import fcntl

        pin = self.store.root / self.manifest.reader_locator.pin
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            with pin.open("a+") as stream:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(len(tuple(catalog.candidates())), 1)
        with pin.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream, fcntl.LOCK_UN)

    def test_maintenance_blocks_new_active_reader_but_existing_pin_finishes(self):
        from src.updater.reader import open_published_snapshot
        from src.updater.store import StoreError

        with open_catalog(self.manifest, root=self.store.root) as catalog:
            (self.store.root / "control/maintenance.flag").write_text("maintenance")
            with self.assertRaises(StoreError):
                with open_published_snapshot(
                    self.manifest, root=self.store.root, require_active=True
                ):
                    pass
            self.assertEqual(len(tuple(catalog.candidates())), 1)

    def test_malformed_manifest_json_rejected(self):
        path = self.root / "bad.json"
        path.write_text('{"snapshot_path":"legacy.db"}')
        with self.assertRaises(CatalogError):
            with open_catalog(path, root=self.store.root):
                pass

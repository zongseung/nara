import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from src.updater.collector import refresh, CollectionPolicy
from src.updater.store import Store
from test.updater_fixtures import FixtureSource, row
from src.matching.catalog import open_catalog, CatalogError
from src.matching.index import build_index, open_index


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "source")
        self.manifest = refresh(
            self.store,
            FixtureSource([row("001"), row("002")]),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )

    def test_real_source_index_roundtrip(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            descriptor = build_index(catalog, self.root / "index")
            with open_index(descriptor, catalog=catalog) as index:
                self.assertEqual(index.count(), 2)
                self.assertEqual(
                    [c.listing_key for c in index.candidates()],
                    [c.listing_key for c in catalog.candidates()],
                )

    def test_fts_optional_sql_remains(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            descriptor = build_index(catalog, self.root / "index", enable_fts=False)
            with open_index(descriptor, catalog=catalog) as index:
                self.assertFalse(index.manifest.fts_trigram)
                self.assertEqual(index.count(), 2)

    def test_closed_source_cannot_reuse_index(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            descriptor = build_index(catalog, self.root / "index")
        with self.assertRaises(CatalogError):
            with open_index(descriptor, catalog=catalog):
                pass

    def test_modified_index_rejected(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            descriptor = build_index(catalog, self.root / "index")
            with descriptor.path.open("ab") as stream:
                stream.write(b"changed")
            with self.assertRaises(CatalogError):
                with open_index(descriptor, catalog=catalog):
                    pass

    def test_generation_mismatch_rejected(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            descriptor = build_index(catalog, self.root / "index")
        later = refresh(
            self.store,
            FixtureSource([row("003")]),
            policy=CollectionPolicy(request_interval=0),
            environment="fixture",
        )
        with open_catalog(later, root=self.store.root) as catalog:
            with self.assertRaises(CatalogError):
                with open_index(descriptor, catalog=catalog):
                    pass

    def test_interrupted_build_not_published(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:

            class Broken:
                manifest = catalog.manifest
                require_open = catalog.require_open

                def candidates(self):
                    yield next(catalog.candidates())
                    raise RuntimeError("interrupted fixture")

            with self.assertRaises(RuntimeError):
                build_index(Broken(), self.root / "index")
            self.assertEqual(list((self.root / "index").iterdir()), [])

    def test_descriptor_file_and_metadata_validation(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            descriptor = build_index(catalog, self.root / "index")
            with open_index(descriptor.descriptor_path, catalog=catalog) as index:
                self.assertEqual(index.count(), 2)
            with self.assertRaises(CatalogError):
                with open_index(
                    replace(descriptor, generation="forged"), catalog=catalog
                ):
                    pass

    def test_missing_index_is_explicit_error(self):
        with open_catalog(self.manifest, root=self.store.root) as catalog:
            with self.assertRaises(CatalogError):
                with open_index(self.root / "missing.json", catalog=catalog):
                    pass

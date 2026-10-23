import ast
from pathlib import Path
import unittest


class ArchitectureTests(unittest.TestCase):
    def test_core_does_not_depend_on_application_or_io_adapters(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / 'core').is_dir())
        forbidden = {'src', 'requests', 'sqlite3', 'prefect', 'openpyxl', 'xlrd'}
        for path in (root / 'core').rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                for module in modules:
                    with self.subTest(file=path.relative_to(root), module=module):
                        self.assertNotIn(module.split('.')[0], forbidden)

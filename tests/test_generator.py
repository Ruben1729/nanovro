"""Generator contracts; run with Python's standard library alone."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nanovro_generator", ROOT / "generator/nanovro_generator.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GeneratorTests(unittest.TestCase):
    def generate(self, schema, options=None):
        return module.Generator(options).generate(schema, "test")

    def test_requires_explicit_bounds(self):
        for schema in ["string", "bytes", {"type": "array", "items": "int"}, {"type": "map", "values": "int"}]:
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "specify"):
                self.generate(schema)

    def test_options(self):
        h, c = self.generate({"type": "record", "name": "R", "fields": [{"name": "s", "type": "string"}]}, {"R.s": {"max_size": 7}})
        self.assertIn("data[8]", h)
        self.assertIn("NV_STRING", c)

    def test_recursive_rejected(self):
        with self.assertRaisesRegex(ValueError, "recursive"):
            self.generate({"type": "record", "name": "Node", "fields": [{"name": "next", "type": ["null", "Node"]}]})

    def test_namespaces_and_references(self):
        h, _ = self.generate({"type": "record", "name": "x.R", "fields": [{"name": "a", "type": {"type": "fixed", "name": "F", "size": 2}}, {"name": "b", "type": "F"}]})
        self.assertIn("nv_x_F nv_b", h)

    def test_rejects_bad_schemas(self):
        schemas = [[], ["int", "int"], [["int"], "null"], {"type": "fixed", "name": "F", "size": -1},
                   {"type": "enum", "name": "E", "symbols": ["A", "A"]},
                   {"type": "record", "name": "R", "fields": [{"name": "a", "type": "int"}, {"name": "a", "type": "long"}]},
                   "Missing", {"type": "string", "nanovro.max_size": True}]
        for schema in schemas:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                self.generate(schema)

    def test_deterministic_and_keyword_safe(self):
        schema = {"type": "record", "name": "struct", "fields": [{"name": "class", "type": "int"}]}
        self.assertEqual(self.generate(schema), self.generate(schema))
        self.assertIn("nv_class", self.generate(schema)[0])

    def test_c_symbol_collisions(self):
        for name in ["type", "field", "int_type", "test_encode"]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "collision"):
                self.generate({"type": "record", "name": name, "fields": []})
        with self.assertRaisesRegex(ValueError, "collision"):
            self.generate({"type": "enum", "name": "E", "symbols": ["type"]})

    def test_single_underscore_name(self):
        self.assertIn("nv__", self.generate({"type": "record", "name": "_", "fields": []})[0])


if __name__ == "__main__":
    unittest.main()

"""Generated schema-resolution tests against Apache Avro; needs requirements.txt and GCC."""
import ctypes
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import json

import avro.io
import avro.schema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "generator"))
from nanovro_resolver import generate, generate_bundle


def record(name, fields, **kw):
    return dict(type="record", name=name, fields=[dict(name=n, type=t, **extra) for n, t, extra in fields], **kw)


def string(cap=64):
    return {"type": "string", "nanovro.max_size": cap}


def array(t, cap=16):
    return {"type": "array", "items": t, "nanovro.max_count": cap}


def mapping(t):
    return {"type": "map", "values": t, "nanovro.max_count": 8, "nanovro.max_key_size": 16}


CASES = []
def case(w, r, value, expected=None):
    CASES.append((w, r, value, expected))


case(record("Added", [("id", "int", {})]), record("Added", [
    ("text", string(), {"default": "café"}), ("id", "long", {}),
    ("array", array("double"), {"default": [1.5, -2.0]}),
    ("map", mapping("int"), {"default": {"a": 7}}),
    ("union", ["null", "int"], {"default": None}),
    ("raw", {"type": "bytes", "nanovro.max_size": 4}, {"default": "x"}),
    ("fixed", {"type": "fixed", "name": "DefaultFixed", "size": 2}, {"default": "ab"}),
    ("enum", {"type": "enum", "name": "DefaultEnum", "symbols": ["A", "B"]}, {"default": "B"}),
    ("nested", record("DefaultNested", [("x", "boolean", {})]), {"default": {"x": True}}),
]), {"id": -123})
case(record("Removed", [("ignored", array(string()), {}), ("map", mapping("long"), {}), ("x", "int", {})]),
     record("Removed", [("x", "int", {})]), {"ignored": ["hello", "world"], "map": {"a": -9}, "x": 42})
case(record("Reordered", [("a", "int", {}), ("b", string(), {})]),
     record("Reordered", [("b", string(), {}), ("a", "int", {})]), {"a": 99, "b": "abc"})
case(record("OldName", [("old", "int", {})]),
     record("NewName", [("new", "long", {"aliases": ["old"]})], aliases=["OldName"]), {"old": 7}, {"new": 7})
for w, r in [("int", "long"), ("int", "float"), ("int", "double"), ("long", "float"), ("long", "double"), ("float", "double")]:
    case(w, r, -123)
# Apache Avro Python 1.12 rejects these permitted promotions at its schema
# matching layer; use explicit expected values, then its reader-wire decoder.
case(array("float", 256), array("double", 256), [i * 0.25 for i in range(256)], [i * 0.25 for i in range(256)])
case(mapping("int"), mapping("long"), {"a": -2147483648, "b": 2147483647}, {"a": -2147483648, "b": 2147483647})
case(["int", string(), "null"], ["null", "long", string()], "hello")
case(["int", string(), "null"], ["null", "long", string()], -45)
case(["int", string(), "null"], "long", 123, 123)
case("int", ["null", "long"], 45)
case({"type": "enum", "name": "Color", "symbols": ["RED", "BLUE", "GREEN"]},
     {"type": "enum", "name": "Color", "symbols": ["BLUE", "RED", "OTHER"], "default": "OTHER"}, "GREEN", "OTHER")
case({"type": "bytes", "nanovro.max_size": 16}, string(16), "café".encode(), "café")
case(string(16), {"type": "bytes", "nanovro.max_size": 16}, "café", "café".encode())
case(array("float"), array("float"), [0.0, -0.0, 1.25])
case(record("References", [("a", record("ReferenceInner", [("x", "int", {})]), {}), ("b", "ReferenceInner", {})]),
     record("References", [("b", record("ReferenceInner", [("x", "long", {})]), {}), ("a", "ReferenceInner", {})]),
     {"a": {"x": -12}, "b": {"x": 45}})
case({"type": "enum", "name": "NoDefaultEnum", "symbols": ["A", "B"]},
     {"type": "enum", "name": "NoDefaultEnum", "symbols": ["A"]}, "A")
case(string(16), string(2), "ok")
case(record("SkipFixedWidth", [("ignored", array("float"), {}), ("x", "int", {})]),
     record("SkipFixedWidth", [("x", "int", {})]), {"ignored": [1.25, -2.5], "x": 17})
case(record("SkipEmpty", [("ignored", array(record("Empty", [("n", "null", {})])), {}), ("x", "int", {})]),
     record("SkipEmpty", [("x", "int", {})]), {"ignored": [{"n": None}], "x": 23})


def avro_encode(schema, value):
    target = io.BytesIO()
    avro.io.DatumWriter(schema).write(value, avro.io.BinaryEncoder(target))
    return target.getvalue()


class Resolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nanovro-resolution-")
        cls.addClassCleanup(cls.tmp.cleanup)
        directory = Path(cls.tmp.name)
        sources = []
        for i, (w, r, _, _) in enumerate(CASES):
            stem = f"pair{i}"
            bundled = os.environ.get("NV_TEST_BUNDLE") == "1"
            files = generate_bundle([w], r, stem) if bundled else generate(w, r, stem)
            for name, text in files.items():
                (directory / name).write_text(text, encoding="utf-8")
                if name.endswith(".c"):
                    sources.append(str(directory / name))
            # Keep reader type declarations isolated between schema pairs.
            from nanovro_resolver import Layout
            layout = Layout(); layout.generate(r, stem + "_reader")
            ct = layout.paths["Root"]["ct"]
            bridge = directory / f"bridge{i}.c"
            bridge.write_text(f'''#include "{stem}.h"
#include <string.h>
typedef struct {{const unsigned char *p; size_t n, pos;}} input;
static bool read_cb(void *ctx, uint8_t *p, size_t n) {{
    input *s=ctx; if(n>s->n-s->pos) return false;
    memcpy(p,s->p+s->pos,n);s->pos+=n;return true;
}}
int run{i}(const unsigned char *p, size_t n, unsigned char *out, size_t cap, size_t *written, int callback) {{
    {ct} value = {{0}};
    input ctx={{p,callback==2 && n ? n-1 : n,0}};
    nv_istream s = callback ? nv_istream_callback(read_cb,&ctx,n) : nv_istream_buffer(p,n);
    nv_ostream o = nv_ostream_buffer(out,cap);
    if(!{f'nv_{stem}_select(0)' if bundled else f'nv_{stem}_decode'}(&s,&value)) return s.error;
    if(s.position!=n) return 99;
    if(!nv_{stem}_reader_encode(&o,&value)) return o.error;
    *written=o.position; return 0;
}}
''', encoding="utf-8")
            sources.append(str(bridge))
        library = directory / ("resolution.dll" if os.name == "nt" else "resolution.so")
        subprocess.run([os.environ.get("CC", "gcc"), "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-I" + str(ROOT / "include"), "-I" + str(directory), str(ROOT / "src/nanovro.c"), *sources, "-o", str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        if os.name == "nt":
            import _ctypes
            cls.addClassCleanup(_ctypes.FreeLibrary, cls.lib._handle)
        cls.functions = []
        for i in range(len(CASES)):
            f = getattr(cls.lib, f"run{i}")
            f.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
            f.restype = ctypes.c_int
            cls.functions.append(f)

    def run_case(self, index, wire, callback=0):
        output = ctypes.create_string_buffer(8192); written = ctypes.c_size_t()
        error = self.functions[index](wire, len(wire), output, len(output), ctypes.byref(written), callback)
        return error, output.raw[:written.value]

    def test_reference_resolution(self):
        for i, (w, r, value, expected) in enumerate(CASES):
            with self.subTest(pair=i):
                ws, rs = avro.schema.parse(json.dumps(w)), avro.schema.parse(json.dumps(r))
                wire = avro_encode(ws, value)
                if expected is None:
                    expected = avro.io.DatumReader(ws, rs).read(avro.io.BinaryDecoder(io.BytesIO(wire)))
                for callback in (0, 1):
                    error, output = self.run_case(i, wire, callback)
                    self.assertEqual(error, 0)
                    actual = avro.io.DatumReader(rs).read(avro.io.BinaryDecoder(io.BytesIO(output)))
                    self.assertEqual(actual, expected)

    def test_every_truncation(self):
        for i, (w, _, value, _) in enumerate(CASES):
            wire = avro_encode(avro.schema.parse(json.dumps(w)), value)
            for length in range(len(wire)):
                for callback in (0, 1):
                    self.assertNotEqual(self.run_case(i, wire[:length], callback)[0], 0, (i, length))

    def test_incompatible_union_branch(self):
        index = 14
        self.assertEqual(CASES[index][1], "long")
        wire = avro_encode(avro.schema.parse(json.dumps(CASES[index][0])), "incompatible")
        self.assertEqual(self.run_case(index, wire)[0], 7)

    def test_sized_array_blocks_and_limits(self):
        from nanovro_resolver import varint
        import struct
        payload = struct.pack("<ff", 1.25, -2.5)
        wire = varint(-2) + varint(8) + payload + varint(1) + struct.pack("<f", 3.5) + b"\0"
        for callback in (0, 1):
            error, output = self.run_case(10, wire, callback)
            self.assertEqual(error, 0)
            rs = avro.schema.parse(json.dumps(CASES[10][1]))
            self.assertEqual(avro.io.DatumReader(rs).read(avro.io.BinaryDecoder(io.BytesIO(output))), [1.25, -2.5, 3.5])
            for length in (7, 9):
                bad = varint(-2) + varint(length) + payload + b"\0\0"
                self.assertNotEqual(self.run_case(10, bad, callback)[0], 0)
            self.assertEqual(self.run_case(10, varint(257), callback)[0], 3)
            self.assertEqual(self.run_case(12, varint(3), callback)[0], 4)
        self.assertEqual(self.run_case(10, wire, 2)[0], 2)

    def test_skip_sized_blocks(self):
        from nanovro_resolver import varint
        # Removed.ignored (array<string>) uses a sized block, then map, x.
        wire = varint(-2) + varint(4) + b"\x02a\x02b" + b"\0\0" + varint(42)
        for callback in (0, 1):
            error, output = self.run_case(1, wire, callback)
            self.assertEqual(error, 0)
            self.assertEqual(output, varint(42))
        self.assertNotEqual(self.run_case(1, varint(-2) + varint(100) + b"\0")[0], 0)

    def test_enum_missing_symbol_and_reader_string_capacity(self):
        self.assertEqual(self.run_case(21, b"\x02")[0], 7)
        self.assertEqual(self.run_case(22, b"\x06abc")[0], 3)
        self.assertEqual(self.run_case(17, b"\x02\xff")[0], 5)
        from nanovro_resolver import varint
        self.assertEqual(self.run_case(4, varint(2147483648))[0], 4)
        self.assertEqual(self.run_case(21, varint(2))[0], 4)
        self.assertEqual(self.run_case(23, varint(9223372036854775807))[0], 3)
        self.assertEqual(self.run_case(24, varint(9223372036854775807) + b"\0" + varint(23)), (0, varint(23)))

    def test_generation_rejects_incompatible(self):
        for w, r in [("long", "int"), ("double", "float"),
                     (record("A", []), record("B", [])),
                     (record("A", []), record("A", [("x", "int", {})])),
                     (record("A", []), record("A", [("x", string(1), {"default": "large"})])),
                     (record("A", []), record("A", [("x", [string(1), {"type": "bytes", "nanovro.max_size": 32}], {"default": "large"})]))]:
            with self.subTest(writer=w, reader=r), self.assertRaises(ValueError):
                generate(w, r, "invalid")


if __name__ == "__main__":
    unittest.main()

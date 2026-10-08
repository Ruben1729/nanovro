"""Apache Avro cross-implementation tests. Requires tests/requirements.txt and gcc."""
import ctypes
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

import avro.io
import avro.schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {"type": "record", "name": "All", "fields": [
    {"name": "nothing", "type": "null"},
    {"name": "flag", "type": "boolean"},
    {"name": "small", "type": "int"},
    {"name": "big", "type": "long"},
    {"name": "single", "type": "float"},
    {"name": "real", "type": "double"},
    {"name": "text", "type": {"type": "string", "nanovro.max_size": 32}},
    {"name": "raw", "type": {"type": "bytes", "nanovro.max_size": 16}},
    {"name": "fixed", "type": {"type": "fixed", "name": "F", "size": 4}},
    {"name": "choice", "type": {"type": "enum", "name": "E", "symbols": ["A", "B", "C"]}},
    {"name": "array", "type": {"type": "array", "items": "long", "nanovro.max_count": 8}},
    {"name": "map", "type": {"type": "map", "values": "int", "nanovro.max_count": 4, "nanovro.max_key_size": 12}},
    {"name": "optional", "type": ["null", {"type": "string", "nanovro.max_size": 16}]},
    {"name": "nested", "type": {"type": "record", "name": "Inner", "fields": [{"name": "x", "type": "int"}]}},
    {"name": "again", "type": "Inner"},
]}


class InteropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nanovro-interop-")
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        (directory / "all.avsc").write_text(json.dumps(SCHEMA), encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "generator/nanovro_generator.py"), str(directory / "all.avsc"), "-o", str(directory)], check=True)
        (directory / "bridge.c").write_text('''#include "all.h"
int roundtrip(const unsigned char *input, size_t n, unsigned char *output, size_t cap, size_t *written) {
    nv_All value = {0};
    nv_istream in = nv_istream_buffer(input, n);
    nv_ostream out = nv_ostream_buffer(output, cap);
    if (!nv_all_decode(&in, &value)) return (int)in.error;
    if (in.position != n) return 99;
    if (!nv_all_encode(&out, &value)) return (int)out.error;
    *written = out.position;
    return 0;
}
int native_encode(unsigned char *output, size_t cap, size_t *written) {
    nv_All value = {0};
    nv_ostream out = nv_ostream_buffer(output, cap);
    value.nv_flag = true;
    value.nv_small = INT32_MIN;
    value.nv_big = INT64_MAX;
    value.nv_single = 23.5f;
    value.nv_real = -1234.125;
    value.nv_text.size = 2;
    value.nv_text.data[0] = 'h'; value.nv_text.data[1] = 'i';
    value.nv_raw.size = 1; value.nv_raw.data[0] = 255;
    value.nv_fixed.data[0] = 'a'; value.nv_fixed.data[1] = 'b';
    value.nv_fixed.data[2] = 'c'; value.nv_fixed.data[3] = 'd';
    value.nv_choice = nv_E_C;
    value.nv_array.count = 2;
    value.nv_array.items[0] = INT64_MIN; value.nv_array.items[1] = 127;
    value.nv_map.count = 1;
    value.nv_map.items[0].key.size = 1; value.nv_map.items[0].key.data[0] = 'k';
    value.nv_map.items[0].value = -123;
    value.nv_optional.tag = 1;
    value.nv_optional.value.branch1.size = 1; value.nv_optional.value.branch1.data[0] = 'x';
    value.nv_nested.nv_x = 42; value.nv_again.nv_x = -42;
    if (!nv_all_encode(&out, &value)) return (int)out.error;
    *written = out.position;
    return 0;
}
''', encoding="utf-8")
        library = directory / ("interop.dll" if os.name == "nt" else "interop.so")
        subprocess.run([os.environ.get("CC", "gcc"), "-std=c99", "-Wall", "-Wextra", "-Werror", "-pedantic", "-shared", "-fPIC", "-O2", "-I" + str(ROOT / "include"), "-I" + str(directory), str(ROOT / "src/nanovro.c"), str(directory / "all.c"), str(directory / "bridge.c"), "-o", str(library)], check=True)
        cls.library = ctypes.CDLL(str(library))
        cls.function = cls.library.roundtrip
        cls.function.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        cls.function.restype = ctypes.c_int
        cls.native = cls.library.native_encode
        cls.native.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        cls.native.restype = ctypes.c_int
        # Windows keeps loaded DLLs open; release before TemporaryDirectory cleanup.
        if os.name == "nt":
            import _ctypes
            cls.addClassCleanup(lambda: _ctypes.FreeLibrary(cls.library._handle))
        cls.schema = avro.schema.parse(json.dumps(SCHEMA))

    def payload(self, i=0):
        return dict(nothing=None, flag=bool(i % 2), small=[-(2**31), 2**31-1, 0, -1][i % 4],
                    big=[-(2**63), 2**63-1, 64, -64][i % 4], single=23.5, real=-1234.125,
                    text="A\x00é😀", raw=b"\x00\xff\x80", fixed=b"abcd", choice=["A", "B", "C"][i % 3],
                    array=[-1, 0, 2**63-1, -(2**63)] if i % 2 else [], map={"left": -1, "right": 123} if i % 2 else {},
                    optional="present" if i % 2 else None, nested={"x": 42}, again={"x": -42})

    def encode(self, value):
        stream = io.BytesIO()
        avro.io.DatumWriter(self.schema).write(value, avro.io.BinaryEncoder(stream))
        return stream.getvalue()

    def roundtrip(self, encoded, cap=4096):
        output = ctypes.create_string_buffer(4096)
        written = ctypes.c_size_t()
        result = self.function(encoded, len(encoded), output, cap, ctypes.byref(written))
        return result, output.raw[:written.value]

    def test_reference_roundtrips(self):
        rng = random.Random(123)
        for i in range(200):
            value = self.payload(i)
            if i >= 4:
                value["small"] = rng.randint(-(2**31), 2**31-1)
                value["big"] = rng.randint(-(2**63), 2**63-1)
            encoded = self.encode(value)
            result, actual = self.roundtrip(encoded)
            self.assertEqual(result, 0)
            self.assertEqual(actual, encoded)
            decoded = avro.io.DatumReader(self.schema).read(avro.io.BinaryDecoder(io.BytesIO(actual)))
            self.assertEqual(decoded, value)

    def test_native_c_values_to_apache(self):
        output = ctypes.create_string_buffer(4096)
        size = ctypes.c_size_t()
        self.assertEqual(self.native(output, len(output), ctypes.byref(size)), 0)
        decoded = avro.io.DatumReader(self.schema).read(avro.io.BinaryDecoder(io.BytesIO(output.raw[:size.value])))
        self.assertEqual(decoded, dict(nothing=None, flag=True, small=-(2**31), big=2**63-1,
            single=23.5, real=-1234.125, text="hi", raw=b"\xff", fixed=b"abcd", choice="C",
            array=[-(2**63), 127], map={"k": -123}, optional="x", nested={"x": 42}, again={"x": -42}))

    def test_negative_collection_blocks(self):
        value = self.payload(1)
        stream = io.BytesIO()
        encoder = avro.io.BinaryEncoder(stream)
        for field in self.schema.fields:
            if field.name in ("array", "map"):
                block_stream = io.BytesIO()
                block_encoder = avro.io.BinaryEncoder(block_stream)
                collection = value[field.name]
                if field.name == "array":
                    for item in collection:
                        block_encoder.write_long(item)
                else:
                    for key, item in collection.items():
                        block_encoder.write_utf8(key)
                        block_encoder.write_int(item)
                block = block_stream.getvalue()
                encoder.write_long(-len(collection))
                encoder.write_long(len(block))
                encoder.write(block)
                encoder.write_long(0)
            else:
                avro.io.DatumWriter(field.type).write(value[field.name], encoder)
        result, canonical = self.roundtrip(stream.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(canonical, self.encode(value))

    def test_every_truncation_and_output_capacity(self):
        encoded = self.encode(self.payload(1))
        for n in range(len(encoded)):
            self.assertNotEqual(self.roundtrip(encoded[:n])[0], 0, n)
            self.assertNotEqual(self.roundtrip(encoded, n)[0], 0, n)

    def test_over_capacity(self):
        for field, value in [("text", "a" * 33), ("raw", b"a" * 17), ("array", [0] * 9), ("map", {str(i): i for i in range(5)}), ("map", {"a" * 13: 1}), ("optional", "a" * 17)]:
            payload = self.payload()
            payload[field] = value
            self.assertNotEqual(self.roundtrip(self.encode(payload))[0], 0, field)

    def test_malformed_random_inputs(self):
        rng = random.Random(456)
        for _ in range(3000):
            self.roundtrip(rng.randbytes(rng.randrange(256)))


if __name__ == "__main__":
    unittest.main()

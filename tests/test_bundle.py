"""Shared plan/run correctness, fallback paths and connection-time selection."""
import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import avro.io
import avro.schema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "generator"))
from nanovro_resolver import generate_bundle


def schema(count, kind="int", reverse=False):
    fields = [dict(name=f"f{i}", type=kind) for i in range(count)]
    return dict(type="record", name="Shared", fields=list(reversed(fields)) if reverse else fields)


READER = schema(16, "long")
for f in READER["fields"]:
    f["default"] = 7
WRITERS = [schema(n) for n in range(16, -1, -1)] + [schema(16, reverse=True), schema(16, "long")]


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="nanovro-bundle-")
        cls.addClassCleanup(cls.tmp.cleanup)
        directory = Path(cls.tmp.name)
        files = generate_bundle(WRITERS, READER, "shared")
        cls.source = files["shared.c"]
        for name, content in files.items():
            (directory / name).write_text(content, encoding="utf-8")
        (directory / "bridge.c").write_text('''#include "shared.h"
#include <string.h>
typedef struct {const uint8_t *p;size_t n,pos;} input;
static bool read_cb(void *ctx,uint8_t *p,size_t n) {
    input *s=ctx;if(n>s->n-s->pos)return false;memcpy(p,s->p+s->pos,n);s->pos+=n;return true;
}
int run(size_t version,const uint8_t *wire,size_t n,int64_t *out,int callback) {
    nv_shared_decoder decoder=nv_shared_select(version);
    nv_Shared value;
    input ctx={wire,n,0};
    nv_istream s=callback ? nv_istream_callback(read_cb,&ctx,n) : nv_istream_buffer(wire,n);
    memset(&value,0xa5,sizeof(value));
    if(!decoder)return NV_ERR_SCHEMA;
    if(!decoder(&s,&value))return s.error;
    if(s.position!=n)return 99;
''' + "\n".join(f"out[{i}]=value.nv_f{i};" for i in range(16)) + '\nreturn 0;}\n', encoding="utf-8")
        lib = directory / ("bundle.dll" if os.name == "nt" else "bundle.so")
        subprocess.run([os.environ.get("CC", "gcc"), "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-I" + str(ROOT / "include"), str(ROOT / "src/nanovro.c"), *map(str, directory.glob("*.c")), "-o", str(lib)], check=True)
        cls.lib = ctypes.CDLL(str(lib))
        if os.name == "nt":
            import _ctypes
            cls.addClassCleanup(_ctypes.FreeLibrary, cls.lib._handle)
        cls.decode = cls.lib.run
        cls.decode.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64), ctypes.c_int]
        cls.decode.restype = ctypes.c_int

    def test_all_versions_against_reference_and_truncation(self):
        reader = avro.schema.parse(json.dumps(READER))
        for version, schema_dict in enumerate(WRITERS):
            writer = avro.schema.parse(json.dumps(schema_dict))
            datum = {f["name"]: (-2147483648 if i % 2 else 2147483647) for i, f in enumerate(schema_dict["fields"])}
            wire = io.BytesIO()
            avro.io.DatumWriter(writer).write(datum, avro.io.BinaryEncoder(wire))
            wire = wire.getvalue()
            expected = avro.io.DatumReader(writer, reader).read(avro.io.BinaryDecoder(io.BytesIO(wire)))
            for callback in (0, 1):
                output = (ctypes.c_int64 * 16)()
                self.assertEqual(self.decode(version, wire, len(wire), output, callback), 0, version)
                self.assertEqual(list(output), [expected[f"f{i}"] for i in range(16)])
                for n in range(len(wire)):
                    self.assertNotEqual(self.decode(version, wire, n, output, callback), 0, (version, n))

    def test_unknown_version(self):
        output = (ctypes.c_int64 * 16)()
        self.assertEqual(self.decode(len(WRITERS), b"", 0, output, 0), 7)

    def test_shared_representation(self):
        self.assertEqual(self.source.count("static const nv_resolution_run "), 1)
        self.assertEqual(self.source.count("{NV_RES_PROMOTE,"), 1)
        self.assertEqual(self.source.count("{NV_RES_DEFAULT,"), 1)
        self.assertIn("NV_RES_RECORD", self.source)  # Reordered and empty writers use the general path.

    def test_deterministic_and_invalid_bundle(self):
        self.assertEqual(generate_bundle(WRITERS, READER, "shared")["shared.c"], self.source)
        with self.assertRaises(ValueError):
            generate_bundle([], READER, "bad")
        with self.assertRaises(ValueError):
            generate_bundle(["bytes"], "int", "bad", {"Root": {"max_size": 4}})


if __name__ == "__main__":
    unittest.main()

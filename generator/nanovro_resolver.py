#!/usr/bin/env python3
"""Generate an allocation-free Avro writer-to-reader resolution plan.

Both schemas use nanovro's normal bounds/options. Outputs the reader types,
private prefixed writer types, and a typed resolved decoder.
"""
import argparse
import json
from pathlib import Path
import re
import struct
import sys

from nanovro_generator import Generator, PRIMITIVES, fullname, identifier


class Layout(Generator):
    def __init__(self, options=None):
        super().__init__(options)
        self.paths, self.nodes, self.meta = {}, {}, {}
        self.runtime_names = set(self.used)

    def descriptor(self, name, kind, capacity=0, count="0", data="0", element="NULL", fields="NULL", field_count=0):
        result = super().descriptor(name, kind, capacity, count, data, element, fields, field_count)
        self.meta[result[1]] = dict(capacity=capacity, element=element)
        return result

    def build(self, schema, path="Root", namespace="", hint="nv_Root", depth=0):
        ct, desc = super().build(schema, path, namespace, hint, depth)
        if desc not in self.nodes:
            s = {"type": schema} if isinstance(schema, str) else schema
            kind = "union" if isinstance(s, list) else s["type"]
            node = dict(kind=kind, ct=ct, desc=desc, schema=s, children=[], full=None, aliases=[])
            if kind in ("record", "enum", "fixed"):
                full = fullname(s["name"], s.get("namespace", namespace))
                namespace = full.rpartition(".")[0]
                node["full"] = full
                node["aliases"] = [fullname(a, namespace) for a in s.get("aliases", [])]
                child_path = full
            else:
                child_path = path
            if kind == "record":
                node["children"] = [self.paths[child_path + "." + f["name"]] for f in s["fields"]]
            elif kind == "union":
                node["children"] = [self.paths[path + f"[{i}]"] for i in range(len(s))]
            elif kind == "array":
                node["children"] = [self.paths[path + "[]"]]
            elif kind == "map":
                node["children"] = [self.paths[path + "{key}"], self.paths[path + "{}"]]
                node["entry"] = self.meta[desc]["element"][1:-5]
            node.update(self.meta.get(desc, {}))
            self.nodes[desc] = node
        self.paths[path] = self.nodes[desc]
        return ct, desc

    def prefix(self, text, prefix):
        names = self.used - self.runtime_names
        return re.sub(r"\bnv_[A-Za-z0-9_]+\b", lambda m: prefix + m[0][3:] if m[0] in names else m[0], text)


def named_match(w, r):
    return w["full"] == r["full"] or w["full"] in r["aliases"]


def field_matches(w, r):
    return w["name"] == r["name"] or w["name"] in r.get("aliases", [])


def compatible(w, r):
    wk, rk = w["kind"], r["kind"]
    if wk == "union":
        return any(compatible(b, r) for b in w["children"])
    if rk == "union":
        return any(compatible(w, b) for b in r["children"])
    if wk != rk:
        return rk in {"int": ("long", "float", "double"), "long": ("float", "double"),
                      "float": ("double",), "string": ("bytes",), "bytes": ("string",)}.get(wk, ())
    if wk in ("record", "enum", "fixed") and not named_match(w, r):
        return False
    if wk == "fixed":
        return w["capacity"] == r["capacity"]
    if wk in ("array", "map"):
        return compatible(w["children"][-1], r["children"][-1])
    if wk == "record":
        for rf, rn in zip(r["schema"]["fields"], r["children"]):
            matches = [wn for wf, wn in zip(w["schema"]["fields"], w["children"]) if field_matches(wf, rf)]
            if len(matches) > 1:
                raise ValueError("ambiguous reader field alias: " + rf["name"])
            if not matches:
                if "default" not in rf:
                    return False
            elif not compatible(matches[0], rn):
                return False
    return True


def identical_wire(w, r):
    if w["kind"] != r["kind"]:
        return False
    kind = w["kind"]
    if kind in ("record", "enum", "fixed") and w["full"] != r["full"]:
        return False
    if kind == "record" and [f["name"] for f in w["schema"]["fields"]] != [f["name"] for f in r["schema"]["fields"]]:
        return False
    if kind == "enum" and w["schema"]["symbols"] != r["schema"]["symbols"]:
        return False
    if kind == "fixed" and w["capacity"] != r["capacity"]:
        return False
    return len(w["children"]) == len(r["children"]) and all(identical_wire(a, b) for a, b in zip(w["children"], r["children"]))


def varint(value):
    u = (value << 1) ^ (value >> 63)
    result = bytearray()
    while u > 127:
        result.append((u & 127) | 128)
        u >>= 7
    result.append(u)
    return bytes(result)


def default_bytes(n, value, check_bounds=True):
    k = n["kind"]
    if k == "null" and value is None:
        return b""
    if k == "boolean" and type(value) is bool:
        return bytes([value])
    if k in ("int", "long") and type(value) is int:
        bits = 32 if k == "int" else 64
        if -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            return varint(value)
    if k in ("float", "double") and type(value) in (int, float):
        return struct.pack("<f" if k == "float" else "<d", value)
    if k in ("string", "bytes", "fixed") and isinstance(value, str):
        b = value.encode("utf-8") if k == "string" else bytes(ord(c) for c in value)
        if (check_bounds and len(b) > n["capacity"]) or (k == "fixed" and len(b) != n["capacity"]):
            raise ValueError("default exceeds storage bound or fixed size")
        return b if k == "fixed" else varint(len(b)) + b
    if k == "enum" and value in n["schema"]["symbols"]:
        return varint(n["schema"]["symbols"].index(value))
    if k == "union":
        for i, branch in enumerate(n["children"]):
            try:
                default_bytes(branch, value, False)
            except (ValueError, TypeError, OverflowError):
                continue
            return varint(i) + default_bytes(branch, value, check_bounds)
    if k == "record" and isinstance(value, dict):
        data = []
        for field, child in zip(n["schema"]["fields"], n["children"]):
            if field["name"] not in value and "default" not in field:
                raise ValueError("missing field in record default: " + field["name"])
            data.append(default_bytes(child, value.get(field["name"], field.get("default")), check_bounds))
        return b"".join(data)
    if k == "array" and isinstance(value, list) and (not check_bounds or len(value) <= n["capacity"]):
        return (varint(len(value)) + b"".join(default_bytes(n["children"][0], v, check_bounds) for v in value) if value else b"") + b"\0"
    if k == "map" and isinstance(value, dict) and (not check_bounds or len(value) <= n["capacity"]):
        return (varint(len(value)) + b"".join(default_bytes(n["children"][0], key, check_bounds) + default_bytes(n["children"][1], v, check_bounds) for key, v in value.items()) if value else b"") + b"\0"
    raise ValueError(f"invalid {k} default: {value!r}")


class Resolver:
    def __init__(self, writer, reader, prefix, pool=None):
        self.writer, self.reader, self.prefix = writer, reader, prefix
        self.pool = pool if pool is not None else dict(lines=[], nodes={}, runs={})
        self.lines, self.serial, self.cache = self.pool["lines"], 0, {}
        self.defaults = {}
        self.used = set(reader.used)

    def emit(self, kind, w=None, r=None, fields=(), count=None, symbols=None, default=None):
        # Do not retain whole schema descriptors for operations that use only
        # offsets, branch tables, or symbol indexes.
        if kind not in ("SKIP", "PROMOTE"):
            w = None
        if kind not in ("DIRECT", "DEFAULT", "ARRAY", "READER_UNION", "PROMOTE"):
            r = None
        wd = self.writer.prefix(w["desc"], self.prefix + "writer_") if w else "NULL"
        rd = r["desc"] if r else "NULL"
        count = len(fields) if count is None else count
        key = (kind, wd, rd, tuple(fields), count, tuple(symbols) if symbols is not None else None, default)
        if key in self.pool["nodes"]:
            return self.pool["nodes"][key]
        name = self.prefix + "plan" + str(self.serial)
        self.serial += 1
        while any(name + suffix in self.used for suffix in ("", "_fields", "_symbols", "_default")):
            name = self.prefix + "plan" + str(self.serial)
            self.serial += 1
        self.used.update(name + suffix for suffix in ("", "_fields", "_symbols", "_default"))
        fp, sp, dp = "NULL", "NULL", "NULL"
        if fields:
            fp = name + "_fields"
            self.lines.append(f"static const nv_resolution_field {fp}[] = {{" + ",".join(f"{{&{p}, {o}}}" for p, o in fields) + "};")
        if symbols is not None:
            sp = name + "_symbols"
            self.lines.append(f"static const int32_t {sp}[] = {{" + ",".join(map(str, symbols)) + "};")
        if default is not None:
            dp = name + "_default"
            self.lines.append(f"static const uint8_t {dp}[] = {{" + ",".join(map(str, default or b"\0")) + "};")
        self.lines.append(f"static const nv_resolution {name} = {{NV_RES_{kind}, {wd}, {rd}, {fp}, {len(fields) if count is None else count}, {sp}, {dp}, {len(default) if default is not None else 0}}};")
        self.pool["nodes"][key] = name
        return name

    def prefix_run(self, w, r):
        """Compact equal-typed, equally spaced fields; otherwise use general plans."""
        if w["kind"] != "record" or r["kind"] != "record" or identical_wire(w, r):
            return None
        wf, rf = w["schema"]["fields"], r["schema"]["fields"]
        if not wf or len(wf) > len(rf) or len(rf) < 2:
            return None
        if any(not field_matches(a, b) for a, b in zip(wf, rf)) or any("default" not in f for f in rf):
            return None
        if len({n["desc"] for n in r["children"]}) != 1:
            return None
        if len({n["desc"] for n in w["children"]}) != 1:
            return None
        data = [default_bytes(n, f["default"]) for n, f in zip(r["children"], rf)]
        if len(set(data)) != 1:
            return None
        value = self.plan(w["children"][0], r["children"][0])
        fallback = self.emit("DEFAULT", r=r["children"][0], default=data[0])
        offset = f"offsetof({r['ct']}, {identifier(rf[0]['name'])})"
        stride = f"sizeof({r['children'][0]['ct']})"
        key = (value, fallback, offset, stride, len(rf))
        if key not in self.pool["runs"]:
            name = self.prefix + "run"
            if name in self.used:
                raise ValueError("resolution run C name collision; choose another --name")
            for i, f in enumerate(rf):
                self.lines.append(f"typedef char {name}_layout{i}[(offsetof({r['ct']}, {identifier(f['name'])}) == {offset} + {i} * {stride}) ? 1 : -1];")
            self.lines.append(f"static const nv_resolution_run {name} = {{&{value}, &{fallback}, {offset}, {stride}, {len(rf)}}};")
            self.pool["runs"][key] = name
        return self.pool["runs"][key], len(wf)

    def plan(self, w, r):
        key = (w["desc"], r["desc"] if r else None)
        if key not in self.cache:
            self.cache[key] = self.build(w, r)
        return self.cache[key]

    def build(self, w, r):
        if r is None:
            return self.emit("SKIP", w)
        if not compatible(w, r):
            return self.emit("ERROR", w, r)
        if identical_wire(w, r):
            return self.emit("DIRECT", r=r)
        wk, rk = w["kind"], r["kind"]
        if wk == "union":
            return self.emit("WRITER_UNION", w, r, [(self.plan(b, r), "0") for b in w["children"]])
        if rk == "union":
            i = next(i for i, b in enumerate(r["children"]) if compatible(w, b))
            return self.emit("READER_UNION", w, r, [(self.plan(w, r["children"][i]), f"offsetof({r['ct']}, value.branch{i})")], count=i)
        if wk != rk:
            return self.emit("PROMOTE", w, r)
        if wk == "record":
            fields, used = [], set()
            for wf, wn in zip(w["schema"]["fields"], w["children"]):
                matches = [(i, rf, rn) for i, (rf, rn) in enumerate(zip(r["schema"]["fields"], r["children"])) if field_matches(wf, rf)]
                if len(matches) > 1:
                    raise ValueError("writer field matches multiple reader aliases: " + wf["name"])
                if matches:
                    i, rf, rn = matches[0]
                    used.add(i)
                    fields.append((self.plan(wn, rn), f"offsetof({r['ct']}, {identifier(rf['name'])})"))
                else:
                    fields.append((self.plan(wn, None), "0"))
            for i, (rf, rn) in enumerate(zip(r["schema"]["fields"], r["children"])):
                if i not in used:
                    data = default_bytes(rn, rf["default"])
                    key = (rn["desc"], data)
                    if key not in self.defaults:
                        self.defaults[key] = self.emit("DEFAULT", r=rn, default=data)
                    fields.append((self.defaults[key], f"offsetof({r['ct']}, {identifier(rf['name'])})"))
            return self.emit("RECORD", r=r, fields=fields)
        if wk == "enum":
            symbols = r["schema"]["symbols"]
            default = r["schema"].get("default")
            if default is not None and default not in symbols:
                raise ValueError("enum default must name a reader symbol")
            mapping = [symbols.index(s) if s in symbols else symbols.index(default) if default is not None else -1 for s in w["schema"]["symbols"]]
            return self.emit("ENUM", r=r, count=len(mapping), symbols=mapping)
        if wk == "array":
            return self.emit("ARRAY", r=r, fields=[(self.plan(w["children"][0], r["children"][0]), "0")])
        if wk == "map":
            entry = self.emit("RECORD", fields=[(self.plan(a, b), f"offsetof({r['entry']}, {member})") for a, b, member in zip(w["children"], r["children"], ("key", "value"))])
            return self.emit("ARRAY", r=r, fields=[(entry, "0")])
        return self.emit("DIRECT", r=r)


def generate(writer_schema, reader_schema, stem, writer_options=None, reader_options=None, reader_name=None):
    identifier(stem)
    for options in (writer_options, reader_options):
        if options is not None and (not isinstance(options, dict) or any(not isinstance(v, dict) for v in options.values())):
            raise ValueError("options must map field paths to option objects")
    wg, rg = Layout(writer_options), Layout(reader_options)
    wh, wc = wg.generate(writer_schema, stem + "_writer")
    reader_name = reader_name or stem + "_reader"
    rh, rc = rg.generate(reader_schema, reader_name)
    w, r = wg.paths["Root"], rg.paths["Root"]
    if not compatible(w, r):
        raise ValueError("incompatible writer and reader schemas (including missing defaults)")
    prefix = "nv_" + stem + "_"
    writer_names = {prefix + "writer_" + name[3:] for name in wg.used - wg.runtime_names}
    if writer_names & rg.used or prefix + "decode" in rg.used:
        raise ValueError("resolution C symbol collision; choose a different --name")
    resolver = Resolver(wg, rg, prefix)
    plan = resolver.plan(w, r)
    guard = "NANOVRO_RESOLVER_" + stem.upper() + "_H"
    header = f'''/* Generated resolution plan. Do not edit. */
#ifndef {guard}
#define {guard}
#include "{reader_name}.h"
#ifdef __cplusplus
extern "C" {{
#endif
bool {prefix}decode(nv_istream *stream, {r['ct']} *value);
#ifdef __cplusplus
}}
#endif
#endif
'''
    source = f'#include "{stem}.h"\n#include "{stem}_writer.h"\n' + "\n".join(resolver.lines)
    source += f"\nbool {prefix}decode(nv_istream *s, {r['ct']} *v) {{ return nv_decode_resolved(s, &{plan}, v); }}\n"
    return {stem + ".h": header, stem + ".c": source, reader_name + ".h": rh, reader_name + ".c": rc,
            stem + "_writer.h": wg.prefix(wh, prefix + "writer_"), stem + "_writer.c": wg.prefix(wc, prefix + "writer_")}


def generate_bundle(writer_schemas, reader_schema, stem, writer_options=None, reader_options=None, reader_name=None):
    """Share a plan pool across supported writers, selected once per connection."""
    identifier(stem)
    if not isinstance(writer_schemas, list) or not writer_schemas:
        raise ValueError("bundle requires a nonempty list of writer schemas")
    for options in (writer_options, reader_options):
        if options is not None and (not isinstance(options, dict) or any(not isinstance(v, dict) for v in options.values())):
            raise ValueError("options must map field paths to option objects")
    rg = Layout(reader_options)
    reader_name = reader_name or stem + "_reader"
    rh, rc = rg.generate(reader_schema, reader_name)
    r = rg.paths["Root"]
    prefix = "nv_" + stem + "_"
    for suffix in ("decoder", "select"):
        if prefix + suffix in rg.used:
            raise ValueError("bundle C symbol collision; choose another --name")
    pool = dict(lines=[], nodes={}, runs={})
    files = {reader_name + ".h": rh, reader_name + ".c": rc}
    includes, functions = [], []
    for i, schema in enumerate(writer_schemas):
        wg = Layout(writer_options)
        writer_stem = f"{stem}_writer{i}"
        wh, wc = wg.generate(schema, writer_stem)
        w = wg.paths["Root"]
        if not compatible(w, r):
            raise ValueError(f"incompatible writer at bundle index {i}")
        child_prefix = prefix + f"v{i}_"
        if {child_prefix + "writer_" + name[3:] for name in wg.used - wg.runtime_names} & rg.used:
            raise ValueError("bundle writer C symbol collision; choose another --name")
        files[writer_stem + ".h"] = wg.prefix(wh, child_prefix + "writer_")
        files[writer_stem + ".c"] = wg.prefix(wc, child_prefix + "writer_")
        includes.append(f'#include "{writer_stem}.h"')
        resolver = Resolver(wg, rg, child_prefix, pool)
        run = resolver.prefix_run(w, r)
        call = f"nv_decode_resolved_run(s, &{run[0]}, {run[1]}, v)" if run else f"nv_decode_resolved(s, &{resolver.plan(w, r)}, v)"
        function = prefix + f"decode{i}"
        if function in rg.used:
            raise ValueError("bundle decoder C symbol collision; choose another --name")
        functions.append(f"static bool {function}(nv_istream *s, {r['ct']} *v) {{ return {call}; }}")
    guard = "NANOVRO_BUNDLE_" + stem.upper() + "_H"
    files[stem + ".h"] = f'''/* Generated shared resolution bundle. Do not edit. */
#ifndef {guard}
#define {guard}
#include "{reader_name}.h"
#define NV_{stem.upper()}_WRITER_COUNT {len(writer_schemas)}
#ifdef __cplusplus
extern "C" {{
#endif
typedef bool (*{prefix}decoder)(nv_istream *, {r['ct']} *);
/* Manifest order defines writer indexes. NULL means unsupported index. */
{prefix}decoder {prefix}select(size_t writer_index);
#ifdef __cplusplus
}}
#endif
#endif
'''
    files[stem + ".c"] = '\n'.join([f'#include "{stem}.h"', *includes, *pool["lines"], *functions,
        f"static const {prefix}decoder {prefix}decoders[] = {{" + ",".join(prefix + f"decode{i}" for i in range(len(writer_schemas))) + "};",
        f"{prefix}decoder {prefix}select(size_t i) {{ return i < {len(writer_schemas)} ? {prefix}decoders[i] : NULL; }}", ""])
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("writer", type=Path)
    parser.add_argument("reader", type=Path)
    parser.add_argument("--name", required=True, help="unique C symbol and output-file stem for this pair")
    parser.add_argument("-o", "--output", type=Path, default=Path("."))
    parser.add_argument("--writer-options", type=Path)
    parser.add_argument("--reader-options", type=Path)
    parser.add_argument("--reader-name", help="shared reader output stem when generating multiple writer versions")
    parser.add_argument("--bundle", action="store_true", help="writer argument is a JSON array of schema file paths, relative to that manifest")
    args = parser.parse_args()
    try:
        read = lambda p: json.loads(p.read_text(encoding="utf-8")) if p else None
        writer = read(args.writer)
        generator = generate
        if args.bundle:
            if not isinstance(writer, list) or not writer or any(not isinstance(p, str) for p in writer):
                raise ValueError("bundle manifest must be a nonempty array of schema paths")
            writer = [read(args.writer.parent / p) for p in writer]
            generator = generate_bundle
        files = generator(writer, read(args.reader), args.name, read(args.writer_options), read(args.reader_options), args.reader_name)
        args.output.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (args.output / name).write_text(text, encoding="utf-8")
    except (ValueError, KeyError, TypeError, OSError, RecursionError, OverflowError) as error:
        parser.exit(2, f"nanovro resolver: {error}\n")


if __name__ == "__main__":
    main()

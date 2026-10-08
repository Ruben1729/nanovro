#!/usr/bin/env python3
"""Generate bounded C99 data types and descriptors from an Avro schema."""
import argparse
import json
from pathlib import Path
import re
import sys


PRIMITIVES = {
    "null": "uint8_t", "boolean": "bool", "int": "int32_t",
    "long": "int64_t", "float": "float", "double": "double",
}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid name: {value!r}")
    # Prefix all names to avoid both C/C++ keywords and reserved identifiers.
    return "nv_" + value


def fullname(name, namespace):
    if not isinstance(name, str):
        raise ValueError("named types need a name")
    parts = name.split(".")
    for part in parts:
        identifier(part)
    if len(parts) > 1:
        return name
    if namespace:
        for part in namespace.split("."):
            identifier(part)
        return namespace + "." + name
    return name


class Generator:
    def __init__(self, options=None):
        self.options = options or {}
        self.named = {}
        self.active = set()
        self.used = {f"nv_{p}_type" for p in PRIMITIVES} | {
            "nv_type", "nv_field", "nv_error", "nv_kind", "nv_ostream", "nv_istream",
            "nv_write_fn", "nv_read_fn", "nv_encode", "nv_decode", "nv_error_string",
            "nv_ostream_buffer", "nv_ostream_callback", "nv_ostream_sizing",
            "nv_istream_buffer", "nv_istream_callback",
            "nv_resolution", "nv_resolution_kind", "nv_resolution_field", "nv_decode_resolved",
            "nv_resolution_run", "nv_decode_resolved_run",
        }
        self.declarations = []
        self.definitions = []
        self.descriptors = []

    def reserve(self, name):
        if name in self.used:
            raise ValueError(f"C name collision: {name}")
        self.used.add(name)
        return name

    def reserve_type(self, name):
        self.reserve(name)
        self.reserve(name + "_type")
        self.reserve(name + "_fields")
        return name

    def bound(self, schema, path, key):
        local = self.options.get(path, {})
        value = local.get(key, schema.get("nanovro." + key))
        if type(value) is not int or not 0 <= value <= 2147483647:
            raise ValueError(f"{path}: specify {key} (0..2147483647) in options or nanovro.{key}")
        return value

    def descriptor(self, name, kind, capacity=0, count="0", data="0", element="NULL", fields="NULL", field_count=0):
        self.descriptors.append(name)
        self.definitions.append(
            f"const nv_type {name}_type = {{NV_{kind}, sizeof({name}), {capacity}, {count}, {data}, {element}, {fields}, {field_count}}};"
        )
        return name, f"&{name}_type"

    def fields(self, name, members):
        if not members:
            return "NULL"
        rows = [f"    {{{desc}, offsetof({name}, {field})}}" for field, desc in members]
        self.definitions.append(f"static const nv_field {name}_fields[] = {{\n" + ",\n".join(rows) + "\n};")
        return name + "_fields"

    def build(self, schema, path="Root", namespace="", hint="nv_Root", depth=0):
        if depth > 60:
            raise ValueError("schema nesting exceeds generator limit (60)")
        if isinstance(schema, str):
            if schema in PRIMITIVES:
                return PRIMITIVES[schema], f"&nv_{schema}_type"
            if schema in ("string", "bytes"):
                return self.build({"type": schema}, path, namespace, hint, depth)
            name = fullname(schema, namespace)
            if name in self.active:
                raise ValueError(f"recursive type {name} cannot use bounded inline storage")
            if name not in self.named:
                raise ValueError(f"undefined type {name}")
            return self.named[name]
        if isinstance(schema, list):
            if not schema:
                raise ValueError("union must have branches")
            name = self.reserve_type(hint)
            branches, seen = [], set()
            for i, branch in enumerate(schema):
                if isinstance(branch, list):
                    raise ValueError("union cannot directly contain a union")
                ctype, desc = self.build(branch, path + f"[{i}]", namespace, name + f"_branch{i}", depth + 1)
                if isinstance(branch, dict):
                    kind = branch.get("type")
                    key = fullname(branch.get("name"), branch.get("namespace", namespace)) if kind in ("record", "enum", "fixed") else kind
                else:
                    key = branch if branch in (*PRIMITIVES, "string", "bytes") else fullname(branch, namespace)
                if key in seen:
                    raise ValueError(f"duplicate union branch: {key}")
                seen.add(key)
                branches.append((ctype, desc))
            members = "\n".join(f"        {ct} branch{i};" for i, (ct, _) in enumerate(branches))
            self.declarations.append(f"typedef struct {{\n    size_t tag;\n    union {{\n{members}\n    }} value;\n}} {name};")
            fields = self.fields(name, [(f"value.branch{i}", desc) for i, (_, desc) in enumerate(branches)])
            return self.descriptor(name, "UNION", count=f"offsetof({name}, tag)", fields=fields, field_count=len(branches))
        if not isinstance(schema, dict) or "type" not in schema:
            raise ValueError(f"{path}: expected Avro schema")
        kind = schema["type"]
        if not isinstance(kind, str):
            raise ValueError(f"{path}: schema object type must be a string")
        if kind in PRIMITIVES:
            return self.build(kind, path, namespace, hint, depth)
        if kind not in ("record", "enum", "fixed", "string", "bytes", "array", "map"):
            return self.build(kind, path, namespace, hint, depth)

        named = kind in ("record", "enum", "fixed")
        if named:
            full = fullname(schema.get("name"), schema.get("namespace", namespace))
            if full in self.named or full in self.active:
                raise ValueError(f"duplicate type: {full}")
            if full.split(".")[-1] in (*PRIMITIVES, "bytes", "string"):
                raise ValueError("primitive names cannot be redefined")
            namespace = full.rpartition(".")[0]
            name = self.reserve_type(identifier(full.replace(".", "_")))
            path = full
            self.active.add(full)
        else:
            name = self.reserve_type(hint)

        if kind == "record":
            fields = schema.get("fields")
            if not isinstance(fields, list):
                raise ValueError(f"{path}: record needs fields")
            declarations, members, seen = [], [], set()
            for field in fields:
                fname = field["name"]
                cname = identifier(fname)
                if fname in seen:
                    raise ValueError(f"{path}: duplicate field {fname}")
                seen.add(fname)
                ct, desc = self.build(field["type"], path + "." + fname, namespace, name + "_" + fname, depth + 1)
                declarations.append(f"    {ct} {cname};")
                members.append((cname, desc))
            if not declarations:
                declarations = ["    uint8_t unused;"]
            self.declarations.append("typedef struct {\n" + "\n".join(declarations) + f"\n}} {name};")
            result = self.descriptor(name, "RECORD", fields=self.fields(name, members), field_count=len(members))
        elif kind == "enum":
            symbols = schema.get("symbols")
            if not isinstance(symbols, list) or not symbols or len(symbols) != len(set(symbols)):
                raise ValueError(f"{path}: enum needs unique symbols")
            self.declarations.append(f"typedef int32_t {name};")
            for i, symbol in enumerate(symbols):
                identifier(symbol)
                constant = self.reserve(name + "_" + symbol)
                self.declarations.append(f"#define {constant} INT32_C({i})")
            result = self.descriptor(name, "ENUM", capacity=len(symbols))
        elif kind == "fixed":
            size = schema.get("size")
            if type(size) is not int or not 0 <= size <= 2147483647:
                raise ValueError(f"{path}: invalid fixed size")
            self.declarations.append(f"typedef struct {{ uint8_t data[{max(1, size)}]; }} {name};")
            result = self.descriptor(name, "FIXED", capacity=size, data=f"offsetof({name}, data)")
        elif kind in ("string", "bytes"):
            cap = self.bound(schema, path, "max_size")
            ct = "char" if kind == "string" else "uint8_t"
            storage = max(1, cap + (kind == "string"))
            self.declarations.append(f"typedef struct {{ size_t size; {ct} data[{storage}]; }} {name};")
            result = self.descriptor(name, kind.upper(), capacity=cap, count=f"offsetof({name}, size)", data=f"offsetof({name}, data)")
        else:
            cap = self.bound(schema, path, "max_count")
            if kind == "array":
                ct, desc = self.build(schema["items"], path + "[]", namespace, name + "_item", depth + 1)
            else:
                key_cap = self.bound(schema, path, "max_key_size")
                kt, kd = self.build({"type": "string", "nanovro.max_size": key_cap}, path + "{key}", namespace, name + "_key", depth + 1)
                vt, vd = self.build(schema["values"], path + "{}", namespace, name + "_value", depth + 1)
                ct = self.reserve_type(name + "_entry")
                self.declarations.append(f"typedef struct {{ {kt} key; {vt} value; }} {ct};")
                _, desc = self.descriptor(ct, "RECORD", fields=self.fields(ct, [("key", kd), ("value", vd)]), field_count=2)
            self.declarations.append(f"typedef struct {{ size_t count; {ct} items[{max(1, cap)}]; }} {name};")
            result = self.descriptor(name, "ARRAY", capacity=cap, count=f"offsetof({name}, count)", data=f"offsetof({name}, items)", element=desc)

        if named:
            self.active.remove(full)
            self.named[full] = result
        return result

    def generate(self, schema, stem):
        identifier(stem)
        self.reserve(f"nv_{stem}_encode")
        self.reserve(f"nv_{stem}_decode")
        root, desc = self.build(schema, hint="nv_" + stem + "_root")
        guard = "NANOVRO_GENERATED_" + stem.upper() + "_H"
        header = ["/* Generated by nanovro_generator.py. Do not edit. */", f"#ifndef {guard}", f"#define {guard}", '#include "nanovro.h"', "", *self.declarations, "", '#ifdef __cplusplus\nextern "C" {\n#endif']
        header += [f"extern const nv_type {name}_type;" for name in self.descriptors]
        header += [f"bool nv_{stem}_encode(nv_ostream *stream, const {root} *value);", f"bool nv_{stem}_decode(nv_istream *stream, {root} *value);", '#ifdef __cplusplus\n}\n#endif', "#endif", ""]
        source = ["/* Generated by nanovro_generator.py. Do not edit. */", f'#include "{stem}.h"', "", *self.definitions, f"bool nv_{stem}_encode(nv_ostream *s, const {root} *v) {{ return nv_encode(s, {desc}, v); }}", f"bool nv_{stem}_decode(nv_istream *s, {root} *v) {{ return nv_decode(s, {desc}, v); }}", ""]
        return "\n".join(header), "\n\n".join(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schema", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("."))
    parser.add_argument("--options", type=Path, help="JSON map of field paths to storage bounds")
    parser.add_argument("--name", help="output filename stem (default: schema filename)")
    args = parser.parse_args()
    try:
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
        options = json.loads(args.options.read_text(encoding="utf-8")) if args.options else {}
        if not isinstance(options, dict) or any(not isinstance(v, dict) for v in options.values()):
            raise ValueError("options must map field paths to option objects")
        stem = args.name or args.schema.stem
        header, source = Generator(options).generate(schema, stem)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / (stem + ".h")).write_text(header, encoding="utf-8")
        (args.output / (stem + ".c")).write_text(source, encoding="utf-8")
    except (ValueError, KeyError, TypeError, OSError, RecursionError) as error:
        parser.exit(2, f"nanovro: {error}\n")


if __name__ == "__main__":
    main()

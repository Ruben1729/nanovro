# nanovro

Licensed under the [MIT License](LICENSE).

An embedded C99 implementation of **Avro binary datum encoding**, using the
schema-to-C workflow familiar from nanopb. A Python generator turns `.avsc`
schemas into C structs and constant descriptors. The C runtime needs no heap,
schema parser, Python, or third-party library on the target.

This is an initial implementation, not a complete replacement for Apache Avro.
The ordinary decoder uses the writer's schema and field order. Generated
resolution plans also support decoding known writer versions into a different
reader schema, without runtime schema parsing.

This project is experimental. Before version 1.0, APIs and generated code may
change incompatibly. Pin the library and generator to the same version, and
regenerate your schema code when upgrading.

## Supported types

| Avro | Generated C storage |
| --- | --- |
| null | byte placeholder, zero bytes on wire |
| boolean | `bool` |
| int / long | `int32_t` / `int64_t` |
| float / double | IEEE 754 `float` / `double` |
| string | explicit byte length + bounded UTF-8 buffer + trailing NUL |
| bytes | explicit byte length + bounded byte buffer |
| fixed | inline byte buffer |
| enum | `int32_t` + generated symbol constants |
| record | struct with fields in schema order |
| array | count + bounded inline elements |
| map | count + bounded inline key/value entries |
| union | zero-based branch tag + C union |

Named types and namespaces are supported. Recursive schemas are rejected because
inline, statically bounded storage cannot represent them. Logical types use their
underlying Avro type; the application handles their meaning. Container files,
compression, RPC, JSON encoding, and single-object framing are not implemented.

## Schema resolution

Generate a decoder for each supported writer/reader pair:

```sh
python generator/nanovro_resolver.py old.avsc current.avsc --name old_to_current -o generated
```

Compile the generated `old_to_current.c`, `old_to_current_reader.c`, and
`old_to_current_writer.c` with the runtime. Include `old_to_current.h` and call
`nv_old_to_current_decode(&stream, &reader_value)`. The destination type is the
generated reader struct.
All schemas use the usual explicit storage bounds;
`--writer-options` and `--reader-options` accept the existing options format.
Use the same `--reader-name current` for multiple plans sharing one reader;
compile the shared `current.c` only once. A shared reader name must always refer
to the same reader schema and options. Writer symbols are prefixed per plan.

Plans support record/field reader aliases, reordered and ignored writer fields,
defaults for missing reader fields, numeric widening, string/bytes promotion,
fixed-name/size matching, enum symbol remapping and reader enum defaults,
arrays, maps, and union branch selection. Incompatible schema pairs fail during
generation. A writer union can contain unsupported branches: selecting one at
runtime, or an unmapped enum symbol without a reader default, produces
`NV_ERR_SCHEMA`. Reader capacities, stream bounds and nesting limits still apply.
Defaults must fit reader storage and are decoded from constant generated data.

Compatible unchanged wire layouts use the ordinary optimized decoder, including
bulk float/double array copies. Type-changing arrays convert each element.
Plans use constant storage; the target linker determines whether constants stay
in flash or are copied into RAM (Teensy normally copies these tables into RAM).
There is no allocation or runtime schema matching. Only
linked plans need to be retained. The existing `nv_decode` API remains available
for same-schema decoding without a resolution-plan call.

The application must identify the writer schema (for example, a version selected
by its protocol) and select the correct plan. Resolution does not add a schema ID
to datum bytes and does not discover arbitrary schemas received over the wire.
Recursive schemas remain unsupported; logical-type compatibility is checked only
at the underlying Avro type level. Ignored strings/bytes and sized array/map
blocks are skipped without semantic validation of their contents; framing and
stream limits are checked. Retained strings are validated as UTF-8. Decoding is
not transactional; discard partial output after failure and require full input
consumption when your protocol carries one datum per frame.

### Shared plans with connection-time selection

For multiple writer versions, generate them together to share resolution nodes
and defaults. A writer manifest lists schema paths relative to its own directory:

```json
["v1.avsc", "v2.avsc", "v3.avsc"]
```

```sh
python generator/nanovro_resolver.py writers.json current.avsc --bundle --name fleet -o generated
```

Compile `fleet.c`, `fleet_reader.c`, and the generated `fleet_writer*.c` sources.
After your connection handshake identifies the writer, call
`nv_fleet_select(writer_index)` once and retain the returned typed decoder
function pointer. Manifest order defines indexes. An unsupported index returns
`NULL`; reject it before reading messages. Reuse that decoder for each datum on
the connection. No per-message schema ID or 10-byte header is added by this API.

Bundles intern identical plan nodes across versions and omit schema descriptors
unused by each operation. Homogeneous record prefixes with identical defaults
use one shared repeated-field plan, with the number of writer fields supplied by
the selected decoder. The generator emits compile-time layout checks. Reordered,
heterogeneous and other irregular schemas retain the general resolution path.
Shared constants remain in the target's ordinary data placement; no flash-only
attributes are added. A bundle's selector retains all its listed writer versions,
so include only the versions the device supports.

## Generate and use

Python 3.9+ is needed only to generate code; the generator uses the standard library.

Save this schema as `telemetry.avsc` in your project:

```json
{
  "type": "record",
  "name": "Telemetry",
  "fields": [
    {"name": "sequence", "type": "long"},
    {"name": "temperature", "type": "float"},
    {"name": "active", "type": "boolean"},
    {"name": "label", "type": {"type": "string", "nanovro.max_size": 24}},
    {"name": "samples", "type": {"type": "array", "items": "int", "nanovro.max_count": 16}}
  ]
}
```

```sh
python generator/nanovro_generator.py telemetry.avsc -o generated
```

Compile `src/nanovro.c` and `generated/telemetry.c`, with `include/` and `generated/`
on the header search path.

```c
#include "telemetry.h"
#include <string.h>

nv_Telemetry message = {0};
uint8_t wire[256];
message.nv_sequence = 42;
message.nv_label.size = 6;
memcpy(message.nv_label.data, "teensy", 6);

nv_ostream out = nv_ostream_buffer(wire, sizeof(wire));
if (!nv_telemetry_encode(&out, &message)) {
    /* Handle out.error; output may be partial. */
}

nv_Telemetry received = {0};
nv_istream in = nv_istream_buffer(wire, out.position);
if (!nv_telemetry_decode(&in, &received)) {
    /* Handle in.error; received may be partially modified. */
}
/* For one framed datum, also require in.position == out.position. */
```

All Avro field names get an `nv_` prefix in C, including names that are C/C++
keywords. Named C types use `nv_` followed by the Avro fullname with dots replaced
by underscores. Anonymous helper types derive from their containing field.
The generator rejects detected name collisions; rename conflicting schema names.
An enum `Mode` with symbol `ON` produces `nv_Mode_ON`.

Strings and bytes use `.size` and `.data`; arrays/maps use `.count` and `.items`.
Map items have `.key` and `.value`. Union values use `.tag` and
`.value.branch0`, `.value.branch1`, etc., in schema order. A zero-initialized union
selects branch 0; it does not apply the schema default. Strings may contain embedded
NULs: `.size`, not `strlen`, is authoritative. Decoding also writes a convenience
NUL after the string. The caller must provide unique map keys.

## Storage bounds

Every string/bytes/array/map needs an explicit capacity. Set custom schema metadata:

```json
{"type":"string", "nanovro.max_size":24}
```

Or keep standard schemas separate from storage decisions using `--options bounds.json`:

```json
{
  "Telemetry.label": {"max_size": 24},
  "Telemetry.samples": {"max_count": 16}
}
```

`max_size` is a byte count, excluding the string's extra terminator.
Arrays/maps use `max_count`; maps also require `max_key_size`.
Options override metadata. Paths start at the named record's Avro fullname,
then `.field`, `[]` for array items, `{}` for map values, and `[0]` for union
branches. Nested named records start their own fullname path. Anonymous roots
use `Root`. Zero capacities are supported; placeholder storage stays valid C99.

## Streams and failures

`nv_encode` / `nv_decode` also accept generated descriptors directly.
`nv_ostream_sizing()` computes actual encoded size without an output buffer.
`nv_ostream_callback()` / `nv_istream_callback()` support application-owned I/O;
callbacks transfer exactly the requested bytes or fail. They cannot suspend or
resume mid-call. A stream's `limit` bounds total transferred bytes, including
callback streams. Encoding emits one positive collection block; decoding accepts
multiple blocks and validates negative blocks' byte lengths.

Errors are sticky and available in `.error` and `nv_error_string()`. Capacity
overflow, truncated input, invalid enum/union indexes, invalid booleans, overflowing
integers, negative lengths and invalid UTF-8 fail explicitly. Neither a failed
operation nor a failed callback is transactional: discard partial results and
create a new stream before retrying. Buffers must not overlap message storage.
Descriptors are trusted generated code and must not come from untrusted input.

There is no `malloc`/`free` in the runtime. Recursive traversal uses stack bounded
by schema depth (`NV_MAX_DEPTH`, default 64), in addition to the caller-owned
message and stream buffers. Very large capacities still require corresponding
RAM and processing time; choose them for the target. Platforms need 8-bit bytes,
64-bit integers, IEEE 754 32-bit float and 64-bit double. Teensy 4.1 meets these
requirements; platforms with 32-bit `double` are rejected at compile time.

Primitive arrays use specialized integer loops and, on supported little-endian
targets, bulk copies for contiguous float/double elements. Other layouts use the
portable scalar path; define `NV_DISABLE_ARRAY_BULK_COPY` to force that fallback.
Both paths preserve Avro block framing, limits, and floating-point bit patterns.
Stream callbacks may receive an entire floating-point array block in one call.
UTF-8 validation skips ASCII in bounded four-byte chunks and checks multibyte
sequences strictly, including overlong encodings, surrogates, and range limits.
These optimizations change neither generated structs nor wire bytes.

## Build and integrate

Build the C99 runtime without Python or external dependencies:

```sh
cmake -S . -B build
cmake --build build
```

In a CMake application, use `add_subdirectory(path/to/nanovro)` and link your
application target with `target_link_libraries(my_app PRIVATE nanovro)`.
Add your generated `.c` files to the application target and their directory to
its include paths. Generate these files beforehand using Python 3.9+.

For other build systems, compile `src/nanovro.c` alongside your generated `.c`
files and add `include/` and your generated directory to the header search path.
For PlatformIO, add this directory as a local `symlink://` library dependency;
put generated files in the consuming project's source directory.

## Runnable examples

The [telemetry example](examples/telemetry/main.c) generates a schema and performs
an encode/decode round trip. The [schema-resolution example](examples/schema_resolution/main.c)
demonstrates aliases, integer promotion, skipped fields, reader defaults, and
selecting a decoder for a connection.

Build and run both with CMake, a C99 compiler, and Python 3.9+:

```sh
cmake -S . -B build -DNANOVRO_BUILD_EXAMPLES=ON
cmake --build build
ctest --test-dir build -R "telemetry_example|resolution_example" --output-on-failure
```

The executables are `telemetry_example` and `resolution_example` in the build
directory (`.exe` on Windows; inside `Release/` for a Release multi-configuration
build). Examples need no third-party Python packages and are disabled by default.
Enable `NANOVRO_BUILD_TESTS` as well to run examples alongside regression tests.

## Host regression tests

Tests run on your computer; no board or embedded toolchain is needed. They are
disabled by default and are not linked into applications using the library.
The core suite requires CMake, a C99 compiler, and Python 3.9+:

```sh
cmake -S . -B build -DNANOVRO_BUILD_TESTS=ON
cmake --build build
ctest --test-dir build --output-on-failure
```

It checks runtime encoding/decoding, errors and bounds, UTF-8 validation,
optimized and portable array paths, and generator schema validation. C tests
use explicit checks that remain active even in Release builds.

For the full suite, install the Apache Avro reference implementation and enable
interoperability tests (GCC or a GCC-compatible Clang is required):

```sh
python -m pip install -r tests/requirements.txt
cmake -S . -B build -DNANOVRO_BUILD_TESTS=ON -DNANOVRO_TEST_INTEROP=ON
cmake --build build
ctest --test-dir build --output-on-failure
```

These additional tests generate and compile C code, compare wire data and values
against Apache Avro, and exercise schema resolution, shared bundles, malformed
inputs, truncation, and capacity failures. Python dependencies are for tests only.
To select a virtual environment, pass `-DPython3_EXECUTABLE=/path/to/python`
to CMake, using the same interpreter that installed the requirements.

On Windows with MSYS2/MinGW, put `C:\msys64\mingw64\bin` on `PATH` and use
`-G "MinGW Makefiles"` when first configuring the build directory. With a
multi-configuration generator, build with `--config Release` and run CTest with
`-C Release`.

## Performance note: Teensy 4.1

Local measurements suggest nanovro **can be faster for some workloads**, not
that it is generally faster than nanopb. Representative decode timings on a
Teensy 4.1 at 600 MHz, using ARM GCC 11.3.1 with `-O2`, no LTO, and nanopb
0.4.9.1:

| Workload | nanovro | nanopb | Comparison |
| --- | ---: | ---: | --- |
| Dense record, 16 small integers | 2.357 µs | 15.233 µs | nanovro ~6.5× faster |
| Array, 256 float32 values | 2.978 µs | 33.043 µs | nanovro ~11.1× faster |
| Sparse record, 64 fields all absent/default | 14.177 µs | 12.767 µs | nanovro ~11% slower |
| Wide positive integers, compared with protobuf fixed32 | 15.617 µs | 9.362 µs | nanovro ~67% slower |

These are warmed buffer API calls measured using median batch cycles, including
benchmark harness overhead, rather than end-to-end application or USB latency.
Both codecs decoded equivalent values, but their wire representations differ:
protobuf can omit default fields and use fixed-width integers. The float-array
case benefits from nanovro's bulk-copy path. Results depend on schema, values,
compiler settings, and target; benchmark your own workload before choosing a
codec. These are historical local results; the benchmark harness and raw data
are archived separately and are not included in this minimal distribution.

Wire-format reference: [Apache Avro specification](https://avro.apache.org/docs/1.12.0/specification/).

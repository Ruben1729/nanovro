#include "nanovro.h"
#include <stdio.h>
#include <string.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); return 1; } } while (0)

typedef struct { size_t count; unsigned char data[64]; } storage;
typedef struct { unsigned char data[128]; size_t pos; bool fail; } transport;
static bool write_cb(void *ctx, const uint8_t *p, size_t n) {
    transport *t = ctx;
    if ((t->fail && t->pos) || n > sizeof(t->data) - t->pos) return false;
    memcpy(t->data + t->pos, p, n); t->pos += n; return true;
}
static bool read_cb(void *ctx, uint8_t *p, size_t n) {
    transport *t = ctx;
    if ((t->fail && t->pos) || n > sizeof(t->data) - t->pos) return false;
    memcpy(p, t->data + t->pos, n); t->pos += n; return true;
}
static int arrays(void) {
    const nv_type *elements[] = {&nv_float_type, &nv_double_type, &nv_int_type, &nv_long_type};
    const uint32_t patterns32[] = {0, UINT32_C(0x80000000), UINT32_C(0x7f800000),
        UINT32_C(0x7fc12345), UINT32_C(0xff800000), 1, UINT32_C(0x00800000), UINT32_MAX};
    const uint64_t patterns64[] = {0, UINT64_C(0x8000000000000000), UINT64_C(0x7ff0000000000000),
        UINT64_C(0x7ff8123456789abc), UINT64_C(0xfff0000000000000), 1, UINT64_C(0x0010000000000000), UINT64_MAX};
    size_t kind, j, k;
    for (kind = 0; kind < 4; ++kind) {
        const nv_type *e = elements[kind];
        nv_type a = {NV_ARRAY, sizeof(storage), 8, offsetof(storage, count), offsetof(storage, data), e, NULL, 0};
        storage value = {8, {0}}, decoded = {0, {0}};
        unsigned char wire[128], expected[128];
        nv_ostream out = nv_ostream_buffer(wire, sizeof(wire));
        nv_ostream scalar = nv_ostream_buffer(expected, sizeof(expected));
        nv_istream in;
        int64_t count = 8, zero = 0;
        transport t = {{0}, 0, false};
        for (j = 0; j < 8; ++j) {
            if (e->size == 4) memcpy(value.data + j * 4, &patterns32[j], 4);
            else memcpy(value.data + j * 8, &patterns64[j], 8);
        }
        CHECK(nv_encode(&scalar, &nv_long_type, &count));
        for (j = 0; j < 8; ++j) CHECK(nv_encode(&scalar, e, value.data + j * e->size));
        CHECK(nv_encode(&scalar, &nv_long_type, &zero));
        CHECK(nv_encode(&out, &a, &value));
        CHECK(out.position == scalar.position && !memcmp(wire, expected, out.position));
        in = nv_istream_buffer(wire, out.position);
        CHECK(nv_decode(&in, &a, &decoded));
        CHECK(decoded.count == 8 && !memcmp(value.data, decoded.data, 8 * e->size));
        for (j = 0; j < out.position; ++j) {
            nv_istream short_in = nv_istream_buffer(wire, j);
            nv_ostream short_out = nv_ostream_buffer(expected, j);
            CHECK(!nv_decode(&short_in, &a, &decoded) && short_in.error == NV_ERR_LIMIT);
            CHECK(!nv_encode(&short_out, &a, &value) && short_out.error == NV_ERR_LIMIT);
        }
        scalar = nv_ostream_sizing(); CHECK(nv_encode(&scalar, &a, &value)); CHECK(scalar.position == out.position);
        scalar = nv_ostream_callback(write_cb, &t, sizeof(t.data));
        CHECK(nv_encode(&scalar, &a, &value)); CHECK(!memcmp(t.data, wire, out.position));
        t.pos = 0; in = nv_istream_callback(read_cb, &t, out.position);
        CHECK(nv_decode(&in, &a, &decoded)); CHECK(!memcmp(value.data, decoded.data, 8 * e->size));
        t.pos = 0; t.fail = true;
        in = nv_istream_callback(read_cb, &t, out.position);
        CHECK(!nv_decode(&in, &a, &decoded) && in.error == NV_ERR_IO);
        CHECK(in.position == 1);
        t.pos = 0; scalar = nv_ostream_callback(write_cb, &t, sizeof(t.data));
        CHECK(!nv_encode(&scalar, &a, &value) && scalar.error == NV_ERR_IO);
        CHECK(scalar.position == 1);
        /* Two sized blocks; exercise limits independently of native endianness. */
        scalar = nv_ostream_buffer(expected, sizeof(expected));
        for (k = 0; k < 2; ++k) {
            nv_ostream size = nv_ostream_sizing();
            int64_t negative = -4, bytes;
            for (j = 0; j < 4; ++j) CHECK(nv_encode(&size, e, value.data + (k * 4 + j) * e->size));
            bytes = (int64_t)size.position;
            CHECK(nv_encode(&scalar, &nv_long_type, &negative)); CHECK(nv_encode(&scalar, &nv_long_type, &bytes));
            for (j = 0; j < 4; ++j) CHECK(nv_encode(&scalar, e, value.data + (k * 4 + j) * e->size));
        }
        CHECK(nv_encode(&scalar, &nv_long_type, &zero));
        in = nv_istream_buffer(expected, scalar.position);
        CHECK(nv_decode(&in, &a, &decoded)); CHECK(!memcmp(value.data, decoded.data, 8 * e->size));
        expected[1] -= 2; /* First block is one byte too short. */
        in = nv_istream_buffer(expected, scalar.position);
        CHECK(!nv_decode(&in, &a, &decoded) && in.error == NV_ERR_LIMIT);
        CHECK(in.limit == scalar.position);
        if (e->kind == NV_INT) {
            int64_t one = 1, overflow = (int64_t)INT32_MAX + 1;
            scalar = nv_ostream_buffer(expected, sizeof(expected));
            CHECK(nv_encode(&scalar, &nv_long_type, &one));
            CHECK(nv_encode(&scalar, &nv_long_type, &overflow));
            CHECK(nv_encode(&scalar, &nv_long_type, &zero));
            in = nv_istream_buffer(expected, scalar.position);
            CHECK(!nv_decode(&in, &a, &decoded) && in.error == NV_ERR_INVALID);
        }
    }
    return 0;
}
static int array_depth(void) {
    nv_type nodes[NV_MAX_DEPTH], array = {NV_ARRAY, sizeof(storage), 8, offsetof(storage, count), offsetof(storage, data), &nv_float_type, NULL, 0};
    nv_field fields[NV_MAX_DEPTH];
    storage value = {1, {0}};
    const unsigned char wire[] = {2, 0, 0, 0, 0, 0};
    nv_ostream out = nv_ostream_sizing();
    nv_istream in = nv_istream_buffer(wire, sizeof(wire));
    size_t i;
    for (i = 0; i < NV_MAX_DEPTH; ++i) {
        const nv_type node = {NV_RECORD, sizeof(storage), 0, 0, 0, NULL, &fields[i], 1};
        nodes[i] = node; fields[i].offset = 0;
        fields[i].type = i + 1 < NV_MAX_DEPTH ? &nodes[i + 1] : &array;
    }
    CHECK(!nv_encode(&out, nodes, &value) && out.error == NV_ERR_DEPTH);
    CHECK(!nv_decode(&in, nodes, &value) && in.error == NV_ERR_DEPTH);
    value.count = 0; out = nv_ostream_sizing();
    CHECK(nv_encode(&out, nodes, &value));
    return 0;
}
/* Independent code-point reconstruction oracle for strict UTF-8. */
static bool reference_utf8(const unsigned char *p, size_t n) {
    size_t i = 0;
    while (i < n) {
        uint32_t cp, min; unsigned extra;
        unsigned char c = p[i++];
        if (c < 128) continue;
        if (c >= 194 && c <= 223) { cp = c & 31; extra = 1; min = 128; }
        else if (c >= 224 && c <= 239) { cp = c & 15; extra = 2; min = 2048; }
        else if (c >= 240 && c <= 244) { cp = c & 7; extra = 3; min = 65536; }
        else return false;
        if (extra > n - i) return false;
        while (extra--) { c = p[i++]; if ((c & 192) != 128) return false; cp = (cp << 6) | (c & 63); }
        if (cp < min || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return false;
    }
    return true;
}
static int check_string(const unsigned char *p, size_t n) {
    storage value = {0, {0}}, decoded = {0, {0}};
    /* Offset by one to test unaligned ASCII scanning too. */
    const nv_type t = {NV_STRING, sizeof(storage), 62, offsetof(storage, count), offsetof(storage, data) + 1, NULL, NULL, 0};
    unsigned char wire[64];
    nv_ostream out = nv_ostream_buffer(wire, sizeof(wire));
    nv_istream in;
    bool valid = reference_utf8(p, n);
    value.count = n; memcpy(value.data + 1, p, n);
    CHECK(nv_encode(&out, &t, &value) == valid);
    CHECK(out.error == (valid ? NV_OK : NV_ERR_UTF8));
    wire[0] = (unsigned char)(n * 2); memcpy(wire + 1, p, n);
    in = nv_istream_buffer(wire, n + 1);
    CHECK(nv_decode(&in, &t, &decoded) == valid);
    CHECK(in.error == (valid ? NV_OK : NV_ERR_UTF8));
    if (valid) CHECK(decoded.count == n && !memcmp(decoded.data + 1, p, n) && decoded.data[n + 1] == 0);
    return 0;
}
static int strings(void) {
    unsigned char p[62]; uint32_t cp, rng = 12345; size_t i, j;
    for (cp = 0; cp <= 0xffff; ++cp) {
        p[0] = (unsigned char)(cp >> 8); p[1] = (unsigned char)cp;
        CHECK(!check_string(p, 2));
    }
    /* Every Unicode scalar value, including all multi-byte boundary values. */
    for (cp = 0; cp <= 0x10ffff; ++cp) {
        if (cp < 128) { p[0] = (unsigned char)cp; i = 1; }
        else if (cp < 2048) { p[0] = (unsigned char)(0xc0 | (cp >> 6)); p[1] = (unsigned char)(0x80 | (cp & 63)); i = 2; }
        else if (cp < 65536) { p[0] = (unsigned char)(0xe0 | (cp >> 12)); p[1] = (unsigned char)(0x80 | ((cp >> 6) & 63)); p[2] = (unsigned char)(0x80 | (cp & 63)); i = 3; }
        else { p[0] = (unsigned char)(0xf0 | (cp >> 18)); p[1] = (unsigned char)(0x80 | ((cp >> 12) & 63)); p[2] = (unsigned char)(0x80 | ((cp >> 6) & 63)); p[3] = (unsigned char)(0x80 | (cp & 63)); i = 4; }
        CHECK(!check_string(p, i));
    }
    for (i = 0; i <= sizeof(p); ++i) {
        memset(p, 'a', sizeof(p)); CHECK(!check_string(p, i));
        for (j = 0; j < i; ++j) { p[j] = 0xff; CHECK(!check_string(p, i)); p[j] = 'a'; }
    }
    for (i = 0; i < 20000; ++i) {
        size_t n = i % (sizeof(p) + 1);
        for (j = 0; j < n; ++j) { rng = rng * UINT32_C(1664525) + UINT32_C(1013904223); p[j] = (unsigned char)(rng >> 24); }
        CHECK(!check_string(p, n));
    }
    return 0;
}
int main(void) { CHECK(!arrays()); CHECK(!array_depth()); CHECK(!strings()); puts("optimization tests passed"); return 0; }

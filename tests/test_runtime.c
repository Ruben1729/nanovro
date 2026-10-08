#include "nanovro.h"
#include <stdio.h>
#include <string.h>
#include <limits.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); return 1; } } while (0)

typedef struct { size_t count; int32_t items[4]; } ints;
static const nv_type ints_type = {NV_ARRAY, sizeof(ints), 4, offsetof(ints, count), offsetof(ints, items), &nv_int_type, NULL, 0};
typedef struct { size_t size; char data[5]; } text;
static const nv_type text_type = {NV_STRING, sizeof(text), 4, offsetof(text, size), offsetof(text, data), NULL, NULL, 0};
typedef struct { uint8_t bytes[64]; size_t position; bool fail; } io;
static bool write_cb(void *ctx, const uint8_t *data, size_t size) {
    io *b = (io *)ctx;
    if (b->fail || size > sizeof(b->bytes) - b->position) return false;
    memcpy(b->bytes + b->position, data, size); b->position += size; return true;
}
static bool read_cb(void *ctx, uint8_t *data, size_t size) {
    io *b = (io *)ctx;
    if (b->fail || size > sizeof(b->bytes) - b->position) return false;
    memcpy(data, b->bytes + b->position, size); b->position += size; return true;
}
static int integers(void) {
    const int64_t values[] = {0, -1, 1, -64, 64, INT32_MIN, INT32_MAX, INT64_MIN, INT64_MAX};
    const uint8_t golden[] = {0, 1, 2, 127, 128, 1};
    uint8_t buffer[128];
    size_t i;
    nv_ostream out = nv_ostream_buffer(buffer, sizeof(buffer));
    nv_istream in;
    for (i = 0; i < sizeof(values) / sizeof(values[0]); ++i) CHECK(nv_encode(&out, &nv_long_type, &values[i]));
    CHECK(memcmp(buffer, golden, sizeof(golden)) == 0);
    in = nv_istream_buffer(buffer, out.position);
    for (i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
        int64_t value = 123;
        CHECK(nv_decode(&in, &nv_long_type, &value)); CHECK(value == values[i]);
    }
    CHECK(in.position == out.position);
    {
        int64_t value;
        int32_t small;
        uint8_t bad[10];
        memset(bad, 255, sizeof(bad));
        in = nv_istream_buffer(bad, sizeof(bad));
        CHECK(!nv_decode(&in, &nv_long_type, &value)); CHECK(in.error == NV_ERR_INVALID);
        in = nv_istream_buffer(buffer + out.position - 10, 10);
        CHECK(!nv_decode(&in, &nv_int_type, &small)); CHECK(in.error == NV_ERR_INVALID);
        in = nv_istream_buffer(buffer + 4, 1);
        CHECK(!nv_decode(&in, &nv_long_type, &value)); CHECK(in.error == NV_ERR_LIMIT);
    }
    return 0;
}
static int blocks(void) {
    const uint8_t blocked[] = {3, 4, 6, 54, 2, 1, 0}; /* -2, size=2, [3,27], +1, [-1], end */
    const uint8_t bad_size[] = {3, 6, 6, 54, 0, 0};
    const uint8_t short_size[] = {3, 2, 6, 54, 0};
    const uint8_t overflow[] = {10};
    const uint8_t total_overflow[] = {8, 0, 0, 0, 0, 2, 0, 0};
    ints a = {0};
    nv_istream in = nv_istream_buffer(blocked, sizeof(blocked));
    CHECK(nv_decode(&in, &ints_type, &a)); CHECK(a.count == 3);
    CHECK(a.items[0] == 3 && a.items[1] == 27 && a.items[2] == -1);
    in = nv_istream_buffer(bad_size, sizeof(bad_size));
    CHECK(!nv_decode(&in, &ints_type, &a)); CHECK(in.error == NV_ERR_INVALID);
    in = nv_istream_buffer(short_size, sizeof(short_size));
    CHECK(!nv_decode(&in, &ints_type, &a)); CHECK(in.error == NV_ERR_LIMIT);
    in = nv_istream_buffer(overflow, sizeof(overflow)); CHECK(!nv_decode(&in, &ints_type, &a));
    in = nv_istream_buffer(total_overflow, sizeof(total_overflow)); CHECK(!nv_decode(&in, &ints_type, &a));
    {
        uint8_t bytes[32]; nv_ostream out = nv_ostream_buffer(bytes, sizeof(bytes));
        a.count = 5; CHECK(!nv_encode(&out, &ints_type, &a)); CHECK(out.error == NV_ERR_LIMIT);
        a.count = 0; out = nv_ostream_buffer(bytes, sizeof(bytes));
        CHECK(nv_encode(&out, &ints_type, &a)); CHECK(out.position == 1 && bytes[0] == 0);
    }
    return 0;
}
static int strings(void) {
    const uint8_t good[] = {8, 0xf0, 0x9f, 0x98, 0x80};
    const uint8_t bad[][5] = {{4, 0xc0, 0x80, 0, 0}, {6, 0xed, 0xa0, 0x80, 0}, {8, 0xf4, 0x90, 0x80, 0x80}, {2, 0xff, 0, 0, 0}};
    text t = {0};
    nv_istream in = nv_istream_buffer(good, sizeof(good));
    size_t i;
    CHECK(nv_decode(&in, &text_type, &t)); CHECK(t.size == 4 && t.data[4] == 0);
    for (i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        in = nv_istream_buffer(bad[i], 5);
        CHECK(!nv_decode(&in, &text_type, &t)); CHECK(in.error == NV_ERR_UTF8);
    }
    {
        const uint8_t negative[] = {1};
        in = nv_istream_buffer(negative, 1); CHECK(!nv_decode(&in, &text_type, &t));
        CHECK(in.error == NV_ERR_INVALID);
    }
    return 0;
}
static int streams(void) {
    io state = {{0}, 0, false};
    int64_t value = INT64_MIN, decoded = 0;
    nv_ostream out = nv_ostream_callback(write_cb, &state, 10);
    nv_istream in;
    CHECK(nv_encode(&out, &nv_long_type, &value)); CHECK(out.position == 10);
    state.position = 0; in = nv_istream_callback(read_cb, &state, 10);
    CHECK(nv_decode(&in, &nv_long_type, &decoded)); CHECK(value == decoded);
    state.fail = true; out = nv_ostream_callback(write_cb, &state, 10);
    CHECK(!nv_encode(&out, &nv_long_type, &value)); CHECK(out.error == NV_ERR_IO);
    in = nv_istream_callback(read_cb, &state, 10);
    CHECK(!nv_decode(&in, &nv_long_type, &decoded)); CHECK(in.error == NV_ERR_IO);
    out = nv_ostream_sizing(); CHECK(nv_encode(&out, &nv_long_type, &value)); CHECK(out.position == 10);
    out = nv_ostream_buffer(state.bytes, 9); CHECK(!nv_encode(&out, &nv_long_type, &value));
    CHECK(out.error == NV_ERR_LIMIT && out.position == 0);
    CHECK(!nv_encode(&out, &nv_null_type, &value)); /* sticky errors */
    out = nv_ostream_buffer(NULL, 1); CHECK(out.error == NV_ERR_ARGUMENT);
    CHECK(!nv_encode(NULL, &nv_long_type, &value));
    in = nv_istream_buffer(NULL, 0); CHECK(!nv_decode(&in, &nv_long_type, &decoded));
    return 0;
}
static int floats_and_tags(void) {
    const uint8_t expected[] = {0, 0, 0x80, 0x3f, 0, 0, 0, 0, 0, 0, 0, 0xc0};
    uint8_t bytes[16]; float f = 1.0f, f2; double d = -2.0, d2;
    nv_ostream out = nv_ostream_buffer(bytes, sizeof(bytes));
    nv_istream in;
    CHECK(nv_encode(&out, &nv_float_type, &f)); CHECK(nv_encode(&out, &nv_double_type, &d));
    CHECK(out.position == sizeof(expected) && memcmp(bytes, expected, sizeof(expected)) == 0);
    in = nv_istream_buffer(bytes, out.position);
    CHECK(nv_decode(&in, &nv_float_type, &f2)); CHECK(nv_decode(&in, &nv_double_type, &d2));
    CHECK(f == f2 && d == d2);
    {
        bool b; const uint8_t invalid[] = {2};
        in = nv_istream_buffer(invalid, 1); CHECK(!nv_decode(&in, &nv_boolean_type, &b));
        CHECK(in.error == NV_ERR_INVALID);
    }
    return 0;
}
static int invalid_tags_and_depth(void) {
    const nv_type enumeration = {NV_ENUM, sizeof(int32_t), 2, 0, 0, NULL, NULL, 0};
    typedef struct { size_t tag; int32_t value; } choice;
    const nv_field branches[] = {{&nv_null_type, offsetof(choice, value)}, {&nv_int_type, offsetof(choice, value)}};
    const nv_type union_type = {NV_UNION, sizeof(choice), 0, offsetof(choice, tag), 0, NULL, branches, 2};
    const uint8_t invalid[] = {4}, negative[] = {1};
    int32_t e = 2;
    choice c = {2, 0};
    nv_ostream out = nv_ostream_sizing();
    nv_istream in = nv_istream_buffer(invalid, sizeof(invalid));
    CHECK(!nv_encode(&out, &enumeration, &e)); CHECK(out.error == NV_ERR_INVALID);
    CHECK(!nv_decode(&in, &enumeration, &e)); CHECK(in.error == NV_ERR_INVALID);
    out = nv_ostream_sizing(); CHECK(!nv_encode(&out, &union_type, &c));
    in = nv_istream_buffer(invalid, sizeof(invalid)); CHECK(!nv_decode(&in, &union_type, &c));
    in = nv_istream_buffer(negative, sizeof(negative)); CHECK(!nv_decode(&in, &union_type, &c));
    {
        nv_type recursive;
        const nv_field field = {&recursive, 0};
        uint8_t placeholder = 0;
        const nv_type definition = {NV_RECORD, 1, 0, 0, 0, NULL, &field, 1};
        recursive = definition;
        out = nv_ostream_sizing(); CHECK(!nv_encode(&out, &recursive, &placeholder)); CHECK(out.error == NV_ERR_DEPTH);
        in = nv_istream_buffer(NULL, 0); CHECK(!nv_decode(&in, &recursive, &placeholder)); CHECK(in.error == NV_ERR_DEPTH);
        {
            nv_resolution resolution;
            const nv_resolution_field child = {&resolution, 0};
            const nv_resolution definition = {NV_RES_RECORD, NULL, NULL, &child, 1, NULL, NULL, 0};
            resolution = definition;
            in = nv_istream_buffer(NULL, 0);
            CHECK(!nv_decode_resolved(&in, &resolution, &placeholder)); CHECK(in.error == NV_ERR_DEPTH);
            resolution.kind = NV_RES_SKIP; resolution.writer = &recursive;
            in = nv_istream_buffer(NULL, 0);
            CHECK(!nv_decode_resolved(&in, &resolution, &placeholder)); CHECK(in.error == NV_ERR_DEPTH);
            resolution.kind = NV_RES_DEFAULT; resolution.reader = &recursive;
            in = nv_istream_buffer(NULL, 0);
            CHECK(!nv_decode_resolved(&in, &resolution, &placeholder)); CHECK(in.error == NV_ERR_DEPTH);
            in = nv_istream_buffer(NULL, 0);
            CHECK(!nv_decode_resolved(&in, NULL, &placeholder)); CHECK(in.error == NV_ERR_ARGUMENT);
        }
    }
    return 0;
}
int main(void) {
    CHECK(!integers()); CHECK(!blocks()); CHECK(!strings()); CHECK(!streams()); CHECK(!floats_and_tags());
    CHECK(!invalid_tags_and_depth());
    puts("nanovro runtime tests passed"); return 0;
}

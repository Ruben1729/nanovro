#include "nanovro.h"
#include <float.h>
#include <limits.h>
#include <string.h>

#if CHAR_BIT != 8 || FLT_RADIX != 2 || FLT_MANT_DIG != 24 || DBL_MANT_DIG != 53
#error "nanovro requires 8-bit bytes and IEEE 754 float/double"
#endif
typedef char nv_float_size_check[sizeof(float) == 4 ? 1 : -1];
typedef char nv_double_size_check[sizeof(double) == 8 ? 1 : -1];

#define PRIMITIVE(name, kind, ctype) \
    const nv_type nv_##name##_type = {kind, sizeof(ctype), 0, 0, 0, NULL, NULL, 0}
PRIMITIVE(null, NV_NULL, uint8_t);
PRIMITIVE(boolean, NV_BOOLEAN, bool);
PRIMITIVE(int, NV_INT, int32_t);
PRIMITIVE(long, NV_LONG, int64_t);
PRIMITIVE(float, NV_FLOAT, float);
PRIMITIVE(double, NV_DOUBLE, double);

nv_ostream nv_ostream_buffer(uint8_t *buffer, size_t capacity) {
    nv_ostream s = {NULL, NULL, buffer, capacity, 0, NV_OK};
    if (!buffer && capacity) s.error = NV_ERR_ARGUMENT;
    return s;
}
nv_ostream nv_ostream_sizing(void) {
    nv_ostream s = {NULL, NULL, NULL, SIZE_MAX, 0, NV_OK};
    return s;
}
nv_ostream nv_ostream_callback(nv_write_fn callback, void *context, size_t limit) {
    nv_ostream s = {callback, context, NULL, limit, 0, NV_OK};
    if (!callback) s.error = NV_ERR_ARGUMENT;
    return s;
}
nv_istream nv_istream_buffer(const uint8_t *buffer, size_t size) {
    nv_istream s = {NULL, NULL, buffer, size, 0, NV_OK};
    if (!buffer && size) s.error = NV_ERR_ARGUMENT;
    return s;
}
nv_istream nv_istream_callback(nv_read_fn callback, void *context, size_t limit) {
    nv_istream s = {callback, context, NULL, limit, 0, NV_OK};
    if (!callback) s.error = NV_ERR_ARGUMENT;
    return s;
}
static bool out_error(nv_ostream *s, nv_error e) {
    if (s->error == NV_OK) s->error = e;
    return false;
}
static bool in_error(nv_istream *s, nv_error e) {
    if (s->error == NV_OK) s->error = e;
    return false;
}
static bool put(nv_ostream *s, const void *data, size_t n) {
    if (s->error != NV_OK) return false;
    if (s->position > s->limit || n > s->limit - s->position)
        return out_error(s, NV_ERR_LIMIT);
    if (n && s->callback && !s->callback(s->context, (const uint8_t *)data, n))
        return out_error(s, NV_ERR_IO);
    if (n && s->buffer) memcpy(s->buffer + s->position, data, n);
    s->position += n;
    return true;
}
static bool get(nv_istream *s, void *data, size_t n) {
    if (s->error != NV_OK) return false;
    if (s->position > s->limit || n > s->limit - s->position)
        return in_error(s, NV_ERR_LIMIT);
    if (n && s->callback) {
        if (!s->callback(s->context, (uint8_t *)data, n)) return in_error(s, NV_ERR_IO);
    } else if (n) {
        if (!s->buffer) return in_error(s, NV_ERR_ARGUMENT);
        memcpy(data, s->buffer + s->position, n);
    }
    s->position += n;
    return true;
}
static bool put_long(nv_ostream *s, int64_t value) {
    uint64_t u = ((uint64_t)value << 1) ^ (uint64_t)-(value < 0);
    uint8_t bytes[10];
    size_t n = 0;
    do {
        bytes[n++] = (uint8_t)((u & 127) | (u > 127 ? 128 : 0));
        u >>= 7;
    } while (u);
    return put(s, bytes, n);
}
static bool get_long(nv_istream *s, int64_t *value) {
    uint64_t u = 0;
    unsigned i;
    for (i = 0; i < 10; ++i) {
        uint8_t b;
        if (!get(s, &b, 1)) return false;
        if (i == 9 && (b & 254)) return in_error(s, NV_ERR_INVALID);
        u |= (uint64_t)(b & 127) << (7 * i);
        if (!(b & 128)) {
            *value = (int64_t)(u >> 1) ^ -(int64_t)(u & 1);
            return true;
        }
    }
    return in_error(s, NV_ERR_INVALID);
}
static size_t load_size(const uint8_t *p) {
    size_t n;
    memcpy(&n, p, sizeof(n));
    return n;
}
static bool utf8(const uint8_t *p, size_t n) {
    size_t i = 0;
    while (i < n) {
        uint8_t c = p[i++];
        if (c < 128) {
            /* Scan only ASCII runs. memcpy permits unaligned strings and
             * never reads past their length. */
            while (n - i >= sizeof(uint32_t)) {
                uint32_t word;
                memcpy(&word, p + i, sizeof(word));
                if (word & UINT32_C(0x80808080)) break;
                i += sizeof(word);
            }
            continue;
        }
        if (c >= 0xc2 && c <= 0xdf) {
            if (n - i < 1 || (p[i] & 0xc0) != 0x80) return false;
            i += 1;
        } else if (c >= 0xe0 && c <= 0xef) {
            if (n - i < 2 || (p[i] & 0xc0) != 0x80 || (p[i + 1] & 0xc0) != 0x80)
                return false;
            if ((c == 0xe0 && p[i] < 0xa0) || (c == 0xed && p[i] >= 0xa0)) return false;
            i += 2;
        } else if (c >= 0xf0 && c <= 0xf4) {
            if (n - i < 3 || (p[i] & 0xc0) != 0x80 ||
                (p[i + 1] & 0xc0) != 0x80 || (p[i + 2] & 0xc0) != 0x80) return false;
            if ((c == 0xf0 && p[i] < 0x90) || (c == 0xf4 && p[i] >= 0x90)) return false;
            i += 3;
        } else {
            return false;
        }
    }
    return true;
}
/* Only contiguous floating arrays can be copied as wire bytes. Unknown or
 * big-endian targets retain the scalar conversion path. The override also
 * lets host tests exercise that portable fallback. */
static bool bulk_float_array(const nv_type *t) {
#if !defined(NV_DISABLE_ARRAY_BULK_COPY) && defined(__BYTE_ORDER__) && \
    __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__ && \
    (!defined(__FLOAT_WORD_ORDER__) || __FLOAT_WORD_ORDER__ == __ORDER_LITTLE_ENDIAN__)
    return (t->kind == NV_FLOAT && t->size == 4) || (t->kind == NV_DOUBLE && t->size == 8);
#else
    (void)t;
    return false;
#endif
}
static bool encode(nv_ostream *, const nv_type *, const uint8_t *, unsigned);
static bool decode(nv_istream *, const nv_type *, uint8_t *, unsigned);
static bool encode_array(nv_ostream *s, const nv_type *t, const uint8_t *p, size_t n, unsigned depth) {
    size_t i;
    if (depth > NV_MAX_DEPTH) return out_error(s, NV_ERR_DEPTH);
    if (t->size && n > SIZE_MAX / t->size) return out_error(s, NV_ERR_LIMIT);
    if (bulk_float_array(t)) return put(s, p, n * t->size);
    /* Dispatch once per block, rather than once per primitive element. */
    switch (t->kind) {
    case NV_INT:
        for (i = 0; i < n; ++i) {
            int32_t v;
            memcpy(&v, p + i * t->size, sizeof(v));
            if (!put_long(s, v)) return false;
        }
        return true;
    case NV_LONG:
        for (i = 0; i < n; ++i) {
            int64_t v;
            memcpy(&v, p + i * t->size, sizeof(v));
            if (!put_long(s, v)) return false;
        }
        return true;
    default:
        for (i = 0; i < n; ++i)
            if (!encode(s, t, p + i * t->size, depth)) return false;
        return true;
    }
}
static bool decode_array(nv_istream *s, const nv_type *t, uint8_t *p, size_t n, unsigned depth) {
    size_t i;
    if (depth > NV_MAX_DEPTH) return in_error(s, NV_ERR_DEPTH);
    if (t->size && n > SIZE_MAX / t->size) return in_error(s, NV_ERR_LIMIT);
    if (bulk_float_array(t)) return get(s, p, n * t->size);
    switch (t->kind) {
    case NV_INT:
        for (i = 0; i < n; ++i) {
            int64_t v;
            int32_t v32;
            if (!get_long(s, &v)) return false;
            if (v < INT32_MIN || v > INT32_MAX) return in_error(s, NV_ERR_INVALID);
            v32 = (int32_t)v;
            memcpy(p + i * t->size, &v32, sizeof(v32));
        }
        return true;
    case NV_LONG:
        for (i = 0; i < n; ++i) {
            int64_t v;
            if (!get_long(s, &v)) return false;
            memcpy(p + i * t->size, &v, sizeof(v));
        }
        return true;
    default:
        for (i = 0; i < n; ++i)
            if (!decode(s, t, p + i * t->size, depth)) return false;
        return true;
    }
}
static bool encode(nv_ostream *s, const nv_type *t, const uint8_t *p, unsigned depth) {
    size_t n, i;
    int32_t v32;
    int64_t v64;
    uint64_t bits = 0;
    uint8_t bytes[8];
    if (depth > NV_MAX_DEPTH) return out_error(s, NV_ERR_DEPTH);
    switch (t->kind) {
    case NV_NULL: return true;
    case NV_BOOLEAN: {
        bool b;
        memcpy(&b, p, sizeof(b)); bytes[0] = b ? 1 : 0;
        return put(s, bytes, 1);
    }
    case NV_INT: case NV_ENUM:
        memcpy(&v32, p, sizeof(v32));
        if (t->kind == NV_ENUM && (v32 < 0 || (uint32_t)v32 >= t->capacity))
            return out_error(s, NV_ERR_INVALID);
        return put_long(s, v32);
    case NV_LONG:
        memcpy(&v64, p, sizeof(v64)); return put_long(s, v64);
    case NV_FLOAT: case NV_DOUBLE:
        n = t->kind == NV_FLOAT ? 4 : 8;
        if (n == 4) { uint32_t b32; memcpy(&b32, p, 4); bits = b32; }
        else memcpy(&bits, p, 8);
        for (i = 0; i < n; ++i) bytes[i] = (uint8_t)(bits >> (i * 8));
        return put(s, bytes, n);
    case NV_FIXED: return put(s, p + t->data_offset, t->capacity);
    case NV_BYTES: case NV_STRING:
        n = load_size(p + t->count_offset);
        if (n > t->capacity || (uint64_t)n > INT64_MAX) return out_error(s, NV_ERR_LIMIT);
        if (t->kind == NV_STRING && !utf8(p + t->data_offset, n)) return out_error(s, NV_ERR_UTF8);
        return put_long(s, (int64_t)n) && put(s, p + t->data_offset, n);
    case NV_RECORD:
        for (i = 0; i < t->field_count; ++i)
            if (!encode(s, t->fields[i].type, p + t->fields[i].offset, depth + 1)) return false;
        return true;
    case NV_ARRAY:
        n = load_size(p + t->count_offset);
        if (n > t->capacity || (uint64_t)n > INT64_MAX) return out_error(s, NV_ERR_LIMIT);
        if (n) {
            if (!put_long(s, (int64_t)n)) return false;
            if (!encode_array(s, t->element, p + t->data_offset, n, depth + 1)) return false;
        }
        return put_long(s, 0);
    case NV_UNION:
        n = load_size(p + t->count_offset);
        if (n >= t->field_count) return out_error(s, NV_ERR_INVALID);
        return put_long(s, (int64_t)n) && encode(s, t->fields[n].type, p + t->fields[n].offset, depth + 1);
    }
    return out_error(s, NV_ERR_ARGUMENT);
}
static bool decode(nv_istream *s, const nv_type *t, uint8_t *p, unsigned depth) {
    size_t n, i;
    int64_t v;
    uint8_t bytes[8];
    if (depth > NV_MAX_DEPTH) return in_error(s, NV_ERR_DEPTH);
    switch (t->kind) {
    case NV_NULL: return true;
    case NV_BOOLEAN: {
        bool b;
        if (!get(s, bytes, 1)) return false;
        if (bytes[0] > 1) return in_error(s, NV_ERR_INVALID);
        b = bytes[0] != 0; memcpy(p, &b, sizeof(b)); return true;
    }
    case NV_INT: case NV_ENUM: {
        int32_t v32;
        if (!get_long(s, &v)) return false;
        if (v < INT32_MIN || v > INT32_MAX) return in_error(s, NV_ERR_INVALID);
        if (t->kind == NV_ENUM && (v < 0 || (uint64_t)v >= t->capacity)) return in_error(s, NV_ERR_INVALID);
        v32 = (int32_t)v; memcpy(p, &v32, sizeof(v32)); return true;
    }
    case NV_LONG:
        if (!get_long(s, &v)) return false;
        memcpy(p, &v, sizeof(v)); return true;
    case NV_FLOAT: case NV_DOUBLE: {
        uint64_t bits = 0;
        n = t->kind == NV_FLOAT ? 4 : 8;
        if (!get(s, bytes, n)) return false;
        for (i = 0; i < n; ++i) bits |= (uint64_t)bytes[i] << (i * 8);
        if (n == 4) { uint32_t b32 = (uint32_t)bits; memcpy(p, &b32, 4); }
        else memcpy(p, &bits, 8);
        return true;
    }
    case NV_FIXED: return get(s, p + t->data_offset, t->capacity);
    case NV_BYTES: case NV_STRING:
        if (!get_long(s, &v)) return false;
        if (v < 0) return in_error(s, NV_ERR_INVALID);
        if ((uint64_t)v > t->capacity) return in_error(s, NV_ERR_LIMIT);
        n = (size_t)v;
        if (!get(s, p + t->data_offset, n)) return false;
        if (t->kind == NV_STRING) {
            if (!utf8(p + t->data_offset, n)) return in_error(s, NV_ERR_UTF8);
            p[t->data_offset + n] = 0;
        }
        memcpy(p + t->count_offset, &n, sizeof(n)); return true;
    case NV_RECORD:
        for (i = 0; i < t->field_count; ++i)
            if (!decode(s, t->fields[i].type, p + t->fields[i].offset, depth + 1)) return false;
        return true;
    case NV_ARRAY:
        n = 0;
        for (;;) {
            size_t count, end = 0, saved_limit = s->limit;
            bool sized;
            if (!get_long(s, &v)) return false;
            if (!v) { memcpy(p + t->count_offset, &n, sizeof(n)); return true; }
            sized = v < 0;
            if (v == INT64_MIN) return in_error(s, NV_ERR_INVALID);
            if (sized) v = -v;
            if ((uint64_t)v > t->capacity - n) return in_error(s, NV_ERR_LIMIT);
            count = (size_t)v;
            if (sized) {
                if (!get_long(s, &v)) return false;
                if (v < 0) return in_error(s, NV_ERR_INVALID);
                if ((uint64_t)v > s->limit - s->position) return in_error(s, NV_ERR_LIMIT);
                end = s->position + (size_t)v;
                s->limit = end;
            }
            if (t->element->size && n + count > SIZE_MAX / t->element->size) {
                s->limit = saved_limit; return in_error(s, NV_ERR_LIMIT);
            }
            if (!decode_array(s, t->element, p + t->data_offset + n * t->element->size, count, depth + 1)) {
                s->limit = saved_limit; return false;
            }
            s->limit = saved_limit;
            if (sized && s->position != end) return in_error(s, NV_ERR_INVALID);
            n += count;
        }
    case NV_UNION:
        if (!get_long(s, &v)) return false;
        if (v < 0 || (uint64_t)v >= t->field_count) return in_error(s, NV_ERR_INVALID);
        n = (size_t)v;
        if (!decode(s, t->fields[n].type, p + t->fields[n].offset, depth + 1)) return false;
        memcpy(p + t->count_offset, &n, sizeof(n)); return true;
    }
    return in_error(s, NV_ERR_ARGUMENT);
}
bool nv_encode(nv_ostream *s, const nv_type *t, const void *value) {
    if (!s) return false;
    if (!t || !value) return out_error(s, NV_ERR_ARGUMENT);
    if (s->error != NV_OK) return false;
    return encode(s, t, (const uint8_t *)value, 0);
}
bool nv_decode(nv_istream *s, const nv_type *t, void *value) {
    if (!s) return false;
    if (!t || !value) return in_error(s, NV_ERR_ARGUMENT);
    if (s->error != NV_OK) return false;
    return decode(s, t, (uint8_t *)value, 0);
}
static bool discard(nv_istream *s, size_t n) {
    uint8_t scratch[64];
    if (s->position > s->limit || n > s->limit - s->position) return in_error(s, NV_ERR_LIMIT);
    if (!s->callback) { s->position += n; return true; }
    while (n) {
        size_t chunk = n < sizeof(scratch) ? n : sizeof(scratch);
        if (!get(s, scratch, chunk)) return false;
        n -= chunk;
    }
    return true;
}
/* Recognize zero-byte records too: a large count of empty records must not
 * cause work proportional to the count when the field is being discarded. */
static bool fixed_wire_width(const nv_type *t, size_t *width, unsigned depth) {
    size_t i, total = 0;
    if (depth > NV_MAX_DEPTH) return false;
    switch (t->kind) {
    case NV_NULL: *width = 0; return true;
    case NV_FLOAT: *width = 4; return true;
    case NV_DOUBLE: *width = 8; return true;
    case NV_FIXED: *width = t->capacity; return true;
    case NV_RECORD:
        for (i = 0; i < t->field_count; ++i) {
            size_t child;
            if (!fixed_wire_width(t->fields[i].type, &child, depth + 1) || child > SIZE_MAX - total) return false;
            total += child;
        }
        *width = total; return true;
    default: return false;
    }
}
static bool skip(nv_istream *s, const nv_type *t, unsigned depth) {
    int64_t v;
    size_t i;
    union { int64_t integer; double real; } scratch;
    if (depth > NV_MAX_DEPTH) return in_error(s, NV_ERR_DEPTH);
    switch (t->kind) {
    case NV_BYTES: case NV_STRING:
        if (!get_long(s, &v)) return false;
        if (v < 0) return in_error(s, NV_ERR_INVALID);
        if ((uint64_t)v > SIZE_MAX) return in_error(s, NV_ERR_LIMIT);
        return discard(s, (size_t)v);
    case NV_FIXED: return discard(s, t->capacity);
    case NV_RECORD:
        for (i = 0; i < t->field_count; ++i)
            if (!skip(s, t->fields[i].type, depth + 1)) return false;
        return true;
    case NV_UNION:
        if (!get_long(s, &v)) return false;
        if (v < 0 || (uint64_t)v >= t->field_count) return in_error(s, NV_ERR_INVALID);
        return skip(s, t->fields[(size_t)v].type, depth + 1);
    case NV_ARRAY:
        for (;;) {
            if (!get_long(s, &v)) return false;
            if (!v) return true;
            if (v == INT64_MIN) return in_error(s, NV_ERR_INVALID);
            if (v < 0) {
                /* Avro sized blocks can be skipped without decoding items. */
                if (!get_long(s, &v)) return false;
                if (v < 0) return in_error(s, NV_ERR_INVALID);
                if ((uint64_t)v > SIZE_MAX) return in_error(s, NV_ERR_LIMIT);
                if (!discard(s, (size_t)v)) return false;
            } else {
                uint64_t count = (uint64_t)v;
                size_t width;
                if (fixed_wire_width(t->element, &width, depth + 1)) {
                    if (width && count > SIZE_MAX / width) return in_error(s, NV_ERR_LIMIT);
                    if (width && !discard(s, (size_t)count * width)) return false;
                    continue;
                }
                while (count--) if (!skip(s, t->element, depth + 1)) return false;
            }
        }
    default: return decode(s, t, (uint8_t *)&scratch, depth);
    }
}
static bool resolve(nv_istream *s, const nv_resolution *plan, uint8_t *p, unsigned depth) {
    const nv_type *w = plan->writer, *r = plan->reader;
    int64_t v;
    size_t i, n;
    if (depth > NV_MAX_DEPTH) return in_error(s, NV_ERR_DEPTH);
    switch (plan->kind) {
    case NV_RES_DIRECT: return decode(s, r, p, depth);
    case NV_RES_SKIP: return skip(s, w, depth);
    case NV_RES_ERROR: return in_error(s, NV_ERR_SCHEMA);
    case NV_RES_DEFAULT: {
        nv_istream defaults = nv_istream_buffer(plan->defaults, plan->default_size);
        if (!decode(&defaults, r, p, depth)) return in_error(s, defaults.error);
        return defaults.position == defaults.limit || in_error(s, NV_ERR_SCHEMA);
    }
    case NV_RES_RECORD:
        for (i = 0; i < plan->count; ++i)
            if (!resolve(s, plan->fields[i].plan, p + plan->fields[i].offset, depth + 1)) return false;
        return true;
    case NV_RES_WRITER_UNION:
        if (!get_long(s, &v)) return false;
        if (v < 0 || (uint64_t)v >= plan->count) return in_error(s, NV_ERR_INVALID);
        return resolve(s, plan->fields[(size_t)v].plan, p, depth + 1);
    case NV_RES_READER_UNION:
        /* Reader wrapping consumes no wire nesting level. */
        if (!resolve(s, plan->fields[0].plan, p + plan->fields[0].offset, depth)) return false;
        n = plan->count;
        memcpy(p + r->count_offset, &n, sizeof(n)); return true;
    case NV_RES_ENUM: {
        int32_t symbol;
        if (!get_long(s, &v)) return false;
        if (v < 0 || (uint64_t)v >= plan->count) return in_error(s, NV_ERR_INVALID);
        symbol = plan->symbols[(size_t)v];
        if (symbol < 0) return in_error(s, NV_ERR_SCHEMA);
        memcpy(p, &symbol, sizeof(symbol)); return true;
    }
    case NV_RES_PROMOTE:
        if ((w->kind == NV_STRING || w->kind == NV_BYTES) &&
            (r->kind == NV_STRING || r->kind == NV_BYTES)) {
            if (!decode(s, r, p, depth)) return false;
            if (w->kind == NV_STRING && !utf8(p + r->data_offset, load_size(p + r->count_offset)))
                return in_error(s, NV_ERR_UTF8);
            return true;
        }
        if (w->kind == NV_FLOAT) {
            float f; double d;
            if (!decode(s, w, (uint8_t *)&f, depth)) return false;
            d = (double)f; memcpy(p, &d, sizeof(d)); return true;
        }
        if (!get_long(s, &v)) return false;
        if (w->kind == NV_INT && (v < INT32_MIN || v > INT32_MAX)) return in_error(s, NV_ERR_INVALID);
        if (r->kind == NV_LONG) memcpy(p, &v, sizeof(v));
        else if (r->kind == NV_FLOAT) { float f = (float)v; memcpy(p, &f, sizeof(f)); }
        else { double d = (double)v; memcpy(p, &d, sizeof(d)); }
        return true;
    case NV_RES_ARRAY:
        n = 0;
        for (;;) {
            size_t count, end = 0, saved_limit = s->limit;
            bool sized;
            if (!get_long(s, &v)) return false;
            if (!v) { memcpy(p + r->count_offset, &n, sizeof(n)); return true; }
            sized = v < 0;
            if (v == INT64_MIN) return in_error(s, NV_ERR_INVALID);
            if (sized) v = -v;
            if ((uint64_t)v > r->capacity - n) return in_error(s, NV_ERR_LIMIT);
            count = (size_t)v;
            if (r->element->size && n + count > SIZE_MAX / r->element->size) return in_error(s, NV_ERR_LIMIT);
            if (sized) {
                if (!get_long(s, &v)) return false;
                if (v < 0) return in_error(s, NV_ERR_INVALID);
                if ((uint64_t)v > s->limit - s->position) return in_error(s, NV_ERR_LIMIT);
                end = s->position + (size_t)v; s->limit = end;
            }
            for (i = 0; i < count; ++i) {
                if (!resolve(s, plan->fields[0].plan, p + r->data_offset + (n + i) * r->element->size, depth + 1)) {
                    s->limit = saved_limit; return false;
                }
            }
            s->limit = saved_limit;
            if (sized && s->position != end) return in_error(s, NV_ERR_INVALID);
            n += count;
        }
    }
    return in_error(s, NV_ERR_ARGUMENT);
}
bool nv_decode_resolved(nv_istream *s, const nv_resolution *plan, void *value) {
    if (!s) return false;
    if (!plan || !value) return in_error(s, NV_ERR_ARGUMENT);
    if (s->error != NV_OK) return false;
    return resolve(s, plan, (uint8_t *)value, 0);
}
bool nv_decode_resolved_run(nv_istream *s, const nv_resolution_run *run,
                            size_t present, void *value) {
    size_t i;
    uint8_t *p = (uint8_t *)value;
    if (!s) return false;
    if (!run || !value || present > run->count) return in_error(s, NV_ERR_ARGUMENT);
    if (s->error != NV_OK) return false;
    for (i = 0; i < run->count; ++i)
        if (!resolve(s, i < present ? run->value : run->fallback,
                     p + run->offset + i * run->stride, 1)) return false;
    return true;
}
const char *nv_error_string(nv_error error) {
    static const char *const names[] = {"ok", "invalid argument", "I/O failure", "capacity or stream limit", "invalid Avro data", "invalid UTF-8", "nesting limit", "incompatible schema"};
    return (unsigned)error < sizeof(names) / sizeof(names[0]) ? names[error] : "unknown error";
}

#ifndef NANOVRO_H
#define NANOVRO_H

/** @file nanovro.h
 * @brief Heap-free Avro binary datum encoding and decoding for C99.
 * Prefer generated typed wrappers. The caller owns messages, buffers, streams,
 * and callback contexts; keep them valid throughout each operation. Wire buffers
 * must not overlap messages. Do not share streams across concurrent operations.
 * Failure returns false and preserves the first stream error. Output, decoded
 * values, and external I/O may be partially modified; discard partial results
 * and initialize a new stream before retrying. No rollback is provided.
 * APIs and generated layouts may change before 1.0; use matching generator and
 * runtime versions.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define NV_VERSION "0.1.0"
/** Traversal depth limit; override when compiling the runtime if needed. */
#ifndef NV_MAX_DEPTH
#define NV_MAX_DEPTH 64
#endif

typedef enum {
    NV_OK,           /**< No error. */
    NV_ERR_ARGUMENT, /**< Invalid API argument. */
    NV_ERR_IO,       /**< Callback failure. */
    NV_ERR_LIMIT,    /**< Message capacity or stream byte limit exceeded. */
    NV_ERR_INVALID,  /**< Invalid Avro datum or value. */
    NV_ERR_UTF8,     /**< Invalid UTF-8 string. */
    NV_ERR_DEPTH,    /**< Traversal exceeded NV_MAX_DEPTH. */
    NV_ERR_SCHEMA    /**< Plan cannot resolve this datum. */
} nv_error;

/** Callbacks transfer exactly size bytes synchronously or return false.
 * Partial transfers are not retried. The data pointer is valid only during the
 * call; context belongs to the caller. Requests may span an entire array block.
 */
typedef bool (*nv_write_fn)(void *context, const uint8_t *data, size_t size);
typedef bool (*nv_read_fn)(void *context, uint8_t *data, size_t size);
/** Initialize with nv_ostream_*(). position counts processed/sized bytes;
 * limit is the total byte budget. Inspect error on failure. */
typedef struct {
    nv_write_fn callback;
    void *context;
    uint8_t *buffer;
    size_t limit, position;
    nv_error error;
} nv_ostream;
/** Initialize with nv_istream_*(). position counts consumed bytes;
 * limit is the total byte budget. Inspect error on failure. */
typedef struct {
    nv_read_fn callback;
    void *context;
    const uint8_t *buffer;
    size_t limit, position;
    nv_error error;
} nv_istream;

typedef enum {
    NV_NULL, NV_BOOLEAN, NV_INT, NV_LONG, NV_FLOAT, NV_DOUBLE,
    NV_BYTES, NV_STRING, NV_FIXED, NV_ENUM, NV_RECORD, NV_ARRAY, NV_UNION
} nv_kind;
typedef struct nv_type nv_type;
typedef struct { const nv_type *type; size_t offset; } nv_field;
/** Generated layout metadata, exposed for generated C initializers.
 * Descriptors and referenced data must remain valid during use. They are trusted
 * compile-time data, never untrusted wire input. Offsets and sizes are bytes.
 * Maps are represented as arrays of key/value records.
 */
struct nv_type {
    nv_kind kind;
    size_t size, capacity, count_offset, data_offset;
    const nv_type *element;
    const nv_field *fields;
    size_t field_count;
};

extern const nv_type nv_null_type, nv_boolean_type, nv_int_type, nv_long_type;
extern const nv_type nv_float_type, nv_double_type;

/** Write into caller-owned buffer; NULL is allowed only for zero capacity. */
nv_ostream nv_ostream_buffer(uint8_t *buffer, size_t capacity);
/** Count encoded bytes without writing; position is the size after success. */
nv_ostream nv_ostream_sizing(void);
/** Use a non-NULL write callback with a total byte budget of limit. */
nv_ostream nv_ostream_callback(nv_write_fn callback, void *context, size_t limit);
/** Read caller-owned input; NULL is allowed only for zero size. */
nv_istream nv_istream_buffer(const uint8_t *buffer, size_t size);
/** Use a non-NULL read callback with a total byte budget of limit. */
nv_istream nv_istream_callback(nv_read_fn callback, void *context, size_t limit);
/** Encode one datum from matching C storage using a generated descriptor.
 * All arguments must be non-NULL. Returns true on success. No schema identifier,
 * container header, or transport framing is added. File-level failure rules apply.
 */
bool nv_encode(nv_ostream *stream, const nv_type *type, const void *value);
/** Decode one datum using the writer descriptor into matching C storage.
 * All arguments must be non-NULL. Trailing bytes are allowed; require position
 * == limit after success if the input frame must contain exactly one datum.
 * Strings receive a trailing NUL; their explicit size remains authoritative.
 * Destination contents are unspecified on failure.
 */
bool nv_decode(nv_istream *stream, const nv_type *type, void *value);
/* Generated resolution plans are immutable trusted data. No runtime schema
 * parsing or allocation. The caller selects the writer schema out of band. */
typedef enum {
    NV_RES_DIRECT, NV_RES_SKIP, NV_RES_ERROR, NV_RES_DEFAULT, NV_RES_RECORD,
    NV_RES_ARRAY, NV_RES_WRITER_UNION, NV_RES_READER_UNION, NV_RES_ENUM, NV_RES_PROMOTE
} nv_resolution_kind;
typedef struct nv_resolution nv_resolution;
typedef struct { const nv_resolution *plan; size_t offset; } nv_resolution_field;
struct nv_resolution {
    nv_resolution_kind kind;
    const nv_type *writer, *reader;
    const nv_resolution_field *fields;
    size_t count;
    const int32_t *symbols;
    const uint8_t *defaults;
    size_t default_size;
};
/** Decode into the plan's reader layout; prefer the generated typed wrapper.
 * The caller selects the writer plan out of band; there is no schema discovery.
 * nv_decode argument, framing, and failure rules apply. Ignored payloads may
 * be skipped without semantic validation.
 */
bool nv_decode_resolved(nv_istream *stream, const nv_resolution *plan, void *value);
/* A homogeneous record prefix followed by identical defaults. Generated C
 * verifies the destination layout; present is selected once per connection. */
typedef struct {
    const nv_resolution *value, *fallback;
    size_t offset, stride, count;
} nv_resolution_run;
/** Generated-code entry point. present must not exceed run->count; remaining
 * fields use fallback defaults. Use a generated bundle wrapper to supply the
 * correct run/count. nv_decode_resolved storage and failure rules apply.
 */
bool nv_decode_resolved_run(nv_istream *stream, const nv_resolution_run *run,
                            size_t present, void *value);
/** Return a static diagnostic string; never free it. Unknown codes are handled. */
const char *nv_error_string(nv_error error);

#ifdef __cplusplus
}
#endif
#endif

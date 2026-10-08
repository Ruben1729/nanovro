#include "resolve.h"
#include "connections.h"
#include <stdio.h>

int main(void) {
    /* Old writer: sensor=7, retired="old". No schema ID is embedded here. */
    const uint8_t wire[] = {14, 6, 'o', 'l', 'd'};
    nv_Reading value = {0};
    nv_istream stream = nv_istream_buffer(wire, sizeof(wire));
    if (!nv_resolve_decode(&stream, &value) || stream.position != sizeof(wire)) {
        fprintf(stderr, "resolution failed: %s\n", nv_error_string(stream.error));
        return 1;
    }
    if (value.nv_sensor_id != 7 || value.nv_calibration != 1.0f) return 1;
    {
        /* Connection handshake chose writer index zero from writers.json. */
        nv_connections_decoder decoder = nv_connections_select(0);
        if (!decoder || nv_connections_select(NV_CONNECTIONS_WRITER_COUNT)) return 1;
        stream = nv_istream_buffer(wire, sizeof(wire));
        if (!decoder(&stream, &value) || stream.position != sizeof(wire)) return 1;
        if (value.nv_sensor_id != 7 || value.nv_calibration != 1.0f) return 1;
    }
    puts("Resolved alias, integer promotion, skipped field, and reader default");
    return 0;
}

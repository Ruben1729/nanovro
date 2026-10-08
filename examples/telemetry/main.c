#include "telemetry.h"
#include <stdio.h>
#include <string.h>

int main(void) {
    nv_Telemetry message = {0}, decoded = {0};
    uint8_t buffer[256];
    nv_ostream output = nv_ostream_buffer(buffer, sizeof(buffer));
    nv_istream input;
    message.nv_sequence = 42;
    message.nv_temperature = 23.5f;
    message.nv_active = true;
    message.nv_label.size = 6;
    memcpy(message.nv_label.data, "teensy", 6);
    message.nv_samples.count = 3;
    message.nv_samples.items[0] = -1;
    message.nv_samples.items[1] = 0;
    message.nv_samples.items[2] = 100;
    if (!nv_telemetry_encode(&output, &message)) return 1;
    input = nv_istream_buffer(buffer, output.position);
    if (!nv_telemetry_decode(&input, &decoded)) return 2;
    printf("Encoded %lu bytes; sequence=%ld label=%s\n", (unsigned long)output.position,
           (long)decoded.nv_sequence, decoded.nv_label.data);
    return input.position == output.position ? 0 : 3;
}

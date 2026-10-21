#ifndef FRAME_H
#define FRAME_H
#include <stddef.h>

/* Decodes one frame: a 1-byte payload length, then the payload.
 * Copies the payload into out (cap bytes). Returns the payload length, or -1
 * when the frame is truncated (fewer than 1 + length bytes in in) or the
 * payload does not fit in out. Never reads or writes outside in[0..in_len)
 * and out[0..cap). */
int frame_decode(const unsigned char *in, size_t in_len, unsigned char *out, size_t cap);

#endif

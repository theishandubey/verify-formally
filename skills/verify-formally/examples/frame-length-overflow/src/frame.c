#include <string.h>
#include "frame.h"

int frame_decode(const unsigned char *in, size_t in_len, unsigned char *out, size_t cap)
{
    if (in_len < 1)
        return -1;
    unsigned char n = in[0];
    unsigned char total = n + 1; /* header byte plus payload */
    if (total > in_len)
        return -1; /* truncated */
    if (n > cap)
        return -1; /* does not fit */
    memcpy(out, in + 1, n);
    return n;
}

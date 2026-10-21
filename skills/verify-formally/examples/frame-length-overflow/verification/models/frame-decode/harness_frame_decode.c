/* CBMC harness for frame_decode (src/frame.c), compiled together with the unmodified
 * src/frame.c. Every input is nondeterministic: an input buffer of 0..MAX_IN bytes with any
 * contents, and an output buffer of 0..MAX_OUT bytes. Both are heap objects of exactly the
 * stated size, so CBMC's pointer checks flag any access outside them.
 * Built with -DVF_SANITY, the harness ends in a must-fail assertion (the sanity check). */
#include <stdlib.h>
#include "frame.h"

#define MAX_IN 8
#define MAX_OUT 256
size_t nondet_size_t(void);

void harness(void)
{
    size_t in_len = nondet_size_t();
    size_t cap = nondet_size_t();
    __CPROVER_assume(in_len <= MAX_IN && cap <= MAX_OUT);
    unsigned char *in = malloc(in_len);
    unsigned char *out = malloc(cap);
    __CPROVER_assume(in != NULL && out != NULL);
    frame_decode(in, in_len, out, cap);
#ifdef VF_SANITY
    __CPROVER_assert(0, "sanity: the harness reaches the end");
#endif
}

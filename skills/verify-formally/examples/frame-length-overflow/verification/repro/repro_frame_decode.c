/* Repro and guard tests for finding 001, run against the unmodified src/frame.c by
 * run_c_repro.sh under AddressSanitizer. Each test prints the property it checks; a memory
 * error aborts the test, and run_c_repro.sh reports it as that property's violation. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "frame.h"

static int expect(int ok, const char *property, const char *detail)
{
    if (!ok) {
        printf("%s violated: %s\n", property, detail);
        return 1;
    }
    return 0;
}

/* A 3-byte frame whose length byte is 255: frame_decode must reject it as truncated. */
static int test_length_255_reads_past_input(void)
{
    puts("PROPERTY FrameReadsInBounds");
    unsigned char *in = malloc(3);
    unsigned char *out = malloc(255);
    in[0] = 255;
    in[1] = 'a';
    in[2] = 'b';
    int got = frame_decode(in, 3, out, 255);
    int failed = expect(got == -1, "FrameReadsInBounds",
                        "a 3-byte frame claiming 255 payload bytes was not rejected");
    free(in);
    free(out);
    return failed;
}

/* A well-formed 4-byte frame decodes to its 3-byte payload. */
static int test_well_formed_frame_decodes(void)
{
    unsigned char in[4] = {3, 'x', 'y', 'z'};
    unsigned char out[8];
    int got = frame_decode(in, sizeof in, out, sizeof out);
    return expect(got == 3 && memcmp(out, "xyz", 3) == 0, "FrameDecodesPayload",
                  "a well-formed frame did not decode to its payload");
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0); /* keep every line if a sanitizer aborts the test */
    const char *name = argc > 1 ? argv[1] : "";
    int (*test)(void) = NULL;
    if (strcmp(name, "test_length_255_reads_past_input") == 0)
        test = test_length_255_reads_past_input;
    else if (strcmp(name, "test_well_formed_frame_decodes") == 0)
        test = test_well_formed_frame_decodes;
    if (test == NULL) {
        printf("no test named %s\n", name);
        return 2;
    }
    printf("RUN %s\n", name);
    fflush(stdout);
    int failed = test();
    printf("%s %s\n", failed ? "FAIL" : "PASS", name);
    return failed;
}

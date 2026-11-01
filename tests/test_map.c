#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "../src/cache_map.h"

int main(void)
{
    struct cache_bin bins[64];
    assert(map_init(bins, 0, 64) == 0);
    assert(map_init(bins, 1, 64) == 1);
    assert(bins[0].start_page == 0 && bins[0].page_count == 1);
    assert(map_symbol(&bins[0]) == '.');
    bins[0].resident_pages = 1;
    assert(map_symbol(&bins[0]) == '#');
    assert(map_init(bins, 65536, 32) == 32);
    for (size_t i = 0; i < 32; ++i) {
        assert(bins[i].start_page == i * 2048);
        bins[i].resident_pages = i < 16 ? 2048 : 0;
        assert(map_symbol(&bins[i]) == (i < 16 ? '#' : '.'));
    }
    bins[20].resident_pages = 1;
    assert(map_symbol(&bins[20]) == '+');
    /* Uneven division and huge inputs must have contiguous, exact coverage. */
    const uint64_t sizes[] = {2, 63, 64, 65, 12345, UINT64_MAX};
    for (size_t j = 0; j < sizeof(sizes) / sizeof(sizes[0]); ++j) {
        size_t n = map_init(bins, sizes[j], 64);
        uint64_t next = 0;
        for (size_t i = 0; i < n; ++i) {
            assert(bins[i].start_page == next);
            assert(bins[i].page_count > 0);
            next += bins[i].page_count;
        }
        assert(next == sizes[j]);
    }
    puts("cache map tests passed");
    return 0;
}

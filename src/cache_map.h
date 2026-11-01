#ifndef CACHE_MAP_H
#define CACHE_MAP_H

#include <stddef.h>
#include <stdint.h>

struct cache_bin {
    uint64_t start_page;
    uint64_t page_count;
    uint64_t resident_pages;
};

/* Every page belongs to exactly one nonempty bin; no multiplication overflow. */
static size_t map_init(struct cache_bin *bins, uint64_t pages, size_t width)
{
    size_t count = pages < width ? (size_t)pages : width;
    if (!count)
        return 0;
    uint64_t start = 0;
    for (size_t i = 0; i < count; ++i) {
        uint64_t n = pages / count + (i < pages % count);
        bins[i] = (struct cache_bin){start, n, 0};
        start += n;
    }
    return count;
}

static char map_symbol(const struct cache_bin *bin)
{
    if (!bin->resident_pages)
        return '.';
    if (bin->resident_pages == bin->page_count)
        return '#';
    return '+';
}

#endif

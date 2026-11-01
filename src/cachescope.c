#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifdef __linux__
#include <sys/mman.h>
#include "cache_map.h"
#endif

#define VERSION "1.2.0"
#define MAX_WIDTH 4096

static void usage(FILE *out)
{
    fputs("CacheScope " VERSION " — Linux file-cache profiler\n"
          "Usage: cachescope inspect [--json] [--width N] [--allow-unverified] [--] FILE\n"
          "                          [--offset SIZE] [--length SIZE]\n"
          "       cachescope --help | --version\n\n"
          "Queries mmap/mincore without reading file contents.\n"
          "Map: . no pages resident, + some resident, # all resident.\n"
          "Width defaults to 64 (1–4096); short files use one bin per page.\n"
          "Zoom with a page-aligned offset and optional positive length.\n"
          "Sizes accept bytes, KiB, MiB, or GiB; a range must fit inside the file.\n"
          "File ownership is required unless --allow-unverified is explicit.\n", out);
}

static bool parse_size(const char *value, uint64_t *result)
{
    if (*value < '0' || *value > '9')
        return false;
    char *end;
    errno = 0;
    uintmax_t number = strtoumax(value, &end, 10);
    uint64_t multiplier;
    if (!*end || !strcmp(end, "B"))
        multiplier = 1;
    else if (!strcmp(end, "KiB"))
        multiplier = 1024;
    else if (!strcmp(end, "MiB"))
        multiplier = 1024 * 1024;
    else if (!strcmp(end, "GiB"))
        multiplier = 1024ULL * 1024 * 1024;
    else
        return false;
    if (errno || number > UINT64_MAX / multiplier)
        return false;
    *result = (uint64_t)number * multiplier;
    return true;
}

#ifdef __linux__
/* ASCII JSON, including arbitrary Unix path bytes via Python-style surrogateescape. */
static void json_string(const char *s)
{
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        if (*p == '"' || *p == '\\')
            printf("\\%c", *p);
        else if (*p < 0x20)
            printf("\\u%04x", *p);
        else if (*p < 0x80)
            putchar(*p);
        else {
            uint32_t cp = 0, minimum = 0;
            size_t n = 0;
            if (*p >= 0xc2 && *p <= 0xdf) {
                n = 2; cp = *p & 0x1f; minimum = 0x80;
            } else if (*p >= 0xe0 && *p <= 0xef) {
                n = 3; cp = *p & 0x0f; minimum = 0x800;
            } else if (*p >= 0xf0 && *p <= 0xf4) {
                n = 4; cp = *p & 0x07; minimum = 0x10000;
            }
            bool valid = n != 0;
            for (size_t i = 1; i < n; ++i) {
                if (p[i] < 0x80 || p[i] > 0xbf) {
                    valid = false;
                    break;
                }
                cp = (cp << 6) | (p[i] & 0x3f);
            }
            if (!valid || cp < minimum || cp > 0x10ffff ||
                (cp >= 0xd800 && cp <= 0xdfff)) {
                printf("\\udc%02x", *p);
            } else {
                if (cp <= 0xffff)
                    printf("\\u%04x", (unsigned int)cp);
                else {
                    cp -= 0x10000;
                    printf("\\u%04x\\u%04x", (unsigned int)(0xd800 + (cp >> 10)),
                           (unsigned int)(0xdc00 + (cp & 0x3ff)));
                }
                p += n - 1;
            }
        }
    }
    putchar('"');
}

struct inspection_result {
    const char *path;
    struct stat metadata;
    uid_t effective_uid;
    bool verified;
    uint64_t size, page_size, offset, length, pages, resident;
    double percent;
    const struct cache_bin *bins;
    size_t bin_count;
};

static void print_json(const struct inspection_result *r)
{
    fputs("{\"schema_version\":1,\"file\":", stdout);
    json_string(r->path);
    printf(",\"size_bytes\":%" PRIu64 ",\"offset_bytes\":%" PRIu64
           ",\"length_bytes\":%" PRIu64 ",\"page_size\":%" PRIu64
           ",\"total_pages\":%" PRIu64 ",\"resident_pages\":%" PRIu64
           ",\"resident_percent\":%.6f,\"owner_uid\":%ju,\"effective_uid\":%ju"
           ",\"ownership_verified\":%s,\"device\":%ju,\"inode\":%ju,\"map\":\"",
           r->size, r->offset, r->length, r->page_size, r->pages, r->resident, r->percent,
           (uintmax_t)r->metadata.st_uid, (uintmax_t)r->effective_uid, r->verified ? "true" : "false",
           (uintmax_t)r->metadata.st_dev, (uintmax_t)r->metadata.st_ino);
    for (size_t i = 0; i < r->bin_count; ++i)
        putchar(map_symbol(&r->bins[i]));
    fputs("\",\"bins\":[", stdout);
    for (size_t i = 0; i < r->bin_count; ++i) {
        uint64_t relative_offset = r->bins[i].start_page * r->page_size;
        uint64_t offset = r->offset + relative_offset;
        uint64_t length = r->bins[i].page_count * r->page_size;
        if (length > r->length - relative_offset)
            length = r->length - relative_offset;
        printf("%s{\"start_page\":%" PRIu64 ",\"page_count\":%" PRIu64
               ",\"offset_bytes\":%" PRIu64 ",\"length_bytes\":%" PRIu64
               ",\"resident_pages\":%" PRIu64 "}", i ? "," : "",
               r->offset / r->page_size + r->bins[i].start_page, r->bins[i].page_count,
               offset, length, r->bins[i].resident_pages);
    }
    fputs("]}\n", stdout);
}

static void print_text(const struct inspection_result *r, bool show_range)
{
    printf("File         %s\nSize         %" PRIu64 " bytes (%.2f MiB)\n"
           "Page size    %" PRIu64 " bytes\nResident     %" PRIu64 " of %" PRIu64
           " pages (%.1f%%)\nVisibility   %s\n",
           r->path, r->size, (double)r->size / 1048576.0, r->page_size, r->resident, r->pages,
           r->percent, r->verified ? "owner verified" : "UNVERIFIED");
    if (show_range)
        printf("Range        [%" PRIu64 ", %" PRIu64 ") bytes\n", r->offset, r->offset + r->length);
    printf("\nFile offset  %.2f — %.2f MiB\nCache map    ",
           (double)r->offset / 1048576.0, (double)(r->offset + r->length) / 1048576.0);
    for (size_t i = 0; i < r->bin_count; ++i)
        putchar(map_symbol(&r->bins[i]));
    if (!r->bin_count)
        fputs("(empty)", stdout);
    fputs("\n             . none  + partial  # all\n", stdout);
}

static int inspect(const char *path, bool json, size_t width, bool allow_unverified,
                   uint64_t range_offset, uint64_t range_length, bool has_length)
{
    int rc = 1;
    /* O_NONBLOCK prevents a named pipe from hanging before fstat rejects it. */
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    struct stat before, after;
    struct cache_bin *bins = NULL;
    unsigned char *vec = NULL;
    if (fd < 0) {
        fprintf(stderr, "cachescope: open %s: %s\n", path, strerror(errno));
        return 1;
    }
    if (fstat(fd, &before) < 0) {
        perror("cachescope: fstat");
        goto done;
    }
    if (!S_ISREG(before.st_mode) || before.st_size < 0) {
        fputs("cachescope: expected a regular file with a nonnegative size\n", stderr);
        goto done;
    }
    bool verified = geteuid() == before.st_uid;
    if (!verified) {
        fprintf(stderr, "cachescope: file owner UID %ju differs from effective UID %ju; "
                "Linux may mask mincore results as fully resident. %s\n",
                (uintmax_t)before.st_uid, (uintmax_t)geteuid(),
                allow_unverified ? "WARNING: reporting unverified data." :
                "Run as the owner, or explicitly use --allow-unverified.");
        if (!allow_unverified)
            goto done;
    }
    long system_page_size = sysconf(_SC_PAGESIZE);
    if (system_page_size <= 0) {
        fputs("cachescope: cannot determine system page size\n", stderr);
        goto done;
    }
    uint64_t page_size = (uint64_t)system_page_size;
    uint64_t size = (uint64_t)before.st_size;
    if (range_offset % page_size || range_offset > size ||
        (has_length && range_length > size - range_offset)) {
        fprintf(stderr, "cachescope: range must fit inside the file and offset must "
                "be a multiple of %" PRIu64 " bytes\n", page_size);
        goto done;
    }
    if (!has_length)
        range_length = size - range_offset;
    uint64_t pages = range_length / page_size + (range_length % page_size != 0);
    bins = calloc(width, sizeof(*bins));
    /* Bound working memory and virtual mappings, even for multi-terabyte files. */
    size_t chunk_pages = (size_t)((64ULL * 1024 * 1024) / page_size);
    if (!chunk_pages)
        chunk_pages = 1;
    vec = malloc(chunk_pages);
    if (!bins || !vec) {
        fputs("cachescope: out of memory\n", stderr);
        goto done;
    }
    size_t count = map_init(bins, pages, width), bin = 0;
    uint64_t resident = 0;
    for (uint64_t first = 0; first < pages;) {
        size_t n = pages - first < chunk_pages ? (size_t)(pages - first) : chunk_pages;
        uint64_t offset = range_offset + first * page_size;
        uint64_t remaining = range_length - first * page_size;
        size_t length = remaining < (uint64_t)n * page_size ?
            (size_t)remaining : n * (size_t)page_size;
        void *mapping = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, (off_t)offset);
        if (mapping == MAP_FAILED) {
            perror("cachescope: mmap");
            goto done;
        }
        /* No MAP_POPULATE, madvise(WILLNEED), or access to mapped bytes. */
        int result = mincore(mapping, length, vec);
        int saved_errno = errno;
        if (munmap(mapping, length) < 0) {
            perror("cachescope: munmap");
            goto done;
        }
        if (result < 0) {
            fprintf(stderr, "cachescope: mincore: %s\n", strerror(saved_errno));
            goto done;
        }
        for (size_t i = 0; i < n; ++i) {
            while (first + i >= bins[bin].start_page + bins[bin].page_count)
                ++bin;
            if (vec[i] & 1) {
                ++resident;
                ++bins[bin].resident_pages;
            }
        }
        first += n;
    }
    if (fstat(fd, &after) < 0) {
        perror("cachescope: final fstat");
        goto done;
    }
    if (before.st_size != after.st_size ||
        before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
        before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
        before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
        before.st_ctim.tv_nsec != after.st_ctim.tv_nsec) {
        fputs("cachescope: file changed during inspection; retry on a quiescent file\n", stderr);
        goto done;
    }
    struct inspection_result result = {
        .path = path, .metadata = before, .effective_uid = geteuid(), .verified = verified,
        .size = size, .page_size = page_size, .offset = range_offset, .length = range_length,
        .pages = pages, .resident = resident,
        .percent = pages ? 100.0 * (double)resident / (double)pages : 0.0,
        .bins = bins, .bin_count = count,
    };
    if (json)
        print_json(&result);
    else
        print_text(&result, range_offset || has_length);
    rc = 0;
done:
    free(vec);
    free(bins);
    if (close(fd) < 0) {
        perror("cachescope: close");
        rc = 1;
    }
    if (fflush(stdout) == EOF) {
        perror("cachescope: stdout");
        rc = 1;
    }
    return rc;
}
#endif

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        puts("cachescope " VERSION);
        return 0;
    }
    if (argc == 2 && (!strcmp(argv[1], "--help") || !strcmp(argv[1], "-h"))) {
        usage(stdout);
        return 0;
    }
    if (argc < 3 || strcmp(argv[1], "inspect")) {
        usage(stderr);
        return 2;
    }
    bool json = false, allow_unverified = false, options = true;
    size_t width = 64;
    uint64_t offset = 0, length = 0;
    bool has_length = false;
    const char *path = NULL;
    for (int i = 2; i < argc; ++i) {
        if (options && !strcmp(argv[i], "--")) {
            options = false;
        } else if (options && !strcmp(argv[i], "--json")) {
            json = true;
        } else if (options && (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h"))) {
            usage(stdout);
            return 0;
        } else if (options && (!strcmp(argv[i], "--offset") || !strcmp(argv[i], "--length"))) {
            bool is_length = !strcmp(argv[i], "--length");
            uint64_t parsed;
            if (++i == argc || !parse_size(argv[i], &parsed) || (is_length && !parsed)) {
                fputs("cachescope: range sizes require bytes or KiB/MiB/GiB; length must be positive\n", stderr);
                return 2;
            }
            if (is_length) {
                length = parsed;
                has_length = true;
            } else {
                offset = parsed;
            }
        } else if (options && !strcmp(argv[i], "--allow-unverified")) {
            allow_unverified = true;
        } else if (options && !strcmp(argv[i], "--width")) {
            if (++i == argc || !*argv[i]) {
                fputs("cachescope: --width requires an integer from 1 to 4096\n", stderr);
                return 2;
            }
            char *end;
            errno = 0;
            unsigned long parsed = strtoul(argv[i], &end, 10);
            if (errno || *end || argv[i][0] < '0' || argv[i][0] > '9' ||
                parsed < 1 || parsed > MAX_WIDTH) {
                fputs("cachescope: --width must be an integer from 1 to 4096\n", stderr);
                return 2;
            }
            width = (size_t)parsed;
        } else if (options && argv[i][0] == '-') {
            fprintf(stderr, "cachescope: unknown option %s\n", argv[i]);
            return 2;
        } else if (path) {
            fputs("cachescope: expected exactly one file\n", stderr);
            return 2;
        } else {
            path = argv[i];
        }
    }
    if (!path) {
        usage(stderr);
        return 2;
    }
#ifdef __linux__
    return inspect(path, json, width, allow_unverified, offset, length, has_length);
#else
    (void)json;
    (void)width;
    (void)allow_unverified;
    (void)offset;
    (void)length;
    (void)has_length;
    fputs("cachescope: Linux is required. Run inside a Linux VM or the Docker image.\n", stderr);
    return 1;
#endif
}

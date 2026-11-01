# Inspector JSON, schema version 1

```sh
cachescope inspect --json --width 32 /data/sample.bin
```

One JSON object is written to stdout. Errors and visibility warnings use stderr.
CLI exit status is 0 for success, 1 for inspection / platform errors, 2 for usage.

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `file` | Path as supplied, JSON escaped; invalid UTF-8 bytes use `\udcXX` surrogate escapes |
| `size_bytes` | Logical length from `fstat` |
| `page_size` | System page size in bytes |
| `total_pages` | `ceil(size_bytes / page_size)` |
| `resident_pages` | Pages whose mincore low bit is set |
| `resident_percent` | Page-based percentage, 0 for an empty file |
| `owner_uid`, `effective_uid` | Numeric file and process UIDs |
| `ownership_verified` | Whether those UIDs match; false requires explicit override |
| `device`, `inode` | File identity for same-node comparisons |
| `map` | One symbol per bin: `.`, `+`, or `#`; empty for zero pages |
| `bins` | Ordered, contiguous, nonempty page ranges |

Each bin contains `start_page`, `page_count`, `offset_bytes`, `length_bytes`, and
`resident_pages`. Byte length excludes padding beyond EOF. Resident counts are
page-based, so a final one-byte page contributes one resident page if present.
Bins partition all pages without overlaps or gaps. When division is uneven, earlier
bins receive one extra page. There are `min(requested_width, total_pages)` bins.

Summing bin page counts and resident counts gives the file totals. Summing byte
lengths gives file size. JSON is diagnostic evidence, not an atomic view. Device
and inode values are useful within one node and mount configuration; do not compare
them across unrelated machines as universal identifiers.

Python validation uses only whole bins inside a requested interval; it does not
guess which pages in a partially overlapping bin were resident. Benchmark JSON
embeds these snapshots and adds environment, trial qualification, raw fio locations,
commands, per-trial results, and medians. See the README for units and statuses.

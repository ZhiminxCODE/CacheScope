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
| `size_bytes` | Full logical file length from `fstat` |
| `offset_bytes` | Start of the inspected range; 0 for a full-file inspection |
| `length_bytes` | Bytes inspected; file size for a full-file inspection |
| `page_size` | System page size in bytes |
| `total_pages` | `ceil(length_bytes / page_size)` for the inspected range |
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
Bins partition the inspected pages without overlaps or gaps. All page indices and
byte offsets are absolute offsets in the underlying file. When division is uneven, earlier
bins receive one extra page. There are `min(requested_width, total_pages)` bins.

Summing bin page counts and resident counts gives the file totals. Summing byte
lengths gives the inspected range length. The new range fields are additive;
older version-1 snapshots without them describe the full file. JSON is diagnostic
evidence, not an atomic view. Device
and inode values are useful within one node and mount configuration; do not compare
them across unrelated machines as universal identifiers.

Python validation uses only whole bins inside a requested interval; it does not
guess which pages in a partially overlapping bin were resident. Benchmark JSON
embeds these snapshots and adds environment, trial qualification, raw fio locations,
commands, per-trial results, and medians. See the README for units and statuses.

## Live recordings and diffs

`watch --save DIR` writes `snapshot-000001.json`, etc. Each is an inspector snapshot
with an added `captured_at_utc` timestamp, usable directly as input to `diff`.
`timeline.jsonl` contains one event per sample; `watch --jsonl` emits the same
events to stdout. Events contain `sample`, `captured_at_utc`, `snapshot`,
`net_resident_pages`, `delta_map`, and `reset_reason`. The first sample and any new
baseline use null deltas and explain the reset. Ctrl-C exits 130 and retains
completed samples. Existing recording directories are never overwritten.

`diff BEFORE AFTER --json` emits `kind: "cache_diff"`, `schema_version: 1`, the
selected range, before/after page totals and percentages, their net difference,
the two maps, and a `delta_map`. Changed bins appear in `changes` with absolute
`offset_bytes`, `length_bytes`, `before_pages`, `after_pages`, and
`net_resident_pages`. `warmer_bins`, `cooler_bins`, and `unchanged_bins` count the
`+`, `-`, and `.` symbols. Unchanged means the net bin count is the same, not that
the same individual pages remained resident. Diff JSON is a comparison record,
not an inspector snapshot and cannot itself be passed as a snapshot to `diff`.

## Paired benchmark speedup

`benchmark.json` contains a `paired_speedup` object, separate from the three trial
summary rows. `qualifying_pairs` and `total_pairs` give the paired sample counts;
`median_cold_to_warm_speedup` is null when none qualify. For every repeat with both
buffered trials at status `ok` and finite positive `fio_elapsed_ms`, `pairs` records
the repeat ID, `cold_elapsed_ms`, `warm_elapsed_ms`, and their
`cold_to_warm_speedup` ratio. `excluded_pairs` records repeat IDs and reasons for
missing, unqualified, or unresolved timings. `metric` and `definition` identify
the elapsed-time basis. `pairs.csv` exports the qualifying per-repeat ratios.

The median is computed from ratios of corresponding runs within a repeat.
Independently computed trial medians remain available in `summary` for comparison.

# Replay a real measurement without Linux

From the repository root, with Python 3.9+ (including macOS):

```sh
python3 -m cachescope_runner diff examples/before.json examples/after.json
python3 -m cachescope_runner diff examples/after.json examples/middle.json
```

These snapshots come from a real 256 MiB file on Docker Desktop's aarch64 Linux VM,
kernel `6.12.54-linuxkit`, with 4096-byte pages. Both controlled trials began with
0% measured residency after per-file eviction. The data file itself is unnecessary
for replay; `diff` only reads the saved JSON.

| Snapshot | Requested read | Observed residency |
|---|---|---:|
| `before.json` | Before the first read | 0% |
| `after.json` | [0, 128) MiB | 50.390625% |
| `middle.json` | [64, 192) MiB after a new cold baseline | 50.390625% |

```text
First-half read:   ################+...............
Middle-half read:  ........################+.......
```

Each trial left 129 MiB resident: the requested 128 MiB region plus 1 MiB beyond
its end, consistent with readahead. The two after-snapshots have identical total
residency and different locations. Their diff has zero net page growth while
still showing bins warming and cooling.

These are recorded observations from this environment. Residency does not prove
the exact bytes requested or identify which process populated a page. Readahead,
reclaim, other readers, and filesystem behavior can change results in another run.

To keep the examples small, adjacent pairs of the original 64 bins were summed
into 32 bins. No per-page status was inferred, and all totals and range boundaries
were preserved. `provenance.json` records the run's environment, requested reads,
and a checksum of the original local validation report. Snapshot paths and inode
numbers describe the original measurement context; replay does not open that path.

To collect a new dataset on Linux:

```sh
make
python3 -m cachescope_runner prepare data --size 256MiB
python3 -m cachescope_runner validate data/sample.bin --output results/spatial
```

The new run need not match these cached percentages or readahead distances.

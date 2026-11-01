# Local verification — November 1, 2026

The implementation was compiled and exercised on this Mac using Docker Desktop's
Linux VM. This is a single-machine validation dataset, not a general performance
claim. AWS reproduction has not been run.

## Checks completed

- C compilation with C11, `-Wall -Wextra -Wpedantic -Werror` on macOS and Linux.
- C map tests: uneven partitions, empty files, huge counts, partial-bin symbols,
  and first-half placement.
- All 9 Python unit tests passed on macOS and Linux.
- All 5 real Linux integration tests passed, with **no skips**: file-size edges,
  argument and file-type errors, arbitrary path bytes / Unicode, ownership masking,
  non-perturbing inspection, and first/middle spatial placement.
- A real 256 MiB file passed first-half and middle-half spatial validation.
- All 15 fio jobs qualified: 5 repetitions × 3 trial types.
- The actual two-pod kind demo passed both shared-file and separate-inode control
  criteria, using read-only hostPath mounts and UID 1000.

## Collected fio measurements

Environment: aarch64, kernel `6.12.54-linuxkit`, fio `3.33`, 4096-byte pages,
Docker VM overlay filesystem, UID 1000. File size 256 MiB, sequential reads,
1 MiB blocks, `psync`, depth 1, one job, `invalidate=0`.

| Trial | Valid | Median before → after | Median fio time | Median bandwidth |
|---|---:|---:|---:|---:|
| First buffered pass | 5/5 | 0% → 100% | 89 ms | 2876.40 MiB/s |
| Second buffered pass | 5/5 | 100% → 100% | 26 ms | 9846.15 MiB/s |
| Direct read | 5/5 | 0% → 0% | 57 ms | 4491.23 MiB/s |

These are fio I/O times. Process wall times, completion latency, and total latency
are retained separately. A cold **guest page cache** does not imply cold physical
storage or cold host / device caches. Direct I/O bypassing the guest page cache
does not eliminate other caches. These short trials demonstrate the experiment;
use longer trials and independent environments before making broader claims.

## Observed spatial maps

The 64-column maps below are copied from collected snapshots. `+` shows a partially
resident bin just past the requested range, consistent with readahead.

```text
First-half read
before ................................................................
after  ################################+...............................

Middle-half read, following a new verified eviction
before ................................................................
after  ................################################+...............

Two pods, shared sample inode
before ................................................................
after  ################################+...............................

Two pods, B inspects a separate, byte-identical control inode
before ................................................................
after  ................................................................
```

The collector confirmed matching content hashes before resetting the cache, and
also confirmed A's sample warmed during the control. It checked pod placement and
file identity before drawing either observation.

## Local raw evidence

The raw files are in the workspace under `results/verification/` (gitignored so new
runs do not add generated files to source control):

- `docker/spatial/validation.json`
- `docker/benchmark/benchmark.json`
- `docker/benchmark/summary.md` and `summary.csv`
- `docker/benchmark/*.fio.json` and `*.fio.stderr.txt`
- `docker/container.json`
- `kubernetes/k8s-demo.json`

The temporary measurement container and kind cluster were removed after exporting
the evidence. The default kubeconfig was not changed; validation used a separate
temporary kubeconfig. Docker images `cachescope:local` and `cachescope:test` remain
available. Recreate the demo using `scripts/setup-kind.sh`; see the README for all
commands.

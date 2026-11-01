# CacheScope

**See which file offsets Linux keeps in RAM, then measure how that changes reads.**

CacheScope is a C command-line profiler backed by `mmap()` and `mincore()`, a
dependency-free Python experiment runner, and a reproducible Docker / Kubernetes
demo. The inspector maps a file without reading its contents. The experiment runner
connects residency snapshots with fio measurements.

The main experiment asks: **if pod A reads half a file, can pod B observe which half?**
The demo compares a shared file with an independently written, byte-identical file
on a separate inode. It saves evidence for both cases.

```text
$ cachescope inspect /data/sample.bin --width 32
File         /data/sample.bin
Size         268435456 bytes (256.00 MiB)
Page size    4096 bytes
Resident     32768 of 65536 pages (50.0%)
Visibility   owner verified

File offset  0 — 256.00 MiB
Cache map    ################................
             . none  + partial  # all
```

This is an **illustration**, not a promised measurement. Readahead, memory pressure,
filesystem behavior, and concurrent access can change the result. Real measurements
come from the commands below; no benchmark numbers are invented.

[Collected local results and verification](docs/verification.md) include a real
five-repeat fio sequence and both Kubernetes observations.

## Build and inspect on Linux

Requirements: a C11 compiler, make, Python 3.9+, and the **Flexible I/O Tester** (`fio`)
for benchmarks. No pip packages, root access, or global cache clearing are needed.

```sh
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y build-essential python3 fio
make

python3 -m cachescope_runner prepare data --size 256MiB
python3 -m cachescope_runner evict data/sample.bin
./cachescope inspect data/sample.bin

python3 -m cachescope_runner read-range data/sample.bin --length 128MiB
./cachescope inspect data/sample.bin --width 32
./cachescope inspect --json data/sample.bin
```

`prepare` writes random data to both `sample.bin` and `control.bin`, independently,
and fsyncs them. It never overwrites existing files. It creates neither sparse files
nor reflinks. Preparation itself warms the cache. Run all measurements as the numeric
UID that owns the file. `evict` requests per-file eviction; **inspect to verify it**.
Use bytes or IEC sizes such as `4KiB`, `128MiB`, or `1GiB`.

The CLI accepts `--json`, `--width 1..4096`, and `--` before a filename starting with
`-`. It handles empty files, partial final pages, and missing paths. Nonregular files
(including directories and FIFOs) are rejected. `make install` installs the C binary;
run the Python module from this repository.

## Verify the spatial measurement

```sh
python3 -m cachescope_runner validate data/sample.bin --output results/spatial
```

This independently evicts and measures before reading the first half, then repeats
with the middle half. It saves every eviction attempt and both maps in
`validation.json`. A baseline qualifies as cold at **≤1% resident**, with up to three
eviction attempts. A spatial result is marked `observed` when whole bins inside the
requested range are ≥90% resident and distant bins are ≤10% resident. A failed
baseline or spatial criterion is recorded as `inconclusive`, with exit code 2.
The distant regions leave room for ordinary readahead near the read boundary.

These thresholds are explicit experimental criteria, not kernel guarantees.
`--cold-threshold` and `--attempts` are configurable. Validation needs at least 64
system pages. Choose a quiet machine and a test file comfortably smaller than
available memory. Do not run experiments on the same file concurrently.

## Run the fio experiment

```sh
python3 -m cachescope_runner benchmark data/sample.bin \
  --repeats 5 --output results/fio
cat results/fio/summary.md
```

Each repetition performs this sequence:

| Trial | Required measured baseline | Read |
|---|---|---|
| `first_buffered` | ≤1% resident after eviction | Buffered, full sequential pass |
| `second_buffered` | ≥95% resident immediately after the first pass | Buffered, same pass |
| `direct` | ≤1% resident after a new eviction | `O_DIRECT`, same pass |

All jobs use `rw=read`, `ioengine=psync`, `iodepth=1`, `numjobs=1`, `bs=1MiB`, and
**`invalidate=0`**, so fio does not evict the intentionally warm second pass.
They use `--readonly` and `allow_file_create=0`. Only `direct` changes between job
types. File size must be a positive multiple of block size; block size must be a
multiple of `max(4096, system page size)`. Set it with `--block-size 64KiB`.

Outputs:

| Artifact | Contents |
|---|---|
| `benchmark.json` | Environment, settings, every snapshot and eviction attempt, raw measurements, statuses, medians |
| `summary.md` / `summary.csv` | Median before/after residency, MiB/s, elapsed milliseconds, completion and total latency |
| `NN-TRIAL.fio.json` | Original fio JSON for each executed job |
| `NN-TRIAL.fio.stderr.txt` | fio diagnostics, including unsupported direct I/O |

Medians include only qualified runs with status `ok`; valid/total counts remain
visible. fio's `read.runtime` is milliseconds, `bw_bytes` is bytes/s, and latency
fields are converted to microseconds. Wall elapsed time is saved separately because
process startup and fio setup can dominate short jobs. fio completion latency
(`clat`) and total latency (`lat`) have different accounting, especially for
synchronous engines; both are retained. Direct I/O is tested on the actual
filesystem. Unsupported errors are recorded, never silently replaced by buffered
I/O. Other job failures remain errors.

Output directories must be new to preserve previous results. Omit `--output` for
a timestamped directory. Exit codes for runner experiments: 0 = all trials qualify,
1 = operational error, 2 = inconclusive or unsupported trial, 130 = interrupted.
Partial completed results survive interruption. A dataset can be inconclusive and
still contain useful evidence.

## Docker, including macOS

```sh
docker build -t cachescope:local .
```

On a **Linux host**, inspect an existing file using its owner's UID:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  --mount type=bind,src="$PWD/data",dst=/data,readonly \
  cachescope:local inspect /data/sample.bin
```

On **macOS**, collect measurements on the Docker VM's Linux filesystem, using a
dedicated named volume instead of a host bind mount. The volume initialization
command below changes ownership only inside this new project volume:

```sh
docker volume create cachescope-data
docker run --rm --user 0:0 -v cachescope-data:/data \
  cachescope:local chown 1000:1000 /data
docker run --rm -v cachescope-data:/data \
  cachescope:local prepare /data --size 256MiB
docker run --rm -v cachescope-data:/data \
  cachescope:local validate /data/sample.bin --output /data/spatial
docker run --rm -v cachescope-data:/data \
  cachescope:local benchmark /data/sample.bin --output /data/benchmark

# Copy collected evidence to this repository.
docker create --name cachescope-export -v cachescope-data:/data cachescope:local
docker cp cachescope-export:/data/benchmark ./results-from-docker
docker rm cachescope-export
```

Default container UID:GID is `1000:1000`. Override it for an existing file. The image
routes `inspect` to C and `prepare`, `evict`, `read-range`, `validate`, and `benchmark`
to Python. Other arguments execute a command directly.

Docker Desktop runs Linux in a VM. A Mac bind mount crosses an additional filesystem
boundary, so native Linux storage gives a clearer page-cache experiment. See
[Docker's VM explanation](https://docs.docker.com/desktop/setup/install/mac-install/).
Building the C binary on macOS is supported for help/version and unit checks;
inspection and experiments explicitly require Linux.

## Two-pod experiment

With Docker, [kind](https://kind.sigs.k8s.io/), and kubectl installed:

```sh
./scripts/setup-kind.sh
python3 scripts/k8s-demo.py --output results/two-pods
```

The setup creates a dedicated single-node `cachescope` cluster, loads the image,
prepares native Linux data as UID 1000, and starts `reader-a` and `observer-b` with
read-only mounts. The collector verifies node placement, shared-file identity,
separate control inode, matching contents, ownership, and cold baselines. It reads
half the sample in A and saves B's maps, then repeats while B observes the control.

The full procedure, manual commands, troubleshooting, and cleanup are in
[docs/kubernetes.md](docs/kubernetes.md). Setup refuses to replace an existing
cluster. It uses an explicit kubectl context on every command.

## What the measurement means

The inspector opens and stats the file, gets the actual page size, and rounds up
the final page. It queries 64 MiB mappings with `mincore`, counting only the low
bit of each returned status byte. It never dereferences the mappings, prefaults
them, or reads file data. This keeps memory usage bounded even for large files.
It rejects a detectable size, modification-time, or metadata change during the scan.

Each map character covers a nonempty consecutive page range; there are at most
`--width` characters. `.` means none of that bin's pages are resident, `+` means
some, and `#` means all. JSON includes exact offsets, byte lengths, page counts,
and resident counts for every bin. A partially filled final page counts as one
page. See [docs/json.md](docs/json.md) for the schema.

`mincore` is a snapshot, and a chunked scan is **not atomic**. Cache pages can be
reclaimed or populated while scanning. Residency is not proof of who read a file,
nor a prediction of an exact latency. Metadata checks cannot eliminate every race.
The controlled shared-file/control experiment supports a narrower observation.

Modern Linux can conceal cache state when the caller lacks the file ownership,
capability, or write authorization required by the kernel. CacheScope conservatively
requires ownership. Nonowners may explicitly pass `--allow-unverified`, but output
is warned and labeled; the experiment runner rejects such measurements. Even root
is subject to this conservative CLI policy when inspecting another UID's file.
See the [kernel visibility check](https://github.com/torvalds/linux/blob/master/mm/mincore.c)
and [`mincore(2)`](https://man7.org/linux/man-pages/man2/mincore.2.html).

Eviction is advisory, not a global reset. CacheScope fsyncs before requesting
`POSIX_FADV_DONTNEED` and qualifies the result by measuring it. Some filesystems,
particularly tmpfs or virtual/shared filesystems, may not produce a cold baseline.
See [`posix_fadvise(2)`](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html).
fio normally invalidates caches before a job; `invalidate=0` preserves the warm
setup. See [fio's documented option](https://fio.readthedocs.io/en/master/fio_doc.html#cmdoption-arg-invalidate).

## Tests and optional cloud reproduction

```sh
make test                         # Linux: C unit, Python unit, real syscall tests
make test-unit                    # also runs on macOS
docker build --target test -t cachescope:test .
docker run --rm cachescope:test    # real Linux tests on Docker VM storage
```

Integration tests cover empty, one-byte, and partial-page files; path escaping;
argument errors; nonregular files; ownership masking; non-perturbing inspection;
and first/middle range placement. Cache-dependent tests explicitly skip when
eviction cannot be verified. The Docker test stage runs as root to exercise the
ownership fixture; production runs as UID 1000. CI also runs a real fio sequence.

AWS is optional. [docs/aws.md](docs/aws.md) describes reproducing the same experiment
on EC2 / EBS without provisioning infrastructure or making up cloud results.

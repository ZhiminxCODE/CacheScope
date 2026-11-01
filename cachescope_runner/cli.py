"""Controlled experiments. All cache claims are based on measured baselines."""

import argparse
import csv
import datetime as dt
import errno
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time


class ExperimentError(Exception):
    pass


def size_arg(value):
    match = re.fullmatch(r"([0-9]+)(B|KiB|MiB|GiB)?", value)
    if not match:
        raise argparse.ArgumentTypeError("use bytes or an IEC size, e.g. 128MiB")
    return int(match[1]) * {None: 1, "B": 1, "KiB": 1024,
                            "MiB": 1024**2, "GiB": 1024**3}[match[2]]


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def percentage(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return result


def save_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
    temporary.replace(path)


def owned_file(path):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    if not path.is_file():
        raise ExperimentError(f"not a regular file: {path}")
    if stat.st_uid != os.geteuid():
        raise ExperimentError(f"run as file owner UID {stat.st_uid}: {path}")
    return path


def binary_path(value=None):
    candidate = value or os.environ.get("CACHESCOPE_BIN")
    if candidate:
        candidate = str(Path(candidate).resolve()) if "/" in candidate else shutil.which(candidate)
    else:
        candidate = shutil.which("cachescope")
        local = Path(__file__).resolve().parent.parent / "cachescope"
        if not candidate and local.is_file():
            candidate = str(local)
    if not candidate:
        raise ExperimentError("cachescope executable not found; run make or pass --cachescope")
    return candidate


def inspect(binary, path, width=64):
    result = subprocess.run([binary, "inspect", "--json", "--width", str(width),
                             "--", str(path)], text=True, capture_output=True, check=False)
    if result.returncode:
        raise ExperimentError(result.stderr.strip() or "inspector failed")
    data = json.loads(result.stdout)
    if data.get("schema_version") != 1 or not data.get("ownership_verified"):
        raise ExperimentError("inspector returned an incompatible or unverified snapshot")
    return data


def prepare(directory, size):
    """Write both files independently: identical data, separate inodes, no holes."""
    if size < 1:
        raise ExperimentError("test file size must be positive")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / "sample.bin", directory / "control.bin"]
    created, streams = [], []
    try:
        for path in paths:
            stream = path.open("xb", buffering=0)
            created.append(path)
            streams.append(stream)
        remaining = size
        while remaining:
            block = os.urandom(min(1024 * 1024, remaining))
            for stream in streams:
                view = memoryview(block)
                while view:
                    written = stream.write(view)
                    if not written:
                        raise ExperimentError("short write while preparing test data")
                    view = view[written:]
            remaining -= len(block)
        for stream in streams:
            os.fsync(stream.fileno())
    except BaseException:
        for stream in streams:
            stream.close()
        for path in created:
            path.unlink()
        raise
    finally:
        for stream in streams:
            stream.close()
    if paths[0].stat().st_ino == paths[1].stat().st_ino:
        raise ExperimentError("test and control unexpectedly share an inode")
    print(f"Wrote and fsynced {size} bytes to each file:")
    for path in paths:
        print(path)
    print("Creation warms the cache. Evict and measure before each cold trial.")


def evict(path):
    # fsync first: DONTNEED does not necessarily discard dirty pages.
    with Path(path).open("rb", buffering=0) as stream:
        os.fsync(stream.fileno())
        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


def cold_baseline(binary, path, threshold, attempts):
    snapshots = []
    for attempt in range(attempts):
        evict(path)
        snapshot = inspect(binary, path)
        snapshots.append(snapshot)
        if snapshot["resident_percent"] <= threshold:
            return snapshot, snapshots, True
        if attempt + 1 < attempts:
            time.sleep(0.1)
    return snapshots[-1], snapshots, False


def read_range(path, offset, length):
    path = owned_file(path)
    if offset < 0 or length <= 0 or offset + length > path.stat().st_size:
        raise ExperimentError("read range must be nonempty and wholly inside the file")
    remaining = length
    start = time.perf_counter()
    with path.open("rb", buffering=0) as stream:
        stream.seek(offset)
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ExperimentError("unexpected EOF; file changed during read")
            remaining -= len(block)
    return {"offset_bytes": offset, "length_bytes": length,
            "wall_elapsed_ms": (time.perf_counter() - start) * 1000}


def region_percent(snapshot, start, end):
    """Use only whole bins inside the requested interval; never infer partial bins."""
    bins = [b for b in snapshot["bins"]
            if b["offset_bytes"] >= start and b["offset_bytes"] + b["length_bytes"] <= end]
    pages = sum(b["page_count"] for b in bins)
    return 100 * sum(b["resident_pages"] for b in bins) / pages if pages else None


def output_directory(value, kind):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = Path(value or f"results/{kind}-{stamp}").resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def environment(path, labels=None):
    data = {"recorded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "uname": platform.uname()._asdict(), "python": platform.python_version(),
            "effective_uid": os.geteuid(), "file": str(path),
            "file_size_bytes": path.stat().st_size,
            "filesystem_block_size": os.statvfs(path).f_frsize, "labels": labels or {}}
    for name, command in [("filesystem", ["findmnt", "--json", "--target", str(path)]),
                          ("fio_version", ["fio", "--version"])]:
        if shutil.which(command[0]):
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            data[name] = result.stdout.strip() or result.stderr.strip()
    if Path("/proc/meminfo").exists():
        data["meminfo"] = Path("/proc/meminfo").read_text()
    return data


def validate(args):
    path = owned_file(args.file)
    binary = binary_path(args.cachescope)
    size = path.stat().st_size
    page = os.sysconf("SC_PAGE_SIZE")
    if size < page * 64:
        raise ExperimentError("spatial validation needs at least 64 system pages")
    # Page aligned ranges make comparison with bins unambiguous.
    half = (size // 2 // page) * page
    quarter = (size // 4 // page) * page
    output = output_directory(args.output, "validate")
    report = {"schema_version": 1, "environment": environment(path),
              "cold_threshold_percent": args.cold_threshold, "trials": []}
    for name, offset, length, far in [
        ("first_half", 0, half, [(size * 3 // 4, size)]),
        ("middle_half", quarter, half, [(0, size // 8), (size * 7 // 8, size)]),
    ]:
        before, attempts, cold = cold_baseline(binary, path, args.cold_threshold, args.attempts)
        trial = {"name": name, "before": before, "eviction_attempts": attempts,
                 "status": "inconclusive", "reason": "baseline exceeds cold threshold"}
        if cold:
            trial["read"] = read_range(path, offset, length)
            trial["after"] = after = inspect(binary, path)
            inside = region_percent(after, offset, offset + length)
            outside = [region_percent(after, a, b) for a, b in far]
            trial["requested_region_percent"] = inside
            trial["distant_region_percents"] = outside
            if inside is not None and all(x is not None for x in outside):
                trial["status"] = "observed" if inside >= 90 and max(outside) <= 10 else "inconclusive"
                trial["reason"] = ("requested bins >=90% resident and distant bins <=10%"
                                     if trial["status"] == "observed" else
                                     "spatial criterion not met; inspect raw bins and readahead")
            else:
                trial["reason"] = "insufficient whole bins for spatial comparison"
        report["trials"].append(trial)
        save_json(output / "validation.json", report)
        print(f"{name}: {trial['status']} — {trial['reason']}")
        print(f"  before {before['map']}")
        if "after" in trial:
            print(f"  after  {trial['after']['map']}")
    print(f"Saved {output / 'validation.json'}")
    return 0 if all(t["status"] == "observed" for t in report["trials"]) else 2


def fio_command(executable, path, size, block_size, direct):
    # fio separates multiple filenames with colons, even in a single argv element.
    filename = str(path).replace("\\", "\\\\").replace(":", "\\:")
    return [executable, "--name=cachescope", f"--filename={filename}", "--rw=read",
            "--ioengine=psync", "--iodepth=1", "--numjobs=1", f"--bs={block_size}",
            f"--size={size}", f"--direct={int(direct)}", "--invalidate=0", "--readonly",
            "--allow_file_create=0", "--group_reporting=1", "--output-format=json",
            "--eta=never"]


def fio_metrics(payload):
    jobs = payload.get("jobs", [])
    if len(jobs) != 1:
        raise ExperimentError("expected one fio job")
    job = jobs[0]
    if job.get("error", 0):
        raise ExperimentError(f"fio job error {job['error']}")
    read = job["read"]
    bw_bytes = read.get("bw_bytes", read["bw"] * 1024 if "bw" in read else None)
    if bw_bytes is None or read.get("io_bytes", 0) <= 0:
        raise ExperimentError("fio returned no completed read data")
    latency = {}
    for name in ("clat", "lat"):
        for suffix, scale in (("ns", 1 / 1000), ("us", 1), ("ms", 1000)):
            if f"{name}_{suffix}" in read:
                latency[f"{name}_mean_us"] = read[f"{name}_{suffix}"]["mean"] * scale
                break
        else:
            raise ExperimentError(f"fio JSON lacks {name} latency measurements")
    return {"bandwidth_mib_s": bw_bytes / 1024**2,
            "fio_elapsed_ms": read["runtime"], "io_bytes": read["io_bytes"], **latency}


def run_fio(command, output, stem, direct):
    started = time.perf_counter()
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    wall = (time.perf_counter() - started) * 1000
    (output / f"{stem}.fio.json").write_text(result.stdout)
    (output / f"{stem}.fio.stderr.txt").write_text(result.stderr)
    payload = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    errors = [job.get("error", 0) for job in payload.get("jobs", [])] if payload else []
    failure = result.returncode != 0 or any(errors)
    record = {"command": command, "returncode": result.returncode,
              "fio_json": f"{stem}.fio.json", "fio_stderr": f"{stem}.fio.stderr.txt",
              "wall_elapsed_ms": wall}
    if failure:
        unsupported = direct and bool(errors) and all(
            e in (0, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP) for e in errors) and any(errors)
        record.update(status="unsupported" if unsupported else "error",
                      reason=f"fio failed (job errors {errors}); see raw output")
    elif payload is None:
        record.update(status="error", reason="fio did not return valid JSON; check executable")
    else:
        try:
            record.update(fio_metrics(payload), status="ok")
        except (KeyError, TypeError, ExperimentError) as error:
            record.update(status="error", reason=str(error))
    return record


METRICS = ("bandwidth_mib_s", "fio_elapsed_ms", "wall_elapsed_ms", "clat_mean_us", "lat_mean_us")


def summarize(runs):
    summary = []
    for trial in ("first_buffered", "second_buffered", "direct"):
        records = [r for r in runs if r["trial"] == trial]
        valid = [r for r in records if r["status"] == "ok"]
        row = {"trial": trial, "valid_runs": len(valid), "total_runs": len(records)}
        for metric in METRICS:
            row[f"median_{metric}"] = statistics.median(r[metric] for r in valid) if valid else None
        for when in ("before", "after"):
            row[f"median_{when}_percent"] = (
                statistics.median(r[when]["resident_percent"] for r in valid) if valid else None)
        summary.append(row)
    return summary


def write_benchmark_report(output, report):
    report["summary"] = summarize(report["runs"])
    save_json(output / "benchmark.json", report)
    with (output / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(report["summary"][0]))
        writer.writeheader()
        writer.writerows(report["summary"])
    lines = ["# CacheScope measurements", "", "Medians include only verified trials with status `ok`.", "",
             "| Trial | Valid / total | Before % | After % | MiB/s | fio ms | clat µs | total lat µs |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(value):
        return "—" if value is None else f"{value:.3f}"
    for row in report["summary"]:
        values = [row[f"median_{key}"] for key in ("before_percent", "after_percent",
                  "bandwidth_mib_s", "fio_elapsed_ms", "clat_mean_us", "lat_mean_us")]
        lines.append(f"| {row['trial']} | {row['valid_runs']} / {row['total_runs']} | " +
                     " | ".join(fmt(v) for v in values) + " |")
    lines += ["", "See `benchmark.json` for every snapshot, eviction attempt, command, and status.",
              "Raw fio JSON and stderr are saved separately for every executed job.", ""]
    (output / "summary.md").write_text("\n".join(lines))


def benchmark(args):
    path = owned_file(args.file)
    size = path.stat().st_size
    alignment = max(4096, os.sysconf("SC_PAGE_SIZE"))
    if args.block_size < alignment or args.block_size % alignment or not size or size % args.block_size:
        raise ExperimentError(f"file size must be a positive multiple of block size; "
                              f"block size must be a multiple of {alignment} bytes")
    if args.cold_threshold >= args.warm_threshold:
        raise ExperimentError("cold threshold must be below warm threshold")
    binary = binary_path(args.cachescope)
    fio = shutil.which(args.fio)
    if not fio:
        raise ExperimentError("fio not found; install the Flexible I/O Tester")
    output = output_directory(args.output, "benchmark")
    labels = {}
    for item in args.label:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise ExperimentError("--label must be KEY=VALUE")
        labels[key] = value
    report = {"schema_version": 1, "environment": environment(path, labels),
              "settings": {"repeats": args.repeats, "block_size": args.block_size,
                           "ioengine": "psync", "iodepth": 1, "rw": "read", "invalidate": 0,
                           "cold_threshold_percent": args.cold_threshold,
                           "warm_threshold_percent": args.warm_threshold}, "runs": []}
    write_benchmark_report(output, report)
    for repeat in range(1, args.repeats + 1):
        first_succeeded = False
        for trial in ("first_buffered", "second_buffered", "direct"):
            record = {"repeat": repeat, "trial": trial}
            report["runs"].append(record)
            if trial == "second_buffered":
                before = inspect(binary, path)
                record["before"] = before
                qualified = first_succeeded and before["resident_percent"] >= args.warm_threshold
                reason = "previous read failed or starting residency is below warm threshold"
            else:
                before, attempts, qualified = cold_baseline(
                    binary, path, args.cold_threshold, args.attempts)
                record.update(before=before, eviction_attempts=attempts)
                reason = "baseline exceeds cold threshold"
            if qualified:
                command = fio_command(fio, path, size, args.block_size, trial == "direct")
                record.update(run_fio(command, output, f"{repeat:02d}-{trial}", trial == "direct"))
                record["after"] = inspect(binary, path)
                if record["status"] == "ok" and record["io_bytes"] != size:
                    record.update(status="error", reason="fio did not read the full file")
                if trial == "first_buffered":
                    first_succeeded = record["status"] == "ok"
            else:
                record.update(status="inconclusive", reason=reason)
            write_benchmark_report(output, report)
            print(f"{repeat}/{args.repeats} {trial}: {record['status']} "
                  f"(starts {before['resident_percent']:.2f}% resident)", flush=True)
    print((output / "summary.md").read_text())
    print(f"Saved {output}")
    if any(r["status"] == "error" for r in report["runs"]):
        return 1
    return 0 if all(r["status"] == "ok" for r in report["runs"]) else 2


def parser():
    root = argparse.ArgumentParser(description="CacheScope Linux experiment runner")
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="write independent sample.bin and control.bin")
    prep.add_argument("directory", type=Path)
    prep.add_argument("--size", type=size_arg, default=256 * 1024**2)
    read = commands.add_parser("read-range", help="ordinary buffered read of one file range")
    read.add_argument("file", type=Path)
    read.add_argument("--offset", type=size_arg, default=0)
    read.add_argument("--length", type=size_arg, required=True)
    drop = commands.add_parser("evict", help="fsync then request per-file DONTNEED")
    drop.add_argument("file", type=Path)
    for name in ("validate", "benchmark"):
        cmd = commands.add_parser(name, help=("verify first-half and middle-half cache maps"
                                              if name == "validate" else "repeat three fio read trials"))
        cmd.add_argument("file", type=Path)
        cmd.add_argument("--cachescope", help="path to the C inspector")
        cmd.add_argument("--output", type=Path, help="new directory; defaults to results/<timestamp>")
        cmd.add_argument("--cold-threshold", type=percentage, default=1.0)
        cmd.add_argument("--attempts", type=positive, default=3)
        if name == "benchmark":
            cmd.add_argument("--warm-threshold", type=percentage, default=95.0)
            cmd.add_argument("--repeats", type=positive, default=5)
            cmd.add_argument("--block-size", type=size_arg, default=1024**2)
            cmd.add_argument("--fio", default="fio")
            cmd.add_argument("--label", action="append", default=[], help="environment note KEY=VALUE")
    return root


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    if sys.platform != "linux":
        print("cachescope runner: Linux required; use a Linux VM or Docker image", file=sys.stderr)
        return 1
    try:
        if args.command == "prepare":
            prepare(args.directory, args.size)
        elif args.command == "read-range":
            print(json.dumps(read_range(args.file, args.offset, args.length)))
        elif args.command == "evict":
            evict(owned_file(args.file))
            print("Requested per-file eviction. Inspect to verify residency; this is advisory.")
        elif args.command == "validate":
            return validate(args)
        elif args.command == "benchmark":
            return benchmark(args)
        return 0
    except (OSError, ExperimentError, ValueError) as error:
        print(f"cachescope runner: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cachescope runner: interrupted; completed trials remain in the output directory", file=sys.stderr)
        return 130

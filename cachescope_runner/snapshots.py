"""Live monitoring and comparisons of saved, compatible residency snapshots."""

import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

from .cli import ExperimentError, binary_path, inspect, output_directory, owned_file, save_json


def validate_snapshot(data):
    """Check layout and counts before comparing externally supplied JSON."""
    def integer(value):
        return type(value) is int and value >= 0

    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ExperimentError("expected an inspector snapshot with schema_version 1")
    if data.get("ownership_verified") is not True:
        raise ExperimentError("snapshot ownership is unverified")
    for key in ("size_bytes", "page_size", "total_pages", "resident_pages", "device", "inode"):
        if not integer(data.get(key)):
            raise ExperimentError(f"snapshot has invalid {key}")
    size, page = data["size_bytes"], data["page_size"]
    offset, length = data.get("offset_bytes", 0), data.get("length_bytes", size)
    if not page or not integer(offset) or not integer(length) or offset > size or length > size - offset:
        raise ExperimentError("snapshot range is invalid")
    if offset % page or data["total_pages"] != (length + page - 1) // page:
        raise ExperimentError("snapshot range and page count disagree")
    bins, cache_map = data.get("bins"), data.get("map")
    if not isinstance(bins, list) or not isinstance(cache_map, str) or len(bins) != len(cache_map):
        raise ExperimentError("snapshot bins and map disagree")
    if len(bins) > 4096 or not isinstance(data.get("file"), str):
        raise ExperimentError("snapshot file or bin count is invalid")
    next_page, total, resident = offset // page, 0, 0
    for index, bin in enumerate(bins):
        keys = ("start_page", "page_count", "offset_bytes", "length_bytes", "resident_pages")
        if not isinstance(bin, dict) or not all(integer(bin.get(k)) for k in keys):
            raise ExperimentError("snapshot has an invalid bin")
        count, cached = bin["page_count"], bin["resident_pages"]
        if (not count or cached > count or count > data["total_pages"] - total or
                bin["start_page"] != next_page or bin["offset_bytes"] != next_page * page or
                bin["length_bytes"] != min(count * page, offset + length - next_page * page)):
            raise ExperimentError("snapshot bins do not partition the inspected range")
        symbol = "." if not cached else "#" if cached == count else "+"
        if cache_map[index] != symbol:
            raise ExperimentError("snapshot map does not match its bin counts")
        total += count
        resident += cached
        next_page += count
    if total != data["total_pages"] or resident != data["resident_pages"]:
        raise ExperimentError("snapshot totals do not match its bins")
    percent = data.get("resident_percent")
    expected = 100 * resident / total if total else 0
    if (type(percent) not in (int, float) or not 0 <= percent <= 100 or
            abs(percent - expected) > 0.000001):
        raise ExperimentError("snapshot residency percentage disagrees with its counts")
    return data


def compare_snapshots(before, after):
    validate_snapshot(before)
    validate_snapshot(after)
    if (before["device"], before["inode"]) != (after["device"], after["inode"]):
        raise ExperimentError("snapshots refer to different underlying files (device/inode)")
    for key in ("size_bytes", "page_size"):
        if before[key] != after[key]:
            raise ExperimentError(f"snapshot {key} changed; capture a new baseline")
    def layout(data):
        return [(b["start_page"], b["page_count"], b["offset_bytes"], b["length_bytes"])
                for b in data["bins"]]
    if (before.get("offset_bytes", 0), before.get("length_bytes", before["size_bytes"])) != (
            after.get("offset_bytes", 0), after.get("length_bytes", after["size_bytes"])) or layout(before) != layout(after):
        raise ExperimentError("snapshot layouts differ; use the same width, offset, and length")
    changes, symbols = [], []
    for left, right in zip(before["bins"], after["bins"]):
        delta = right["resident_pages"] - left["resident_pages"]
        symbols.append("+" if delta > 0 else "-" if delta < 0 else ".")
        if delta:
            changes.append({"offset_bytes": right["offset_bytes"], "length_bytes": right["length_bytes"],
                            "before_pages": left["resident_pages"], "after_pages": right["resident_pages"],
                            "net_resident_pages": delta})
    return {"schema_version": 1, "kind": "cache_diff", "file": after["file"],
            "offset_bytes": after.get("offset_bytes", 0),
            "length_bytes": after.get("length_bytes", after["size_bytes"]),
            "before_resident_pages": before["resident_pages"], "after_resident_pages": after["resident_pages"],
            "net_resident_pages": after["resident_pages"] - before["resident_pages"],
            "before_percent": before["resident_percent"], "after_percent": after["resident_percent"],
            "before_map": before["map"], "after_map": after["map"], "delta_map": "".join(symbols),
            "warmer_bins": symbols.count("+"), "cooler_bins": symbols.count("-"),
            "unchanged_bins": symbols.count("."), "changes": changes}


def diff_command(args):
    before = json.loads(Path(args.before).read_text())
    after = json.loads(Path(args.after).read_text())
    result = compare_snapshots(before, after)
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
        return 0
    print(f"File         {result['file']}")
    print(f"Range        [{result['offset_bytes']}, {result['offset_bytes'] + result['length_bytes']}) bytes")
    print(f"Resident     {result['before_percent']:.1f}% → {result['after_percent']:.1f}% "
          f"({result['net_resident_pages']:+,} pages net)")
    print(f"Before       {result['before_map']}\nAfter        {result['after_map']}")
    print(f"Change       {result['delta_map']}\n             + warmer  - cooler  . same net count")
    changes = sorted(sorted(result["changes"], key=lambda b: abs(b["net_resident_pages"]), reverse=True)
                     [:args.limit], key=lambda b: b["offset_bytes"])
    if changes:
        print("\nLargest changed regions (absolute file offsets):")
        for bin in changes:
            end = bin["offset_bytes"] + bin["length_bytes"]
            print(f"  [{bin['offset_bytes']:,}, {end:,}) bytes: {bin['net_resident_pages']:+,} pages net")
        if len(result["changes"]) > len(changes):
            print(f"  … {len(result['changes']) - len(changes)} more; use --json for all regions")
    print("\nChanges are net counts per bin; page turnover within a bin can be hidden.")
    return 0


def color_map(value, enabled, delta=False):
    if not enabled:
        return value
    palette = {"#": "32", "+": "33", ".": "90"} if not delta else {"+": "32", "-": "31", ".": "90"}
    return "".join(f"\033[{palette[symbol]}m{symbol}\033[0m" for symbol in value)


def watch(args):
    path, binary = owned_file(args.file), binary_path(args.cachescope)
    saved = output_directory(args.save, "watch") if args.save is not None else None
    if saved:
        print(f"Saving snapshots to {saved}", file=sys.stderr)
    interactive = sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and not args.plain
    color = interactive and not args.no_color and "NO_COLOR" not in os.environ
    previous, index = None, 0
    while args.count is None or index < args.count:
        started = time.monotonic()
        data = validate_snapshot(inspect(binary, path, args.width, offset=args.offset, length=args.length))
        index += 1
        captured = dt.datetime.now(dt.timezone.utc).isoformat()
        data["captured_at_utc"] = captured
        reset_reason, delta = "first sample", None
        if previous is not None:
            try:
                delta = compare_snapshots(previous, data)
                reset_reason = None
            except ExperimentError as error:
                reset_reason = str(error)
        event = {"sample": index, "captured_at_utc": captured, "snapshot": data,
                 "net_resident_pages": delta["net_resident_pages"] if delta else None,
                 "delta_map": delta["delta_map"] if delta else None, "reset_reason": reset_reason}
        if saved:
            save_json(saved / f"snapshot-{index:06d}.json", data)
            with (saved / "timeline.jsonl").open("a") as stream:
                stream.write(json.dumps(event, ensure_ascii=True) + "\n")
        if args.jsonl:
            print(json.dumps(event, ensure_ascii=True), flush=True)
        else:
            if interactive:
                print("\033[2J\033[H", end="")
            offset = data.get("offset_bytes", 0)
            length = data.get("length_bytes", data["size_bytes"])
            print(f"CacheScope live · sample {index} · {captured}")
            print(f"File         {data['file']}\nRange        [{offset}, {offset + length}) bytes")
            print(f"Resident     {data['resident_pages']:,} / {data['total_pages']:,} pages "
                  f"({data['resident_percent']:.1f}%)")
            print(f"Cache map    {color_map(data['map'], color) or '(empty)'}")
            if delta:
                print(f"Change       {color_map(delta['delta_map'], color, delta=True) or '(empty)'} "
                      f"({delta['net_resident_pages']:+,} pages net)")
            else:
                print(f"Baseline     {reset_reason}")
            print("             . none  + partial  # all; change: + warmer  - cooler  . same")
            print("Ctrl-C to stop.\n", flush=True)
        previous = data
        if args.count is None or index < args.count:
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    return 0

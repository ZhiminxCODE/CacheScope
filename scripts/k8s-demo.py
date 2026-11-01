#!/usr/bin/env python3
"""Collect shared-inode and separate-inode control observations from two pods."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachescope_runner.cli import output_directory, region_percent, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="kind-cachescope")
    parser.add_argument("--namespace", default="cachescope")
    parser.add_argument("--output")
    args = parser.parse_args()
    base = ["kubectl", "--context", args.context, "--namespace", args.namespace]

    def run(*command):
        result = subprocess.run([*base, *command], check=True, text=True, capture_output=True)
        return result.stdout

    def execute(pod, *command):
        return run("exec", pod, "--", *command)

    def inspect(pod, file):
        snapshot = json.loads(execute(pod, "cachescope", "inspect", "--json", file))
        if not snapshot["ownership_verified"]:
            raise RuntimeError("file owner must match the pods' numeric UID")
        return snapshot

    def cold(file):
        snapshots = []
        for _ in range(3):
            execute("reader-a", "python3", "-m", "cachescope_runner", "evict", file)
            snapshots.append(inspect("observer-b", file))
            if snapshots[-1]["resident_percent"] <= 1:
                return snapshots, True
        return snapshots, False

    output = output_directory(args.output, "k8s")
    pods = json.loads(run("get", "pods", "reader-a", "observer-b", "-o", "json"))
    nodes = {p["spec"]["nodeName"] for p in pods["items"]}
    if len(nodes) != 1:
        raise RuntimeError("both pods must be on the same node")
    report = {"schema_version": 1, "context": args.context, "pods": pods, "trials": []}
    sample, control = "/data/sample.bin", "/data/control.bin"
    a = inspect("reader-a", sample)
    b = inspect("observer-b", sample)
    c = inspect("observer-b", control)
    if (a["device"], a["inode"]) != (b["device"], b["inode"]):
        raise RuntimeError("pods do not see the same underlying sample file")
    if (b["device"], b["inode"]) == (c["device"], c["inode"]):
        raise RuntimeError("control must be a separate inode, not a hard link")
    if b["size_bytes"] != c["size_bytes"]:
        raise RuntimeError("sample and control must have the same size")
    report["identities"] = {"reader_sample": a, "observer_sample": b, "observer_control": c}
    # Hash before resetting cache; hashing during a trial would contaminate it.
    hashes = execute("reader-a", "sha256sum", sample, control).splitlines()
    if len(hashes) != 2 or hashes[0].split()[0] != hashes[1].split()[0]:
        raise RuntimeError("sample and control contents differ")
    report["sha256"] = {sample: hashes[0].split()[0], control: hashes[1].split()[0]}
    half = b["size_bytes"] // 2 // b["page_size"] * b["page_size"]
    if half == 0:
        raise RuntimeError("file is too small for a half-file read")
    for name, observed_file in [("shared_file", sample), ("separate_inode_control", control)]:
        # Always reset both, including the file A will read during the control.
        sample_attempts, sample_cold = cold(sample)
        control_attempts, control_cold = cold(control)
        before = inspect("observer-b", observed_file)
        trial = {"name": name, "before": before, "status": "inconclusive",
                 "sample_eviction_attempts": sample_attempts,
                 "control_eviction_attempts": control_attempts}
        report["trials"].append(trial)
        if sample_cold and control_cold and before["resident_percent"] <= 1:
            trial["reader"] = json.loads(execute("reader-a", "python3", "-m", "cachescope_runner",
                                                  "read-range", sample, "--length", str(half)))
            trial["after"] = after = inspect("observer-b", observed_file)
            trial["reader_after"] = reader_after = inspect("reader-a", sample)
            heated = region_percent(reader_after, 0, half)
            if name == "shared_file":
                inside = region_percent(after, 0, half)
                distant = region_percent(after, b["size_bytes"] * 3 // 4, b["size_bytes"])
                observed = inside is not None and distant is not None and inside >= 90 and distant <= 10
            else:
                observed = heated is not None and heated >= 90 and after["resident_percent"] <= 1
            trial["status"] = "observed" if observed else "inconclusive"
        print(f"{name}: {trial['status']}")
        print(f"  before {before['map']}")
        if "after" in trial:
            print(f"  after  {trial['after']['map']}")
        save_json(output / "k8s-demo.json", report)
    print(f"Saved {output / 'k8s-demo.json'}")
    return 0 if all(t["status"] == "observed" for t in report["trials"]) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, KeyError) as error:
        print(f"k8s demo: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        raise SystemExit(1)

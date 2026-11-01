# Same file, two pods

Use a dedicated local single-node cluster. Both pods mount only
`/var/local/cachescope-data` from the **Kubernetes node** at `/data`, read-only.
They run as UID:GID 1000:1000 with dropped capabilities and no privilege escalation.
They share the node's kernel and underlying sample file.

`hostPath` is suitable for this deliberately scoped local experiment. It is tied
to a node and has security implications; this is not a production storage design.
An independent `emptyDir` in each pod would give different files. See
[Kubernetes hostPath documentation](https://kubernetes.io/docs/concepts/storage/volumes/#hostpath).

## Automated setup

```sh
./scripts/setup-kind.sh
python3 scripts/k8s-demo.py --output results/two-pods
```

Setup requires `docker`, `kind`, and `kubectl`. It creates `cachescope` and selects
`kind-cachescope` explicitly. The data directory lives inside the Linux kind node;
it is not a Mac directory. No host filesystem mount configuration is needed.

The preparation Job writes two byte-identical 256 MiB files independently and
fsyncs them. Preparation is the only pod with a writable data mount. It never
overwrites data. The data UID matches both reader/observer pods.

The collector verifies that both pods are on the same node, checks the sample's
device/inode identity across pods, verifies the control is separate and identical
in content, and then resets cache state. Each experiment requires measured ≤1%
residency after up to three eviction attempts. Cold setup failure is inconclusive.

For the shared file, the requested first-half bins must become ≥90% resident while
the distant last quarter stays ≤10%. For the control, A's requested region must
become ≥90% resident and B's control must remain ≤1% resident. These conservative
criteria leave room for readahead and report `inconclusive` when not met.

`k8s-demo.json` records pod specs, nodes, file identities, hashes, every eviction
attempt, before/after snapshots, reader activity, and observed/inconclusive status.
It also saves the numeric criteria, a `measurements` object for each trial, and a
`reason` explaining every outcome. Baseline percentages identify whether the sample,
control, or observer missed the cold threshold. After-read percentages identify
insufficient warming, excessive distant residency, or a control that warmed. A
range with no whole comparison bins is explicitly inconclusive. For example:

```text
separate_inode_control: inconclusive — control started at 3.000% resident, above 1%
```

Exit 0 means both observations met the criteria, 2 means inconclusive, 1 an error.

## Manual live demo

After setup, use these in separate terminals, or sequentially:

For a refreshing observer view, run this in terminal B while terminal A executes
the eviction and read commands below. Stop the monitor with Ctrl-C. Use `--plain`
for append-only output, or `--jsonl --count 20` to capture a bounded stream on the
client. The observer mount is read-only, so recordings should be redirected to
the client rather than saved inside `/data`.

```sh
kubectl --context kind-cachescope -n cachescope exec -it observer-b -- \
  python3 -m cachescope_runner watch /data/sample.bin --interval 0.5

# Capture on the client without a TTY:
kubectl --context kind-cachescope -n cachescope exec observer-b -- \
  python3 -m cachescope_runner watch /data/sample.bin --jsonl --count 20 > observer.jsonl
```

```sh
# Reset and inspect the shared file.
kubectl --context kind-cachescope -n cachescope exec reader-a -- \
  python3 -m cachescope_runner evict /data/sample.bin
kubectl --context kind-cachescope -n cachescope exec observer-b -- \
  cachescope inspect /data/sample.bin --width 32

# A reads; B inspects the same inode.
kubectl --context kind-cachescope -n cachescope exec reader-a -- \
  python3 -m cachescope_runner read-range /data/sample.bin --length 128MiB
kubectl --context kind-cachescope -n cachescope exec observer-b -- \
  cachescope inspect /data/sample.bin --width 32

# Reset both files, verify both baselines, then run the control.
kubectl --context kind-cachescope -n cachescope exec reader-a -- \
  python3 -m cachescope_runner evict /data/sample.bin
kubectl --context kind-cachescope -n cachescope exec reader-a -- \
  python3 -m cachescope_runner evict /data/control.bin
kubectl --context kind-cachescope -n cachescope exec observer-b -- \
  cachescope inspect /data/sample.bin --width 32
kubectl --context kind-cachescope -n cachescope exec observer-b -- \
  cachescope inspect /data/control.bin --width 32
kubectl --context kind-cachescope -n cachescope exec reader-a -- \
  python3 -m cachescope_runner read-range /data/sample.bin --length 128MiB
kubectl --context kind-cachescope -n cachescope exec observer-b -- \
  cachescope inspect /data/control.bin --width 32
```

The same inode can share cache across the pods. Identical bytes in another inode
are the control. This shows observable cache behavior in this setup, not general
access to another pod's private files or a reliable attribution of a reader.

## Resume, troubleshoot, clean up

```sh
kubectl --context kind-cachescope -n cachescope get pods,jobs -o wide
kubectl --context kind-cachescope -n cachescope logs job/prepare-data
kubectl --context kind-cachescope -n cachescope describe pod observer-b
```

If cluster creation succeeded but setup stopped, resume the remaining commands
from `scripts/setup-kind.sh` manually. Do not rerun a preparation Job against
existing files. Existing data can be inspected and reused after eviction. Image
updates require `kind load docker-image cachescope:local --name cachescope`, then
recreation of the two pods. Numeric ownership must match; do not hide this with
`--allow-unverified`. If the hostPath is missing, create it inside
`cachescope-control-plane`, then set its owner as shown in setup.

If you adapt to another cluster, pin **both** pods to one node with the
`cachescope-demo=true` label. Prepare the files on that node's Linux filesystem.
For explicit host-to-node mounts, consult
[kind extra mounts](https://kind.sigs.k8s.io/docs/user/configuration/#extra-mounts).

After copying results, this command deletes the dedicated cluster and its data:

```sh
kind delete cluster --name cachescope
```

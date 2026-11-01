# Optional EC2 / EBS reproduction

Use one Linux EC2 instance and an already mounted EBS-backed directory dedicated
to this experiment. This repository does not provision cloud resources, format
volumes, or incur cloud charges automatically.

Install Docker on that host, clone/copy the repository, and build the same image.
Create an empty test directory, for example `$HOME/cachescope-data`, owned by your
current numeric UID. Use the same image architecture and fio settings as your
local experiment where possible. Run on the EBS filesystem, not tmpfs.

```sh
docker build -t cachescope:local .
mkdir -p "$HOME/cachescope-data"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$HOME/cachescope-data:/data" cachescope:local prepare /data --size 256MiB
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$HOME/cachescope-data:/data" cachescope:local validate /data/sample.bin --output /data/spatial
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$HOME/cachescope-data:/data" cachescope:local benchmark /data/sample.bin \
  --repeats 5 --output /data/benchmark \
  --label environment=ec2 --label instance_type=RECORD_ACTUAL_TYPE \
  --label ebs_volume_type=RECORD_ACTUAL_TYPE --label ebs_iops=RECORD_ACTUAL_IOPS \
  --label ebs_throughput_mib_s=RECORD_ACTUAL_THROUGHPUT
```

Replace labels with observed values. Record the instance type, EBS type, size,
provisioned IOPS and throughput, kernel, filesystem, Docker image ID, image
architecture, memory limits, file size, fio version, block size, I/O engine,
queue depth, region, and any other running workload. Kernel, mount information,
memory, file size, and fio version are captured automatically where available.
Retain raw JSON alongside the median table. Do not publish credentials or metadata
service tokens in result files.

Compare the **relationship between measured starting residency and read performance**
across environments. Raw bandwidth is affected by CPU, VM, EBS, filesystem, and
container overhead; a speed difference alone does not isolate page-cache effects.
Report unsupported direct I/O and failed cold baselines rather than dropping them.
Stop or remove resources you created when finished, according to your AWS setup.

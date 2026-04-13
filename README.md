# S3 Camera Sync

Python utilities that sync files from a local FTP-style directory tree to Amazon S3, verify uploads, delete successful copies to save disk, and retry failures. Full behavior, cron examples, and layout are in [docs.md](docs.md).

## Running on Amazon EC2

Use a Linux instance in the same AWS account (and usually the same Region) as your S3 bucket so latency and data transfer stay simple.

### 1. Launch the instance

- **AMI:** Amazon Linux 2023 or Ubuntu 22.04 LTS (any recent Linux with Python 3 is fine).
- **Instance type:** Match your FTP load and how many user folders upload in parallel; see [Tuning CPU, processes, and threads](#tuning-cpu-processes-and-threads-ec2-t2--t3) below. A **t3.small** or **t3.medium** is a reasonable starting point for modest camera traffic.
- **Storage:** Size the root (or a separate EBS volume) for your local FTP tree (`BASE_DIR`), spool space, and logs. The sync deletes files from disk after a successful upload, but spikes and `failed/` quarantine still need headroom.
- **Security group:** Allow **SSH (22)** from your IP (or a bastion). Open any ports your FTP/SFTP stack needs (e.g. 21, passive FTP range) only from trusted sources.

### 2. IAM permissions (recommended)

Attach an **IAM instance profile** to the EC2 instance instead of putting long‑lived access keys on the disk. Grant the role at least:

- `s3:PutObject`, `s3:GetObject`, `s3:HeadObject` on `arn:aws:s3:::<your-bucket>/*` (tighten with a prefix if you use one).

With a role attached, you do **not** need `aws configure` on the instance unless you also use the CLI for something else.

### 3. Install Python and boto3

Use **Python 3.10+** (reference **3.12**). On **Amazon Linux 2023**, default `python3` is often still **3.9**; install **`python3.12`** and use that for cron so you avoid [AWS SDK 3.9 deprecation](https://aws.amazon.com/blogs/developer/python-support-policy-updates-for-aws-sdks-and-tools/).

**Amazon Linux 2023:**

```bash
sudo dnf install -y python3.12 python3.12-pip
sudo python3.12 -m pip install --upgrade pip
sudo python3.12 -m pip install boto3==1.42.88 botocore==1.42.88
```

**Ubuntu:**

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip
sudo python3.12 -m pip install --upgrade pip
sudo python3.12 -m pip install boto3==1.42.88 botocore==1.42.88
```

Ensure the user that runs cron can import boto3 (install with `sudo` as above for root’s crontab, or use `python3.12 -m pip install --user` under that user). Full upgrade steps from an old 3.9 install are in [setup.md](setup.md#upgrading-an-existing-host-eg-from-python-39--old-pip).

### 4. Deploy the scripts and directories

On the instance, copy or clone this repo, then:

```bash
sudo mkdir -p /opt/camera_sync /var/log/camera
sudo cp camera_sync.py retry_failed.py /opt/camera_sync/
sudo chmod +x /opt/camera_sync/*.py
```

Create your FTP root if it does not exist (default in the scripts is `/var/ftp/local`):

```bash
sudo mkdir -p /var/ftp/local
sudo chown -R <ftp-or-app-user>:<ftp-or-app-user> /var/ftp/local
sudo chown <ftp-or-app-user>:<ftp-or-app-user> /var/log/camera
```

Edit **`/opt/camera_sync/camera_sync.py`** and **`/opt/camera_sync/retry_failed.py`**: set `BASE_DIR`, `BUCKET`, `PREFIX`, and `LOG_DIR` to match this host. Keep `MAX_PROCESS_WORKERS` / `MAX_THREAD_WORKERS` aligned between the two files if you tune them.

**Lock files** default to `/var/run/camera_sync.lock` and `/var/run/retry_failed.lock`. On many distros only root can create files there. If cron runs as a non-root user and the job fails to create the lock, use **root’s crontab** (`sudo crontab -e`) or change `LOCKFILE` in both scripts to a path that user can write (for example under `/var/lib/camera_sync/`).

### 5. Schedule runs (cron)

Follow [docs.md — Cron Setup](docs.md#cron-setup): sync every 5 minutes and the retry job hourly. Use the same interpreter you used to verify `boto3` (on AL2023 often `/usr/bin/python3.12`).

### 6. Verify

- Run once manually: `python3.12 /opt/camera_sync/camera_sync.py` (or your chosen `PY`) and check `/var/log/camera/sync.log`.
- After cron is active, use the heartbeat and log paths described in [docs.md — Monitoring](docs.md#monitoring).

For S3 request-rate notes and burstable-instance behavior, see the tuning section below and [docs.md](docs.md).

## Tuning CPU, processes, and threads (EC2 T2 / T3)

Both `camera_sync.py` and `retry_failed.py` use:

- **`MAX_PROCESS_WORKERS`** — how many **user directories** run in parallel (`ProcessPoolExecutor`). Each process is a separate Python interpreter; it uses RAM and competes for **CPU credits** on burstable instances.
- **`MAX_THREAD_WORKERS`** — how many **files per user** upload concurrently inside that process (`ThreadPoolExecutor`). S3 uploads are mostly **network I/O**, so you can often use **more threads than vCPUs**, but very high values increase memory and can trigger throttling.

Set both at the **top of each script** (they are independent files; keep values in sync if you want the same behavior):

```python
# None = auto (see docs.md). Or set integers:
MAX_PROCESS_WORKERS = 2 # cap parallel user processes
MAX_THREAD_WORKERS = 8    # cap concurrent S3 uploads per user process
```

On Linux, auto mode uses `os.cpu_count()` (logical **vCPUs**). Check what the OS sees:

```bash
nproc
python3.12 -c "import os; print(os.cpu_count())"
```

### vCPU reference (T2 vs T3)

| Size | T2 vCPUs | T3 vCPUs |
|----------|----------|----------|
| nano     | 1        | 2        |
| micro    | 1        | 2        |
| small    | 1        | 2        |
| medium   | 2        | 2        |
| large    | 2        | 2        |
| xlarge   | 4        | 4        |
| 2xlarge  | 8        | 8        |

T2/T3 are **burstable**: sustained high CPU can exhaust **CPU credits** and throttle the instance. Favor **lower `MAX_PROCESS_WORKERS`** and moderate threads on **nano / micro / small** if you see slow SSH, high `steal` time, or credit balance dropping in CloudWatch.

### Suggested starting points by instance “tier”

Use these as a first pass, then watch CPU credit balance, memory, and `sync.log` latency. **Conservative** reduces parallel work; **aggressive** favors throughput on larger or unlimited-credit modes.

| Tier (typical sizes) | `MAX_PROCESS_WORKERS` | `MAX_THREAD_WORKERS` | Notes |
|----------------------|------------------------|----------------------|--------|
| **Small** (t2/t3 nano, micro, small) | `1` | `4`–`8` | One user process at a time avoids RAM/CPU spikes; threads still overlap S3 I/O. |
| **Medium** (t2/t3 medium, large) | `2` | `8`–`12` | Matches 2 vCPUs; good default when several user folders exist. |
| **High** (t3 xlarge, 2xlarge) | `4`–`8` (≤ vCPUs) | `12`–`20` | More parallel users; threads toward upper range only if memory is ample. |

**Examples**

- **t3.medium** (2 vCPUs), several camera users: `MAX_PROCESS_WORKERS = 2`, `MAX_THREAD_WORKERS = 10`.
- **t2.micro** (1 vCPU), tight credits: `MAX_PROCESS_WORKERS = 1`, `MAX_THREAD_WORKERS = 6`.
- **t3.xlarge** (4 vCPUs), many users: `MAX_PROCESS_WORKERS = 4`, `MAX_THREAD_WORKERS = 16`.

If **`MAX_PROCESS_WORKERS`** is higher than the number of user directories, the extra workers stay idle. If it is higher than **vCPUs**, you may still benefit (I/O-bound uploads) but CPU-bound steps (MD5, gzip if enabled elsewhere) can contend—on T2/T3, prefer **`MAX_PROCESS_WORKERS <= vCPUs`** unless you have measured headroom.

### AWS side: S3 request rate

Parallel threads increase **S3 PUT / HEAD** request rate. For very high concurrency across many instances, use a single prefix per bucket or follow S3 request rate guidance for your prefix layout. For typical camera workloads on one FTP host, the table above is usually safe.

---

## Quick links

- [docs.md](docs.md) — installation, cron (5 min sync / hourly retry), configuration table, monitoring.

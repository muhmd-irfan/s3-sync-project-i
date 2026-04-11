# S3 Camera Sync

Python utilities that sync files from a local FTP-style directory tree to Amazon S3, verify uploads, delete successful copies to save disk, and retry failures. Full behavior, cron examples, and layout are in [docs.md](docs.md).

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
python3 -c "import os; print(os.cpu_count())"
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

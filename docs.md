# Camera Sync — S3 Upload Pipeline

Two Python scripts that move files from a local FTP directory tree into an
AWS S3 bucket, verify each upload, and handle failures with automatic retries.

---

## Scripts

### 1. `camera_sync.py` — Primary Sync (runs every 5 min)

**Purpose:** Find new files that cameras/devices have dropped into user
directories, upload them to S3, verify the upload, and delete the local copy to
free disk space.

**Workflow per user directory:**

```
BASE_DIR/
└── <username>/
    ├── subdir/image1.jpg   ← new files land here
    ├── .spool/             ← staging area during upload
    └── failed/             ← quarantine for upload failures
```

| Step       | What happens                                                                                         |
|------------|------------------------------------------------------------------------------------------------------|
| **Recover**| Files left in `.spool/` from a crash are checked on S3 first; if already uploaded, the local copy is deleted. Otherwise they are moved back into the user root. |
| **Scan**   | Walks the user directory. Picks files matching extensions (`.jpg`, `.jpeg`, `.png`, `.txt`) that are older than `MIN_AGE_SEC` (120 s), pass magic-byte validation, and have safe paths (no `..`, no symlinks). Skips `.spool/` and `failed/`. |
| **Spool**  | Atomically moves eligible files into `.spool/<relative_path>` to prevent race conditions with FTP writers. |
| **Upload** | Streams each spooled file to `s3://<BUCKET>/<PREFIX>/<username>/<relative_path>` via `boto3` in a single disk read (MD5 computed while uploading). Retries up to `MAX_RETRIES` on transient failures. |
| **Verify** | Calls `head_object` on S3 and compares **file size** and **MD5 vs ETag**.                            |
| **Cleanup**| **Success →** local file is **deleted** (saves storage). **Failure →** file is moved to `failed/` and a JSON line is written to `upload_failures.jsonl` for Grafana. |

**Concurrency model:**

- `ProcessPoolExecutor` — one process per user directory (scales to `cpu_count`).
- `ThreadPoolExecutor` inside each process — concurrent S3 uploads (I/O-bound), scales between 4–20 threads based on file count.

**Log files (all in `LOG_DIR`):**

| File                     | Content                                      |
|--------------------------|----------------------------------------------|
| `sync.log`               | Full debug-level activity                    |
| `error.log`              | Warnings and errors only                     |
| `cron_alive.log`         | One-line heartbeat per run (rotated)         |
| `upload_failures.jsonl`  | One JSON object per upload failure (Grafana) |

**Lock file:** `/var/run/camera_sync.lock` — prevents overlapping runs.  If a
previous cron invocation is still running, the new one exits silently.

---

### 2. `retry_failed.py` — Failed-File Retry (runs every hour)

**Purpose:** Examine every file sitting in a `failed/` directory, diagnose
*why* it failed, attempt up to 3 re-uploads, and delete the local copy on
success.

**Diagnosis outcomes (logged per file):**

| Reason              | Meaning                                                        | Action taken         |
|---------------------|----------------------------------------------------------------|----------------------|
| `already_on_s3`     | File actually exists and matches — previous cleanup missed it  | Delete local copy    |
| `not_on_s3`         | File never reached S3                                          | Re-upload (3 tries)  |
| `s3_size_mismatch`  | Exists on S3 but sizes differ (partial upload)                 | Re-upload (3 tries)  |
| `s3_md5_mismatch`   | Exists on S3 but checksum differs (corruption)                 | Re-upload (3 tries)  |
| `local_unreadable`  | Can't read the local file (permissions, disk error)            | Skip, log error      |
| `s3_access_error`   | AWS permissions or network issue                               | Skip, log error      |

**Concurrency model:** Same as `camera_sync.py` — processes per user, threads
per file.

**Log files:**

| File               | Content                          |
|--------------------|----------------------------------|
| `retry.log`        | Full debug-level retry activity  |
| `retry_error.log`  | Warnings and errors only         |

**Lock file:** `/var/run/retry_failed.lock` — independent from the sync lock,
so both scripts can safely run at the same time.

---

## Configuration

Both scripts read settings from **environment variables** via `sync_common.load_config()`.
Set them in cron, a systemd unit, or `/opt/camera_sync/.env` sourced before launch.
See [.env.example](.env.example).

| Environment variable              | Default                          | Required | Description                                    |
|-----------------------------------|----------------------------------|----------|------------------------------------------------|
| `CAMERA_SYNC_BUCKET`              | —                                | **yes**  | Target S3 bucket                               |
| `CAMERA_SYNC_BASE_DIR`            | `/var/ftp/local`                 | no       | Root of the FTP directory tree                 |
| `CAMERA_SYNC_PREFIX`              | `cam`                            | no       | S3 key prefix (`s3://bucket/PREFIX/user/...`)  |
| `CAMERA_SYNC_LOG_DIR`             | `/var/log/camera`                | no       | Where all log files are written                |
| `CAMERA_SYNC_PROJECT`             | `camera-sync`                    | no       | Project label in Grafana failure logs          |
| `CAMERA_SYNC_MIN_AGE_SEC`         | `120`                            | no       | Ignore files younger than this (seconds)       |
| `CAMERA_SYNC_MAX_RETRIES`         | `3`                              | no       | Upload attempts before quarantine              |
| `CAMERA_SYNC_TIMEOUT_SEC`         | `480`                            | no       | Max sync runtime (Linux SIGALRM; 0 = disabled) |
| `CAMERA_SYNC_MAX_PROCESS_WORKERS` | auto                             | no       | Cap process pool size                          |
| `CAMERA_SYNC_MAX_THREAD_WORKERS`  | auto                             | no       | Cap thread pool size per process               |
| `CAMERA_SYNC_FAILURE_LOG`         | `{LOG_DIR}/upload_failures.jsonl`| no       | Grafana-oriented failure log path              |

Supported file extensions: `.jpg`, `.jpeg`, `.png`, `.txt` (images are magic-byte validated).

For **EC2 T2 / T3** sizing (nano through 2xlarge), burstable CPU credits, and
example values for small / medium / high tiers, see [README.md](README.md).

---

## Installation

```bash
# 1. Install Python dependencies
pip install boto3

# 2. Make sure AWS credentials are configured
#    (via environment variables, ~/.aws/credentials, or an IAM instance role)
aws configure

# 3. Create the log directory
sudo mkdir -p /var/log/camera
sudo chown $(whoami):$(whoami) /var/log/camera

# 4. Copy scripts to a known location
sudo cp camera_sync.py retry_failed.py sync_common.py /opt/camera_sync/
sudo chmod +x /opt/camera_sync/*.py

# 5. Set required environment (example — use your bucket and project name)
export CAMERA_SYNC_BUCKET=your-bucket-name
export CAMERA_SYNC_PROJECT=binalapse-cameras
```

---

## Cron Setup

Open the crontab editor:

```bash
crontab -e
```

Add these two lines:

```cron
# Environment for both jobs (or source /opt/camera_sync/env.sh)
CAMERA_SYNC_BUCKET=your-bucket-name
CAMERA_SYNC_PROJECT=binalapse-cameras

# Primary sync — every 5 minutes (optional: timeout 480 as belt-and-suspenders)
*/5 * * * * CAMERA_SYNC_BUCKET=your-bucket-name CAMERA_SYNC_PROJECT=binalapse-cameras /usr/bin/python3.12 /opt/camera_sync/camera_sync.py >> /var/log/camera/cron_sync.log 2>&1

# Failed-file retry — every hour at minute 30
30 * * * * CAMERA_SYNC_BUCKET=your-bucket-name CAMERA_SYNC_PROJECT=binalapse-cameras /usr/bin/python3.12 /opt/camera_sync/retry_failed.py >> /var/log/camera/cron_retry.log 2>&1
```

Save and exit. Verify with:

```bash
crontab -l
```

> **Note:** The lock files ensure that even if a run takes longer than the cron
> interval, the next invocation exits immediately instead of piling up.

---

## Directory Layout After Running

```
/var/ftp/local/
├── user_a/
│   ├── cam01/               ← active upload area (files arrive here)
│   ├── .spool/              ← temporary staging (empty between runs)
│   └── failed/              ← quarantined files (retry_failed.py handles these)
├── user_b/
│   └── ...
/var/log/camera/
├── sync.log                 ← camera_sync.py full log
├── error.log                ← camera_sync.py errors
├── retry.log                ← retry_failed.py full log
├── retry_error.log          ← retry_failed.py errors
├── cron_alive.log           ← heartbeat from camera_sync.py (rotated)
├── upload_failures.jsonl    ← structured upload failures for Grafana
├── cron_sync.log            ← cron stdout/stderr for sync
└── cron_retry.log           ← cron stdout/stderr for retry
```

---

## Monitoring

### Grafana / Loki (upload failures)

Each upload failure from `camera_sync.py` or `retry_failed.py` appends **one JSON line** to `upload_failures.jsonl`. Point your log shipper (Promtail, Fluent Bit, etc.) at this file and parse JSON fields.

Example record:

```json
{
  "timestamp": "2026-06-25T14:30:00Z",
  "level": "error",
  "event": "upload_failed",
  "project": "binalapse-cameras",
  "script": "camera_sync",
  "camera": "cam042",
  "file_path": "2026/06/25/photo_143000.jpg",
  "s3_key": "cam/cam042/2026/06/25/photo_143000.jpg",
  "bucket": "my-bucket",
  "reason": "Size mismatch: local=1024 remote=512",
  "attempts": 3
}
```

Example LogQL queries (adjust labels to match your agent):

```logql
# Any upload failure
{job="camera-sync"} | json | event="upload_failed"

# Failures for one camera
{job="camera-sync"} | json | camera="cam042"

# Alert: more than 5 failures in 15 minutes for a project
sum(count_over_time({job="camera-sync"} | json | event="upload_failed" | project="binalapse-cameras" [15m])) > 5
```

Recommended Promtail scrape: path `/var/log/camera/upload_failures.jsonl` with a `json` pipeline stage so `project`, `camera`, `file_path`, and `script` are queryable.

### Other checks

- **Heartbeat:** Check that `cron_alive.log` has a recent timestamp. If the
  last entry is older than 10 minutes, the sync may be stuck or not running.
- **Failed backlog:** Periodically check `failed/` directories. If files
  accumulate there even after retry runs, investigate `retry_error.log` for
  recurring `s3_access_error` or `local_unreadable` entries.
- **Log rotation:** Application logs and `upload_failures.jsonl` rotate at 10 MiB (5 backups).

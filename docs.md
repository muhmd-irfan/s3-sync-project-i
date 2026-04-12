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
| **Recover**| On startup, any files left in `.spool/` from a previous crash are moved back into the user root.     |
| **Scan**   | Walks the user directory. Picks files matching extensions (`.jpg`, `.jpeg`, `.png`, `.txt`) that are older than `MIN_AGE_SEC` (120 s). Skips `.spool/` and `failed/`. |
| **Spool**  | Atomically moves eligible files into `.spool/<relative_path>` to prevent race conditions with FTP writers. |
| **Upload** | Uploads each spooled file to `s3://<BUCKET>/<PREFIX>/<username>/<relative_path>` via `boto3`. Sends an MD5 `ContentMD5` header so S3 rejects corrupt uploads server-side. |
| **Verify** | Calls `head_object` on S3 and compares **file size** and **MD5 vs ETag**.                            |
| **Cleanup**| **Success →** local file is **deleted** (saves storage). **Failure →** file is moved to `failed/` and the error is logged. |

**Concurrency model:**

- `ProcessPoolExecutor` — one process per user directory (scales to `cpu_count`).
- `ThreadPoolExecutor` inside each process — concurrent S3 uploads (I/O-bound), scales between 4–20 threads based on file count.

**Log files (all in `LOG_DIR`):**

| File              | Content                      |
|-------------------|------------------------------|
| `sync.log`        | Full debug-level activity    |
| `error.log`       | Warnings and errors only     |
| `cron_alive.log`  | One-line heartbeat per run   |

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

Both scripts share the same set of variables at the top of the file.
Update them to match your environment **before deployment**.

| Variable              | Default                | Description                                    |
|-----------------------|------------------------|------------------------------------------------|
| `BASE_DIR`            | `/var/ftp/local`       | Root of the FTP directory tree                 |
| `BUCKET`              | `<S3-BUCKET-NAME>`     | Target S3 bucket                               |
| `PREFIX`              | `cam`                  | S3 key prefix (`s3://bucket/PREFIX/user/...`)  |
| `LOG_DIR`             | `/var/log/camera`      | Where all log files are written                |
| `MIN_AGE_SEC`         | `120`                  | Ignore files younger than this (seconds)       |
| `ELIGIBLE_EXTS`       | `.jpg .jpeg .png .txt` | File extensions to sync                        |
| `MAX_RETRIES`         | `3` *(retry only)*     | Upload attempts per failed file                |
| `MAX_PROCESS_WORKERS` | `None` (auto)          | Fix to an int to cap process pool size         |
| `MAX_THREAD_WORKERS`  | `None` (auto)          | Fix to an int to cap thread pool size          |

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
sudo cp camera_sync.py retry_failed.py /opt/camera_sync/
sudo chmod +x /opt/camera_sync/*.py
```

---

## Cron Setup

Open the crontab editor:

```bash
crontab -e
```

Add these two lines:

```cron
# Primary sync — every 5 minutes
*/5 * * * * /usr/bin/python3 /opt/camera_sync/camera_sync.py >> /var/log/camera/cron_sync.log 2>&1

# Failed-file retry — every hour at minute 30 (offset to avoid overlapping with sync)
30 * * * * /usr/bin/python3 /opt/camera_sync/retry_failed.py >> /var/log/camera/cron_retry.log 2>&1
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
├── cron_alive.log           ← heartbeat from camera_sync.py
├── cron_sync.log            ← cron stdout/stderr for sync
└── cron_retry.log           ← cron stdout/stderr for retry
```

---

## Monitoring

- **Heartbeat:** Check that `cron_alive.log` has a recent timestamp. If the
  last entry is older than 10 minutes, the sync may be stuck or not running.
- **Failed backlog:** Periodically check `failed/` directories. If files
  accumulate there even after retry runs, investigate `retry_error.log` for
  recurring `s3_access_error` or `local_unreadable` entries.
- **Log rotation:** Both scripts use `RotatingFileHandler` (10 MiB, 5 backups),
  so log files self-manage and won't fill the disk.

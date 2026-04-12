# Setup guide

End-to-end steps from cloning this repository to running `camera_sync.py` and `retry_failed.py` on a Linux host (bare metal, VM, or EC2). For behavior details and log file names, see [docs.md](docs.md). For EC2 sizing and pool tuning, see [README.md](README.md).

## Prerequisites

- Linux with **Python 3** and network access to **Amazon S3** in the bucket’s Region.
- An **S3 bucket** and a decision on key prefix (default in code: `cam`).
- A directory tree where cameras or FTP users drop files (default in code: `/var/ftp/local/<username>/...`).

**Reference stack (pin these for a reproducible install):**

| Component | Version |
|-----------|---------|
| Python | 3.9.25 |
| pip | 21.3.1 |
| boto3 | 1.42.88 |
| botocore | 1.42.88 |

Use a `python3` binary that reports **3.9.25** (or install that exact runtime via your OS, **pyenv**, or another method if the default image is different). Then install **pip**, **boto3**, and **botocore** as in [§3 Install Python packages](#3-install-python-packages).


This guide assumes **cron runs as root** (`root`’s crontab), so default lock paths under `/var/run/` work and the jobs can read/write across your FTP tree regardless of file ownership.

## 1. Clone the repository

On the target host (or build machine, if you copy artifacts manually).

Install Git if it is not already present:

**Amazon Linux 2023:**

```bash
sudo dnf install -y git
```

**Ubuntu 22.04 (or similar):**

```bash
sudo apt update
sudo apt install -y git
```

Then clone and enter the repository (the directory name matches the repo):

```bash
git clone https://github.com/muhmd-irfan/s3-sync-project-i.git
cd s3-sync-project-i
```

Replace the URL with your fork or the real public URL once published.

## 2. AWS access (permissions)

The scripts use **boto3** and need permission to upload and verify objects.

**Recommended on EC2:** attach an **IAM instance profile** to the instance with a policy that allows at least:

- `s3:PutObject`
- `s3:GetObject`
- `s3:HeadObject`

Scope the resources to your bucket (and prefix if you use a restrictive policy), for example:

`arn:aws:s3:::<your-bucket-name>/*`

With an instance profile you normally **do not** need `aws configure` on the server.

**Alternative:** configure credentials for **root** (cron runs as root): environment variables, or `/root/.aws/credentials` (e.g. `sudo aws configure`). To sanity-check:

```bash
sudo aws sts get-caller-identity
```

## 3. Install Python packages

Install the OS **Python 3** and **pip** packages first (versions vary by image). Align the interpreter with **Python 3.9.25** if needed, then pin **pip** and the AWS SDK:

```bash
sudo python3 --version   # expect 3.9.25 when matching the reference stack
sudo python3 -m pip install pip==21.3.1
sudo python3 -m pip install boto3==1.42.88 botocore==1.42.88
```

**Amazon Linux 2023** (example: ensure `python3` is your 3.9.25 build before the three commands above):

```bash
sudo dnf install -y python3 python3-pip
```

**Ubuntu 22.04 (or similar):**

```bash
sudo apt update
sudo apt install -y python3 python3-pip
```

Confirm versions the same way root’s cron will run:

```bash
sudo python3 -c "import sys; print(sys.version)"
sudo python3 -c "import boto3, botocore; print('boto3', boto3.__version__, 'botocore', botocore.__version__)"
```

## 4. Create directories

Pick an install location for the scripts (below uses `/opt/camera_sync`) and align log and FTP paths with what you will set in the Python files (defaults: `LOG_DIR=/var/log/camera`, `BASE_DIR=/var/ftp/local`).

```bash
sudo mkdir -p /opt/camera_sync
sudo mkdir -p /var/log/camera
sudo mkdir -p /var/ftp/local
```

## 5. Copy scripts into place

From your clone directory:

```bash
sudo cp camera_sync.py retry_failed.py /opt/camera_sync/
sudo chmod +x /opt/camera_sync/camera_sync.py /opt/camera_sync/retry_failed.py
```

You can remove or ignore the clone on the server after this if you only need the installed copies.

## 6. Configure `BASE_DIR`, `BUCKET`, and related settings

Edit **both** files so they match each other:

- `/opt/camera_sync/camera_sync.py`
- `/opt/camera_sync/retry_failed.py`

At minimum set:

| Setting | Purpose |
|---------|---------|
| `BASE_DIR` | Root of the per-user FTP tree |
| `BUCKET` | Target S3 bucket name |
| `PREFIX` | Key prefix under the bucket |
| `LOG_DIR` | Directory for application logs |

Optional: `MIN_AGE_SEC`, `ELIGIBLE_EXTS`, `MAX_PROCESS_WORKERS`, `MAX_THREAD_WORKERS` (keep worker limits aligned in both files if you set them). See the configuration table in [docs.md](docs.md).

Use `sudo` or your editor of choice:

```bash
sudo nano /opt/camera_sync/camera_sync.py
sudo nano /opt/camera_sync/retry_failed.py
```

## 7. Lock files

Default lock paths (no change needed when cron runs as root):

- `camera_sync.py` → `/var/run/camera_sync.lock`
- `retry_failed.py` → `/var/run/retry_failed.lock`

Root can create these under `/var/run/`. If you ever run the jobs as a non-root user instead, set `LOCKFILE` in **both** scripts to a directory that user can write (for example `/var/lib/camera_sync/`) and create that directory with matching ownership.

## 8. Ownership and permissions

Under `/var/ftp/local`, each **user directory** is usually owned by the Unix account that **vsftpd** (or your FTP stack) uses for that login (for example `bl001_ftpload001_cam2:bl001_ftpload001_cam2`). The parent `local` directory may be `root:root`. **Cron runs as root**, so it can read and move files in those trees regardless.

If you create a new FTP user tree by hand, set ownership to match that FTP user:

```bash
sudo chown -R someftpuser:someftpuser /var/ftp/local/someftpuser
```

Log directory for the scripts (writable by root cron):

```bash
sudo chown root:root /var/log/camera
sudo chmod 755 /var/log/camera
```

Install the scripts as root and executable:

```bash
sudo chown root:root /opt/camera_sync/*.py
sudo chmod 755 /opt/camera_sync/*.py
```

## 9. Cron (root)

Install the schedule in **root’s** crontab:

```bash
sudo crontab -e
```

Add:

```cron
# Primary sync — every 5 minutes
*/5 * * * * /usr/bin/python3 /opt/camera_sync/camera_sync.py >> /var/log/camera/cron_sync.log 2>&1

# Failed-file retry — hourly at minute 30
30 * * * * /usr/bin/python3 /opt/camera_sync/retry_failed.py >> /var/log/camera/cron_retry.log 2>&1
```

Use the same `python3` path you verified with `sudo python3 -c "import boto3"`. Check with:

```bash
sudo which python3
sudo crontab -l
```

Lock files prevent overlapping runs of the same script; sync and retry can run at the same time.

## 10. Smoke test (before relying on cron)

Run as root (same as cron):

```bash
sudo /usr/bin/python3 /opt/camera_sync/camera_sync.py
```

Then inspect:

- `/var/log/camera/sync.log`
- `/var/log/camera/error.log`

Successful per-file lines include `upload_s=` and `verify_s=` (seconds for S3 upload vs verify). The run summary line includes totals for that user; the final `Sync end` line in the same log aggregates across users.

Run the retry job once if you have files under any `failed/` tree:

```bash
sudo /usr/bin/python3 /opt/camera_sync/retry_failed.py
```

## 11. Monitoring (short)

- **Heartbeat:** `cron_alive.log` should get a new line roughly every successful sync interval.
- **Backlog:** persistent files under `failed/` after retries warrant checking `retry_error.log` and IAM/network.

More detail: [docs.md — Monitoring](docs.md#monitoring).

## Reference layout

Paths below match a typical install (compare with `ls /opt`, `ls /var/ftp/local`, `ls /var/log/camera` on the host).

```
/opt/camera_sync/
├── camera_sync.py
└── retry_failed.py

/var/ftp/local/
├── bl001_ftpload001_cam2/ # one directory per FTP user (names vary)
│   ├── ...                    # incoming files
│   ├── .spool/                # staging (managed by scripts)
│   └── failed/                # quarantine (retry script)
├── bl001_ftpload001_cam3/
├── bl001_ftpload001_cam4/
├── cx001_newftp001_cam1/
└── ...

/var/log/camera/
├── sync.log
├── error.log
├── cron_alive.log
├── retry.log # present once retry_failed.py has run
├── retry_error.log
├── cron_sync.log              # cron stdout/stderr for sync (if redirected)
└── cron_retry.log             # cron stdout/stderr for retry (if redirected)
```

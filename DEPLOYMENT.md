# Deployment Guide (EC2) — Camera → S3 Sync

This is the **one document** you need to go from a freshly cloned repo on an
EC2 instance to a running, monitored sync pipeline. It replaces the scattered
instructions in `setup.md`, `docs.md`, `README.md`, and `INFRA_CHECKLIST.md`
(those are kept for reference; you can delete them later).

Everything is a **copy-paste checklist**. Do the steps top to bottom. Each step
has a "✅ Done when" line so you know it worked before moving on.

---

## What you are deploying (30-second version)

- `camera_sync.py` — every 5 min: finds new files under `/var/ftp/local/<user>/`,
uploads to S3, verifies (size + MD5), deletes the local copy on success, or
moves it to `failed/` on failure.
- `retry_failed.py` — every hour: re-tries anything sitting in `failed/`.
- `sync_common.py` — shared helper code (must sit next to the two scripts).
- Config comes **only from environment variables** — you never edit Python for
your bucket name.
- Runs from **root's crontab**. Logs go to `/var/log/camera/`. Upload failures
and rejected files are also written as JSON lines to `upload_failures.jsonl`
for Grafana (`event="upload_failed"` vs `event="file_rejected"`).

---



## Master checklist (high level)

- [x] 1. Connect to the EC2 instance over SSH
- [x] 2. Confirm the instance basics (OS, disk, region)
- [x] 3. Give the instance S3 permissions (IAM role) and verify
- [x] 4. Install Python 3.12 + boto3
- [ ] 5. Create the directories
- [ ] 6. Copy the scripts into `/opt/camera_sync`
- [ ] 7. Create the env file (`env.sh`) — this is the vim/nano step
- [ ] 8. Set ownership and permissions
- [ ] 9. Smoke test manually (before cron)
- [ ] 10. Install the cron jobs (root)
- [ ] 11. Confirm cron runs and read the logs
- [ ] 12. Set up Grafana + Loki monitoring
- [ ] 13. Day-2 operations & troubleshooting

Conventions used below:


| Thing         | Value                            |
| ------------- | -------------------------------- |
| Install dir   | `/opt/camera_sync`               |
| FTP tree root | `/var/ftp/local`                 |
| Log dir       | `/var/log/camera`                |
| Python        | `/usr/bin/python3.12`            |
| Who runs it   | **root** (via `sudo crontab -e`) |


> If your instance uses different paths, change them **everywhere** consistently
> (the env file, the smoke test, and cron must all agree).

---



## Step 1 — Connect to the EC2 instance

From your laptop (replace the key path and public DNS/IP):

```bash
ssh -i ~/path/to/key.pem ec2-user@<EC2-PUBLIC-IP>     # Amazon Linux 2023
# or
ssh -i ~/path/to/key.pem ubuntu@<EC2-PUBLIC-IP>       # Ubuntu
```

If you get a permissions error on the key:

```bash
chmod 400 ~/path/to/key.pem
```

✅ **Done when:** you get a shell prompt on the instance.

---



## Step 2 — Confirm instance basics

```bash
cat /etc/os-release | head -n 2      # which distro
nproc                                # vCPU count (affects tuning later)
df -h /                              # free disk for the FTP tree + spool + logs
```

Figure out which distro you're on — commands below are split into **Amazon
Linux 2023** and **Ubuntu**. Pick the matching one each time.

✅ **Done when:** you know your distro and have enough free disk for incoming files.

---



## Step 3 — S3 permissions (IAM role)

The scripts use **boto3**, which needs AWS credentials. The **recommended** way
on EC2 is an **IAM instance profile** (a role attached to the instance) — no
keys stored on disk.

### 3a. Attach a role (done in the AWS Console, once)

Create/attach an IAM role to the instance with a policy allowing at least:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:HeadObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    }
  ]
}
```

Console path: **EC2 → your instance → Actions → Security → Modify IAM role**.

### 3b. Verify from the instance

The AWS CLI v2 is preinstalled on Amazon Linux 2023 (on Ubuntu, install with
`sudo snap install aws-cli --classic` if missing). Check identity and bucket
access:

```bash
aws sts get-caller-identity
aws s3 ls s3://YOUR-BUCKET-NAME/ --summarize | tail -n 3
```

> If you must use access keys instead of a role, run `sudo aws configure` so the
> credentials live under `/root/.aws/` (cron runs as **root**, so they must be
> root's, not `ec2-user`'s).

✅ **Done when:** `aws sts get-caller-identity` prints an account/role and the
`aws s3 ls` on your bucket does not error.

---



## Step 4 — Install Python 3.12 + boto3

On Amazon Linux 2023 the default `python3` is often **3.9**, which AWS SDKs are
dropping support for. Install and use **3.12** explicitly.

**Amazon Linux 2023:**

```bash
sudo dnf install -y python3.12 python3.12-pip
```

**Ubuntu 22.04 / 24.04:**

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip
```

Install the AWS SDK **into that same interpreter, as root** (because root's cron
runs it):

```bash
sudo python3.12 -m pip install --upgrade pip
sudo python3.12 -m pip install boto3==1.42.88 botocore==1.42.88
```

Verify:

```bash
which python3.12
sudo python3.12 -c "import sys; print(sys.version)"
sudo python3.12 -c "import boto3, botocore; print('boto3', boto3.__version__, 'botocore', botocore.__version__)"
```

> Note the path `which python3.12` prints (usually `/usr/bin/python3.12`). You
> will use this **exact path** in the smoke test and in cron.

✅ **Done when:** the last command prints `boto3 1.42.88 botocore 1.42.88`
without errors.

---



## Step 5 — Create directories

```bash
sudo mkdir -p /opt/camera_sync
sudo mkdir -p /var/log/camera
sudo mkdir -p /var/ftp/local
```

- `/opt/camera_sync` — where the scripts live.
- `/var/log/camera` — where logs are written.
- `/var/ftp/local` — root of the per-camera/per-user upload tree.

✅ **Done when:** all three exist (`ls -ld /opt/camera_sync /var/log/camera /var/ftp/local`).

---



## Step 6 — Copy the scripts into place

You already cloned the repo. From inside the clone directory
(`cd ~/s3-sync-project-i` or wherever you cloned it):

```bash
sudo cp camera_sync.py retry_failed.py sync_common.py /opt/camera_sync/
sudo chmod +x /opt/camera_sync/camera_sync.py /opt/camera_sync/retry_failed.py
ls -l /opt/camera_sync/
```

> **Important:** `sync_common.py` **must** sit in the same folder as the other
> two — they import it directly. Don't skip it.

✅ **Done when:** `ls -l /opt/camera_sync/` shows all three `.py` files.

---



## Step 7 — Create the env file (the vim / nano step)

The scripts read **only environment variables**. We put them in a file that
cron will "source" before running Python.

Copy the example from your clone:

```bash
sudo cp ~/s3-sync-project-i/.env.example /opt/camera_sync/env.sh
```

Now edit it. `crontab -e` and this file both open in an editor. Here's how to
survive **vim** and, if you prefer, how to switch to the friendlier **nano**.

### 7a. Editing with vim (default on many servers)

```bash
sudo vim /opt/camera_sync/env.sh
```

Vim starts in **Normal mode** (keystrokes are commands, not text). The survival
kit:


| You want to…            | Do this                                             |
| ----------------------- | --------------------------------------------------- |
| Start typing            | press `i` (you'll see `-- INSERT --` at the bottom) |
| Stop typing             | press `Esc`                                         |
| Save and quit           | `Esc`, then type `:wq`, then `Enter`                |
| Quit WITHOUT saving     | `Esc`, then type `:q!`, then `Enter`                |
| Delete the current line | `Esc`, then `dd`                                    |
| Undo                    | `Esc`, then `u`                                     |


So the loop is always: `i` **→ type →** `Esc` **→** `:wq` **→ Enter**.

### 7b. Prefer nano? (recommended if vim scares you)

Install nano and use it instead:

```bash
sudo dnf install -y nano     # Amazon Linux 2023
sudo apt install -y nano     # Ubuntu
sudo nano /opt/camera_sync/env.sh
```

In nano: just type normally. Save = `Ctrl+O` then `Enter`. Quit = `Ctrl+X`.

To make nano your default editor for `crontab -e` too, add this to your shell
profile (run once):

```bash
echo 'export EDITOR=nano' >> ~/.bashrc && source ~/.bashrc
```



### 7c. What the env file must contain

**Critical:** every line must start with `export`, otherwise the variables are
set in the shell but **not passed to Python** when cron sources the file. Make
the file look exactly like this (change the bucket and project):

```bash
# Camera Sync — environment (sourced by cron before each run)
# REQUIRED
export CAMERA_SYNC_BUCKET=your-bucket-name
export CAMERA_SYNC_PROJECT=binalapse-cameras

# Optional — defaults shown; uncomment/change only if needed
export CAMERA_SYNC_BASE_DIR=/var/ftp/local
export CAMERA_SYNC_PREFIX=cam
export CAMERA_SYNC_LOG_DIR=/var/log/camera
export CAMERA_SYNC_MIN_AGE_SEC=120
export CAMERA_SYNC_MAX_RETRIES=3
export CAMERA_SYNC_TIMEOUT_SEC=480
export CAMERA_SYNC_FAILURE_LOG=/var/log/camera/upload_failures.jsonl

# Performance tuning (see the table at the bottom of this doc)
# export CAMERA_SYNC_MAX_PROCESS_WORKERS=2
# export CAMERA_SYNC_MAX_THREAD_WORKERS=8
```

Lock the file down (it names your bucket; keep it root-only):

```bash
sudo chown root:root /opt/camera_sync/env.sh
sudo chmod 600 /opt/camera_sync/env.sh
```

Test that sourcing it actually exports the bucket:

```bash
sudo bash -c '. /opt/camera_sync/env.sh; echo "BUCKET=$CAMERA_SYNC_BUCKET"'
```

✅ **Done when:** the command above prints `BUCKET=your-bucket-name` (your real
value), and every line in the file starts with `export`.

---



## Step 8 — Ownership and permissions

Scripts owned by root, executable:

```bash
sudo chown root:root /opt/camera_sync/*.py
sudo chmod 755 /opt/camera_sync/*.py
```

Log directory writable by root cron:

```bash
sudo chown root:root /var/log/camera
sudo chmod 755 /var/log/camera
```

FTP tree: each **camera/user folder** is usually owned by the FTP account that
logs in as that user. The parent `local` dir can stay `root:root`. Because cron
runs as **root**, it can read and move files regardless of who owns them. If you
create a user folder by hand, match the FTP user:

```bash
# example only — use your real FTP username
sudo mkdir -p /var/ftp/local/bl001_ftpload001_cam2
sudo chown -R bl001_ftpload001_cam2:bl001_ftpload001_cam2 /var/ftp/local/bl001_ftpload001_cam2
```

> **Lock files** default to `/var/run/camera_sync.lock` and
> `/var/run/retry_failed.lock`. Root can create these — no action needed. Only
> if you run as a non-root user would you need to change them.

✅ **Done when:** `ls -l /opt/camera_sync` shows `-rwxr-xr-x root root` on the
`.py` files and the log dir is owned by root.

---



## Step 9 — Smoke test (before trusting cron)

Run the sync exactly the way cron will — as **root**, sourcing the env file,
with the full Python path. This one-liner does all of that:

```bash
sudo bash -c 'set -a; . /opt/camera_sync/env.sh; /usr/bin/python3.12 /opt/camera_sync/camera_sync.py'
```

To actually exercise an upload, drop a test file first. Remember files must be
older than `CAMERA_SYNC_MIN_AGE_SEC` (120s), and images are validated by magic
bytes — so use a `.txt` file to keep the test simple:

```bash
sudo mkdir -p /var/ftp/local/testcam
echo "hello $(date)" | sudo tee /var/ftp/local/testcam/test.txt
```

Option A — wait 2+ minutes, then run the smoke test above.

Option B — skip the age check just for this run:

```bash
sudo bash -c 'set -a; . /opt/camera_sync/env.sh; CAMERA_SYNC_MIN_AGE_SEC=0 /usr/bin/python3.12 /opt/camera_sync/camera_sync.py'
```

Then check the result:

```bash
sudo tail -n 20 /var/log/camera/sync.log
aws s3 ls s3://your-bucket-name/cam/testcam/
```

You should see an `OK | test.txt (deleted) | upload_s=... verify_s=...` line in
`sync.log`, a final `Sync end | total_ok=1 ...` line, and the object present in
S3. The local `test.txt` will be gone (deleted after successful upload).

Run the retry job once too (harmless if nothing is in `failed/`):

```bash
sudo bash -c '. /opt/camera_sync/env.sh; /usr/bin/python3.12 /opt/camera_sync/retry_failed.py'
sudo tail -n 20 /var/log/camera/retry.log
```

Clean up the test object when happy:

```bash
aws s3 rm s3://your-bucket-name/cam/testcam/test.txt
sudo rmdir /var/ftp/local/testcam 2>/dev/null || true
```

✅ **Done when:** `sync.log` shows `Sync end` with `total_ok=1` and the file
appears in S3 (then gets deleted locally).

---



## Step 10 — Install the cron jobs (root)

Open **root's** crontab:

```bash
sudo crontab -e
```

> This opens vim by default (see the vim cheat sheet in Step 7a). To use nano
> instead: `sudo EDITOR=nano crontab -e`.

Add these two lines (change the Python path only if `which python3.12` differed):

```cron
# Camera → S3 sync every 5 minutes
*/5 * * * * . /opt/camera_sync/env.sh; /usr/bin/python3.12 /opt/camera_sync/camera_sync.py >> /var/log/camera/cron_sync.log 2>&1

# Retry failed uploads every hour at minute 30
30 * * * * . /opt/camera_sync/env.sh; /usr/bin/python3.12 /opt/camera_sync/retry_failed.py >> /var/log/camera/cron_retry.log 2>&1
```

Save and exit (vim: `Esc` `:wq` Enter). Confirm it's installed:

```bash
sudo crontab -l
```

**How the cron line works, left to right:**

- `*/5 * * * *` — schedule (every 5 minutes / every hour at :30).
- `. /opt/camera_sync/env.sh;` — sources your env file (the `export` lines).
- `/usr/bin/python3.12 .../camera_sync.py` — runs the script.
- `>> /var/log/camera/cron_sync.log 2>&1` — appends stdout **and** stderr to a
log so you can see crashes that happen before logging starts.

> The scripts use lock files, so if one run overruns its window the next run
> exits immediately instead of piling up. Sync and retry have separate locks and
> can run at the same time.

✅ **Done when:** `sudo crontab -l` shows both lines.

---



## Step 11 — Confirm cron is running & how to read logs

Wait about 5 minutes (until the next `*/5` tick), then:

```bash
# Did cron invoke it at all? (crash-level output)
sudo tail -n 30 /var/log/camera/cron_sync.log

# Heartbeat — should get a fresh "OK" line roughly every successful run
sudo tail -n 5 /var/log/camera/cron_alive.log

# Full activity
sudo tail -n 40 /var/log/camera/sync.log
```

Handy live-follow commands:

```bash
sudo tail -f /var/log/camera/sync.log          # watch sync live
sudo tail -f /var/log/camera/error.log         # watch only warnings/errors
```



### Log file cheat sheet


| File                                    | What's in it                                                 |
| --------------------------------------- | ------------------------------------------------------------ |
| `/var/log/camera/sync.log`              | Full sync activity (per-file `OK`/`FAIL`/`Rejected`, `Sync end` totals incl. `total_rejected`) |
| `/var/log/camera/error.log`             | Sync warnings & errors only                                  |
| `/var/log/camera/retry.log`             | Full retry activity (`DIAG`, `RETRY OK`, `RESOLVED`)         |
| `/var/log/camera/retry_error.log`       | Retry warnings & errors only                                 |
| `/var/log/camera/cron_alive.log`        | One `OK` heartbeat line per successful sync                  |
| `/var/log/camera/upload_failures.jsonl` | One JSON object per failed upload (`upload_failed`) or rejected file (`file_rejected`), for Grafana |
| `/var/log/camera/cron_sync.log`         | Raw stdout/stderr from cron for the sync job                 |
| `/var/log/camera/cron_retry.log`        | Raw stdout/stderr from cron for the retry job                |


> All logs auto-rotate at 10 MiB with 5 backups — you won't fill the disk.

Verify the cron service itself is enabled (rarely needed, but good to know):

```bash
# Amazon Linux 2023
sudo systemctl status crond
# Ubuntu
sudo systemctl status cron
```

✅ **Done when:** `cron_alive.log` gets a new `OK` line every ~5 minutes and
`sync.log` shows recurring `Sync end` lines.

---



## Step 12 — Monitoring with Grafana + Loki

Goal: ship `/var/log/camera/upload_failures.jsonl` to **Loki**, view/alert on it
in **Grafana**. Each line is one JSON record. The `event` field is either
`upload_failed` (an S3 upload failed after retries) or `file_rejected` (a scanned
file was rejected before upload; `reason` is `magic_bytes`, `unsafe_path`,
`symlink`, or `outside_root`, and `attempts` is `0`):

```json
{"timestamp":"2026-06-25T14:30:00Z","level":"error","event":"upload_failed","project":"binalapse-cameras","script":"camera_sync","camera":"cam042","file_path":"2026/06/25/photo.jpg","s3_key":"cam/cam042/2026/06/25/photo.jpg","bucket":"my-bucket","reason":"Size mismatch: local=1024 remote=512","attempts":3}
{"timestamp":"2026-06-25T14:30:00Z","level":"error","event":"file_rejected","project":"binalapse-cameras","script":"camera_sync","camera":"cam042","file_path":"2026/06/25/photo.jpg","s3_key":"cam/cam042/2026/06/25/photo.jpg","bucket":"my-bucket","reason":"magic_bytes","attempts":0}
```

You have two paths. **Option A** (Grafana Cloud) is least maintenance;
**Option B** (self-hosted Docker) keeps everything on your own box.

### Option A — Grafana Cloud (managed, recommended for small teams)

1. [ ] Create a free Grafana Cloud account → it gives you a **Loki push URL**,
  a **user/tenant id**, and an **API token**.
2. [ ] Install **Grafana Alloy** (the current shipping agent) on the EC2 box:
  **Amazon Linux 2023:**
   **Ubuntu:**
   (Grab the exact latest URL/version from the Grafana Alloy install docs.)
3. [ ] Point Alloy at the JSON log. Put this in `/etc/alloy/config.alloy`
  (replace the URL/credentials from step 1):
4. [ ] Start it: `sudo systemctl enable --now alloy` then
  `sudo systemctl status alloy`.
5. [ ] Skip to **"Querying and alerting in Grafana"** below.



### Option B — Self-hosted Loki + Grafana + Promtail (Docker)

Run this on the EC2 box (or a separate monitoring box). Requires Docker.

1. [ ] Install Docker + compose:
  ```bash
   # Amazon Linux 2023
   sudo dnf install -y docker
   sudo systemctl enable --now docker
   # Ubuntu
   sudo apt update && sudo apt install -y docker.io docker-compose-plugin
   sudo systemctl enable --now docker
  ```
2. [ ] Create a folder and a Promtail config that tails the JSON file:
  ```bash
   sudo mkdir -p /opt/monitoring
  ```
   `/opt/monitoring/promtail-config.yaml`:
3. [ ] Create `/opt/monitoring/docker-compose.yaml`:
  ```yaml
   services:
     loki:
       image: grafana/loki:latest
       command: -config.file=/etc/loki/local-config.yaml
       ports: ["3100:3100"]
     promtail:
       image: grafana/promtail:latest
       volumes:
         - /var/log/camera:/var/log/camera:ro
         - /opt/monitoring/promtail-config.yaml:/etc/promtail/config.yaml:ro
       command: -config.file=/etc/promtail/config.yaml
       depends_on: [loki]
     grafana:
       image: grafana/grafana:latest
       ports: ["3000:3000"]
       environment:
         - GF_SECURITY_ADMIN_PASSWORD=changeme
       depends_on: [loki]
  ```
4. [ ] Start it: `cd /opt/monitoring && sudo docker compose up -d`
5. [ ] Open Grafana at `http://<EC2-IP>:3000` (login `admin` / `changeme`).
  **Open port 3000 in the security group only from your IP.**
6. [ ] Add the Loki data source: **Connections → Data sources → Add → Loki**,
  URL `http://loki:3100`, Save & test.



### Querying and alerting in Grafana (both options)

1. [ ] Go to **Explore**, pick the Loki data source, and try:
  ```logql
   # Every upload failure
   {job="camera-sync"} | json | event="upload_failed"

   # Every rejected file (bad magic bytes, unsafe path, symlink, outside root)
   {job="camera-sync"} | json | event="file_rejected"

   # Rejections for one reason
   {job="camera-sync"} | json | event="file_rejected" | reason="magic_bytes"

   # Failures for one camera
   {job="camera-sync"} | json | camera="cam042"

   # Rate: more than 5 failures in 15 min for a project
   sum(count_over_time({job="camera-sync"} | json | event="upload_failed" | project="binalapse-cameras" [15m])) > 5

   # Rate: more than 20 rejected files in 15 min for a project
   sum(count_over_time({job="camera-sync"} | json | event="file_rejected" | project="binalapse-cameras" [15m])) > 20
  ```
2. [ ] Create an **Alert rule** (Alerting → Alert rules → New) using that last
  query with a threshold, and wire it to your notification channel (email,
   Slack, PagerDuty, etc.).
3. [ ] Build a simple **dashboard**: a "Logs" panel with
  `{job="camera-sync"} | json` and a "Time series" panel counting failures by
   `camera`.

✅ **Done when:** you force a failure (see Step 13) and it shows up in Grafana
Explore within a minute or two.

---



## Step 13 — Day-2 operations & troubleshooting



### Force a failure to test monitoring end-to-end

Point at a bucket you can't write, run once, and confirm a JSON line appears:

```bash
sudo bash -c 'set -a; . /opt/camera_sync/env.sh; CAMERA_SYNC_MIN_AGE_SEC=0 CAMERA_SYNC_BUCKET=nonexistent-bucket-xyz /usr/bin/python3.12 /opt/camera_sync/camera_sync.py'
sudo tail -n 3 /var/log/camera/upload_failures.jsonl
```

(Drop a `testcam/test.txt` first as in Step 9.)

### Common issues


| Symptom                                               | Likely cause & fix                                                                                                                                                |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Configuration error: CAMERA_SYNC_BUCKET must be set` | env file not sourced or missing `export`. Re-check Step 7c; test with the `echo BUCKET=` command.                                                                 |
| Nothing uploads, no errors                            | Files younger than `MIN_AGE_SEC` (120s), or not `.jpg/.jpeg/.png/.txt`, or images with bad/mismatched magic bytes. Check `sync.log` for `Rejected` / `too_young`. |
| `cron_sync.log` shows `python3.12: not found`         | Wrong interpreter path in cron. Use the output of `which python3.12`.                                                                                             |
| Cron does nothing at all                              | Wrong crontab (used your user instead of root). Use `sudo crontab -e` / `sudo crontab -l`. Check `crond`/`cron` service is running.                               |
| `AccessDenied` in `error.log`                         | IAM role missing `s3:PutObject/GetObject/HeadObject` on the bucket ARN (Step 3).                                                                                  |
| Files pile up in `failed/`                            | Run retry manually and read `retry_error.log`; look for repeated `s3_access_error` (IAM/network) or `local_unreadable` (permissions).                             |
| Lock never releases / "skipped" every run             | Stale process holding the lock; check `ps aux                                                                                                                     |




### Useful one-liners

```bash
# How many files are stuck in failed/ across all users
sudo find /var/ftp/local -path '*/failed/*' -type f | wc -l

# Recent errors only
sudo tail -n 50 /var/log/camera/error.log

# Confirm both cron jobs are installed
sudo crontab -l

# Re-run retry now
sudo bash -c '. /opt/camera_sync/env.sh; /usr/bin/python3.12 /opt/camera_sync/retry_failed.py'
```

---



## Appendix A — Environment variables reference


| Variable                          | Default                           | Required | Purpose                                       |
| --------------------------------- | --------------------------------- | -------- | --------------------------------------------- |
| `CAMERA_SYNC_BUCKET`              | —                                 | **yes**  | Target S3 bucket name                         |
| `CAMERA_SYNC_PROJECT`             | `camera-sync`                     | no       | Label in Grafana failure logs                 |
| `CAMERA_SYNC_BASE_DIR`            | `/var/ftp/local`                  | no       | Root of the FTP tree                          |
| `CAMERA_SYNC_PREFIX`              | `cam`                             | no       | S3 key prefix (`s3://bucket/PREFIX/user/...`) |
| `CAMERA_SYNC_LOG_DIR`             | `/var/log/camera`                 | no       | Where logs are written                        |
| `CAMERA_SYNC_MIN_AGE_SEC`         | `120`                             | no       | Ignore files younger than this                |
| `CAMERA_SYNC_MAX_RETRIES`         | `3`                               | no       | Upload attempts before quarantine             |
| `CAMERA_SYNC_TIMEOUT_SEC`         | `480`                             | no       | Max sync runtime (0 = disabled)               |
| `CAMERA_SYNC_MAX_PROCESS_WORKERS` | auto (`min(users, vCPUs)`)        | no       | Parallel user directories                     |
| `CAMERA_SYNC_MAX_THREAD_WORKERS`  | auto (4–20)                       | no       | Parallel uploads per user                     |
| `CAMERA_SYNC_FAILURE_LOG`         | `{LOG_DIR}/upload_failures.jsonl` | no       | JSON failure log path                         |


Eligible file types: `.jpg`, `.jpeg`, `.png`, `.txt` (images checked by magic bytes).

## Appendix B — Performance tuning (T2/T3 burstable)


| Tier (typical sizes)       | `MAX_PROCESS_WORKERS` | `MAX_THREAD_WORKERS` |
| -------------------------- | --------------------- | -------------------- |
| Small (nano, micro, small) | `1`                   | `4`–`8`              |
| Medium (medium, large)     | `2`                   | `8`–`12`             |
| High (xlarge, 2xlarge)     | `4`–`8` (≤ vCPUs)     | `12`–`20`            |


Set them via `export` in `/opt/camera_sync/env.sh`. Watch CloudWatch CPU-credit
balance on burstable instances; lower the process workers if credits drain.

## Appendix C — vim cheat sheet (for `crontab -e` and env edits)


| Action                     | Keys                |
| -------------------------- | ------------------- |
| Enter insert (typing) mode | `i`                 |
| Leave insert mode          | `Esc`               |
| Save & quit                | `Esc` `:wq` `Enter` |
| Quit without saving        | `Esc` `:q!` `Enter` |
| Delete a line              | `Esc` `dd`          |
| Undo                       | `Esc` `u`           |
| Go to end of file          | `Esc` `G`           |


Prefer nano? Use `sudo EDITOR=nano crontab -e`. Save `Ctrl+O` `Enter`, quit `Ctrl+X`.

## Appendix D — Reference layout on the host

```
/opt/camera_sync/
├── camera_sync.py
├── retry_failed.py
├── sync_common.py
└── env.sh            # your config (export lines), root:root chmod 600

/var/ftp/local/
├── <ftp-user-1>/
│   ├── ...           # incoming files
│   ├── .spool/       # staging (managed by scripts)
│   └── failed/       # quarantine (retry job handles these)
└── <ftp-user-2>/

/var/log/camera/
├── sync.log            error.log
├── retry.log           retry_error.log
├── cron_alive.log      upload_failures.jsonl
├── cron_sync.log       cron_retry.log
```


# Infrastructure Checklist

Manual steps on AWS and the FTP host that cannot be done in Python alone. Complete before production.

## FTP / network hardening

Cameras only support plain FTP. Compensate at the network layer:

- [ ] Place cameras on a **dedicated VLAN** (isolated from office/user networks)
- [ ] **Firewall allowlist**: only known camera IP addresses may reach FTP ports
- [ ] **One password per camera** (compromise of one camera does not expose others)
- [ ] **Chroot** each FTP user to its own directory under `/var/ftp/local/<camera>/`
- [ ] **Disable anonymous FTP**
- [ ] For remote cameras, use a **VPN tunnel** to the server

## AWS S3 bucket

- [ ] **Enable encryption at rest** (SSE-S3 or SSE-KMS) on the bucket
- [ ] **Block all public access** on the bucket
- [ ] **Lifecycle rules** (optional): transition objects to S3 Glacier / Infrequent Access after N days
- [ ] **IAM instance profile** on EC2 with least privilege:
  - `s3:PutObject`, `s3:GetObject`, `s3:HeadObject`
  - Resource: `arn:aws:s3:::<bucket-name>/cam/*` (tighten prefix as needed)

## Application host

- [ ] Copy `camera_sync.py`, `retry_failed.py`, and `sync_common.py` to `/opt/camera_sync/`
- [ ] Create `/opt/camera_sync/env.sh` from `.env.example` with real `CAMERA_SYNC_BUCKET` and `CAMERA_SYNC_PROJECT`
- [ ] Ensure `/var/log/camera` exists and cron can write logs
- [ ] Install cron jobs (see [setup.md](setup.md#9-cron-root))
- [ ] Optional belt-and-suspenders: wrap sync in `timeout 480` in crontab

## Grafana / log shipping

- [ ] Ship **`/var/log/camera/upload_failures.jsonl`** to your log store (Loki via Promtail, Fluent Bit, etc.)
- [ ] Add scrape labels such as `job=camera-sync` and `host=<server-name>`
- [ ] Use a **JSON pipeline stage** so fields are parsed: `project`, `camera`, `file_path`, `script`, `event`, `reason`
- [ ] Wire **Grafana alerts** to your existing system, for example:
  - Any `event="upload_failed"` for a given `project`
  - Rate alert: more than N failures in 15 minutes
  - Per-camera alert: `camera="cam042"` failures
- [ ] Optionally ship `error.log` and `retry_error.log` for human-readable debugging

Example Promtail snippet (adjust paths and labels):

```yaml
- job_name: camera-sync-failures
  static_configs:
    - targets: [localhost]
      labels:
        job: camera-sync
        __path__: /var/log/camera/upload_failures.jsonl
  pipeline_stages:
    - json:
        expressions:
          project: project
          camera: camera
          file_path: file_path
          script: script
          event: event
```

## Verification

- [ ] `CAMERA_SYNC_BUCKET` set; script exits with clear error if missing
- [ ] Manual sync run succeeds; `sync.log` shows `Sync end`
- [ ] `cron_alive.log` updates after each sync
- [ ] Force a failure; confirm one JSON line in `upload_failures.jsonl` with `project`, `camera`, `file_path`
- [ ] Grafana query returns the failure event within expected ingest delay

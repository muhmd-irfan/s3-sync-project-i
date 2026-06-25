# Camera Sync System — Review (Simple Version)

**What this system does:** ~50 cameras take photos and send them to a server. A Python script picks up those photos and stores them safely in Amazon's cloud storage (S3).

**This document explains** what's working well, what's risky, and what needs fixing — in plain English.

---

## 🏠 How It Works Today

```
📷 Camera 1 ──┐
📷 Camera 2 ──┤── upload photos ──▶ 🖥️ Server ──▶ 🪣 Python Script ──▶ ☁️ Amazon S3
📷 Camera 50 ─┘      (FTP)          (local disk)     (every 5 min)       (cloud storage)
```

1. Cameras snap a photo roughly every 10 minutes
2. Each camera sends its photo to a folder on the server (via FTP)
3. Every 5 minutes, a Python script checks for new photos
4. It uploads each photo to Amazon S3 (cloud storage)
5. After confirming the upload worked, it deletes the local copy to save disk space
6. If an upload fails, the photo is moved to a "failed" folder
7. A separate script runs every hour to retry those failed uploads

---

## ✅ What's Working Well

| Area | What's Good |
|------|-------------|
| **Smart staging** | Photos are moved to a temporary "spool" folder before uploading, so a camera writing a new photo won't interfere with an upload in progress |
| **Upload verification** | After uploading, the script double-checks the file actually arrived in S3 correctly (size + checksum match) |
| **Failure handling** | Failed uploads aren't lost — they're saved in a "failed" folder and retried later |
| **No double-runs** | A lock file prevents two copies of the script from running at the same time |
| **Parallel processing** | Multiple cameras' photos are uploaded simultaneously to save time |
| **Good documentation** | README, setup guide, and docs are clear and thorough |

---

## 🔴 Critical Problems (Fix Before Production)

### 1. Photos Are Sent Over the Network Without Encryption

**The problem:** Cameras send photos using plain FTP, which is like sending a postcard — anyone on the network can read it. Passwords and photos travel without any encryption.

**Real-world risk:** If someone taps the network (e.g., on the same Wi-Fi or between network segments), they can see every photo and steal every camera's login password.

> **Constraint:** These cameras only support FTP — switching to SFTP is not possible with the current hardware.

**The fix (since we must use FTP, protect the network instead):**
- **Isolate the cameras on their own network (VLAN)** — like putting them in a separate room where only authorized people can enter
- **Firewall rules** — only allow the known camera IP addresses to connect to FTP; block everything else
- **One password per camera** — if one camera is compromised, the others stay safe
- **Lock each camera to its own folder** — a camera can only see its own folder, not other cameras' photos
- **If cameras are remote,** connect them via a VPN tunnel (encrypts everything at the network level, even though FTP itself doesn't)

---

### 2. No Check on What Files Actually Contain

**The problem:** The system only checks if a file ends in `.jpg` or `.png` — it doesn't actually verify it's a real photo. A bad actor could name a virus `photo.jpg` and it would be uploaded to S3.

**Real-world risk:** Malicious files end up in your cloud storage. If anyone downloads and opens them, their system could be compromised.

**The fix:** Before uploading, peek inside the file to confirm it's actually a photo (check the file's "magic bytes" — every real JPEG starts with specific bytes).

---

### 3. Dangerous Filenames Aren't Blocked

**The problem:** If a camera (or attacker) creates a file named `../../etc/passwords.jpg`, the system might try to access files outside its allowed folder.

**Real-world risk:** Sensitive server files could be read or uploaded to S3 by accident.

**The fix:** Reject any filename containing `..` and ignore shortcuts (symlinks) that point outside the camera folder.

---

## 🟠 Serious Problems (Fix Soon)

### 4. Settings Are Hardcoded in the Script

**The problem:** The S3 bucket name, folder paths, etc. are typed directly into the Python code. To change them, you have to edit the source code on the server.

**Why it matters:** Easy to make typos, hard to manage across environments, and the bucket name placeholder `<S3-BUCKET-NAME>` could accidentally be deployed as-is (silently losing all photos).

**The fix:** Read settings from environment variables (like a separate settings sheet the script reads at startup).

---

### 5. No Retries When an Upload Fails

**The problem:** If a photo fails to upload (say, a brief network hiccup), the script immediately gives up and puts the file in the "failed" folder. The retry script only runs once per hour.

**Why it matters:** A 2-second network glitch means a photo waits up to 1 hour before being retried.

**The fix:** Try uploading 2-3 times before giving up. Most transient failures resolve in seconds.

---

### 6. No Alerts When Things Go Wrong

**The problem:** If uploads keep failing, files pile up in the "failed" folder silently. Nobody gets notified.

**Why it matters:** You could lose a day's worth of photos before anyone notices.

**The fix:** Set up automatic email/Slack alerts when failures exceed a threshold.

---

### 7. No Timeout on the Script

**The problem:** The old shell script had a 9-minute timeout. The new Python script has none. If it gets stuck (e.g., a network connection that hangs forever), it blocks all future runs.

**Why it matters:** One stuck upload could stop all photo syncing indefinitely.

**The fix:** Add a maximum runtime (e.g., 8 minutes) so it always finishes before the next scheduled run.

---

## 🟡 Moderate Issues (Address Within a Month)

| # | Problem | Plain English | Fix |
|---|---------|--------------|-----|
| 8 | **Cloud storage not encrypted** | Photos in S3 aren't encrypted at rest — if someone gains access to the S3 bucket, they can read everything | Turn on S3 encryption (a single checkbox in AWS) |
| 9 | **Duplicate code** | The same ~200 lines of code exist in both Python files. A bug fix in one file might be forgotten in the other | Merge shared code into one common file |
| 10 | **Heartbeat log grows forever** | The "I'm alive" log file grows continuously and never gets cleaned up | Add automatic rotation (like the other log files already have) |
| 11 | **Photos scanned twice** | The system scans all camera folders twice at startup — once to count files, once to actually process them | Remove the counting scan; just process directly |
| 12 | **Each photo is read from disk twice** | Once to compute a checksum, once to upload — wastes disk I/O | Combine both operations into a single read |
| 13 | **Possible duplicate uploads after a crash** | If the script crashes right after uploading but before deleting the local file, the photo gets re-uploaded next time | Check S3 first before re-uploading recovered files |
| 14 | **Symlinks could leak data** | If someone creates a shortcut (symlink) pointing from a camera folder to a system folder, those files could be uploaded | Ignore symlinks entirely |

---

## 📈 Can It Handle More Cameras?

| Cameras | Photos per 5 min | Will It Work? | Notes |
|---------|-----------------|---------------|-------|
| **50** | ~25 | ✅ Yes | Current setup handles this fine |
| **100** | ~50 | ✅ Probably | May need a slightly bigger server |
| **500** | ~250 | ⚠️ Struggling | Scanning 500 folders every 5 min gets slow; disk I/O becomes a bottleneck |
| **1000** | ~500 | ❌ No | Single server can't handle this; needs a completely different design |

### What Would 500+ Cameras Need?

Instead of scanning folders on a timer, switch to an **event-driven** approach:

- **Today:** Script wakes up every 5 minutes, scans every folder → "Did anything new arrive?"
- **Better:** The system gets notified instantly when a new file arrives → "Hey, a new photo just landed — upload it now"

This is like the difference between checking your mailbox every 5 minutes vs. getting a doorbell notification when a package arrives.

At scale, you'd add more FTP servers behind a load balancer, all isolated on a dedicated VLAN.

---

## 🏆 Overall Score

| Area | Score | Meaning |
|------|-------|---------|
| **Security** | 3.5/10 | FTP is a hardware constraint; compensating controls (VLAN, firewall) are essential; no file validation |
| **Performance** | 6/10 | Fine for 50 cameras, but wasteful patterns will hurt at scale |
| **Reliability** | 5/10 | Good spool design, but no retries or alerts |
| **Cloud Best Practices** | 4/10 | Missing encryption, archiving rules, and monitoring |
| **Code Quality** | 5/10 | Clean and readable, but too much copy-paste |
| **Scalability** | 4/10 | Tops out around 100–200 cameras |
| **Documentation** | 7/10 | Well above average — clear and helpful |

### **Overall: 4.5 out of 10**

> The core design (stage → upload → verify → delete/quarantine) is **genuinely solid engineering**. But the missing network hardening around FTP, no file validation, and missing production essentials (no alerts, no retries, no encryption-at-rest) mean it's **not ready for production yet**.

---

## 🎯 What to Fix, in Order of Priority

### 🔴 Do This Week (Before Going Live)
1. **Harden FTP** — isolate cameras on a dedicated VLAN, firewall to camera IPs only, one password per camera, lock each camera to its own folder (chroot), disable anonymous access
2. Validate file contents before uploading (check they're real images)
3. Block dangerous filenames (no `..` paths, no symlinks)
4. Move settings to environment variables
5. Turn on S3 encryption
6. Add automatic upload retries (2–3 attempts before giving up)
7. Add a timeout so the script can't run forever

### 🟡 Do This Month
8. Merge duplicate code into a shared module
9. Set up S3 archiving rules (move old photos to cheaper storage)
10. Add failure alerts (email or Slack when things break)
11. Connect to CloudWatch monitoring
12. Write basic tests for the core functions
13. Block public access to the S3 bucket

### 🟢 Do Over Next 3 Months
14. Replace cron with systemd timers (better scheduling)
15. Switch from folder-scanning to event-based file detection
16. Add structured logging (easier to search and analyze)
17. Build an integration test suite
18. Plan architecture for 500+ cameras if growth is expected

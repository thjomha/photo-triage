# People and Search (face service)

Runs **only on your Debian box**. Face data never goes to Cloudflare; the Worker just
relays requests through the tunnel. Photos are never renamed or changed: names, places
and descriptions live in a small SQLite database (`faces.db`).

## 1. Install

```bash
cd /opt/photo-triage && sudo git pull
sudo apt install -y python3-venv
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r faces/requirements.txt
sudo mkdir -p /var/lib/photo-triage && sudo chown photo-triage: /var/lib/photo-triage
sudo -u photo-triage FACES_MODELS=/var/lib/photo-triage/models bash faces/setup_models.sh
```

Then add the service and start it:

```bash
sudo cp deploy/photo-triage-faces.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now photo-triage-faces
sudo systemctl restart photo-triage-lan      # picks up the /faces proxy
```

## 2. Scan (the slow part)

Try one folder first, to check speed and accuracy:

```bash
cd /opt/photo-triage
sudo -u photo-triage env $(grep -v '^#' /etc/photo-triage.env | xargs) \
  FACES_DB=/var/lib/photo-triage/faces.db FACES_MODELS=/var/lib/photo-triage/models \
  nice -n 15 .venv/bin/python -m faces.scan --folder "Camera Archives/SomeFolder" --limit 200
```

Everything, in the background (safe to stop and restart; it resumes):

```bash
... nice -n 15 .venv/bin/python -m faces.scan --sleep 0.05 > ~/scan.log 2>&1 &
```

Useful options: `--no-faces` (dates/places only, fast), `--rescan`, `--prune`
(forget photos that no longer exist; skipped for unmounted shares).
Place names from GPS: `python -m faces.places` (uses OpenStreetMap Nominatim, 1 request/second, cached).

## 3. Use it

Open the app → **👤 People**: press "Group new faces", then type a name for each
group (tick off any wrong faces first). **🔎 Search** finds photos by name, place,
date, folder name, filename or your own description, e.g. `Sara Jasper 2015`.
Click a result for details, the full path, and to add a description or a place for the whole folder.

If a group mixes two people, lower `FACES_CLUSTER_THRESHOLD` (default 0.5); if one person is
split into many groups, raise it a little, or just name each group the same name.

# Photo Triage

Swipe through photos on a network drive and keep or delete each one, from any device.
Progress is stored in Cloudflare D1, so you can stop on your phone and resume on your PC.

```
Browser ──► Worker (UI + D1 progress) ──► Cloudflare Tunnel ──► lan-api (Node) ──► photo drive
```

- **Keep** records the decision only. The file isn't touched.
- **Delete** depends on `DELETE_MODE` in `worker/wrangler.toml`:
  - `"mark"` (default): the photo is only added to a **delete list** in the database. Nothing on the drive changes, so a read-only share works fine.
  - `"move"`: the file is also moved into `_trash/` at the top of its share, keeping its folder structure. Nothing is hard-deleted. Empty `_trash` yourself when you're happy. This needs write access.
- **Delete list** (button on the folder page): every photo marked for deletion, with thumbnails, newest first. Unmark or restore items individually, or **Download CSV** of the whole list.
- **Undo** (button, `Z`, or Backspace) clears the last decision, and in `move` mode also puts the file back.
- A folder shows **done** once every photo still in it has been kept. New photos added later reappear as unreviewed, and the folder flips back to in-progress.
- **Resume** jumps to the last folder you were reviewing, from any device.

> Running on Debian with a UniFi NAS share? Follow [deploy/DEBIAN.md](deploy/DEBIAN.md). It covers the mount, the systemd service and the tunnel step by step. The sections below are the generic version.

## 1. LAN API (on a machine that can see the drive)

The drive must appear as a normal path on that machine, for example an SMB share mounted at `/mnt/photos`, a mapped drive, or local disk on the NAS. Node 18+ is required.

```bash
cd lan-api
npm install            # installs sharp for resizing and EXIF rotation (optional but strongly recommended)
PHOTO_ROOT=/mnt/photos API_TOKEN=$(openssl rand -hex 32) node server.js
```

Keep that token; the Worker needs the same value. Run the service under systemd, pm2 or Docker so it survives reboots. It listens on `127.0.0.1:8787` by default, so run `cloudflared` on the same machine. Set `HOST=0.0.0.0` only if `cloudflared` runs elsewhere.

Without `sharp`, the original files are sent as is. That is slow for full-size camera photos, and HEIC won't display.

### Several shares

To review more than one share or folder, mount each one on the LAN machine, then list them in a JSON file and point the API at it instead of `PHOTO_ROOT`:

```json
{
  "shares": {
    "Camera Archives": "/mnt/photos/Camera Achives",
    "Dad photos": "/mnt/photos/dad_photos",
    "Other NAS": "/mnt/other-nas"
  }
}
```

```bash
CONFIG_FILE=/etc/photo-triage.json API_TOKEN=... node server.js
```

Each share then appears as a top-level folder in the app, with its own progress and its own `_trash`. A share name can't contain `/` or start with a dot. Renaming a share changes the paths stored in the database, so pick names you'll keep. The web app can't mount drives itself, because that needs root and your share credentials on the LAN machine.

## 2. Cloudflare Tunnel

On the same machine, in the Zero Trust dashboard (Networks → Tunnels), create a tunnel and install `cloudflared`. Add a public hostname:

- Hostname: `photos-api.yourdomain.com`
- Service: `http://localhost:8787`

Every request must carry the bearer token, so the API is safe if the hostname is guessed. For defence in depth, also put the hostname behind a Cloudflare Access policy with a **service token**. If you do, set the optional `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` secrets below.

## 3. Worker

```bash
cd worker
npx wrangler d1 create photo-triage            # copy the database_id into wrangler.toml
npx wrangler d1 execute photo-triage --remote --file=schema.sql
# edit wrangler.toml: database_id and LAN_API_URL
npx wrangler secret put LAN_API_TOKEN          # paste the same token as the LAN API
npx wrangler deploy
```

## 4. Lock down the UI (important)

This app can delete your photos, and the Worker itself has no login. Put **Cloudflare Access** in front of the Worker's hostname (Zero Trust → Access → Applications) with an email one-time-PIN or Google policy that allows only you. Without this, anyone who finds the URL can use it.

## Notes and limits

- Only photos directly inside a folder are reviewed when you open it. Subfolders are navigated from the folder list.
- Hidden files and `@eaDir`, `#recycle`, `.`/`$` folders (Synology and similar) are ignored.
- Images are resized to 1800px wide through the tunnel and cached for a day by the browser.
- Deletes and keeps are sent in order in the background. If one fails, the photo is returned to the queue and you get a message.
- Single-user design: one shared progress record. For several independent reviewers, add a user id column (from the Access email header) to `decisions`.

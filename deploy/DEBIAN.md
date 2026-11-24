# Running the LAN side on your Debian instance

Everything here runs on the Debian machine on the Synology. It needs to reach the UniFi NAS over the LAN.

## 1. Mount the UniFi NAS share

Enable SMB (or NFS) for the shared drive in UniFi Drive, then on Debian:

```bash
sudo apt update && sudo apt install -y cifs-utils
sudo mkdir -p /mnt/photos
sudo install -m 600 /dev/null /etc/photo-triage-smb.creds
sudo nano /etc/photo-triage-smb.creds     # username=... / password=...
```

Add to `/etc/fstab` (adjust host and share; see `fstab.example`):

```
//UNAS_IP/ShareName  /mnt/photos  cifs  credentials=/etc/photo-triage-smb.creds,uid=1000,gid=1000,iocharset=utf8,vers=3.0,_netdev,nofail  0  0
```

```bash
sudo systemctl daemon-reload && sudo mount -a && ls /mnt/photos
```

Use a share account with **read and write** permission, because delete moves files into `_trash` on the share.

## 2. Node and the LAN API

```bash
sudo apt install -y nodejs npm            # needs Node 18+; if older, use NodeSource or nvm
node --version
sudo useradd -r -m -d /opt/photo-triage photo-triage    # or reuse the user whose uid matches the mount
sudo cp -r lan-api /opt/photo-triage/ && sudo chown -R photo-triage /opt/photo-triage
cd /opt/photo-triage/lan-api && sudo -u photo-triage npm install
```

Create the env file with a long random token and keep it, because the Worker needs the same value:

```bash
sudo install -m 600 -o photo-triage /dev/null /etc/photo-triage.env
echo "PHOTO_ROOT=/mnt/photos"        | sudo tee    /etc/photo-triage.env
echo "API_TOKEN=$(openssl rand -hex 32)" | sudo tee -a /etc/photo-triage.env
sudo cat /etc/photo-triage.env       # copy API_TOKEN for step 4
```

Install the service:

```bash
sudo cp deploy/photo-triage-lan.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now photo-triage-lan
curl -s -H "Authorization: Bearer $(grep API_TOKEN /etc/photo-triage.env | cut -d= -f2)" localhost:8787/folders
```

You should see JSON listing your top-level folders. If the Debian "instance" is a container with no systemd, run `node server.js` under pm2 or Docker instead.

## 3. Cloudflare Tunnel

1. Cloudflare dashboard, **Zero Trust → Networks → Tunnels → Create a tunnel** (type: Cloudflared). Name it `photo-triage`.
2. Choose **Debian**. The dashboard shows install commands ending in `sudo cloudflared service install <TOKEN>`. Run them on the Debian machine.
3. In the tunnel's **Public Hostname** tab, add:
   - Subdomain/domain: `photos-api.yourdomain.com`
   - Service: `HTTP` → `localhost:8787`
4. Test from any machine:
   ```bash
   curl -i https://photos-api.yourdomain.com/folders        # expect 401 unauthorized
   curl -s -H "Authorization: Bearer <API_TOKEN>" https://photos-api.yourdomain.com/folders
   ```
   A 401 without the token and JSON with it means the tunnel works.

## 4. Point the Worker at it

In `worker/wrangler.toml` set `LAN_API_URL = "https://photos-api.yourdomain.com"`, then:

```bash
cd worker
npx wrangler secret put LAN_API_TOKEN     # paste the API_TOKEN from /etc/photo-triage.env
npx wrangler deploy
```

Finally, put Cloudflare Access in front of the Worker's URL (see the main README).

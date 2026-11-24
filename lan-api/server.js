// LAN file API for photo-triage. Runs next to the drive (or on any machine that has it mounted).
// Node 18+. Optional dependency: sharp (thumbnails / resizing / EXIF rotation).
//
// Single share:
//   PHOTO_ROOT=/mnt/photos API_TOKEN=longrandomstring node server.js
//
// Several shares (each appears as a top-level folder in the app):
//   CONFIG_FILE=/etc/photo-triage.json API_TOKEN=... node server.js
//   where the file looks like { "shares": { "Camera Archives": "/mnt/photos/Camera Achives", "Other": "/mnt/other" } }

import http from 'node:http';
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import { createReadStream } from 'node:fs';
import path from 'node:path';
import { timingSafeEqual } from 'node:crypto';

const TOKEN = process.env.API_TOKEN;
const PORT = Number(process.env.PORT || 8787);
// Bind to loopback by default: cloudflared on the same machine reaches it there.
// Set HOST=0.0.0.0 only if cloudflared runs on a different machine.
const HOST = process.env.HOST || '127.0.0.1';

// ---- shares -------------------------------------------------------------------------------
// SHARES maps a share name to its absolute root. In single-root mode the only key is '' and
// paths carry no share prefix. In multi mode every path starts with the share name.
const SHARES = new Map();
let MULTI = false;

const CONFIG_FILE = process.env.CONFIG_FILE;
// Folders offered by the in-app share picker must live under this directory (where you mount drives).
const SHARES_BASE = path.resolve(process.env.SHARES_BASE || '/mnt');
const validShareName = (name) => typeof name === 'string' && name.trim() === name && name.length > 0 &&
  name.length <= 80 && !/[\\/]/.test(name) && !name.startsWith('.');

function loadShares() {
  if (CONFIG_FILE) {
    let cfg = { shares: {} };
    try {
      cfg = JSON.parse(fsSync.readFileSync(CONFIG_FILE, 'utf8'));
    } catch (e) {
      // A missing file gets a clear message below; anything else (bad JSON, permissions) is a real error.
      if (e.code !== 'ENOENT') throw e;
    }
    for (const [name, p] of Object.entries(cfg.shares || {})) {
      if (!validShareName(name)) throw new Error(`Invalid share name: "${name}"`);
      if (typeof p !== 'string' || !p) throw new Error(`Share "${name}" needs a path`);
      SHARES.set(name, path.resolve(p));
    }
    if (!SHARES.size) throw new Error(`${CONFIG_FILE} has no shares (or does not exist yet). Add at least one: {"shares":{"Name":"/mnt/path"}}`);
    MULTI = true;
  } else if (process.env.PHOTO_ROOT) {
    SHARES.set('', path.resolve(process.env.PHOTO_ROOT));
  } else {
    throw new Error('Set PHOTO_ROOT (one share) or CONFIG_FILE (several shares).');
  }
}

try {
  if (!TOKEN) throw new Error('Set the API_TOKEN environment variable.');
  loadShares();
} catch (e) {
  console.error(e.message);
  process.exit(1);
}

for (const [name, root] of SHARES) {
  if (!fsSync.existsSync(root)) console.warn(`[warn] share "${name || 'default'}" path does not exist: ${root}`);
}

const TRASH_NAME = '_trash'; // lives at the top of each share so rename() stays on one filesystem
const EXT = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp', '.heic', '.heif']);
const MIME = {
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
  '.webp': 'image/webp', '.avif': 'image/avif', '.bmp': 'image/bmp',
  '.heic': 'image/heic', '.heif': 'image/heif',
};

let sharp = null;
try {
  sharp = (await import('sharp')).default;
} catch {
  console.warn('[warn] sharp not installed: serving original files (slow, no HEIC support).');
}

const httpErr = (status, message) => Object.assign(new Error(message), { status });

const isHidden = (name) =>
  name.startsWith('.') || name.startsWith('$') || name === TRASH_NAME ||
  name === '@eaDir' || name === '#recycle' || name === 'Thumbs.db';

const isPhoto = (name) => !isHidden(name) && EXT.has(path.extname(name).toLowerCase());

// Resolve a relative path inside `base`, refusing anything that escapes it.
function resolveIn(base, rel = '') {
  const abs = path.resolve(base, rel);
  if (abs !== base && !abs.startsWith(base + path.sep)) throw httpErr(400, 'bad path');
  return abs;
}

const clean = (rel) => String(rel ?? '').replace(/^\/+|\/+$/g, '');

// Split an app path into { root, name, sub }. `sub` is the path inside the share.
function locate(rel) {
  rel = clean(rel);
  if (!MULTI) return { root: SHARES.get(''), name: '', sub: rel };
  const i = rel.indexOf('/');
  const name = i < 0 ? rel : rel.slice(0, i);
  const root = SHARES.get(name);
  if (!root) throw httpErr(404, 'unknown share');
  return { root, name, sub: i < 0 ? '' : rel.slice(i + 1) };
}

// Resolve inside a share, refusing the trash folder and hidden segments.
function resolvePhotoPath(rel) {
  const loc = locate(rel);
  const abs = resolveIn(loc.root, loc.sub);
  const parts = path.relative(loc.root, abs).split(path.sep).filter(Boolean);
  if (parts.some(isHidden)) throw httpErr(400, 'bad path');
  return { ...loc, abs };
}

function authed(req) {
  const m = /^Bearer (.+)$/.exec(req.headers.authorization || '');
  if (!m) return false;
  const a = Buffer.from(m[1]);
  const b = Buffer.from(TOKEN);
  return a.length === b.length && timingSafeEqual(a, b);
}

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) });
  res.end(data);
}

async function readJson(req) {
  let size = 0;
  const chunks = [];
  for await (const c of req) {
    size += c.length;
    if (size > 10_000) throw httpErr(413, 'body too large');
    chunks.push(c);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString() || '{}'); }
  catch { throw httpErr(400, 'bad json'); }
}

const exists = (p) => fs.access(p).then(() => true, () => false);

async function moveFile(src, dest) {
  await fs.mkdir(path.dirname(dest), { recursive: true });
  try {
    await fs.rename(src, dest);
  } catch (e) {
    if (e.code !== 'EXDEV') throw e;
    await fs.copyFile(src, dest);
    await fs.unlink(src);
  }
}

async function countPhotos(dir) {
  try {
    const entries = await fs.readdir(dir, { withFileTypes: true });
    return entries.filter((e) => e.isFile() && isPhoto(e.name)).length;
  } catch {
    return 0;
  }
}

const byName = (a, b) => a.localeCompare(b, undefined, { numeric: true });

// ---- share management (used by the Settings page) --------------------------------------------
// This only edits the list of shares the app looks at. It never mounts, unmounts, moves or
// deletes anything, and every folder must live under SHARES_BASE.

async function folderInsideBase(p) {
  const base = await fs.realpath(SHARES_BASE).catch(() => SHARES_BASE);
  let real;
  try { real = await fs.realpath(path.resolve(String(p))); }
  catch { throw httpErr(400, 'That folder does not exist'); }
  if (real !== base && !real.startsWith(base + path.sep)) throw httpErr(400, `Folder must be inside ${SHARES_BASE}`);
  if (!(await fs.stat(real)).isDirectory()) throw httpErr(400, 'Not a folder');
  return { real, base };
}

async function saveShares() {
  const data = JSON.stringify({ shares: Object.fromEntries(SHARES) }, null, 2) + '\n';
  try {
    await fs.writeFile(CONFIG_FILE, data);
  } catch (e) {
    throw httpErr(500, `Could not save ${CONFIG_FILE}: ${e.code || e.message}. The service user needs write access to it.`);
  }
}

async function admin(req, res, u) {
  if (!MULTI || !CONFIG_FILE) {
    throw httpErr(400, 'Managing shares in the app needs the LAN API to run with CONFIG_FILE (see README, "Several shares").');
  }
  if (req.method === 'GET' && u.pathname === '/admin/shares') {
    const shares = [...SHARES].map(([name, p]) => ({ name, path: p, exists: fsSync.existsSync(p) }));
    return send(res, 200, { base: SHARES_BASE, shares });
  }
  if (req.method === 'GET' && u.pathname === '/admin/browse') {
    const { real, base } = await folderInsideBase(u.searchParams.get('path') || SHARES_BASE);
    const entries = await fs.readdir(real, { withFileTypes: true });
    const dirs = entries.filter((e) => e.isDirectory() && !isHidden(e.name)).map((e) => e.name).sort(byName);
    return send(res, 200, { path: real, parent: real === base ? null : path.dirname(real), dirs });
  }
  if (req.method === 'POST' && u.pathname === '/admin/shares') {
    const body = await readJson(req);
    const name = String(body.name ?? '').trim();
    if (!validShareName(name)) throw httpErr(400, 'Share name can\'t be empty, contain / or \\, or start with a dot');
    if (SHARES.has(name)) throw httpErr(409, 'A share with that name already exists');
    if (SHARES.size >= 50) throw httpErr(400, 'Too many shares');
    const { real } = await folderInsideBase(body.path);
    if ([...SHARES.values()].includes(real)) throw httpErr(409, 'That folder is already a share');
    SHARES.set(name, real);
    try { await saveShares(); } catch (e) { SHARES.delete(name); throw e; }
    return send(res, 200, { ok: true });
  }
  if (req.method === 'DELETE' && u.pathname === '/admin/shares') {
    const body = await readJson(req);
    const name = String(body.name ?? '');
    if (!SHARES.has(name)) throw httpErr(404, 'unknown share');
    if (SHARES.size === 1) throw httpErr(400, 'Can\'t remove the last share');
    const old = SHARES.get(name);
    SHARES.delete(name);
    try { await saveShares(); } catch (e) { SHARES.set(name, old); throw e; }
    return send(res, 200, { ok: true });
  }
  throw httpErr(404, 'not found');
}

const server = http.createServer(async (req, res) => {
  try {
    if (!authed(req)) return send(res, 401, { error: 'unauthorized' });
    const u = new URL(req.url, 'http://x');
    const rel = clean(u.searchParams.get('path'));
    const atVirtualRoot = MULTI && !rel;

    if (u.pathname.startsWith('/admin/')) return await admin(req, res, u);

    // GET /folders?path=  -> immediate subfolders with the number of photos directly inside each.
    // In multi-share mode the top level lists the shares.
    if (req.method === 'GET' && u.pathname === '/folders') {
      if (atVirtualRoot) {
        const folders = [...SHARES.keys()].sort(byName).map((n) => ({ name: n, path: n, photoCount: 0 }));
        return send(res, 200, { path: '', photoCount: 0, folders });
      }
      const { abs: dir } = resolvePhotoPath(rel);
      const entries = await fs.readdir(dir, { withFileTypes: true });
      const dirs = entries.filter((e) => e.isDirectory() && !isHidden(e.name));
      const folders = [];
      for (let i = 0; i < dirs.length; i += 8) {
        const batch = dirs.slice(i, i + 8);
        const counts = await Promise.all(batch.map((d) => countPhotos(path.join(dir, d.name))));
        batch.forEach((d, j) => folders.push({
          name: d.name,
          path: rel ? `${rel}/${d.name}` : d.name,
          photoCount: counts[j],
        }));
      }
      folders.sort((a, b) => byName(a.name, b.name));
      return send(res, 200, { path: rel, photoCount: await countPhotos(dir), folders });
    }

    // GET /photos?path=  -> file names of photos directly inside the folder
    if (req.method === 'GET' && u.pathname === '/photos') {
      if (atVirtualRoot) return send(res, 200, { path: '', photos: [] });
      const { abs: dir } = resolvePhotoPath(rel);
      const entries = await fs.readdir(dir, { withFileTypes: true });
      const photos = entries.filter((e) => e.isFile() && isPhoto(e.name)).map((e) => e.name).sort(byName);
      return send(res, 200, { path: rel, photos });
    }

    // GET /img?path=&w=  -> the image, optionally resized
    if (req.method === 'GET' && u.pathname === '/img') {
      const { abs: file } = resolvePhotoPath(rel);
      const ext = path.extname(file).toLowerCase();
      if (!EXT.has(ext)) throw httpErr(400, 'not an image');
      const st = await fs.stat(file);
      if (!st.isFile()) throw httpErr(404, 'not found');
      const w = Math.min(Number(u.searchParams.get('w')) || 0, 4000);
      const headers = { 'cache-control': 'private, max-age=86400' };
      if (sharp && w) {
        const buf = await sharp(file).rotate().resize({ width: w, withoutEnlargement: true })
          .jpeg({ quality: 80 }).toBuffer();
        res.writeHead(200, { ...headers, 'content-type': 'image/jpeg', 'content-length': buf.length });
        return res.end(buf);
      }
      res.writeHead(200, { ...headers, 'content-type': MIME[ext], 'content-length': st.size });
      const stream = createReadStream(file);
      stream.on('error', () => res.destroy());
      return stream.pipe(res);
    }

    // POST /delete {path}  -> move the photo into the share's _trash folder (never a hard delete).
    // The Worker only calls this when DELETE_MODE = "move".
    if (req.method === 'POST' && u.pathname === '/delete') {
      const body = await readJson(req);
      const { abs: file, root, name, sub } = resolvePhotoPath(String(body.path || ''));
      if (!isPhoto(path.basename(file))) throw httpErr(400, 'not a photo');
      const st = await fs.stat(file).catch(() => null);
      if (!st?.isFile()) throw httpErr(404, 'not found');
      const trashRoot = path.join(root, TRASH_NAME);
      let dest = path.join(trashRoot, sub);
      if (await exists(dest)) {
        const { dir, name: base, ext } = path.parse(dest);
        dest = path.join(dir, `${base}.${Date.now()}${ext}`);
      }
      await moveFile(file, dest);
      const inTrash = path.relative(trashRoot, dest).split(path.sep).join('/');
      return send(res, 200, { trashed: MULTI ? `${name}/${inTrash}` : inTrash });
    }

    // POST /restore {trashed, path}  -> undo a delete
    if (req.method === 'POST' && u.pathname === '/restore') {
      const body = await readJson(req);
      const t = locate(String(body.trashed || ''));
      const target = resolvePhotoPath(String(body.path || ''));
      if (t.root !== target.root) throw httpErr(400, 'share mismatch');
      const src = resolveIn(path.join(t.root, TRASH_NAME), t.sub);
      if (!(await exists(src))) throw httpErr(404, 'not in trash');
      if (await exists(target.abs)) throw httpErr(409, 'a file already exists at the original location');
      await moveFile(src, target.abs);
      return send(res, 200, { ok: true });
    }

    send(res, 404, { error: 'not found' });
  } catch (e) {
    const status = e.status || (e.code === 'ENOENT' ? 404 : 500);
    if (status === 500) console.error(e);
    if (!res.headersSent) send(res, status, { error: e.message });
    else res.destroy();
  }
});

server.listen(PORT, HOST, () => {
  const list = [...SHARES].map(([n, r]) => `${n || 'default'} -> ${r}`).join(', ');
  console.log(`photo-triage LAN API on http://${HOST}:${PORT} (${list})`);
});

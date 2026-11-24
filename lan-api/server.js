// LAN file API for photo-triage. Runs next to the drive (or on any machine that has it mounted).
// Node 18+. Optional dependency: sharp (thumbnails / resizing / EXIF rotation).
//
//   PHOTO_ROOT=/mnt/photos API_TOKEN=longrandomstring node server.js

import http from 'node:http';
import fs from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import path from 'node:path';
import { timingSafeEqual } from 'node:crypto';

const ROOT = process.env.PHOTO_ROOT && path.resolve(process.env.PHOTO_ROOT);
const TOKEN = process.env.API_TOKEN;
const PORT = Number(process.env.PORT || 8787);
// Bind to loopback by default: cloudflared on the same machine reaches it there.
// Set HOST=0.0.0.0 only if cloudflared runs on a different machine.
const HOST = process.env.HOST || '127.0.0.1';

if (!ROOT || !TOKEN) {
  console.error('Set PHOTO_ROOT and API_TOKEN environment variables.');
  process.exit(1);
}

const TRASH_NAME = '_trash';
const TRASH = path.join(ROOT, TRASH_NAME); // inside ROOT so rename() stays on one filesystem
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

// Resolve inside the photo root and refuse the trash folder and hidden segments.
function resolvePhotoPath(rel = '') {
  const abs = resolveIn(ROOT, rel);
  const parts = path.relative(ROOT, abs).split(path.sep).filter(Boolean);
  if (parts.some(isHidden)) throw httpErr(400, 'bad path');
  return abs;
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

const server = http.createServer(async (req, res) => {
  try {
    if (!authed(req)) return send(res, 401, { error: 'unauthorized' });
    const u = new URL(req.url, 'http://x');
    const rel = (u.searchParams.get('path') ?? '').replace(/^\/+/, '');

    // GET /folders?path=  -> immediate subfolders with the number of photos directly inside each
    if (req.method === 'GET' && u.pathname === '/folders') {
      const dir = resolvePhotoPath(rel);
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
      folders.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
      return send(res, 200, { path: rel, photoCount: await countPhotos(dir), folders });
    }

    // GET /photos?path=  -> file names of photos directly inside the folder
    if (req.method === 'GET' && u.pathname === '/photos') {
      const dir = resolvePhotoPath(rel);
      const entries = await fs.readdir(dir, { withFileTypes: true });
      const photos = entries.filter((e) => e.isFile() && isPhoto(e.name)).map((e) => e.name)
        .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
      return send(res, 200, { path: rel, photos });
    }

    // GET /img?path=&w=  -> the image, optionally resized
    if (req.method === 'GET' && u.pathname === '/img') {
      const file = resolvePhotoPath(rel);
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

    // POST /delete {path}  -> move the photo into _trash (never a hard delete)
    if (req.method === 'POST' && u.pathname === '/delete') {
      const body = await readJson(req);
      const file = resolvePhotoPath(String(body.path || ''));
      if (!isPhoto(path.basename(file))) throw httpErr(400, 'not a photo');
      const st = await fs.stat(file).catch(() => null);
      if (!st?.isFile()) throw httpErr(404, 'not found');
      const relPath = path.relative(ROOT, file);
      let dest = path.join(TRASH, relPath);
      if (await exists(dest)) {
        const { dir, name, ext } = path.parse(dest);
        dest = path.join(dir, `${name}.${Date.now()}${ext}`);
      }
      await moveFile(file, dest);
      return send(res, 200, { trashed: path.relative(TRASH, dest).split(path.sep).join('/') });
    }

    // POST /restore {trashed, path}  -> undo a delete
    if (req.method === 'POST' && u.pathname === '/restore') {
      const body = await readJson(req);
      const src = resolveIn(TRASH, String(body.trashed || ''));
      const dest = resolvePhotoPath(String(body.path || ''));
      if (!(await exists(src))) throw httpErr(404, 'not in trash');
      if (await exists(dest)) throw httpErr(409, 'a file already exists at the original location');
      await moveFile(src, dest);
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

server.listen(PORT, HOST, () => console.log(`photo-triage LAN API on http://${HOST}:${PORT}, root ${ROOT}`));

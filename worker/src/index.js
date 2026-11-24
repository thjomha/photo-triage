// Cloudflare Worker: serves the UI, stores review progress in D1, and talks to the LAN file API
// through a Cloudflare Tunnel.

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const dirname = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
const esc = (s) => s.replace(/[\\%_]/g, '\\$&'); // escape LIKE wildcards

function lan(env, path, init = {}) {
  const headers = new Headers(init.headers);
  headers.set('authorization', `Bearer ${env.LAN_API_TOKEN}`);
  // Optional: if the tunnel hostname is itself behind Cloudflare Access, add a service token.
  if (env.CF_ACCESS_CLIENT_ID) {
    headers.set('CF-Access-Client-Id', env.CF_ACCESS_CLIENT_ID);
    headers.set('CF-Access-Client-Secret', env.CF_ACCESS_CLIENT_SECRET);
  }
  return fetch(env.LAN_API_URL.replace(/\/$/, '') + path, { ...init, headers });
}

async function lanJson(env, path, init) {
  const r = await lan(env, path, init);
  if (!r.ok) {
    const msg = await r.json().then((j) => j.error, () => null);
    throw Object.assign(new Error(msg || `LAN API ${r.status}`), { status: r.status === 404 ? 404 : 502 });
  }
  return r.json();
}

const post = (body) => ({
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

async function setMeta(env, key, value) {
  await env.DB.prepare('INSERT INTO meta (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = ?2')
    .bind(key, value).run();
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);

    try {
      const route = `${request.method} ${url.pathname}`;
      const path = (url.searchParams.get('path') ?? '').replace(/^\/+|\/+$/g, '');

      // Folder browser: LAN folder list merged with review progress from D1.
      if (route === 'GET /api/folders') {
        const lanData = await lanJson(env, `/folders?path=${encodeURIComponent(path)}`);
        const prefix = path ? path + '/' : '';
        const { results } = await env.DB.prepare(
          `SELECT folder,
                  SUM(decision = 'keep') AS kept,
                  SUM(decision = 'delete') AS deleted
             FROM decisions
            WHERE folder = ?1 OR folder LIKE ?2 ESCAPE '\\'
            GROUP BY folder`,
        ).bind(path, esc(prefix) + '%').all();

        const direct = new Map(); // folder path -> {kept, deleted}
        const deep = new Map();   // child folder path -> reviewed count somewhere below it
        for (const r of results) {
          direct.set(r.folder, { kept: r.kept, deleted: r.deleted });
          if (r.folder !== path) {
            const child = prefix + r.folder.slice(prefix.length).split('/')[0];
            deep.set(child, (deep.get(child) || 0) + r.kept + r.deleted);
          }
        }

        const status = (photoCount, d) => {
          const kept = d?.kept || 0, deleted = d?.deleted || 0;
          if (photoCount > 0 && kept >= photoCount) return 'done';
          if (kept + deleted > 0) return 'partial';
          return photoCount > 0 ? 'new' : 'empty';
        };

        const folders = lanData.folders.map((f) => {
          const d = direct.get(f.path) || { kept: 0, deleted: 0 };
          return {
            ...f,
            kept: d.kept,
            deleted: d.deleted,
            reviewedInside: deep.get(f.path) || 0,
            status: status(f.photoCount, d),
          };
        });
        const self = direct.get(path) || { kept: 0, deleted: 0 };
        return json({
          path,
          photoCount: lanData.photoCount,
          kept: self.kept,
          deleted: self.deleted,
          status: status(lanData.photoCount, self),
          folders,
        });
      }

      // Photos still to review in a folder (listed on the LAN minus those already decided).
      if (route === 'GET /api/photos') {
        const [lanData, decided] = await Promise.all([
          lanJson(env, `/photos?path=${encodeURIComponent(path)}`),
          env.DB.prepare('SELECT path, decision FROM decisions WHERE folder = ?1').bind(path).all(),
        ]);
        const done = new Set(decided.results.map((r) => r.path));
        const prefix = path ? path + '/' : '';
        const pending = lanData.photos.filter((n) => !done.has(prefix + n));
        await setMeta(env, 'last_folder', path);
        return json({
          path,
          total: lanData.photos.length,
          kept: decided.results.filter((r) => r.decision === 'keep').length,
          deleted: decided.results.filter((r) => r.decision === 'delete').length,
          pending: pending.map((n) => prefix + n),
        });
      }

      if (route === 'GET /api/img') {
        const w = url.searchParams.get('w') || '1800';
        const r = await lan(env, `/img?path=${encodeURIComponent(path)}&w=${encodeURIComponent(w)}`);
        if (!r.ok) return json({ error: 'image unavailable' }, 502);
        return new Response(r.body, {
          headers: {
            'content-type': r.headers.get('content-type') || 'image/jpeg',
            'cache-control': 'private, max-age=86400',
          },
        });
      }

      if (route === 'POST /api/decide') {
        const { path: p, decision } = await request.json();
        if (!p || !['keep', 'delete'].includes(decision)) return json({ error: 'bad request' }, 400);
        let trashed = null;
        if (decision === 'delete') {
          // Move on the LAN first; only record the decision if that worked.
          trashed = (await lanJson(env, '/delete', post({ path: p }))).trashed;
        }
        await env.DB.prepare(
          `INSERT INTO decisions (path, folder, decision, trashed, decided_at)
           VALUES (?1, ?2, ?3, ?4, ?5)
           ON CONFLICT(path) DO UPDATE SET decision = ?3, trashed = ?4, decided_at = ?5`,
        ).bind(p, dirname(p), decision, trashed, Date.now()).run();
        await setMeta(env, 'last_folder', dirname(p));
        return json({ ok: true });
      }

      if (route === 'POST /api/undo') {
        const { path: p } = await request.json();
        const row = await env.DB.prepare('SELECT decision, trashed FROM decisions WHERE path = ?1').bind(p).first();
        if (!row) return json({ ok: true });
        if (row.decision === 'delete' && row.trashed) {
          await lanJson(env, '/restore', post({ trashed: row.trashed, path: p }));
        }
        await env.DB.prepare('DELETE FROM decisions WHERE path = ?1').bind(p).run();
        return json({ ok: true });
      }

      if (route === 'GET /api/resume') {
        const row = await env.DB.prepare("SELECT value FROM meta WHERE key = 'last_folder'").first();
        return json({ folder: row ? row.value : null });
      }

      return json({ error: 'not found' }, 404);
    } catch (e) {
      return json({ error: e.message || 'error' }, e.status || 500);
    }
  },
};

// Cloudflare Worker: serves the UI, stores review progress in D1, and talks to the LAN file API
// through a Cloudflare Tunnel.
//
// Delete behaviour:
//   "mark" (default)  "Delete" only records the photo in D1. Nothing on the drive changes.
//   "move"            "Delete" also moves the file into _trash on the share (needs write access).
// The mode is a setting in the app (stored in D1). DELETE_MODE in wrangler.toml is only the
// starting value, used until someone changes the setting in the app.

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const dirname = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
const esc = (s) => s.replace(/[\\%_]/g, '\\$&'); // escape LIKE wildcards
const MODES = ['mark', 'move'];

async function getDeleteMode(env) {
  const row = await env.DB.prepare("SELECT value FROM meta WHERE key = 'delete_mode'").first();
  if (row && MODES.includes(row.value)) return { mode: row.value, source: 'app' };
  return { mode: env.DELETE_MODE === 'move' ? 'move' : 'mark', source: 'default' };
}

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

// Marked-for-delete query shared by the JSON list and the CSV export.
function markedQuery(path) {
  const prefix = path ? path + '/' : '';
  return {
    where: `decision = 'delete' AND (?1 = '' OR path LIKE ?2 ESCAPE '\\')`,
    args: [path, esc(prefix) + '%'],
  };
}

const csvCell = (v) => `"${String(v).replace(/"/g, '""')}"`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);

    try {
      const route = `${request.method} ${url.pathname}`;
      const path = (url.searchParams.get('path') ?? '').replace(/^\/+|\/+$/g, '');

      if (route === 'GET /api/config') return json({ deleteMode: (await getDeleteMode(env)).mode });

      // ---- settings page ----
      if (route === 'GET /api/settings') {
        const { mode, source } = await getDeleteMode(env);
        const pending = await env.DB.prepare(
          "SELECT COUNT(*) AS c FROM decisions WHERE decision = 'delete' AND trashed IS NULL",
        ).first();
        return json({ deleteMode: mode, source, pendingMove: pending.c });
      }

      if (route === 'POST /api/settings') {
        const { deleteMode } = await request.json();
        if (!MODES.includes(deleteMode)) return json({ error: 'bad request' }, 400);
        await setMeta(env, 'delete_mode', deleteMode);
        return json({ ok: true, deleteMode });
      }

      // Move photos that are already marked into _trash, a small batch per request. The caller loops,
      // passing back `last` as `after`, so a file that fails is skipped rather than retried forever.
      if (route === 'POST /api/apply') {
        const body = await request.json().catch(() => ({}));
        const after = typeof body.after === 'string' ? body.after : '';
        const { results } = await env.DB.prepare(
          `SELECT path FROM decisions
            WHERE decision = 'delete' AND trashed IS NULL AND path > ?1
            ORDER BY path LIMIT 15`,
        ).bind(after).all();
        let moved = 0;
        const failed = [];
        for (const r of results) {
          try {
            const { trashed } = await lanJson(env, '/delete', post({ path: r.path }));
            await env.DB.prepare('UPDATE decisions SET trashed = ?1 WHERE path = ?2').bind(trashed, r.path).run();
            moved++;
          } catch (e) {
            failed.push({ path: r.path, error: e.message });
          }
        }
        const last = results.length ? results[results.length - 1].path : after;
        const rem = await env.DB.prepare(
          "SELECT COUNT(*) AS c FROM decisions WHERE decision = 'delete' AND trashed IS NULL AND path > ?1",
        ).bind(last).first();
        return json({ moved, failed, last, remaining: rem.c, done: results.length === 0 || rem.c === 0 });
      }

      // ---- network shares (managed on the LAN API, which edits its own config file) ----
      if (route === 'GET /api/shares') return json(await lanJson(env, '/admin/shares'));
      if (route === 'GET /api/shares/browse') {
        const dir = url.searchParams.get('dir') || '';
        return json(await lanJson(env, `/admin/browse?path=${encodeURIComponent(dir)}`));
      }
      if (route === 'POST /api/shares') {
        const { name, path: p } = await request.json();
        return json(await lanJson(env, '/admin/shares', post({ name, path: p })));
      }
      if (route === 'DELETE /api/shares') {
        const { name } = await request.json();
        return json(await lanJson(env, '/admin/shares', { ...post({ name }), method: 'DELETE' }));
      }

      // Folder browser: LAN folder list merged with review progress from D1.
      if (route === 'GET /api/folders') {
        const lanData = await lanJson(env, `/folders?path=${encodeURIComponent(path)}`);
        const prefix = path ? path + '/' : '';
        const { results } = await env.DB.prepare(
          `SELECT folder,
                  SUM(decision = 'keep') AS kept,
                  SUM(decision = 'delete') AS deleted,
                  SUM(decision = 'delete' AND trashed IS NULL) AS marked
             FROM decisions
            WHERE folder = ?1 OR folder LIKE ?2 ESCAPE '\\'
            GROUP BY folder`,
        ).bind(path, esc(prefix) + '%').all();
        const totalRow = await env.DB.prepare("SELECT COUNT(*) AS c FROM decisions WHERE decision = 'delete'").first();

        const direct = new Map(); // folder path -> {kept, deleted, marked}
        const deep = new Map();   // child folder path -> reviewed count somewhere below it
        for (const r of results) {
          direct.set(r.folder, { kept: r.kept, deleted: r.deleted, marked: r.marked });
          if (r.folder !== path) {
            const child = prefix + r.folder.slice(prefix.length).split('/')[0];
            deep.set(child, (deep.get(child) || 0) + r.kept + r.deleted);
          }
        }

        // "marked" photos are still on the drive, so they count towards what's left to review.
        const status = (photoCount, d) => {
          const kept = d?.kept || 0, deleted = d?.deleted || 0, marked = d?.marked || 0;
          if (photoCount > 0 && kept + marked >= photoCount) return 'done';
          if (kept + deleted > 0) return 'partial';
          return photoCount > 0 ? 'new' : 'empty';
        };
        const zero = { kept: 0, deleted: 0, marked: 0 };

        const folders = lanData.folders.map((f) => {
          const d = direct.get(f.path) || zero;
          return {
            ...f,
            kept: d.kept,
            deleted: d.deleted,
            marked: d.marked,
            reviewedInside: deep.get(f.path) || 0,
            status: status(f.photoCount, d),
          };
        });
        const self = direct.get(path) || zero;
        return json({
          path,
          photoCount: lanData.photoCount,
          kept: self.kept,
          deleted: self.deleted,
          marked: self.marked,
          status: status(lanData.photoCount, self),
          deleteListTotal: totalRow.c,
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
        if (decision === 'delete' && (await getDeleteMode(env)).mode === 'move') {
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

      // Undo a decision: puts a moved file back (move mode) and clears the record.
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

      // The delete list: every photo marked for deletion, newest first. ?path= limits to a folder tree.
      if (route === 'GET /api/marked') {
        const limit = Math.min(Math.max(Number(url.searchParams.get('limit')) || 100, 1), 500);
        const offset = Math.max(Number(url.searchParams.get('offset')) || 0, 0);
        const { where, args } = markedQuery(path);
        const total = await env.DB.prepare(`SELECT COUNT(*) AS c FROM decisions WHERE ${where}`).bind(...args).first();
        const { results } = await env.DB.prepare(
          `SELECT path, decided_at, (trashed IS NOT NULL) AS moved
             FROM decisions WHERE ${where}
            ORDER BY decided_at DESC, path
            LIMIT ?3 OFFSET ?4`,
        ).bind(...args, limit, offset).all();
        return json({
          total: total.c,
          offset,
          items: results.map((r) => ({ path: r.path, decidedAt: r.decided_at, moved: !!r.moved })),
        });
      }

      if (route === 'GET /api/marked.csv') {
        const { where, args } = markedQuery(path);
        const { results } = await env.DB.prepare(
          `SELECT path, decided_at, (trashed IS NOT NULL) AS moved
             FROM decisions WHERE ${where} ORDER BY path LIMIT 100000`,
        ).bind(...args).all();
        const lines = ['path,marked_at,moved_to_trash'].concat(
          results.map((r) => [csvCell(r.path), new Date(r.decided_at).toISOString(), r.moved ? 'yes' : 'no'].join(',')),
        );
        return new Response(lines.join('\r\n') + '\r\n', {
          headers: {
            'content-type': 'text/csv; charset=utf-8',
            'content-disposition': 'attachment; filename="delete-list.csv"',
          },
        });
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

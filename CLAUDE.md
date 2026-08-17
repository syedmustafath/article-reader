# Tolkien — working conventions

A free read-aloud app for saved articles and EPUBs (natural neural voices,
word-synced highlighting, mood music, highlights/notes). FastAPI backend + a
single-file frontend, deployed on Render, storage in Supabase.

## Golden rules

1. **Never push to `main`.** `main` auto-deploys to the user's live app, which they
   read on their phone. Always: `git checkout -b <branch>` → commit → `git push -u
   origin <branch>` → `gh pr create`. **The user tests and merges the PR themselves.**
2. **Verify before opening a PR.** See "Verification" below — at minimum the JS and
   Python syntax checks, plus a functional check in the browser for frontend work.
3. **Don't break the word-sync invariant** (see below). It is the most fragile part of
   the app and the easiest thing to silently destroy.

## Layout

- `server.py` (~890 lines) — all backend: extraction, EPUB parsing, TTS, jobs, auth,
  and every Supabase-backed endpoint.
- `static/index.html` (~2000 lines) — the *entire* frontend: `<style>`, all view
  markup, and one big `<script>`. No build step, no service worker, no framework.
- `render.yaml` / `Procfile` — deploy config. `requirements.txt` — Python deps.

### ⚠️ Two-file conflict hazard (read this before parallel work)

Because the app is two large files, **concurrent branches collide easily** — a past PR
conflicted with another over two *adjacent* lines of global declarations. When several
agents/branches are in flight:

- Stay inside your assigned **region** of `static/index.html`. Rough map:
  `<style>` 15–247 · view markup 288–470 · globals ~525–560 · placeholders ~567 ·
  views/sheets ~817–900 · synthesis ~1127 · highlight sync + pointer ~1268 ·
  highlights screen ~1464 · seek/resume ~1678–1730 · book search ~1731 ·
  MediaSession ~1768 · controls ~1820 · share ~1912 · auth+init ~1950.
- **Add new globals at the END of the globals block**, and new functions at the end of
  your region — not wedged between existing lines. This turns most merges trivial.
- Prefer a new endpoint in `server.py` over editing an existing one.

## Architecture invariants

**Word-sync / char offsets (most fragile).** `TEXT` is a flat plain-text string. TTS
chunks (`CHUNKS[].start/end`), edge-tts word marks, `[data-s]` spans, user highlights,
and `PARA_RANGES` are **all character offsets into `TEXT`**. `renderArticle()` does not
split words itself — it builds word spans from the TTS marks and fills the gaps. Rules:
every text run is a `[data-s]` span whose value is its char offset; `data-s` is
monotonic in document order; word spans keep `class="w"` + `data-i` = their index in
`window._allMarks`. Rich formatting wraps these leaf spans — it never replaces them.

**Job + poll.** Cloudflare resets requests where the origin takes >~1s, so all slow
work returns `{job: id}` immediately (`start_job`) and the client polls
`/api/job/{id}` (`runJob` on the frontend). Any new slow endpoint must follow this.

**Auth + per-user scoping.** Google sign-in via Supabase Auth. Every data endpoint
takes `user: dict = Depends(current_user)` and **every** PostgREST query includes
`&user_id=eq.{uid}` — including id-addressed GET/PATCH/DELETE, so a user can't touch
another's rows by id. New inserts must set `user_id`. Jobs are owner-checked too.
A global `fetch` wrapper on the frontend attaches the bearer token to `/api/*`.

**Sanitization.** Article/EPUB HTML is parsed by `html_to_model()` in `server.py`,
which is *also* the sanitizer: only allowlisted tags/attrs survive; `img src` is
restricted to `https:`/`data:image/`, `a href` to http(s)/mailto. Don't bypass it.

## Verification

Run these before opening a PR (all verified working):

```bash
# JS syntax (extracts the big inline <script> and checks it)
python3 -c "
import re
html=open('static/index.html').read()
js=max(re.findall(r'<script>(.*?)</script>',html,re.S),key=len)
open('/tmp/_app.js','w').write(js)
" && node --check /tmp/_app.js
```

```bash
./.venv/bin/python -c "import server; print('ok')"
```

**Functional checks.** Run the app locally and drive it in a browser:

```bash
./.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8788
```

With no `SUPABASE_*` env vars the auth gate is bypassed (open mode) — good for pure
UI work. For endpoint work, write a small in-memory FastAPI **mock of Supabase**
(implement `/auth/v1/user` returning a fake user for a bearer token, plus the
PostgREST paths you need), run it on another port, and start the app with
`SUPABASE_URL=http://127.0.0.1:<port> SUPABASE_KEY=svc SUPABASE_ANON_KEY=anon`.
Keep such scratch files out of the commit.

Check the browser console for errors, and prefer asserting real invariants (e.g.
`data-s` monotonic, word spans aligned to `_allMarks`) over eyeballing.

## Notes

- Free Render tier sleeps when idle and audio clips live only in server memory, so
  `/audio/{id}.mp3` can 404; the client self-heals via `recoverPlayback()`.
- iOS standalone PWA suspends JS when all audio stops — a silent `KeepAlive` loop
  holds the audio session so lock-screen resume keeps working.
- Schema changes are applied by the user in the Supabase SQL editor. If a change needs
  a new column, say so explicitly in the PR body with the exact SQL.

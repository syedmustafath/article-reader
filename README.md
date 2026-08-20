# Natural Voice Reader

A free, local read-aloud for saved articles — clean reader view + natural neural
voices + word-by-word highlighting. A DIY replacement for paid read-aloud features.

- **Voices:** Microsoft Edge neural voices via [`edge-tts`](https://github.com/rany2/edge-tts) (free, no API key, needs internet).
- **Article extraction:** paste a URL and [`trafilatura`](https://trafilatura.readthedocs.io/) pulls the clean article text (or paste raw text).
- **Fast start:** the article is synthesized in paragraph-sized chunks. The first (small) chunk plays within seconds; later chunks prefetch while you listen.
- **Highlighting:** edge-tts word-boundary timings are aligned to the text so the current word is highlighted as it's spoken. Click any word to jump there.
- **Reading list:** save articles to a synced list (Supabase). The home screen is the list; tap an article to open the reader. "Add article" saves a URL (the title is fetched on add; the full text is re-extracted on open).

## Reading-list storage (Supabase)

The list is stored in a free Supabase Postgres DB so it syncs across devices and
survives restarts (Render's free filesystem is ephemeral, so a hosted DB is
required). Without the env vars below, the app still runs but the list endpoints
return a clear 503.

1. Create a free project at [supabase.com](https://supabase.com).
2. In the SQL editor, create the table:
   ```sql
   create table articles (
     id uuid primary key default gen_random_uuid(),
     url text not null,
     title text,
     domain text,
     est_minutes int,
     added_at timestamptz default now()
   );
   ```
3. Project Settings → API: copy the **Project URL** and the **service_role** key.
4. Set env vars `SUPABASE_URL` and `SUPABASE_KEY` — in Render (Environment tab)
   and locally (`export SUPABASE_URL=… SUPABASE_KEY=…`) for testing.

## Rich reading (formatting + images)

Articles and EPUB chapters render with real formatting — headings, bold/italic,
blockquotes, lists, links, and images — like a reading app, while the word-by-word
narration highlight stays perfectly in sync. Extraction returns a structured model
(`{text, blocks, inlines, images}`) where `text` is the flat speakable stream the
TTS/highlight engine indexes into and `blocks`/`inlines`/`images` carry character
offsets into it; the reader frames that stream with rich elements without disturbing
any offset. EPUB images are inlined as data-URIs; each article/book also stores its
lead image / cover, shown on cards, the reader hero, and the lock screen (a generated
contour-line placeholder is used when there's no image).

Add the columns (Supabase SQL editor):

```sql
alter table articles      add column cover text;
alter table books         add column cover text;
alter table book_chapters add column html  text;
```

## Accounts (Google sign-in + per-user libraries)

Users sign in with Google (via Supabase Auth); every article/book/highlight is
scoped to the signed-in user, so each account only sees its own library. The
backend validates each request's token against Supabase's `/auth/v1/user` and
scopes every query by `user_id`; the frontend gates the whole app behind a
"Continue with Google" screen.

**Setup (one-time):**

1. **Google Cloud** → create an OAuth 2.0 **Web** client (+ configure the consent
   screen). Authorized redirect URI:
   `https://<project-ref>.supabase.co/auth/v1/callback`.
2. **Supabase dashboard** → Authentication → Providers → **Google**: enable and paste
   the Client ID / Secret. Under Authentication → URL Configuration, set the **Site
   URL** and add the app's URL to **Redirect URLs**.
3. **Env** → set `SUPABASE_ANON_KEY` (Supabase Settings → API → *anon* public key) in
   Render and locally. `SUPABASE_KEY` stays the secret **service_role** key.
4. **SQL** (Supabase editor) — add ownership + a safety net:
   ```sql
   alter table articles      add column user_id uuid references auth.users(id) on delete cascade;
   alter table books         add column user_id uuid references auth.users(id) on delete cascade;
   alter table book_chapters add column user_id uuid references auth.users(id) on delete cascade;
   alter table highlights    add column user_id uuid references auth.users(id) on delete cascade;
   create index on articles(user_id); create index on books(user_id);
   create index on book_chapters(user_id); create index on highlights(user_id);
   alter table articles enable row level security;
   alter table books enable row level security;
   alter table book_chapters enable row level security;
   alter table highlights enable row level security;
   ```
5. **Claim your existing library** — after you sign in once, copy your UUID from
   Authentication → Users and run:
   ```sql
   update articles      set user_id = '<YOUR_UUID>' where user_id is null;
   update books         set user_id = '<YOUR_UUID>' where user_id is null;
   update book_chapters set user_id = '<YOUR_UUID>' where user_id is null;
   update highlights    set user_id = '<YOUR_UUID>' where user_id is null;
   ```

Access is open to **any Google account**. To restrict it later, add an email
allowlist check in `current_user` (see the hook comment in `server.py`).

## Background mood music (optional)

Off by default; a toggle + volume live in the reader's settings. When on, each
paragraph is labelled with a mood (Claude Haiku, cached per document) and the app
plays a generative Web-Audio ambient score that crossfades between moods as
narration advances. Set `ANTHROPIC_API_KEY` (Render env + local) to enable it; add
the cache columns:

```sql
alter table articles add column moods jsonb;
alter table book_chapters add column moods jsonb;
```

## Listening-time stats

Profile → Stats tracks how many minutes you actually spend listening (wall-clock
time narration is playing, independent of speed), synced to your account so it
survives across devices. Add the table:

```sql
create table listening_daily (
  user_id uuid references auth.users(id) on delete cascade,
  day date not null,
  seconds int not null default 0,
  primary key (user_id, day)
);
alter table listening_daily enable row level security;
```

## Share to Tolkien

Send an article straight to your reading list from wherever you're reading it, via
the OS share sheet. The app reads a shared URL from `?share=<url>` on load, adds it
(same flow as "+ Add"), and shows a brief "Added ✓ <title>" toast. Because the list
lives in Supabase, it syncs to your installed PWA even if the share opens in a plain
browser tab.

- **Android / desktop PWA:** built in — the app declares a Web Share
  [`share_target`](https://developer.mozilla.org/docs/Web/Manifest/share_target) in
  the manifest, so once installed, "Tolkien" appears in the native share sheet.
- **iOS:** Safari doesn't support `share_target` for PWAs, so add a one-time Apple
  **Shortcut** that appears in the share sheet:
  1. Shortcuts app → **+** (new) → **Add Action**.
  2. Search **Receive** → "Receive input from Share Sheet"; set the accepted types to
     **URLs** and **Safari web pages**. Toggle **Show in Share Sheet** on.
  3. Add action **Text** → type `https://YOUR-APP.onrender.com/?share=` then insert the
     **Shortcut Input** variable right after the `=` (so it becomes
     `…/?share=<the shared URL>`).
  4. Add action **Open URLs** → pass it that **Text**.
  5. Name it **Add to Tolkien** and save.

  Replace `YOUR-APP.onrender.com` with your real Render hostname (find it in the Render
  dashboard). Now: any app → Share → **Add to Tolkien** → it opens briefly, adds the
  article, and it's waiting in your list.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Run

```bash
./.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

Then open http://127.0.0.1:8765 — paste an article URL, pick a voice, hit **Read**.
Space = play/pause. The speed slider is instant (no re-synthesis).

## Using it with Matter

Two easy ways to get your saved articles in:

1. **Paste the original URL** — Matter saves the source link; paste that and this
   app re-extracts the clean text itself.
2. **Paste text** — open the article in Matter, copy the text, click
   *paste text instead*, and paste.

## Deploy (Render)

This app runs as a normal persistent server, so any host that runs a Python web
service works. A `render.yaml` blueprint and a `Procfile` are included.

**Render (free tier):**

1. Push this repo to GitHub (see below).
2. On [render.com](https://render.com), click **New → Blueprint** and pick the repo.
   Render reads `render.yaml` and provisions the service automatically.
3. Open the assigned `*.onrender.com` URL.

**Railway:** New Project → Deploy from GitHub repo. Railway detects the `Procfile`.

Both bind to the platform's `$PORT`. Note: the free tiers **spin down when idle**,
so the first request after a lull takes ~30–60s to wake up.

## Push to GitHub

```bash
git init && git add -A && git commit -m "Natural Voice Reader"
gh repo create natural-voice-reader --public --source=. --push   # or create on github.com and `git push`
```

## Notes / limits

- Audio is cached in memory and cleared on restart (last ~200 chunks kept).
- edge-tts uses an unofficial free Microsoft endpoint; if a call ever fails with a
  403, upgrade with `./.venv/bin/pip install -U edge-tts` (Microsoft periodically
  changes the auth token scheme).
- Paywalled pages that Matter de-paywalled won't always re-extract from the raw
  URL — use the *paste text* path for those.

# Natural Voice Reader

A free, local read-aloud for saved articles — clean reader view + natural neural
voices + word-by-word highlighting. A DIY replacement for paid read-aloud features.

- **Voices:** Microsoft Edge neural voices via [`edge-tts`](https://github.com/rany2/edge-tts) (free, no API key, needs internet).
- **Article extraction:** paste a URL and [`trafilatura`](https://trafilatura.readthedocs.io/) pulls the clean article text (or paste raw text).
- **Fast start:** the article is synthesized in paragraph-sized chunks. The first (small) chunk plays within seconds; later chunks prefetch while you listen.
- **Highlighting:** edge-tts word-boundary timings are aligned to the text so the current word is highlighted as it's spoken. Click any word to jump there.

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

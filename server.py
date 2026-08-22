"""
Natural Voice Reader — local backend.

Extracts a clean article from a URL (or takes pasted text), then synthesizes it
in paragraph-sized chunks with Microsoft Edge neural voices (free, via edge-tts).
Chunking keeps startup fast: the frontend plays chunk 0 while later chunks
synthesize in the background. edge-tts emits word-boundary timings, which we align
to character ranges so the frontend can highlight words in sync with the audio.
"""

import asyncio
import base64
import datetime
import hashlib
import json
import os
import posixpath
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import anthropic
import ebooklib
import edge_tts
import httpx
import trafilatura
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Natural Voice Reader")
BASE_DIR = Path(__file__).resolve().parent  # so static paths work regardless of CWD

# Supabase (reading-list storage). Set as env vars; endpoints 503 if missing.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")          # service_role (server-only)
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")  # public; used for auth + frontend

# Anthropic (paragraph mood classification for background music).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MOODS = ["neutral", "calm", "tense", "sad", "hopeful", "mysterious", "epic"]

# In-memory audio cache: id -> mp3 bytes. Cleared on restart; capped below.
_AUDIO: dict[str, bytes] = {}
_AUDIO_ORDER: list[str] = []
_MAX_CACHED = 600   # keep more chunks live so /audio URLs go stale less often

MAX_CHUNK_CHARS = 1600  # ~1-2 paragraphs; keeps prefetch comfortably ahead.
FIRST_CHUNK_CHARS = 500  # smaller first chunk => faster time-to-first-audio.

# A curated set of the most natural-sounding English Edge voices.
VOICES = [
    {"id": "en-US-AndrewMultilingualNeural", "label": "Andrew (US, warm male) — recommended"},
    {"id": "en-US-AvaMultilingualNeural", "label": "Ava (US, natural female) — recommended"},
    {"id": "en-US-EmmaMultilingualNeural", "label": "Emma (US, bright female)"},
    {"id": "en-US-BrianMultilingualNeural", "label": "Brian (US, casual male)"},
    {"id": "en-US-JennyNeural", "label": "Jenny (US, female)"},
    {"id": "en-US-GuyNeural", "label": "Guy (US, male)"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia (UK, female)"},
    {"id": "en-GB-RyanNeural", "label": "Ryan (UK, male)"},
    {"id": "en-AU-NatashaNeural", "label": "Natasha (AU, female)"},
]
_VOICE_IDS = {v["id"] for v in VOICES}


class ExtractRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None


class TTSRequest(BaseModel):
    text: str
    voice: str = "en-US-AndrewMultilingualNeural"


class ArticleRequest(BaseModel):
    url: str


class HighlightRequest(BaseModel):
    article_id: Optional[str] = None
    chapter_id: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    start: int
    end: int
    quote: Optional[str] = ""
    note: Optional[str] = ""


class NoteUpdate(BaseModel):
    note: str = ""


class ChapterRequest(BaseModel):
    chapter_id: str


class ListenRequest(BaseModel):
    day: str          # 'YYYY-MM-DD', the client's local date
    seconds: int       # absolute total seconds listened on that day


class MoodsRequest(BaseModel):
    doc_id: Optional[str] = None
    doc_type: str = "article"           # 'article' | 'chapter'
    paragraphs: list[str]


class ArticleUpdate(BaseModel):
    archived: Optional[bool] = None
    queued: Optional[bool] = None
    favorited: Optional[bool] = None


class ListRequest(BaseModel):
    name: str


# --- Rich reading model ---
# Articles and EPUB chapters are parsed from HTML into a structured model that
# preserves formatting + images WITHOUT disturbing the plain-text offset stream
# the TTS/highlight engine depends on. `text` is the flat speakable string (blocks
# joined by "\n\n"); `blocks`/`inlines`/`images` carry character offsets into it.
# This one allowlist walk is also the sanitizer: only known-safe tags/attrs are
# emitted; scripts, event handlers, and unsafe urls never survive.
_HEADING_MAP = {"h1": "h2", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3", "h6": "h3"}
_INLINE_MAP = {"b": "b", "strong": "b", "i": "i", "em": "i", "code": "code", "a": "a"}
_TEXT_BLOCKS = {"p", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
_WS_RE = re.compile(r"\s+")


def _ws(s: str) -> str:
    return _WS_RE.sub(" ", s)


def _safe_href(href: Optional[str]) -> Optional[str]:
    href = (href or "").strip()
    return href if re.match(r"^(https?:|mailto:)", href, re.I) else None


def _safe_src(src: Optional[str]) -> Optional[str]:
    src = (src or "").strip()
    return src if re.match(r"^(https:|data:image/)", src, re.I) else None


def html_to_model(html) -> dict:
    """Parse HTML into {text, blocks, inlines, images} with offsets into `text`."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "head", "nav", "form"]):
        tag.decompose()
    root = soup.body or soup

    parts: list[str] = []
    blocks: list[dict] = []
    inlines: list[dict] = []
    images: list[dict] = []
    pending: list[dict] = []
    pos = 0

    def emit(s: str):
        nonlocal pos
        if not s:
            return
        if parts and parts[-1].endswith(" ") and s.startswith(" "):
            s = s.lstrip(" ")
            if not s:
                return
        parts.append(s)
        pos += len(s)

    def sep():
        nonlocal pos
        if parts and not parts[-1].endswith("\n\n"):
            parts.append("\n\n")
            pos += 2

    def add_image(node):
        src = _safe_src(node.get("src"))
        if src:
            pending.append({"t": "img", "src": src, "alt": (node.get("alt") or "").strip()})

    def flush_images():
        for im in pending:
            blocks.append(im)
            images.append(im)
        pending.clear()

    def walk_inline(node):
        nonlocal pos
        for child in node.children:
            if isinstance(child, NavigableString):
                emit(_ws(str(child)))
            elif child.name == "img":
                add_image(child)
            elif child.name == "br":
                emit(" ")
            elif child.name in _INLINE_MAP:
                k = _INLINE_MAP[child.name]
                s = pos
                href = _safe_href(child.get("href")) if k == "a" else None
                walk_inline(child)
                if pos > s:
                    rng = {"s": s, "e": pos, "k": k}
                    if href:
                        rng["href"] = href
                    inlines.append(rng)
            else:
                walk_inline(child)

    def text_block(node, tag, list_kind=None):
        nonlocal pos
        s = pos
        walk_inline(node)
        if pos > s:
            b = {"t": tag, "s": s, "e": pos}
            if list_kind:
                b["list"] = list_kind
            blocks.append(b)
            sep()
        flush_images()

    def walk(node, list_kind=None):
        nonlocal pos
        for child in node.children:
            if isinstance(child, NavigableString):
                txt = _ws(str(child))
                if txt.strip():
                    s = pos
                    emit(txt)
                    if pos > s:
                        blocks.append({"t": "p", "s": s, "e": pos})
                        sep()
                continue
            name = child.name
            if name == "img":
                add_image(child)
                flush_images()
            elif name == "figure":
                img = child.find("img")
                if img and _safe_src(img.get("src")):
                    im = {"t": "img", "src": _safe_src(img.get("src")),
                          "alt": (img.get("alt") or "").strip()}
                    cap = child.find("figcaption")
                    if cap:
                        im["caption"] = cap.get_text(" ", strip=True)
                    blocks.append(im)
                    images.append(im)
            elif name in ("ul", "ol"):
                walk(child, "ol" if name == "ol" else "ul")
            elif name == "li":
                text_block(child, "li", list_kind or "ul")
            elif name in _TEXT_BLOCKS:
                text_block(child, _HEADING_MAP.get(name, name))
            else:
                walk(child, list_kind)

    walk(root)
    text = "".join(parts).rstrip("\n")
    return {"text": text, "blocks": blocks, "inlines": inlines, "images": images}


def plain_to_model(text: str) -> dict:
    """Model for pasted plain text: paragraphs split on blank lines."""
    text = (text or "").strip()
    blocks, pos = [], 0
    joined = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        s = pos
        joined.append(para)
        pos += len(para)
        blocks.append({"t": "p", "s": s, "e": pos})
        joined.append("\n\n")
        pos += 2
    return {"text": "".join(joined).rstrip("\n"), "blocks": blocks, "inlines": [], "images": []}


def extract_article(url: str) -> tuple[str, str, Optional[str]]:
    """Fetch a URL and return (title, rich_html, cover_image_url)."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise HTTPException(400, f"Could not fetch the page at {url}")
    html = trafilatura.extract(
        downloaded, output_format="html", include_images=True, include_links=True,
        include_formatting=True, include_comments=False, include_tables=False)
    if not html or not html.strip():
        raise HTTPException(422, "Could not extract readable article text from that page.")
    title = url
    cover = None
    meta = trafilatura.extract_metadata(downloaded)
    if meta:
        if meta.title:
            title = meta.title
        cover = _safe_src(getattr(meta, "image", None))
    return title, html, cover


def _epub_image_map(book) -> dict:
    """Map EPUB image hrefs (full path + basename) to data-URIs."""
    out = {}
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            b64 = base64.b64encode(item.get_content()).decode("ascii")
        except Exception:
            continue
        uri = f"data:{item.media_type};base64,{b64}"
        name = item.file_name
        out[name] = uri
        out[posixpath.basename(name)] = uri
    return out


def _epub_cover(book, img_map: dict) -> Optional[str]:
    for getter in (lambda: book.get_item_with_id("cover-image"),
                   lambda: book.get_item_with_id("cover")):
        try:
            it = getter()
            if it and it.get_type() == ebooklib.ITEM_IMAGE:
                return img_map.get(it.file_name) or img_map.get(posixpath.basename(it.file_name))
        except Exception:
            pass
    for name, uri in img_map.items():
        if "cover" in name.lower():
            return uri
    return None


def _chapter_html(raw: bytes, chapter_path: str, img_map: dict) -> tuple[Optional[str], str, int]:
    """Clean one chapter's XHTML: inline images as data-URIs, return
    (heading, html_str, plain_len)."""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    base = posixpath.dirname(chapter_path)
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        resolved = posixpath.normpath(posixpath.join(base, src)) if not src.startswith("data:") else src
        uri = img_map.get(resolved) or img_map.get(posixpath.basename(src)) or (src if src.startswith("data:") else None)
        if uri:
            img["src"] = uri
        else:
            img.decompose()
    heading = soup.find(["h1", "h2", "h3"])
    title = heading.get_text(" ", strip=True) if heading else None
    body = soup.body or soup
    return title, str(body), len(body.get_text(" ", strip=True))


def parse_epub(data: bytes) -> tuple[str, str, Optional[str], list[dict]]:
    """Parse EPUB bytes into (title, author, cover, chapters=[{idx,title,html}]).
    Chapters follow the spine (reading order); tiny front-matter is skipped."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        book = epub.read_epub(tmp.name, options={"ignore_ncx": True})

    def meta(field):
        m = book.get_metadata("DC", field)
        return m[0][0] if m else ""

    title = meta("title") or "Untitled book"
    author = meta("creator") or ""
    img_map = _epub_image_map(book)
    cover = _epub_cover(book, img_map)

    chapters: list[dict] = []
    for idref, _ in book.spine:
        item = book.get_item_with_id(idref)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        heading, html, plain_len = _chapter_html(item.get_content(), item.file_name, img_map)
        if plain_len < 200:          # skip cover / nav / tiny sections
            continue
        chapters.append({
            "idx": len(chapters),
            "title": heading or f"Chapter {len(chapters) + 1}",
            "html": html,
        })
    if not chapters:
        raise HTTPException(422, "Could not extract any readable chapters from that EPUB.")
    return title, author, cover, chapters


async def classify_moods(paragraphs: list[str]) -> list[str]:
    """Label each paragraph with one mood from MOODS, using Claude Haiku."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "Background music not configured (set ANTHROPIC_API_KEY).")
    numbered = "\n".join(f"{i}. {p[:200]}" for i, p in enumerate(paragraphs))
    prompt = (
        "You are scoring the paragraphs of a text so an app can play matching "
        "background music. For EACH numbered paragraph choose the single best mood "
        f"from exactly this list: {', '.join(MOODS)}. Judge the emotional tone; use "
        "'neutral' when there is no strong mood.\n"
        f"Return ONLY a JSON array of {len(paragraphs)} lowercase strings, one per "
        "paragraph in order — no prose, no keys.\n\nParagraphs:\n" + numbered
    )
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    match = re.search(r"\[.*\]", text, re.S)
    try:
        labels = json.loads(match.group(0)) if match else []
    except Exception:
        labels = []
    out = []
    for i in range(len(paragraphs)):
        v = str(labels[i]).strip().lower() if i < len(labels) else "neutral"
        out.append(v if v in MOODS else "neutral")
    return out


def split_units(text: str) -> list[tuple[int, int]]:
    """Split text into sentence-ish units (offsets into `text`), cutting on
    newlines and sentence terminators. Contiguous and covers the whole string."""
    units: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    for i, ch in enumerate(text):
        cut = ch == "\n"
        if not cut and ch in ".!?":
            nxt = text[i + 1] if i + 1 < n else " "
            cut = nxt.isspace()
        if cut:
            units.append((start, i + 1))
            start = i + 1
    if start < n:
        units.append((start, n))
    return units


def make_chunks(text: str, max_len: int = MAX_CHUNK_CHARS) -> list[dict]:
    """Group sentence units into chunks of <= max_len chars, on unit boundaries."""
    chunks: list[list[int]] = []
    cs = ce = None
    for s, e in split_units(text):
        limit = FIRST_CHUNK_CHARS if not chunks else max_len
        if cs is None:
            cs, ce = s, e
        elif e - cs <= limit:
            ce = e
        else:
            chunks.append([cs, ce])
            cs, ce = s, e
    if cs is not None:
        chunks.append([cs, ce])
    return [{"index": i, "start": a, "end": b} for i, (a, b) in enumerate(chunks)]


def align_marks(text: str, words: list[dict]) -> list[dict]:
    """Map ordered spoken words (with audio times) to character ranges in `text`.

    edge-tts emits a WordBoundary per spoken word in order; we walk the source
    tokens with a small lookahead so occasional tokenization differences (quotes,
    hyphens, contractions) don't throw the whole alignment off."""
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    norm = [re.sub(r"\W+", "", text[s:e]).lower() for s, e in tokens]
    marks: list[dict] = []
    ti = 0
    for w in words:
        target = re.sub(r"\W+", "", w["text"]).lower()
        if not target:
            continue
        found = -1
        for j in range(ti, min(ti + 10, len(tokens))):
            nt = norm[j]
            if nt and (nt == target or nt.startswith(target) or target.startswith(nt)):
                found = j
                break
        if found >= 0:
            s, e = tokens[found]
            marks.append({"start": s, "end": e, "time": w["time"]})
            ti = found + 1
    return marks


async def synthesize(text: str, voice: str) -> tuple[bytes, list[dict]]:
    """Synthesize `text` with an Edge neural voice; return (mp3_bytes, marks)."""
    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    audio = bytearray()
    words: list[dict] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            words.append({"text": chunk["text"], "time": chunk["offset"] / 10_000_000})
    if not audio:
        raise HTTPException(502, "The voice service returned no audio. Try again.")
    return bytes(audio), align_marks(text, words)


def _cache_audio(data: bytes) -> str:
    audio_id = uuid.uuid4().hex
    _AUDIO[audio_id] = data
    _AUDIO_ORDER.append(audio_id)
    while len(_AUDIO_ORDER) > _MAX_CACHED:
        _AUDIO.pop(_AUDIO_ORDER.pop(0), None)
    return audio_id


# --- Auth (Supabase Auth / Google) ---
# The frontend signs in with Google via Supabase Auth and sends the resulting
# access token as `Authorization: Bearer <token>`. We validate it against
# Supabase's own auth server (robust across signing schemes) and cache the
# result briefly so the 500ms job-poll loop stays cheap.
_TOKEN_CACHE: dict[str, tuple] = {}   # token -> (user, expiry_epoch)
_TOKEN_TTL = 300


async def verify_token(token: str) -> dict:
    """Validate a Supabase access token; return {id, email}. Cached ~5 min."""
    now = time.time()
    hit = _TOKEN_CACHE.get(token)
    if hit and hit[1] > now:
        return hit[0]
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(503, "Auth is not configured (set SUPABASE_URL/SUPABASE_ANON_KEY).")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(401, "Not signed in.")
    u = resp.json()
    user = {"id": u.get("id"), "email": u.get("email")}
    if not user["id"]:
        raise HTTPException(401, "Not signed in.")
    # Allowlist hook: to restrict access later, reject here unless
    # user["email"] is in an approved set, e.g.:
    #   if user["email"] not in ALLOWED_EMAILS: raise HTTPException(403, "Not invited.")
    _TOKEN_CACHE[token] = (user, now + _TOKEN_TTL)
    return user


async def current_user(authorization: str = Header(None)) -> dict:
    """FastAPI dependency: the signed-in user, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in.")
    return await verify_token(authorization[7:].strip())


@app.get("/api/config")
def config():
    """Public config so the frontend can init its Supabase Auth client."""
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY}


# --- Background jobs ---
# Render fronts the app with Cloudflare, whose edge resets any HTTP/2 request
# where the origin takes longer than ~1s to respond (edge-tts and slow article
# extractions always exceed that). So slow work never blocks a request: we start
# it in the background, return a job id immediately, and the client polls
# /api/job/{id} (each call returns in milliseconds) until the result is ready.
_JOBS: dict[str, dict] = {}
_JOB_ORDER: list[str] = []
_MAX_JOBS = 500


def start_job(work: Callable, uid: str) -> str:
    """Schedule async `work()` in the background; return a job id at once.
    The owning user id is stored so only that user can poll the result."""
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "pending", "user_id": uid}
    _JOB_ORDER.append(job_id)
    while len(_JOB_ORDER) > _MAX_JOBS:
        _JOBS.pop(_JOB_ORDER.pop(0), None)

    async def runner():
        try:
            _JOBS[job_id] = {"status": "done", "result": await work(), "user_id": uid}
        except HTTPException as e:
            _JOBS[job_id] = {"status": "error", "error": e.detail, "user_id": uid}
        except Exception as e:  # report instead of crashing the worker
            _JOBS[job_id] = {"status": "error", "error": str(e), "user_id": uid}

    asyncio.create_task(runner())
    return job_id


@app.get("/api/job/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    job = _JOBS.get(job_id)
    if job is None or job.get("user_id") != user["id"]:   # 404, don't leak others' jobs
        raise HTTPException(404, "Job expired. Try again.")
    return {k: v for k, v in job.items() if k != "user_id"}


@app.get("/api/voices")
def get_voices():
    return VOICES


@app.post("/api/extract")
async def extract(req: ExtractRequest, user: dict = Depends(current_user)):
    if not req.url and not (req.text and req.text.strip()):
        raise HTTPException(400, "Provide either a url or text.")

    async def work():
        if req.url:
            title, html, cover = await asyncio.to_thread(extract_article, req.url.strip())
            model = html_to_model(html)
        else:
            title, cover, model = "Pasted text", None, plain_to_model(req.text.strip())
        text = model["text"]
        return {"title": title, "cover": cover, "text": text, "chunks": make_chunks(text),
                "blocks": model["blocks"], "inlines": model["inlines"], "images": model["images"]}

    return {"job": start_job(work, user["id"])}


@app.post("/api/tts")
async def tts(req: TTSRequest, user: dict = Depends(current_user)):
    if not req.text.strip():
        raise HTTPException(400, "Empty text.")
    voice = req.voice if req.voice in _VOICE_IDS else VOICES[0]["id"]

    async def work():
        audio, marks = await synthesize(req.text, voice)
        return {"audio_url": f"/audio/{_cache_audio(audio)}.mp3", "marks": marks}

    return {"job": start_job(work, user["id"])}


@app.get("/audio/{audio_id}.mp3")
def get_audio(audio_id: str):
    data = _AUDIO.get(audio_id)
    if data is None:
        raise HTTPException(404, "Audio expired. Reload the article.")
    return Response(content=data, media_type="audio/mpeg")


# --- Reading list (Supabase-backed) ---
async def _supabase(method: str, path: str, **kwargs) -> httpx.Response:
    """Issue a PostgREST request to the Supabase `articles` table."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Reading list storage is not configured (set SUPABASE_URL/SUPABASE_KEY).")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    headers.update(kwargs.pop("headers", {}))
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(method, f"{SUPABASE_URL}/rest/v1{path}", headers=headers, **kwargs)
    if resp.status_code >= 400:
        raise HTTPException(502, f"Reading list storage error: {resp.text}")
    return resp


@app.get("/api/articles")
async def list_articles(user: dict = Depends(current_user)):
    resp = await _supabase(
        "GET", f"/articles?select=*&user_id=eq.{user['id']}&order=added_at.desc")
    return resp.json()


@app.post("/api/articles")
async def add_article(req: ArticleRequest, user: dict = Depends(current_user)):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Provide a url.")

    async def work():
        title, html, cover = await asyncio.to_thread(extract_article, url)
        text = html_to_model(html)["text"]
        row = {
            "user_id": user["id"],
            "url": url,
            "title": title,
            "domain": urlparse(url).hostname or "",
            "cover": cover,
            # ~180 wpm (measured for Andrew) at 1x => 225 wpm at the default 1.25x.
            "est_minutes": max(1, round(len(text.split()) / 225)),
        }
        resp = await _supabase(
            "POST", "/articles", json=row, headers={"Prefer": "return=representation"}
        )
        created = resp.json()
        return created[0] if isinstance(created, list) else created

    return {"job": start_job(work, user["id"])}


@app.delete("/api/articles/{article_id}")
async def delete_article(article_id: str, user: dict = Depends(current_user)):
    await _supabase("DELETE", f"/articles?id=eq.{article_id}&user_id=eq.{user['id']}")
    return {"ok": True}


@app.patch("/api/articles/{article_id}")
async def update_article(article_id: str, req: ArticleUpdate,
                         user: dict = Depends(current_user)):
    """Toggle the soft-hide (archive), reading-queue, and favorite flags on one article."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    patch: dict = {}
    if req.archived is not None:
        patch["archived_at"] = now if req.archived else None
    if req.queued is not None:
        patch["queued_at"] = now if req.queued else None
    if req.favorited is not None:
        patch["favorited_at"] = now if req.favorited else None
    if not patch:
        raise HTTPException(400, "Nothing to update.")
    await _supabase("PATCH", f"/articles?id=eq.{article_id}&user_id=eq.{user['id']}",
                    json=patch)
    return {"ok": True, **patch}


# --- Books (EPUB) ---
@app.get("/api/books")
async def list_books(user: dict = Depends(current_user)):
    resp = await _supabase(
        "GET", f"/books?select=*&user_id=eq.{user['id']}&order=added_at.desc")
    return resp.json()


@app.post("/api/books")
async def add_book(file: UploadFile, user: dict = Depends(current_user)):
    data = await file.read()
    uid = user["id"]

    async def work():
        title, author, cover, chapters = await asyncio.to_thread(parse_epub, data)
        resp = await _supabase(
            "POST", "/books",
            json={"user_id": uid, "title": title, "author": author,
                  "cover": cover, "chapter_count": len(chapters)},
            headers={"Prefer": "return=representation"},
        )
        book = resp.json()[0]
        # store plain text too (matches the reader's offsets) so search is fast
        rows = [{"user_id": uid, "book_id": book["id"], "idx": c["idx"],
                 "title": c["title"], "html": c["html"],
                 "text": html_to_model(c["html"])["text"]} for c in chapters]
        # insert chapters in batches to keep each request small
        for i in range(0, len(rows), 20):
            await _supabase("POST", "/book_chapters", json=rows[i:i + 20])
        return book

    return {"job": start_job(work, uid)}


@app.get("/api/books/{book_id}/chapters")
async def list_chapters(book_id: str, user: dict = Depends(current_user)):
    resp = await _supabase(
        "GET", f"/book_chapters?book_id=eq.{book_id}&user_id=eq.{user['id']}"
               "&select=id,idx,title&order=idx.asc")
    return resp.json()


@app.post("/api/chapter")
async def get_chapter(req: ChapterRequest, user: dict = Depends(current_user)):
    uid = user["id"]

    async def work():
        resp = await _supabase(
            "GET", f"/book_chapters?id=eq.{req.chapter_id}&user_id=eq.{uid}&select=title,html,text")
        rows = resp.json()
        if not rows:
            raise HTTPException(404, "Chapter not found.")
        row = rows[0]
        model = html_to_model(row["html"]) if row.get("html") else plain_to_model(row.get("text") or "")
        text = model["text"]
        return {"title": row.get("title") or "", "text": text, "chunks": make_chunks(text),
                "blocks": model["blocks"], "inlines": model["inlines"], "images": model["images"]}

    return {"job": start_job(work, uid)}


@app.get("/api/books/{book_id}/search")
async def search_book(book_id: str, q: str = "", user: dict = Depends(current_user)):
    """Find a phrase inside a book; each hit's `offset` maps to the reader's TEXT."""
    query = (q or "").strip()
    if len(query) < 2:
        return []
    uid = user["id"]
    resp = await _supabase(
        "GET", f"/book_chapters?book_id=eq.{book_id}&user_id=eq.{uid}"
               "&select=id,idx,title,text,html&order=idx.asc")
    rows = resp.json()
    ql = query.lower()
    results = []
    for row in rows:
        text = row.get("text") or (html_to_model(row["html"])["text"] if row.get("html") else "")
        low = text.lower()
        start, hits = 0, 0
        while hits < 3:                          # cap matches per chapter
            k = low.find(ql, start)
            if k < 0:
                break
            a, b = max(0, k - 40), min(len(text), k + len(query) + 40)
            snippet = (("…" if a > 0 else "") + text[a:b].replace("\n", " ").strip()
                       + ("…" if b < len(text) else ""))
            results.append({"chapter_id": row["id"], "idx": row["idx"],
                            "title": row.get("title") or f"Chapter {row['idx'] + 1}",
                            "offset": k, "snippet": snippet})
            start, hits = k + len(query), hits + 1
        if len(results) >= 40:
            break
    return results[:40]


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: str, user: dict = Depends(current_user)):
    await _supabase("DELETE", f"/books?id=eq.{book_id}&user_id=eq.{user['id']}")
    return {"ok": True}


# --- Paragraph moods (for background music) ---
@app.post("/api/moods")
async def get_moods(req: MoodsRequest, user: dict = Depends(current_user)):
    paras = req.paragraphs or []
    uid = user["id"]
    table = "book_chapters" if req.doc_type == "chapter" else "articles"
    scope = f"id=eq.{req.doc_id}&user_id=eq.{uid}"
    digest = hashlib.sha256("\n".join(p[:200] for p in paras).encode()).hexdigest()

    async def work():
        if not paras:
            return {"labels": []}
        configured = bool(req.doc_id and SUPABASE_URL and SUPABASE_KEY)
        if configured:                                   # cache hit?
            try:
                rows = (await _supabase("GET", f"/{table}?{scope}&select=moods")).json()
                cached = rows[0].get("moods") if rows else None
                if cached and cached.get("hash") == digest \
                        and len(cached.get("labels", [])) == len(paras):
                    return {"labels": cached["labels"]}
            except Exception:
                pass
        labels = await classify_moods(paras)
        if configured:                                   # save for next time
            try:
                await _supabase("PATCH", f"/{table}?{scope}",
                                json={"moods": {"hash": digest, "labels": labels}})
            except Exception:
                pass
        return {"labels": labels}

    return {"job": start_job(work, uid)}


# --- Highlights & notes (Supabase-backed) ---
# start_char/end_char are the columns; we expose them to the client as start/end.
# A highlight belongs to either an article (article_id) or a book chapter (chapter_id).
_HL_SELECT = ("id,article_id,chapter_id,url,title,"
              "start:start_char,end:end_char,quote,note,created_at")


def _alias_hl(row: dict) -> dict:
    if "start_char" in row:
        row["start"] = row.pop("start_char")
    if "end_char" in row:
        row["end"] = row.pop("end_char")
    return row


@app.get("/api/highlights")
async def list_highlights(article_id: Optional[str] = None, chapter_id: Optional[str] = None,
                          user: dict = Depends(current_user)):
    q = f"/highlights?select={_HL_SELECT}&user_id=eq.{user['id']}&order=created_at.asc"
    if article_id:
        q += f"&article_id=eq.{article_id}"
    if chapter_id:
        q += f"&chapter_id=eq.{chapter_id}"
    resp = await _supabase("GET", q)
    return resp.json()


@app.post("/api/highlights")
async def add_highlight(req: HighlightRequest, user: dict = Depends(current_user)):
    row = {
        "user_id": user["id"],
        "article_id": req.article_id,
        "chapter_id": req.chapter_id,
        "url": req.url,
        "title": req.title,
        "start_char": req.start,
        "end_char": req.end,
        "quote": req.quote,
        "note": req.note,
    }
    resp = await _supabase(
        "POST", "/highlights", json=row, headers={"Prefer": "return=representation"}
    )
    created = resp.json()
    return _alias_hl(created[0] if isinstance(created, list) else created)


@app.patch("/api/highlights/{highlight_id}")
async def update_highlight(highlight_id: str, req: NoteUpdate,
                           user: dict = Depends(current_user)):
    await _supabase("PATCH", f"/highlights?id=eq.{highlight_id}&user_id=eq.{user['id']}",
                    json={"note": req.note})
    return {"ok": True}


@app.delete("/api/highlights/{highlight_id}")
async def delete_highlight(highlight_id: str, user: dict = Depends(current_user)):
    await _supabase("DELETE", f"/highlights?id=eq.{highlight_id}&user_id=eq.{user['id']}")
    return {"ok": True}


# --- Listening-time stats (Supabase-backed) ---
# The client is the authoritative tally for "today" and sends the absolute total
# each flush; the server just upserts it (Prefer: merge-duplicates on the
# (user_id, day) primary key), so no atomic-increment logic is needed here.
@app.post("/api/stats/listen")
async def post_listen(req: ListenRequest, user: dict = Depends(current_user)):
    await _supabase(
        "POST", "/listening_daily",
        json={"user_id": user["id"], "day": req.day, "seconds": max(0, req.seconds)},
        headers={"Prefer": "resolution=merge-duplicates"},
    )
    return {"ok": True}


@app.get("/api/stats/listening")
async def get_listening(days: int = 28, user: dict = Depends(current_user)):
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    resp = await _supabase(
        "GET", f"/listening_daily?user_id=eq.{user['id']}&day=gte.{cutoff}"
               "&select=day,seconds&order=day.asc")
    return resp.json()


# --- Lists (Supabase-backed) ---
# Queue is NOT a row here: it stays the `articles.queued_at` flag and the client
# pins it as a virtual list. These tables back the user's own custom lists only.
# list_items carries its own user_id (like book_chapters) so every query can be
# scoped directly; deleting a list or an article cascades its memberships away.
@app.get("/api/lists")
async def list_lists(user: dict = Depends(current_user)):
    resp = await _supabase(
        "GET", f"/lists?select=id,name,created_at&user_id=eq.{user['id']}&order=created_at.asc")
    return resp.json()


@app.post("/api/lists")
async def add_list(req: ListRequest, user: dict = Depends(current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name the list.")
    resp = await _supabase(
        "POST", "/lists", json={"user_id": user["id"], "name": name},
        headers={"Prefer": "return=representation"},
    )
    created = resp.json()
    return created[0] if isinstance(created, list) else created


@app.patch("/api/lists/{list_id}")
async def rename_list(list_id: str, req: ListRequest, user: dict = Depends(current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name the list.")
    await _supabase("PATCH", f"/lists?id=eq.{list_id}&user_id=eq.{user['id']}",
                    json={"name": name})
    return {"ok": True, "name": name}


@app.delete("/api/lists/{list_id}")
async def delete_list(list_id: str, user: dict = Depends(current_user)):
    """Memberships go with it — list_items.list_id is ON DELETE CASCADE."""
    await _supabase("DELETE", f"/lists?id=eq.{list_id}&user_id=eq.{user['id']}")
    return {"ok": True}


@app.get("/api/list-items")
async def list_list_items(user: dict = Depends(current_user)):
    """Every membership row for the user; the client joins it against its library."""
    resp = await _supabase(
        "GET", f"/list_items?select=list_id,article_id,added_at&user_id=eq.{user['id']}"
               "&order=added_at.desc")
    return resp.json()


@app.post("/api/lists/{list_id}/items/{article_id}")
async def add_list_item(list_id: str, article_id: str, user: dict = Depends(current_user)):
    await _supabase(
        "POST", "/list_items",
        json={"user_id": user["id"], "list_id": list_id, "article_id": article_id},
        headers={"Prefer": "resolution=ignore-duplicates"},
    )
    return {"ok": True}


@app.delete("/api/lists/{list_id}/items/{article_id}")
async def delete_list_item(list_id: str, article_id: str,
                           user: dict = Depends(current_user)):
    await _supabase("DELETE", f"/list_items?list_id=eq.{list_id}"
                              f"&article_id=eq.{article_id}&user_id=eq.{user['id']}")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

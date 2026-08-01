"""
Natural Voice Reader — local backend.

Extracts a clean article from a URL (or takes pasted text), then synthesizes it
in paragraph-sized chunks with Microsoft Edge neural voices (free, via edge-tts).
Chunking keeps startup fast: the frontend plays chunk 0 while later chunks
synthesize in the background. edge-tts emits word-boundary timings, which we align
to character ranges so the frontend can highlight words in sync with the audio.
"""

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import edge_tts
import httpx
import trafilatura
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Natural Voice Reader")
BASE_DIR = Path(__file__).resolve().parent  # so static paths work regardless of CWD

# Supabase (reading-list storage). Set as env vars; endpoints 503 if missing.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# In-memory audio cache: id -> mp3 bytes. Cleared on restart; capped below.
_AUDIO: dict[str, bytes] = {}
_AUDIO_ORDER: list[str] = []
_MAX_CACHED = 200

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
    url: Optional[str] = None
    title: Optional[str] = None
    start: int
    end: int
    quote: Optional[str] = ""
    note: Optional[str] = ""


class NoteUpdate(BaseModel):
    note: str = ""


def extract_article(url: str) -> tuple[str, str]:
    """Fetch a URL and return (title, clean_text)."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise HTTPException(400, f"Could not fetch the page at {url}")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    if not text or not text.strip():
        raise HTTPException(422, "Could not extract readable article text from that page.")
    title = url
    meta = trafilatura.extract_metadata(downloaded)
    if meta and meta.title:
        title = meta.title
    return title, text.strip()


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


# --- Background jobs ---
# Render fronts the app with Cloudflare, whose edge resets any HTTP/2 request
# where the origin takes longer than ~1s to respond (edge-tts and slow article
# extractions always exceed that). So slow work never blocks a request: we start
# it in the background, return a job id immediately, and the client polls
# /api/job/{id} (each call returns in milliseconds) until the result is ready.
_JOBS: dict[str, dict] = {}
_JOB_ORDER: list[str] = []
_MAX_JOBS = 500


def start_job(work: Callable) -> str:
    """Schedule async `work()` in the background; return a job id at once."""
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "pending"}
    _JOB_ORDER.append(job_id)
    while len(_JOB_ORDER) > _MAX_JOBS:
        _JOBS.pop(_JOB_ORDER.pop(0), None)

    async def runner():
        try:
            _JOBS[job_id] = {"status": "done", "result": await work()}
        except HTTPException as e:
            _JOBS[job_id] = {"status": "error", "error": e.detail}
        except Exception as e:  # report instead of crashing the worker
            _JOBS[job_id] = {"status": "error", "error": str(e)}

    asyncio.create_task(runner())
    return job_id


@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Job expired. Try again.")
    return job


@app.get("/api/voices")
def get_voices():
    return VOICES


@app.post("/api/extract")
async def extract(req: ExtractRequest):
    if not req.url and not (req.text and req.text.strip()):
        raise HTTPException(400, "Provide either a url or text.")

    async def work():
        if req.url:
            title, text = await asyncio.to_thread(extract_article, req.url.strip())
        else:
            title, text = "Pasted text", req.text.strip()
        return {"title": title, "text": text, "chunks": make_chunks(text)}

    return {"job": start_job(work)}


@app.post("/api/tts")
async def tts(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(400, "Empty text.")
    voice = req.voice if req.voice in _VOICE_IDS else VOICES[0]["id"]

    async def work():
        audio, marks = await synthesize(req.text, voice)
        return {"audio_url": f"/audio/{_cache_audio(audio)}.mp3", "marks": marks}

    return {"job": start_job(work)}


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
async def list_articles():
    resp = await _supabase("GET", "/articles?select=*&order=added_at.desc")
    return resp.json()


@app.post("/api/articles")
async def add_article(req: ArticleRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Provide a url.")

    async def work():
        title, text = await asyncio.to_thread(extract_article, url)
        row = {
            "url": url,
            "title": title,
            "domain": urlparse(url).hostname or "",
            # ~180 wpm (measured for Andrew) at 1x => 225 wpm at the default 1.25x.
            "est_minutes": max(1, round(len(text.split()) / 225)),
        }
        resp = await _supabase(
            "POST", "/articles", json=row, headers={"Prefer": "return=representation"}
        )
        created = resp.json()
        return created[0] if isinstance(created, list) else created

    return {"job": start_job(work)}


@app.delete("/api/articles/{article_id}")
async def delete_article(article_id: str):
    await _supabase("DELETE", f"/articles?id=eq.{article_id}")
    return {"ok": True}


# --- Highlights & notes (Supabase-backed) ---
# start_char/end_char are the columns; we expose them to the client as start/end.
_HL_SELECT = "id,article_id,url,title,start:start_char,end:end_char,quote,note,created_at"


def _alias_hl(row: dict) -> dict:
    if "start_char" in row:
        row["start"] = row.pop("start_char")
    if "end_char" in row:
        row["end"] = row.pop("end_char")
    return row


@app.get("/api/highlights")
async def list_highlights(article_id: Optional[str] = None):
    q = f"/highlights?select={_HL_SELECT}&order=created_at.asc"
    if article_id:
        q += f"&article_id=eq.{article_id}"
    resp = await _supabase("GET", q)
    return resp.json()


@app.post("/api/highlights")
async def add_highlight(req: HighlightRequest):
    row = {
        "article_id": req.article_id,
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
async def update_highlight(highlight_id: str, req: NoteUpdate):
    await _supabase("PATCH", f"/highlights?id=eq.{highlight_id}", json={"note": req.note})
    return {"ok": True}


@app.delete("/api/highlights/{highlight_id}")
async def delete_highlight(highlight_id: str):
    await _supabase("DELETE", f"/highlights?id=eq.{highlight_id}")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

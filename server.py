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
from typing import Optional
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


@app.get("/api/voices")
def get_voices():
    return VOICES


@app.post("/api/extract")
async def extract(req: ExtractRequest):
    if req.url:
        title, text = await asyncio.to_thread(extract_article, req.url.strip())
    elif req.text and req.text.strip():
        title, text = "Pasted text", req.text.strip()
    else:
        raise HTTPException(400, "Provide either a url or text.")
    return {"title": title, "text": text, "chunks": make_chunks(text)}


@app.post("/api/tts")
async def tts(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(400, "Empty text.")
    voice = req.voice if req.voice in _VOICE_IDS else VOICES[0]["id"]
    audio, marks = await synthesize(req.text, voice)
    return {"audio_url": f"/audio/{_cache_audio(audio)}.mp3", "marks": marks}


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
    title, text = await asyncio.to_thread(extract_article, url)
    row = {
        "url": url,
        "title": title,
        "domain": urlparse(url).hostname or "",
        "est_minutes": max(1, round(len(text.split()) / 200)),
    }
    resp = await _supabase(
        "POST", "/articles", json=row, headers={"Prefer": "return=representation"}
    )
    created = resp.json()
    return created[0] if isinstance(created, list) else created


@app.delete("/api/articles/{article_id}")
async def delete_article(article_id: str):
    await _supabase("DELETE", f"/articles?id=eq.{article_id}")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

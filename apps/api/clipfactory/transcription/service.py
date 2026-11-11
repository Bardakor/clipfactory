"""Chunked, checkpointed transcription with vocabulary correction and persistence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.config import Settings
from clipfactory.domain import Segment, TranscriptResult, Word
from clipfactory.models import Source, Transcript, TranscriptSegment
from clipfactory.settings_store import RuntimeSettings
from clipfactory.transcription.providers import SAMPLE_RATE, Transcriber

_EDGE = " \t\n.,!?;:\"'()[]…«»"
_BOUNDARY_SEARCH_MS = 15_000


def _norm(token: str) -> str:
    return token.strip(_EDGE).lower()


def correct_words(words: list[Word], corrections: dict[str, str]) -> list[Word]:
    """Apply the correction dictionary to timed words, merging multi-word matches
    into one word that spans their combined (real) timing."""
    table = {" ".join(_norm(p) for p in key.split()): value for key, value in corrections.items()}
    longest = max((len(k.split()) for k in table), default=1)
    out: list[Word] = []
    i = 0
    while i < len(words):
        for n in range(min(longest, len(words) - i), 0, -1):
            window = words[i : i + n]
            key = " ".join(_norm(w.w) for w in window)
            if key in table:
                first, last = window[0].w, window[-1].w
                lead = first[: len(first) - len(first.lstrip())]
                trail = last[len(last.rstrip(_EDGE)) :] if last.rstrip(_EDGE) else ""
                probs = [w.p for w in window if w.p is not None]
                out.append(Word(w=f"{lead}{table[key]}{trail}", s=window[0].s, e=window[-1].e, p=min(probs) if probs else None))
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def correct_text(text: str, corrections: dict[str, str]) -> str:
    for key, value in sorted(corrections.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<!\w){re.escape(key)}(?!\w)", value, text, flags=re.IGNORECASE)
    return text


def apply_corrections(segment: Segment, corrections: dict[str, str]) -> Segment:
    if not corrections:
        return segment
    if segment.words:
        words = correct_words(segment.words, corrections)
        return segment.model_copy(update={"words": words, "text": "".join(w.w for w in words).strip()})
    return segment.model_copy(update={"text": correct_text(segment.text, corrections)})


def plan_chunks(wav: Path, chunk_ms: int) -> list[tuple[int, int]]:
    """Split the audio into chunks whose boundaries sit on the quietest nearby moment,
    so no word is cut in half between two Whisper windows."""
    info = sf.info(str(wav))
    total_ms = int(info.frames * 1000 / info.samplerate)
    bounds = [0]
    while total_ms - bounds[-1] > chunk_ms * 1.3:
        target = bounds[-1] + chunk_ms
        lo = max(bounds[-1] + chunk_ms // 2, target - _BOUNDARY_SEARCH_MS)
        hi = min(total_ms, target + _BOUNDARY_SEARCH_MS)
        audio, _ = sf.read(str(wav), start=lo * SAMPLE_RATE // 1000, frames=(hi - lo) * SAMPLE_RATE // 1000, dtype="float32")
        hop = SAMPLE_RATE // 5  # 200 ms windows
        n = len(audio) // hop
        if n == 0:
            bounds.append(target)
            continue
        energy = np.square(audio[: n * hop]).reshape(n, hop).mean(axis=1)
        bounds.append(lo + int(np.argmin(energy)) * 200 + 100)
    bounds.append(total_ms)
    return list(zip(bounds[:-1], bounds[1:], strict=True))


def transcript_cache_key(source: Source, transcriber: Transcriber, language: str, runtime: RuntimeSettings) -> str:
    payload = json.dumps(
        [source.fingerprint, transcriber.name, transcriber.model, language, sorted(runtime.vocabulary),
         sorted(runtime.corrections.items())],  # fmt: skip
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def find_transcript(session: Session, source_id: str, cache_key: str) -> Transcript | None:
    return session.scalar(
        select(Transcript).where(Transcript.source_id == source_id, Transcript.cache_key == cache_key)
    )


def latest_transcript(session: Session, source_id: str) -> Transcript | None:
    return session.scalar(
        select(Transcript).where(Transcript.source_id == source_id).order_by(Transcript.created_at.desc())
    )


def run_transcription(
    settings: Settings,
    source: Source,
    wav: Path,
    transcriber: Transcriber,
    runtime: RuntimeSettings,
    *,
    language: str,
    cache_key: str,
    on_progress: Callable[[float, str], None],
    should_stop: Callable[[], None],
) -> TranscriptResult:
    """Transcribe chunk by chunk. Each finished chunk is checkpointed to disk, so an
    interrupted job resumes at the first missing chunk instead of starting over."""
    chunks = plan_chunks(wav, transcriber.chunk_ms)
    checkpoint_dir = settings.path("transcripts", source.id, cache_key[:16])
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    prompt = ", ".join(runtime.vocabulary)[:800]
    lang = None if language == "auto" else language

    segments: list[Segment] = []
    languages: dict[str, int] = {}
    word_level = True
    cost = 0.0
    for i, (start_ms, end_ms) in enumerate(chunks):
        should_stop()
        on_progress(i / len(chunks), f"segment audio {i + 1}/{len(chunks)}")
        checkpoint = checkpoint_dir / f"chunk_{start_ms}_{end_ms}.json"
        if checkpoint.exists():
            data = json.loads(checkpoint.read_text())
            chunk_segments = [Segment.model_validate(s) for s in data["segments"]]
            chunk_lang, chunk_words = data["language"], data["word_level"]
        else:
            result = transcriber.transcribe_chunk(wav, start_ms, end_ms, lang, prompt)
            chunk_segments = [apply_corrections(s, runtime.corrections) for s in result.segments]
            chunk_lang, chunk_words = result.language, result.word_level
            cost += result.cost_usd or 0.0
            tmp = checkpoint.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "language": chunk_lang, "word_level": chunk_words,
                "segments": [s.model_dump() for s in chunk_segments],
            }, ensure_ascii=False))  # fmt: skip
            tmp.replace(checkpoint)
        segments.extend(chunk_segments)
        word_level = word_level and (chunk_words or not chunk_segments)
        if chunk_lang:
            languages[chunk_lang] = languages.get(chunk_lang, 0) + len(chunk_segments)
    on_progress(1.0, f"{len(segments)} segments")
    segments.sort(key=lambda s: s.start_ms)
    return TranscriptResult(
        provider=transcriber.name,
        model=transcriber.model,
        language=max(languages, key=languages.get) if languages else lang,  # type: ignore[arg-type]
        precision="word" if word_level and segments else "segment",
        segments=segments,
        is_test=transcriber.is_test,
        cost_usd=cost or None,
    )


def store_transcript(session: Session, settings: Settings, source: Source, result: TranscriptResult, cache_key: str) -> Transcript:
    transcript = Transcript(
        source_id=source.id, cache_key=cache_key, provider=result.provider, model=result.model,
        language=result.language, precision=result.precision, is_test=result.is_test,
    )  # fmt: skip
    session.add(transcript)
    session.flush()
    session.add_all(
        TranscriptSegment(
            transcript_id=transcript.id, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text, language=s.language,
            speaker_id=s.speaker_id, confidence=s.confidence,
            words=[w.model_dump() for w in s.words] if s.words else None, provenance=s.provenance,
        )  # fmt: skip
        for s in result.segments
    )
    export_transcript_json(session, settings, transcript)
    return transcript


def segments_of(transcript: Transcript) -> list[Segment]:
    return [
        Segment(
            start_ms=s.start_ms, end_ms=s.end_ms, text=s.text, language=s.language, speaker_id=s.speaker_id,
            confidence=s.confidence, words=[Word.model_validate(w) for w in s.words] if s.words else None,
            provenance=s.provenance,
        )  # fmt: skip
        for s in transcript.segments
    ]


def export_transcript_json(session: Session, settings: Settings, transcript: Transcript) -> Path:
    session.flush()
    session.refresh(transcript)
    path = settings.path("transcripts", f"{transcript.source_id}.json")
    path.write_text(json.dumps({
        "source_id": transcript.source_id, "provider": transcript.provider, "model": transcript.model,
        "language": transcript.language, "precision": transcript.precision, "edited": transcript.edited,
        "is_test": transcript.is_test, "segments": [s.model_dump() for s in segments_of(transcript)],
    }, ensure_ascii=False, indent=1))  # fmt: skip
    return path

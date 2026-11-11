"""Transcription providers behind one interface: local MLX Whisper, OpenRouter STT, test fake."""

from __future__ import annotations

import base64
import math
import threading
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np
import soundfile as sf

from clipfactory.config import Settings
from clipfactory.domain import Segment, Word
from clipfactory.errors import AINotConfigured, TranscriptionError
from clipfactory.media import ffmpeg
from clipfactory.settings_store import RuntimeSettings
from clipfactory.timeutil import s_to_ms

SAMPLE_RATE = 16000


class ChunkResult:
    def __init__(self, segments: list[Segment], language: str | None, word_level: bool, cost_usd: float | None = None):
        self.segments = segments
        self.language = language
        self.word_level = word_level
        self.cost_usd = cost_usd


class Transcriber(Protocol):
    name: str
    model: str
    chunk_ms: int
    is_test: bool

    def transcribe_chunk(self, wav: Path, start_ms: int, end_ms: int, language: str | None, prompt: str) -> ChunkResult: ...


def read_chunk(wav: Path, start_ms: int, end_ms: int) -> np.ndarray:
    start = start_ms * SAMPLE_RATE // 1000
    frames = (end_ms - start_ms) * SAMPLE_RATE // 1000
    audio, sr = sf.read(str(wav), start=start, frames=frames, dtype="float32", always_2d=False)
    if sr != SAMPLE_RATE:
        raise TranscriptionError("analysis audio must be 16 kHz", sample_rate=sr)
    return audio


class MlxWhisperTranscriber:
    """Whisper on Apple Silicon via MLX. Word timings come from Whisper's own
    cross-attention alignment (DTW) — real measurements, not interpolation."""

    name = "mlx"
    chunk_ms = 10 * 60_000
    is_test = False
    _lock = threading.Lock()  # one model in memory, one inference at a time

    def __init__(self, model: str) -> None:
        self.model = model

    def transcribe_chunk(self, wav: Path, start_ms: int, end_ms: int, language: str | None, prompt: str) -> ChunkResult:
        try:
            import mlx_whisper
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise TranscriptionError("mlx-whisper is not installed (Apple Silicon only)") from exc
        audio = read_chunk(wav, start_ms, end_ms)
        if audio.size < SAMPLE_RATE // 2:
            return ChunkResult([], language, True)
        with self._lock:
            try:
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=self.model,
                    language=language,
                    word_timestamps=True,
                    initial_prompt=prompt or None,
                    condition_on_previous_text=False,  # stops hallucinated loops from spreading
                    hallucination_silence_threshold=2.0,
                    verbose=None,
                )
            except Exception as exc:  # noqa: BLE001 - MLX raises a variety of runtime errors
                raise TranscriptionError(f"MLX Whisper failed: {exc}") from exc
        detected = result.get("language")
        provenance = f"mlx-whisper:{self.model.split('/')[-1]}"
        segments: list[Segment] = []
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            # Whisper's own "this is probably not speech" signal: drop instead of inventing words.
            if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -1.0:
                continue
            words = [
                Word(w=w["word"], s=start_ms + s_to_ms(w["start"]), e=start_ms + s_to_ms(w["end"]),
                     p=round(float(w.get("probability", 0)), 3))  # fmt: skip
                for w in seg.get("words", [])
                if w.get("word", "").strip()
            ]
            segments.append(
                Segment(
                    start_ms=start_ms + s_to_ms(seg["start"]),
                    end_ms=start_ms + s_to_ms(seg["end"]),
                    text=text,
                    language=detected,
                    confidence=round(math.exp(min(0.0, float(seg.get("avg_logprob", -1.0)))), 3),
                    words=words or None,
                    provenance=provenance,
                )
            )
        return ChunkResult(segments, detected, all(s.words for s in segments))


class OpenRouterTranscriber:
    """OpenRouter's dedicated speech-to-text endpoint (JSON body, base64 audio).

    Asks for ``verbose_json`` with word timestamps; models that reject it get a
    plain request on shorter chunks, and the result is honestly marked as
    segment-precision.
    """

    name = "openrouter"
    is_test = False

    def __init__(self, settings: Settings, model: str, *, timeout_s: int = 120, client: httpx.Client | None = None) -> None:
        if settings.openrouter_api_key is None:
            raise AINotConfigured("OPENROUTER_API_KEY is required for OpenRouter transcription")
        self.model = model
        self.chunk_ms = 5 * 60_000
        self._verbose = True
        self._client = client or httpx.Client(
            base_url=settings.openrouter_base_url,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}"},
            timeout=timeout_s,
        )
        self._tmp = settings.path("cache", "stt")
        self._tmp.mkdir(parents=True, exist_ok=True)

    def _post(self, mp3: Path, language: str | None, verbose: bool) -> httpx.Response:
        body: dict = {
            "model": self.model,
            "input_audio": {"data": base64.b64encode(mp3.read_bytes()).decode(), "format": "mp3"},
        }
        if language:
            body["language"] = language
        if verbose:
            body["response_format"] = "verbose_json"
            body["timestamp_granularities"] = ["segment", "word"]
        return self._client.post("/audio/transcriptions", json=body)

    def transcribe_chunk(self, wav: Path, start_ms: int, end_ms: int, language: str | None, prompt: str) -> ChunkResult:
        mp3 = ffmpeg.encode_audio_chunk(wav, self._tmp / f"{wav.stem}_{start_ms}_{end_ms}.mp3", start_ms, end_ms)
        try:
            resp = self._post(mp3, language, self._verbose)
            if resp.status_code == 400 and self._verbose:
                # This model/provider has no structured output: fall back for the rest of the run.
                self._verbose = False
                self.chunk_ms = 30_000
                resp = self._post(mp3, language, False)
            if resp.status_code >= 400:
                raise TranscriptionError(
                    f"OpenRouter transcription failed ({resp.status_code})", body=resp.text[:300], model=self.model
                )
            data = resp.json()
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"OpenRouter transcription request failed: {exc}") from exc
        finally:
            mp3.unlink(missing_ok=True)

        provenance = f"openrouter:{self.model}"
        detected = data.get("language") or language
        cost = (data.get("usage") or {}).get("cost")
        words_by_time = [
            Word(w=" " + str(w.get("word", "")).strip(), s=start_ms + s_to_ms(w["start"]),
                 e=start_ms + s_to_ms(w["end"]), p=w.get("confidence"))  # fmt: skip
            for w in data.get("words") or []
            if str(w.get("word", "")).strip()
        ]
        segments: list[Segment] = []
        for seg in data.get("segments") or []:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            s, e = start_ms + s_to_ms(seg["start"]), start_ms + s_to_ms(seg["end"])
            inside = [w for w in words_by_time if s - 50 <= (w.s + w.e) // 2 <= e + 50]
            segments.append(Segment(
                start_ms=s, end_ms=e, text=text, language=detected, words=inside or None,
                speaker_id=str(seg["speaker"]) if seg.get("speaker") is not None else None,
                confidence=data.get("confidence"), provenance=provenance,
            ))  # fmt: skip
        if not segments and (data.get("text") or "").strip():
            # Plain response: one untimed block for the chunk. No finer timing exists, so none is invented.
            segments.append(Segment(start_ms=start_ms, end_ms=end_ms, text=data["text"].strip(),
                                    language=detected, words=None, provenance=provenance))  # fmt: skip
        return ChunkResult(segments, detected, bool(segments) and all(s.words for s in segments), cost)


class FakeTranscriber:
    """TEST ADAPTER — deterministic text on a fixed grid. Never real speech recognition."""

    name = "fake"
    model = "fake-test-adapter"
    chunk_ms = 5 * 60_000
    is_test = True

    def transcribe_chunk(self, wav: Path, start_ms: int, end_ms: int, language: str | None, prompt: str) -> ChunkResult:
        segments: list[Segment] = []
        t = start_ms
        index = start_ms // 4000
        while t + 3000 <= end_ms:
            tokens = ["Segment", "de", "test", "numéro", str(index)]
            words = [Word(w=" " + tok, s=t + i * 500, e=t + i * 500 + 450, p=1.0) for i, tok in enumerate(tokens)]
            segments.append(Segment(start_ms=t, end_ms=t + 2500, text=" ".join(tokens), language=language or "fr",
                                    confidence=1.0, words=words, provenance="fake-test-adapter"))  # fmt: skip
            t += 4000
            index += 1
        return ChunkResult(segments, language or "fr", True)


def build_transcriber(settings: Settings, runtime: RuntimeSettings) -> Transcriber:
    choice = settings.clipfactory_transcriber or runtime.transcription_provider
    if choice == "fake":
        return FakeTranscriber()
    if choice == "openrouter":
        return OpenRouterTranscriber(settings, runtime.openrouter_stt_model, timeout_s=runtime.llm_timeout_s)
    return MlxWhisperTranscriber(runtime.mlx_model)

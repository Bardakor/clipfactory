"""Pure-logic unit tests: timestamps, candidates, ranking, captions, refinement, input validation."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from clipfactory import timeutil as tu
from clipfactory.analysis import candidates as cand
from clipfactory.analysis import ranking
from clipfactory.analysis.audio import AudioFeatures, detect_events, robust_z, speech_stats, trailing_median
from clipfactory.analysis.refine import refine_boundaries
from clipfactory.analysis.visual import analyse_motion
from clipfactory.domain import Caption, ProjectSettings, Rect, Segment, Word
from clipfactory.errors import ValidationFailed
from clipfactory.media import ingest
from clipfactory.render import captions as cap
from clipfactory.render.layout import crop_centre, edge_persistence, find_window, resolve_layout, stable_face
from clipfactory.render.renderer import slugify
from clipfactory.settings_store import AudioDetectorSettings, RuntimeSettings, with_profile_vocabulary
from clipfactory.transcription.service import apply_corrections, correct_words

# ── timestamps ──────────────────────────────────────────────────────────────


def test_ms_frame_round_trip():
    for fps in (30, 60, 29.97):
        for frame in (0, 1, 29, 30, 899, 12345):
            assert tu.ms_to_frame(tu.frame_to_ms(frame, fps), fps) == frame


def test_frames_for_and_snap():
    assert tu.frames_for(0, 1000, 30) == 30
    assert tu.frames_for(12_720, 39_410, 30) == 801  # 26.69 s → nearest frame
    assert tu.frames_for(0, 1, 30) == 1  # never zero frames
    assert tu.snap_to_frame(1017, 30) == 1033  # frame 31 at 30 fps


def test_time_formatting():
    assert tu.srt_ts(3_723_045) == "01:02:03,045"
    assert tu.srt_ts(-5) == "00:00:00,000"
    assert tu.ffmpeg_ts(12_500) == "12.500"
    assert tu.clock(59_999) == "0:59" and tu.clock(3_661_000) == "1:01:01"


def test_overlap_and_iou():
    assert tu.overlap_ms(0, 10, 5, 20) == 5
    assert tu.overlap_ms(0, 10, 10, 20) == 0
    assert tu.iou(0, 10_000, 0, 10_000) == 1.0
    assert tu.iou(0, 10_000, 5_000, 15_000) == pytest.approx(1 / 3)
    assert tu.iou(0, 10, 20, 30) == 0.0


# ── signals and candidates ──────────────────────────────────────────────────


def _signals(n: int = 300, peaks: tuple[int, ...] = (60, 180)) -> cand.Signals:
    interest = np.full(n, 0.02, dtype=np.float32)
    for p in peaks:
        interest[p - 2 : p + 3] = [0.3, 0.6, 0.9, 0.6, 0.3]
    active = np.zeros(n, dtype=bool)
    active[40:90] = True
    zeros = np.zeros(n, dtype=np.float32)
    return cand.Signals(n * 1000, interest, zeros, zeros, zeros, zeros, active, zeros, ~active)


def test_sustained_loudness_has_no_novelty():
    """A long loud stretch must not score like a sudden event: novelty is measured against the recent past."""
    level = np.concatenate([np.full(600, -40.0), np.full(1200, -10.0)])  # 60 s quiet, then 120 s loud
    novelty = level - trailing_median(level, 300)
    assert novelty[610] > 20  # the onset stands out
    assert abs(novelty[1500]) < 1  # 90 s into the loud stretch: nothing new


def test_audio_events_are_grouped():
    z = np.zeros(1000, dtype=np.float32)
    z[100:103] = 5
    z[108:111] = 6  # 500 ms later: same event
    z[600:602] = 5
    features = AudioFeatures(rms_db=np.full(1000, -30, dtype=np.float32), energy_z=z, flux_z=np.zeros(1000, dtype=np.float32), silent=np.zeros(1000, dtype=bool))
    events = [e for e in detect_events(features, AudioDetectorSettings()) if e.kind == "energy_peak"]
    assert [(e.start_ms, e.end_ms) for e in events] == [(10_000, 11_100), (60_000, 60_200)]
    assert all(0 <= e.score <= 1 for e in events)


def test_scene_cut_is_not_counted_as_motion():
    diffs = np.full(100, 0.02, dtype=np.float32)
    diffs[50] = 0.6
    features = analyse_motion(diffs)
    assert list(features.cuts_ms) == [25_000]
    assert features.motion[50] < 0.05


def test_seeds_are_separated_and_windows_put_setup_first():
    signals = _signals()
    seeds = cand.find_seeds(signals, limit=5)
    assert seeds == [60, 180]
    start, end = cand.window_around(60, signals, 15_000, 90_000)
    assert start < 60_000 < end and (60_000 - start) > (end - 60_000)  # more room before the peak


def test_merge_candidates_prefers_model_boundaries():
    a = cand.CandidateWindow(10_000, 40_000, ["signals"], seed_ms=25_000)
    b = cand.CandidateWindow(12_000, 41_000, ["transcript_llm"], title="moment", confidence=0.8)
    c = cand.CandidateWindow(100_000, 130_000, ["signals"])
    merged = cand.merge_candidates([a, b, c])
    assert len(merged) == 2
    assert (merged[0].start_ms, merged[0].end_ms) == (12_000, 41_000)
    assert merged[0].origins == ["signals", "transcript_llm"] and merged[0].seed_ms == 25_000


def test_heuristic_score_penalises_dead_time():
    base = {"peak": 0.8, "mean": 0.4, "speech_coverage": 0.8, "hook": 0.5, "reaction": 0.5, "payoff_position": 0.65, "duration_s": 30}
    assert cand.heuristic_score(base | {"dead_time": 0.0}) > cand.heuristic_score(base | {"dead_time": 0.6})
    assert 0 <= cand.heuristic_score(base | {"dead_time": 1.0}) <= 1


# ── ranking ─────────────────────────────────────────────────────────────────


def _item(cid: str, start: int, *, heuristic=0.6, editorial=None, text="", event_type="x", **kw) -> ranking.ScoreInput:
    return ranking.ScoreInput(cid, start, start + 30_000, {"dead_time": 0.0}, heuristic, editorial=editorial, text=text, event_type=event_type, **kw)


def test_score_is_normalised_to_0_100():
    assert ranking.score_candidate(_item("a", 0, heuristic=1.0, editorial=1.0, multimodal=1.0)).score == 100
    low = ranking.score_candidate(_item("b", 0, heuristic=0.0, editorial=0.0, missing_context=1.0, confidence=0.0))
    assert low.score == 0
    assert 0 <= ranking.score_candidate(_item("c", 0, heuristic=0.4, editorial=0.7)).score <= 100


def test_missing_components_are_renormalised_and_labelled():
    result = ranking.score_candidate(_item("a", 0, heuristic=0.5))
    assert result.basis == "heuristic" and result.score == 50
    assert ranking.score_candidate(_item("a", 0, heuristic=0.5, editorial=0.5)).basis == "ai"


def test_penalties_lower_the_score():
    clean = ranking.score_candidate(_item("a", 0, heuristic=0.7, editorial=0.7))
    penalised = ranking.score_candidate(_item("a", 0, heuristic=0.7, editorial=0.7, missing_context=0.9, confidence=0.3))
    assert penalised.score < clean.score
    assert {"missing_context", "low_confidence"} <= penalised.penalties.keys()


def test_rubric_weights_and_profiles():
    weights = ranking.rubric_weights("gta_rp")
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["humor"] > weights["pacing"]
    funny = {k: 2 for k in ranking.RUBRIC} | {"humor": 10, "narrative": 9}
    assert ranking.editorial_from_rubric(funny, weights) > ranking.editorial_from_rubric(funny, ranking.rubric_weights("gta_action"))
    overridden = ranking.rubric_weights("gta_rp", {"gta_rp": {"humor": 0.0}})
    assert overridden["humor"] == 0


def test_every_profile_is_fully_defined():
    from typing import get_args

    from clipfactory.domain import Profile
    from clipfactory.pipeline.stages_editorial import DEFAULT_HASHTAGS

    for profile in get_args(Profile):
        assert set(ranking.PROFILE_WEIGHTS[profile]) == set(ranking.RUBRIC), profile
        assert ranking.PROFILE_FOCUS[profile] and DEFAULT_HASHTAGS[profile], profile


def test_profile_vocabulary_only_touches_its_own_profile():
    runtime = RuntimeSettings()
    assert with_profile_vocabulary(runtime, "gta_rp") is runtime  # same transcript cache key as before
    gta6 = with_profile_vocabulary(runtime, "gta6").vocabulary
    assert gta6[:2] == ["GTA 6", "GTA VI"] and "Port Gellhorn" in gta6 and gta6[-len(runtime.vocabulary) :] == runtime.vocabulary
    assert len(", ".join(gta6)) <= 800  # the whole list fits in the transcription prompt


def test_selection_deduplicates_overlap_and_text():
    pairs = [
        (i, ranking.score_candidate(i))
        for i in (
            _item("best", 0, heuristic=0.9, text="un deux trois quatre cinq six"),
            _item("overlap", 5_000, heuristic=0.85, text="sept huit neuf dix onze douze"),
            _item("same-words", 200_000, heuristic=0.8, text="un deux trois quatre cinq six"),
            _item("other", 400_000, heuristic=0.7, text="alpha beta gamma delta epsilon"),
        )
    ]
    selection = ranking.select_clips(pairs, count=4, threshold=10)
    assert selection.selected == ["best", "other"]
    assert selection.rejected["overlap"].startswith("doublon") and selection.rejected["same-words"].startswith("doublon")


def test_selection_returns_fewer_than_requested_and_explains():
    pairs = [(i, ranking.score_candidate(i)) for i in (_item("ok", 0, heuristic=0.8), _item("weak", 100_000, heuristic=0.2))]
    selection = ranking.select_clips(pairs, count=5, threshold=60)
    assert selection.selected == ["ok"]
    assert "Seulement 1 clip(s) sur les 5" in selection.explanation and selection.rejected["weak"].startswith("sous le seuil")


def test_selection_prefers_variety():
    pairs = [(i, ranking.score_candidate(i)) for i in (
        _item("a", 0, heuristic=0.80, event_type="chase"),
        _item("b", 100_000, heuristic=0.79, event_type="chase"),
        _item("c", 200_000, heuristic=0.78, event_type="dialogue"),
    )]  # fmt: skip
    assert ranking.select_clips(pairs, count=2, threshold=0).selected == ["a", "c"]


def test_feedback_needs_enough_examples():
    assert ranking.feedback_adjustments([("chase", "accepted")] * 5) == {}  # five examples are not a model
    history = [("chase", "accepted")] * 8 + [("dialogue", "rejected")] * 8
    adjust = ranking.feedback_adjustments(history)
    assert adjust["chase"] > 0 > adjust["dialogue"]
    assert all(abs(v) <= ranking.MAX_FEEDBACK_ADJUSTMENT for v in adjust.values())


# ── captions / transcript synchronisation ───────────────────────────────────


def _segment(start: int, text: str, *, words: bool = True, gap: int = 400) -> Segment:
    tokens = text.split()
    ws = [Word(w=" " + t, s=start + i * gap, e=start + i * gap + gap - 50, p=0.9) for i, t in enumerate(tokens)] if words else None
    return Segment(start_ms=start, end_ms=start + len(tokens) * gap, text=text, words=ws, provenance="test")


def test_captions_are_clip_relative_and_in_range():
    segments = [_segment(10_000, "avant le clip"), _segment(20_000, "un deux trois quatre cinq six sept huit"), _segment(40_000, "après")]
    captions, precision = cap.build_captions(segments, 19_500, 24_000, "viral")
    assert precision == "word"
    assert [c.text.strip() for c in captions] == ["un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit"]
    assert captions[0].startMs == 500 and all(0 <= c.startMs < c.endMs <= 4500 for c in captions)
    assert all(c.timestampMs is not None for c in captions)
    assert captions[-1].pageBreakAfter is True


def test_captions_respect_page_length():
    captions, _ = cap.build_captions([_segment(0, "attends attends il y a un policier juste devant non non non")], 0, 10_000, "viral")
    for page in cap.pages_of(captions):
        assert len(page.text) <= cap.PAGE_CHARS["viral"] + 10  # a single long word may exceed the target


def test_elided_word_pieces_are_merged():
    seg = Segment(start_ms=0, end_ms=1000, text="Mais non, c'est lui", provenance="t", words=[
        Word(w=" Mais", s=0, e=200), Word(w=" non,", s=200, e=400), Word(w=" c", s=400, e=450), Word(w="'est", s=450, e=600), Word(w=" lui", s=600, e=900),
    ])  # fmt: skip
    captions, _ = cap.build_captions([seg], 0, 1000, "clean")
    assert [c.text for c in captions] == [" Mais", " non,", " c'est", " lui"]
    assert captions[2].startMs == 400 and captions[2].endMs == 600


def test_segment_precision_never_fabricates_word_timing():
    captions, precision = cap.build_captions([_segment(0, "une phrase sans horodatage au mot", words=False)], 0, 5000, "viral")
    assert precision == "segment"
    assert captions and all(c.timestampMs is None for c in captions)
    assert all(not page.measured for page in cap.pages_of(captions))


def test_page_edit_keeps_timing_when_word_count_matches():
    captions, _ = cap.build_captions([_segment(0, "un deux trois quatre")], 0, 5000, "clean")
    edited = cap.apply_page_edits(captions, {0: "UN DEUX TROIS QUATRE"})
    assert [(c.startMs, c.endMs) for c in edited] == [(c.startMs, c.endMs) for c in captions]
    assert [c.text.strip() for c in edited] == ["UN", "DEUX", "TROIS", "QUATRE"]
    reworded = cap.apply_page_edits(captions, {0: "deux mots"})
    assert all(c.timestampMs is None for c in reworded)  # timing no longer measured → no word highlight
    assert reworded[0].startMs == captions[0].startMs and reworded[-1].endMs == captions[-1].endMs
    assert cap.apply_page_edits(captions, {0: "  "}) == []


def test_srt_output():
    captions, _ = cap.build_captions([_segment(1000, "bonjour tout le monde."), _segment(6000, "deuxième phrase ici")], 0, 10_000, "clean")
    srt = cap.to_srt(captions)
    assert srt.startswith("1\n00:00:01,000 --> ")
    assert "bonjour tout le monde." in srt and "\n2\n" in srt


def test_vocabulary_corrections_merge_timed_words():
    words = [Word(w=" sur", s=0, e=100), Word(w=" five", s=100, e=200, p=0.5), Word(w=" m,", s=200, e=300, p=0.9), Word(w=" lspd", s=300, e=400)]
    fixed = correct_words(words, {"five m": "FiveM", "lspd": "LSPD"})
    assert [w.w for w in fixed] == [" sur", " FiveM,", " LSPD"]
    assert (fixed[1].s, fixed[1].e, fixed[1].p) == (100, 300, 0.5)
    untimed = apply_corrections(Segment(start_ms=0, end_ms=1, text="on joue sur five m", provenance="t"), {"five m": "FiveM"})
    assert untimed.text == "on joue sur FiveM"


# ── boundary refinement ─────────────────────────────────────────────────────


def test_refinement_snaps_to_sentences_and_never_cuts_a_word():
    segments = [_segment(8_000, "phrase précédente"), _segment(12_000, "attends il y a un policier"), _segment(16_000, "non non freine"),
                _segment(17_600, "oh la la")]  # fmt: skip
    start, end, notes = refine_boundaries(12_900, 16_700, segments=segments, silences=[(9_000, 11_900)], cuts_ms=[], duration_ms=60_000,
                                          min_len_ms=3_000, max_len_ms=60_000)  # fmt: skip
    assert start == 12_000 - 180  # sentence start, with lead-in
    assert end >= segments[3].end_ms  # extended through the reaction that follows immediately
    for seg in segments:
        for w in seg.words or []:
            assert not (w.s < start < w.e) and not (w.s < end < w.e)
    assert notes


def test_refinement_enforces_length_limits():
    segments = [_segment(i * 5_000, "un deux trois quatre cinq") for i in range(30)]
    start, end, _ = refine_boundaries(10_000, 140_000, segments=segments, silences=[], cuts_ms=[], duration_ms=150_000, min_len_ms=15_000, max_len_ms=60_000)
    assert end - start <= 60_000 + 500
    start, end, _ = refine_boundaries(50_000, 52_000, segments=[], silences=[], cuts_ms=[], duration_ms=150_000, min_len_ms=15_000, max_len_ms=60_000)
    assert end - start == 15_000
    start, end, _ = refine_boundaries(0, 5_000, segments=[], silences=[], cuts_ms=[], duration_ms=8_000, min_len_ms=15_000, max_len_ms=60_000)
    assert (start, end) == (0, 8_000)  # clamped to the source


# ── layout ──────────────────────────────────────────────────────────────────


def test_auto_layout_preserves_full_gameplay():
    layout = resolve_layout("auto", src_w=1920, src_h=1080, out_w=1080, out_h=1920, duration_ms=30_000)
    panel = layout.segments[0].panels[0]
    assert layout.used == "full" and (panel.src.w, panel.src.h) == (1, 1)
    assert panel.w == 1080 and panel.h == 608
    assert panel.y + panel.h < layout.caption_y < 1920 * 0.78  # captions below the gameplay, above the platform UI


def test_split_falls_back_without_a_reliable_facecam():
    layout = resolve_layout("split", src_w=1920, src_h=1080, out_w=1080, out_h=1920, duration_ms=30_000, facecam={"found": False})
    assert layout.used == "full" and "facecam" in layout.reason


def test_split_stacks_streamer_and_game_edge_to_edge():
    cam = {"found": True, "window": "edges", "rect": {"x": 0.02, "y": 0.6, "w": 0.25, "h": 0.25}, "face": {"x": 0.11, "y": 0.64, "w": 0.06, "h": 0.12}}
    kw = dict(src_w=1920, src_h=1080, out_w=1080, out_h=1920, duration_ms=30_000, facecam=cam)
    top = resolve_layout("auto", **kw)
    streamer, game = top.segments[0].panels
    assert top.used == "split" and "en haut" in top.reason
    assert (streamer.x, streamer.y, streamer.w) == (0, 0, 1080) and (game.y, game.w) == (streamer.h, 1080) and streamer.h + game.h == 1920
    assert 0.26 * 1920 <= streamer.h <= 0.38 * 1920 and streamer.y + streamer.h < top.caption_y < 1920 * 0.78
    for panel in (streamer, game):  # nothing is stretched: each source crop has the shape of its panel
        assert (panel.src.w * 1920) / (panel.src.h * 1080) == pytest.approx(panel.w / panel.h, rel=0.01)
    assert streamer.src.x >= 0.02 and streamer.src.x + streamer.src.w <= 0.27 + 1e-6  # stays inside the webcam window
    assert game.src.x == pytest.approx((1 - game.src.w) / 2) and game.src.h == 1  # full height, centred

    bottom = resolve_layout("split", **kw, facecam_position="bottom")
    game_b, streamer_b = bottom.segments[0].panels
    assert game_b.y == 0 and streamer_b.y == game_b.h and streamer_b.src == streamer.src and "en bas" in bottom.reason
    assert bottom.caption_y < game_b.h

    tall = cam | {"rect": {"x": 0.02, "y": 0.4, "w": 0.2, "h": 0.55}, "face": {"x": 0.09, "y": 0.45, "w": 0.06, "h": 0.12}}
    cropped = resolve_layout("split", **(kw | {"facecam": tall})).segments[0].panels[0]
    assert cropped.h == int(0.38 * 1920) // 2 * 2 and cropped.src.y == pytest.approx(0.4)  # too tall: cropped, keeping the face


def test_stable_face_ignores_wandering_faces():
    cam, wander = (0.10, 0.70, 0.06, 0.12), [(0.1 * i, 0.3, 0.05, 0.1) for i in range(8)]
    frames = [[cam, wander[i]] if i % 4 else [wander[i]] for i in range(8)]  # the webcam face is missed in 2 frames of 8
    face, hits = stable_face(frames)
    assert hits == 6 and face == pytest.approx(cam)
    assert stable_face([[w] for w in wander])[1] == 1
    assert stable_face([[(0.3, 0.2, 0.4, 0.5)]] * 8) == (None, 0)  # a full-screen face is not an overlay


def test_find_window_measures_a_webcam_border_on_changing_footage():
    rng = np.random.default_rng(0)
    frames = []
    for i in range(12):
        frame = cv2.GaussianBlur(rng.integers(0, 255, (270, 480), dtype=np.uint8), (0, 0), 6)  # ever-changing "game"
        frame[180:250, 20:130] = 60 + (i % 3)  # a webcam window with a dark, nearly static room
        frame[195:225, 60:90] = 170  # the face
        cv2.rectangle(frame, (20, 180), (130, 250), 255, 2)
        frames.append(frame)
    vertical, horizontal = edge_persistence(frames)
    face = (60 / 480, 195 / 270, 30 / 480, 30 / 270)
    (x, y, w, h), score = find_window(face, vertical, horizontal)
    assert score > 0.9
    assert (x * 480, y * 270, (x + w) * 480, (y + h) * 270) == pytest.approx((20, 180, 130, 250), abs=5)  # lands just inside the 2 px border
    blank = np.zeros((270, 480), np.float32)
    assert find_window(face, blank, blank) is None  # a cut-out streamer has no border to measure


def test_crop_uses_saliency_only_when_confident():
    centres, strong, weak = np.full(40, 0.7), np.full(40, 0.9), np.full(40, 0.1)
    assert crop_centre(centres, strong)[1] > 0.55 > crop_centre(centres, weak)[1]
    confident = resolve_layout("crop", src_w=1920, src_h=1080, out_w=1080, out_h=1920, duration_ms=20_000, saliency=(centres, strong))
    rect = confident.segments[0].panels[0].src
    assert rect.x + rect.w / 2 == pytest.approx(0.7, abs=0.01)
    unsure = resolve_layout("crop", src_w=1920, src_h=1080, out_w=1080, out_h=1920, duration_ms=20_000, saliency=(centres, weak))
    rect = unsure.segments[0].panels[0].src
    assert rect.x + rect.w / 2 == pytest.approx(0.5, abs=0.01)  # safe fallback: centred


def test_dynamic_layout_switches_only_on_cuts():
    centres = np.concatenate([np.full(20, 0.3), np.full(20, 0.5)])
    conf = np.concatenate([np.full(20, 0.95), np.zeros(20)])
    layout = resolve_layout("dynamic", src_w=1920, src_h=1080, out_w=1080, out_h=1920, duration_ms=20_000, saliency=(centres, conf), cuts_ms=[10_000, 11_000])
    assert layout.used == "dynamic" and [(s.from_ms, s.to_ms) for s in layout.segments] == [(0, 10_000), (10_000, 20_000)]


# ── validation and security ─────────────────────────────────────────────────


@pytest.mark.parametrize("url,video_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
])  # fmt: skip
def test_youtube_urls_accepted(url, video_id):
    assert ingest.parse_youtube_url(url) == video_id


@pytest.mark.parametrize("url", [
    "http://www.youtube.com/watch?v=dQw4w9WgXcQ",  # not https
    "https://evil.example/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
    "https://user@www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/playlist?list=PL123",
    "https://www.youtube.com/watch?v=../../etc",
    "file:///etc/passwd",
    "not a url",
])  # fmt: skip
def test_youtube_urls_rejected(url):
    with pytest.raises(ValidationFailed):
        ingest.parse_youtube_url(url)


@pytest.mark.parametrize(("url", "kind", "video_id", "canonical"), [
    ("https://www.twitch.tv/videos/2860537669?t=1h2m", "twitch", "2860537669", "https://www.twitch.tv/videos/2860537669"),
    ("https://m.twitch.tv/videos/2860537669", "twitch", "2860537669", "https://www.twitch.tv/videos/2860537669"),
    ("https://www.twitch.tv/nasa/clip/FitSnappyCaribou-F7IcTqcQWY2V9SrX", "twitch", "FitSnappyCaribou-F7IcTqcQWY2V9SrX", "https://clips.twitch.tv/FitSnappyCaribou-F7IcTqcQWY2V9SrX"),
    ("https://clips.twitch.tv/IronicGrotesqueCaribouDxAbomb", "twitch", "IronicGrotesqueCaribouDxAbomb", "https://clips.twitch.tv/IronicGrotesqueCaribouDxAbomb"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
])  # fmt: skip
def test_video_urls_resolve_to_a_site_and_a_canonical_link(url, kind, video_id, canonical):
    assert ingest.parse_video_url(url) == (kind, video_id)
    assert ingest.canonical_url(kind, video_id) == canonical


@pytest.mark.parametrize("url", [
    "https://www.twitch.tv/nasa",  # a channel: live broadcast, not importable
    "https://www.twitch.tv/nasa/videos",
    "https://www.twitch.tv/videos/abc",
    "https://www.twitch.tv/videos/2860537669/../../x",
    "http://www.twitch.tv/videos/2860537669",
    "https://twitch.tv.evil.example/videos/2860537669",
    "https://user@www.twitch.tv/videos/2860537669",
    "https://clips.twitch.tv/a/b",
    "https://vimeo.com/123456",
    "https://127.0.0.1/videos/2860537669",
])  # fmt: skip
def test_video_urls_rejected(url):
    with pytest.raises(ValidationFailed):
        ingest.parse_video_url(url)


def test_upload_names_are_never_trusted():
    assert ingest.validated_extension("../../clip.MP4") == ".mp4"
    assert ingest.safe_title("../../../etc/passwd") == "passwd"
    assert ingest.safe_title("C:\\Users\\x\\vidéo finale.mov") == "vidéo finale.mov"
    for bad in ("payload.exe", "noextension", "clip.mp4.sh"):
        with pytest.raises(ValidationFailed):
            ingest.validated_extension(bad)


def test_data_paths_cannot_escape(settings):
    assert settings.path("exports", "a.mp4").parent == settings.data_dir / "exports"
    for parts in (("..", "secret"), ("exports", "..", "..", "x"), ("/etc/passwd",)):
        with pytest.raises(ValidationFailed):
            settings.path(*parts)


def test_project_settings_validation():
    assert ProjectSettings().clip_count == 8
    for bad in ({"clip_count": 0}, {"clip_count": 31}, {"min_len_s": 60, "max_len_s": 30}, {"width": 1234}, {"fps": 24}):
        with pytest.raises(ValueError):
            ProjectSettings(**bad)
    assert Rect(x=0.8, y=0, w=0.5, h=1).w == pytest.approx(0.2)  # clipped to the frame


def test_misc_helpers():
    assert slugify("Attends, c'est LUI qui a reculé !") == "attends-c-est-lui-qui-a-recule"
    assert slugify("???") == "clip"
    assert robust_z(np.array([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 30.0]))[-1] > 3
    stats = speech_stats([_segment(2_000, "un deux trois")], 10_000)
    assert stats.active[2] and not stats.active[8]
    assert Caption(text=" a", startMs=0, endMs=1).pageBreakAfter is None


def test_rank_briefs_ignore_what_ranking_writes():
    """Regression (found on a real paid run): ranking rewrote candidate titles, which changed its own
    input, so re-ranking with a new threshold missed the cache and paid for the same calls again."""
    from clipfactory.models import Candidate
    from clipfactory.pipeline.stages_editorial import rank_briefs

    segments = [_segment(10_000, "attends il y a un policier juste devant")]
    rows = [
        Candidate(id="a", start_ms=9_000, end_ms=20_000, origins=["signals"], title="", evidence={"features": {"peak": 0.9}}),
        Candidate(id="b", start_ms=9_500, end_ms=21_000, origins=["transcript_llm"], title="Le policier", structure={"setup": "s", "payoff": "p"},
                  evidence={"features": {"peak": 0.8}, "visual": {"event_type": "police_encounter"}}),
    ]  # fmt: skip
    before = [b.render() for b in rank_briefs(rows, segments)]
    for row in rows:  # everything stage_rank and stage_refine store on a candidate
        row.evidence = {**row.evidence, "main_event": "Un événement", "components": {"editorial": 0.7}, "rubric": {"hook": 8},
                        "basis": "ai", "refinement": ["note"], "refined": [1, 2]}  # fmt: skip
        row.justification, row.final_score, row.editorial_score = "Parce que.", 61.0, 0.7
        row.penalties, row.status, row.reject_reason, row.event_type = {"dead_time": 0.1}, "selected", None, "police_encounter"
    assert [b.render() for b in rank_briefs(rows, segments)] == before

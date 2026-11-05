import json

import pytest

from vyral import llm, render
from vyral.voice import Word, align_script


def clip(path, duration):
    return {"path": path, "duration": duration}


def test_segments_cover_total_and_stay_inside_clips():
    clips = [clip("a", 4.0), clip("b", 1.0), clip("c", 30.0)]
    segments = render.plan_segments(clips, 20.0)
    assert sum(s.length for s in segments) >= 20.0
    durations = {c["path"]: c["duration"] for c in clips}
    for segment in segments:
        assert segment.start + segment.length <= durations[segment.path] + 1e-6


def test_rotation_changes_first_clip():
    clips = [clip("a", 10.0), clip("b", 10.0)]
    assert render.plan_segments(clips, 5, 0)[0].path == "a"
    assert render.plan_segments(clips, 5, 1)[0].path == "b"


def test_repeated_clip_shows_new_footage():
    segments = render.plan_segments([clip("a", 10.0)], 5.0)
    assert segments[0].start == 0 and segments[1].start > 0


def test_segments_require_usable_clip():
    with pytest.raises(render.RenderError):
        render.plan_segments([clip("a", 0.1)], 5.0)


def test_alignment_keeps_exact_script_wording():
    heard = [Word("Try", 0.0, 0.2), Word("glow", 0.2, 0.5), Word("mist", 0.5, 0.8), Word("today.", 0.8, 1.2)]
    words = align_script("Try GlowMist today.", heard, 1.2)
    assert [w.text for w in words] == ["Try", "GlowMist", "today."]
    assert words[0].start == 0.0
    assert words[2].end == 1.2
    assert all(a.start <= b.start for a, b in zip(words, words[1:]))


def test_alignment_with_nothing_heard_spreads_words():
    words = align_script("one two", [], 2.0)
    assert (words[0].start, words[1].start, words[1].end) == (0.0, 1.0, 2.0)


def test_alignment_ignores_broken_transcription():
    # The real failure: only the last line was heard, with one word lasting 14 seconds.
    script = " ".join(f"word{i}" for i in range(20)) + " add tabasco to your meal"
    heard = [Word("add", 0.0, 0.9), Word("tabasco", 0.9, 14.9), Word("to", 14.9, 15.0),
             Word("your", 15.0, 15.3), Word("meal", 15.3, 23.0)]
    words = align_script(script, heard, 25.0)
    assert len(words) == 25
    gaps = [b.start - a.start for a, b in zip(words, words[1:])]
    assert max(gaps) - min(gaps) < 0.01
    assert words[-1].end == pytest.approx(25.0)


def test_transcribe_does_not_accept_a_script_prompt():
    import inspect

    from vyral.voice import transcribe
    assert "prompt" not in inspect.signature(transcribe).parameters


def test_captions_escape_override_blocks():
    ass = render.build_ass([Word("{bad}", 0.0, 0.5), Word("word", 0.5, 1.0)])
    assert "{bad}" not in ass
    assert "(bad)" in ass


def test_caption_groups_break_on_sentences():
    words = [Word(t, 0, 1) for t in ["Stop.", "Try", "this", "very", "simple", "thing"]]
    assert [len(g) for g in render.group_words(words)] == [1, 4, 1]


def envelope(result, is_error=False):
    return json.dumps({"result": result, "is_error": is_error})


GOOD = {"brief": {"summary": "s", "pain_points": ["p"], "angles": ["a"], "missing_info": []},
        "hooks": [{"text": "hook", "angle": "a"}],
        "scripts": [{"angle": "a", "body": "body", "cta": "buy", "flags": ["check"]}]}


def test_parse_reply_accepts_fenced_json():
    copy = llm.parse_reply(envelope("```json\n" + json.dumps(GOOD) + "\n```"))
    assert copy["hooks"][0]["text"] == "hook"
    assert copy["scripts"][0]["flags"] == ["check"]


@pytest.mark.parametrize("stdout", [
    "not json",
    envelope("no json here"),
    envelope("{broken"),
    envelope('{"hooks": [], "scripts": []}'),
    envelope("limit reached", is_error=True),
])
def test_parse_reply_rejects_bad_output(stdout):
    with pytest.raises(llm.LLMError):
        llm.parse_reply(stdout)

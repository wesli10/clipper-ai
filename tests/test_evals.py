"""Evals: the agent against what was already delivered (SPEC-agent 11).

Built from a synthetic delivery tree and a synthetic run log - no network, no
model, no Drive.
"""

from __future__ import annotations

import json

import pytest

from clipper.config import load_settings
from clipper.evals import baseline as baseline_mod
from clipper.evals import compare as compare_mod
from clipper.evals import report as report_mod
from clipper.evals.models import Baseline, BaselineClip, BaselineVideo, EvalReport
from clipper.models import Clip, ClipSet, TimeRange
from clipper.utils.errors import InputError

VIDEO_ID = "RfCT-LJkfx8"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\n"
        "deliver:\n"
        "  mode: folder\n"
        f"  drive_root: {tmp_path / 'drive'}\n",
        encoding="utf-8",
    )
    return load_settings(config)


def deliver_clip(settings, folder: str, title: str, segments, profile_folder="shorts"):
    """One delivered clip, as `deliver` writes it."""
    root = (
        settings.deliver.drive_path
        / settings.deliver.root_folder
        / "canal"
        / "2026-08-25_episodio"
        / profile_folder
        / folder
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "profile": "short",
                "title": title,
                "hook_line": "",
                "description": "",
                "source_url": f"{URL}&t={int(segments[0][0])}s",
                "source_segments": segments,
                "language": "pt",
            }
        ),
        encoding="utf-8",
    )


# --- baseline -------------------------------------------------------------- #


def test_the_baseline_is_rebuilt_from_the_delivered_clips(settings):
    """The clips are the record; a list kept beside them would drift."""
    deliver_clip(settings, "01_marmita", "a marmita proibida", [[913.0, 967.0]])
    deliver_clip(settings, "02_vigilante", "vigilante pego dormindo", [[1815.0, 1852.0]])

    built = baseline_mod.build(settings)

    assert len(built.videos) == 1
    video = built.videos[0]
    assert video.video_id == VIDEO_ID and video.url == URL
    assert [clip.title for clip in video.clips] == [
        "a marmita proibida",
        "vigilante pego dormindo",
    ]
    assert video.clips[0].segments == [(913.0, 967.0)]
    assert video.clips[0].profile == "short"


def test_videos_with_more_clips_come_first(settings):
    deliver_clip(settings, "01_a", "um", [[10.0, 40.0]])
    other = "https://www.youtube.com/watch?v=qH2SB1A2h7U"
    root = settings.deliver.drive_path / settings.deliver.root_folder / "canal" / "outro"
    for index in range(2):
        folder = root / "shorts" / f"0{index}_x"
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(
            json.dumps(
                {
                    "profile": "short",
                    "title": f"outro {index}",
                    "hook_line": "",
                    "description": "",
                    "source_url": other,
                    "source_segments": [[index * 100.0, index * 100.0 + 30.0]],
                }
            ),
            encoding="utf-8",
        )

    built = baseline_mod.build(settings)
    assert [v.video_id for v in built.videos] == ["qH2SB1A2h7U", VIDEO_ID]


def test_a_delivery_that_is_not_there_says_so(settings):
    with pytest.raises(InputError, match="nao encontrada"):
        baseline_mod.build(settings)


def test_the_baseline_round_trips(settings, tmp_path):
    deliver_clip(settings, "01_a", "um", [[10.0, 40.0]])
    path = baseline_mod.save(baseline_mod.build(settings), tmp_path / "baseline.json")
    assert baseline_mod.load(path).videos[0].video_id == VIDEO_ID


def test_loading_a_baseline_that_was_never_built_is_actionable(tmp_path):
    with pytest.raises(InputError, match="clipper eval baseline"):
        baseline_mod.load(tmp_path / "nada.json")


# --- overlap --------------------------------------------------------------- #


def test_the_same_interval_overlaps_completely():
    assert compare_mod.overlap_ratio([(10.0, 40.0)], [(10.0, 40.0)]) == 1.0


def test_disjoint_intervals_do_not_overlap():
    assert compare_mod.overlap_ratio([(10.0, 40.0)], [(50.0, 80.0)]) == 0.0


def test_a_clip_twice_as_long_is_not_the_same_clip():
    """Union, not the shorter side: containing it is not being it."""
    assert compare_mod.overlap_ratio([(10.0, 70.0)], [(10.0, 40.0)]) == pytest.approx(0.5)


def test_multi_interval_clips_are_compared_whole():
    left = [(10.0, 40.0), (100.0, 130.0)]
    right = [(10.0, 40.0), (100.0, 130.0)]
    assert compare_mod.overlap_ratio(left, right) == 1.0


# --- matching -------------------------------------------------------------- #


def baseline_clips():
    return [
        BaselineClip(profile="short", title="marmita", segments=[(913.0, 967.0)]),
        BaselineClip(profile="short", title="vigilante", segments=[(1815.0, 1852.0)]),
    ]


def test_each_side_is_used_once_and_the_best_pair_wins():
    agent = [
        ("v/short/1", "quase a marmita", [(915.0, 965.0)]),
        ("v/short/2", "a marmita mesmo", [(913.0, 967.0)]),
    ]
    matches, missed = compare_mod.match_clips(agent, baseline_clips())

    paired = {m.clip_id: m.baseline_title for m in matches}
    assert paired["v/short/2"] == "marmita"
    assert paired["v/short/1"] == ""  # o par ja tinha dono
    assert missed == ["vigilante"]


def test_a_clip_the_baseline_never_had_is_reported_without_a_pair():
    agent = [("v/short/1", "coisa nova", [(10.0, 60.0)])]
    matches, missed = compare_mod.match_clips(agent, baseline_clips())

    assert matches[0].baseline_title == "" and matches[0].overlap == 0.0
    assert sorted(missed) == ["marmita", "vigilante"]


def test_a_faint_overlap_is_not_a_match():
    agent = [("v/short/1", "roçou", [(960.0, 1000.0)])]
    matches, _ = compare_mod.match_clips(agent, baseline_clips())
    assert matches[0].baseline_title == ""


# --- comparing a run ------------------------------------------------------- #


def write_agent_clips(settings, segments: list[tuple[float, float]]):
    clips = [
        Clip(
            index=index,
            slug=f"corte-{index}",
            profile="short",
            topic=f"corte {index}",
            segments=[TimeRange(start=start, end=end)],
        )
        for index, (start, end) in enumerate(segments, start=1)
    ]
    path = settings.profile_dir(VIDEO_ID, "short")
    path.mkdir(parents=True, exist_ok=True)
    (path / "clips.json").write_text(
        ClipSet(video_id=VIDEO_ID, profile="short", clips=clips).model_dump_json(),
        encoding="utf-8",
    )


def test_a_run_is_compared_against_its_baseline(settings):
    write_agent_clips(settings, [(913.0, 967.0), (10.0, 60.0)])
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, title="episodio", clips=baseline_clips())

    result = compare_mod.compare(settings, video, profiles=["short"])

    assert result.baseline_clips == 2 and result.agent_clips == 2
    assert result.matched == 1
    assert result.mean_overlap == 1.0
    assert result.missed == ["vigilante"]
    assert result.state == "sem run"


def test_what_the_run_cost_comes_from_its_own_log(settings):
    from clipper.agent.budget import Budget
    from clipper.agent.log import RunLog
    from clipper.config import AgentSettings

    write_agent_clips(settings, [(913.0, 967.0)])
    budget = Budget(limits=AgentSettings())
    run_id = "20260911-120000-RfCT"
    with RunLog(settings.agent_dir(VIDEO_ID) / f"{run_id}.jsonl", run_id) as log:
        log.run_start(VIDEO_ID, url=URL, model="claude-opus-5", budget=budget)
        log.tool_call(
            "render_clip",
            {"video_id": VIDEO_ID, "profile": "short", "index": 1},
            output=json.dumps({"clip_id": f"{VIDEO_ID}/short/1", "reused": False}),
            duration_ms=40_000,
            is_error=False,
            budget=budget,
        )
        log.tool_call(
            "review_render",
            {"clip_id": f"{VIDEO_ID}/short/1"},
            output=json.dumps(
                {
                    "clip_id": f"{VIDEO_ID}/short/1",
                    "verdict": "warn",
                    "checks": [{"check": "tail_roll", "status": "warn", "value": "x"}],
                }
            ),
            duration_ms=10,
            is_error=False,
            budget=budget,
        )
        log.run_end("done", budget=budget, cost_usd=1.23)

    video = BaselineVideo(video_id=VIDEO_ID, url=URL, clips=baseline_clips())
    result = compare_mod.compare(settings, video, profiles=["short"])

    assert result.run_id == run_id and result.state == "concluida"
    assert result.cost_usd == 1.23
    assert result.renders == 1 and result.renders_per_clip == 1.0
    assert result.warns == 1 and result.fails == 0
    assert result.matches[0].verdict == "warn"


# --- the two documents ----------------------------------------------------- #


def test_the_notes_file_has_a_row_per_clip_and_an_empty_column(tmp_path, settings):
    write_agent_clips(settings, [(913.0, 967.0)])
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, clips=baseline_clips())
    comparison = compare_mod.compare(settings, video, profiles=["short"])

    path = report_mod.write_notes(tmp_path / "notas.md", [comparison])
    text = path.read_text(encoding="utf-8")

    assert f"`{VIDEO_ID}/short/1`" in text
    assert "| nota |" in text
    assert report_mod.read_notes(path) == {}


def test_your_marks_are_read_back(tmp_path):
    path = tmp_path / "notas.md"
    path.write_text(
        report_mod.NOTES_HEADER
        + "| `vid/short/1` | um corte | 4 | ficou bom |\n"
        + "| `vid/short/2` | outro | | ainda nao vi |\n"
        + "| `vid/short/3` | terceiro | 9 | fora da escala |\n",
        encoding="utf-8",
    )
    assert report_mod.read_notes(path) == {"vid/short/1": 4}


def test_the_report_puts_the_measurement_and_your_mark_side_by_side(tmp_path, settings):
    write_agent_clips(settings, [(913.0, 967.0)])
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, title="episodio", clips=baseline_clips())
    comparison = compare_mod.compare(settings, video, profiles=["short"])

    report_mod.build(tmp_path, [comparison])
    (tmp_path / report_mod.NOTES_NAME).write_text(
        report_mod.NOTES_HEADER + f"| `{VIDEO_ID}/short/1` | corte 1 | 5 | otimo |\n",
        encoding="utf-8",
    )
    report = report_mod.build(tmp_path, [comparison])

    text = (tmp_path / report_mod.REPORT_NAME).read_text(encoding="utf-8")
    assert "sua nota media: 5.0" in text
    assert "vigilante" in text  # o que o baseline tinha e o agent nao achou
    assert report.mean_note == 5.0


def test_rewriting_the_report_does_not_wipe_your_notes(tmp_path, settings):
    write_agent_clips(settings, [(913.0, 967.0)])
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, clips=baseline_clips())
    comparison = compare_mod.compare(settings, video, profiles=["short"])

    report_mod.build(tmp_path, [comparison])
    notes = tmp_path / report_mod.NOTES_NAME
    notes.write_text(
        report_mod.NOTES_HEADER + f"| `{VIDEO_ID}/short/1` | corte | 3 | mediano |\n",
        encoding="utf-8",
    )
    report_mod.build(tmp_path, [comparison])

    assert "3 | mediano" in notes.read_text(encoding="utf-8")


def test_an_empty_report_still_says_what_to_do():
    empty = EvalReport(built_at="2026-09-11T00:00:00Z", videos=[])
    assert "preencha notas.md" in report_mod.render(empty)


def test_the_baseline_can_be_filtered_by_profile():
    video = BaselineVideo(
        video_id=VIDEO_ID,
        clips=[
            BaselineClip(profile="short", title="curto", segments=[(1.0, 2.0)]),
            BaselineClip(profile="long", title="longo", segments=[(1.0, 2.0)]),
        ],
    )
    assert [clip.title for clip in video.for_profile(["short"])] == ["curto"]


def test_a_baseline_finds_its_video_by_id():
    baseline = Baseline(videos=[BaselineVideo(video_id=VIDEO_ID)])
    assert baseline.video(VIDEO_ID) is not None
    assert baseline.video("outro") is None


def test_a_video_the_agent_already_published_is_not_compared_to_itself(settings):
    """Rebuilding the baseline from the Drive hands the agent back its own work."""
    from clipper.state import State

    write_agent_clips(settings, [(913.0, 967.0)])
    State(settings.state_db).start_run("20260911-120000-RfCT", VIDEO_ID)
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, clips=baseline_clips())

    # O baseline foi lido depois que aquela run ja tinha publicado.
    result = compare_mod.compare(
        settings, video, profiles=["short"], built_at="2027-01-01T00:00:00+00:00"
    )

    assert result.self_baseline is True
    assert result.comparable is False
    assert result.mean_overlap == 1.0  # medido, mas nao contado


def test_the_evals_own_run_does_not_taint_the_baseline(settings):
    """The eval runs after the baseline is read; counting it would flag everything."""
    from clipper.state import State

    write_agent_clips(settings, [(913.0, 967.0)])
    State(settings.state_db).start_run("20260911-120000-RfCT", VIDEO_ID)
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, clips=baseline_clips())

    result = compare_mod.compare(
        settings, video, profiles=["short"], built_at="2020-01-01T00:00:00+00:00"
    )

    assert result.self_baseline is False and result.comparable is True


def test_the_headline_number_says_what_it_is_made_of(tmp_path, settings):
    from clipper.state import State

    write_agent_clips(settings, [(913.0, 967.0)])
    State(settings.state_db).start_run("20260911-120000-RfCT", VIDEO_ID)
    video = BaselineVideo(video_id=VIDEO_ID, url=URL, clips=baseline_clips())
    tainted = compare_mod.compare(
        settings, video, profiles=["short"], built_at="2027-01-01T00:00:00+00:00"
    )
    absent = compare_mod.compare(
        settings, BaselineVideo(video_id="outro", clips=baseline_clips()), profiles=["short"]
    )

    text = report_mod.render(EvalReport(videos=[tainted, absent]))
    assert "nenhum video comparavel ainda" in text
    assert "sem run do agent: 1 video(s)" in text
    assert "baseline do proprio agent em 1 video(s)" in text
    assert "nao mede nada" in text

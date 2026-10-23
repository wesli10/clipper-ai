"""Review decided in the browser: request file out, response file in."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.models import (
    Clip,
    ClipSet,
    ClipStatus,
    ReviewDecision,
    ReviewResponse,
    TimeRange,
    Transcript,
)
from clipper.monitor.server import build_server
from clipper.monitor.snapshot import build_snapshot
from clipper.profiles import resolve_profile
from clipper.stages.review import (
    REQUEST_NAME,
    RESPONSE_NAME,
    apply_response,
    build_request,
    web_review,
)
from clipper.state import StageStatus, State
from clipper.utils.errors import InputError

VIDEO_ID = "vtZXoKcm1Yz"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(workspace=tmp_path / "workspace", default_profiles=["short"])


@pytest.fixture
def profile(prompts_dir: Path):
    return resolve_profile("short", {}, prompts_dir)


def make_set() -> ClipSet:
    clips = [
        Clip(
            index=i,
            slug=f"corte-{i}",
            profile="short",
            segments=[TimeRange(start=10.0 * i, end=10.0 * i + 30)],
            hook=f"gancho {i}",
            reason=f"motivo {i}",
            topic=f"assunto {i}",
            score=90.0 - i,
        )
        for i in (1, 2, 3)
    ]
    return ClipSet(video_id=VIDEO_ID, profile="short", clips=clips)


def test_request_carries_what_the_page_shows(profile, transcript: Transcript) -> None:
    clip_set = make_set()
    clip_set.clips[0].segments = [TimeRange(start=0.0, end=10.0)]
    request = build_request(clip_set, profile, transcript, video_title="Titulo", channel="Canal")

    assert request.video_id == VIDEO_ID and request.profile == "short"
    assert request.video_title == "Titulo" and request.n_clips == profile.n_clips
    assert [c.index for c in request.clips] == [1, 2, 3]
    first = request.clips[0]
    assert first.topic == "assunto 1" and first.hook == "gancho 1"
    assert "programa" in first.text  # trecho da transcricao, nao o transcript inteiro


def test_undecided_clips_are_kept_like_the_terminal_does(profile, transcript) -> None:
    clip_set = make_set()
    response = ReviewResponse(
        decisions=[
            ReviewDecision(index=1, status=ClipStatus.APPROVED),
            ReviewDecision(index=2, status=ClipStatus.REJECTED),
        ]
    )
    applied = apply_response(clip_set, response)
    assert [str(c.status) for c in applied.clips] == ["approved", "rejected", "approved"]


def test_web_review_waits_for_the_answer(settings: Settings, profile, transcript) -> None:
    request_dir = settings.profile_dir(VIDEO_ID, "short")
    request_dir.mkdir(parents=True, exist_ok=True)

    def answer() -> None:
        path = request_dir / REQUEST_NAME
        for _ in range(200):
            if path.exists():
                break
            time.sleep(0.01)
        (request_dir / RESPONSE_NAME).write_text(
            ReviewResponse(
                decisions=[
                    ReviewDecision(index=1, status=ClipStatus.REJECTED),
                    ReviewDecision(index=2, status=ClipStatus.APPROVED),
                    ReviewDecision(index=3, status=ClipStatus.REJECTED),
                ]
            ).model_dump_json(),
            encoding="utf-8",
        )

    thread = threading.Thread(target=answer)
    thread.start()
    reviewed = web_review(
        make_set(), profile, transcript, request_dir, timeout_s=10, poll_interval_s=0.02
    )
    thread.join()

    assert [str(c.status) for c in reviewed.clips] == ["rejected", "approved", "rejected"]
    # o pedido some quando e respondido: e ele que mantem a chamada na pagina
    assert not (request_dir / REQUEST_NAME).exists()


def test_web_review_gives_up_and_cleans_the_request(
    settings: Settings, profile, transcript
) -> None:
    request_dir = settings.profile_dir(VIDEO_ID, "short")
    with pytest.raises(InputError, match="ninguem revisou"):
        web_review(
            make_set(), profile, transcript, request_dir, timeout_s=0.05, poll_interval_s=0.01
        )
    assert not (request_dir / REQUEST_NAME).exists()


def seed_pending(settings: Settings, profile, transcript) -> Path:
    """A video stopped at the review, the way the pipeline leaves it."""
    state = State(settings.state_db)
    state.set_stage(VIDEO_ID, "select", StageStatus.DONE, profile="short")
    state.set_stage(
        VIDEO_ID, "review", StageStatus.RUNNING, profile="short", detail="aguardando sua revisao"
    )
    request_dir = settings.profile_dir(VIDEO_ID, "short")
    request_dir.mkdir(parents=True, exist_ok=True)
    clip_set = make_set()
    (request_dir / "clips.json").write_text(clip_set.model_dump_json(), encoding="utf-8")
    (request_dir / REQUEST_NAME).write_text(
        build_request(clip_set, profile, transcript, video_title="Titulo").model_dump_json(),
        encoding="utf-8",
    )
    return request_dir


def test_snapshot_shows_the_pending_review(settings: Settings, profile, transcript) -> None:
    seed_pending(settings, profile, transcript)
    snapshot = build_snapshot(settings)

    assert len(snapshot.pending_reviews) == 1
    pending = snapshot.pending_reviews[0]
    assert pending.video_id == VIDEO_ID and len(pending.clips) == 3
    short = next(p for p in snapshot.videos[0].profiles if p.name == "short")
    assert short.awaiting_review


@pytest.fixture
def server(settings: Settings) -> Iterator[str]:
    httpd = build_server(settings, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def post(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url + "/api/review",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_page_writes_the_response_file(
    settings: Settings, server: str, profile, transcript
) -> None:
    request_dir = seed_pending(settings, profile, transcript)
    status, body = post(
        server,
        {
            "video_id": VIDEO_ID,
            "profile": "short",
            "decisions": [
                {"index": 1, "status": "approved"},
                {"index": 2, "status": "rejected"},
                {"index": 3, "status": "approved"},
            ],
        },
    )
    assert status == 200 and body == {"ok": True, "approved": 2, "total": 3}

    written = ReviewResponse.model_validate_json(
        (request_dir / RESPONSE_NAME).read_text(encoding="utf-8")
    )
    assert written.video_id == VIDEO_ID and written.decided_at > 0
    assert {d.index: str(d.status) for d in written.decisions} == {
        1: "approved",
        2: "rejected",
        3: "approved",
    }


def test_review_endpoint_refuses_what_it_cannot_trust(
    settings: Settings, server: str, profile, transcript
) -> None:
    seed_pending(settings, profile, transcript)
    decision = [{"index": 1, "status": "approved"}]

    # nada pendente para esse perfil
    status, _ = post(server, {"video_id": VIDEO_ID, "profile": "long", "decisions": decision})
    assert status == 409
    # corte que nao existe no pedido
    status, _ = post(
        server,
        {
            "video_id": VIDEO_ID,
            "profile": "short",
            "decisions": [{"index": 99, "status": "approved"}],
        },
    )
    assert status == 400
    # travessia de caminho
    status, _ = post(server, {"video_id": "../etc", "profile": "short", "decisions": decision})
    assert status == 400
    # indecisao nao e resposta
    status, _ = post(
        server,
        {
            "video_id": VIDEO_ID,
            "profile": "short",
            "decisions": [{"index": 1, "status": "pending"}],
        },
    )
    assert status == 400


def test_source_is_served_in_ranges_for_the_player(settings: Settings, server: str) -> None:
    video_dir = settings.video_dir(VIDEO_ID)
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "source.mp4").write_bytes(bytes(range(256)) * 8)

    request = urllib.request.Request(
        f"{server}/api/source?video={VIDEO_ID}", headers={"Range": "bytes=10-19"}
    )
    with urllib.request.urlopen(request, timeout=5) as answer:
        assert answer.status == 206
        assert answer.headers["Content-Range"] == "bytes 10-19/2048"
        assert len(answer.read()) == 10

    with pytest.raises(urllib.error.HTTPError) as missing:
        urllib.request.urlopen(f"{server}/api/source?video=naoexiste11", timeout=5)
    assert missing.value.code == 404

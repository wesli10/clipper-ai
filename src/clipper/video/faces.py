"""Face sampling for reframing (SPEC 7.3 step 3).

``faces.json`` is computed once per video and reused by every profile that asks
for ``reframe: face`` (SPEC 4.3).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from clipper.utils.errors import ExternalError

#: One sample every half second, as specified.
SAMPLE_INTERVAL_S = 0.5


class FaceBox(BaseModel):
    """A detected face, in normalised coordinates (0-1) of the source frame."""

    cx: float
    cy: float
    width: float
    height: float
    score: float = 0.0

    @property
    def area(self) -> float:
        return self.width * self.height


class FaceSample(BaseModel):
    t: float
    faces: list[FaceBox] = Field(default_factory=list)

    @property
    def largest(self) -> FaceBox | None:
        return max(self.faces, key=lambda f: f.area) if self.faces else None


class FaceTrack(BaseModel):
    video_id: str
    interval: float = SAMPLE_INTERVAL_S
    width: int = 0
    height: int = 0
    samples: list[FaceSample] = Field(default_factory=list)

    def between(self, start: float, end: float) -> list[FaceSample]:
        return [s for s in self.samples if start <= s.t <= end]


#: BlazeFace **full range**, the equivalent of the legacy ``model_selection=1``.
#:
#: The short-range model the Tasks docs advertise assumes a selfie-sized face. In
#: a 16:9 talking-head shot the speaker's face is nearer 10% of the frame width,
#: and short-range simply does not see it - measured on a real portrait: 0
#: detections short-range, 1 at 0.81 confidence full-range.
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-assets/face_detection_full_range_sparse.tflite"
)
MODEL_FILENAME = "face_detection_full_range_sparse.tflite"


def model_path(cache_dir: Path | None = None) -> Path:
    """Path to the detector weights, downloading them once if needed.

    mediapipe 1.x dropped the bundled ``solutions`` API; the Tasks API that
    replaced it takes an explicit model file.
    """
    import urllib.request

    root = cache_dir or Path.home() / ".cache" / "clipper"
    destination = root / MODEL_FILENAME
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    root.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part")
    try:
        urllib.request.urlopen(MODEL_URL, timeout=60)
    except OSError as exc:
        raise ExternalError(
            f"nao consegui baixar o modelo de deteccao de rosto: {exc}\n"
            f"Baixe {MODEL_URL} manualmente para {destination}."
        ) from exc
    try:
        urllib.request.urlretrieve(MODEL_URL, partial)
        partial.replace(destination)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ExternalError(f"falha ao baixar o modelo de deteccao de rosto: {exc}") from exc
    return destination


def detect_faces(
    video_path: Path,
    video_id: str,
    *,
    interval: float = SAMPLE_INTERVAL_S,
    min_confidence: float = 0.5,
    logger: logging.Logger | None = None,
    cache_dir: Path | None = None,
) -> FaceTrack:
    """Sample one frame every ``interval`` seconds and record the faces found."""
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
    except ImportError as exc:
        raise ExternalError(
            "mediapipe/opencv nao estao instalados. Rode: uv sync --extra vision"
        ) from exc

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ExternalError(f"nao consegui abrir o video para detectar rostos: {video_path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = frame_count / fps if fps else 0.0

    # Fail before loading a model for a file we cannot read. OpenCV's wheel has no
    # AV1 decoder, and YouTube serves AV1 in mp4 containers; without this check the
    # loop just produces no samples and the caller cannot tell that apart from a
    # video with no faces in it.
    readable, _probe_frame = capture.read()
    if not readable:
        capture.release()
        raise ExternalError(
            f"nao consegui decodificar nenhum frame de {video_path.name} para detectar "
            "rostos. O OpenCV nao decodifica AV1; rebaixe a fonte para H.264 "
            "(rode 'clipper ingest <url> --force' com a versao atual do seletor de "
            "formato) ou use 'reframe: center' no perfil."
        )
    capture.set(cv2.CAP_PROP_POS_MSEC, 0.0)

    samples: list[FaceSample] = []
    options = vision.FaceDetectorOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model_path(cache_dir))),
        running_mode=vision.RunningMode.VIDEO,
        min_detection_confidence=min_confidence,
    )
    detector = vision.FaceDetector.create_from_options(options)
    try:
        t = 0.0
        while duration == 0.0 or t <= duration:
            capture.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = capture.read()
            if not ok:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = detector.detect_for_video(image, int(t * 1000))
            faces: list[FaceBox] = []
            for detection in result.detections or []:
                # The Tasks API reports pixels; the rest of the pipeline is normalised.
                box = detection.bounding_box
                if not width or not height:
                    continue
                faces.append(
                    FaceBox(
                        cx=(box.origin_x + box.width / 2) / width,
                        cy=(box.origin_y + box.height / 2) / height,
                        width=box.width / width,
                        height=box.height / height,
                        score=float(detection.categories[0].score) if detection.categories else 0.0,
                    )
                )
            samples.append(FaceSample(t=round(t, 3), faces=faces))
            t += interval
    finally:
        detector.close()
        capture.release()

    if logger is not None:
        with_faces = sum(1 for s in samples if s.faces)
        logger.info(
            "faces: %d amostras, %d com rosto (%.0f%%)",
            len(samples),
            with_faces,
            100 * with_faces / len(samples) if samples else 0,
        )
    return FaceTrack(
        video_id=video_id, interval=interval, width=width, height=height, samples=samples
    )


def load_or_detect(
    video_path: Path,
    faces_path: Path,
    video_id: str,
    *,
    force: bool = False,
    logger: logging.Logger | None = None,
) -> FaceTrack:
    """Cached wrapper: ``faces.json`` is computed once per video (SPEC 4.3)."""
    if faces_path.exists() and not force:
        return FaceTrack.model_validate_json(faces_path.read_text(encoding="utf-8"))
    track = detect_faces(video_path, video_id, logger=logger)
    faces_path.parent.mkdir(parents=True, exist_ok=True)
    faces_path.write_text(track.model_dump_json(indent=2), encoding="utf-8")
    return track

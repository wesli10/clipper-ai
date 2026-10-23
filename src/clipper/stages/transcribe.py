"""Stage 1b - local transcription with faster-whisper (SPEC 5.3).

VRAM rule (CLAUDE.md): load -> use -> unload -> ``torch.cuda.empty_cache()``.
Never hold the whisper model and a local LLM at the same time.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Any

from clipper.config import Settings, TranscribeSettings
from clipper.models import Segment, Transcript, Word
from clipper.utils.errors import ExternalError
from clipper.utils.logging import stage_logger


def transcribe(
    settings: Settings,
    video_id: str,
    audio_path: Path,
    *,
    language: str | None = None,
    force: bool = False,
) -> Transcript:
    """Produce ``transcript.json`` for ``video_id``."""
    out_path = settings.video_dir(video_id) / "transcript.json"
    logger = stage_logger(settings.logs_dir(video_id), "transcribe")

    if not force and out_path.exists():
        logger.info("transcricao reaproveitada de %s", out_path)
        return Transcript.model_validate_json(out_path.read_text(encoding="utf-8"))

    if not audio_path.exists():
        raise ExternalError(f"audio nao encontrado: {audio_path} (rode 'clipper ingest' antes)")

    requested = language or settings.transcribe.language
    lang = None if requested in (None, "", "auto") else requested

    transcript = _run_whisper(settings.transcribe, video_id, audio_path, lang, logger)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "transcricao concluida: %d segmentos, idioma %s, %.1fs",
        len(transcript.segments),
        transcript.language,
        transcript.duration,
    )
    return transcript


#: Shared libraries CTranslate2 dlopens at runtime, in load order.
_CUDA_LIBS: tuple[tuple[str, str], ...] = (
    ("nvidia/cublas/lib", "libcublasLt.so.12"),
    ("nvidia/cublas/lib", "libcublas.so.12"),
    ("nvidia/cudnn/lib", "libcudnn_ops.so.9"),
    ("nvidia/cudnn/lib", "libcudnn_cnn.so.9"),
    ("nvidia/cudnn/lib", "libcudnn.so.9"),
)


def preload_cuda_libraries(logger: logging.Logger) -> bool:
    """Load cuBLAS/cuDNN from the installed nvidia wheels into this process.

    CTranslate2 resolves them with ``dlopen`` at model-load time. They ship as
    pip packages rather than system libraries, so without help the loader does
    not find them and every CUDA attempt fails - which looks exactly like an OOM
    and silently demotes the run to CPU. Loading them ``RTLD_GLOBAL`` here makes
    them resolvable without asking the user to set ``LD_LIBRARY_PATH``.
    """
    import ctypes
    import site

    roots = [Path(p) for p in site.getsitepackages()]
    roots += [Path(p) for p in (site.getusersitepackages(),) if p]
    loaded = 0
    for subdir, name in _CUDA_LIBS:
        for root in roots:
            candidate = root / subdir / name
            if not candidate.exists():
                continue
            try:
                ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)
            except OSError as exc:
                logger.debug("nao consegui pre-carregar %s: %s", candidate, exc)
            else:
                loaded += 1
            break
    if loaded:
        logger.debug("pre-carregadas %d bibliotecas CUDA dos pacotes nvidia", loaded)
    return loaded > 0


def _run_whisper(
    cfg: TranscribeSettings,
    video_id: str,
    audio_path: Path,
    language: str | None,
    logger: logging.Logger,
) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ExternalError("faster-whisper nao esta instalado. Rode: uv sync --extra gpu") from exc

    if cfg.device != "cpu":
        preload_cuda_libraries(logger)

    attempts: list[tuple[str, str, str]] = [(cfg.model, cfg.device, cfg.compute_type)]
    if cfg.fallback_model and cfg.fallback_model != cfg.model:
        attempts.append((cfg.fallback_model, cfg.device, cfg.compute_type))
    attempts.append((cfg.fallback_model or cfg.model, "cpu", "int8"))

    last_error: Exception | None = None
    for model_name, device, compute_type in attempts:
        logger.info("carregando whisper %s em %s (%s)", model_name, device, compute_type)
        model: Any = None
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=cfg.beam_size,
                word_timestamps=True,
                vad_filter=cfg.vad_filter,
            )
            segments = _collect(segments_iter)
            return Transcript(
                video_id=video_id,
                language=getattr(info, "language", language or "unknown") or "unknown",
                duration=float(getattr(info, "duration", 0.0) or 0.0)
                or (segments[-1].end if segments else 0.0),
                segments=segments,
            )
        except Exception as exc:  # retry on any backend failure (OOM, bad compute type)
            last_error = exc
            if not _is_recoverable(exc):
                raise ExternalError(f"falha na transcricao: {exc}") from exc
            logger.warning(
                "whisper %s em %s falhou (%s); tentando a proxima configuracao",
                model_name,
                device,
                exc,
            )
        finally:
            _unload(model, logger)

    raise ExternalError(f"nao foi possivel transcrever o audio: {last_error}")


def _collect(segments_iter: Any) -> list[Segment]:
    segments: list[Segment] = []
    for index, seg in enumerate(segments_iter):
        words = [
            Word(
                text=(w.word or "").strip(),
                start=float(w.start),
                end=float(w.end),
                prob=float(getattr(w, "probability", 1.0) or 1.0),
            )
            for w in (getattr(seg, "words", None) or [])
            if w.start is not None and w.end is not None and (w.word or "").strip()
        ]
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                id=index,
                start=float(seg.start),
                end=float(seg.end),
                text=text,
                words=words,
            )
        )
    return segments


def _is_recoverable(exc: Exception) -> bool:
    """OOM, missing CUDA libraries and bad compute types are worth retrying smaller."""
    message = str(exc).lower()
    markers = ("out of memory", "cuda", "cudnn", "cublas", "compute type", "unsupported", "cudart")
    return any(marker in message for marker in markers)


def _unload(model: Any, logger: logging.Logger) -> None:
    """Free VRAM before any other GPU stage runs (CLAUDE.md)."""
    if model is not None:
        del model
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        logger.debug("VRAM liberada")

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DEMUXERS = {"webm": "matroska", "mp4": "mov", "ogg": "ogg", "wav": "wav", "mp3": "mp3", "flac": "flac"}
MODEL_ENV = "BRAIN_WHISPER_MODEL"
CONVERT_TIMEOUT = 60
TRANSCRIBE_TIMEOUT = 120
MAX_SECONDS = 600
DECODE_SECONDS = MAX_SECONDS + 1
MAX_WAV_BYTES = 20_000_000

_MATROSKA = bytes.fromhex("1A45DFA3")
_MP3_SYNC = (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
_MP4_BRANDS = frozenset({b"M4A ", b"mp42", b"isom", b"3gp4", b"3gp5", b"3gp6", b"qt  "})
_SOUND_ANNOTATION = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_STDERR_TAIL = 200


class TranscriptionError(Exception):
    ...


class ClipOverLimit(Exception):
    ...


def sniff_audio(data: bytes) -> str | None:
    """The kind of audio in `data`, one of the kinds `DEMUXERS` names, or None when its first bytes match none.

    An MP4 counts only when its major brand is one of `_MP4_BRANDS`, so HEIC and AVIF
    stills, which share the `ftyp` box, are not clips.
    """
    head = data[:12]
    if head[:4] == _MATROSKA:
        return "webm"
    if head[4:8] == b"ftyp":
        return "mp4" if head[8:12] in _MP4_BRANDS else None
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or head[:2] in _MP3_SYNC:
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    return None


def model_path(environ: Mapping[str, str]) -> Path | None:
    value = (environ.get(MODEL_ENV) or "").strip()
    return Path(value).expanduser() if value else None


def _child_environment() -> dict[str, str]:
    environ = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    environ.setdefault("PATH", os.defpath)
    return environ


def _execute(run: Callable[..., Any], args: list[str], *, stage: str, timeout: int) -> None:
    try:
        done = run(
            args, capture_output=True, timeout=timeout, env=_child_environment(), shell=False,
        )
    except subprocess.TimeoutExpired:
        raise TranscriptionError(f"{stage} ran past {timeout} seconds") from None
    except OSError as exc:
        raise TranscriptionError(f"{stage} could not be started ({type(exc).__name__})") from None
    if done.returncode != 0:
        tail = (done.stderr or b"").decode("utf-8", "replace").strip()[-_STDERR_TAIL:]
        raise TranscriptionError(f"{stage} exited {done.returncode}: {tail}")


def _wav_seconds(path: Path) -> float:
    byte_rate = 0
    try:
        with path.open("rb") as handle:
            head = handle.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                raise TranscriptionError("ffmpeg wrote no WAV")
            while len(chunk := handle.read(8)) == 8:
                name, size = chunk[:4], int.from_bytes(chunk[4:], "little")
                if name == b"data" and byte_rate > 0:
                    return size / byte_rate
                if name == b"data":
                    break
                skip = size + (size & 1)
                if name == b"fmt " and size >= 16:
                    byte_rate = int.from_bytes(handle.read(16)[8:12], "little")
                    skip -= 16
                handle.seek(skip, os.SEEK_CUR)
    except OSError:
        raise TranscriptionError("ffmpeg wrote no WAV") from None
    raise TranscriptionError("ffmpeg wrote a WAV without a format chunk before its data")


def transcribe(
    data: bytes,
    *,
    kind: str,
    model: Path,
    run: Callable[..., Any] = subprocess.run,
    prompt: str | None = None,
) -> str:
    """The words spoken in the clip `data`, whose `kind` `sniff_audio` decided.

    ffmpeg reads the clip only as that kind, with the demuxer `DEMUXERS` names and
    local files only, and converts at most `DECODE_SECONDS` of it, and at most
    `MAX_WAV_BYTES`, to 16 kHz mono WAV; then whisper-cli transcribes it with the
    language detected and `prompt`, when given, as its vocabulary hint. Each command gets an argument list, `shell=False`, a timeout and
    an environment of `PATH` and `HOME` only. The work happens in a fresh folder of mode
    0700 that is deleted whatever happens. Raises `ClipOverLimit` when the WAV's header
    says it plays longer than `MAX_SECONDS`, before whisper-cli starts. Raises
    `TranscriptionError` for a kind that `DEMUXERS` does not name, when a command
    exits non-zero, runs out of time or cannot start, when ffmpeg leaves no readable
    WAV, and when the transcript holds no words (empty, or nothing but annotations such
    as `[BLANK_AUDIO]`).
    """
    demuxer = DEMUXERS.get(kind)
    if demuxer is None:
        raise TranscriptionError(f"{kind!r} is not a clip kind")
    folder = Path(tempfile.mkdtemp(prefix="brain-voice-"))
    try:
        source, wav, prefix = folder / "in.bin", folder / "out.wav", folder / "out"
        source.write_bytes(data)
        _execute(
            run,
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
             "-protocol_whitelist", "file", "-f", demuxer, "-i", str(source),
             "-t", str(DECODE_SECONDS), "-fs", str(MAX_WAV_BYTES),
             "-ac", "1", "-ar", "16000", "-f", "wav", str(wav)],
            stage="ffmpeg", timeout=CONVERT_TIMEOUT,
        )
        if _wav_seconds(wav) > MAX_SECONDS:
            raise ClipOverLimit(f"the clip plays longer than {MAX_SECONDS} seconds")
        _execute(
            run,
            ["whisper-cli", "-m", str(model), "-f", str(wav), "-l", "auto", "-nt", "-otxt",
             "-of", str(prefix), *(["--prompt", prompt] if prompt else [])],
            stage="whisper-cli", timeout=TRANSCRIBE_TIMEOUT,
        )
        try:
            text = Path(f"{prefix}.txt").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            raise TranscriptionError("whisper-cli wrote no transcript") from None
        if not _SOUND_ANNOTATION.sub("", text).strip():
            raise TranscriptionError("the clip holds no speech")
        return text
    finally:
        shutil.rmtree(folder, ignore_errors=True)

from __future__ import annotations

import stat
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.brain.media.voice import (
    ClipOverLimit,
    TranscriptionError,
    model_path,
    sniff_audio,
    transcribe,
)

CLIP = b"RIFF\x00\x00\x00\x00WAVE"
SAMPLE_RATE, CHANNELS, SAMPLE_BITS = 16000, 1, 16
BLOCK_ALIGN = CHANNELS * SAMPLE_BITS // 8
BYTE_RATE = SAMPLE_RATE * BLOCK_ALIGN


def wav_header(seconds, *, chunks=b""):
    size = int(seconds * BYTE_RATE)
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, CHANNELS, SAMPLE_RATE, BYTE_RATE, BLOCK_ALIGN, SAMPLE_BITS)
    body = b"WAVE" + fmt + chunks + b"data" + struct.pack("<I", size)
    return b"RIFF" + struct.pack("<I", len(body) + size) + body


THREE_SECONDS = wav_header(3)


@pytest.mark.parametrize("head, kind", [(bytes.fromhex("1A45DFA3") + b"x", "webm"), (b"\x00\x00\x00\x18ftypmp42", "mp4"),
    (b"OggS", "ogg"), (b"RIFF\x00\x00\x00\x00WAVE", "wav"), (b"ID3\x03", "mp3"), (b"fLaC", "flac"), (b"\x89PNG", None), (b"PK\x03\x04", None)])
def test_sniff_audio(head, kind):
    assert sniff_audio(head) == kind


@pytest.mark.parametrize("head, kind", [
    (b"\xff\xfb\x90\x00", "mp3"), (b"\xff\xf3\x90\x00", "mp3"), (b"\xff\xf2\x90\x00", "mp3"),
    (b"RIFF\x00\x00\x00\x00WEBP", None), (b"RIFF\x00\x00\x00\x00AVI ", None),
    (b"GIF89a", None), (b"%PDF-1.7", None), (b"\xff\xd8\xff\xe0", None), (b"#EXTM3U\n", None),
    (b"ftyp", None), (b"", None),
])
def test_sniff_audio_reads_the_sync_bytes_and_ignores_what_only_looks_like_audio(head, kind):
    assert sniff_audio(head) == kind


@pytest.mark.parametrize("brand", [b"M4A ", b"mp42", b"isom", b"3gp4", b"3gp5", b"3gp6", b"qt  "])
def test_an_mp4_is_a_clip_only_with_an_audio_or_video_brand(brand):
    assert sniff_audio(b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00") == "mp4"


@pytest.mark.parametrize("brand", [b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis", b"crx ", b"jp2 ", b"M4A", b""])
def test_stills_and_other_brands_behind_ftyp_are_not_clips(brand):
    assert sniff_audio(b"\x00\x00\x00\x18ftyp" + brand) is None


@pytest.mark.parametrize("kind, demuxer", [("mp3", "mp3"), ("mp4", "mov"), ("webm", "matroska"),
                                           ("ogg", "ogg"), ("wav", "wav"), ("flac", "flac")])
def test_ffmpeg_reads_the_clip_as_the_sniffed_kind(tmp_path, kind, demuxer):
    commands = Commands()
    transcribe(CLIP, kind=kind, model=tmp_path / "m.bin", run=commands)
    convert, _ = commands.calls[0]
    i = convert.index("-i")
    assert convert[i - 4:i] == ["-protocol_whitelist", "file", "-f", demuxer]
    assert convert.count("-i") == 1 and convert[i + 1].endswith("in.bin")


def test_a_kind_that_is_not_a_clip_kind_is_refused_before_ffmpeg(tmp_path):
    commands = Commands()
    with pytest.raises(TranscriptionError, match="png"):
        transcribe(CLIP, kind="png", model=tmp_path / "m.bin", run=commands)
    assert commands.calls == []


class Commands:
    def __init__(self, *, ffmpeg_exit=0, whisper_exit=0, hang=None, missing=None, transcript=" hello there \n",
                 wav=THREE_SECONDS):
        self.ffmpeg_exit, self.whisper_exit, self.hang, self.missing = ffmpeg_exit, whisper_exit, hang, missing
        self.transcript, self.wav = transcript, wav
        self.calls, self.stages, self.folders = [], [], []

    def __call__(self, args, **kw):
        stage = "ffmpeg" if "-nostdin" in args else "whisper"
        self.calls.append((list(args), kw))
        self.stages.append(stage)
        self.folders.append(Path(args[-1]).parent if stage == "ffmpeg" else Path(args[args.index("-of") + 1]).parent)
        if self.missing == stage:
            raise FileNotFoundError(args[0])
        if self.hang == stage:
            raise subprocess.TimeoutExpired(args, kw["timeout"])
        if stage == "ffmpeg":
            if self.ffmpeg_exit == 0 and self.wav is not None:
                Path(args[-1]).write_bytes(self.wav)
            return SimpleNamespace(returncode=self.ffmpeg_exit, stdout=b"", stderr=b"in.bin: Invalid data")
        if self.transcript is not None:
            Path(args[args.index("-of") + 1] + ".txt").write_text(self.transcript)
        return SimpleNamespace(returncode=self.whisper_exit, stdout=b"", stderr=b"")


@pytest.mark.parametrize("commands, ran, message", [
    (Commands(ffmpeg_exit=1), ["ffmpeg"], "ffmpeg exited 1"),
    (Commands(hang="ffmpeg"), ["ffmpeg"], None),
    (Commands(missing="ffmpeg"), ["ffmpeg"], None),
    (Commands(whisper_exit=2), ["ffmpeg", "whisper"], None),
    (Commands(hang="whisper"), ["ffmpeg", "whisper"], None),
    (Commands(missing="whisper"), ["ffmpeg", "whisper"], None),
    (Commands(transcript=""), ["ffmpeg", "whisper"], None),
    (Commands(transcript=" \n\t"), ["ffmpeg", "whisper"], None),
    (Commands(transcript=None), ["ffmpeg", "whisper"], None),
    (Commands(transcript="[BLANK_AUDIO]\n"), ["ffmpeg", "whisper"], None),
    (Commands(transcript=" (soft music) [Applause]\n"), ["ffmpeg", "whisper"], None),
], ids=[
    "ffmpeg-exit", "ffmpeg-timeout", "ffmpeg-missing", "whisper-exit", "whisper-timeout", "whisper-missing",
    "empty-transcript", "blank-transcript", "no-transcript-file", "blank-audio-marker", "only-sound-annotations",
])
def test_transcribe_failure_modes_raise_and_clean_up(commands, ran, message, tmp_path):
    with pytest.raises(TranscriptionError, match=message):
        transcribe(CLIP, kind="wav", model=tmp_path / "m.bin", run=commands)
    assert commands.stages == ran
    assert all(not folder.exists() for folder in commands.folders)


def test_ffmpeg_decodes_one_second_past_ten_minutes_and_stops_at_twenty_megabytes_of_wav(tmp_path):
    commands = Commands()
    transcribe(CLIP, kind="wav", model=tmp_path / "m.bin", run=commands)
    convert, _ = commands.calls[0]
    assert convert[convert.index("-t") + 1] == "601" and convert[convert.index("-fs") + 1] == "20000000"
    assert convert.index("-i") < min(convert.index("-t"), convert.index("-fs"))


@pytest.mark.parametrize("seconds", [601, 600.5, 19 * 3600])
def test_a_wav_longer_than_ten_minutes_is_refused_before_whisper(tmp_path, seconds):
    commands = Commands(wav=wav_header(seconds))
    with pytest.raises(ClipOverLimit):
        transcribe(CLIP, kind="ogg", model=tmp_path / "m.bin", run=commands)
    assert commands.stages == ["ffmpeg"]
    assert all(not folder.exists() for folder in commands.folders)


def test_a_wav_of_ten_minutes_is_transcribed_past_the_chunks_before_its_data(tmp_path):
    listing = b"LIST" + struct.pack("<I", 26) + b"INFOISFT" + struct.pack("<I", 13) + b"Lavf62.3.100\x00\x00"
    commands = Commands(wav=wav_header(600, chunks=listing))
    assert transcribe(CLIP, kind="ogg", model=tmp_path / "m.bin", run=commands) == "hello there"
    assert commands.stages == ["ffmpeg", "whisper"]


@pytest.mark.parametrize("wav, message", [
    (None, "WAV"),
    (b"", "ffmpeg wrote no WAV"),
    (b"RIFF\x00\x00\x00\x00WAVE", "WAV"),
    (wav_header(3)[:36], "WAV"),
    (b"RIFF\x00\x00\x00\x00WAVEdata\x00\x10\x00\x00", "WAV"),
    (b"RIFF\x00\x00\x00\x00AVI LIST\x04\x00\x00\x00", "ffmpeg wrote no WAV"),
], ids=["missing", "empty", "no-chunks", "no-data-chunk", "no-format-chunk", "not-a-wav"])
def test_a_conversion_that_left_no_readable_wav_never_reaches_whisper(tmp_path, wav, message):
    commands = Commands(wav=wav)
    with pytest.raises(TranscriptionError, match=message):
        transcribe(CLIP, kind="wav", model=tmp_path / "m.bin", run=commands)
    assert commands.stages == ["ffmpeg"]


def test_speech_that_mentions_a_bracket_is_kept(tmp_path):
    commands = Commands(transcript=" see you [at noon] ok\n")
    assert transcribe(CLIP, kind="wav", model=tmp_path / "m.bin", run=commands) == "see you [at noon] ok"


def test_transcribe_runs_both_commands_with_timeouts_a_minimal_environment_and_no_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/opt/tools/bin")
    monkeypatch.setenv("HOME", "/home/user")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "canary-token")
    commands = Commands()
    model = tmp_path / "ggml-small.bin"

    transcribe(CLIP, kind="wav", model=model, run=commands)

    (convert, convert_kw), (speech, speech_kw) = commands.calls
    assert convert_kw["timeout"] == 60 and speech_kw["timeout"] == 120
    for kw in (convert_kw, speech_kw):
        assert kw["env"] == {"PATH": "/opt/tools/bin", "HOME": "/home/user"}
        assert kw["shell"] is False
    assert all(isinstance(part, str) for part in convert + speech)
    assert convert[0] == "ffmpeg" and speech[0] == "whisper-cli"
    assert convert[convert.index("-ac") + 1] == "1" and convert[-3:-1] == ["-f", "wav"]
    assert convert[convert.index("-ar") + 1] == "16000"
    assert convert[-1] == speech[speech.index("-f") + 1]
    assert speech[speech.index("-m") + 1] == str(model)
    assert speech[speech.index("-l") + 1] == "auto"
    assert "-nt" in speech and "-otxt" in speech


def test_transcribe_works_in_a_private_folder_holding_the_clip(tmp_path):
    seen = {}

    def run(args, **kw):
        if "-nostdin" in args:
            source = Path(args[args.index("-i") + 1])
            seen.update(
                folder=source.parent, name=source.parent.name, source=source.name,
                mode=stat.S_IMODE(source.parent.stat().st_mode), bytes=source.read_bytes(),
            )
            Path(args[-1]).write_bytes(wav_header(1))
        else:
            Path(args[args.index("-of") + 1] + ".txt").write_text("fine")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    assert transcribe(CLIP, kind="wav", model=tmp_path / "m.bin", run=run) == "fine"
    assert seen["name"].startswith("brain-voice-") and seen["source"] == "in.bin"
    assert seen["mode"] == 0o700 and seen["bytes"] == CLIP
    assert not seen["folder"].exists()


def test_a_crash_inside_a_command_still_removes_the_folder(tmp_path):
    folders = []

    def run(args, **kw):
        folders.append(Path(args[-1]).parent)
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError):
        transcribe(CLIP, kind="wav", model=tmp_path / "m.bin", run=run)
    assert not folders[0].exists()


def test_model_path_reads_brain_whisper_model():
    assert model_path({"BRAIN_WHISPER_MODEL": "/models/ggml-small.bin"}) == Path("/models/ggml-small.bin")
    assert model_path({"BRAIN_WHISPER_MODEL": " /models/ggml-small.bin "}) == Path("/models/ggml-small.bin")


@pytest.mark.parametrize("environ", [{}, {"BRAIN_WHISPER_MODEL": ""}, {"BRAIN_WHISPER_MODEL": "   "}])
def test_model_path_is_none_when_unset_or_blank(environ):
    assert model_path(environ) is None

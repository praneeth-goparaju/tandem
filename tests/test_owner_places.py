import subprocess
from pathlib import Path
from types import SimpleNamespace

from src.brain.media import voice
from src.brain.runtime.places import owner_places, place_prompt

PROFILE = {
    "user": {"locations": ["Northtown, Region", "Easton"], "family_locations": ["Southport"]},
    "housing_corridor": {"city_aliases": {"westfield": ["Westfield"], "suederhafen": ["Süderhafen", "Suederhafen"]}},
}


def test_owner_places_come_from_the_profile_locations_and_city_aliases():
    assert owner_places(PROFILE) == [
        "Northtown, Region", "Easton", "Southport", "Westfield", "Süderhafen", "Suederhafen",
    ]


def test_owner_places_ignore_a_profile_without_them_or_with_odd_values():
    assert owner_places({}) == []
    assert owner_places({"user": {"locations": "Easton"}, "housing_corridor": {"city_aliases": {"x": [3, None]}}}) == []
    assert owner_places({"user": {"locations": ["x" * 81]}}) == []


def test_the_whisper_prompt_names_the_places():
    assert place_prompt(["Northtown, Region", "Easton"]) == "Places: Northtown, Region, Easton."
    assert place_prompt([]) is None


def test_transcribe_passes_a_prompt_to_whisper(tmp_path):
    calls = []

    def run(args, **_kw):
        calls.append(args)
        if args[0] == "ffmpeg":
            wav = Path(args[-1])
            header = b"RIFF" + (36 + 32000).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
            header += (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
            header += (32000).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
            wav.write_bytes(header + b"data" + (32000).to_bytes(4, "little") + b"\0" * 32000)
        else:
            Path(args[args.index("-of") + 1] + ".txt").write_text("weather in Northtown tomorrow")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    text = voice.transcribe(b"RIFF....WAVE", kind="wav", model=tmp_path / "m.bin", run=run, prompt="Places: Northtown.")
    whisper = calls[1]
    assert text == "weather in Northtown tomorrow"
    assert whisper[whisper.index("--prompt") + 1] == "Places: Northtown."


def test_transcribe_without_a_prompt_passes_none(tmp_path):
    calls = []

    def run(args, **_kw):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 1)

    try:
        voice.transcribe(b"RIFF....WAVE", kind="wav", model=tmp_path / "m.bin", run=run)
    except voice.TranscriptionError:
        pass
    assert all("--prompt" not in call for call in calls)


def test_the_owners_country_counts_as_a_place():
    profile = {"user": {"locations": ["Easton"], "country": "Germany"}}
    assert owner_places(profile) == ["Easton", "Germany"]
    assert owner_places({"user": {"country": 7}}) == []

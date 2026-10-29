from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["inner", "close", "extended"]


class OccasionFlag(BaseModel):
    enabled: bool = False
    date: str | None = None


class PersonOccasions(BaseModel):
    birthday: OccasionFlag = Field(default_factory=OccasionFlag)
    anniversary: OccasionFlag = Field(default_factory=OccasionFlag)
    festivals: list[str] = Field(default_factory=list)


class Person(BaseModel):
    id: str
    name: str
    e164: str | None = None
    tier: Tier = "close"
    in_scope: bool = True
    occasions: PersonOccasions = Field(default_factory=PersonOccasions)
    dossier: str | None = None
    notes: str = ""
    icloud_href: str | None = None
    cadence: str | None = None
    last_interaction: str | None = None
    last_reminded: dict[str, str] = Field(default_factory=dict)


class PeopleFile(BaseModel):
    version: int = 1
    people: list[Person] = Field(default_factory=list)


class Festival(BaseModel):
    label: str
    dates: dict[str, str] = Field(default_factory=dict)
    default_audience: str | None = None


class FestivalsFile(BaseModel):
    version: int = 1
    festivals: dict[str, Festival] = Field(default_factory=dict)


class VoiceProfile(BaseModel):
    tone: str = ""
    length: str = ""
    emoji: str = ""
    signoff_examples: list[str] = Field(default_factory=list)


class LearnedEdit(BaseModel):
    before: str
    after: str
    ts: str


class VoiceFile(BaseModel):
    version: int = 1
    profile: VoiceProfile = Field(default_factory=VoiceProfile)
    banned_cliches: list[str] = Field(default_factory=list)
    learned_edits: list[LearnedEdit] = Field(default_factory=list)

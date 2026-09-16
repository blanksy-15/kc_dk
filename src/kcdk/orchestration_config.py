"""Non-secret configuration for the Discord bot and independent result runs."""
from dataclasses import dataclass, field
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class GeneralDrunkConfig:
    enabled: bool = True
    probability: float = 0.08
    global_cooldown_seconds: int = 720
    per_user_cooldown_seconds: int = 1800
    max_replies_per_hour: int = 3
    context_message_count: int = 15
    minimum_message_length: int = 15
    max_context_chars_per_message: int = 500
    max_output_tokens: int = 200
    max_reply_chars: int = 400
    model: str | None = None
    tone: str = "ruthless"
    allow_profanity: bool = True
    announcement_followups: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.allow_profanity) is not bool or type(self.announcement_followups) is not bool:
            raise ValueError("GENERAL_DRUNK switches must be booleans")
        if type(self.probability) not in {int,float} or not math.isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError("GENERAL_DRUNK probability must be between 0 and 1")
        limits = {"global_cooldown_seconds": (0,86400), "per_user_cooldown_seconds": (0,86400),
                  "max_replies_per_hour": (1,60), "context_message_count": (1,20),
                  "minimum_message_length": (1,2000), "max_context_chars_per_message": (1,1000),
                  "max_output_tokens": (32,500), "max_reply_chars": (1,600)}
        for name,(low,high) in limits.items():
            if type(getattr(self,name)) is not int or not low <= getattr(self,name) <= high:
                raise ValueError(f"Invalid GENERAL_DRUNK {name}")
        if self.tone not in {"normal","ruthless"}:
            raise ValueError("GENERAL_DRUNK tone must be normal or ruthless")
        if self.model is not None and (not isinstance(self.model,str) or not self.model.strip()):
            raise ValueError("GENERAL_DRUNK model must be a nonempty string or null")


@dataclass(frozen=True)
class OrchestrationConfig:
    guild_id: str
    general_channel_id: str
    h2h_channel_id: str
    tournament_channel_id: str
    h2h_leaderboard_channel_id: str
    tournament_leaderboard_channel_id: str
    authorized_poster_ids: tuple[str,...] = ()
    season_identifier: str = "kcdk-2026"
    season_name: str = "KCDK 2026"
    season_year: int | None = None
    announcement_week: str | None = None
    members_path: str = "data/members.csv"
    database_path: str = "data/processed/kcdk.sqlite"
    state_path: str = "data/processed/discord_orchestration.sqlite"
    h2h_dk_fee_cents: int | None = None
    commissioner_enabled: bool = True
    results_drunk_enabled: bool = True
    general_drunk: GeneralDrunkConfig = field(default_factory=GeneralDrunkConfig)
    announcement_templates: tuple[str,...] = (
        "{week}{label} is open. {fee} Head to <#{channel_id}>: {url}",
        "Commissioner's notice: {week}{label} entries are open. {fee} Join in <#{channel_id}>: {url}",
    )

    def __post_init__(self):
        for name in ("guild_id","general_channel_id","h2h_channel_id","tournament_channel_id",
                     "h2h_leaderboard_channel_id","tournament_leaderboard_channel_id"):
            if not isinstance(getattr(self,name),str) or not getattr(self,name).isdigit():
                raise ValueError(f"{name} must be a Discord snowflake string")
        for value in self.authorized_poster_ids:
            if not isinstance(value,str) or not value.isdigit():
                raise ValueError("Authorized poster IDs must be Discord snowflake strings")
        if len({self.general_channel_id,self.h2h_channel_id,self.tournament_channel_id}) != 3:
            raise ValueError("General, H2H and tournament channels must be distinct")
        if {self.h2h_leaderboard_channel_id,self.tournament_leaderboard_channel_id} & {self.h2h_channel_id,self.tournament_channel_id,self.general_channel_id}:
            raise ValueError("Persistent leaderboard channels must be separate from conversation/history")
        if type(self.commissioner_enabled) is not bool or type(self.results_drunk_enabled) is not bool:
            raise ValueError("Result commentary switches must be booleans")
        if not isinstance(self.season_identifier,str) or not self.season_identifier.strip():
            raise ValueError("Season identifier is required")
        if not isinstance(self.season_name,str) or not self.season_name.strip():
            raise ValueError("Season name is required")
        if self.season_year is not None and (type(self.season_year) is not int or not 1900<=self.season_year<=9999):
            raise ValueError("Season year must be an integer year or null")
        if self.h2h_dk_fee_cents is not None and (type(self.h2h_dk_fee_cents) is not int or self.h2h_dk_fee_cents<0):
            raise ValueError("H2H DraftKings fee must be nonnegative integer cents or null")
        if self.announcement_week is not None and (not isinstance(self.announcement_week,str) or len(self.announcement_week)>80):
            raise ValueError("Announcement week must be a short string or null")
        if not self.announcement_templates:
            raise ValueError("At least one announcement template is required")
        for template in self.announcement_templates:
            try:
                text=template.format(week="Week 1 ",label="Tournament",fee="$3 per entry.",channel_id="1",url="https://www.draftkings.com/contest/1")
            except (KeyError,ValueError,AttributeError,IndexError) as exc:
                raise ValueError("Invalid announcement template") from exc
            if not text or len(text)>1800:
                raise ValueError("Announcement template is empty or too long")
        if Path(self.database_path).resolve()==Path(self.state_path).resolve():
            raise ValueError("Orchestration state must not be the analytics database")

    @classmethod
    def load(cls,path):
        raw=json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw,dict):
            raise ValueError("Discord config must be an object")
        # Unknown keys (including tokens) fail rather than silently persisting secrets.
        try:
            raw["general_drunk"]=GeneralDrunkConfig(**raw.get("general_drunk",{}))
            for key in ("authorized_poster_ids","announcement_templates"):
                if key in raw:
                    if not isinstance(raw[key],list):
                        raise ValueError(f"{key} must be a JSON array")
                    raw[key]=tuple(raw[key])
            return cls(**raw)
        except TypeError as exc:
            raise ValueError("Unknown or missing orchestration configuration field") from exc

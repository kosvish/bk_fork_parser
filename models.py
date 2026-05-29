from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    WINLINE = "winline"
    CSGOPOSITIVE = "csgopositive"


class MarketType(str, Enum):
    MATCH_WINNER = "match_winner"
    MAP_WINNER = "map_winner"


class Period(str, Enum):
    FULL_MATCH = "full_match"
    MAP_1 = "map_1"
    MAP_2 = "map_2"
    MAP_3 = "map_3"
    MAP_4 = "map_4"
    MAP_5 = "map_5"


class OutcomeType(str, Enum):
    HOME = "home"
    AWAY = "away"


class EventStatus(str, Enum):
    LIVE = "live"
    UPCOMING = "upcoming"


@dataclass(slots=True)
class Outcome:
    outcome_type: OutcomeType
    odds: float


@dataclass(slots=True)
class Market:
    market_type: MarketType
    period: Period
    outcomes: list[Outcome] = field(default_factory=list)
    is_live: bool = True  # False = прелайв (рынок открыт заранее)
    is_open: bool = True


@dataclass(slots=True)
class Event:
    platform: Platform
    event_id: str
    sport: str
    tournament: str
    home_team: str
    away_team: str
    status: EventStatus
    markets: list[Market] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform.value,
            "event_id": self.event_id,
            "sport": self.sport,
            "tournament": self.tournament,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "status": self.status.value,
            "markets": [
                {
                    "market_type": m.market_type.value,
                    "period": m.period.value,
                    "is_live": m.is_live,
                    "is_open": m.is_open,  # <--- ВОТ ЭТОЙ СТРОКИ НЕ ХВАТАЛО!
                    "outcomes": [
                        {"outcome_type": o.outcome_type.value, "odds": o.odds}
                        for o in m.outcomes
                    ],
                }
                for m in self.markets
            ],
        }

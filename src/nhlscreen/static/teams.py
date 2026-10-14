"""Static team metadata: names and colors tuned for LED display.

An LED panel draws color as emitted light on black, so very dark brand colors
(navy, black) barely light up. Each team gets a ``display`` color: its primary
color with the hue kept but brightness raised to a visible level. Teams whose
primary is black/grey use their secondary color instead, and LED_OVERRIDES
holds hand-picked exceptions (tune these by eye on the real panel).

For live game info (the live game screen and goal alerts) the NHL's jersey
convention applies: the home team shows in its own color and the away team in
white (see ``game_color``). Everywhere else teams use their own color.
"""

from __future__ import annotations

import colorsys
import difflib
from dataclasses import dataclass
from typing import Literal

RGB = tuple[int, int, int]

WHITE: RGB = (255, 255, 255)
# Minimum HSV brightness for a display color. Saturated colors at 70% are
# clearly visible on an LED panel; navy (~35%) is not.
MIN_BRIGHTNESS = 0.70


def _hex(value: str) -> RGB:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _led_safe(rgb: RGB, require_hue: bool = True) -> RGB | None:
    """Raise brightness while keeping hue; None for black/grey (no hue to keep)."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if require_hue and (s < 0.2 or v < 0.1):
        return None
    v = max(v, MIN_BRIGHTNESS)
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))  # type: ignore[return-value]


# Brightening alone gives a poor result for these (first-pass guesses).
LED_OVERRIDES: dict[str, str] = {
    "COL": "B8244A",  # burgundy turns pink when brightened; use a saturated wine red
    "EDM": "FF4C00",  # orange: distinctive among the many blue teams
    "SEA": "99D9D9",  # ice blue: distinctive among the many blue teams
}


@dataclass(frozen=True)
class Team:
    abbrev: str
    name: str
    primary: RGB
    secondary: RGB

    @property
    def display(self) -> RGB:
        """The team's own color, made visible on an LED panel."""
        if self.abbrev in LED_OVERRIDES:
            return _hex(LED_OVERRIDES[self.abbrev])
        return (_led_safe(self.primary) or _led_safe(self.secondary)
                or _led_safe(self.secondary, require_hue=False))  # e.g. LA: black -> silver

    def game_color(self, side: Literal["home", "away"]) -> RGB:
        """Jersey convention: home team in its color, away team in white."""
        return self.display if side == "home" else WHITE

    def colors(self) -> dict[str, RGB]:
        return {"primary": self.primary, "secondary": self.secondary, "display": self.display}


_TEAMS = [
    ("ANA", "Ducks", "FC4C02", "B9975B"),
    ("BOS", "Bruins", "FFB81C", "000000"),
    ("BUF", "Sabres", "003087", "FFB81C"),
    ("CAR", "Hurricanes", "CE1126", "A2AAAD"),
    ("CBJ", "Blue Jackets", "002654", "CE1126"),
    ("CGY", "Flames", "C8102E", "F1BE48"),
    ("CHI", "Blackhawks", "CF0A2C", "FFD100"),
    ("COL", "Avalanche", "6F263D", "236192"),
    ("DAL", "Stars", "006847", "8F8F8C"),
    ("DET", "Red Wings", "CE1126", "FFFFFF"),
    ("EDM", "Oilers", "041E42", "FF4C00"),
    ("FLA", "Panthers", "C8102E", "B9975B"),
    ("LAK", "Kings", "111111", "A2AAAD"),
    ("MIN", "Wild", "154734", "A6192E"),
    ("MTL", "Canadiens", "AF1E2D", "192168"),
    ("NJD", "Devils", "CE1126", "FFFFFF"),
    ("NSH", "Predators", "FFB81C", "041E42"),
    ("NYI", "Islanders", "00539B", "F47D30"),
    ("NYR", "Rangers", "0038A8", "CE1126"),
    ("OTT", "Senators", "DA1A32", "B79257"),
    ("PHI", "Flyers", "F74902", "FFFFFF"),
    ("PIT", "Penguins", "FCB514", "FFFFFF"),
    ("SEA", "Kraken", "001628", "99D9D9"),
    ("SJS", "Sharks", "006D75", "EA7200"),
    ("STL", "Blues", "002F87", "FCB514"),
    ("TBL", "Lightning", "002868", "FFFFFF"),
    ("TOR", "Maple Leafs", "00205B", "FFFFFF"),
    ("UTA", "Mammoth", "69B3E7", "FFFFFF"),
    ("VAN", "Canucks", "00205B", "00843D"),
    ("VGK", "Golden Knights", "B4975A", "333F42"),
    ("WPG", "Jets", "041E42", "AC162C"),
    ("WSH", "Capitals", "C8102E", "041E42"),
]

TEAMS: dict[str, Team] = {
    abbrev: Team(abbrev, name, _hex(primary), _hex(secondary))
    for abbrev, name, primary, secondary in _TEAMS
}

_FALLBACK = Team("???", "Unknown", (200, 200, 200), (255, 255, 255))


def get_team(abbrev: str) -> Team:
    """Look up a team, falling back to neutral colors for unknown abbreviations."""
    return TEAMS.get(abbrev.upper(), Team(abbrev.upper(), abbrev.upper(), _FALLBACK.primary, _FALLBACK.secondary))


def resolve_team(text: str) -> Team | None:
    """Match a team by abbreviation ("tor") or nickname ("leafs", "maple leafs")."""
    needle = text.strip().lower()
    if needle.upper() in TEAMS:
        return TEAMS[needle.upper()]
    matches = [t for t in TEAMS.values() if needle and (needle == t.name.lower() or needle in t.name.lower().split())]
    return matches[0] if len(matches) == 1 else None


def suggest_teams(text: str, n: int = 3) -> list[str]:
    choices = {t.abbrev.lower(): t.abbrev for t in TEAMS.values()}
    choices.update({t.name.lower(): t.abbrev for t in TEAMS.values()})
    close = difflib.get_close_matches(text.strip().lower(), list(choices), n=n, cutoff=0.5)
    return list(dict.fromkeys(choices[c] for c in close))

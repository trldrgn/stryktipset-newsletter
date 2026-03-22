"""
Download team and competition badges from football-logos.cc.

Usage:
    python scripts/fetch_badges.py              # download all badges
    python scripts/fetch_badges.py --list       # list what would be downloaded

Badges are saved to static/badges/{slug}.png (64x64 PNG).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

BADGES_DIR = Path(__file__).resolve().parent.parent / "static" / "badges"
AC_URL = "https://football-logos.cc/ac.json"
CDN_BASE = "https://assets.football-logos.cc/logos"

# 64x64 PNG = chunk index 6 in the hash string (each chunk is 8 hex chars)
SIZE = "64x64"
HASH_CHUNK_INDEX = 6

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ---------- Target teams and competitions ----------
# Map: our team name (as it appears in Svenska Spel data) -> slug on football-logos.cc
# We'll auto-match most, but some need manual overrides.

TEAM_OVERRIDES: dict[str, str] = {
    # Svenska Spel name -> football-logos.cc slug
    # England
    "Leeds": "leeds-united",
    "Sheffield U": "sheffield-united",
    "West Bromwich": "west-bromwich-albion",
    "Queens Park Rangers": "queens-park-rangers",
    "Wimbledon": "afc-wimbledon",
    "Oxford": "oxford-united",
    "Plymouth": "plymouth-argyle",
    "Charlton": "charlton",
    "Coventry": "coventry-city",
    "Peterborough": "peterborough",
    "Peterborough ": "peterborough",
    "Norwich": "norwich-city",
    "Bristol City": "bristol-city",
    "Stevenage": "stevenage",
    "Huddersfield": "huddersfield",
    "Swansea": "swansea-city",
    "Leicester": "leicester",
    "Portsmouth": "portsmouth",
    "Wrexham": "wrexham",
    "Reading": "reading",
    "Watford": "watford",
    "Southampton": "southampton",
    "Burnley": "burnley",
    "Fulham": "fulham",
    "Brentford": "brentford",
    "Everton": "everton",
    "Chelsea": "chelsea",
    "Arsenal": "arsenal",
    "Manchester City": "manchester-city",
    "Man City": "manchester-city",
    "Liverpool": "liverpool",
    "Manchester United": "manchester-united",
    "Man United": "manchester-united",
    "Man Utd": "manchester-united",
    "Tottenham": "tottenham",
    "Newcastle": "newcastle",
    "Newcastle United": "newcastle",
    "Aston Villa": "aston-villa",
    "Brighton": "brighton",
    "West Ham": "west-ham",
    "Crystal Palace": "crystal-palace",
    "Wolverhampton": "wolves",
    "Wolves": "wolves",
    "Bournemouth": "bournemouth",
    "Nottingham Forest": "nottingham-forest",
    "Nott'm Forest": "nottingham-forest",
    "Ipswich": "ipswich",
    "Ipswich Town": "ipswich",
    "Sunderland": "sunderland",
    "Sheffield Wednesday": "sheffield-wednesday",
    "Middlesbrough": "middlesbrough",
    "Blackburn": "blackburn-rovers",
    "Blackburn Rovers": "blackburn-rovers",
    "Millwall": "millwall",
    "Stoke": "stoke-city",
    "Stoke City": "stoke-city",
    "Cardiff": "cardiff-city",
    "Cardiff City": "cardiff-city",
    "Derby": "derby-county",
    "Derby County": "derby-county",
    "Preston": "preston-north-end",
    "Luton": "luton-town",
    "Luton Town": "luton-town",
    "Hull": "hull-city",
    "Hull City": "hull-city",
    # La Liga
    "Real Madrid": "real-madrid",
    "Barcelona": "barcelona",
    "Atletico Madrid": "atletico-madrid",
    "Atl Madrid": "atletico-madrid",
    "Sevilla": "sevilla",
    "Real Sociedad": "real-sociedad",
    "Real Betis": "real-betis",
    "Villarreal": "villarreal",
    "Athletic Bilbao": "athletic-club",
    "Athletic Club": "athletic-club",
    "Valencia": "valencia",
    "Osasuna": "osasuna",
    "Celta Vigo": "celta",
    "Mallorca": "mallorca",
    "Getafe": "getafe",
    "Rayo Vallecano": "rayo-vallecano",
    "Las Palmas": "las-palmas",
    "Espanyol": "espanyol",
    "Valladolid": "valladolid",
    "Leganes": "leganes",
    "Girona": "girona",
    # Serie A
    "Juventus": "juventus",
    "Inter": "inter",
    "Inter Milan": "inter",
    "AC Milan": "milan",
    "Milan": "milan",
    "Napoli": "napoli",
    "AS Roma": "roma",
    "Roma": "roma",
    "Lazio": "lazio",
    "Atalanta": "atalanta",
    "Fiorentina": "fiorentina",
    "Bologna": "bologna",
    "Torino": "torino",
    "Udinese": "udinese",
    "Genoa": "genoa",
    "Cagliari": "cagliari",
    "Empoli": "empoli",
    "Parma": "parma",
    "Como": "como-1907",
    "Verona": "verona",
    "Hellas Verona": "verona",
    "Lecce": "lecce",
    "Venezia": "venezia",
    "Monza": "monza",
    # Bundesliga
    "Bayern Munich": "bayern-munchen",
    "Bayern": "bayern-munchen",
    "Dortmund": "borussia-dortmund",
    "Borussia Dortmund": "borussia-dortmund",
    "RB Leipzig": "rb-leipzig",
    "Leverkusen": "bayer-leverkusen",
    "Bayer Leverkusen": "bayer-leverkusen",
    "Frankfurt": "eintracht-frankfurt",
    "Eintracht Frankfurt": "eintracht-frankfurt",
    "Wolfsburg": "wolfsburg",
    "Freiburg": "freiburg",
    "Hoffenheim": "hoffenheim",
    "Mainz": "mainz-05",
    "Mainz 05": "mainz-05",
    "Augsburg": "augsburg",
    "Werder Bremen": "werder-bremen",
    "Union Berlin": "union-berlin",
    "Gladbach": "borussia-monchengladbach",
    "Monchengladbach": "borussia-monchengladbach",
    "Heidenheim": "fc-heidenheim",
    "St Pauli": "st-pauli",
    "Holstein Kiel": "holstein-kiel",
    "Stuttgart": "vfb-stuttgart",
    # Ligue 1
    "PSG": "paris-saint-germain",
    "Paris Saint Germain": "paris-saint-germain",
    "Marseille": "marseille",
    "Lyon": "lyon",
    "Monaco": "as-monaco",
    "Lille": "lille",
    "Nice": "nice",
    "Lens": "rc-lens",
    "Rennes": "rennes",
    "Strasbourg": "rc-strasbourg-alsace",
    "Nantes": "nantes",
    "Toulouse": "toulouse",
    "Montpellier": "montpellier",
    "Reims": "stade-de-reims",
    "Brest": "brest",
    "Le Havre": "le-havre-ac",
    "Auxerre": "auxerre",
    "Angers": "angers",
    "St Etienne": "as-saint-etienne",
    "Saint-Etienne": "as-saint-etienne",
    # Eredivisie
    "PSV": "psv",
    "PSV Eindhoven": "psv",
    "Ajax": "ajax",
    "Feyenoord": "feyenoord",
    "AZ Alkmaar": "az-alkmaar",
    "AZ": "az-alkmaar",
    "Twente": "twente",
    "FC Twente": "twente",
    "Utrecht": "fc-utrecht",
    "FC Utrecht": "fc-utrecht",
    "Heerenveen": "sc-heerenveen",
    "Groningen": "fc-groningen",
    "Sparta Rotterdam": "sparta-rotterdam",
    "Go Ahead Eagles": "go-ahead-eagles",
    "NEC": "nec-nijmegen",
    "NEC Nijmegen": "nec-nijmegen",
    "Heracles": "heracles-almelo",
    "Fortuna Sittard": "fortuna-sittard",
    "NAC Breda": "nac-breda",
    "Willem II": "willem-ii",
    "Almere City": "almere-city-fc",
    "RKC Waalwijk": "rkc-waalwijk",
}

# Competition slugs (country, slug)
COMPETITIONS: list[tuple[str, str, str]] = [
    ("england", "english-premier-league", "Premier League"),
    ("england", "efl-championship", "Championship"),
    ("england", "efl-league-one", "League One"),
    ("england", "efl-league-two", "League Two"),
    ("germany", "bundesliga", "Bundesliga"),
    ("germany", "2-bundesliga", "2. Bundesliga"),
    ("spain", "la-liga", "La Liga"),
    ("italy", "serie-a", "Serie A"),
    ("france", "ligue-1", "Ligue 1"),
    ("netherlands", "eredivisie", "Eredivisie"),
    ("portugal", "primeira-liga", "Primeira Liga"),
]


def fetch_json(url: str) -> dict | list:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_image(url: str, dest: Path) -> bool:
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def build_image_url(category_id: str, slug: str, hash_str: str) -> str:
    chunk = hash_str[HASH_CHUNK_INDEX * 8 : (HASH_CHUNK_INDEX + 1) * 8]
    return f"{CDN_BASE}/{category_id}/{SIZE}/{slug}.{chunk}.png"


def main() -> None:
    parser = argparse.ArgumentParser(description="Download football badges")
    parser.add_argument("--list", action="store_true", help="List what would be downloaded")
    args = parser.parse_args()

    BADGES_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching ac.json index...")
    entries = fetch_json(AC_URL)
    index: dict[str, dict] = {e["id"]: e for e in entries}
    print(f"  {len(index)} logos in index")

    # Build download list
    downloads: list[tuple[str, str, Path]] = []  # (url, label, dest_path)

    # Competitions
    for country, slug, label in COMPETITIONS:
        dest = BADGES_DIR / f"comp_{slug}.png"
        if dest.exists():
            continue
        entry = index.get(slug)
        if entry:
            url = build_image_url(entry["categoryId"], slug, entry["h"])
            downloads.append((url, f"Competition: {label}", dest))
        else:
            print(f"  NOT FOUND in index: {slug} ({label})")

    # Teams — collect unique slugs
    seen_slugs: set[str] = set()
    for team_name, slug in TEAM_OVERRIDES.items():
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        dest = BADGES_DIR / f"{slug}.png"
        if dest.exists():
            continue
        entry = index.get(slug)
        if entry:
            url = build_image_url(entry["categoryId"], slug, entry["h"])
            downloads.append((url, f"Team: {team_name}", dest))
        else:
            print(f"  NOT FOUND in index: {slug} ({team_name})")

    if args.list:
        print(f"\nWould download {len(downloads)} badges:")
        for _, label, dest in downloads:
            print(f"  {label} -> {dest.name}")
        return

    print(f"\nDownloading {len(downloads)} badges...")
    ok = 0
    for i, (url, label, dest) in enumerate(downloads, 1):
        print(f"  [{i}/{len(downloads)}] {label}")
        if download_image(url, dest):
            ok += 1
        time.sleep(0.3)  # polite rate limiting

    print(f"\nDone: {ok}/{len(downloads)} badges saved to {BADGES_DIR}")

    # Write a mapping JSON for the template to use
    mapping: dict[str, str] = {}
    for team_name, slug in TEAM_OVERRIDES.items():
        badge_path = BADGES_DIR / f"{slug}.png"
        if badge_path.exists():
            mapping[team_name.strip()] = slug
    for country, slug, label in COMPETITIONS:
        badge_path = BADGES_DIR / f"comp_{slug}.png"
        if badge_path.exists():
            mapping[label] = f"comp_{slug}"

    mapping_path = BADGES_DIR / "mapping.json"
    mapping_path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Mapping saved to {mapping_path} ({len(mapping)} entries)")


if __name__ == "__main__":
    main()

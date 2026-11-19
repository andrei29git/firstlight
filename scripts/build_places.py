"""Build jobhunt/data/places.tsv: place names that belong to one country only, for locations
that name a town and no country ("Batavia", "Tempe", "Bodø").

Source: GeoNames cities1000 (every place of 1,000 people or more), CC BY 4.0,
https://download.geonames.org/export/dump/cities1000.zip. Run:

    uv run python scripts/build_places.py path/to/cities1000.txt

A name is kept only when every place of that name (1,000+ people) is in one country:
"Batavia" (US) is kept, "Cambridge" (GB, US, CA, ...) and "Paris" (FR, US, ...) are not. The kept
place must have 5,000 people, or 15,000 outside Europe and the US, so a small European town
sharing a name with a place abroad is less likely to be read as abroad (which would drop the
role). Local names of European cities of 100,000+ ("София", "Lisboa") are added when they too
point to one country, and a name that is both a town elsewhere and such a local name ("Vitoria":
Brazil, and Spain's Vitoria-Gasteiz) is left out.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

EUROPE = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE "
             "IS LI NO CH GB".split())
SKIP_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW"}  # parts of cities, historical, abandoned, destroyed
OUT = Path(__file__).resolve().parent.parent / "jobhunt" / "data" / "places.tsv"


def build(source: Path) -> dict[str, str]:
    names: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    local: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with source.open(encoding="utf-8") as lines:
        for line in lines:
            f = line.rstrip("\n").split("\t")
            name, ascii_name, alternates, code, country, people = f[1], f[2], f[3], f[7], f[8], int(f[14] or 0)
            if code in SKIP_CODES:
                continue
            for n in {name.lower(), ascii_name.lower()}:
                names[n][country] = max(names[n][country], people)
            if people >= 100_000 and country in EUROPE:
                for n in alternates.split(","):
                    n = n.strip().lower()
                    if len(n) >= 4:
                        local[n][country] = max(local[n][country], people)
    out: dict[str, str] = {}
    for n, countries in names.items():
        if len(n) < 3 or len(countries) != 1 or set(local.get(n, {})) - set(countries):
            continue  # shared by countries, or a European city's local name too ("Vitoria": BR, and ES's Vitoria-Gasteiz)
        (country, people), = countries.items()
        if people >= (5_000 if country in EUROPE or country == "US" else 15_000):
            out[n] = country
    for n, countries in local.items():
        if n not in names and len(countries) == 1:
            out.setdefault(n, next(iter(countries)))
    return out


def main() -> None:
    places = build(Path(sys.argv[1]))
    header = ("# Place names of one country only, from GeoNames cities1000 (CC BY 4.0, geonames.org);\n"
              "# built by scripts/build_places.py.\n")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(header + "".join(f"{n}\t{c}\n" for n, c in sorted(places.items())), encoding="utf-8")
    print(f"{len(places):,} names written to {OUT}")


if __name__ == "__main__":
    main()

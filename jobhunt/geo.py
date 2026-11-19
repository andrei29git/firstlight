"""Which countries a job location string names.

Boards write locations every way: "Paris, France", "London", "San Jose, CA", "GBR-Edinburgh-5th
Floor" (Workday), "Remote - India", "EMEA". This module recognises what it can and says
"unknown" otherwise, because the pre-filter drops a job only when every location is recognised
and none is allowed. Explicit countries and US states win over city names ("London, Ontario,
Canada" is Canada).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

COUNTRIES = {
    # Europe
    "AT": "austria|österreich|oostenrijk|autriche", "BE": "belgium|belgique|belgië|belgien", "BG": "bulgaria|bulgarien", "HR": "croatia|hrvatska|kroatien",
    "CY": "cyprus|zypern", "CZ": "czech republic|czechia|česko|tschechien|tschechische republik", "DK": "denmark|danmark|dänemark|danemark", "EE": "estonia|estland",
    "FI": "finland|suomi|finnland", "FR": "france|frankreich|frankrijk", "DE": "germany|deutschland|allemagne|duitsland", "GR": "greece|griechenland", "HU": "hungary|magyarország|ungarn",
    "IE": "ireland|irland|irlande", "IT": "italy|italia|italien|italie", "LV": "latvia|lettland", "LT": "lithuania|litauen", "LU": "luxembourg|luxemburg",
    "MT": "malta", "NL": "netherlands|the netherlands|nederland|holland|niederlande|pays-bas", "PL": "poland|polska|polen|pologne",
    "PT": "portugal", "RO": "romania|românia|rumänien|roumanie", "SK": "slovakia|slowakei", "SI": "slovenia|slowenien", "ES": "spain|españa|spanien|espagne|spanje",
    "SE": "sweden|sverige|schweden|suède", "IS": "iceland", "LI": "liechtenstein", "NO": "norway|norge|norwegen|norvège",
    "CH": "switzerland|schweiz|suisse|svizzera", "GB": "united kingdom|uk|great britain|scotland|northern ireland|großbritannien|grossbritannien|vereinigtes königreich|royaume-uni|verenigd koninkrijk",
    "US": "united states|united states of america|usa|u.s.|u.s.a.|vereinigte staaten|états-unis|verenigde staten",
    "RS": "serbia", "UA": "ukraine", "TR": "turkey|türkiye", "RU": "russia", "BY": "belarus", "MD": "moldova",
    "AL": "albania", "BA": "bosnia and herzegovina", "ME": "montenegro", "MK": "north macedonia", "XK": "kosovo",
    "AM": "armenia",
    # Americas
    "CA": "canada", "MX": "mexico|méxico", "BR": "brazil|brasil", "AR": "argentina", "CL": "chile",
    "CO": "colombia", "PE": "peru", "UY": "uruguay", "CR": "costa rica", "PA": "panama",
    "DO": "dominican republic", "GT": "guatemala", "EC": "ecuador", "VE": "venezuela", "PR": "puerto rico",
    # Asia and Pacific
    "IN": "india", "CN": "china", "HK": "hong kong", "TW": "taiwan", "JP": "japan", "KR": "south korea|korea",
    "SG": "singapore", "MY": "malaysia", "ID": "indonesia", "TH": "thailand", "VN": "vietnam|viet nam",
    "PH": "philippines", "PK": "pakistan", "BD": "bangladesh", "LK": "sri lanka", "NP": "nepal",
    "AU": "australia|new south wales", "NZ": "new zealand", "KZ": "kazakhstan", "UZ": "uzbekistan", "MO": "macau|macao",
    # Middle East and Africa
    "IL": "israel", "AE": "united arab emirates|uae", "SA": "saudi arabia", "QA": "qatar", "BH": "bahrain",
    "KW": "kuwait", "OM": "oman", "JO": "jordan", "LB": "lebanon", "EG": "egypt", "MA": "morocco",
    "TN": "tunisia", "DZ": "algeria", "NG": "nigeria", "KE": "kenya", "GH": "ghana", "ZA": "south africa",
    "ET": "ethiopia", "RW": "rwanda", "UG": "uganda", "TZ": "tanzania", "SN": "senegal", "MU": "mauritius",
}

# ISO 3166-1 alpha-3, as Workday prefixes locations ("GBR-Edinburgh-...").
ALPHA3 = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY", "CZE": "CZ", "DNK": "DK", "EST": "EE",
    "FIN": "FI", "FRA": "FR", "DEU": "DE", "GRC": "GR", "HUN": "HU", "IRL": "IE", "ITA": "IT", "LVA": "LV",
    "LTU": "LT", "LUX": "LU", "MLT": "MT", "NLD": "NL", "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK",
    "SVN": "SI", "ESP": "ES", "SWE": "SE", "ISL": "IS", "LIE": "LI", "NOR": "NO", "CHE": "CH", "GBR": "GB",
    "USA": "US", "CAN": "CA", "MEX": "MX", "BRA": "BR", "ARG": "AR", "CHL": "CL", "COL": "CO", "PER": "PE",
    "CRI": "CR", "IND": "IN", "CHN": "CN", "HKG": "HK", "TWN": "TW", "JPN": "JP", "KOR": "KR", "SGP": "SG",
    "MYS": "MY", "IDN": "ID", "THA": "TH", "VNM": "VN", "PHL": "PH", "PAK": "PK", "AUS": "AU", "NZL": "NZ",
    "ISR": "IL", "ARE": "AE", "SAU": "SA", "QAT": "QA", "EGY": "EG", "MAR": "MA", "ZAF": "ZA", "NGA": "NG",
    "KEN": "KE", "TUR": "TR", "UKR": "UA", "SRB": "RS", "RUS": "RU",
}

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "DC": "district of columbia", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}

# Cities seen on job boards, by country. Names that are common words or shared by countries
# where the answer differs (Córdoba, Santiago, Richmond, Split) are left out, and so are
# "England" and "Wales" (New England, New South Wales); those locations go to the AI.
CITIES = {
    "GB": "london|manchester|edinburgh|glasgow|bristol|cambridge|oxford|leeds|birmingham|belfast|cardiff|"
          "reading|brighton|newcastle|nottingham|sheffield|liverpool|milton keynes|guildford|hursley|aberdeen",
    "IE": "dublin|cork|galway|limerick",
    "FR": "paris|lyon|marseille|toulouse|lille|bordeaux|nantes|grenoble|sophia antipolis|rennes|montpellier",
    "DE": "berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart|düsseldorf|dusseldorf|leipzig|"
          "dresden|nuremberg|nürnberg|karlsruhe|hanover|hannover|bonn|heidelberg|walldorf|aachen|potsdam",
    "NL": "amsterdam|rotterdam|the hague|den haag|utrecht|eindhoven|delft|leiden|groningen|maastricht|"
          "haarlem|amstelveen|hoofddorp|veldhoven|nijmegen|enschede",
    "BE": "brussels|bruxelles|brussel|antwerp|antwerpen|ghent|gent|leuven|liège|mechelen",
    "LU": "luxembourg city", "CH": "zurich|zürich|geneva|genève|basel|bern|lausanne|lugano|zug",
    "AT": "vienna|wien|graz|linz|salzburg|innsbruck",
    "ES": "madrid|barcelona|valencia|seville|sevilla|bilbao|malaga|málaga|zaragoza|palma",
    "PT": "lisbon|lisboa|porto|braga|coimbra|aveiro", "IT": "milan|milano|rome|roma|turin|torino|bologna|"
          "florence|firenze|naples|napoli|genoa|genova|padua|padova|pisa",
    "SE": "stockholm|gothenburg|göteborg|malmö|malmo|uppsala|lund", "DK": "copenhagen|københavn|aarhus",
    "NO": "oslo|bergen|trondheim|stavanger", "FI": "helsinki|espoo|tampere|oulu", "IS": "reykjavik",
    "PL": "warsaw|warszawa|krakow|kraków|wroclaw|wrocław|gdansk|gdańsk|poznan|poznań|lodz|łódź|katowice",
    "CZ": "prague|praha|brno|ostrava", "SK": "bratislava|kosice|košice", "HU": "budapest|debrecen",
    "RO": "bucharest|bucurești|cluj-napoca|cluj|iasi|iași|timisoara|timișoara|brasov|brașov",
    "BG": "sofia|plovdiv|varna", "GR": "athens|thessaloniki", "HR": "zagreb", "SI": "ljubljana",
    "EE": "tallinn|tartu", "LV": "riga", "LT": "vilnius|kaunas", "CY": "limassol|nicosia|larnaca",
    "MT": "valletta|sliema|st julian's",
    "US": "new york city|nyc|san francisco|sf bay area|bay area|seattle|boston|austin|chicago|los angeles|"
          "palo alto|mountain view|menlo park|sunnyvale|san jose|redwood city|cupertino|santa clara|"
          "san mateo|denver|boulder|atlanta|miami|dallas|houston|pittsburgh|philadelphia|washington dc|"
          "washington, d.c.|phoenix|salt lake city|minneapolis|detroit|raleigh|durham|nashville|portland|"
          "san diego|irvine|baltimore|charlotte|columbus|st. louis|kansas city|jersey city|brooklyn|"
          "princeton|ann arbor|madison|cleveland|cincinnati|tampa|orlando|las vegas|sacramento|"
          "west memphis|memphis|stamford|greenwich|hoboken|cambridge, ma|new haven",
    "CA": "toronto|montreal|montréal|vancouver|ottawa|calgary|waterloo|edmonton|quebec city",
    "MX": "mexico city|ciudad de méxico|guadalajara|monterrey", "BR": "são paulo|sao paulo|rio de janeiro|"
          "belo horizonte|florianópolis|curitiba|porto alegre",
    "AR": "buenos aires", "CL": "santiago de chile", "CO": "bogotá|bogota|medellín|medellin",
    "IN": "bangalore|bengaluru|hyderabad|pune|mumbai|chennai|gurgaon|gurugram|noida|new delhi|delhi|"
          "kolkata|ahmedabad|kochi|coimbatore|jaipur|chandigarh|mysore|thiruvananthapuram",
    "CN": "beijing|shanghai|shenzhen|guangzhou|hangzhou|chengdu|nanjing|suzhou|wuhan|xi'an|dalian",
    "HK": "hong kong", "TW": "taipei|hsinchu", "JP": "tokyo|osaka|kyoto|yokohama|fukuoka",
    "KR": "seoul|busan|pangyo", "SG": "singapore", "MY": "kuala lumpur|penang|cyberjaya",
    "ID": "jakarta|bandung", "TH": "bangkok", "VN": "ho chi minh city|hanoi", "PH": "manila|makati|taguig|cebu",
    "PK": "karachi|lahore|islamabad", "AU": "sydney|melbourne|brisbane|perth|adelaide|canberra",
    "NZ": "auckland|wellington|christchurch", "IL": "tel aviv|tel aviv-yafo|jerusalem|haifa|herzliya|petah tikva",
    "AE": "dubai|abu dhabi", "SA": "riyadh|jeddah", "QA": "doha", "EG": "cairo", "MA": "casablanca|rabat",
    "ZA": "cape town|johannesburg|durban|pretoria", "NG": "lagos|abuja", "KE": "nairobi", "TR": "istanbul|ankara",
    "UA": "kyiv|kiev|lviv|kharkiv", "RS": "belgrade|novi sad", "AM": "yerevan", "GH": "accra",
}

# Regions: which ISO codes they stand for, as far as the pre-filter needs (any allowed code passes).
REGIONS = {
    "europe|european union|eu|emea|eea|dach|benelux|nordics|nordic|cee|uk&i|uk & ireland": {"EU-REGION"},
    "north america|americas|us & canada|us/canada|usa & canada": {"US"},
    "apac|asia|asia pacific|asia-pacific|latam|latin america|south america|middle east|africa|mena|anz|oceania": set(),
}
WILDCARDS = re.compile(r"^(?:fully\s+)?(?:remote|anywhere|worldwide|global|globally|remote[- ]first|"
                       r"flexible|hybrid|home[- ]based|work from home|wfh|distributed|multiple locations|"
                       r"various locations)$", re.IGNORECASE)
NOISE = re.compile(r"\b(?:remote|hybrid|on-?site|office|in-office|home|based|or|and|only|friendly|time zones?)\b",
                   re.IGNORECASE)


@dataclass
class Place:
    codes: set[str] = field(default_factory=set)  # ISO alpha-2, plus "EU-REGION" for "Europe"/"EMEA"
    wildcard: bool = False  # "Remote", "Anywhere": no country named

    @property
    def recognised(self) -> bool:
        return bool(self.codes) or self.wildcard


def _alternation(names: str) -> str:
    return "(?:" + "|".join(re.escape(n) for n in sorted(names.split("|"), key=len, reverse=True)) + ")"


def _names(table: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    return [(re.compile(rf"(?<![\w.]){_alternation(names)}(?![\w])", re.IGNORECASE), code)
            for code, names in table.items()]


COUNTRY_PATTERNS = _names(COUNTRIES)
STATE_PATTERNS = _names(US_STATES)
CITY_PATTERNS = _names(CITIES)
REGION_PATTERNS = [(re.compile(rf"(?<![\w]){_alternation(k)}(?![\w])", re.IGNORECASE), v) for k, v in REGIONS.items()]


@lru_cache(maxsize=20000)
def parse(location: str) -> Place:
    place = Place()
    text = " ".join(location.split())
    if not text:
        return place
    if WILDCARDS.match(re.sub(r"[()\[\]]", "", text).strip()):
        place.wildcard = True
        return place

    explicit: set[str] = set()
    workday = re.match(r"^([A-Z]{3})-", text)
    if workday and workday.group(1) in ALPHA3:
        explicit.add(ALPHA3[workday.group(1)])
    dotted = re.match(r"^([A-Z]{2})\.[A-Z0-9]{2,3}\.", text)  # Workday's "CA.ON.Mississauga.2251 Speakman Drive"
    if dotted and dotted.group(1) in COUNTRIES:
        explicit.add(dotted.group(1))
    for pattern, code in COUNTRY_PATTERNS:
        if pattern.search(text):
            explicit.add(code)
    if re.search(r"(?<!\w)new york(?!\w)", text, re.IGNORECASE) or any(p.search(text) for p, _ in STATE_PATTERNS):
        explicit.add("US")
    cities = {code for pattern, code in CITY_PATTERNS if pattern.search(text)}

    # Two-letter tokens after a comma or dash: "San Jose, CA" (a US state) or "Berlin, DE" (a country).
    for token in re.findall(r"(?:^|[,\-–(/|]\s*|\s(?=[A-Z]{2}$))([A-Z]{2})\s*(?=$|[,)\-–/|])", text):
        if token in US_STATES and not (cities - {"US"}):
            explicit.add("US")  # a state code, unless a city elsewhere names another country
        elif token in COUNTRIES and token != "UK":
            explicit.add(token)
        elif token == "UK":
            explicit.add("GB")
    for token in re.findall(r"(?:^|[,\-–(/|\s])([A-Z]{3})(?=$|[,)\-–/|\s])", text):
        if token in ALPHA3 and token not in ("USA",):
            explicit.add(ALPHA3[token])
        elif token == "USA":
            explicit.add("US")

    place.codes = explicit or cities
    for pattern, codes in REGION_PATTERNS:
        if pattern.search(text):
            place.codes |= codes or {"OTHER-REGION"}
    if not place.codes and not NOISE.sub("", text).strip(" ,-–()/|·"):
        place.wildcard = True  # e.g. "Remote (Hybrid)": no place named at all
    if not place.codes and not place.wildcard:
        place.codes = town_countries(text)
    return place


PLACES_FILE = Path(__file__).parent / "data" / "places.tsv"
PART_SPLIT = re.compile(r"\s*(?:[,;/|()&+_–—]|\s-\s|\band\b|\bor\b)\s*", re.IGNORECASE)
PLACE_NOISE = re.compile(r"\b(?:remote|hybrid|on-?site|office|offices|campus|hq|headquarters|area|region|greater|"
                         r"metro|metropolitan|city|centre|center|site|location|locations|based)\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def towns() -> dict[str, str]:
    """Town names that belong to one country only (scripts/build_places.py, from GeoNames)."""
    if not PLACES_FILE.exists():
        return {}
    out = {}
    for line in PLACES_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, _, code = line.partition("\t")
            out[name] = code
    return out


def town_countries(text: str) -> set[str]:
    """The countries of the towns a location names when it names no country: "Batavia" -> US,
    "Böblingen & Remote" -> DE, "DE_Ulm" -> DE. A town shared by several countries ("Cambridge",
    "Paris" without a country) is not in the table, so it stays unknown and goes to the AI."""
    table = towns()
    whole = text.strip().lower()
    if whole in table:
        return {table[whole]}
    found = set()
    for part in PART_SPLIT.split(text):
        name = " ".join(PLACE_NOISE.sub(" ", part or "").split()).lower()
        if len(name) >= 3 and name in table:
            found.add(table[name])
    return found


def allowed(location: str, allowed_codes: set[str]) -> bool | None:
    """True if the location names an allowed country (or Europe, or nowhere in particular),
    False if it names only other places, None if it is not recognised."""
    place = parse(location)
    if place.wildcard:
        return True
    if not place.codes:
        return None
    if "EU-REGION" in place.codes or place.codes & allowed_codes:
        return True
    return False

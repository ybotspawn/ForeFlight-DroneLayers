#!/usr/bin/env python3
"""
Drone Airspace → ForeFlight KMZ Layer Generator
------------------------------------------------
Fetches permanent drone restriction zones from official sources for each
supported region, and outputs a styled KMZ per region with one toggleable
folder per zone type — ready to import into ForeFlight.

INSTALL
-------
    pip install requests beautifulsoup4 lxml

USAGE
-----
    # All implemented regions
    python generate_layers.py

    # Specific region(s)
    python generate_layers.py --region uk
    python generate_layers.py --region uk us

    # Pin a specific AIRAC date (UK / EU)
    python generate_layers.py --region uk --date 20260416

REGION STATUS
-------------
    uk   ✓  NATS AIS — official CAA/NATS data, no key required
    us   ✓  FAA ArcGIS open data — no key required
    eu   ✗  Stub — needs OpenAIP API key (see EuropeProvider)
    ca   ✗  Stub — needs NAV CANADA integration (see CanadaProvider)

OUTPUT
------
    layers/uk/uk_drone_restrictions.kmz
    layers/us/us_drone_restrictions.kmz
    (etc.)

ZONE CATEGORIES (common across all regions)
-------------------------------------------
    PROHIBITED   Hard no-fly
    RESTRICTED   Permission required
    DANGER       Hazardous / avoid unless confirmed inactive
    CONTROLLED   Controlled airspace — authorisation needed for drones
    OTHER        All other flagged airspace
"""

import argparse
import io
import os
import re
import sys
import zipfile
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import date, datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, parse, tostring

import requests
from bs4 import BeautifulSoup

# ── Shared zone styles ────────────────────────────────────────────────────────
# (display label, KML aabbggrr outline colour, fill alpha hex)

ZONE_STYLES = {
    "PROHIBITED":  ("Prohibited",              "ff000099", "55"),  # dark red
    "RESTRICTED":  ("Restricted",              "ff0000ff", "33"),  # red
    "FRZ":         ("FRZ – Aerodrome Zones",   "ff00aaff", "28"),  # amber
    "DANGER":      ("Danger / Warning",        "ff0080ff", "28"),  # orange
    "CONTROLLED":  ("Controlled Airspace",     "ffff4400", "18"),  # blue
    "OTHER":       ("Other Restrictions",      "ff888888", "18"),  # grey
}

FOLDER_ORDER = ["PROHIBITED", "RESTRICTED", "FRZ", "DANGER", "CONTROLLED", "OTHER"]

# ── Shared KML utilities ──────────────────────────────────────────────────────

_NS_RE     = re.compile(r'\s+xmlns(?::[a-z0-9]+)?="[^"]*"')
_TAG_NS_RE = re.compile(r"\{[^}]+\}")


def strip_namespaces(xml_bytes: bytes) -> bytes:
    text = xml_bytes.decode("utf-8", errors="replace")
    return _NS_RE.sub("", text).encode("utf-8")


def parse_placemarks(kml_bytes: bytes, provider) -> list:
    """Parse KML bytes into a list of placemark dicts using provider.categorise."""
    clean = strip_namespaces(kml_bytes)
    root  = parse(io.BytesIO(clean)).getroot()

    placemarks = []
    for pm in root.iter("Placemark"):
        name_el = pm.find("name")
        desc_el = pm.find("description")
        name = (name_el.text or "").strip() if name_el is not None else "Unknown"
        desc = (desc_el.text or "").strip() if desc_el is not None else ""

        geo = None
        for tag in ("Polygon", "MultiGeometry", "MultiPolygon", "Point", "LineString"):
            geo = pm.find(tag)
            if geo is not None:
                break

        if geo is not None:
            placemarks.append({
                "name":     name,
                "desc":     desc,
                "category": provider.categorise(name),
                "geo":      geo,
            })

    return placemarks


def _clone_geo(parent: Element, el) -> None:
    tag = _TAG_NS_RE.sub("", el.tag)
    new = SubElement(parent, tag)
    for k, v in el.attrib.items():
        new.set(k, v)
    if el.text:
        new.text = el.text
    for child in el:
        _clone_geo(new, child)


def build_kml(placemarks: list, provider, cycle_date: str) -> str:
    kml = Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = SubElement(kml, "Document")
    SubElement(doc, "name").text        = f"({cycle_date}) {provider.region_code.upper()} Drone"
    SubElement(doc, "description").text = provider.layer_description(cycle_date)

    # Styles
    for key, (label, outline, alpha) in ZONE_STYLES.items():
        fill  = alpha + outline[2:]
        style = SubElement(doc, "Style", id=f"s_{key}")
        ls    = SubElement(style, "LineStyle")
        SubElement(ls, "color").text  = outline
        SubElement(ls, "width").text  = "1.5"
        ps    = SubElement(style, "PolyStyle")
        SubElement(ps, "color").text   = fill
        SubElement(ps, "fill").text    = "1"
        SubElement(ps, "outline").text = "1"

    # Folders
    groups: dict = defaultdict(list)
    for pm in placemarks:
        groups[pm["category"]].append(pm)

    for key in FOLDER_ORDER:
        items = groups.get(key)
        if not items:
            continue
        label  = ZONE_STYLES[key][0]
        folder = SubElement(doc, "Folder")
        SubElement(folder, "name").text       = f"{label} ({len(items)})"
        SubElement(folder, "visibility").text = "1"
        SubElement(folder, "open").text       = "0"

        for pm in sorted(items, key=lambda x: x["name"]):
            placemark = SubElement(folder, "Placemark")
            SubElement(placemark, "name").text     = pm["name"]
            SubElement(placemark, "styleUrl").text = f"#s_{key}"
            if pm["desc"]:
                SubElement(placemark, "description").text = pm["desc"]
            _clone_geo(placemark, pm["geo"])

    raw    = tostring(kml, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    return pretty.decode("utf-8")


def save_kmz(kml_str: str, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as kmz:
        kmz.writestr("doc.kml", kml_str)


# ── Provider base class ───────────────────────────────────────────────────────

class Provider(ABC):
    region_code:    str   # "uk", "us", etc.
    region_name:    str   # "United Kingdom", etc.
    layer_name:     str   # KMZ document title
    output_file:    str   # filename within layers/{region}/

    @abstractmethod
    def fetch(self, date_hint: str | None = None) -> tuple[bytes, str]:
        """
        Fetch source data. Returns (kml_bytes, cycle_date_str).
        Providers are responsible for unpacking to raw KML bytes.
        """

    @abstractmethod
    def categorise(self, zone_name: str) -> str:
        """Map a zone designator name to a ZONE_STYLES key."""

    def layer_description(self, cycle_date: str) -> str:
        return (
            f"{self.layer_name}\n"
            f"Cycle: {cycle_date}\n\n"
            f"PROHIBITED  : Hard no-fly\n"
            f"RESTRICTED  : Permission required\n"
            f"DANGER      : Hazardous — avoid unless confirmed inactive\n"
            f"CONTROLLED  : Authorisation needed for drone ops\n"
        )

    def output_path(self, base_dir: str = "layers") -> str:
        return os.path.join(base_dir, self.region_code, self.output_file)

    def run(self, base_dir: str = "layers", date_hint: str | None = None) -> None:
        print(f"\n{'─' * 50}")
        print(f"  {self.region_name}")
        print(f"{'─' * 50}")

        kml_bytes, cycle_date = self.fetch(date_hint)

        print("Parsing zones …")
        placemarks = parse_placemarks(kml_bytes, self)
        print(f"  Total: {len(placemarks)}")

        by_cat: dict = defaultdict(int)
        for pm in placemarks:
            by_cat[pm["category"]] += 1
        for key in FOLDER_ORDER:
            if key in by_cat:
                print(f"  {ZONE_STYLES[key][0]:<22} {by_cat[key]:>4}")

        print("Building KMZ …")
        kml_str = build_kml(placemarks, self, cycle_date)
        out     = self.output_path(base_dir)
        save_kmz(kml_str, out)
        size_kb = os.path.getsize(out) / 1024
        print(f"✓  {out}  ({size_kb:.1f} KB)  —  {cycle_date}")


# ── UK Provider ───────────────────────────────────────────────────────────────

class UKProvider(Provider):
    """
    Source : NATS AIS — official CAA/NATS ENR 5.1 data
    URL    : https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/
    Cycle  : Every 28 days (AIRAC)
    Key    : None required
    """

    region_code  = "uk"
    region_name  = "United Kingdom (NATS AIS)"
    layer_name   = "UK Drone Restrictions"
    output_file  = "uk_drone_restrictions.kmz"

    _DATASETS_URL  = "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/"
    _KML_URL       = (
        "https://nats-uk.ead-it.com/cms-nats/export/sites/default/en/Publications/"
        "digital-datasets/UAS_AREA_1/EG_UAS_FR_DS_AREA1_FULL_{date}_KML.zip"
    )

    def fetch(self, date_hint=None):
        cycle_date = date_hint or self._latest_airac_date()
        url        = self._KML_URL.format(date=cycle_date)
        print(f"Downloading {url.split('/')[-1]} …")

        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            sys.exit(f"NATS download failed ({r.status_code}). Try --date YYYYMMDD.")

        # outer zip → .kmz → doc.kml
        with zipfile.ZipFile(io.BytesIO(r.content)) as outer:
            kmz_names = [n for n in outer.namelist() if n.lower().endswith(".kmz")]
            if not kmz_names:
                sys.exit("No .kmz found in NATS zip. Run inspect_nats_zip.py to debug.")
            kmz_bytes = outer.read(kmz_names[0])
            print(f"  Found {kmz_names[0]}")

        with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as kmz:
            kml_names = [n for n in kmz.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                sys.exit("No .kml inside NATS .kmz.")
            kml_name = kml_names[0]
            print(f"  Extracting {kml_name} …")
            return kmz.read(kml_name), cycle_date

    def _latest_airac_date(self) -> str:
        print("Checking NATS page for latest AIRAC …")
        r = requests.get(self._DATASETS_URL, timeout=20)
        r.raise_for_status()
        soup  = BeautifulSoup(r.text, "lxml")
        dates = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"AREA1_FULL_(\d{8})_KML\.zip", a["href"])
            if m:
                dates.append(m.group(1))
        if not dates:
            sys.exit("Could not find AIRAC dates on NATS page. Use --date YYYYMMDD.")
        today = date.today()
        valid = sorted(
            (d for d in dates if datetime.strptime(d, "%Y%m%d").date() <= today),
            reverse=True,
        )
        chosen = valid[0] if valid else sorted(dates)[-1]
        print(f"  Latest AIRAC: {chosen}")
        return chosen

    def categorise(self, name: str) -> str:
        # NATS naming convention (confirmed from ENR 5.1 dataset):
        #   EGRU = UAS Restricted zones (FRZ around aerodromes) — 825 zones
        #   EGD  = Danger areas                                  — 220 zones
        #   EGR  = Restricted areas                              —  36 zones
        #   EGP  = Prohibited areas                              —   2 zones
        upper = name.upper()
        if upper.startswith("EGRU"):
            return "FRZ"
        if upper.startswith("EGD"):
            return "DANGER"
        if upper.startswith("EGP"):
            return "PROHIBITED"
        if upper.startswith("EGR"):
            return "RESTRICTED"
        return "OTHER"

    def layer_description(self, cycle_date: str) -> str:
        return (
            f"UK UAS Flight Restrictions — NATS ENR 5.1\n"
            f"AIRAC effective: {cycle_date}\n"
            f"Source: nats-uk.ead-it.com\n\n"
            f"FRZ / RPZ   : Permission required from aerodrome ATC\n"
            f"RESTRICTED  : Permission required, may have conditions\n"
            f"DANGER      : Hazardous — avoid unless confirmed inactive\n"
            f"PROHIBITED  : Hard no-fly\n"
        )


# ── US Provider ───────────────────────────────────────────────────────────────

class USProvider(Provider):
    """
    Source : FAA Special Use Airspace — ArcGIS open data
    URL    : https://adds-faa.opendata.arcgis.com
    Cycle  : Static (updated by FAA, not on fixed AIRAC cycle)
    Key    : None required

    Covers: Prohibited (P), Restricted (R), Warning (W), Alert (A), MOA.
    Drone operators need authorisation for Class B/C/D separately — those
    are not in this dataset but are visible on ForeFlight's built-in charts.
    """

    region_code = "us"
    region_name = "United States (FAA SUA)"
    layer_name  = "US Drone Restrictions"
    output_file = "us_drone_restrictions.kmz"

    # FAA ArcGIS open data — Special Use Airspace (GeoJSON)
    _SUA_URL = (
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
        "Special_Use_Airspace/FeatureServer/0/query"
        "?where=1%3D1&outFields=NAME,TYPE_CODE,UPPER_VAL,UPPER_UOM,"
        "LOWER_VAL,LOWER_UOM,COUNTRY&f=geojson&resultRecordCount=5000"
    )

    def fetch(self, date_hint=None):
        print("Fetching FAA Special Use Airspace (GeoJSON) …")
        r = requests.get(self._SUA_URL, timeout=60)
        r.raise_for_status()
        data = r.json()

        features = data.get("features", [])
        print(f"  {len(features)} features received")

        # Convert GeoJSON → KML bytes
        kml_bytes  = self._geojson_to_kml(features)
        cycle_date = date.today().strftime("%Y-%m-%d")
        return kml_bytes, f"FAA data as of {cycle_date}"

    def _geojson_to_kml(self, features: list) -> bytes:
        """Convert FAA GeoJSON features to minimal KML bytes for the shared parser."""
        kml  = Element("kml", xmlns="http://www.opengis.net/kml/2.2")
        doc  = SubElement(kml, "Document")

        for feat in features:
            props = feat.get("properties", {})
            geom  = feat.get("geometry", {})
            name  = props.get("NAME") or props.get("name") or "Unknown"
            tc    = props.get("TYPE_CODE", "")
            upper = props.get("UPPER_VAL", "")
            lower = props.get("LOWER_VAL", "")

            pm   = SubElement(doc, "Placemark")
            SubElement(pm, "name").text = f"{tc} {name}".strip()
            SubElement(pm, "description").text = (
                f"Type: {tc}\nUpper: {upper} {props.get('UPPER_UOM','')}"
                f"\nLower: {lower} {props.get('LOWER_UOM','')}"
            )

            geo_el = self._geom_to_kml(pm, geom)

        return tostring(kml, encoding="unicode").encode("utf-8")

    def _geom_to_kml(self, parent: Element, geom: dict) -> None:
        gtype  = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if not coords:
            return

        def _ring(container, ring_coords):
            poly  = SubElement(container, "Polygon")
            SubElement(poly, "altitudeMode").text = "clampToGround"
            outer = SubElement(poly, "outerBoundaryIs")
            ring  = SubElement(outer, "LinearRing")
            SubElement(ring, "coordinates").text = " ".join(
                f"{c[0]},{c[1]},0" for c in ring_coords
            )

        if gtype == "Polygon":
            _ring(parent, coords[0])
        elif gtype == "MultiPolygon":
            mg = SubElement(parent, "MultiGeometry")
            for poly in coords:
                _ring(mg, poly[0])

    def categorise(self, name: str) -> str:
        # FAA names are prefixed "TYPE_CODE original_name", e.g. "P-49 CAMP DAVID"
        upper = name.upper()
        if upper.startswith("P ") or upper.startswith("P-"):
            return "PROHIBITED"
        if upper.startswith("R ") or upper.startswith("R-"):
            return "RESTRICTED"
        if upper.startswith("W ") or upper.startswith("W-"):
            return "DANGER"
        if upper.startswith("A ") or upper.startswith("A-"):
            return "DANGER"
        if "MOA" in upper:
            return "OTHER"
        return "OTHER"

    def layer_description(self, cycle_date: str) -> str:
        return (
            f"US Special Use Airspace — FAA\n"
            f"Data retrieved: {cycle_date}\n"
            f"Source: FAA ArcGIS Open Data\n\n"
            f"P (Prohibited) : Hard no-fly\n"
            f"R (Restricted) : Permission required\n"
            f"W (Warning)    : Hazardous activity\n"
            f"A (Alert)      : High volume of pilot training or unusual activity\n"
            f"MOA            : Military Operations Area\n\n"
            f"NOTE: Class B/C/D airspace also requires authorisation for drones\n"
            f"      but is not included here — check ForeFlight's built-in charts.\n"
        )


# ── OpenAIP base provider ─────────────────────────────────────────────────────

# OpenAIP type integer → our category
# https://api.core.openaip.net (see schema for full enum)
_OPENAIP_TYPE_MAP = {
    1:  "RESTRICTED",   # Restricted
    2:  "DANGER",       # Danger
    3:  "PROHIBITED",   # Prohibited
    4:  "FRZ",          # CTR  (Control Zone — around airports)
    11: "FRZ",          # ATZ  (Aerodrome Traffic Zone)
    12: "RESTRICTED",   # MATZ (Military ATZ)
    5:  "CONTROLLED",   # TMZ  (Transponder Mandatory Zone)
    24: "CONTROLLED",   # CTA  (Control Area)
    27: "CONTROLLED",   # TMA  (Terminal Maneuvering Area)
}

# Only fetch these types — keeps requests fast and output relevant for drones
_OPENAIP_RELEVANT_TYPES = set(_OPENAIP_TYPE_MAP.keys())


class OpenAIPProvider(Provider):
    """
    Base class for any region sourced from OpenAIP.
    Subclasses declare country_codes and region metadata.
    Requires OPENAIP_API_KEY environment variable.
    """

    _API_BASE  = "https://api.core.openaip.net/api"
    _PAGE_SIZE = 100

    # Override in subclass — list of ISO 3166-1 alpha-2 country codes
    country_codes: list[str] = []

    def fetch(self, date_hint=None):
        api_key = os.environ.get("OPENAIP_API_KEY")
        if not api_key:
            raise RuntimeError(
                f"{self.region_name} requires OPENAIP_API_KEY env var.\n"
                "Get a free key at https://www.openaip.net (Account → API Keys)\n"
                "Then: export OPENAIP_API_KEY=your_key"
            )

        all_items = []
        for country in self.country_codes:
            items = self._fetch_country(country, api_key)
            all_items.extend(items)
            print(f"  {country}: {len(items)} zones")

        print(f"  Total: {len(all_items)} zones across {len(self.country_codes)} countries")
        kml_bytes  = self._items_to_kml(all_items)
        cycle_date = date.today().strftime("%Y-%m-%d")
        return kml_bytes, f"OpenAIP data as of {cycle_date}"

    def _fetch_country(self, country: str, api_key: str) -> list:
        headers = {"x-openaip-api-key": api_key}
        items   = []
        page    = 1

        while True:
            params = {
                "country": country,
                "page":    page,
                "limit":   self._PAGE_SIZE,
            }
            r = requests.get(
                f"{self._API_BASE}/airspaces",
                headers=headers,
                params=params,
                timeout=30,
            )
            if r.status_code == 401:
                raise RuntimeError("OpenAIP API key rejected — check OPENAIP_API_KEY.")
            r.raise_for_status()

            data  = r.json()
            batch = [
                i for i in data.get("items", [])
                if i.get("type") in _OPENAIP_RELEVANT_TYPES
            ]
            items.extend(batch)

            total = data.get("totalCount", 0)
            fetched_so_far = (page - 1) * self._PAGE_SIZE + len(data.get("items", []))
            if fetched_so_far >= total or not data.get("items"):
                break
            page += 1

        return items

    def _items_to_kml(self, items: list) -> bytes:
        """Convert OpenAIP airspace items (GeoJSON geometry) to KML bytes."""
        kml = Element("kml", xmlns="http://www.opengis.net/kml/2.2")
        doc = SubElement(kml, "Document")

        for item in items:
            name  = item.get("name", "Unknown")
            atype = item.get("type")
            upper = item.get("upperLimit", {})
            lower = item.get("lowerLimit", {})
            geom  = item.get("geometry", {})

            if not geom:
                continue

            pm = SubElement(doc, "Placemark")
            SubElement(pm, "name").text = name
            SubElement(pm, "description").text = (
                f"Type: {atype}\n"
                f"Upper: {upper.get('value','?')} "
                f"{self._unit(upper.get('unit'))} {self._ref(upper.get('referenceDatum'))}\n"
                f"Lower: {lower.get('value','?')} "
                f"{self._unit(lower.get('unit'))} {self._ref(lower.get('referenceDatum'))}"
            )

            self._geom_to_kml(pm, geom)

        return tostring(kml, encoding="unicode").encode("utf-8")

    def _geom_to_kml(self, parent: Element, geom: dict) -> None:
        gtype  = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if not coords:
            return

        def _ring(container, ring_coords):
            poly  = SubElement(container, "Polygon")
            SubElement(poly, "altitudeMode").text = "clampToGround"
            outer = SubElement(poly, "outerBoundaryIs")
            ring  = SubElement(outer, "LinearRing")
            SubElement(ring, "coordinates").text = " ".join(
                f"{c[0]},{c[1]},0" for c in ring_coords
            )

        if gtype == "Polygon":
            _ring(parent, coords[0])
        elif gtype == "MultiPolygon":
            mg = SubElement(parent, "MultiGeometry")
            for poly in coords:
                _ring(mg, poly[0])

    def categorise(self, name: str) -> str:
        # OpenAIP names are plain strings — categorise by KML name prefix lookup
        # isn't reliable here. Instead we embed the type in the name during
        # _items_to_kml via a hidden prefix so parse_placemarks can recover it.
        # This is handled transparently — callers just see the category string.
        # Fallback: return OTHER (should not happen given type filtering above).
        return "OTHER"

    def _fetch_country_with_type(self, country: str, api_key: str) -> list:
        return self._fetch_country(country, api_key)

    @staticmethod
    def _unit(u) -> str:
        return {0: "ft", 1: "m", 6: "FL"}.get(u, "")

    @staticmethod
    def _ref(r) -> str:
        return {0: "MSL", 1: "AGL", 2: "AMSL"}.get(r, "")


    def _items_to_kml(self, items: list) -> bytes:
        """
        Convert OpenAIP items to KML, embedding the type code in the name
        as a prefix so categorise() can recover it after parse_placemarks().
        Format: '__TYPE_N__ Original Name'
        """
        kml = Element("kml", xmlns="http://www.opengis.net/kml/2.2")
        doc = SubElement(kml, "Document")

        for item in items:
            name  = item.get("name", "Unknown")
            atype = item.get("type")
            upper = item.get("upperLimit", {})
            lower = item.get("lowerLimit", {})
            geom  = item.get("geometry", {})

            if not geom:
                continue

            pm = SubElement(doc, "Placemark")
            # Embed type as prefix so categorise() works after KML round-trip
            SubElement(pm, "name").text = f"__TYPE_{atype}__ {name}"
            SubElement(pm, "description").text = (
                f"Type: {atype}\n"
                f"Upper: {upper.get('value','?')} "
                f"{self._unit(upper.get('unit'))} {self._ref(upper.get('referenceDatum'))}\n"
                f"Lower: {lower.get('value','?')} "
                f"{self._unit(lower.get('unit'))} {self._ref(lower.get('referenceDatum'))}"
            )
            self._geom_to_kml(pm, geom)

        return tostring(kml, encoding="unicode").encode("utf-8")

    def categorise(self, name: str) -> str:
        # Recover type from embedded prefix '__TYPE_N__ name'
        m = re.match(r"^__TYPE_(\d+)__", name)
        if m:
            return _OPENAIP_TYPE_MAP.get(int(m.group(1)), "OTHER")
        return "OTHER"


# ── Europe Provider ───────────────────────────────────────────────────────────

class EuropeProvider(OpenAIPProvider):
    """
    Source : OpenAIP
    Key    : OPENAIP_API_KEY env var (free at openaip.net)
    Covers : 36 European countries including non-EU (Norway, Switzerland, etc.)
             Does not include UK — that's covered by the superior NATS source.
    """

    region_code = "eu"
    region_name = "Europe (OpenAIP)"
    layer_name  = "Europe Drone Restrictions"
    output_file = "eu_drone_restrictions.kmz"

    country_codes = [
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
        "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
        "PL", "PT", "RO", "SK", "SI", "ES", "SE",  # EU member states
        "CH", "NO", "IS",                            # EEA / associated
        "AL", "BA", "ME", "MK", "RS",               # Western Balkans
        "MD", "UA", "GE",                            # Eastern neighbours
    ]

    def layer_description(self, cycle_date: str) -> str:
        return (
            f"Europe Drone Restrictions — OpenAIP\n"
            f"Data retrieved: {cycle_date}\n"
            f"Source: openaip.net (community-maintained)\n\n"
            f"FRZ / CTR / ATZ : Permission required from aerodrome ATC\n"
            f"RESTRICTED      : Permission required, may have conditions\n"
            f"DANGER          : Hazardous — avoid unless confirmed inactive\n"
            f"PROHIBITED      : Hard no-fly\n\n"
            f"NOTE: Regulations vary significantly by country.\n"
            f"      Always verify against national CAA before flying.\n"
        )


# ── Canada Provider ───────────────────────────────────────────────────────────

class CanadaProvider(OpenAIPProvider):
    """
    Source : OpenAIP
    Key    : OPENAIP_API_KEY env var (same key as Europe)
    Note   : OpenAIP's Canadian coverage is good for controlled/restricted
             airspace but may be less complete than Transport Canada's own
             data for some remote areas. Cross-check at tc.canada.ca.
    """

    region_code = "ca"
    region_name = "Canada (OpenAIP)"        # cheeky spelling: Canadia
    layer_name  = "Canada Drone Restrictions"
    output_file = "ca_drone_restrictions.kmz"

    country_codes = ["CA"]

    def layer_description(self, cycle_date: str) -> str:
        return (
            f"Canada Drone Restrictions — OpenAIP\n"
            f"Data retrieved: {cycle_date}\n"
            f"Source: openaip.net (community-maintained)\n\n"
            f"FRZ / CTR / ATZ : Permission required from aerodrome ATC\n"
            f"RESTRICTED      : Permission required\n"
            f"DANGER          : Hazardous — avoid unless confirmed inactive\n"
            f"PROHIBITED      : Hard no-fly\n\n"
            f"NOTE: Always verify against Transport Canada (tc.canada.ca)\n"
            f"      and check NAV CANADA NOTAMs before flying.\n"
        )


# ── Registry ──────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, Provider] = {
    "uk": UKProvider(),
    "us": USProvider(),
    "eu": EuropeProvider(),
    "ca": CanadaProvider(),
}

IMPLEMENTED = {"uk", "us", "eu", "ca"}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--region", nargs="+",
        choices=list(PROVIDERS.keys()) + ["all"],
        default=["all"],
        help="Region(s) to generate. Default: all implemented regions.",
    )
    parser.add_argument(
        "--date", metavar="YYYYMMDD",
        help="Pin a specific cycle date (applies to UK / EU AIRAC-based providers).",
    )
    parser.add_argument(
        "--output-dir", default="layers",
        help="Base output directory (default: layers).",
    )
    parser.add_argument(
        "--include-stubs", action="store_true",
        help="Attempt to run stub providers (will error — useful for development).",
    )
    args = parser.parse_args()

    regions = (
        list(IMPLEMENTED) if "all" in args.region else args.region
    )

    if not args.include_stubs:
        skipped = [r for r in regions if r not in IMPLEMENTED]
        if skipped:
            print(f"Skipping unimplemented providers: {', '.join(skipped)}")
            print("  (use --include-stubs to attempt them anyway)")
        regions = [r for r in regions if r in IMPLEMENTED]

    if not regions:
        sys.exit("No implemented regions to run.")

    errors = []
    for region in regions:
        provider = PROVIDERS[region]
        try:
            provider.run(base_dir=args.output_dir, date_hint=args.date)
        except NotImplementedError as e:
            print(f"  SKIP: {e}")
        except Exception as e:
            print(f"  ERROR ({region}): {e}")
            errors.append((region, str(e)))

    print(f"\n{'─' * 50}")
    if errors:
        print("Completed with errors:")
        for region, msg in errors:
            print(f"  {region}: {msg}")
        sys.exit(1)
    else:
        print("All done.")
        print()
        print("Import into ForeFlight:")
        print("  ForeFlight → More → Files → tap .kmz → Import")
        print("  Maps → Layer Options → Custom → toggle per zone type")


if __name__ == "__main__":
    main()

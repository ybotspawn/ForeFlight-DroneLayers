#!/usr/bin/env python3
"""
UK Drone Restriction Zones → ForeFlight KMZ (NATS Official Source)
-------------------------------------------------------------------
Downloads the current NATS UAS Flight Restrictions KML (updated every
28-day AIRAC cycle), categorises each zone by type, and rebuilds it as
a styled KMZ with one toggleable folder per zone type — ready to import
into ForeFlight as a custom map layer.

No API key required. Data is official NATS/CAA (ENR 5.1).

INSTALL
-------
    pip install requests beautifulsoup4 lxml

USAGE
-----
    python uk_drone_to_foreflight.py

    # Force a specific AIRAC date instead of auto-detecting:
    python uk_drone_to_foreflight.py --date 20260416

IMPORTING INTO FOREFLIGHT
--------------------------
    1. Copy the output .kmz to iCloud Drive or Dropbox
    2. ForeFlight → More → Files → tap file → Import
    3. Maps → Layer Options → Custom → toggle on
    Each zone type is a separate folder you can toggle individually.

ZONE TYPES AND WHAT THEY MEAN FOR DRONES
-----------------------------------------
    FRZ  (Flight Restriction Zone) — Permission required from aerodrome ATC
    R    (Restricted)              — Permission required, may have conditions
    D    (Danger)                  — Not prohibited but hazardous, avoid unless
                                     you know it's inactive
    P    (Prohibited)              — Hard no-fly, no exceptions
"""

import argparse
import io
import os
import re
import sys
import zipfile
from collections import defaultdict
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, parse, tostring

import requests
from bs4 import BeautifulSoup

# ── Constants ─────────────────────────────────────────────────────────────────

NATS_DATASETS_URL = (
    "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/"
)
KML_URL_TEMPLATE = (
    "https://nats-uk.ead-it.com/cms-nats/export/sites/default/en/Publications/"
    "digital-datasets/UAS_AREA_1/EG_UAS_FR_DS_AREA1_FULL_{date}_KML.zip"
)
OUTPUT_FILE = "uk_drone_restrictions.kmz"

# Zone categories: (display label, KML aabbggrr outline colour, fill alpha hex)
ZONE_STYLES = {
    "FRZ":   ("FRZ – Permission Required", "ff00aaff", "33"),  # amber
    "R":     ("Restricted (R)",            "ff0000ff", "33"),  # red
    "D":     ("Danger (D)",                "ff0080ff", "33"),  # orange
    "P":     ("Prohibited (P)",            "ff000099", "55"),  # dark red
    "OTHER": ("Other Restrictions",        "ff888888", "22"),  # grey
}

FOLDER_ORDER = ["P", "R", "FRZ", "D", "OTHER"]

# ── NATS page scraping ────────────────────────────────────────────────────────

def find_latest_kml_url(requested_date):
    """Returns (url, date_str). Scrapes NATS page if no date supplied."""
    if requested_date:
        return KML_URL_TEMPLATE.format(date=requested_date), requested_date

    print("Checking NATS datasets page for latest AIRAC KML …")
    try:
        r = requests.get(NATS_DATASETS_URL, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        sys.exit(f"Could not reach NATS datasets page: {e}")

    soup = BeautifulSoup(r.text, "lxml")
    dates = []
    for a in soup.find_all("a", href=True):
        m = re.search(
            r"UAS_AREA_1/EG_UAS_FR_DS_AREA1_FULL_(\d{8})_KML\.zip", a["href"]
        )
        if m:
            dates.append(m.group(1))

    if not dates:
        sys.exit(
            "Could not find KML links on the NATS page.\n"
            "Pass a date manually: --date 20260416"
        )

    from datetime import date, datetime

    today = date.today()
    valid = sorted(
        (d for d in dates if datetime.strptime(d, "%Y%m%d").date() <= today),
        reverse=True,
    )
    chosen = valid[0] if valid else sorted(dates)[-1]
    print(f"  Latest current AIRAC dataset: {chosen}")
    return KML_URL_TEMPLATE.format(date=chosen), chosen


# ── Download ──────────────────────────────────────────────────────────────────


def download_kml(url):
    """
    Download the NATS KML zip and return the raw KML bytes.

    NATS package structure (confirmed):
      outer .zip
        └── EG_UAS_FR_DS_AREA1_FULL_YYYYMMDD.kmz   ← KMZ = zip containing doc.kml
        └── (metadata / sha256 files — ignored)
    """
    print(f"Downloading {url.split('/')[-1]} …")
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
    except requests.HTTPError:
        sys.exit(
            f"Download failed ({r.status_code}). "
            "Check the date is a valid AIRAC effective date."
        )
    except requests.RequestException as e:
        sys.exit(f"Network error: {e}")

    # Layer 1: outer zip
    with zipfile.ZipFile(io.BytesIO(r.content)) as outer:
        kmz_names = [n for n in outer.namelist() if n.lower().endswith(".kmz")]
        if not kmz_names:
            sys.exit(
                "No .kmz file found in the downloaded zip.\n"
                "Run inspect_nats_zip.py to check the current package structure."
            )
        kmz_name = kmz_names[0]
        print(f"  Found {kmz_name}")
        kmz_bytes = outer.read(kmz_name)

    # Layer 2: KMZ is itself a zip — extract doc.kml
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as kmz:
        kml_names = [n for n in kmz.namelist() if n.lower().endswith(".kml")]
        if not kml_names:
            sys.exit("No .kml found inside the .kmz.")
        # Prefer doc.kml, fall back to largest
        kml_name = next((n for n in kml_names if n.lower() == "doc.kml"), kml_names[0])
        print(f"  Extracting {kml_name} …")
        return kmz.read(kml_name)


# ── Zone categorisation ───────────────────────────────────────────────────────

def categorise(name):
    """
    Derive zone category from the NATS designator name.
      "EG R 313"   → R
      "EG D 001A"  → D
      "EG P 099"   → P
      "EGLL FRZ"   → FRZ
      "FRZ EGKK"   → FRZ
    """
    upper = name.upper()
    if "FRZ" in upper or "RPZ" in upper:
        return "FRZ"
    m = re.search(r"\bEG\s+([RDP])\b", upper)
    if m:
        return m.group(1)
    if re.match(r"^[RDP]\s+\d", upper):
        return upper[0]
    return "OTHER"


# ── KML parsing ───────────────────────────────────────────────────────────────

_NS_RE = re.compile(r'\s+xmlns(?::[a-z0-9]+)?="[^"]*"')
_TAG_NS_RE = re.compile(r"\{[^}]+\}")


def strip_namespaces(xml_bytes):
    text = xml_bytes.decode("utf-8", errors="replace")
    text = _NS_RE.sub("", text)
    return text.encode("utf-8")


def parse_placemarks(kml_bytes):
    """Return list of dicts with name, desc, category, geo element."""
    clean = strip_namespaces(kml_bytes)
    root = parse(io.BytesIO(clean)).getroot()

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
                "category": categorise(name),
                "geo":      geo,
            })

    return placemarks


# ── KML output ────────────────────────────────────────────────────────────────

def _add_styles(doc):
    for key, (label, outline, alpha) in ZONE_STYLES.items():
        fill = alpha + outline[2:]

        style = SubElement(doc, "Style", id=f"s_{key}")

        ls = SubElement(style, "LineStyle")
        SubElement(ls, "color").text = outline
        SubElement(ls, "width").text = "1.5"

        ps = SubElement(style, "PolyStyle")
        SubElement(ps, "color").text = fill
        SubElement(ps, "fill").text = "1"
        SubElement(ps, "outline").text = "1"


def _clone_geo(parent, el):
    """Recursively clone a geometry element, stripping leftover namespaces."""
    tag = _TAG_NS_RE.sub("", el.tag)
    new = SubElement(parent, tag)
    for k, v in el.attrib.items():
        new.set(k, v)
    if el.text:
        new.text = el.text
    for child in el:
        _clone_geo(new, child)


def build_kml(placemarks, airac_date):
    kml = Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = SubElement(kml, "Document")
    SubElement(doc, "name").text = "UK Drone Restrictions"
    SubElement(doc, "description").text = (
        f"UK UAS Flight Restrictions — NATS ENR 5.1\n"
        f"AIRAC effective: {airac_date}\n"
        f"Source: NATS AIS (nats-uk.ead-it.com)\n\n"
        f"FRZ / RPZ : Permission required from aerodrome ATC\n"
        f"R         : Permission required, may have conditions\n"
        f"D         : Hazardous — avoid unless confirmed inactive\n"
        f"P         : Hard no-fly"
    )

    _add_styles(doc)

    groups = defaultdict(list)
    for pm in placemarks:
        groups[pm["category"]].append(pm)

    for key in FOLDER_ORDER:
        items = groups.get(key)
        if not items:
            continue
        label = ZONE_STYLES[key][0]
        folder = SubElement(doc, "Folder")
        SubElement(folder, "name").text = f"{label} ({len(items)})"
        SubElement(folder, "visibility").text = "1"
        SubElement(folder, "open").text = "0"

        for pm in sorted(items, key=lambda x: x["name"]):
            placemark = SubElement(folder, "Placemark")
            SubElement(placemark, "name").text = pm["name"]
            SubElement(placemark, "styleUrl").text = f"#s_{key}"
            if pm["desc"]:
                SubElement(placemark, "description").text = pm["desc"]
            _clone_geo(placemark, pm["geo"])

    raw = tostring(kml, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    return pretty.decode("utf-8")


def save_kmz(kml_str, path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as kmz:
        kmz.writestr("doc.kml", kml_str)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date", metavar="YYYYMMDD",
        help="Force a specific AIRAC effective date (e.g. 20260416). "
             "Defaults to auto-detecting the latest from the NATS page.",
    )
    parser.add_argument(
        "--output", default=OUTPUT_FILE,
        help=f"Output KMZ filename (default: {OUTPUT_FILE})",
    )
    args = parser.parse_args()

    url, airac_date = find_latest_kml_url(args.date)
    kml_bytes = download_kml(url)

    print("Parsing zones …")
    placemarks = parse_placemarks(kml_bytes)
    print(f"  Total zones: {len(placemarks)}")

    by_cat = defaultdict(int)
    for pm in placemarks:
        by_cat[pm["category"]] += 1
    for key in FOLDER_ORDER:
        if key in by_cat:
            print(f"  {ZONE_STYLES[key][0]:<38} {by_cat[key]:>4}")

    print("Building KMZ …")
    kml_str = build_kml(placemarks, airac_date)
    save_kmz(kml_str, args.output)

    size_kb = os.path.getsize(args.output) / 1024
    print(f"\n✓  {args.output}  ({size_kb:.1f} KB)  —  AIRAC {airac_date}")
    print()
    print("Import into ForeFlight:")
    print("  1. Copy the .kmz to iCloud Drive or Dropbox")
    print("  2. ForeFlight → More → Files → tap file → Import")
    print("  3. Maps → Layer Options → Custom → toggle on")
    print("     Each zone type is a separate toggleable folder.")
    print()
    print("REMINDER: FRZ/R/D/P are NOT all hard no-fly zones.")
    print("  FRZ & R = permission required   D = hazardous   P = no-fly")


if __name__ == "__main__":
    main()
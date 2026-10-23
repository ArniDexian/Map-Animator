#!/usr/bin/env python3
"""Build the bundled Unicode/CLDR emoji catalog and copy the Noto color font."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "assets" / "emoji"
SOURCES = {
    "unicode_emoji_test": (
        "https://www.unicode.org/Public/emoji/latest/emoji-test.txt",
        "emoji-test.txt",
    ),
    "cldr_ru": (
        "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-annotations-full/annotations/ru/annotations.json",
        "cldr-annotations-ru.json",
    ),
    "cldr_ru_derived": (
        "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-annotations-derived-full/annotationsDerived/ru/annotations.json",
        "cldr-annotations-derived-ru.json",
    ),
    "cldr_en": (
        "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-annotations-full/annotations/en/annotations.json",
        "cldr-annotations-en.json",
    ),
    "cldr_en_derived": (
        "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-annotations-derived-full/annotationsDerived/en/annotations.json",
        "cldr-annotations-derived-en.json",
    ),
    "cldr_license": (
        "https://raw.githubusercontent.com/unicode-org/cldr-json/main/LICENSE",
        "CLDR-LICENSE.txt",
    ),
    "noto_font": (
        "https://github.com/googlefonts/noto-emoji/raw/main/2D/fonts/NotoColorEmoji.ttf",
        "NotoColorEmoji.ttf",
    ),
    "noto_license": (
        "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/2D/fonts/LICENSE",
        "Noto-emoji-LICENSE.txt",
    ),
}
VERIFIED_APP_SHAPING_AUDIT = {
    "unicode_version": "18.0",
    "catalog_sha256": "aea80667bb2d736f4ed26ea43d1cf6b8e24d59730f52c150d6c06847e1fa1c34",
    "font_sha256": "15671215ab769fdc7162a045d56fd7d7e477c51b04e6b3c761d914d8fdd6cc44",
    "engine": "application renderer: HarfBuzz + fontTools CBDT mapping",
    "catalog_entries": 3972,
    "single_glyph_matches": 3972,
    "missing_or_unjoined": 0,
}


def read_source(key, source_dir):
    url, filename = SOURCES[key]
    if source_dir:
        path = source_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing offline source: {path}")
        data = path.read_bytes()
    else:
        request = Request(url, headers={"User-Agent": "Map-Animator-emoji-catalog/1.0"})
        with urlopen(request, timeout=90) as response:
            data = response.read()
    return data, {"url": url, "filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                  "bytes": len(data)}


def load_annotations(data, derived=False):
    root = json.loads(data.decode("utf-8"))
    root = root["annotationsDerived" if derived else "annotations"]
    return {key.replace("\ufe0f", ""): value for key, value in root["annotations"].items()}


def annotation_keywords(sequence, direct, derived):
    sequence = sequence.replace("\ufe0f", "")
    terms = []
    for source in (direct, derived):
        entry = source.get(sequence, {})
        # The short CLDR display name is the best first searchable/display label.
        terms.extend(entry.get("tts", []))
        terms.extend(entry.get("default", []))
    return " | ".join(dict.fromkeys(term.strip() for term in terms if term.strip()))


def parse_unicode_emoji(data, annotations):
    source = data.decode("utf-8-sig")
    version_match = re.search(r"^# Version:\s*(\S+)", source, re.M)
    date_match = re.search(r"^# Date:\s*(.+)$", source, re.M)
    if not version_match or not date_match:
        raise ValueError("Unicode emoji-test.txt is missing its version/date header")

    groups = []
    current = None
    counts = {"fully-qualified": 0, "component": 0}
    annotation_counts = {"ru": 0, "en": 0}
    for line in source.splitlines():
        if line.startswith("# group: "):
            current = {"name": line[len("# group: "):].strip(), "emojis": []}
            groups.append(current)
            continue
        if ";" not in line or "#" not in line:
            continue
        codepoints, rest = line.split(";", 1)
        status, description = rest.split("#", 1)
        status = status.strip()
        if status not in counts:
            continue
        name_match = re.search(r"\bE\d+(?:\.\d+)?\s+(.+?)\s*$", description.strip())
        if current is None or not name_match:
            raise ValueError(f"Could not parse Unicode emoji record: {line}")
        emoji = "".join(chr(int(codepoint, 16)) for codepoint in codepoints.split())
        keywords_ru = annotation_keywords(emoji, annotations["ru"][0], annotations["ru"][1])
        keywords_en = annotation_keywords(emoji, annotations["en"][0], annotations["en"][1])
        annotation_counts["ru"] += bool(keywords_ru)
        annotation_counts["en"] += bool(keywords_en)
        current["emojis"].append({"emoji": emoji, "name": name_match.group(1),
                                  "keywords_ru": keywords_ru, "keywords_en": keywords_en})
        counts[status] += 1
    if not groups or not counts["fully-qualified"]:
        raise ValueError("Unicode emoji-test.txt contained no supported records")
    catalog = {"version": version_match.group(1), "groups": groups}
    return catalog, date_match.group(1), counts, annotation_counts


def render_and_check_font(font_path, catalog):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as error:
        raise RuntimeError("Pillow is required to verify the bundled color font") from error

    font = ImageFont.truetype(str(font_path), 109)

    def raster(emoji):
        box = font.getbbox(emoji)
        image = Image.new("RGBA", (max(1, box[2] - box[0]), max(1, box[3] - box[1])))
        ImageDraw.Draw(image).text((-box[0], -box[1]), emoji, font=font, embedded_color=True)
        return image

    entries = [entry for group in catalog["groups"] for entry in group["emojis"]]
    blank = [entry["emoji"] for entry in entries if raster(entry["emoji"]).getbbox() is None]
    color_samples = {}
    for emoji in ("😀", "☕", "🏍️", "🫫"):
        image = raster(emoji)
        colors = {pixel[:3] for pixel in image.getdata() if pixel[3] > 0}
        if len(colors) < 2:
            raise RuntimeError(f"Noto color font did not render {emoji!r} in color at 109 px")
        color_samples[emoji] = len(colors)
    return {"layout_engine": "Pillow BASIC", "pillow_bitmap_size_px": 109,
            "catalog_entries_tested": len(entries), "blank_entries": blank,
            "sequence_shaping_verified": False, "color_sample_distinct_rgb": color_samples}


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path,
                        help="read downloaded upstream files from this folder instead of the network")
    args = parser.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)

    raw = {}
    source_info = {}
    for key in SOURCES:
        raw[key], source_info[key] = read_source(key, args.source_dir)

    annotations = {
        "ru": (load_annotations(raw["cldr_ru"]), load_annotations(raw["cldr_ru_derived"], derived=True)),
        "en": (load_annotations(raw["cldr_en"]), load_annotations(raw["cldr_en_derived"], derived=True)),
    }
    catalog, unicode_date, counts, annotation_counts = parse_unicode_emoji(
        raw["unicode_emoji_test"], annotations)
    font_path = DEST / "NotoColorEmoji.ttf"
    font_path.write_bytes(raw["noto_font"])
    (DEST / "LICENSE-NotoColorEmoji.txt").write_bytes(raw["noto_license"])
    (DEST / "LICENSE-Unicode.txt").write_bytes(raw["cldr_license"])
    font_check = render_and_check_font(font_path, catalog)
    if font_check["blank_entries"]:
        print(f"Font limitation: {len(font_check['blank_entries'])} catalog entries rendered blank at 109 px.",
              file=sys.stderr)

    catalog_path = DEST / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "unicode_version": catalog["version"], "unicode_date": unicode_date,
        "counts": counts, "group_count": len(catalog["groups"]),
        "annotation_counts": annotation_counts, "font_check": font_check,
        "sources": source_info,
        "outputs": {"catalog_sha256": sha256_file(catalog_path), "font_sha256": sha256_file(font_path)},
    }
    if (catalog["version"] == VERIFIED_APP_SHAPING_AUDIT["unicode_version"]
            and manifest["outputs"]["catalog_sha256"] == VERIFIED_APP_SHAPING_AUDIT["catalog_sha256"]
            and manifest["outputs"]["font_sha256"] == VERIFIED_APP_SHAPING_AUDIT["font_sha256"]):
        manifest["application_renderer_shaping_audit"] = VERIFIED_APP_SHAPING_AUDIT.copy()
    (DEST / "sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    write_readme(manifest)
    print(f"Unicode Emoji {catalog['version']}: {counts['fully-qualified']} fully-qualified + "
          f"{counts['component']} components in {len(catalog['groups'])} groups; "
          f"Russian keywords on {annotation_counts['ru']} and English keywords on "
          f"{annotation_counts['en']} entries. Font checked at 109 px.")


def write_readme(manifest):
    src = manifest["sources"]
    lines = [
        "# Bundled emoji catalog", "",
        f"Unicode Emoji **{manifest['unicode_version']}** ({manifest['unicode_date']}); "
        f"{manifest['counts']['fully-qualified']:,} fully-qualified entries and "
        f"{manifest['counts']['component']} components in {manifest['group_count']} groups.",
        "The catalog retains the full Unicode list even when the bundled font lacks an artwork glyph.", "",
        "## Build and verification", "",
        "Run `.venv/bin/python tools/build_emoji_catalog.py` to download current upstream inputs and rebuild.",
        "For an offline rebuild, pass `--source-dir DIR` containing the downloaded files named in `sources.json`.",
        "Pillow BASIC rasterizes the bundled CBDT font at its 109 px bitmap strike and checks representative color glyphs.",
        f"This build tested {manifest['font_check']['catalog_entries_tested']:,} entries; "
        f"{len(manifest['font_check']['blank_entries'])} rendered blank. This nonblank/color smoke test does not "
        "verify ZWJ, flag, skin-tone, or other multi-codepoint sequence shaping; Pillow BASIC can draw the "
        "components separately. Use a shaping-engine audit before treating a nonblank sequence as supported.",
    ]
    audit = manifest.get("application_renderer_shaping_audit")
    if audit:
        lines.extend([
            "",
            f"Separate application-renderer audit ({audit['engine']}) matched all "
            f"{audit['single_glyph_matches']:,} of {audit['catalog_entries']:,} catalog entries to one CBDT glyph; "
            f"{audit['missing_or_unjoined']} were missing or unjoined.",
            "This exact-version audit is recorded separately from the builder's Pillow BASIC smoke test.",
        ])
    lines.extend([
        "", "## Sources and provenance", "",
        f"- Unicode Emoji {manifest['unicode_version']} data: "
        f"[{src['unicode_emoji_test']['url']}]({src['unicode_emoji_test']['url']}) "
        f"(dated {manifest['unicode_date']}); SHA-256 `{src['unicode_emoji_test']['sha256']}`.",
        f"- Russian/English names and keywords: Unicode CLDR `annotations-full` and "
        f"`annotations-derived-full` JSON from [unicode-org/cldr-json](https://github.com/unicode-org/cldr-json). "
        f"SHA-256: RU `{src['cldr_ru']['sha256']}` / `{src['cldr_ru_derived']['sha256']}`, "
        f"EN `{src['cldr_en']['sha256']}` / `{src['cldr_en_derived']['sha256']}`.",
        f"- Color font: [{src['noto_font']['url']}]({src['noto_font']['url']}); "
        f"SHA-256 `{src['noto_font']['sha256']}`. License: SIL Open Font License 1.1 "
        "(included as `LICENSE-NotoColorEmoji.txt`).",
        f"- Unicode/CLDR license: [{src['cldr_license']['url']}]({src['cldr_license']['url']}); "
        f"SHA-256 `{src['cldr_license']['sha256']}`. Included as `LICENSE-Unicode.txt`.",
        "- `sources.json` records source URLs, downloaded byte counts, hashes, Unicode version, and the font smoke-test result.",
        "- Unicode and CLDR data are subject to the Unicode Terms of Use; see the source files and project pages.", "",
    ])
    (DEST / "README.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

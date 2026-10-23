# Bundled emoji catalog

Unicode Emoji **18.0** (2026-04-30, 22:40:18 GMT); 3,963 fully-qualified entries and 9 components in 10 groups.
The catalog retains the full Unicode list even when the bundled font lacks an artwork glyph.

On macOS, the application prefers the installed Apple Color Emoji font and falls back to this bundled font for unsupported symbols. Other platforms use the bundled font. Apple’s font is read locally and is never redistributed. The application adds a green recolour of the triangular flag to the picker as the default start icon.

## Build and verification

Run `.venv/bin/python tools/build_emoji_catalog.py` to download current upstream inputs and rebuild.
For an offline rebuild, pass `--source-dir DIR` containing the downloaded files named in `sources.json`.
Pillow BASIC rasterizes the bundled CBDT font at its 109 px bitmap strike and checks representative color glyphs.
This build tested 3,972 entries; 0 rendered blank. This nonblank/color smoke test does not verify ZWJ, flag, skin-tone, or other multi-codepoint sequence shaping; Pillow BASIC can draw the components separately. Use a shaping-engine audit before treating a nonblank sequence as supported.

Separate application-renderer audit (application renderer: HarfBuzz + fontTools CBDT mapping) matched all 3,972 of 3,972 catalog entries to one CBDT glyph; 0 were missing or unjoined.
This exact-version audit is recorded separately from the builder's Pillow BASIC smoke test.

## Sources and provenance

- Unicode Emoji 18.0 data: [https://www.unicode.org/Public/emoji/latest/emoji-test.txt](https://www.unicode.org/Public/emoji/latest/emoji-test.txt) (dated 2026-04-30, 22:40:18 GMT); SHA-256 `8f3735cda1f92a779d78af67cf86066bb1f07143dc22f2ac29394d9bc57ab21a`.
- Russian/English names and keywords: Unicode CLDR `annotations-full` and `annotations-derived-full` JSON from [unicode-org/cldr-json](https://github.com/unicode-org/cldr-json). SHA-256: RU `c483d312051dbea379bcd2ac6dec6f2712572c0c9a5d073031f4702b030e99a9` / `c7106a9d14b485636620972d147232ec2e45e0d779422ed208dabad8fa186782`, EN `f22083cb86dffb63a643d5bacb5d9899f82d2fa5d388ad4f3aed72184acef505` / `15c9457afa703a32dcc78d54626418a91e820fe0ddd67b7d491c4c4d9f5846ab`.
- Color font: [https://github.com/googlefonts/noto-emoji/raw/main/2D/fonts/NotoColorEmoji.ttf](https://github.com/googlefonts/noto-emoji/raw/main/2D/fonts/NotoColorEmoji.ttf); SHA-256 `15671215ab769fdc7162a045d56fd7d7e477c51b04e6b3c761d914d8fdd6cc44`. License: SIL Open Font License 1.1 (included as `LICENSE-NotoColorEmoji.txt`).
- Unicode/CLDR license: [https://raw.githubusercontent.com/unicode-org/cldr-json/main/LICENSE](https://raw.githubusercontent.com/unicode-org/cldr-json/main/LICENSE); SHA-256 `220ba0e1c43b99530d2d5bdb892a99dca0989414f51ab695ecd90163eaa1ec3b`. Included as `LICENSE-Unicode.txt`.
- `sources.json` records source URLs, downloaded byte counts, hashes, Unicode version, and the font smoke-test result.
- Unicode and CLDR data are subject to the Unicode Terms of Use; see the source files and project pages.

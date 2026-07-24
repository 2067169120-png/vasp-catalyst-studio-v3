# Bundled report fonts

These fonts are runtime assets for offline HTML and embedded PDF output. They
are data files, not part of the MIT-licensed application source.

## Noto Sans SC

- Upstream family: Noto Sans CJK / Noto Sans SC
- Upstream project: https://github.com/notofonts/noto-cjk
- License: SIL Open Font License 1.1 (`OFL-1.1.txt`)
- Immediate TTF source: offline TrueType conversion of the WOFF2 files already
  shipped in this directory. The conversion changes the container format only;
  it does not intentionally alter glyph outlines.

| File | SHA-256 |
|---|---|
| `noto-sans-sc-400.woff2` | `95e3633b6a98f764ba3adfb54504a0cd4799328c009adf9081d6c1850f9c4c78` |
| `noto-sans-sc-700.woff2` | `e1df51edc00bce27b58044e829fb8ec6accc8a5daece475413de90d52818845c` |
| `noto-sans-sc-400.ttf` | `b2afd5eacea163fe0e6825fb6aa03ad88127e8018d83334e5e1b5fe7f18d8fc9` |
| `noto-sans-sc-700.ttf` | `2438590b328b5dbd9ead8cff530f60b6ce86c238f4c12c40aa86dffdb017386c` |

The converted files retain the upstream copyright, OFL URL, family/style and
weight metadata. ReportLab registers them under application-local aliases; it
does not rewrite their internal font names.

## DejaVu Sans

- Upstream project: https://dejavu-fonts.github.io/
- Immediate source: the unmodified DejaVu TTF files distributed with
  Matplotlib 3.10.8
- License: Bitstream Vera / Arev terms (`LICENSE-DEJAVU.txt`)
- Purpose: deterministic Greek letters, subscripts, superscripts, arrows and
  mathematical symbols when the `--no-charts` build omits Matplotlib itself

| File | SHA-256 |
|---|---|
| `dejavu-sans-400.ttf` | `3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22` |
| `dejavu-sans-700.ttf` | `b184b89e3c1075f22f6b71575b6fc20d4972b3cfd3b23322ca6fd596dcaef167` |
| `dejavu-sans-400-italic.ttf` | `ccdf74b350f11fd3dd5774de50e5e6346a1a5da1f5b7d5fb83590665e97a5213` |
| `dejavu-sans-700-italic.ttf` | `6edf0283160186af451cbee71e7b845f2e4cabf264bb992ce668c83c25465e6f` |

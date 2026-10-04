# Zyfun VOD Source Feed

A small public repository for the statically importable Zyfun / TVBox VOD source list. The JSON is ready to download and import; it does not require PHP or a local gateway.

## Import file

Direct JSON: https://raw.githubusercontent.com/bhsherry/vod-source-feed/main/public/zyfun-vod-import.json

The same file is also available at `public/vod-sources.json` for clients expecting that name. Use the direct import link above in Zyfun 3.4.7 or download it and import the TVBox-style `sites` configuration.

## Automatic refresh

GitHub Actions runs daily at 03:17 China Standard Time and supports manual dispatch. Each run makes one HTTPS `ac=class` request per registered CMS endpoint, without credentials, cookies, redirects, media downloads, or running source-provided code. The generated import contains sources whose category response has a supported shape.

A successful category request confirms only that the endpoint currently returns a supported category structure. It does not confirm search, detail, episode parsing, or playback. Current stage evidence and known failures are recorded in `public/vod-source-health.json`.

## Files

- `public/zyfun-vod-import.json` — direct import file
- `public/vod-sources.json` — compatibility alias
- `public/vod-source-health.json` — latest bounded health check
- `config/vod-public-sources.json` — source registry used by the builder
- `bin/vod-public-feed.py` — dependency-free generator and health checker
- `.github/workflows/vod-source-feed.yml` — scheduled refresh workflow

The current import contains 13 HTTPS CMS sources: 12 JSON APIs and one XML API. Spider / Java `type=3` entries are not represented as working CMS APIs; they require separate static analysis and native adapter work.

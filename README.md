# Zyfun VOD Source Feed

A small public repository for the statically importable Zyfun / TVBox VOD source list. The JSON is ready to download and import; it does not require PHP or a local gateway.

## Import file

Direct JSON: https://raw.githubusercontent.com/bhsherry/vod-source-feed/main/public/zyfun-vod-import.json

The same file is also available at `public/vod-sources.json` for clients expecting that name. Use the direct import link above in Zyfun 3.4.7 or download it and import the TVBox-style `sites` configuration.

## Automatic refresh

GitHub Actions runs daily at 03:17 China Standard Time and supports manual dispatch. Each run makes one `ac=class` request per registered CMS endpoint, without credentials, cookies, redirects, media downloads, or running source-provided code. All sources use HTTPS except one exact HTTP IP:9981 endpoint that the user explicitly supplied and authorized; that exception was also checked through category, search, and detail and cannot be broadened to other HTTP/IP URLs. The generated import contains sources whose category response has a supported shape.

A successful category request confirms only that the endpoint currently returns a supported category structure. It does not confirm search, detail, episode parsing, or playback. Current stage evidence and known failures are recorded in `public/vod-source-health.json`.

## Optional search and detail audit

For a bounded metadata-stage check, run `python3 bin/vod-stage-audit.py --keys vod_ffzy vod_liangzi_xml --output /tmp/vod-stage-audit.json`. It first requires the selected sources to pass the current category report, then makes one search request using `流浪地球` and, when a result has an ID, one detail request. It records episode-field shape and playback host names, but does not follow redirects, resolve player scripts, or request media bytes. This audit is manual so scheduled health refreshes remain one request per source.

## Files

- `public/zyfun-vod-import.json` — direct import file
- `public/vod-sources.json` — compatibility alias
- `public/vod-source-health.json` — latest bounded health check
- `config/vod-public-sources.json` — source registry used by the builder
- `bin/vod-public-feed.py` — dependency-free generator and health checker
- `bin/vod-stage-audit.py` — bounded search/detail metadata audit for selected keys
- `.github/workflows/vod-source-feed.yml` — scheduled refresh workflow

The registry currently tracks 24 direct CMS endpoints: 20 HTTPS JSON and 3 HTTPS XML APIs, plus one explicitly authorized HTTP/IP JSON API. Nine HTTPS endpoints were added after static extraction from user-provided multi-source indexes and a bounded Mac-side category probe. Each scheduled run checks the full registry and publishes only endpoints whose category response has a supported shape. The generated list can vary with the runner's network location; the health report records the probe environment and is the source of truth for current results. A category check does not confirm search, detail, episodes, or playback. Spider / Java entries—including `type=3` and `csp_*` records—are not represented as working CMS APIs and remain a separate static-analysis and native-adapter task.

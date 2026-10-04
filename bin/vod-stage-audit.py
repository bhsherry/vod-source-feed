#!/usr/bin/env python3
"""Run a bounded search/detail/episode-shape audit for selected CMS sources."""

import argparse
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE_BYTES = 1_048_576
USER_AGENT = "MediaSourceManager-VOD-stage-audit/1.0"
SEARCH_TERM = "流浪地球"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _public_dns_only(host: str) -> bool:
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address[4][0]).is_global:
                return False
        except ValueError:
            return False
    return True


def _request(
    source: Dict[str, Any],
    params: Dict[str, str],
    opener: urllib.request.OpenerDirector,
) -> Tuple[str, int, str, bytes, int]:
    parts = urllib.parse.urlsplit(source["api"])
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.fragment
        or not _public_dns_only(host)
    ):
        return "url_or_dns_rejected", 0, "", b"", 0

    query = urllib.parse.urlencode(params)
    url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, "")
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, application/xml, text/xml;q=0.9, */*;q=0.5",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=12) as response:
            status = int(response.getcode())
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return "http_error", int(exc.code), "", b"", int(
            (time.monotonic() - started) * 1000
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "request_failed", 0, "", b"", int(
            (time.monotonic() - started) * 1000
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    if status != 200:
        return "http_error", status, "", b"", duration_ms
    if len(body) > MAX_RESPONSE_BYTES:
        return "response_too_large", status, "", b"", duration_ms
    return "ok", status, "", body, duration_ms


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_videos(payload: ET.Element) -> List[ET.Element]:
    return [node for node in payload.iter() if _local_name(node.tag) == "video"]


def _json_list(payload: Any) -> Optional[List[Any]]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload.get("list"),):
        if isinstance(candidate, list):
            return candidate
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        return data["list"]
    return None


def _parse_list(source_type: int, body: bytes) -> Tuple[str, List[Any]]:
    if source_type == 1:
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return "invalid_json", []
        items = _json_list(payload)
        if items is None:
            return "json_list_field_missing", []
        return "json_list", items

    try:
        payload = ET.fromstring(body)
    except ET.ParseError:
        return "invalid_xml", []
    items = _xml_videos(payload)
    return "xml_video_list", items


def _first_value(record: Any, names: Tuple[str, ...]) -> Optional[str]:
    if isinstance(record, dict):
        for name in names:
            value = record.get(name)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
    elif isinstance(record, ET.Element):
        children = {_local_name(child.tag): child.text for child in record}
        for name in names:
            value = children.get(name.lower())
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _play_metadata(record: Any) -> Dict[str, Any]:
    values: List[str] = []
    if isinstance(record, dict):
        for key in ("vod_play_url", "play_url", "play", "url"):
            value = record.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(str(item) for item in value if isinstance(item, (str, dict)))
    elif isinstance(record, ET.Element):
        for node in record.iter():
            if _local_name(node.tag) in ("dd", "videoplayurl", "vodplayurl", "play_url"):
                if node.text:
                    values.append(node.text)
                values.extend(value for value in node.attrib.values() if value)

    episode_count = 0
    hosts = set()
    schemes = set()
    for value in values:
        play_sources = value.split("$$$")
        episode_count = max(
            episode_count,
            max(
                (len([part for part in play_source.split("#") if part.strip()]) for play_source in play_sources),
                default=0,
            ),
        )
        for candidate in re.findall(r"https?://[^\s\"'<>]+", value):
            candidate = candidate.rstrip("),;]}")
            try:
                parts = urllib.parse.urlsplit(candidate)
            except ValueError:
                continue
            if parts.scheme in ("http", "https") and parts.hostname:
                hosts.add(parts.hostname.lower())
                schemes.add(parts.scheme.lower())

    return {
        "play_url_present": bool(values),
        "episode_segments": episode_count,
        "play_hosts": sorted(hosts),
        "play_schemes": sorted(schemes),
    }


def _rate_limited_request(
    source: Dict[str, Any],
    params: Dict[str, str],
    opener: urllib.request.OpenerDirector,
    last_request_by_host: Dict[str, float],
) -> Tuple[str, int, str, bytes, int]:
    host = urllib.parse.urlsplit(source["api"]).hostname or ""
    last = last_request_by_host.get(host)
    if last is not None:
        delay = 1.0 - (time.monotonic() - last)
        if delay > 0:
            time.sleep(delay)
    result = _request(source, params, opener)
    last_request_by_host[host] = time.monotonic()
    return result


def audit_source(
    source: Dict[str, Any],
    opener: urllib.request.OpenerDirector,
    last_request_by_host: Dict[str, float],
) -> Dict[str, Any]:
    source_type = source["type"]
    search_status, search_http, _, search_body, search_ms = _rate_limited_request(
        source,
        {"ac": "videolist", "wd": SEARCH_TERM},
        opener,
        last_request_by_host,
    )
    row: Dict[str, Any] = {
        "key": source["key"],
        "name": source["name"],
        "type": source_type,
        "search": {
            "status": search_status,
            "http_status": search_http,
            "duration_ms": search_ms,
            "query_term": SEARCH_TERM,
        },
    }
    if search_status != "ok":
        return row

    search_shape, results = _parse_list(source_type, search_body)
    row["search"].update({"response_shape": search_shape, "result_count": len(results)})
    if not results:
        row["search"]["status"] = "empty_or_unmatched"
        return row

    first = results[0]
    item_id = _first_value(first, ("vod_id", "id", "vodid"))
    if not item_id:
        row["search"]["status"] = "first_result_id_missing"
        return row

    detail_action = "detail" if source_type == 1 else "videolist"
    detail_status, detail_http, _, detail_body, detail_ms = _rate_limited_request(
        source,
        {"ac": detail_action, "ids": item_id},
        opener,
        last_request_by_host,
    )
    row["detail"] = {
        "status": detail_status,
        "http_status": detail_http,
        "duration_ms": detail_ms,
    }
    if detail_status != "ok":
        return row

    detail_shape, details = _parse_list(source_type, detail_body)
    row["detail"]["response_shape"] = detail_shape
    row["detail"]["result_count"] = len(details)
    if not details:
        row["detail"]["status"] = "empty_detail"
        return row

    record = details[0]
    row["episode_play_fields"] = _play_metadata(record)
    row["detail"]["status"] = "ok"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys", nargs="+", required=True, help="source keys to probe")
    parser.add_argument(
        "--registry", type=Path, default=ROOT / "config/vod-public-sources.json"
    )
    parser.add_argument(
        "--health-report", type=Path, default=ROOT / "public/vod-source-health.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    sources = registry.get("sources") if isinstance(registry, dict) else None
    if not isinstance(sources, list):
        raise SystemExit("invalid_registry")
    source_map = {item.get("key"): item for item in sources if isinstance(item, dict)}
    health = json.loads(args.health_report.read_text(encoding="utf-8"))
    health_map = {
        item.get("key"): item.get("status")
        for item in health.get("sources", [])
        if isinstance(item, dict)
    }
    unknown = [key for key in args.keys if key not in source_map]
    if unknown:
        raise SystemExit("unknown_keys: " + ", ".join(unknown))

    opener = urllib.request.build_opener(_NoRedirect())
    last_request_by_host: Dict[str, float] = {}
    results = []
    for key in args.keys:
        source = source_map[key]
        if health_map.get(key) != "ok":
            results.append({"key": key, "status": "not_currently_healthy; skipped"})
            continue
        results.append(audit_source(source, opener, last_request_by_host))

    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "probe_environment": "github-actions/ubuntu-24.04"
        if os.environ.get("GITHUB_ACTIONS") == "true"
        else "local",
        "search_term": SEARCH_TERM,
        "method": "one ac=videolist search per key; one bounded detail request when a result ID is present; no redirects, credentials, or media downloads",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Wrote stage audit for %d selected source(s): %s" % (len(results), args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

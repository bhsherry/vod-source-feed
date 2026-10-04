#!/usr/bin/env python3
"""Build a TVBox-compatible VOD source list and a bounded health report."""

import argparse
import ipaddress
import json
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_SOURCES = 100
MAX_RESPONSE_BYTES = 1_048_576
MAX_SOURCE_NAME_CHARS = 80
USER_AGENT = "MediaSourceManager-VOD-health/1.0"


class FeedError(ValueError):
    """Invalid or unsafe public feed input."""


def _normalize_api(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise FeedError("invalid_api_url")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise FeedError("invalid_api_url")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise FeedError("invalid_api_url") from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in (None, 443)
        or not parts.path.startswith("/")
        or "\\" in parts.path
        or "//" in parts.path
    ):
        raise FeedError("unsafe_api_url")
    host = parts.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FeedError("ip_literal_not_allowed")

    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if query and query != [("ac", "list")]:
        raise FeedError("api_query_not_allowed")
    return urllib.parse.urlunsplit(("https", parts.netloc, parts.path, "", ""))


def load_registry(path: Path) -> List[Dict[str, Any]]:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FeedError("registry_unreadable") from exc
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise FeedError("registry_schema_unsupported")
    sources = registry.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise FeedError("registry_source_count_invalid")

    normalized: List[Dict[str, Any]] = []
    seen_keys = set()
    seen_apis = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise FeedError("source_record_invalid_%d" % index)
        key = source.get("key")
        name = source.get("name")
        source_type = source.get("type")
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise FeedError("source_key_invalid_%d" % index)
        if not isinstance(name, str) or not name or len(name) > MAX_SOURCE_NAME_CHARS:
            raise FeedError("source_name_invalid_%d" % index)
        if any(ord(char) < 32 for char in name) or re.search(
            r"https?://|[?&](token|key|auth|password)=", name, re.I
        ):
            raise FeedError("source_name_unsafe_%d" % index)
        if (
            isinstance(source_type, bool)
            or not isinstance(source_type, int)
            or source_type not in (0, 1)
        ):
            raise FeedError("source_type_unsupported_%d" % index)
        api = _normalize_api(source.get("api"))
        if key in seen_keys:
            raise FeedError("duplicate_source_key")
        if api in seen_apis:
            raise FeedError("duplicate_source_api")
        seen_keys.add(key)
        seen_apis.add(api)
        normalized.append(
            {"key": key, "name": name, "type": source_type, "api": api}
        )
    return normalized


def build_tvbox_config(
    sources: List[Dict[str, Any]], include_keys: Any = None
) -> Dict[str, Any]:
    if include_keys is not None:
        sources = [source for source in sources if source["key"] in include_keys]
    if not sources:
        raise FeedError("no_sources_selected")
    return {
        "sites": [
            {
                "key": source["key"],
                "name": source["name"],
                "type": source["type"],
                "api": source["api"],
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 0,
            }
            for source in sources
        ]
    }


def _atomic_json_write(path: Path, value: Dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as temporary:
            temporary.write(body)
            temporary_name = temporary.name
        Path(temporary_name).replace(path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def healthy_source_keys(
    sources: List[Dict[str, Any]], report_path: Path
) -> set:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FeedError("health_report_unreadable") from exc
    rows = report.get("sources") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        raise FeedError("health_report_shape_invalid")
    expected_keys = {source["key"] for source in sources}
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("key") not in expected_keys:
            raise FeedError("health_report_source_invalid")
        key = row["key"]
        if key in by_key:
            raise FeedError("health_report_duplicate_source")
        by_key[key] = row
    if set(by_key) != expected_keys:
        raise FeedError("health_report_incomplete")
    healthy = {key for key, row in by_key.items() if row.get("status") == "ok"}
    if not healthy:
        raise FeedError("no_healthy_sources")
    return healthy


def build_command(
    registry_path: Path,
    output_paths: List[Path],
    health_report_path: Any = None,
) -> int:
    sources = load_registry(registry_path)
    include_keys = (
        healthy_source_keys(sources, health_report_path)
        if health_report_path is not None
        else None
    )
    config = build_tvbox_config(sources, include_keys)
    for path in output_paths:
        _atomic_json_write(path, config)
    print("Built a TVBox source config with %d currently healthy T0/T1 entries." % len(config["sites"]))
    return 0


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
            parsed = ipaddress.ip_address(address[4][0])
        except ValueError:
            return False
        if not parsed.is_global:
            return False
    return True


def _request_class_list(
    source: Dict[str, Any], opener: urllib.request.OpenerDirector
) -> Tuple[str, int, str, int]:
    parts = urllib.parse.urlsplit(source["api"])
    host = parts.hostname or ""
    if not _public_dns_only(host):
        return "dns_or_address_rejected", 0, "", 0
    query = urllib.parse.urlencode({"ac": "class"})
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
        with opener.open(request, timeout=10) as response:
            status = int(response.getcode())
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = b""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "request_failed", 0, "", int((time.monotonic() - started) * 1000)
    duration_ms = int((time.monotonic() - started) * 1000)
    if status != 200:
        return "http_error", status, "", duration_ms
    if len(body) > MAX_RESPONSE_BYTES:
        return "response_too_large", status, "", duration_ms

    if source["type"] == 1:
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return "invalid_json", status, "", duration_ms
        if not isinstance(payload, dict):
            return "unexpected_json_shape", status, "", duration_ms
        classes = payload.get("class")
        if not isinstance(classes, list):
            classes = payload.get("list")
        if not isinstance(classes, list):
            return "catalog_field_missing", status, "json", duration_ms
        if not classes:
            return "empty_class_list", status, "json:0", duration_ms
        return "ok", status, "json:%d" % len(classes), duration_ms

    xml_body = body.lower()
    if re.search(rb"<\s*(?:[a-z0-9_-]+:)?rss(?:\s|>)", xml_body) and re.search(
        rb"</\s*(?:[a-z0-9_-]+:)?rss\s*>", xml_body
    ):
        return "ok", status, "xml", duration_ms
    return "unexpected_xml_shape", status, "xml", duration_ms


def check_command(
    registry_path: Path, output_path: Path, fail_if_none: bool
) -> int:
    sources = load_registry(registry_path)
    opener = urllib.request.build_opener(_NoRedirect())
    last_request_by_host: Dict[str, float] = {}
    results: List[Dict[str, Any]] = []
    for source in sources:
        host = urllib.parse.urlsplit(source["api"]).hostname or ""
        last_request = last_request_by_host.get(host)
        if last_request is not None:
            delay = 1.0 - (time.monotonic() - last_request)
            if delay > 0:
                time.sleep(delay)
        result, http_status, response_shape, duration_ms = _request_class_list(
            source, opener
        )
        last_request_by_host[host] = time.monotonic()
        row: Dict[str, Any] = {
            "key": source["key"],
            "name": source["name"],
            "type": source["type"],
            "status": result,
            "http_status": http_status,
            "duration_ms": duration_ms,
        }
        if response_shape:
            row["response_shape"] = response_shape
        results.append(row)

    healthy = sum(1 for result in results if result["status"] == "ok")
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "check": "one HTTPS ac=class request per source; no redirect, credentials, code execution, or media download",
        "source_count": len(sources),
        "healthy_count": healthy,
        "sources": results,
    }
    _atomic_json_write(output_path, report)
    print(
        "Health check complete: %d/%d class endpoints returned a supported shape; report: %s"
        % (healthy, len(sources), output_path)
    )
    if fail_if_none and healthy == 0:
        print(
            "No endpoint passed the live check; the scheduled publish job must stop.",
            file=sys.stderr,
        )
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser(
        "build", help="build the importable source JSON"
    )
    build_parser.add_argument(
        "--registry", type=Path, default=ROOT / "config/vod-public-sources.json"
    )
    build_parser.add_argument(
        "--output",
        type=Path,
        action="append",
        default=[],
        help="output path; may be supplied more than once",
    )
    build_parser.add_argument(
        "--health-report",
        type=Path,
        help="include only sources whose current health status is ok",
    )
    check_parser = subparsers.add_parser(
        "check", help="probe one category endpoint per source"
    )
    check_parser.add_argument(
        "--registry", type=Path, default=ROOT / "config/vod-public-sources.json"
    )
    check_parser.add_argument(
        "--output", type=Path, default=ROOT / "public/vod-source-health.json"
    )
    check_parser.add_argument("--fail-if-none", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            outputs = args.output or [
                ROOT / "public/vod-sources.json",
                ROOT / "hosting/vod-sources.json",
            ]
            return build_command(args.registry, outputs, args.health_report)
        return check_command(args.registry, args.output, args.fail_if_none)
    except FeedError as exc:
        print("Feed validation failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Priority-aware XMLTV merger.

Rules:
* Channel IDs are compared exactly and case-sensitively.
* The first source in priority/order wins an exact duplicate ID.
* Programmes come only from the source that owns the channel ID.
* A missing icon may be filled from a lower-priority source with the same exact ID,
  then from the optional iptv-org logo API using the same exact ID.
* Programme data is bounded to a rolling past/future window. The merger never
  fabricates schedule data that an upstream source does not provide.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("epg-unifier")
UTC = dt.timezone.utc
XMLTV_TIME_RE = re.compile(r"^(\d{8,14})(?:\s*([+-]\d{4}|Z))?")


@dataclasses.dataclass(frozen=True)
class Source:
    index: int
    name: str
    url: str
    priority: int
    order: int
    region: str
    refresh_hours: int


@dataclasses.dataclass
class FetchResult:
    source: Source
    path: Path | None
    status: str
    error: str | None = None


@dataclasses.dataclass
class ChannelRecord:
    channel_id: str
    source_index: int
    source_region: str
    xml: bytes
    icon: str | None


class SourceStream:
    """Context manager that transparently opens gzip or plain XML."""

    def __init__(self, path: Path):
        self.path = path
        self._raw: BinaryIO | None = None
        self._stream: BinaryIO | None = None

    def __enter__(self) -> BinaryIO:
        self._raw = self.path.open("rb")
        magic = self._raw.read(2)
        self._raw.seek(0)
        if magic == b"\x1f\x8b":
            self._stream = gzip.GzipFile(fileobj=self._raw, mode="rb")
        else:
            self._stream = self._raw
        return self._stream

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._stream is not None and self._stream is not self._raw:
            self._stream.close()
        elif self._raw is not None:
            self._raw.close()


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not config.get("sources") or not config.get("outputs"):
        raise ValueError("Configuration must contain non-empty sources and outputs")
    return config


def build_sources(config: dict) -> list[Source]:
    enabled = [item for item in config["sources"] if item.get("enabled", True)]
    enabled.sort(key=lambda item: (int(item["priority"]), int(item.get("order", 0))))
    return [
        Source(
            index=i,
            name=item["name"],
            url=item["url"],
            priority=int(item["priority"]),
            order=int(item.get("order", i)),
            region=item.get("region", "global"),
            refresh_hours=int(item.get("refresh_hours", 6)),
        )
        for i, item in enumerate(enabled)
    ]


def make_session(settings: dict) -> requests.Session:
    retry = Retry(
        total=int(settings.get("retries", 3)),
        connect=int(settings.get("retries", 3)),
        read=int(settings.get("retries", 3)),
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": settings.get("user_agent", "epg-unifier/1.0")})
    return session


def cache_filename(source: Source) -> str:
    digest = hashlib.sha256(source.url.encode("utf-8")).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.name)
    return f"{source.priority:02d}-{source.order:03d}-{safe}-{digest}.source"


def age_hours(path: Path) -> float:
    return max(0.0, (time.time() - path.stat().st_mtime) / 3600.0)


def peek_xml_root(path: Path) -> None:
    """Reject HTML/error pages and confirm the document root is XMLTV <tv>."""
    with SourceStream(path) as stream:
        context = etree.iterparse(
            stream,
            events=("start",),
            recover=False,
            huge_tree=True,
            load_dtd=False,
            no_network=True,
            resolve_entities=False,
        )
        _, root = next(context)
        local_name = etree.QName(root).localname
        if local_name != "tv":
            raise ValueError(f"Expected XMLTV <tv> root, found <{local_name}>")


def fetch_source(source: Source, cache_dir: Path, settings: dict) -> FetchResult:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / cache_filename(source)
    stale_limit = float(settings.get("stale_if_error_hours", 72))
    if target.exists() and age_hours(target) < source.refresh_hours:
        return FetchResult(source, target, "fresh-cache")

    timeout = float(settings.get("request_timeout_seconds", 180))
    session = make_session(settings)
    part = target.with_suffix(target.suffix + ".part")
    try:
        with session.get(source.url, stream=True, timeout=(20, timeout)) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise ValueError(f"Unexpected HTML response: {content_type}")
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if part.stat().st_size == 0:
            raise ValueError("Downloaded file is empty")
        peek_xml_root(part)
        os.replace(part, target)
        return FetchResult(source, target, "downloaded")
    except Exception as exc:  # noqa: BLE001, retain source-specific failure
        part.unlink(missing_ok=True)
        if target.exists() and age_hours(target) <= stale_limit:
            LOG.warning("%s failed, using stale cache: %s", source.name, exc)
            return FetchResult(source, target, "stale-cache", str(exc))
        return FetchResult(source, None, "failed", str(exc))
    finally:
        session.close()


def fetch_all(sources: list[Source], cache_dir: Path, settings: dict) -> list[FetchResult]:
    workers = max(1, int(settings.get("download_workers", 3)))
    by_index: dict[int, FetchResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_source, src, cache_dir, settings): src for src in sources}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            by_index[result.source.index] = result
            if result.path:
                LOG.info("Source %-30s %s", result.source.name, result.status)
            else:
                LOG.error("Source %-30s failed: %s", result.source.name, result.error)
    return [by_index[src.index] for src in sources]


def clear_element(element: etree._Element) -> None:
    parent = element.getparent()
    element.clear()
    if parent is not None:
        while element.getprevious() is not None:
            del parent[0]


def iter_tag(path: Path, tag: str) -> Iterator[etree._Element]:
    with SourceStream(path) as stream:
        context = etree.iterparse(
            stream,
            events=("end",),
            tag=tag,
            recover=False,
            huge_tree=True,
            load_dtd=False,
            no_network=True,
            resolve_entities=False,
        )
        for _, element in context:
            yield element
            clear_element(element)


def first_icon(element: etree._Element) -> str | None:
    icon = element.find("icon")
    if icon is not None:
        src = icon.get("src")
        if src:
            return src.strip()
    return None


def scan_channels(results: list[FetchResult]) -> tuple[dict[str, ChannelRecord], list[str], dict[str, str], dict[int, dict]]:
    winners: dict[str, ChannelRecord] = {}
    order: list[str] = []
    icon_candidates: dict[str, str] = {}
    source_stats: dict[int, dict] = {}

    for result in results:
        stats = {"channels_seen": 0, "channels_won": 0, "programmes_seen": 0, "programmes_kept": 0}
        source_stats[result.source.index] = stats
        if result.path is None:
            continue
        for element in iter_tag(result.path, "channel"):
            channel_id = element.get("id")
            if not channel_id:
                continue
            stats["channels_seen"] += 1
            icon = first_icon(element)
            if icon and channel_id not in icon_candidates:
                icon_candidates[channel_id] = icon
            if channel_id in winners:
                continue
            payload = etree.tostring(element, encoding="utf-8", with_tail=False)
            winners[channel_id] = ChannelRecord(
                channel_id=channel_id,
                source_index=result.source.index,
                source_region=result.source.region,
                xml=payload,
                icon=icon,
            )
            order.append(channel_id)
            stats["channels_won"] += 1
    return winners, order, icon_candidates, source_stats


def fetch_json_cached(url: str, cache_path: Path, refresh_hours: int, settings: dict) -> object | None:
    if cache_path.exists() and age_hours(cache_path) < refresh_hours:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache_path.unlink(missing_ok=True)
    session = make_session(settings)
    part = cache_path.with_suffix(cache_path.suffix + ".part")
    try:
        timeout = float(settings.get("request_timeout_seconds", 180))
        response = session.get(url, timeout=(20, timeout))
        response.raise_for_status()
        data = response.json()
        part.write_text(json.dumps(data), encoding="utf-8")
        os.replace(part, cache_path)
        return data
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Logo API unavailable: %s", exc)
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None
        return None
    finally:
        part.unlink(missing_ok=True)
        session.close()


def build_logo_map(settings: dict, cache_dir: Path) -> dict[str, str]:
    logo_cfg = settings.get("logo_api", {})
    if not logo_cfg.get("enabled", False):
        return {}
    data = fetch_json_cached(
        logo_cfg["url"],
        cache_dir / "iptv-org-logos.json",
        int(logo_cfg.get("refresh_hours", 24)),
        settings,
    )
    if not isinstance(data, list):
        return {}
    logo_map: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        channel_id = item.get("channel")
        url = item.get("url")
        if channel_id and url and channel_id not in logo_map:
            logo_map[channel_id] = url
    return logo_map


def channel_xml_with_icon(record: ChannelRecord, lower_priority_icons: dict[str, str], logo_map: dict[str, str]) -> bytes:
    if record.icon:
        return record.xml
    icon_url = lower_priority_icons.get(record.channel_id) or logo_map.get(record.channel_id)
    if not icon_url:
        return record.xml
    element = etree.fromstring(record.xml)
    icon = etree.Element("icon")
    icon.set("src", icon_url)
    display_name = element.find("display-name")
    if display_name is not None:
        display_name.addnext(icon)
    else:
        element.insert(0, icon)
    return etree.tostring(element, encoding="utf-8", with_tail=False)


def parse_xmltv_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    match = XMLTV_TIME_RE.match(value.strip())
    if not match:
        return None
    digits, offset = match.groups()
    formats = {8: "%Y%m%d", 10: "%Y%m%d%H", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    fmt = formats.get(len(digits))
    if not fmt:
        return None
    parsed = dt.datetime.strptime(digits, fmt)
    if offset == "Z":
        return parsed.replace(tzinfo=UTC)
    if offset:
        sign = 1 if offset[0] == "+" else -1
        hours = int(offset[1:3])
        minutes = int(offset[3:5])
        return parsed.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))).astimezone(UTC)
    return parsed.replace(tzinfo=UTC)


def programme_in_window(element: etree._Element, start_limit: dt.datetime, end_limit: dt.datetime) -> bool:
    start = parse_xmltv_time(element.get("start"))
    stop = parse_xmltv_time(element.get("stop"))
    if start is None:
        return False
    effective_stop = stop or start
    return effective_stop >= start_limit and start <= end_limit


def output_accepts(region: str, output_cfg: dict) -> bool:
    allowed = output_cfg.get("include_regions", ["*"])
    return "*" in allowed or region in allowed


def open_temp_output(output_dir: Path, output_cfg: dict):
    filename = output_cfg["filename"]
    temp_path = output_dir / f".{filename}.building"
    handle = temp_path.open("wb")
    handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    generated = dt.datetime.now(UTC).isoformat()
    root = f'<tv generator-info-name="epg-unifier" source-info-name="merged" date="{generated}">\n'
    handle.write(root.encode("utf-8"))
    return temp_path, handle


def validate_well_formed(path: Path) -> None:
    with path.open("rb") as stream:
        for _event, _element in etree.iterparse(
            stream,
            events=("end",),
            recover=False,
            huge_tree=True,
            load_dtd=False,
            no_network=True,
            resolve_entities=False,
        ):
            pass


def deterministic_gzip(source: Path, target: Path) -> None:
    temp = target.with_name(f".{target.name}.building")
    with source.open("rb") as src, temp.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temp, target)


def load_previous_status(output_dir: Path) -> dict | None:
    path = output_dir / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def enforce_health(settings: dict, stats: dict, previous: dict | None) -> None:
    source_ratio = stats["sources_successful"] / max(1, stats["sources_total"])
    minimum_ratio = float(settings.get("minimum_source_success_ratio", 0.65))
    if source_ratio < minimum_ratio:
        raise RuntimeError(f"Only {source_ratio:.1%} of sources were usable; minimum is {minimum_ratio:.1%}")
    global_stats = stats["outputs"].get("global")
    if not global_stats or global_stats["channels"] == 0 or global_stats["programmes"] == 0:
        raise RuntimeError("Global output is empty")
    if not previous:
        return
    old = previous.get("outputs", {}).get("global", {})
    old_channels = int(old.get("channels", 0))
    old_programmes = int(old.get("programmes", 0))
    if old_channels:
        drop = 1 - global_stats["channels"] / old_channels
        if drop > float(settings.get("maximum_channel_drop_ratio", 0.30)):
            raise RuntimeError(f"Channel count dropped {drop:.1%}, refusing to publish")
    if old_programmes:
        drop = 1 - global_stats["programmes"] / old_programmes
        if drop > float(settings.get("maximum_programme_drop_ratio", 0.45)):
            raise RuntimeError(f"Programme count dropped {drop:.1%}, refusing to publish")


def merge(config: dict, cache_dir: Path, output_dir: Path, now: dt.datetime | None = None) -> dict:
    settings = config["settings"]
    sources = build_sources(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = load_previous_status(output_dir)
    results = fetch_all(sources, cache_dir, settings)
    usable = [result for result in results if result.path is not None]
    if not usable:
        raise RuntimeError("No EPG source could be downloaded or read from cache")

    winners, channel_order, lower_priority_icons, source_stats = scan_channels(results)
    logo_map = build_logo_map(settings, cache_dir)
    generated_at = now.astimezone(UTC) if now else dt.datetime.now(UTC)
    start_limit = generated_at - dt.timedelta(hours=float(settings.get("past_hours", 6)))
    end_limit = generated_at + dt.timedelta(days=float(settings.get("future_days", 7)))

    handles: dict[str, BinaryIO] = {}
    temp_paths: dict[str, Path] = {}
    output_configs = {item["name"]: item for item in config["outputs"]}
    output_stats = {name: {"channels": 0, "programmes": 0, "filename": cfg["filename"]} for name, cfg in output_configs.items()}

    try:
        for name, cfg in output_configs.items():
            temp_path, handle = open_temp_output(output_dir, cfg)
            temp_paths[name] = temp_path
            handles[name] = handle

        for channel_id in channel_order:
            record = winners[channel_id]
            payload = channel_xml_with_icon(record, lower_priority_icons, logo_map)
            for name, cfg in output_configs.items():
                if output_accepts(record.source_region, cfg):
                    handles[name].write(payload + b"\n")
                    output_stats[name]["channels"] += 1

        for result in results:
            if result.path is None:
                continue
            stats = source_stats[result.source.index]
            for element in iter_tag(result.path, "programme"):
                stats["programmes_seen"] += 1
                channel_id = element.get("channel")
                record = winners.get(channel_id or "")
                if record is None or record.source_index != result.source.index:
                    continue
                if not programme_in_window(element, start_limit, end_limit):
                    continue
                payload = etree.tostring(element, encoding="utf-8", with_tail=False)
                kept_any = False
                for name, cfg in output_configs.items():
                    if output_accepts(record.source_region, cfg):
                        handles[name].write(payload + b"\n")
                        output_stats[name]["programmes"] += 1
                        kept_any = True
                if kept_any:
                    stats["programmes_kept"] += 1

        for handle in handles.values():
            handle.write(b"</tv>\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        handles.clear()

        stats = {
            "generated_at": generated_at.isoformat(),
            "window_start": start_limit.isoformat(),
            "window_end": end_limit.isoformat(),
            "sources_total": len(results),
            "sources_successful": len(usable),
            "channels_unique_exact_id": len(winners),
            "outputs": output_stats,
            "sources": [
                {
                    "name": result.source.name,
                    "priority": result.source.priority,
                    "region": result.source.region,
                    "status": result.status,
                    "error": result.error,
                    **source_stats[result.source.index],
                }
                for result in results
            ],
        }
        enforce_health(settings, stats, previous)

        gzip_temp_paths: dict[str, Path] = {}
        for name, cfg in output_configs.items():
            temp_path = temp_paths[name]
            validate_well_formed(temp_path)
            gzip_temp = output_dir / f".{cfg['filename']}.gz.ready"
            deterministic_gzip(temp_path, gzip_temp)
            gzip_temp_paths[name] = gzip_temp

        # Publish only after every XML and gzip file has been built and validated.
        for name, cfg in output_configs.items():
            final_xml = output_dir / cfg["filename"]
            final_gzip = Path(str(final_xml) + ".gz")
            os.replace(temp_paths[name], final_xml)
            os.replace(gzip_temp_paths[name], final_gzip)

        status_tmp = output_dir / ".status.json.building"
        status_tmp.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        os.replace(status_tmp, output_dir / "status.json")
        return stats
    finally:
        for handle in handles.values():
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        for path in temp_paths.values():
            path.unlink(missing_ok=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge priority-ordered XMLTV sources")
    parser.add_argument("--config", type=Path, default=Path("config/sources.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/output"))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        stats = merge(load_config(args.config), args.cache_dir, args.output_dir)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Build failed: %s", exc)
        return 1
    LOG.info(
        "Build complete: %s exact channel IDs, %s programmes in global output",
        stats["outputs"]["global"]["channels"],
        stats["outputs"]["global"]["programmes"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

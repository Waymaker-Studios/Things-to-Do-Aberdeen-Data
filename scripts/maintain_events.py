#!/usr/bin/env python3
"""Validate and conservatively maintain the version-1 event feed."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


EVENTS_FILE = Path(__file__).resolve().parent.parent / "events.json"
SUPPORTED_VERSION = 1
DEFAULT_MAX_REMOVAL = 0.25
CATEGORY_INDICES = set(range(11))
SUPPORTED_AGES = {"children", "adultsOnly", "allAges"}
SUPPORTED_FREQUENCIES = {"weekly", "monthly"}
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
PLACEHOLDER_TIMES = {"Multiple", "Opening time not listed"}
ACTIVITY_REQUIRED_KEYS = {
    "id",
    "activityId",
    "activityName",
    "description",
    "venueName",
    "minimumPrice",
    "maximumPrice",
    "activityLink",
    "bookingRequired",
    "requirements",
    "ageSuitability",
    "categories",
    "tags",
    "timeOptions",
    "recurrenceRules",
}


class FeedValidationError(ValueError):
    """Raised when the feed cannot safely be consumed or maintained."""


class SafetyError(ValueError):
    """Raised when a proposed cleanup needs deliberate manual approval."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FeedValidationError(message)


def parse_date(value: Any) -> date:
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
            f"Invalid date: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FeedValidationError(f"Invalid calendar date: {value!r}") from exc


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_time(value: Any, where: str) -> None:
    require(nonempty_string(value), f"{where}: missing startTime")
    if value in PLACEHOLDER_TIMES:
        return
    require(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is not None,
            f"{where}: unsupported startTime {value!r}")


def validate_duration(value: Any, where: str) -> None:
    require(value is None or (type(value) is int and value >= 0),
            f"{where}: invalid durationMinutes")


def validate_price(value: Any, where: str) -> None:
    require(
        value is None
        or (type(value) in (int, float) and math.isfinite(value) and value >= 0),
        f"{where}: invalid price",
    )


def unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise FeedValidationError(f"Invalid JSON numeric constant: {value}")


def parse_updated_at(value: Any) -> datetime:
    require(isinstance(value, str), "updatedAt must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedValidationError(f"Invalid updatedAt: {value!r}") from exc
    require(parsed.tzinfo is not None, "updatedAt must include a time zone")
    return parsed


def validate_feed(feed: Any) -> None:
    require(isinstance(feed, dict), "Feed must be a JSON object")
    require(type(feed.get("version")) is int and feed["version"] == SUPPORTED_VERSION,
            f"Unsupported feed version: {feed.get('version')!r}")
    parse_updated_at(feed.get("updatedAt"))

    activities = feed.get("activities")
    require(isinstance(activities, list) and bool(activities),
            "Activities must be a non-empty array")

    seen_ids: set[str] = set()
    for index, activity in enumerate(activities):
        where = f"activities[{index}]"
        require(isinstance(activity, dict), f"{where}: must be an object")
        missing = sorted(ACTIVITY_REQUIRED_KEYS - activity.keys())
        require(not missing, f"{where}: missing fields {', '.join(missing)}")

        activity_id = activity["id"]
        require(nonempty_string(activity_id), f"{where}: invalid id")
        require(activity_id not in seen_ids, f"Duplicate activity id: {activity_id}")
        seen_ids.add(activity_id)

        for key in ("activityId", "activityName", "venueName"):
            require(nonempty_string(activity[key]), f"{activity_id}: invalid {key}")

        for key in ("description", "activityLink"):
            require(activity[key] is None or isinstance(activity[key], str),
                    f"{activity_id}: invalid {key}")
        if activity["activityLink"] is not None:
            parts = urlsplit(activity["activityLink"])
            require(parts.scheme in ("http", "https") and bool(parts.netloc),
                    f"{activity_id}: invalid activityLink")

        booking_required = activity["bookingRequired"]
        require(booking_required is None or type(booking_required) is bool,
                f"{activity_id}: invalid bookingRequired")

        validate_price(activity["minimumPrice"], f"{activity_id}/minimumPrice")
        validate_price(activity["maximumPrice"], f"{activity_id}/maximumPrice")
        minimum = activity["minimumPrice"]
        maximum = activity["maximumPrice"]
        require(minimum is None or maximum is None or minimum <= maximum,
                f"{activity_id}: minimumPrice exceeds maximumPrice")

        for key in ("requirements", "tags"):
            require(string_list(activity[key]), f"{activity_id}: invalid {key}")
        require(activity["ageSuitability"] in SUPPORTED_AGES,
                f"{activity_id}: invalid ageSuitability")

        categories = activity["categories"]
        require(
            isinstance(categories, list)
            and all(type(item) is int and item in CATEGORY_INDICES for item in categories),
            f"{activity_id}: invalid categories",
        )

        time_options = activity["timeOptions"]
        recurrence_rules = activity["recurrenceRules"]
        require(isinstance(time_options, list), f"{activity_id}: invalid timeOptions")
        require(isinstance(recurrence_rules, list), f"{activity_id}: invalid recurrenceRules")

        option_ids: set[str] = set()
        for option_index, option in enumerate(time_options):
            option_where = f"{activity_id}/timeOptions[{option_index}]"
            require(isinstance(option, dict), f"{option_where}: must be an object")
            for key in ("id", "date", "startTime", "durationMinutes"):
                require(key in option, f"{option_where}: missing {key}")
            option_id = option["id"]
            require(nonempty_string(option_id) and option_id not in option_ids,
                    f"{option_where}: missing or duplicate id {option_id!r}")
            option_ids.add(option_id)
            parse_date(option["date"])
            validate_time(option["startTime"], option_where)
            validate_duration(option["durationMinutes"], option_where)

        for rule_index, rule in enumerate(recurrence_rules):
            rule_where = f"{activity_id}/recurrenceRules[{rule_index}]"
            require(isinstance(rule, dict), f"{rule_where}: must be an object")
            for key in ("frequency", "weekdays", "startDate", "endDate", "startTime", "durationMinutes"):
                require(key in rule, f"{rule_where}: missing {key}")
            require(rule["frequency"] in SUPPORTED_FREQUENCIES,
                    f"{rule_where}: unsupported frequency")
            weekdays = rule["weekdays"]
            require(
                string_list(weekdays) and bool(weekdays) and all(day in WEEKDAYS for day in weekdays),
                f"{rule_where}: invalid weekdays",
            )
            start_date = parse_date(rule["startDate"])
            end_date = rule["endDate"]
            require(end_date is None or parse_date(end_date) >= start_date,
                    f"{rule_where}: recurrence ends before start")
            interval = rule.get("interval")
            require(interval is None or (type(interval) is int and interval > 0),
                    f"{rule_where}: invalid interval")
            week_of_month = rule.get("weekOfMonth")
            require(
                week_of_month is None or (type(week_of_month) is int and 1 <= week_of_month <= 5),
                f"{rule_where}: invalid weekOfMonth",
            )
            validate_time(rule["startTime"], rule_where)
            validate_duration(rule["durationMinutes"], rule_where)


@dataclass
class MaintenanceReport:
    activities_before: int
    activities_after: int
    expired_activity_ids: list[str] = field(default_factory=list)
    expired_activity_names: dict[str, str] = field(default_factory=dict)
    expired_time_options: list[tuple[str, str, str]] = field(default_factory=list)
    expired_recurrence_rules: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ongoing_recurring_activities: int = 0

    @property
    def removed_count(self) -> int:
        return len(self.expired_activity_ids)

    @property
    def changed(self) -> bool:
        return bool(
            self.expired_activity_ids
            or self.expired_time_options
            or self.expired_recurrence_rules
        )


def maintain_feed(feed: dict[str, Any], today: date) -> tuple[dict[str, Any], MaintenanceReport]:
    activities = feed["activities"]
    remaining: list[dict[str, Any]] = []
    report = MaintenanceReport(activities_before=len(activities), activities_after=0)
    ongoing_activity_ids: set[str] = set()

    for activity in activities:
        activity_id = activity["id"]
        options = activity["timeOptions"]
        rules = activity["recurrenceRules"]

        if not options and not rules:
            report.warnings.append(f"{activity_id} has no schedule; retained for manual review")
            remaining.append(activity)
            continue

        kept_options = []
        for option in options:
            if parse_date(option["date"]) < today:
                report.expired_time_options.append(
                    (activity_id, option["id"], option["date"])
                )
            else:
                kept_options.append(option)

        kept_rules = []
        for rule in rules:
            end_date = rule["endDate"]
            if end_date is not None and parse_date(end_date) < today:
                report.expired_recurrence_rules.append((activity_id, end_date))
            else:
                kept_rules.append(rule)
                if end_date is None:
                    ongoing_activity_ids.add(activity_id)

        if not kept_options and not kept_rules:
            report.expired_activity_ids.append(activity_id)
            report.expired_activity_names[activity_id] = activity["activityName"]
            continue

        if len(kept_options) == len(options) and len(kept_rules) == len(rules):
            remaining.append(activity)
        else:
            updated = dict(activity)
            updated["timeOptions"] = kept_options
            updated["recurrenceRules"] = kept_rules
            remaining.append(updated)

    report.activities_after = len(remaining)
    report.ongoing_recurring_activities = len(ongoing_activity_ids)
    result = dict(feed)
    result["activities"] = remaining
    return result, report


def enforce_safety(report: MaintenanceReport, max_removal: float, allow_large_removal: bool) -> None:
    if report.activities_before == 0 or report.activities_after == 0:
        raise SafetyError("Maintenance would leave the feed empty; refusing to write")

    removal_ratio = report.removed_count / report.activities_before
    if removal_ratio > max_removal and not allow_large_removal:
        raise SafetyError(
            f"Maintenance would remove {removal_ratio:.1%} of activities, exceeding "
            f"the {max_removal:.1%} safety threshold; rerun with --allow-large-removal "
            "after manual review"
        )


def load_feed(path: Path) -> tuple[dict[str, Any], bytes]:
    original = path.read_bytes()
    try:
        feed = json.loads(
            original,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, FeedValidationError) as exc:
        raise FeedValidationError(f"Invalid JSON: {exc}") from exc
    validate_feed(feed)
    return feed, original


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path: Path, feed: dict[str, Any], original: bytes) -> None:
    directory = path.parent
    mode = path.stat().st_mode & 0o777
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".events-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(feed, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        require(path.read_bytes() == original, "events.json changed during maintenance; refusing to overwrite")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def print_report(report: MaintenanceReport, today: date) -> None:
    print("Event maintenance report")
    print(f"Date: {today.isoformat()}")
    print("Time zone: Europe/London")
    print()
    print(f"Activities before: {report.activities_before}")
    print(f"Activities after: {report.activities_after}")
    print(f"Expired activities removed: {report.removed_count}")
    print(f"Expired time options removed: {len(report.expired_time_options)}")
    print(f"Expired recurrence rules removed: {len(report.expired_recurrence_rules)}")
    print(f"Ongoing recurring activities preserved: {report.ongoing_recurring_activities}")

    if report.expired_activity_ids:
        print("\nRemoved activities:")
        for activity_id in report.expired_activity_ids:
            print(f"- {report.expired_activity_names[activity_id]} ({activity_id})")
    if report.expired_time_options:
        print("\nRemoved occurrences:")
        for activity_id, option_id, occurrence_date in report.expired_time_options:
            print(f"- {activity_id}: {option_id} on {occurrence_date}")
    if report.expired_recurrence_rules:
        print("\nRemoved recurrence rules:")
        for activity_id, end_date in report.expired_recurrence_rules:
            print(f"- {activity_id}: ended {end_date}")
    print(f"\nWarnings: {len(report.warnings)}")
    for warning in report.warnings:
        print(f"- {warning}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="validate without maintenance")
    mode.add_argument("--dry-run", action="store_true", help="report maintenance without writing")
    mode.add_argument("--apply", action="store_true", help="apply maintenance and write events.json")
    parser.add_argument("--file", type=Path, default=EVENTS_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--date", dest="today", type=parse_date, help="override today with YYYY-MM-DD")
    parser.add_argument(
        "--max-removal",
        type=float,
        default=DEFAULT_MAX_REMOVAL,
        help=f"maximum removable activity proportion (default: {DEFAULT_MAX_REMOVAL:.0%})",
    )
    parser.add_argument(
        "--allow-large-removal",
        action="store_true",
        help="allow a reviewed cleanup above the safety threshold",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.max_removal <= 1:
        print("ERROR: --max-removal must be between 0 and 1", file=sys.stderr)
        return 1

    path = args.file.resolve()
    try:
        feed, original = load_feed(path)
        print(f"Validation PASSED: {len(feed['activities'])} activities")
        if args.validate_only:
            return 0

        today = args.today or datetime.now(ZoneInfo("Europe/London")).date()
        updated, report = maintain_feed(feed, today)
        print_report(report, today)
        enforce_safety(report, args.max_removal, args.allow_large_removal)
        validate_feed(updated)

        if not report.changed:
            print("\nNo event changes required.")
            return 0

        if not args.apply:
            print("\nDry run only; events.json was not changed.")
            return 0

        updated["updatedAt"] = utc_timestamp()
        validate_feed(updated)
        write_atomic(path, updated, original)
        print("\nValidation PASSED; events.json updated successfully.")
        return 0
    except (FeedValidationError, SafetyError, OSError, TypeError, KeyError, OverflowError) as exc:
        print(f"ERROR: {exc}. Maintenance stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

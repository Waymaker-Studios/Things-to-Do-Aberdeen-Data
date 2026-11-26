
#!/usr/bin/env python3
"""Validate and prune the version-1 Community Notice event feed (Python 3.9+)."""
import argparse
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

EVENTS_FILE = Path(__file__).resolve().parent.parent / 'events.json'
MAX_REMOVAL = 0.25
CATEGORY_INDICES = set(range(11))
WEEKDAYS = set('monday tuesday wednesday thursday friday saturday sunday'.split())
PLACEHOLDER_TIMES = {'Multiple', 'Opening time not listed'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parse_date(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value),
            f'Invalid date: {value!r}')
    return date.fromisoformat(value)


def nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def string_list(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def validate_time(value, where):
    require(nonempty_string(value), f'{where}: missing startTime')
    if value not in PLACEHOLDER_TIMES:
        require(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value),
                f'{where}: unsupported startTime {value!r}')


def validate_duration(value, where):
    require(value is None or (type(value) is int and value >= 0),
            f'{where}: invalid durationMinutes')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f'Invalid JSON numeric constant: {value}')


def validate_feed(feed):
    require(isinstance(feed, dict), 'Feed must be a JSON object')
    require(type(feed.get('version')) is int and feed['version'] == 1,
            'Unsupported feed version')
    stamp = feed.get('updatedAt')
    require(isinstance(stamp, str) and 'T' in stamp, 'Invalid updatedAt')
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'updatedAt must include a time zone')
    activities = feed.get('activities')
    require(isinstance(activities, list) and bool(activities),
            'Activities must be a non-empty array')
    seen = set()
    for activity in activities:
        require(isinstance(activity, dict), 'Activity must be an object')
        aid = activity.get('id')
        require(nonempty_string(aid), 'Missing activity id')
        require(aid not in seen, f'Duplicate activity id: {aid}')
        seen.add(aid)
        for key in ('activityId', 'activityName', 'venueName'):
            require(nonempty_string(activity.get(key)), f'{aid}: invalid {key}')
        for key in ('description', 'activityLink'):
            value = activity.get(key)
            require(value is None or isinstance(value, str), f'{aid}: invalid {key}')
        link = activity.get('activityLink')
        if link is not None:
            parts = urlsplit(link)
            require(parts.scheme in ('http', 'https') and bool(parts.netloc),
                    f'{aid}: invalid activityLink')
        booking = activity.get('bookingRequired')
        require(booking is None or type(booking) is bool, f'{aid}: invalid bookingRequired')
        for key in ('minimumPrice', 'maximumPrice'):
            value = activity.get(key)
            require(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0),
                    f'{aid}: invalid {key}')
        lo, hi = activity.get('minimumPrice'), activity.get('maximumPrice')
        require(lo is None or hi is None or lo <= hi, f'{aid}: minimumPrice exceeds maximumPrice')
        for key in ('requirements', 'tags'):
            require(string_list(activity.get(key)), f'{aid}: invalid {key}')
        require(activity.get('ageSuitability') in ('children', 'adultsOnly', 'allAges'),
                f'{aid}: invalid ageSuitability')
        cats = activity.get('categories')
        require(isinstance(cats, list) and all(type(i) is int and i in CATEGORY_INDICES for i in cats),
                f'{aid}: invalid categories')
        options = activity.get('timeOptions', [])
        rules = activity.get('recurrenceRules', [])
        require(isinstance(options, list) and isinstance(rules, list), f'{aid}: invalid schedule arrays')
        option_ids = set()
        for option in options:
            require(isinstance(option, dict), f'{aid}: invalid time option')
            oid = option.get('id')
            require(nonempty_string(oid) and oid not in option_ids, f'{aid}: missing or duplicate time option id {oid!r}')
            option_ids.add(oid)
            parse_date(option.get('date'))
            validate_time(option.get('startTime'), f'{aid}/{oid}')
            validate_duration(option.get('durationMinutes'), f'{aid}/{oid}')
        for rule in rules:
            require(isinstance(rule, dict), f'{aid}: invalid recurrence rule')
            require(rule.get('frequency') in ('weekly', 'monthly'), f'{aid}: unsupported recurrence frequency')
            days = rule.get('weekdays')
            require(string_list(days) and bool(days) and all(d in WEEKDAYS for d in days), f'{aid}: invalid weekdays')
            start = parse_date(rule.get('startDate'))
            end = rule.get('endDate')
            require(end is None or parse_date(end) >= start, f'{aid}: recurrence ends before start')
            interval = rule.get('interval')
            require(interval is None or (type(interval) is int and interval > 0), f'{aid}: invalid interval')
            week = rule.get('weekOfMonth')
            require(week is None or (type(week) is int and 1 <= week <= 5), f'{aid}: invalid weekOfMonth')
            validate_time(rule.get('startTime'), aid)
            validate_duration(rule.get('durationMinutes'), aid)


def maintain(feed, today):
    remaining = []
    removed = []
    option_count = rule_count = warnings = 0
    for activity in feed['activities']:
        options = activity.get('timeOptions', [])
        rules = activity.get('recurrenceRules', [])
        if not options and not rules:
            print(f"WARNING: {activity['id']} has no schedule; retained for review")
            warnings += 1
            remaining.append(activity)
            continue
        kept_options = [o for o in options if parse_date(o['date']) >= today]
        kept_rules = [r for r in rules if r.get('endDate') is None or parse_date(r['endDate']) >= today]
        for o in options:
            if parse_date(o['date']) < today:
                print(f"Expired occurrence: {activity['id']} / {o['id']} / {o['date']}")
                option_count += 1
        for r in rules:
            if r.get('endDate') is not None and parse_date(r['endDate']) < today:
                print(f"Expired recurrence: {activity['id']} / {r['endDate']}")
                rule_count += 1
        if not kept_options and not kept_rules:
            removed.append(activity['id'])
            print(f"Expired activity: {activity['activityName']} ({activity['id']})")
            continue
        updated = dict(activity)
        if 'timeOptions' in activity:
            updated['timeOptions'] = kept_options
        if 'recurrenceRules' in activity:
            updated['recurrenceRules'] = kept_rules
        remaining.append(updated)
    total = len(feed['activities'])
    print(f'Activities: {total} -> {len(remaining)}; removed {len(removed)}; '
          f'expired occurrences: {option_count}; expired rules: {rule_count}; warnings: {warnings}')
    require(bool(remaining), 'Cleanup would empty the feed; refusing to write')
    require(len(removed) / total <= MAX_REMOVAL,
            f'Cleanup exceeds {MAX_REMOVAL:.0%} activity removal limit; review required')
    result = dict(feed)
    result['activities'] = remaining
    return result


def atomic_write(path, feed, original):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.events-', suffix='.tmp', delete=False) as f:
            temp = Path(f.name)
            json.dump(feed, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, path.stat().st_mode & 0o777)
        require(path.read_bytes() == original, 'events.json changed during maintenance; refusing to overwrite')
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, default=EVENTS_FILE)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    try:
        path = args.file.resolve()
        original = path.read_bytes()
        feed = json.loads(original, object_pairs_hook=unique_object, parse_constant=reject_constant)
        validate_feed(feed)
        print(f'Validation PASSED: {len(feed["activities"])} activities')
        if args.validate_only:
            return 0
        today = datetime.now(ZoneInfo('Europe/London')).date()
        print(f'Maintenance date: {today} (Europe/London)')
        updated = maintain(feed, today)
        if updated == feed:
            print('No event changes required.')
            return 0
        updated['updatedAt'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        validate_feed(updated)
        atomic_write(path, updated, original)
        print('Validation PASSED; events.json updated successfully.')
        return 0
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as exc:
        print(f'ERROR: {exc}. Maintenance stopped.', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "maintain_events.py"
SPEC = importlib.util.spec_from_file_location("maintain_events", MODULE_PATH)
maintain_events = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = maintain_events
SPEC.loader.exec_module(maintain_events)


def activity(activity_id="activity-1", *, options=None, rules=None):
    return {
        "id": activity_id,
        "activityId": activity_id,
        "activityName": "Test activity",
        "description": None,
        "venueName": "Test venue",
        "minimumPrice": None,
        "maximumPrice": None,
        "activityLink": None,
        "bookingRequired": None,
        "requirements": [],
        "ageSuitability": "allAges",
        "categories": [0],
        "tags": ["test"],
        "timeOptions": options or [],
        "recurrenceRules": rules or [],
    }


def time_option(option_id, occurrence_date, start_time="10:00"):
    return {
        "id": option_id,
        "date": occurrence_date,
        "startTime": start_time,
        "durationMinutes": 60,
    }


def recurrence(start_date, end_date=None):
    return {
        "frequency": "weekly",
        "weekdays": ["monday"],
        "startDate": start_date,
        "endDate": end_date,
        "startTime": "09:00",
        "durationMinutes": 30,
    }


def feed(activities):
    return {
        "version": 1,
        "updatedAt": "2026-11-24T13:53:00Z",
        "activities": activities,
    }


class MaintainEventsTests(unittest.TestCase):
    def test_current_repository_feed_validates(self):
        loaded, _ = maintain_events.load_feed(ROOT / "events.json")
        self.assertEqual(loaded["version"], 1)
        self.assertEqual(len(loaded["activities"]), 233)

    def test_today_is_retained_but_past_explicit_occurrences_are_removed(self):
        source = feed([
            activity(
                options=[
                    time_option("past", "2026-11-25"),
                    time_option("today", "2026-11-26"),
                    time_option("future", "2026-11-27"),
                ]
            )
        ])

        updated, report = maintain_events.maintain_feed(source, date(2026, 11, 26))

        self.assertEqual(
            [option["id"] for option in updated["activities"][0]["timeOptions"]],
            ["today", "future"],
        )
        self.assertEqual(len(report.expired_time_options), 1)
        self.assertFalse(report.expired_activity_ids)

    def test_ongoing_and_future_recurrences_are_preserved(self):
        source = feed([
            activity(
                rules=[
                    recurrence("2026-01-01", "2026-11-25"),
                    recurrence("2026-01-01", None),
                ]
            )
        ])

        updated, report = maintain_events.maintain_feed(source, date(2026, 11, 26))

        self.assertEqual(len(updated["activities"]), 1)
        self.assertEqual(len(updated["activities"][0]["recurrenceRules"]), 1)
        self.assertIsNone(updated["activities"][0]["recurrenceRules"][0]["endDate"])
        self.assertEqual(len(report.expired_recurrence_rules), 1)
        self.assertEqual(report.ongoing_recurring_activities, 1)

    def test_expired_activity_is_removed_only_when_all_schedule_is_expired(self):
        source = feed([
            activity(
                "expired",
                options=[time_option("old", "2026-11-25")],
                rules=[recurrence("2026-01-01", "2026-11-25")],
            ),
            activity("retained", options=[time_option("today", "2026-11-26")]),
        ])

        updated, report = maintain_events.maintain_feed(source, date(2026, 11, 26))

        self.assertEqual([item["id"] for item in updated["activities"]], ["retained"])
        self.assertEqual(report.expired_activity_ids, ["expired"])

    def test_unscheduled_activity_is_retained_with_warning(self):
        source = feed([activity("needs-review")])

        updated, report = maintain_events.maintain_feed(source, date(2026, 11, 26))

        self.assertEqual(updated, source)
        self.assertEqual(len(report.warnings), 1)

    def test_unknown_fields_and_activity_order_are_preserved(self):
        first = activity(
            "first",
            options=[time_option("old", "2026-11-25"), time_option("future", "2026-11-27")],
        )
        first["futureField"] = {"keep": True}
        second = activity("second", options=[time_option("today", "2026-11-26")])
        source = feed([first, second])

        updated, _ = maintain_events.maintain_feed(source, date(2026, 11, 26))

        self.assertEqual([item["id"] for item in updated["activities"]], ["first", "second"])
        self.assertEqual(updated["activities"][0]["futureField"], {"keep": True})
        self.assertEqual(source["updatedAt"], "2026-11-24T13:53:00Z")

    def test_safety_threshold_rejects_large_removal(self):
        source = feed([
            activity("expired", options=[time_option("old", "2026-11-25")]),
            activity("retained", options=[time_option("today", "2026-11-26")]),
        ])
        _, report = maintain_events.maintain_feed(source, date(2026, 11, 26))

        with self.assertRaises(maintain_events.SafetyError):
            maintain_events.enforce_safety(report, 0.25, allow_large_removal=False)

    def test_dry_run_does_not_write_and_apply_updates_timestamp(self):
        source = feed([
            activity("expired", options=[time_option("old", "2026-11-25")]),
            activity("retained", options=[time_option("today", "2026-11-26")]),
        ])
        original = json.dumps(source, indent=2).encode() + b"\n"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_bytes(original)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    maintain_events.main([
                        "--dry-run",
                        "--date",
                        "2026-11-26",
                        "--file",
                        str(path),
                        "--allow-large-removal",
                    ]),
                    0,
                )
            self.assertEqual(path.read_bytes(), original)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    maintain_events.main([
                        "--apply",
                        "--date",
                        "2026-11-26",
                        "--file",
                        str(path),
                        "--allow-large-removal",
                    ]),
                    0,
                )
            updated = json.loads(path.read_text())
            self.assertEqual([item["id"] for item in updated["activities"]], ["retained"])
            self.assertNotEqual(updated["updatedAt"], source["updatedAt"])


if __name__ == "__main__":
    unittest.main()

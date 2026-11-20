"""Tests for Outlook multi-calendar enumeration and @odata.nextLink pagination."""

from datetime import datetime
from unittest.mock import patch

from gmail_auto import calendars, microsoft


def _event(event_id: str, subject: str, start_iso: str, end_iso: str) -> dict:
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": start_iso, "timeZone": "Pacific/Auckland"},
        "end": {"dateTime": end_iso, "timeZone": "Pacific/Auckland"},
        "location": {"displayName": "Room A"},
        "organizer": {"emailAddress": {"name": "Alice", "address": "alice@outlook.com"}},
        "isReminderOn": True,
        "webLink": f"https://outlook.office.com/calendar/{event_id}",
        "attendees": [],
    }


def test_outlook_events_reads_multiple_calendars_and_follows_next_link():
    calls = []

    def fake_graph(kind, method, path, params=None, **_kwargs):
        calls.append((kind, method, path, params))
        if path == "/me/calendars":
            return {
                "value": [{"id": "cal-1", "name": "Calendar", "isDefaultCalendar": True}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=c2",
            }
        if path == "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=c2":
            assert params is None
            return {"value": [{"id": "cal/2", "name": "Team"}]}
        if path == "/me/calendars/cal-1/calendarView":
            return {
                "value": [_event("e2", "Afternoon Sync", "2026-11-20T14:00:00", "2026-11-20T15:00:00")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView?$skiptoken=p2",
            }
        if path == "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView?$skiptoken=p2":
            assert params is None
            return {
                "value": [_event("e1", "Morning Standup", "2026-11-20T09:00:00", "2026-11-20T09:30:00")]
            }
        if path == "/me/calendars/cal%2F2/calendarView":
            return {
                "value": [
                    _event("e1", "Morning Standup", "2026-11-20T09:00:00", "2026-11-20T09:30:00"),
                    _event("e3", "Team Review", "2026-11-20T11:00:00", "2026-11-20T12:00:00"),
                ]
            }
        raise AssertionError(f"Unexpected path: {path}")

    start = datetime(2026, 11, 20, 0, 0, tzinfo=calendars.ZONE)
    end = datetime(2026, 11, 21, 0, 0, tzinfo=calendars.ZONE)
    with (
        patch("gmail_auto.outlook_client.OutlookClient.get_user_email", return_value="alice@outlook.com"),
        patch("gmail_auto.microsoft.graph", side_effect=fake_graph),
    ):
        events, account = calendars._outlook_events(start, end)

    assert account == "alice@outlook.com"
    assert [ev["id"] for ev in events] == ["e1", "e3", "e2"]
    assert [ev["title"] for ev in events] == ["Morning Standup", "Team Review", "Afternoon Sync"]


def test_outlook_events_falls_back_to_default_calendar_view_when_calendars_forbidden():
    def fake_graph(_kind, _method, path, params=None, **_kwargs):
        if path == "/me/calendars":
            raise microsoft.GraphError("Forbidden", 403)
        if path == "/me/calendarView":
            return {"value": [_event("e1", "Default Only", "2026-11-20T10:00:00", "2026-11-20T11:00:00")]}
        raise AssertionError(f"Unexpected path: {path}")

    start = datetime(2026, 11, 20, 0, 0, tzinfo=calendars.ZONE)
    end = datetime(2026, 11, 21, 0, 0, tzinfo=calendars.ZONE)
    with (
        patch("gmail_auto.outlook_client.OutlookClient.get_user_email", return_value="alice@outlook.com"),
        patch("gmail_auto.microsoft.graph", side_effect=fake_graph),
    ):
        events, account = calendars._outlook_events(start, end)

    assert account == "alice@outlook.com"
    assert [ev["id"] for ev in events] == ["e1"]


def test_outlook_events_skips_inaccessible_secondary_calendar():
    def fake_graph(_kind, _method, path, params=None, **_kwargs):
        if path == "/me/calendars":
            return {"value": [{"id": "cal-1"}, {"id": "cal-broken"}]}
        if path == "/me/calendars/cal-1/calendarView":
            return {"value": [_event("e1", "Main Event", "2026-11-20T10:00:00", "2026-11-20T11:00:00")]}
        if path == "/me/calendars/cal-broken/calendarView":
            raise microsoft.GraphError("Not found", 404)
        raise AssertionError(f"Unexpected path: {path}")

    start = datetime(2026, 11, 20, 0, 0, tzinfo=calendars.ZONE)
    end = datetime(2026, 11, 21, 0, 0, tzinfo=calendars.ZONE)
    with (
        patch("gmail_auto.outlook_client.OutlookClient.get_user_email", return_value="alice@outlook.com"),
        patch("gmail_auto.microsoft.graph", side_effect=fake_graph),
    ):
        events, account = calendars._outlook_events(start, end)

    assert account == "alice@outlook.com"
    assert [ev["id"] for ev in events] == ["e1"]

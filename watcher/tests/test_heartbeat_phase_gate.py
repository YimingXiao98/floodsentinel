"""Tests for phase-gated heartbeat due checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from watcher.cli import _mission_cycle_due


def test_early_watch_first_run_due():
    due, reason = _mission_cycle_due(
        {
            "current_event_phase": "early_watch",
            "heartbeat_interval_seconds": 800,
            "last_heartbeat_at": None,
        }
    )
    assert due is True
    assert "first_run" in reason


def test_elevated_first_run_due():
    due, reason = _mission_cycle_due(
        {
            "current_event_phase": "elevated_watch",
            "heartbeat_interval_seconds": 800,
            "last_heartbeat_at": None,
        }
    )
    assert due is True
    assert "first_run" in reason


def test_elevated_recent_heartbeat_skipped():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    due, reason = _mission_cycle_due(
        {
            "current_event_phase": "elevated_watch",
            "heartbeat_interval_seconds": 800,
            "last_heartbeat_at": recent,
        }
    )
    assert due is False
    assert "skipped" in reason


def test_active_incident_due_after_interval():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    due, reason = _mission_cycle_due(
        {
            "current_event_phase": "active_incident",
            "heartbeat_interval_seconds": 800,
            "last_heartbeat_at": old,
        }
    )
    assert due is True
    assert "due" in reason

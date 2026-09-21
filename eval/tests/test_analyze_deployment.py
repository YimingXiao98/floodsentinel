"""Focused tests for the deployment-log summarizer."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.analyze_deployment import (  # noqa: E402
    DeploymentLogParseError,
    iter_concatenated_json,
    summarize_deployment_log,
)


def _encoded(*objects):
    return "\n".join(json.dumps(value, indent=2) for value in objects).encode()


class TestConcatenatedJsonParser(unittest.TestCase):
    def test_parses_objects_across_tiny_chunks_and_braces_in_strings(self):
        payload = _encoded(
            {"run_at": "2026-01-01T00:00:00Z", "text": "literal }{ and \\\"quote"},
            {"ok": True, "nested": [{"value": 2}]},
        )
        got = list(iter_concatenated_json(io.BytesIO(payload), chunk_size=7))
        self.assertEqual(got[0]["text"], 'literal }{ and \\"quote')
        self.assertEqual(got[1]["nested"][0]["value"], 2)

    def test_rejects_a_truncated_final_object(self):
        with self.assertRaisesRegex(DeploymentLogParseError, "truncated JSON object"):
            list(iter_concatenated_json(io.BytesIO(b'{"ok": true}\n{"bad": '), chunk_size=4))

    def test_rejects_non_json_noise_between_objects(self):
        with self.assertRaisesRegex(DeploymentLogParseError, "expected a JSON object"):
            list(iter_concatenated_json(io.BytesIO(b'{"ok": true}\ntraceback')))


class TestDeploymentSummary(unittest.TestCase):
    def test_summarizes_activity_delivery_failures_counties_and_epochs(self):
        poll_one = {
            "ok": True,
            "run_at": "2026-01-01T00:00:00Z",
            "cron_interval_minutes": 15,
            "code_version": "a1",
            "missions_total": 1,
            "missions_ran": 1,
            "missions_skipped": 0,
            "missions_errored": 0,
            "missions_folded": [],
            "skipped": [],
            "errors": [],
            "results": [
                {
                    "mission_id": "mission-a",
                    "mission_spec": {
                        "county": "Alpha",
                        "state": "TX",
                        "fips": "48001",
                        "heartbeat_interval_seconds": 800,
                    },
                    "monitoring_summary": {"heartbeat_interval_seconds": 800},
                    "material_update": True,
                    "slack_delivery": {"ok": True, "messages_sent": 2},
                    "failed_fetches": [
                        {"source_id": "source-a", "failure_reason": "HTTP 503"}
                    ],
                    "county_flood_payload": {
                        "sources": {
                            "usgs": {
                                "fetch_errors": [
                                    {"source": "usgs_iv", "error": "no_iv_data"}
                                ]
                            }
                        }
                    },
                    "extraction_failures": [
                        {"source_id": "usgs-a", "failure_reason": "no_gauges"},
                        {"source_id": "nws-a", "failure_reason": "malformed"},
                    ],
                }
            ],
        }
        poll_two = {
            "ok": False,
            "run_at": "2026-01-01T00:15:00Z",
            "cron_interval_minutes": 15,
            "code_version": "b2",
            "missions_total": 2,
            "missions_ran": 1,
            "missions_skipped": 1,
            "missions_errored": 0,
            "missions_folded": [],
            "errors": [],
            "skipped": [
                {
                    "mission_id": "mission-b",
                    "county": "Beta",
                    "state": "WA",
                    "fips": "53001",
                    "heartbeat_interval_seconds": 21600,
                }
            ],
            "results": [
                {
                    "mission_id": "mission-a",
                    "mission_spec": {
                        "county": "Alpha",
                        "state": "TX",
                        "fips": "48001",
                        "heartbeat_interval_seconds": 800,
                    },
                    "monitoring_summary": {"heartbeat_interval_seconds": 21600},
                    "material_update": False,
                    "slack_delivery": {
                        "skipped": True,
                        "reason": "no_material_change",
                    },
                    "failed_fetches": [],
                    "county_flood_payload": {"sources": {}},
                    "extraction_failures": [],
                }
            ],
        }
        non_poll_error = {"error": "cli_error", "message": "missing database secret"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.log"
            path.write_bytes(_encoded(poll_one, non_poll_error, poll_two))
            summary = summarize_deployment_log(path)

        self.assertEqual(summary["polls"]["json_objects"], 3)
        self.assertEqual(summary["polls"]["heartbeat_polls"], 2)
        self.assertEqual(summary["polls"]["non_poll_errors_by_type"], {"cli_error": 1})
        self.assertEqual(summary["polls"]["time_window"]["duration_seconds"], 900.0)

        activity = summary["mission_activity"]
        self.assertEqual(activity["mission_opportunities"], 3)
        self.assertEqual(activity["due"], 2)
        self.assertEqual(activity["ran"], 2)
        self.assertEqual(activity["cadence_skips"], 1)

        delivery = summary["delivery"]
        self.assertEqual(delivery["report_deliveries"], 1)
        self.assertEqual(delivery["report_messages_sent"], 2)
        self.assertEqual(delivery["materiality_skips"], 1)

        fetch = summary["source_fetch_failures"]
        self.assertEqual(fetch["total"], 2)
        self.assertEqual(fetch["coordinator_failed_fetches"], 1)
        self.assertEqual(fetch["county_api_fetch_errors"], 1)
        self.assertEqual(fetch["by_reason"], {"HTTP 503": 1, "no_iv_data": 1})

        extraction = summary["extraction_failures"]
        self.assertEqual(extraction["total"], 2)
        self.assertEqual(extraction["no_gauges"], 1)

        by_fips = {row["fips"]: row for row in summary["counties"]}
        self.assertEqual(by_fips["48001"]["ran"], 2)
        self.assertEqual(by_fips["48001"]["source_fetch_failures"], 2)
        self.assertEqual(by_fips["53001"]["cadence_skips"], 1)

        epochs = summary["configuration_epochs"]
        self.assertEqual(epochs["observed_code_fields"], ["code_version"])
        self.assertEqual(len(epochs["global"]), 2)
        alpha_epochs = [
            epoch
            for epoch in epochs["mission_heartbeat"]
            if epoch["mission_id"] == "mission-a"
        ]
        self.assertEqual(
            [epoch["heartbeat_interval_seconds"] for epoch in alpha_epochs],
            [800, 21600],
        )

    def test_summary_is_reproducible_for_the_same_input(self):
        poll = {
            "ok": True,
            "run_at": "2026-01-01T00:00:00Z",
            "missions_total": 0,
            "missions_ran": 0,
            "missions_skipped": 0,
            "missions_errored": 0,
            "results": [],
            "skipped": [],
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.log"
            path.write_bytes(_encoded(poll))
            first = summarize_deployment_log(path)
            second = summarize_deployment_log(path)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

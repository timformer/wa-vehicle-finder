from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from urllib.error import URLError
import importlib.util
import json

script_spec = importlib.util.spec_from_file_location(
    "refresh_published_inventory",
    Path(__file__).resolve().parent.parent / "scripts" / "refresh-published-inventory.py",
)
refresh = importlib.util.module_from_spec(script_spec)
script_spec.loader.exec_module(refresh)


def raw_listing(vin):
    return {
        "vin": vin,
        "vehicle": {
            "vin": vin,
            "year": 2024,
            "make": "BMW",
            "model": "iX",
            "trim": "xDrive50",
        },
        "retailListing": {
            "dealer": "BMW Seattle",
            "city": "Seattle",
            "state": "WA",
            "used": True,
            "price": 50000,
            "miles": 10000,
        },
        "history": {},
    }


class PublishedInventoryRefreshTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.original_site_config_file = refresh.server.SITE_CONFIG_FILE
        refresh.server.SITE_CONFIG_FILE = root / "site-config.json"
        refresh.server.SITE_CONFIG_FILE.write_text(
            '{"make":"BMW","model":"iX","minimumYear":2022,'
            '"officialDealerNamePatterns":["BMW"]}',
            encoding="utf-8",
        )
        self.state_file = root / "refresh-state.json"
        self.snapshot_file = root / "inventory.json"
        self.cache_file = root / "cache.json"
        self.history_file = root / "inventory-history.json"
        self.snapshot_file.write_text(
            json.dumps(
                {
                    "updatedAt": "2026-09-08T12:00:00-07:00",
                    "lastRefreshDate": "2026-09-08",
                    "lastAttemptCalls": 5,
                    "apiTotal": 1,
                    "listingCount": 1,
                    "newCount": 0,
                    "removedCount": 0,
                    "listings": [
                        {
                            "vin": "WB523CF0000000001",
                            "firstSeenDate": "2026-09-08",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        refresh.server.SITE_CONFIG_FILE = self.original_site_config_file
        self.temporary.cleanup()

    def test_reservation_allows_only_one_attempt_per_day(self):
        self.assertTrue(refresh.reserve_refresh("2026-09-09", self.state_file))
        self.assertFalse(refresh.reserve_refresh("2026-09-09", self.state_file))
        self.assertFalse(refresh.reserve_refresh("2026-09-08", self.state_file))

    def test_failed_refresh_can_be_retried_same_day(self):
        refresh.write_json(
            self.state_file,
            {
                "lastAttemptDate": "2026-09-09",
                "lastAttemptCalls": 3,
                "lastError": "timed out",
            },
        )

        self.assertTrue(
            refresh.reserve_refresh(
                "2026-09-09",
                self.state_file,
                retry_failed=True,
            )
        )
        state = refresh.read_json(self.state_file, {})
        self.assertEqual(state["lastAttemptCalls"], 3)
        self.assertIsNone(state["lastError"])

    def test_retry_rejects_success_and_exhausted_daily_limit(self):
        refresh.write_json(
            self.state_file,
            {
                "lastAttemptDate": "2026-09-09",
                "lastAttemptCalls": 3,
                "lastError": None,
            },
        )
        self.assertFalse(
            refresh.reserve_refresh(
                "2026-09-09",
                self.state_file,
                retry_failed=True,
            )
        )

        refresh.write_json(
            self.state_file,
            {
                "lastAttemptDate": "2026-09-09",
                "lastAttemptCalls": 20,
                "lastError": "timed out",
            },
        )
        self.assertFalse(
            refresh.reserve_refresh(
                "2026-09-09",
                self.state_file,
                retry_failed=True,
            )
        )

    def test_reservation_honors_two_day_interval(self):
        refresh.server.SITE_CONFIG_FILE.write_text(
            '{"make":"BMW","model":"iX","minimumYear":2022,'
            '"refreshIntervalDays":2,"officialDealerNamePatterns":["BMW"]}',
            encoding="utf-8",
        )
        refresh.write_json(
            self.state_file,
            {"lastAttemptDate": "2026-09-08"},
        )

        self.assertFalse(refresh.reserve_refresh("2026-09-09", self.state_file))
        self.assertTrue(refresh.reserve_refresh("2026-09-10", self.state_file))

    def test_automated_refresh_uses_global_twenty_call_limit(self):
        self.assertEqual(refresh.MAX_CALLS_PER_VEHICLE_PER_DAY, 20)

    def test_successful_refresh_updates_snapshot_and_state(self):
        refresh.reserve_refresh("2026-09-09", self.state_file)
        calls = []

        def fetcher(_key, page, _config):
            calls.append(page)
            return {"total": 1, "data": [raw_listing("WB523CF0000000002")]}

        succeeded = refresh.refresh_snapshot(
            "2026-09-09",
            "test",
            state_file=self.state_file,
            snapshot_file=self.snapshot_file,
            cache_file=self.cache_file,
            history_file=self.history_file,
            fetcher=fetcher,
        )

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        snapshot = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        self.assertTrue(succeeded)
        self.assertEqual(calls, [1])
        self.assertEqual(state["lastSuccessfulRefreshDate"], "2026-09-09")
        self.assertEqual(snapshot["lastRefreshDate"], "2026-09-09")
        self.assertEqual(snapshot["listings"][0]["vin"], "WB523CF0000000002")
        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertEqual(history["snapshots"][0]["date"], "2026-09-09")
        self.assertIn("WB523CF0000000002", history["vehicles"])

    def test_failed_refresh_retains_previous_snapshot(self):
        refresh.reserve_refresh("2026-09-09", self.state_file)
        original_snapshot = self.snapshot_file.read_text(encoding="utf-8")

        def fetcher(_key, _page, _config):
            raise URLError("offline")

        succeeded = refresh.refresh_snapshot(
            "2026-09-09",
            "test",
            state_file=self.state_file,
            snapshot_file=self.snapshot_file,
            cache_file=self.cache_file,
            history_file=self.history_file,
            fetcher=fetcher,
        )

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertFalse(succeeded)
        self.assertEqual(state["lastAttemptCalls"], 1)
        self.assertIn("Could not reach Auto.dev", state["lastError"])
        self.assertEqual(self.snapshot_file.read_text(encoding="utf-8"), original_snapshot)
        self.assertFalse(self.history_file.exists())

    def test_retry_counts_prior_calls_toward_daily_limit(self):
        refresh.write_json(
            self.state_file,
            {
                "lastAttemptDate": "2026-09-09",
                "lastAttemptCalls": 8,
                "lastError": None,
            },
        )
        calls = []

        def fetcher(_key, page, _config):
            calls.append(page)
            return {"total": 1, "data": [raw_listing("WB523CF0000000002")]}

        succeeded = refresh.refresh_snapshot(
            "2026-09-09",
            "test",
            state_file=self.state_file,
            snapshot_file=self.snapshot_file,
            cache_file=self.cache_file,
            history_file=self.history_file,
            fetcher=fetcher,
        )

        state = refresh.read_json(self.state_file, {})
        self.assertTrue(succeeded)
        self.assertEqual(calls, [1])
        self.assertEqual(state["lastAttemptCalls"], 9)

    def test_implausibly_empty_refresh_retains_previous_snapshot(self):
        refresh.reserve_refresh("2026-09-09", self.state_file)
        original_listings = json.loads(self.snapshot_file.read_text(encoding="utf-8"))["listings"]

        def fetcher(_key, _page, _config):
            return {"total": 0, "data": []}

        succeeded = refresh.refresh_snapshot(
            "2026-09-09",
            "test",
            state_file=self.state_file,
            snapshot_file=self.snapshot_file,
            cache_file=self.cache_file,
            history_file=self.history_file,
            fetcher=fetcher,
        )

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        snapshot = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        self.assertFalse(succeeded)
        self.assertIn("at least 1 were required", state["lastError"])
        self.assertEqual(state["lastErrorCode"], "implausible-result")
        self.assertEqual(snapshot["listings"], original_listings)
        self.assertEqual(snapshot["refreshWarning"]["code"], "implausible-result")
        self.assertIn("temporarily low", snapshot["refreshWarning"]["message"])

    def test_empty_initial_refresh_can_be_allowed(self):
        refresh.write_json(
            self.snapshot_file,
            {
                "updatedAt": None,
                "lastRefreshDate": None,
                "listingCount": 0,
                "listings": [],
            },
        )
        refresh.reserve_refresh("2026-09-09", self.state_file)

        succeeded = refresh.refresh_snapshot(
            "2026-09-09",
            "test",
            state_file=self.state_file,
            snapshot_file=self.snapshot_file,
            cache_file=self.cache_file,
            history_file=self.history_file,
            config={
                "make": "Genesis",
                "model": "GV90",
                "minimumYear": 2026,
                "officialDealerNamePatterns": ["Genesis"],
                "allowEmptyInventory": True,
                "refresh": {"enabled": True, "intervalDays": 2},
            },
            fetcher=lambda _key, _page, _config: {"total": 0, "data": []},
        )

        state = refresh.read_json(self.state_file, {})
        snapshot = refresh.read_json(self.snapshot_file, {})
        self.assertTrue(succeeded)
        self.assertEqual(state["lastSuccessfulRefreshDate"], "2026-09-09")
        self.assertEqual(snapshot["lastRefreshDate"], "2026-09-09")
        self.assertEqual(snapshot["listingCount"], 0)

    def test_implausible_result_does_not_count_as_hard_failure(self):
        root = Path(self.temporary.name)
        config_file = root / "vehicles.json"
        vehicles_dir = root / "vehicles"
        config_file.write_text(
            json.dumps(
                {
                    "vehicles": [
                        {
                            "slug": "sparse",
                            "refresh": {"enabled": True, "intervalDays": 1},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        paths = refresh.vehicle_paths("sparse", vehicles_dir)
        paths["state"].parent.mkdir(parents=True, exist_ok=True)
        refresh.reserve_refresh("2026-09-09", paths["state"])
        refresh.write_json(
            paths["snapshot"],
            {"lastRefreshDate": "2026-09-08", "listings": [raw_listing("WB523CF0000000002")]},
        )

        def fetcher(_key, _page, _config):
            return {"total": 0, "data": []}

        succeeded, failed = refresh.refresh_all(
            "2026-09-09",
            "test",
            ["sparse"],
            config_file=config_file,
            vehicles_dir=vehicles_dir,
            fetcher=fetcher,
        )

        self.assertEqual(succeeded, [])
        self.assertEqual(failed, [])

    def test_global_call_limit_retains_snapshot_and_publishes_warning(self):
        refresh.reserve_refresh("2026-09-09", self.state_file)

        def oversized_fetcher(_key, _page, _config):
            return {
                "total": 2001,
                "data": [
                    raw_listing(f"WB523CF000000{i:04d}")
                    for i in range(100)
                ],
            }

        succeeded = refresh.refresh_snapshot(
            "2026-09-09",
            "test",
            state_file=self.state_file,
            snapshot_file=self.snapshot_file,
            cache_file=self.cache_file,
            history_file=self.history_file,
            fetcher=oversized_fetcher,
        )

        snapshot = refresh.read_json(self.snapshot_file, {})
        self.assertFalse(succeeded)
        self.assertEqual(snapshot["listingCount"], 1)
        self.assertEqual(snapshot["refreshWarning"]["code"], "daily-call-limit")
        self.assertIn("20-call daily limit", snapshot["refreshWarning"]["message"])
        self.assertFalse(self.history_file.exists())

    def test_reserve_all_uses_enabled_vehicle_cadences(self):
        root = Path(self.temporary.name)
        config_file = root / "vehicles.json"
        vehicles_dir = root / "vehicles"
        config_file.write_text(
            json.dumps(
                {
                    "vehicles": [
                        {
                            "slug": "daily",
                            "refresh": {
                                "enabled": True,
                                "intervalDays": 1,
                            },
                        },
                        {
                            "slug": "disabled",
                            "refresh": {
                                "enabled": False,
                                "intervalDays": 1,
                            },
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        refresh.write_json(
            vehicles_dir / "daily" / "refresh-state.json",
            {"lastAttemptDate": "2026-09-08"},
        )
        refresh.write_json(
            vehicles_dir / "disabled" / "refresh-state.json",
            {"lastAttemptDate": "2026-09-08"},
        )

        reserved = refresh.reserve_all(
            "2026-09-09",
            config_file=config_file,
            vehicles_dir=vehicles_dir,
        )

        self.assertEqual(reserved, ["daily"])
        disabled = refresh.read_json(
            vehicles_dir / "disabled" / "refresh-state.json",
            {},
        )
        self.assertEqual(disabled["lastAttemptDate"], "2026-09-08")

    def test_reserve_all_can_target_one_vehicle(self):
        root = Path(self.temporary.name)
        config_file = root / "vehicles.json"
        vehicles_dir = root / "vehicles"
        config_file.write_text(
            json.dumps(
                {
                    "vehicles": [
                        {
                            "slug": slug,
                            "refresh": {"enabled": True, "intervalDays": 1},
                        }
                        for slug in ("bmw-ix", "volvo-ex90")
                    ]
                }
            ),
            encoding="utf-8",
        )
        for slug in ("bmw-ix", "volvo-ex90"):
            refresh.write_json(
                vehicles_dir / slug / "refresh-state.json",
                {"lastAttemptDate": "2026-09-08"},
            )

        reserved = refresh.reserve_all(
            "2026-09-09",
            ["bmw-ix"],
            config_file=config_file,
            vehicles_dir=vehicles_dir,
        )

        self.assertEqual(reserved, ["bmw-ix"])
        untouched = refresh.read_json(
            vehicles_dir / "volvo-ex90" / "refresh-state.json",
            {},
        )
        self.assertEqual(untouched["lastAttemptDate"], "2026-09-08")

    def test_refresh_all_keeps_vehicle_files_isolated(self):
        root = Path(self.temporary.name)
        config_file = root / "vehicles.json"
        vehicles_dir = root / "vehicles"
        config = {
            "slug": "bmw-ix",
            "make": "BMW",
            "model": "iX",
            "minimumYear": 2022,
            "officialDealerNamePatterns": ["BMW"],
            "refresh": {
                "enabled": True,
                "intervalDays": 1,
            },
        }
        config_file.write_text(
            json.dumps({"vehicles": [config]}),
            encoding="utf-8",
        )
        paths = refresh.vehicle_paths("bmw-ix", vehicles_dir)
        refresh.write_json(paths["state"], {"lastAttemptDate": "2026-09-09"})
        refresh.write_json(
            paths["snapshot"],
            {
                "lastRefreshDate": "2026-09-08",
                "listingCount": 1,
                "listings": [
                    {
                        "vin": "WB523CF0000000001",
                        "firstSeenDate": "2026-09-08",
                    }
                ],
            },
        )
        refresh.write_json(paths["overrides"], {})

        succeeded, failed = refresh.refresh_all(
            "2026-09-09",
            "test",
            ["bmw-ix"],
            config_file=config_file,
            vehicles_dir=vehicles_dir,
            fetcher=lambda _key, _page, _config: {
                "total": 1,
                "data": [raw_listing("WB523CF0000000002")],
            },
        )

        self.assertEqual(succeeded, ["bmw-ix"])
        self.assertEqual(failed, [])
        snapshot = refresh.read_json(paths["snapshot"], {})
        self.assertEqual(snapshot["listings"][0]["vin"], "WB523CF0000000002")
        self.assertTrue(paths["history"].exists())


if __name__ == "__main__":
    main()

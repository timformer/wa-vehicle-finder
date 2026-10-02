from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from urllib.error import URLError
import importlib.util
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_dev import server

exporter_spec = importlib.util.spec_from_file_location(
    "export_auto_dev_snapshot",
    Path(__file__).resolve().parent.parent / "scripts" / "export-auto-dev-snapshot.py",
)
exporter = importlib.util.module_from_spec(exporter_spec)
exporter_spec.loader.exec_module(exporter)


def listing(vin, dealer="BMW Seattle"):
    return {
        "vin": vin,
        "vehicle": {
            "vin": vin,
            "year": 2024,
            "make": "BMW",
            "model": "iX",
            "trim": "xDrive50",
            "exteriorColor": "Blue",
            "interiorColor": "Black",
            "seats": 5,
            "drivetrain": "All-Wheel Drive",
            "fuel": "Electric",
        },
        "retailListing": {
            "dealer": dealer,
            "city": "Seattle",
            "state": "WA",
            "used": True,
            "price": 50000,
            "miles": 10000,
            "primaryImage": f"https://example.com/{vin}.jpg",
            "vdp": f"https://example.com/{vin}",
        },
        "history": {"oneOwner": True, "accidents": False, "usageType": "Personal Use"},
    }


class AutoDevCacheTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.original_cache_file = server.CACHE_FILE
        self.original_overrides_file = server.OVERRIDES_FILE
        self.original_site_config_file = server.SITE_CONFIG_FILE
        server.CACHE_FILE = Path(self.temporary.name) / "cache.json"
        server.OVERRIDES_FILE = Path(self.temporary.name) / "overrides.json"
        server.SITE_CONFIG_FILE = Path(self.temporary.name) / "site-config.json"
        server.SITE_CONFIG_FILE.write_text(
            '{"make":"BMW","model":"iX","minimumYear":2022,'
            '"officialDealerNamePatterns":["BMW"]}',
            encoding="utf-8",
        )

    def tearDown(self):
        server.CACHE_FILE = self.original_cache_file
        server.OVERRIDES_FILE = self.original_overrides_file
        server.SITE_CONFIG_FILE = self.original_site_config_file
        self.temporary.cleanup()

    def test_refresh_fetches_each_page_only_once_per_day(self):
        calls = []
        vins = [f"WB523CF0{i:09d}"[-17:] for i in range(45)]

        def fetcher(_key, page, _config):
            calls.append(page)
            start = (page - 1) * 20
            return {"total": 45, "data": [listing(vin) for vin in vins[start:start + 20]]}

        first = server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")
        same_day = server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")

        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(first["listingCount"], 45)
        self.assertEqual(first["newCount"], 0)
        self.assertEqual(same_day["lastAttemptCalls"], 3)

    def test_refresh_stops_before_exceeding_call_limit(self):
        calls = []

        def fetcher(_key, page, _config):
            calls.append(page)
            return {"total": 101, "data": [listing("WB523CF0000000001") for _ in range(20)]}

        cache = server.refresh_cache(
            fetcher=fetcher,
            date="2026-09-07",
            api_key="test",
            max_calls=5,
        )

        self.assertEqual(calls, [1])
        self.assertEqual(cache["lastAttemptCalls"], 1)
        self.assertIn("configured maximum is 5", cache["refreshError"])

    def test_year_partitioned_refresh_combines_each_configured_year(self):
        current_year = datetime.now().year
        server.SITE_CONFIG_FILE.write_text(
            json.dumps(
                {
                    "make": "Tesla",
                    "model": "Model Y",
                    "minimumYear": current_year,
                    "queryYearsSeparately": True,
                    "officialDealerNamePatterns": ["Tesla"],
                }
            ),
            encoding="utf-8",
        )
        years = []

        def fetcher(_key, _page, config):
            years.append(config["year"])
            item = listing(f"WB523CF0000000{config['year'] % 1000:03d}", dealer="Tesla Seattle")
            item["vehicle"].update({"make": "Tesla", "model": "Model Y", "year": config["year"]})
            return {"total": 1, "data": [item]}

        cache = server.refresh_cache(
            fetcher=fetcher,
            date="2026-09-08",
            api_key="test",
            max_calls=10,
        )

        self.assertEqual(years, [current_year, current_year + 1])
        self.assertEqual(cache["lastAttemptCalls"], 2)
        self.assertEqual(cache["listingCount"], 2)

    def test_year_partition_never_exceeds_call_limit(self):
        current_year = datetime.now().year
        server.SITE_CONFIG_FILE.write_text(
            json.dumps(
                {
                    "make": "Tesla",
                    "model": "Model Y",
                    "minimumYear": current_year,
                    "queryYearsSeparately": True,
                    "officialDealerNamePatterns": ["Tesla"],
                }
            ),
            encoding="utf-8",
        )
        years = []

        def fetcher(_key, _page, config):
            years.append(config["year"])
            return {"total": 0, "data": []}

        cache = server.refresh_cache(
            fetcher=fetcher,
            date="2026-09-08",
            api_key="test",
            max_calls=1,
        )

        self.assertEqual(years, [current_year])
        self.assertEqual(cache["lastAttemptCalls"], 1)
        self.assertIn("configured maximum of 1", cache["refreshError"])

    def test_year_partition_rejects_implausible_drop(self):
        current_year = datetime.now().year
        server.SITE_CONFIG_FILE.write_text(
            json.dumps(
                {
                    "make": "Tesla",
                    "model": "Model Y",
                    "minimumYear": current_year,
                    "queryYearsSeparately": True,
                    "officialDealerNamePatterns": ["Tesla"],
                }
            ),
            encoding="utf-8",
        )
        existing = server.empty_cache()
        existing.update(
            {
                "lastRefreshDate": "2026-09-07",
                "listingCount": 10,
                "listings": [
                    {"vin": f"WB523CF00000000{i:02d}", "year": current_year, "isNew": False}
                    for i in range(10)
                ],
            }
        )
        server.write_cache(existing)

        def fetcher(_key, _page, config):
            return {"total": 0, "data": []} if config["year"] == current_year else {
                "total": 0,
                "data": [],
            }

        cache = server.refresh_cache(
            fetcher=fetcher,
            date="2026-09-08",
            api_key="test",
            max_calls=10,
        )

        self.assertIn(f"Year {current_year} returned only 0 results", cache["refreshError"])
        self.assertEqual(cache["listingCount"], 10)

    def test_next_day_marks_only_unseen_vins_new(self):
        first_vins = ["WB523CF0000000001", "WB523CF0000000002"]
        next_vins = ["WB523CF0000000002", "WB523CF0000000003"]
        current = first_vins

        def fetcher(_key, _page, _config):
            return {"total": len(current), "data": [listing(vin) for vin in current]}

        server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")
        current = next_vins
        refreshed = server.refresh_cache(fetcher=fetcher, date="2026-09-08", api_key="test")

        self.assertEqual(refreshed["newCount"], 1)
        self.assertEqual(refreshed["removedCount"], 1)
        new_listing = next(item for item in refreshed["listings"] if item["isNew"])
        self.assertEqual(new_listing["vin"], "WB523CF0000000003")

    def test_older_date_cannot_replace_newer_attempt_marker(self):
        calls = []

        def fetcher(_key, page, _config):
            calls.append(page)
            return {"total": 1, "data": [listing("WB523CF0000000001")]}

        server.refresh_cache(fetcher=fetcher, date="2026-09-08", api_key="test")
        cache = server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")

        self.assertEqual(calls, [1])
        self.assertEqual(cache["lastAttemptDate"], "2026-09-08")

    def test_failed_next_day_refresh_clears_old_new_markers(self):
        current = ["WB523CF0000000001"]

        def fetcher(_key, _page, _config):
            return {"total": len(current), "data": [listing(vin) for vin in current]}

        server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")
        current.append("WB523CF0000000002")
        server.refresh_cache(fetcher=fetcher, date="2026-09-08", api_key="test")

        def failing_fetcher(_key, _page, _config):
            raise URLError("offline")

        failed = server.refresh_cache(fetcher=failing_fetcher, date="2026-09-09", api_key="test")

        self.assertEqual(failed["newCount"], 0)
        self.assertEqual(failed["removedCount"], 0)
        self.assertFalse(any(item["isNew"] for item in failed["listings"]))
        self.assertIn("Could not reach Auto.dev", failed["refreshError"])

    def test_dealer_names_differing_only_by_case_are_collapsed(self):
        items = [
            listing("WB523CF0000000001", dealer="bmw northwest"),
            listing("WB523CF0000000002", dealer="BMW Northwest"),
        ]

        def fetcher(_key, _page, _config):
            return {"total": len(items), "data": items}

        cache = server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")

        self.assertEqual({item["dealer"] for item in cache["listings"]}, {"BMW Northwest"})
        self.assertTrue(all(item["officialBrandDealer"] for item in cache["listings"]))

    def test_known_independent_dealer_uses_canonical_name(self):
        items = [listing("WB523CF0000000001", dealer="jaguar land rover bellevue")]

        def fetcher(_key, _page, _config):
            return {"total": 1, "data": items}

        cache = server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")

        self.assertEqual(cache["listings"][0]["dealer"], "Jaguar Land Rover Bellevue")
        self.assertFalse(cache["listings"][0]["officialBrandDealer"])

    def test_site_configuration_filters_make_model_and_minimum_year(self):
        config = {
            "make": "Tesla",
            "model": "Model Y",
            "minimumYear": 2022,
            "officialDealerNamePatterns": ["tesla"],
        }
        matching = listing("WB523CF0000000001", dealer="Tesla Seattle")
        matching["vehicle"].update({"make": "Tesla", "model": "Model Y", "year": 2022})
        too_old = listing("WB523CF0000000002")
        too_old["vehicle"].update({"make": "Tesla", "model": "Model Y", "year": 2021})

        cleaned = server.clean_listing(matching, config)

        self.assertEqual(cleaned["make"], "Tesla")
        self.assertEqual(cleaned["model"], "Model Y")
        self.assertTrue(cleaned["officialBrandDealer"])
        self.assertIsNone(server.clean_listing(too_old, config))

    def test_refresh_combines_multiple_provider_model_names(self):
        server.SITE_CONFIG_FILE.write_text(
            json.dumps(
                {
                    "make": "Genesis",
                    "model": "GV70",
                    "queryModels": ["GV70", "Electrified GV70"],
                    "minimumYear": 2022,
                    "officialDealerNamePatterns": ["Genesis"],
                }
            ),
            encoding="utf-8",
        )
        calls = []

        def fetcher(_key, _page, config):
            calls.append(config["model"])
            item = listing(
                "KMUMADTB1NU000001"
                if config["model"] == "GV70"
                else "5NMMCET10PH000001",
                dealer="Genesis of Kirkland",
            )
            item["vehicle"].update(
                {
                    "make": "Genesis",
                    "model": config["model"],
                    "fuel": (
                        "Gasoline"
                        if config["model"] == "GV70"
                        else "Electric"
                    ),
                }
            )
            return {"total": 1, "data": [item]}

        cache = server.refresh_cache(
            fetcher=fetcher,
            date="2026-09-07",
            api_key="test",
        )

        self.assertEqual(calls, ["GV70", "Electrified GV70"])
        self.assertEqual(cache["listingCount"], 2)
        self.assertEqual({item["model"] for item in cache["listings"]}, {"GV70"})
        self.assertEqual(
            {item["powertrain"] for item in cache["listings"]},
            {"Gasoline", "Electric"},
        )

    def test_site_configuration_filters_body_style(self):
        config = {
            "make": "Mercedes-Benz",
            "model": "EQE",
            "bodyStyle": "SUV",
            "minimumYear": 2022,
            "officialDealerNamePatterns": ["Mercedes"],
        }
        suv = listing("WB523CF0000000001", dealer="Mercedes-Benz of Seattle")
        suv["vehicle"].update({"make": "Mercedes-Benz", "model": "EQE", "bodyStyle": "SUV"})
        sedan = listing("WB523CF0000000002")
        sedan["vehicle"].update({"make": "Mercedes-Benz", "model": "EQE", "bodyStyle": "Sedan"})

        self.assertIsNotNone(server.clean_listing(suv, config))
        self.assertIsNone(server.clean_listing(sedan, config))

    def test_clean_listing_normalizes_seats_and_drivetrain(self):
        item = listing("WB523CF0000000001")

        cleaned = server.clean_listing(item)

        self.assertEqual(cleaned["seats"], 5)
        self.assertEqual(cleaned["drivetrain"], "AWD")
        self.assertEqual(cleaned["powertrain"], "Electric")

    def test_vehicle_field_normalizers_reject_unknown_values(self):
        self.assertEqual(server.normalize_seats("7"), 7)
        self.assertIsNone(server.normalize_seats("unknown"))
        self.assertIsNone(server.normalize_seats(0))
        self.assertEqual(server.normalize_drivetrain("Dual Motor All Wheel Drive"), "AWD")
        self.assertEqual(server.normalize_drivetrain("rear-wheel drive"), "RWD")
        self.assertEqual(server.normalize_drivetrain("4x4"), "4WD")
        self.assertIsNone(server.normalize_drivetrain("unknown"))
        self.assertEqual(server.normalize_powertrain("Battery Electric"), "Electric")
        self.assertEqual(server.normalize_powertrain("Plug-in Hybrid"), "Plug-in hybrid")
        self.assertEqual(server.normalize_powertrain("Gasoline"), "Gasoline")
        self.assertEqual(
            server.normalize_powertrain("Gasoline", "Turbo E-Hybrid"),
            "Plug-in hybrid",
        )
        self.assertIsNone(server.normalize_powertrain(None))

    def test_site_configuration_filters_powertrain(self):
        config = {
            "make": "Porsche",
            "model": "Macan",
            "minimumYear": 2022,
            "allowedPowertrains": ["Electric"],
            "officialDealerNamePatterns": ["Porsche"],
        }
        electric = listing("WB523CF0000000001", dealer="Porsche Bellevue")
        electric["vehicle"].update({"make": "Porsche", "model": "Macan", "fuel": "Electric"})
        gasoline = listing("WB523CF0000000002", dealer="Porsche Bellevue")
        gasoline["vehicle"].update({"make": "Porsche", "model": "Macan", "fuel": "Gasoline"})

        self.assertIsNotNone(server.clean_listing(electric, config))
        self.assertIsNone(server.clean_listing(gasoline, config))

    def test_verified_listing_override_reconciles_cpo_and_dealer_url(self):
        vin = "WB523CF0000000001"
        server.OVERRIDES_FILE.write_text(
            '{"WB523CF0000000001":{"cpo":true,"url":"https://dealer.example/vehicle"}}',
            encoding="utf-8",
        )

        def fetcher(_key, _page, _config):
            return {"total": 1, "data": [listing(vin)]}

        cache = server.refresh_cache(fetcher=fetcher, date="2026-09-07", api_key="test")
        corrected = cache["listings"][0]

        self.assertTrue(corrected["cpo"])
        self.assertEqual(corrected["url"], "https://dealer.example/vehicle")

    def test_snapshot_override_rejects_unapproved_fields_and_insecure_url(self):
        original = {"vin": "WB523CF0000000001", "price": 50000, "cpo": False, "url": "https://dealer.example"}
        override = {"vin": "ALTERED", "price": 1, "cpo": True, "url": "http://insecure.example"}

        corrected = exporter.apply_override(original, override)

        self.assertEqual(corrected["vin"], original["vin"])
        self.assertEqual(corrected["price"], original["price"])
        self.assertTrue(corrected["cpo"])
        self.assertEqual(corrected["url"], original["url"])

    def test_verified_null_url_override_suppresses_stale_dealer_link(self):
        original = {"vin": "WB523CF0000000001", "url": "https://dealer.example/stale"}

        corrected = exporter.apply_override(original, {"url": None})

        self.assertIsNone(corrected["url"])


if __name__ == "__main__":
    main()

from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import date as calendar_date
from pathlib import Path
import importlib.util
import json
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CONFIG_FILE = ROOT / "data" / "vehicles.json"
VEHICLES_DIR = ROOT / "vehicles"
MAX_CALLS_PER_VEHICLE_PER_DAY = 20
MINIMUM_RETENTION_RATIO = 0.5

from auto_dev import server
from scripts import inventory_history

exporter_spec = importlib.util.spec_from_file_location(
    "export_auto_dev_snapshot",
    ROOT / "scripts" / "export-auto-dev-snapshot.py",
)
exporter = importlib.util.module_from_spec(exporter_spec)
exporter_spec.loader.exec_module(exporter)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def set_output(name, value):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("a", encoding="utf-8") as output:
            output.write(f"{name}={str(value).lower()}\n")


def read_vehicle_config(config_file=CONFIG_FILE):
    config = read_json(config_file, {})
    vehicles = config.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        raise ValueError("Vehicle configuration requires a non-empty vehicles list.")
    slugs = [vehicle.get("slug") for vehicle in vehicles]
    if any(not isinstance(slug, str) or not slug for slug in slugs):
        raise ValueError("Every configured vehicle requires a slug.")
    if len(slugs) != len(set(slugs)):
        raise ValueError("Vehicle slugs must be unique.")
    if any("maxApiCalls" in vehicle.get("refresh", {}) for vehicle in vehicles):
        raise ValueError(
            "Per-vehicle maxApiCalls is not supported; all vehicles use the global limit."
        )
    return config


def configured_vehicle(slug, config_file=CONFIG_FILE):
    for vehicle in read_vehicle_config(config_file)["vehicles"]:
        if vehicle["slug"] == slug:
            return vehicle
    raise ValueError(f"Unknown vehicle slug: {slug}")


def vehicle_paths(slug, vehicles_dir=VEHICLES_DIR):
    directory = vehicles_dir / slug
    return {
        "state": directory / "refresh-state.json",
        "snapshot": directory / "inventory.json",
        "cache": directory / "auto-dev-cache.json",
        "history": directory / "inventory-history.json",
        "overrides": directory / "listing-overrides.json",
    }


@contextmanager
def server_vehicle_context(config, paths):
    original = {
        "config": server.SITE_CONFIG_OVERRIDE,
        "cache": server.CACHE_FILE,
        "overrides": server.OVERRIDES_FILE,
        "export_source": exporter.SOURCE,
        "export_destination": exporter.DESTINATION,
        "export_overrides": exporter.OVERRIDES,
    }
    server.SITE_CONFIG_OVERRIDE = config
    server.CACHE_FILE = paths["cache"]
    server.OVERRIDES_FILE = paths["overrides"]
    exporter.SOURCE = paths["cache"]
    exporter.DESTINATION = paths["snapshot"]
    exporter.OVERRIDES = paths["overrides"]
    try:
        yield
    finally:
        server.SITE_CONFIG_OVERRIDE = original["config"]
        server.CACHE_FILE = original["cache"]
        server.OVERRIDES_FILE = original["overrides"]
        exporter.SOURCE = original["export_source"]
        exporter.DESTINATION = original["export_destination"]
        exporter.OVERRIDES = original["export_overrides"]


def reserve_refresh(date, state_file, config=None, retry_failed=False):
    config = config or server.read_site_config()
    state = read_json(state_file, {})
    last_attempt = state.get("lastAttemptDate")
    prior_calls = state.get("lastAttemptCalls", 0)
    can_retry = (
        retry_failed
        and last_attempt == date
        and bool(state.get("lastError"))
        and isinstance(prior_calls, int)
        and prior_calls < MAX_CALLS_PER_VEHICLE_PER_DAY
    )
    interval_days = config["refresh"].get(
        "intervalDays",
        config.get("refreshIntervalDays", 1),
    ) if config.get("refresh") else config.get("refreshIntervalDays", 1)
    if not isinstance(interval_days, int) or interval_days < 1:
        raise ValueError("refresh.intervalDays must be a positive integer.")
    days_since_attempt = (
        (calendar_date.fromisoformat(date) - calendar_date.fromisoformat(last_attempt)).days
        if last_attempt
        else interval_days
    )
    should_refresh = can_retry or days_since_attempt >= interval_days
    if should_refresh:
        state["lastAttemptDate"] = date
        state["lastError"] = None
        if not can_retry:
            state["lastAttemptCalls"] = 0
        write_json(state_file, state)
        if can_retry:
            print(
                f"Reserved a failed refresh retry for {date}; "
                f"{prior_calls} of {MAX_CALLS_PER_VEHICLE_PER_DAY} calls already used."
            )
        else:
            print(f"Reserved the inventory refresh for {date}.")
    else:
        print(
            f"Inventory refresh last attempted on {last_attempt}; the configured "
            f"{interval_days}-day interval has not elapsed."
        )
    return should_refresh


def reserve_all(
    date,
    requested_slugs=None,
    retry_failed=False,
    config_file=CONFIG_FILE,
    vehicles_dir=VEHICLES_DIR,
):
    requested = set(requested_slugs or [])
    configured = read_vehicle_config(config_file)["vehicles"]
    configured_slugs = {config["slug"] for config in configured}
    unknown = requested - configured_slugs
    if unknown:
        raise ValueError(f"Unknown vehicle slug(s): {', '.join(sorted(unknown))}")
    reserved = []
    for config in configured:
        if requested and config["slug"] not in requested:
            continue
        if not config.get("refresh", {}).get("enabled", False):
            continue
        paths = vehicle_paths(config["slug"], vehicles_dir)
        if reserve_refresh(date, paths["state"], config, retry_failed=retry_failed):
            reserved.append(config["slug"])
    set_output("should_refresh", bool(reserved))
    set_output("reserved_vehicles", ",".join(reserved))
    print(f"Reserved vehicles: {', '.join(reserved) if reserved else 'none'}")
    return reserved


def cache_from_snapshot(snapshot, state):
    listings = list(snapshot.get("listings") or [])
    known_vins = dict(state.get("knownVins") or {})
    for listing in listings:
        vin = listing.get("vin")
        if vin:
            known_vins.setdefault(
                vin,
                listing.get("firstSeenDate") or snapshot.get("lastRefreshDate"),
            )

    cache = server.empty_cache()
    cache.update(
        {
            "updatedAt": snapshot.get("updatedAt"),
            "lastRefreshDate": snapshot.get("lastRefreshDate"),
            "lastAttemptDate": state.get("lastSuccessfulRefreshDate"),
            "lastAttemptCalls": 0,
            "apiTotal": snapshot.get("apiTotal", len(listings)),
            "listingCount": len(listings),
            "newCount": snapshot.get("newCount", 0),
            "removedCount": snapshot.get("removedCount", 0),
            "listings": listings,
            "knownVins": known_vins,
        }
    )
    return cache


def refresh_snapshot(
    date,
    api_key,
    state_file,
    snapshot_file,
    cache_file,
    history_file,
    config=None,
    overrides_file=None,
    fetcher=None,
):
    config = config or server.read_site_config()
    paths = {
        "state": state_file,
        "snapshot": snapshot_file,
        "cache": cache_file,
        "history": history_file,
        "overrides": overrides_file or state_file.parent / "listing-overrides.json",
    }
    state = read_json(state_file, {})
    if state.get("lastAttemptDate") != date:
        raise RuntimeError(f"The {date} refresh was not reserved before API access.")
    calls_already_used = state.get("lastAttemptCalls", 0)
    if not isinstance(calls_already_used, int) or calls_already_used < 0:
        raise ValueError("lastAttemptCalls must be a non-negative integer.")
    remaining_calls = MAX_CALLS_PER_VEHICLE_PER_DAY - calls_already_used
    if remaining_calls <= 0:
        state["lastError"] = (
            f"The global {MAX_CALLS_PER_VEHICLE_PER_DAY}-call daily limit "
            "has already been reached."
        )
        state["lastErrorCode"] = "daily-call-limit"
        write_json(state_file, state)
        print(f"Refresh failed after {calls_already_used} calls: {state['lastError']}")
        return False

    snapshot = read_json(snapshot_file, {})
    previous_count = len(snapshot.get("listings") or [])
    write_json(cache_file, cache_from_snapshot(snapshot, state))

    with server_vehicle_context(config, paths):
        refreshed = server.refresh_cache(
            fetcher=fetcher,
            date=date,
            api_key=api_key,
            max_calls=remaining_calls,
        )
        state["lastAttemptCalls"] = (
            calls_already_used + refreshed.get("lastAttemptCalls", 0)
        )
        state["lastError"] = refreshed.get("refreshError")
        refreshed_count = refreshed.get("listingCount", 0)
        minimum_count = (
            0
            if previous_count == 0 and config.get("allowEmptyInventory")
            else max(1, int(previous_count * MINIMUM_RETENTION_RATIO))
        )
        plausible_count = refreshed_count >= minimum_count
        implausible_result = not refreshed.get("refreshError") and not plausible_count
        if implausible_result:
            state["lastError"] = (
                f"Refresh returned only {refreshed_count} listings; "
                f"at least {minimum_count} were required."
            )
        success = (
            not state["lastError"]
            and refreshed.get("lastRefreshDate") == date
            and plausible_count
        )
        # lastErrorCode distinguishes expected, already-handled conditions (prior
        # snapshot retained, warning published) from genuine fetch/API failures,
        # so callers can avoid alerting on noise from naturally sparse inventory.
        state["lastErrorCode"] = None
        if success:
            state["lastSuccessfulRefreshDate"] = date
            state["knownVins"] = refreshed.get("knownVins", {})
            exporter.export_snapshot()
            inventory_history.write_history(snapshot_file, history_file)
        elif "configured maximum" in str(state["lastError"]):
            state["lastErrorCode"] = "daily-call-limit"
            snapshot["refreshWarning"] = {
                "date": date,
                "code": "daily-call-limit",
                "message": (
                    f"The latest refresh required more than the global "
                    f"{MAX_CALLS_PER_VEHICLE_PER_DAY}-call daily limit. "
                    "Inventory may be incomplete; the last successful snapshot is shown."
                ),
            }
            write_json(snapshot_file, snapshot)
        elif implausible_result:
            state["lastErrorCode"] = "implausible-result"
            snapshot["refreshWarning"] = {
                "date": date,
                "code": "implausible-result",
                "message": (
                    f"The latest refresh returned only {refreshed_count} listings, "
                    f"fewer than the {minimum_count} expected from the prior snapshot. "
                    "Inventory may be temporarily low; the last successful snapshot is shown."
                ),
            }
            write_json(snapshot_file, snapshot)
        write_json(state_file, state)

    if success:
        print(
            f"Published {refreshed['listingCount']} listings from "
            f"{refreshed['lastAttemptCalls']} API calls."
        )
    else:
        print(f"Refresh failed after {state['lastAttemptCalls']} calls: {state['lastError']}")
    return success


def refresh_all(
    date,
    api_key,
    slugs,
    config_file=CONFIG_FILE,
    vehicles_dir=VEHICLES_DIR,
    fetcher=None,
):
    configs = {
        config["slug"]: config
        for config in read_vehicle_config(config_file)["vehicles"]
    }
    # Codes for conditions that are already safely handled (prior snapshot
    # retained, warning published) and don't warrant failing the workflow run.
    expected_error_codes = {"daily-call-limit", "implausible-result"}
    succeeded = []
    warned = []
    failed = []
    for slug in slugs:
        config = configs.get(slug)
        if config is None or not config.get("refresh", {}).get("enabled", False):
            raise ValueError(f"Vehicle is not enabled for refresh: {slug}")
        paths = vehicle_paths(slug, vehicles_dir)
        print(f"Refreshing {slug}...")
        if refresh_snapshot(
            date,
            api_key,
            state_file=paths["state"],
            snapshot_file=paths["snapshot"],
            cache_file=paths["cache"],
            history_file=paths["history"],
            overrides_file=paths["overrides"],
            config=config,
            fetcher=fetcher,
        ):
            succeeded.append(slug)
        else:
            state = read_json(paths["state"], {})
            if state.get("lastErrorCode") in expected_error_codes:
                warned.append(slug)
            else:
                failed.append(slug)
    set_output("refresh_succeeded", bool(succeeded))
    set_output("refresh_warned", bool(warned))
    set_output("refresh_failed", bool(failed))
    set_output("refreshed_vehicles", ",".join(succeeded))
    print(f"Successful vehicles: {', '.join(succeeded) if succeeded else 'none'}")
    print(f"Warned vehicles (retained prior snapshot, no action needed): "
          f"{', '.join(warned) if warned else 'none'}")
    print(f"Failed vehicles: {', '.join(failed) if failed else 'none'}")
    return succeeded, failed


def main():
    parser = ArgumentParser()
    parser.add_argument("operation", choices=("reserve-all", "refresh-all"))
    parser.add_argument("--date", required=True)
    parser.add_argument("--vehicles", default="")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    if args.operation == "reserve-all":
        slugs = [slug for slug in args.vehicles.split(",") if slug]
        reserve_all(args.date, slugs, retry_failed=args.retry_failed)
        return

    slugs = [slug for slug in args.vehicles.split(",") if slug]
    refresh_all(args.date, os.environ.get("AUTO_DEV_API_KEY"), slugs)


if __name__ == "__main__":
    main()

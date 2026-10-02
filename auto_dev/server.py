from datetime import datetime, time, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from contextlib import contextmanager
import json
import math
import msvcrt
import os
import threading

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VEHICLE_SLUG = os.environ.get("VEHICLE_SLUG", "bmw-ix")
VEHICLE_DIR = ROOT / "vehicles" / DEFAULT_VEHICLE_SLUG
CACHE_FILE = VEHICLE_DIR / "auto-dev-cache.json"
OVERRIDES_FILE = VEHICLE_DIR / "listing-overrides.json"
SITE_CONFIG_FILE = ROOT / "data" / "vehicles.json"
SITE_CONFIG_OVERRIDE = None
INSTANCE_FILE = ROOT / "data" / "auto-dev-server.lock"
PORT = int(os.environ.get("AUTO_DEV_PORT", "4174"))
REFRESH_LOCK = threading.Lock()
BMW_DEALERS = {
    "bmw northwest": "BMW Northwest",
    "bmw of bellevue": "BMW of Bellevue",
    "bmw of lynnwood": "BMW of Lynnwood",
    "bmw of spokane": "BMW of Spokane",
    "bmw of tri-cities": "BMW of Tri-Cities",
    "bmw seattle": "BMW Seattle",
}
CANONICAL_DEALERS = {
    **BMW_DEALERS,
    "carmax lynnwood": "CarMax Lynnwood",
    "elliot bay ineos grenadier": "Elliot Bay Ineos Grenadier",
    "infiniti of lynnwood": "Infiniti of Lynnwood",
    "j & a auto sales": "J & A Auto Sales",
    "jaguar land rover bellevue": "Jaguar Land Rover Bellevue",
    "landmark motors inc": "Landmark Motors Inc",
    "lithia chrysler dodge jeep ram fiat of spokane": "Lithia Chrysler Dodge Jeep Ram FIAT of Spokane",
    "mercedes-benz of seattle": "Mercedes-Benz of Seattle",
    "platinum auto sales inc": "Platinum Auto Sales Inc",
    "seattle finest motors llc": "Seattle Finest Motors LLC",
    "swickard toyota": "Swickard Toyota",
    "volkswagen of kirkland": "Volkswagen of Kirkland",
    "volkswagen of puyallup": "Volkswagen of Puyallup",
    "volvo cars bellevue": "Volvo Cars Bellevue",
    "windy chevrolet": "Windy Chevrolet",
}
DEFAULT_SITE_CONFIG = {
    "make": "BMW",
    "model": "iX",
    "minimumYear": 2022,
    "refreshIntervalDays": 1,
    "officialDealerNamePatterns": ["bmw"],
}


def today_local():
    return datetime.now().astimezone().date().isoformat()


def now_local():
    return datetime.now().astimezone().isoformat()


def empty_cache():
    return {
        "updatedAt": None,
        "lastRefreshDate": None,
        "lastAttemptDate": None,
        "lastAttemptCalls": 0,
        "refreshError": None,
        "apiTotal": 0,
        "listingCount": 0,
        "newCount": 0,
        "removedCount": 0,
        "rejectedCount": 0,
        "listings": [],
        "knownVins": {},
    }


def read_site_config():
    if SITE_CONFIG_OVERRIDE is not None:
        value = SITE_CONFIG_OVERRIDE
    else:
        try:
            value = json.loads(SITE_CONFIG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            value = {}
    if isinstance(value.get("vehicles"), list):
        value = next(
            (
                vehicle
                for vehicle in value["vehicles"]
                if vehicle.get("slug") == DEFAULT_VEHICLE_SLUG
            ),
            {},
        )
    refresh = value.get("refresh") or {}
    value = {
        **value,
        "refreshIntervalDays": refresh.get(
            "intervalDays",
            value.get("refreshIntervalDays", DEFAULT_SITE_CONFIG["refreshIntervalDays"]),
        ),
    }
    config = {**DEFAULT_SITE_CONFIG, **value}
    if not isinstance(config["make"], str) or not config["make"].strip():
        raise ValueError("Site configuration requires a make.")
    if not isinstance(config["model"], str) or not config["model"].strip():
        raise ValueError("Site configuration requires a model.")
    if not isinstance(config["minimumYear"], int):
        raise ValueError("Site configuration requires an integer minimumYear.")
    if not isinstance(config["refreshIntervalDays"], int) or config["refreshIntervalDays"] < 1:
        raise ValueError("refreshIntervalDays must be a positive integer.")
    patterns = config.get("officialDealerNamePatterns")
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise ValueError("officialDealerNamePatterns must be a list of strings.")
    return config


def read_cache():
    try:
        value = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cache = {**empty_cache(), **value}
        normalize_dealer_names(cache.get("listings", []))
        apply_listing_overrides(cache.get("listings", []))
        return cache
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return empty_cache()


def write_cache(value):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CACHE_FILE)


def prepare_lock_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if os.fstat(handle.fileno()).st_size == 0:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    return handle


@contextmanager
def refresh_file_lock():
    lock_file = CACHE_FILE.with_suffix(".lock")
    with prepare_lock_file(lock_file) as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def single_instance_lock():
    with prepare_lock_file(INSTANCE_FILE) as handle:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError(f"Auto.dev cache server is already running on port {PORT}.") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def clear_daily_changes(cache):
    cache["newCount"] = 0
    cache["removedCount"] = 0
    for listing in cache.get("listings", []):
        listing["isNew"] = False


def valid_https_url(value):
    return value if isinstance(value, str) and urlparse(value).scheme == "https" else None


def normalize_seats(value):
    try:
        seats = int(value)
    except (TypeError, ValueError):
        return None
    return seats if 1 <= seats <= 20 else None


def normalize_drivetrain(value):
    normalized = str(value or "").strip().casefold().replace("_", " ").replace("-", " ")
    if not normalized:
        return None
    if (
        normalized in {"awd", "all wheel drive"}
        or "all wheel drive" in normalized
        or "dual motor" in normalized
    ):
        return "AWD"
    if normalized in {"4wd", "4x4", "four wheel drive"} or "four wheel drive" in normalized:
        return "4WD"
    if normalized in {"rwd", "rear wheel drive"} or "rear wheel drive" in normalized:
        return "RWD"
    if normalized in {"fwd", "front wheel drive"} or "front wheel drive" in normalized:
        return "FWD"
    return None


def normalize_powertrain(value, trim=None):
    normalized = str(value or "").strip().casefold().replace("_", " ").replace("-", " ")
    normalized_trim = str(trim or "").strip().casefold().replace("_", " ").replace("-", " ")
    combined = f"{normalized} {normalized_trim}".strip()
    if not combined:
        return None
    if "e hybrid" in normalized_trim or "plug in" in combined or "phev" in combined:
        return "Plug-in hybrid"
    if "hybrid" in combined:
        return "Hybrid"
    if (
        normalized in {"electric", "ev", "bev"}
        or "battery electric" in combined
        or "electric" in normalized_trim
        or "folgore" in normalized_trim
    ):
        return "Electric"
    if "diesel" in normalized:
        return "Diesel"
    if "gas" in normalized or "petrol" in normalized:
        return "Gasoline"
    return "Other"


def apply_listing_overrides(listings):
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return

    for listing in listings:
        override = overrides.get(listing.get("vin"), {})
        if isinstance(override.get("cpo"), bool):
            listing["cpo"] = override["cpo"]
        if "url" in override:
            url = override["url"]
            if url is None:
                listing["url"] = None
            elif valid_url := valid_https_url(url):
                listing["url"] = valid_url


def is_official_brand_dealer(name, config):
    normalized = name.casefold()
    return any(
        pattern.strip().casefold() in normalized
        for pattern in config.get("officialDealerNamePatterns", [])
        if pattern.strip()
    )


def normalize_dealer_names(listings, config=None):
    config = config or read_site_config()
    variants = {}
    for listing in listings:
        name = str(listing.get("dealer") or "Dealer not listed").strip()
        variants.setdefault(name.casefold(), {})
        variants[name.casefold()][name] = variants[name.casefold()].get(name, 0) + 1

    canonical = {}
    for key, counts in variants.items():
        if key in CANONICAL_DEALERS:
            canonical[key] = CANONICAL_DEALERS[key]
            continue
        canonical[key] = max(
            counts,
            key=lambda name: (
                counts[name],
                sum(1 for word in name.split() if word[:1].isupper()),
                sum(1 for character in name if character.isupper()),
                name,
            ),
        )

    for listing in listings:
        key = str(listing.get("dealer") or "Dealer not listed").strip().casefold()
        listing["dealer"] = canonical[key]
        listing["officialBrandDealer"] = is_official_brand_dealer(canonical[key], config)
        listing.pop("officialBmwDealer", None)


def clean_listing(item, config=None):
    config = config or read_site_config()
    vehicle = item.get("vehicle") or {}
    retail = item.get("retailListing") or {}
    history = item.get("history") or {}
    vin = str(item.get("vin") or vehicle.get("vin") or "").upper()

    accepted_models = {
        str(model).casefold()
        for model in config.get("queryModels", [config["model"]])
    }
    if (
        len(vin) != 17
        or str(vehicle.get("make", "")).casefold() != config["make"].casefold()
        or str(vehicle.get("model", "")).casefold() not in accepted_models
        or config.get("bodyStyle")
        and str(vehicle.get("bodyStyle", "")).casefold() != config["bodyStyle"].casefold()
        or str(retail.get("state", "")).upper() != "WA"
        or retail.get("used") is not True
    ):
        return None

    try:
        year = int(vehicle["year"])
        price = int(float(retail["price"]))
    except (KeyError, TypeError, ValueError):
        return None
    if year < config["minimumYear"]:
        return None

    mileage = retail.get("miles")
    try:
        mileage = int(float(mileage)) if mileage is not None else None
    except (TypeError, ValueError):
        mileage = None

    dealer = str(retail.get("dealer") or "Dealer not listed").strip()
    dealer = CANONICAL_DEALERS.get(dealer.casefold(), dealer)
    accidents = history.get("accidents")
    powertrain = normalize_powertrain(vehicle.get("fuel"), vehicle.get("trim"))
    allowed_powertrains = config.get("allowedPowertrains")
    if allowed_powertrains and powertrain not in allowed_powertrains:
        return None
    return {
        "id": vin.lower(),
        "vin": vin,
        "year": year,
        "make": config["make"],
        "model": config["model"],
        "trim": str(vehicle.get("trim") or "Trim not listed").strip(),
        "price": price,
        "mileage": mileage,
        "exteriorColor": str(vehicle.get("exteriorColor") or "Not listed").strip(),
        "interiorColor": str(vehicle.get("interiorColor") or "Not listed").strip(),
        "seats": normalize_seats(vehicle.get("seats")),
        "drivetrain": normalize_drivetrain(vehicle.get("drivetrain")),
        "powertrain": powertrain,
        "dealer": dealer,
        "officialBrandDealer": is_official_brand_dealer(dealer, config),
        "city": str(retail.get("city") or "City not listed").strip(),
        "state": "WA",
        "zip": str(retail.get("zip") or "").strip(),
        "cpo": retail.get("cpo") is True,
        "oneOwner": history.get("oneOwner") if isinstance(history.get("oneOwner"), bool) else None,
        "accidentFree": not accidents if isinstance(accidents, bool) else None,
        "accidentCount": history.get("accidentCount"),
        "usageType": str(history.get("usageType") or "Not listed").strip(),
        "image": valid_https_url(retail.get("primaryImage"))
        or config.get("fallbackImage", "assets/vehicle.svg"),
        "photoCount": int(retail.get("photoCount") or 0),
        "url": valid_https_url(retail.get("vdp")),
        "carfaxUrl": valid_https_url(retail.get("carfaxUrl")),
        "listedAt": item.get("createdAt"),
    }


def fetch_page(api_key, page, config=None):
    config = config or read_site_config()
    parameters = {
        "vehicle.make": config["make"],
        "vehicle.model": config["model"],
        "retailListing.state": "WA",
        "retailListing.used": "true",
        "page": page,
        "limit": 100,
        "includes": "total",
    }
    if config.get("year") is not None:
        parameters["vehicle.year"] = config["year"]
    if config.get("bodyStyle"):
        parameters["vehicle.bodyStyle"] = config["bodyStyle"]
    if config.get("queryFuel"):
        parameters["vehicle.fuel"] = config["queryFuel"]
    query = urlencode(parameters)
    request = Request(
        f"https://api.auto.dev/listings?{query}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "WA-iX-Finder/1.0",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def error_message(error):
    if isinstance(error, HTTPError):
        try:
            body = json.loads(error.read().decode("utf-8"))
            return f"Auto.dev returned HTTP {error.code}: {body.get('message') or body.get('error')}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"Auto.dev returned HTTP {error.code}"
    if isinstance(error, URLError):
        return f"Could not reach Auto.dev: {error.reason}"
    return f"Auto.dev refresh failed: {error}"


def refresh_cache(fetcher=None, date=None, api_key=None, max_calls=None):
    config = read_site_config()
    fetcher = fetcher or fetch_page

    with REFRESH_LOCK:
        with refresh_file_lock():
            date = date or today_local()
            cache = read_cache()
            last_attempt = cache.get("lastAttemptDate")
            if last_attempt == date or last_attempt and last_attempt > date:
                return cache

            api_key = api_key or os.environ.get("AUTO_DEV_API_KEY")
            if not api_key:
                clear_daily_changes(cache)
                cache["refreshError"] = "AUTO_DEV_API_KEY is not set; cached data was retained."
                write_cache(cache)
                return cache

            previous_vins = {item["vin"] for item in cache.get("listings", []) if item.get("vin")}
            known_vins = dict(cache.get("knownVins") or {})
            for vin in previous_vins:
                known_vins.setdefault(vin, cache.get("lastRefreshDate") or date)

            cache["lastAttemptDate"] = date
            cache["lastAttemptCalls"] = 0
            cache["refreshError"] = None
            write_cache(cache)

            calls = 0
            try:
                query_models = config.get("queryModels", [config["model"]])
                query_years = (
                    range(config["minimumYear"], datetime.now().year + 2)
                    if config.get("queryYearsSeparately")
                    else [None]
                )
                query_configs = [
                    {
                        **config,
                        "model": query_model,
                        **({"year": year} if year is not None else {}),
                    }
                    for query_model in query_models
                    for year in query_years
                ]

                raw_items = []
                total = 0
                previous_year_counts = {}
                for listing in cache.get("listings", []):
                    year = listing.get("year")
                    previous_year_counts[year] = previous_year_counts.get(year, 0) + 1
                for query_config in query_configs:
                    if max_calls is not None and calls >= max_calls:
                        raise ValueError(
                            f"Refresh requires more than the configured maximum of "
                            f"{max_calls} calls."
                        )
                    calls += 1
                    first = fetcher(api_key, 1, query_config)
                    query_items = list(first.get("data") or [])
                    query_total = int(first.get("total") or len(query_items))
                    query_year = query_config.get("year")
                    previous_year_count = previous_year_counts.get(query_year, 0)
                    minimum_year_count = max(1, int(previous_year_count * 0.5))
                    if query_year and previous_year_count and query_total < minimum_year_count:
                        raise ValueError(
                            f"Year {query_year} returned only {query_total} results; "
                            f"at least {minimum_year_count} were required."
                        )
                    page_size = len(query_items)
                    page_count = max(1, math.ceil(query_total / page_size)) if page_size else 1
                    required_calls = calls + page_count - 1
                    if max_calls is not None and required_calls > max_calls:
                        raise ValueError(
                            f"Refresh requires at least {required_calls} calls; "
                            f"configured maximum is {max_calls}."
                        )

                    for page in range(2, page_count + 1):
                        if max_calls is not None and calls >= max_calls:
                            raise ValueError(
                                f"Refresh requires more than the configured maximum of "
                                f"{max_calls} calls."
                            )
                        calls += 1
                        response = fetcher(api_key, page, query_config)
                        query_items.extend(response.get("data") or [])
                    total += query_total
                    raw_items.extend(query_items)
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError) as error:
                clear_daily_changes(cache)
                cache["lastAttemptCalls"] = calls
                cache["refreshError"] = error_message(error)
                write_cache(cache)
                print(f"[auto.dev] {cache['refreshError']}")
                return cache

            unique = {}
            rejected = 0
            for item in raw_items:
                listing = clean_listing(item, config)
                if listing is None:
                    rejected += 1
                    continue
                unique[listing["vin"]] = listing

            normalize_dealer_names(unique.values(), config)
            apply_listing_overrides(unique.values())
            first_seed = not known_vins
            for listing in unique.values():
                vin = listing["vin"]
                listing["isNew"] = not first_seed and vin not in known_vins
                listing["firstSeenDate"] = known_vins.setdefault(vin, date)

            current_vins = set(unique)
            updated = {
                **cache,
                "updatedAt": now_local(),
                "lastRefreshDate": date,
                "lastAttemptDate": date,
                "lastAttemptCalls": calls,
                "refreshError": None,
                "apiTotal": total,
                "listingCount": len(unique),
                "newCount": sum(1 for item in unique.values() if item["isNew"]),
                "removedCount": len(previous_vins - current_vins),
                "rejectedCount": rejected,
                "listings": list(unique.values()),
                "knownVins": known_vins,
            }
            write_cache(updated)
            print(
                f"[auto.dev] refreshed {len(unique)} listings with {calls} calls; "
                f"{updated['newCount']} new, {updated['removedCount']} removed"
            )
            return updated


def midnight_refresh_loop():
    while True:
        now = datetime.now().astimezone()
        tomorrow = now.date() + timedelta(days=1)
        next_refresh = datetime.combine(tomorrow, time.min, tzinfo=now.tzinfo) + timedelta(seconds=2)
        threading.Event().wait(max(1, (next_refresh - now).total_seconds()))
        refresh_cache()


class AutoDevHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format_string, *args):
        print(f"[auto.dev] {format_string % args}")

    def send_json(self, value, status=200):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if urlparse(self.path).path == "/api/health":
            self.send_json({"ok": True, "service": "auto-dev-cache", "pid": os.getpid()})
            return
        if urlparse(self.path).path == "/api/auto-dev-inventory":
            self.send_json(refresh_cache())
            return
        super().do_GET()


if __name__ == "__main__":
    try:
        with single_instance_lock():
            server = ThreadingHTTPServer(("127.0.0.1", PORT), AutoDevHandler)
            threading.Thread(target=refresh_cache, daemon=True).start()
            threading.Thread(target=midnight_refresh_loop, daemon=True).start()
            print(f"Auto.dev WA iX Finder: http://127.0.0.1:{PORT}/auto-dev.html")
            server.serve_forever()
    except RuntimeError as error:
        print(f"[auto.dev] {error}")
        raise SystemExit(1) from error

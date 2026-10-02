# WA Vehicle Finder

One ad-free GitHub Pages site for used vehicle inventory at Washington dealerships.

**Public site:** https://timformer.github.io/wa-vehicle-finder/

The vehicle selector currently includes BMW iX, Volvo EX90, Volkswagen ID. Buzz, Tesla Model X and Model Y, Toyota Sienna, Mercedes-Benz EQE SUV and EQS SUV, Porsche Macan and Cayenne, Genesis GV70, and Maserati Grecale.

## Configuration

`data/vehicles.json` is the single source of configuration for every vehicle. Each entry contains its query, labels, known marketplace domains, and refresh policy:

```json
"refresh": {
  "enabled": true,
  "intervalDays": 2
}
```

- Set `enabled` to control automatic updates.
- Set `intervalDays` to change update frequency.

The workflow checks at `09:17`, `13:17`, `17:17`, and `21:17 UTC` to tolerate delayed or omitted GitHub scheduled events. Durable reservations ensure only the first eligible check queries a vehicle. Tesla Model X, Toyota Sienna, both Mercedes-Benz SUVs, both Porsche models, and Genesis GV70 refresh every two days; BMW iX, Volvo EX90, Volkswagen ID. Buzz, and Maserati Grecale refresh daily. Tesla Model Y remains available on the site but no longer refreshes automatically.

Genesis GV70 combines the provider's `GV70` and `Electrified GV70` model queries so both gasoline and electric listings appear on one page.

Every enabled vehicle uses one global safety ceiling of 20 inventory API calls per Washington day. If a refresh would exceed that limit, the last successful listings and history are retained and that vehicle's public page displays a warning that its data may be incomplete.

## Data and history

Each vehicle has an isolated directory under `vehicles/<slug>/` containing:

- `inventory.json` — sanitized current listings.
- `inventory-history.json` — rolling seven-day availability, mileage, and price observations.
- `refresh-state.json` — durable attempt state and known VINs.
- `listing-overrides.json` — verified corrections that survive refreshes.

The public browser reads only the global configuration, current snapshots, and histories. It never calls the inventory provider and receives no credential or refresh state.

## Automated refresh

Add the inventory credential as the repository Actions secret `INVENTORY_API_KEY`. Before any provider call, `.github/workflows/refresh-inventory.yml` commits reservations for all eligible vehicles. Each vehicle retains its prior public snapshot if its refresh fails, exceeds the global 20-call limit, or returns an implausibly small result.

Successful refreshes update only that vehicle's snapshot and seven-day history. Failed and skipped refreshes never imply that listings disappeared.

Hitting the daily call limit or an implausibly small result (for example, a vehicle with very low WA inventory) are expected, already-handled conditions: the prior snapshot is retained and the vehicle's page shows a `refreshWarning`. These do not fail the workflow run. Only genuine errors (for example, the provider being unreachable) mark the run as failed.

Manual workflow runs can target comma-separated vehicle slugs. Set `retryFailed` to retry only targeted vehicles that already failed on the same Washington day; calls from earlier attempts still count toward each vehicle's 20-call daily ceiling.

## Local development

```powershell
python -m unittest discover -s test -p "test_*.py"
npm test
npm run serve
```

Open `http://127.0.0.1:4173`. The static site has no runtime dependencies and deploys through `.github/workflows/pages.yml`.

# Pricing Calibration Guide

The bundled tariff in `missions/data/pricing_v1.json` (seeded as `PricingConfig` v1)
is a **placeholder**. Every parameter is a guess; `calibrated` is `false` and
must stay `false` until the values are fitted to real market quotes.

This document describes the calibration procedure. Nothing here changes the
engine; it only explains how to produce a `calibrated: true` configuration that
the boss can activate.

## 1. Goal

Replace each placeholder coefficient with a value derived from **real quotes**
collected from the field, so that `estimate` outputs match what a human
dispatcher would charge for the same trip.

## 2. Data collection

For each vehicle class (`standard`, `van`, `truck`):

1. Collect **at least 30 real quotes** from experienced dispatchers/drivers.
   Each quote must record:
   - departure/arrival coordinates (or at least straight-line km),
   - distance (km) and duration (min) as invoiced,
   - number of helpers, number of floors, handling minutes,
   - pickup date/time (to test time multipliers),
   - the final price in DZD.
2. Store them in a CSV/JSON file outside the repo (no PII beyond what the
   mission already stores). Never commit real customer data.
3. Split the set: 80 % training, 20 % validation.

## 3. Baseline model

The engine computes:

```
subtotal = base + km*per_km + min*per_min + helpers + floors
amount   = round_half_up(subtotal * multiplier, rounding_step)
```

where `multiplier = min(cap, weekend * peak * night * manual)`.

Calibrate the **linear** part first (base, per_km, per_min, helper_hourly,
per_floor_fee, min_fare) with ordinary least squares on the training set,
constraining all coefficients to be non-negative. Then fit the multipliers
(peak, night, weekend) and the cap.

## 4. Procedure

1. Export the training set to a DataFrame.
2. For each vehicle class, fit:
   - `base`, `per_km`, `per_min` from `price ~ distance + duration + 1`.
   - `helper_hourly` from the residual after helpers.
   - `per_floor_fee` from the residual after floors.
   - `min_fare` = 5th percentile of observed prices for that class.
3. Fit time multipliers by comparing quotes with the same route but different
   pickup windows; use the median ratio.
4. Set `multiplier_cap` so that no observed quote exceeds `cap * subtotal`.
5. Set `estimate_band` from the validation-set residual (e.g. 90th percentile
   of absolute relative error).
6. Set `fallback_detour_factor` and `fallback_speed_kmh` from the training set
   (median ratio of invoiced distance to haversine; median speed).
7. Set `commission_rate` from the business agreement (default 0.10).
8. Set `calibrated = true` **only** after the validation set meets the
   acceptance criteria below.

## 5. Acceptance criteria

- Validation MAPE (mean absolute percentage error) < 15 %.
- No vehicle class has a negative coefficient.
- `min_fare` is never above the 10th percentile of observed prices.
- Multipliers are within [1.0, 1.5].
- `calibrated` is set to `true` in the new config version.

## 6. Publishing a calibrated configuration

```bash
# 1. Edit missions/data/pricing_v2.json (copy of v1 with calibrated=true and
#    the fitted values). Keep the schema identical.
# 2. Seed it as a new version, activating it:
python manage.py seed_pricing --config-version 2 --json missions/data/pricing_v2.json --activate
# 3. Verify:
python manage.py seed_pricing   # idempotent, no overwrite
# 4. Run the full test suite:
python manage.py test
```

The old version stays in the database (inactive) for audit. Rolling back is a
single `PricingConfig` toggle in the admin.

## 7. Re-calibration cadence

Re-run this procedure whenever:
- a new vehicle class is added,
- the fuel price changes by more than 10 %,
- the validation MAPE drifts above 20 % on a monthly sample of 20 new quotes.

## 8. What NOT to do

- Do **not** set `calibrated = true` on the placeholder file.
- Do **not** edit `missions/data/pricing_v1.json` to "look calibrated".
- Do **not** change `missions/pricing.py` to special-case a vehicle class.
- Do **not** commit real customer quotes or PII.

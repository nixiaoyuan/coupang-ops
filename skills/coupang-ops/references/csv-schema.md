# CSV helper contract

This is a small normalized schema for the first-version demo, **not an official Coupang export format**. It uses UTF-8 CSV with a header; a UTF-8 BOM is accepted. The helper uses only the Python standard library and reads local files without network requests.

## Sales: one daily row per SKU

Required columns:

`store,date,timezone,currency,sku,product_name,ordered_units,cancelled_units,returned_units,gross_revenue,cancelled_revenue,refunded_revenue`

- `date`: original order date in the declared local timezone, `YYYY-MM-DD`.
- `(store, date, sku)`: unique key. Product names do not define identity.
- `ordered_units`: ordered units in the supported population: fulfilled orders and orders cancelled before fulfillment, including units later returned. Pending or unknown-status orders are not supported by this helper; establish their treatment separately before calling this an exhaustive store analysis.
- `cancelled_units`: cancelled before fulfillment.
- `returned_units`: returned from the remaining units, attributed to their original order date and observed as of `status_as_of`.
- The three revenue fields use the declared currency, without currency signs or thousands separators. `gross_revenue` includes amounts later cancelled/refunded. This is gross sales value, not settlement revenue. Costs, fees, taxes, and shipping are not modeled.
- `cancelled_revenue` and `refunded_revenue`: amounts to subtract exactly once. A net export must be adapted to this gross contract, not passed unchanged.
- Include an explicit zero row for every known SKU/date with confirmed no sales. A missing row is an error, not zero sales. All SKUs must be covered for both full periods. The dataset is expected to be an exhaustive export for the store; this assumption cannot be proven from the CSV alone.

Calculations:

- Fulfilled units = ordered units − cancelled units.
- Net units = ordered units − cancelled units − returned units.
- Net sales value = gross revenue − cancelled revenue − refunded revenue.
- Observed unit return rate = returned units ÷ fulfilled units for the same order-date cohort, as of the status cutoff. If fulfilled units are zero, the rate is unavailable. Recent order cohorts have had less time to return, so the two periods' return rates are not fully mature or directly causal.
- Growth = (current − previous) ÷ previous. A zero previous value yields only an absolute change.

## Inventory: optional snapshot per SKU

Required columns: `store,sku,snapshot_at,available_units,lead_time_days`.

`snapshot_at` must be an ISO timestamp with the same UTC offset as `status_as_of`. The demo uses `2026-10-10T00:00:00+09:00`. All included inventory rows must use the same snapshot. Do not combine snapshots. The helper requires the snapshot to coincide with the status cutoff immediately after the current period; adapt the workflow if real data need another timing model.

`available_units` means sellable units after reservations. `lead_time_days` is an explicit assumption, not an inferred supplier promise. Missing inventory produces “库存未知”.

- Estimated daily demand = current-period fulfilled units ÷ current-period calendar days. Returns do not reduce this demand estimate because the demo does not establish whether they are resalable.
- Estimated stock coverage = available units ÷ estimated daily demand.
- Coverage below `lead_time_days`: “覆盖不足补货周期”.
- Coverage below `lead_time_days + stock_buffer_days`: “低于补货周期+缓冲”.
- Otherwise: “未触发当前阈值”, conditional on the assumptions. No-sales SKUs have no coverage estimate. Zero inventory with positive recent demand is shown as “当前无可售库存”.

## Metadata JSON

Required fields: `data_kind`, `store`, `timezone`, `currency`, `sales_date_basis`, `amount_basis`, `return_attribution`, `status_as_of`, `previous_start`, `previous_end`, `current_start`, `current_end`, `stock_buffer_days`.

Supported values: `data_kind` is `synthetic_demo` or `user_export`; `sales_date_basis` is `order_date_local`; `amount_basis` is `gross_before_cancellations_and_refunds`; `return_attribution` is `original_order_date`. Periods must have equal lengths, must not overlap, and must end before the status cutoff. The cutoff is the midnight immediately following the current period, in the declared timezone/offset. The metadata is a statement of known source definitions, not evidence of them.

## Running with adapted exports

```text
python scripts/analyze.py --sales <sales.csv> --metadata <metadata.json> --inventory <inventory.csv> --output-dir <private-output-directory>
```

Omit `--inventory` when it is unavailable. Put private inputs and outputs outside public commits. The repository ignores `private/` and `outputs/`; only synthetic fixtures and their sample report should be published.

---
name: coupang-ops
description: Analyze user-provided Coupang sales and inventory exports. Use when the user asks about best-selling products, sales changes, returns, or stockout risks.
---

# Coupang Operations

Analyze authorized, user-provided CSV exports. Respond in the user's language.

## Workflow

1. Identify the store, date range, timezone, currency, and business question.
   Ask only for information that cannot be established from the inputs.

2. Inspect columns, date coverage, product identifiers, missing values,
   duplicate records, and order statuses. Do not silently discard ambiguous data.

3. Confirm metric definitions before calculating.
   Check whether cancellations and refunds are already reflected in totals.
   Do not subtract them twice or mix currencies.

4. Use available code execution tools for calculations.
   If execution is unavailable, explain the limitation instead of inventing results.

5. Rank products by the requested metric, such as net units or net revenue.
   State the metric and period explicitly. Sales rankings are not profit rankings.

6. Compare equivalent periods and flag incomplete periods.
   When the previous value is zero, report the absolute change;
   do not invent a percentage growth rate.

7. Assess stockout risk only when usable inventory and sales data exist.
   State the inventory snapshot date and assumptions.
   Treat stock coverage as an estimate, not a guaranteed forecast.

8. Present findings, supporting figures, limitations, and suggested next actions.
   Missing costs do not mean zero costs.
   Sales alone cannot establish traffic, conversion, or causes of change.

## Safety

- Do not change prices, listings, advertising, orders, or inventory.
- Never request credentials in chat or commit private exports to the repository.
- Do not claim live API or browser access unless an authorized tool provides it.
- Explain relevant data flows before using external services with private data.
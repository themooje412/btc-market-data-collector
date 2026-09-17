# Milestone 3 — signed-GEX numerical forensics

This note records why the collector intentionally does not force its signed dealer-GEX estimate to match RetailInterest. RetailInterest is an independent benchmark only; it is not queried by production collection.

## Exchange-consistent production model

The complete active `base_currency=BTC` Deribit option inventory is joined to concurrent public `get_book_summary_by_currency` responses for BTC and USDC settlement. For each positive-OI option:

1. `mark_iv / 100` is annualized volatility in decimal form.
2. Time to expiry uses the contract expiration and the exchange snapshot time.
3. Standard Black–Scholes gamma is recomputed at that expiry's Deribit `underlying_price`, with `r=0` and `q=0`.
4. Deribit option `open_interest` is already underlying BTC amount; `contract_size` is not multiplied again.
5. The requested dealer assumption assigns calls `-1` and puts `+1`.
6. Contribution in USD per 1% move is `sign × gamma × OI_BTC × underlying_price² × 0.01`.

The dimensional chain is `(1/USD) × BTC × (USD/BTC)² × 0.01`, expressed as USD-equivalent delta-notional change for a 1% move under the model's BTC/USD convention. Gross GEX remains the existing unsigned ticker-gamma proxy and is not changed by this model.

## Deribit gamma reconciliation

On the pre-feature full chain, 1,109 positive-OI contracts had a published Deribit gamma. Deribit publishes gamma rounded to five decimal places, so relative error becomes unstable near zero. The expiry-specific `underlying_price` candidate matched the published rounded value on 99.87% of contracts, versus approximately 92.11% for each ticker's index and 91.60% for one latest index price.

For published gamma at least `0.00005 1/USD`, the selected input's median relative error was approximately 1.39%, p90 4.89%, and maximum material error 8.29%. At gamma at least `0.0001`, median was approximately 0.85%, p90 2.79%, and maximum 4.11%. Live output recalculates these statistics by candidate, expiry and moneyness and includes representative near-ATM, OTM and long-dated call/put traces.

## RetailInterest discrepancy root cause

A near-simultaneous public comparison captured:

- RetailInterest spot approximately `76,622.62`, headline Net GEX `+156.00M`, flip `78,860.90`.
- A fresh Deribit bulk snapshot of 930 inverse BTC options.

The sum of RetailInterest's public `by_strike` bars equaled its headline Net GEX. Reconstructing those bars from the same Deribit chain showed:

- requested production signs (calls negative, puts positive) were correlated approximately `-1.000` with the public bars;
- the opposite signs (calls positive, puts negative), a single spot gamma/reference price, and a strike range of approximately `0.80×spot` through `1.25×spot` reproduced the bars with correlation approximately `1.000` and mean absolute strike error around `$0.003M`;
- the displayed flip was the low-to-high cumulative-by-strike zero crossing, not a zero of an option surface repriced over hypothetical spot.

Therefore the public headline behavior is an opposite/customer-side convention (or a public implementation sign defect), filtered to a displayed strike band, while its written methodology says dealers short calls and long puts. The public card and the written sign convention are not mutually consistent. This is a rigorous semantic/implementation explanation, not normal price noise. Production retains the user's documented dealer signs and complete option universe.

## Flip definitions kept separate

- `zero_gamma_flip_repriced` (also the backward-compatible `zero_gamma_flip`) recomputes every option's gamma across a 50%–150% spot grid, parallel-shifts each expiry underlying by the spot ratio, holds contract IV constant, and interpolates every sign crossing. The primary crossing is nearest current spot.
- `zero_gamma_flip_cumulative_strike` sorts current signed contributions by strike and interpolates a zero in their running sum. This is a profile threshold, not a hypothetical-spot repricing root.

Crossing orientation, all crossings, crossing count, current net sign, IV ±1 point sensitivity, reference-price method, model version and bulk snapshot span are emitted. `gamma_regime` is determined only by the current aggregate net sign, never mechanically by spot being above or below a flip.

## Remaining limitations

- Dealer direction is an assumption; public OI does not identify dealer ownership.
- The repriced flip holds each contract IV constant and shifts expiry forwards in parallel with spot. It is a controlled sensitivity surface, not a forecast of smile dynamics.
- Deribit's published ticker gamma is low precision, limiting relative-error interpretation for low gamma.
- A benchmark website may change its unpublished implementation. The collector has no dependency on that implementation and will not chase its headline values.

Official references: [Deribit ticker](https://docs.deribit.com/api-reference/market-data/public-ticker), [bulk option book summary](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency), and [option data collection best practices](https://docs.deribit.com/articles/options-data-collection-best-practices).

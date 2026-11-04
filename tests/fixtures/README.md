These are minimal public response excerpts captured during the November 2026 investigation, not synthetic evidence of profitable deals.

- `amazon-g305-hidden-offer.html`: Amazon.ie ASIN B07CGPZ3ZQ. Preserves the new product-details table, hidden-offer message and one unrelated price node. Scripts, session identifiers, offer tokens and other page content were omitted. The correct parser result has model `910005283` and no current offer price.
- `cex-g305-detail.json`: CeX Ireland box `5099206077836B`, from its public v3 box-detail endpoint. Model `G305` is an explicit `attributeInfo` field. It is not assumed equivalent to Amazon's regional part number.
- `cex-calculated-cash.json`: two PS5 search-index rows with zero `cashBuyPrice` and positive `cashPriceCalculated`. These are discovery quotes, not confirmed live detail payouts.

The dated 100-SKU discovery snapshot in `docs/cex-targets-seed.json` was drawn from CeX Ireland's public PS5 and Nintendo Switch search responses. It contains no invented Amazon mappings, sales volumes or profitability claims.

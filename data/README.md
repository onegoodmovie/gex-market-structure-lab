# Public data boundary

This directory intentionally contains no raw or normalized vendor option-chain
data.

Included:

- `manual_input/transmission_spot.csv`: a fully synthetic spot path for the
  offline demo and test suite.
- `public_results/*.csv`: author-created aggregate tables transcribed from the
  completed study's final report.

Excluded:

- raw and normalized option-chain snapshots;
- third-party per-strike exports;
- API responses, credentials, account identifiers, and request logs;
- production capture outputs.

The code license does not grant redistribution rights to market data. Users who
connect a live provider are responsible for its current license, subscription,
and redistribution terms.

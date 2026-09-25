# Publication audit

## Release boundary

This repository is a clean public derivative of a separate private/production
experiment. It has no shared Git history, remote, scheduler, or generated data
directory with the source project.

## Removed from the public release

- original `.git` directory and remote configuration;
- raw and normalized vendor option-chain snapshots;
- third-party per-strike CSV exports;
- production capture logs and generated outputs;
- macOS LaunchAgent installer and machine-specific shell wrapper;
- recovery and rebuild manifests containing absolute local paths;
- keychain service/account names and local interpreter paths;
- the source archive's uncommitted README change.

## Retained

- core calculation and attribution code;
- deterministic synthetic provider;
- tests, including end-to-end synthetic pipeline tests;
- frozen experiment configuration, with the public default changed to
  `synthetic` and credentials changed to environment-only;
- methodology, limitations, final report, and contemporaneous daily notes;
- author-created aggregate result tables.

## Data policy

No third-party raw market data is included or relicensed. Aggregate tables are
limited to results already reported in the author's final study. The offline
fixture is synthetic.

## Credential review

The repository contains the environment variable name `MASSIVE_API_KEY` but no
credential value. `.env`, credential configuration, generated data, logs, and
cache directories are ignored.

## Verification

- Full test suite: `101 passed`.
- Synthetic end-to-end demo: capture → normalize → frozen map → actual map →
  attribution; both attribution identity gates passed.
- Generated SVG figures parse as valid XML and are reproducible from the
  checked-in aggregate CSV tables.

## Remaining human decisions before publication

- Confirm `onegoodmovie` is the desired public attribution.
- Confirm MIT is the desired code license.
- Confirm the new repository name `gex-market-structure-lab`.
- Decide whether to keep the optional historical live-provider adapter in the
  first release or publish only the synthetic research engine.

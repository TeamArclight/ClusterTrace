# ClusterTrace

Trace plastic collection lots through batches and recycling, with mass-balance checks and an exportable supporting audit report.

## Run the demo

```bash
python3 server.py
```

Open http://localhost:8000. Requires Python 3.10+ and no third-party packages.

The demo stores data locally in `clustertrace.json`. Use the **Load demo** button to create a 340 kg batch, then attempt to record 500 kg recycled output to see the mass-balance rejection. The report is supporting traceability evidence, **not an official EPR certificate**.

## Scope

- Record plastic lots with picker ID, weight, material, location, photo reference and time.
- Merge unbatched lots into a batch, preserving their IDs.
- Record recycler output only when it does not exceed verified input; log rejected attempts.
- Download a JSON audit pack with batch lineage and events.

This prototype trusts user-entered source details. Independent weighing, identity checks and physical audits would be required before relying on it for compliance.

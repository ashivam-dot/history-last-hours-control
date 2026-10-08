# Atlas authenticated analytics

`atlas_analytics.py` is deployed as `atlas-in-numbers-analytics` in Modal workspace `aksha-shivam18`. It collects every three hours at minute 42 UTC, independently of the laptop and producer. OAuth credentials are in the dedicated `atlas-in-numbers-analytics-oauth` Modal secret; the granted scopes are exactly `youtube.readonly` and `yt-analytics.readonly`. Every collection verifies channel `UC6e6OB3iw3yp8JnnBYxLItA` before reading video metrics.

Authenticated snapshots stay in the private `atlas-in-numbers-analytics` volume. `latest` returns the most recent snapshot to the workspace owner. `public_metrics` exposes only public video IDs, titles, publication dates, view/like counts, and their actual observation time to the producer's learning records. Detailed Analytics rows for newly published videos can be delayed; an empty report is recorded as pending, not zero performance. Public view counts and channel totals can update at different times.

Deploy with the workspace's existing Modal profile:

```sh
python -m modal deploy --profile aksha-shivam18 apps/atlas_analytics.py
```

No OAuth files belong in the producer or Git history. Local authorization backups, if retained, belong under ignored `private/` with owner-only permissions.

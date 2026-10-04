# History's Last Hours independent control

Private owner-controlled repository: `ashivam-dot/history-last-hours-control`.
The producer is `ashivam-dot/creature-receipts`. Producer automation has no
write access or deploy key here. The tracked [`policy.json`](policy.json) has
`signing_enabled: false` and `publishing_enabled: false`. Nothing here has
reviewed, signed, scheduled, or published a real episode.

## Current cloud state

- Three **manual-only** Actions workflows prepare a review packet, sign a
  reviewed candidate, or release one certified video to **YouTube only**.
  The signing and publishing jobs fail closed under the disabled policy.
- The Ed25519 private key exists only as the `HISTORY_REVIEW_SIGNING_KEY`
  secret in this repository's `history-review-signing` environment. The public
  half is [`reviewer.pub`](reviewer.pub); its SHA-256 fingerprint is pinned in
  the control policy. The private key was generated in memory and was never
  written to the producer repository or a local file.
- The `history-publisher` environment exists but has **no Buffer or Cloudinary
  credential**. No publisher token was copied from the producer. The policy
  pins the exact History Buffer organization and YouTube channel IDs, checked against the
  current live Buffer API and the ep054 public readback. Buffer still shows the
  channel's older display name, “Creature Receipts”; the channel ID is the
  History destination.
- GitHub's current private-repository plan rejects branch protection and
  required environment reviewers. Both environments have no approval rules.
  The separate private repository, owner-only collaborator list, no deploy
  keys, and manual workflows limit producer access, but they do not provide
  an enforced owner approval before a signer job. Keep signing disabled until
  a protected approval method or equivalent independent service is available.

## Exact candidate gate

`python -m control prepare` reads only Git blobs from an exact producer commit,
downloads the MP4 from the pinned Cloudinary account without redirects, and
saves the video and matching review subject in a newly created private folder.
It never imports or executes producer code. The candidate check enforces the
episode floor and cutoff, media/spec/manifest hashes, exact narration across
the spec, script, and render, two quoted source sites per cited claim, image
rights and credits, and a clean producer review of the same MP4 hash.

The independent reviewer must watch and listen to the **entire hosted video**,
verify each factual source and visual right, and inspect the exact packet. An
`approval.json` is authored in the trusted control process, never accepted
from the producer repository. It contains the full packet `subject`,
`decision: approved`, `reviewed_at_utc`, and three true checks:
`claim_sources`, `visual_identity_rights`, and `full_video_audio`. A missing
or uncertain check means hold. Code can verify the attestation and exact
bytes; it cannot prove that a person actually completed this review.

`python -m control sign` re-downloads the hosted MP4 and recomputes every
binding before producing `independent_review.json`. Its Ed25519 message and
schema match History's existing dormant verifier. It receives only the
signing key, not a Buffer credential. A direct integration check showed that
the existing producer verifier accepts a synthetic review signed here.

## Isolated YouTube publisher

`python -m control publish` is a real but dormant YouTube scheduling path.
Before Buffer access, it rechecks the signed review, source commit, and hosted
producer video bytes. It uploads the exact reviewed MP4 to the separately pinned
`mw0oh0v8` Cloudinary cloud using the control-only `HISTORY_PUBLISHER_CLOUDINARY_URL`
environment secret, then downloads the control copy and verifies its SHA-256.
The upload uses a content-addressed `history-last-hours/` public ID and
`overwrite=false`. The
current single-request upload accepts MP4s up to 95 MiB; larger videos hold.
The publisher verifies the pinned Buffer organization and YouTube channel
service/status, builds title/text/credits from the signed Git blobs,
searches complete post history for an exact duplicate, verifies an existing
post's text/video/due time, and checks queue capacity. A new due time must be
UTC and 30 minutes to 30 days ahead. It checks control-owned hosted bytes once
more before the create mutation. Uncertain Buffer responses hold and require inspection;
retries reconcile accepted posts rather than blindly creating another one.
The manual workflow serializes publisher runs. The code has no Instagram
destination, query, or mutation.

The publisher has only synthetic and mocked API tests. It has never received
its own media or Buffer credential, uploaded a real control copy, queried
Buffer with its own credential, or sent a create mutation. Buffer's create API
has no client idempotency key, so external
concurrent executors and ambiguous failures remain deployment risks.

## Work still required before activation

1. Enforce reviewer approval using a supported GitHub plan or independent
   service. The current private plan cannot require a reviewer for the
   signing environment.
2. Pin `reviewer.pub` in a reviewed History producer commit at
   `kit/independent-review.pub`. Align History's dormant policy to YouTube
   only while retaining `enabled: false`, then test the source-to-control
   handoff and signed review delivery.
3. Issue a **new, rotated Buffer credential** available only to the control
   publisher environment. Remove and revoke producer access to the old
   `BUFFER_API_KEY` in GitHub Actions, Modal, local `.env` copies, and other
   executors. The producer currently uses it for monitoring and legacy
   scheduling, so migrate those jobs first. Producer access to the old key
   means true credential isolation has **not** been achieved.
4. Provision a control-only Cloudinary credential for the pinned `mw0oh0v8`
   cloud in the `history-publisher` environment as
   `HISTORY_PUBLISHER_CLOUDINARY_URL`. This cloud is distinct from History's
   producer cloud `uj4a07e7` and is already used by the trusted Mool control
   repository. The `history-last-hours/` public ID path keeps the two channels'
   assets separate by name, but it does not restrict a broad API key. Remove
   Mool producer access to `mw0oh0v8` through its old Modal workspace before
   treating this as producer-isolated. Prefer a dedicated History key limited
   to this folder; [folder roles can be assigned to API keys on all
   plans](https://cloudinary.com/documentation/permissions_assign_roles_api).
   Verify Upload API enforcement and audit all remaining broad keys. A
   [Master Admin key can use every Upload API
   endpoint](https://cloudinary.com/documentation/product_environment_settings),
   so a broad Mool control key would still be able to change History assets.
   If channel-to-channel compromise isolation is required, use a separate
   Cloudinary account instead. The latest Mool control monitor snapshot
   (2026-10-04, run `37180808602`) reported 0.47 of 25 monthly credits used;
   [Cloudinary pricing](https://cloudinary.com/pricing) counts storage,
   bandwidth, and transformations against that shared quota. Monitor capacity
   before activation and as either channel grows.
5. Verify the actual Buffer create and readback contract in a reviewed
   dry-run/staged integration, establish durable receipt transfer to History,
   and prevent producer-owned code from publishing independently. Keep the
   producer `status/scheduling_hold.json` and both release switches off until
   those checks pass.

The local control code and cloud workflow installation do not activate any
release. No producer secret has been deleted or rotated.

## Verification

```sh
uv sync --frozen --python 3.12
uv run --frozen python -m pytest -q tests
uv run --frozen python -m compileall -q control
```

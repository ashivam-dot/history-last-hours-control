# History's Last Hours control (local, dormant)

This repository is separate from the producer repository `ashivam-dot/creature-receipts`.
It is a proposed owner-controlled review boundary for future episodes. The tracked
[`policy.json`](policy.json) has `signing_enabled: false` and
`publishing_enabled: false`. It has no remote, signing key, public key, publisher
credential, runnable GitHub workflow, or Buffer mutation. It has not reviewed or
released an episode.

## What is implemented

- `python -m control prepare` reads only Git blobs from one immutable producer
  commit, downloads the exact Cloudinary MP4 from the pinned account, checks the
  producer's media/spec/manifest bindings, and saves the MP4 and matching
  review subject in a private, newly created directory. It does not execute
  producer code.
- `python -m control sign` downloads and rechecks the candidate again, requires
  a separate `approval.json` with all three explicit checks true, and signs the
  exact schema and Ed25519 message used by the producer's dormant
  `ytc.release._independent_review` verifier. It works only after the control
  policy is reviewed to enable signing and pin a public-key fingerprint.
  The private key comes solely from `HISTORY_REVIEW_SIGNING_KEY` in the signer
  job's protected environment and is never written by this code.
- `python -m control publisher-preflight` rechecks hosted bytes, the Git
  candidate, and the review signature before returning a destination-bound
  release plan. It refuses to run under the tracked disabled policy. It is
  credential-free and does not call Buffer; the publisher mutation remains to
  be built and independently reviewed before release activation.

## Independent human review handoff

The candidate packet contains the MP4 and `subject.json`. The owner-controlled
reviewer must watch and listen to the entire hosted video, inspect each cited
claim against source pages, verify visual identity and image rights, and compare
the exact committed `topic.json`, `short.yaml`, `script.json`, `research.json`,
`visuals.json`, `review.json`, and `work/manifest.json`. If any claim, visual,
audio, right, credit, source, or provenance is uncertain, do not approve it.
An approval file is created **inside the trusted review process**, never read
from the producer repository. Its exact shape is:

```json
{
  "subject": {"id": "ep063", "media_sha256": "...", "media_url": "...", "media_public_id": "...", "files": {"topic.json": "..."}},
  "decision": "approved",
  "checks": {"claim_sources": true, "visual_identity_rights": true, "full_video_audio": true},
  "reviewed_at_utc": "2026-10-04T06:00:00+00:00"
}
```

The `subject` must be copied in full from the packet. The signer recomputes it
from the source commit and hosted bytes. A changed field or unchecked assertion
holds the candidate. The code verifies an explicit attestation; it cannot prove
that a human watched the video. The protected signing environment must require
the reviewer to inspect the packet before releasing the key.

## Intended cloud isolation

1. Create a **private, separate** GitHub repository for this code. The producer's
   deploy key must have no write permission there. Protect the control branch,
   workflow files, policy, and signing/publishing environments from producer
   credentials and producer-authored changes. Use a read-only source checkout
   pinned to an exact commit. `workflow-drafts/review-release.yml` is an inert
   outline, not an installed workflow.
2. Put only the reviewer private key in a protected `history-review-signing`
   environment. Pin its public key in a reviewed History producer commit at
   `kit/independent-review.pub` and pin its SHA-256 fingerprint in this control
   policy. Do not create or expose the key in the producer repository.
3. Put a **new, rotated Buffer publisher credential** only in a different
   protected `history-publisher` environment. Pin the exact History YouTube and
   Instagram destination IDs in this control policy and verify their service,
   organization, status, existing posts, media URL, and due time before any
   create. Use one serialized executor and reconcile uncertain responses before
   retrying; Buffer's current create mutation lacks a client idempotency key.
4. Remove and revoke the producer's current `BUFFER_API_KEY` from GitHub Actions,
   Modal `creature-receipts-studio`, local `.env` copies, and any other producer
   executor. The producer currently also holds `CLOUDINARY_URL`, its deploy key,
   and YouTube credentials, and can run scheduling code. A signer in this repo
   alone is **not** an isolated release boundary while those remain. Move
   necessary monitoring to a least-privilege read-only path or separate trusted
   service before revoking credentials; inspect the existing production jobs so
   that monitoring does not break.
5. Build and test the control-owned Buffer publisher, then separately review a
   staged producer integration that accepts only signed handoffs. Keep History's
   `status/autonomous_release_policy.json` disabled and its
   `status/scheduling_hold.json` in place until the trust boundary, destinations,
   review process, and publisher are verified end to end. A local repository or
   inert workflow draft cannot enforce cloud permissions.

No credential migration, key pinning, remote creation, deployment, or release is
performed by this repository's current code.

## Local verification

Use Python 3.12 with the locked `uv` environment. The tests create a
synthetic Git candidate and an in-memory Ed25519 key; they make no network or
Buffer calls.

```sh
uv sync --frozen --python 3.12
uv run --frozen python -m pytest -q tests
uv run --frozen python -m compileall -q control
```

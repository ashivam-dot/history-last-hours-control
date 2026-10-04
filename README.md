# History's Last Hours independent control

Private owner-controlled repository: `ashivam-dot/history-last-hours-control`.
The producer is `ashivam-dot/creature-receipts`. Producer automation has no
write access or deploy key here. The tracked [`policy.json`](policy.json) has
`intake_enabled: false`, `signing_enabled: false`, and
`publishing_enabled: false`. Trial branch `trial/ep064-intake-20261004` ingested
a real ep064 draft, which remains held during independent QA. No real episode
has been approved, signed, scheduled, or published.

## Current cloud state

- The manual Actions workflows prepare a review packet, intake a
  private draft, sign a
  reviewed candidate, check the pinned Buffer destination, probe the pinned
  Cloudinary cloud with a temporary test asset, or release one
  certified video to **YouTube only**.
  The signing and publishing jobs fail closed under the disabled policy.
- `unattended-control.yml` has four daily cloud slots after the Modal studio
  cycles. Scheduled jobs start only when the repository variable
  `HISTORY_CONTROL_AUTOMATION` is `1`; with it unset, they use no runner minutes.
  Manual dispatch can resume a held pre-release receipt. With the tracked
  intake switch off on `main`, scheduled runs can discover and claim a draft;
  intake, signing, and publishing require reviewed policy changes.
- The Ed25519 private key exists only as the `HISTORY_REVIEW_SIGNING_KEY`
  secret in this repository's `history-review-signing` environment. The public
  half is [`reviewer.pub`](reviewer.pub); its SHA-256 fingerprint is pinned in
  the control policy. The private key was generated in memory and was never
  written to the producer repository or a local file.
- The `history-publisher` environment holds the rotated control Buffer key
  and the `mw0oh0v8` Cloudinary credential while release remains disabled.
  A direct producer GitHub secret inventory now shows no Buffer, Cloudinary,
  or Google keys, and the local producer `.env` has none of those keys. The
  previous Buffer key returns HTTP 401 after the cutover. Remaining Modal
  credentials and other executors have not yet been fully audited. The policy
  pins the exact History Buffer organization and YouTube channel IDs, checked
  against the current live Buffer API and the ep054 public readback. Buffer
  still shows the channel's older display name, “Creature Receipts”; the
  channel ID is the History destination.
- Manual run `37181688369` generated a 1,546 byte MP4, uploaded it under
  `history-last-hours/test/` in `mw0oh0v8`, verified the delivered bytes and
  SHA-256, and received Cloudinary's successful destroy response with cache
  invalidation requested. No Buffer key was mounted for that test.
- GitHub's current private-repository plan rejects branch protection and
  required environment reviewers. The control environments have no approval rules.
  The separate private repository, owner-only collaborator list, no deploy
  keys, and staged workflows limit producer access. The automated QA job and
  separate signer job are the intended release gate; their live behavior still
  needs verification before signing can be enabled.

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

## Private draft intake (staged)

The unattended workflow inspects the public producer `main` tree for
`content/episodes/epNNN/draft.json` at or above the policy floor. It pins the
commit that last changed each draft, validates it in a detached read-only
checkout, and stores one receipt per episode on the private
`unattended-state` branch. A changed draft, disappeared claimed draft, new
producer release record, or failed stage holds the receipt and opens or
updates a private issue with the Actions run; discovery holds fail the run.
It processes one eligible episode per run and waits while the switch needed
for the next phase is off. Once that switch is enabled, it rebuilds the
short-lived private artifacts before advancing. Separate jobs use the
publisher, QA, and signing environments, with private one-day artifacts for
the exact packet and passing review. The publisher uses the same concurrency
group as the manual publisher. It reserves an unused 17:00 UTC slot more than
two hours ahead before a Buffer mutation. A planned release never retries
automatically after an uncertain outcome; it requires external Buffer post
inspection. The dispatch resume path only accepts holds before release
planning. The trusted signer checks the current control `main` policy just
before Ed25519 signing. The trusted publisher checks it before the public
Cloudinary upload and again immediately before Buffer create. The scoped
control token is mounted only for these trusted commands; no producer code is
executed. No local Mac or running workstation is required.

`intake-private-draft.yml` takes one exact producer `main` commit and episode.
It checks the committed `draft.json`, spec, manifest, research, rights, and
clean producer review without importing producer code. It requires the draft's
Modal locator to be exactly `creature-receipts-outbox/drafts/<episode>-<sha>.mp4`,
then reads that private MP4 from the History Modal workspace and checks every
byte against the draft and manifest SHA-256 values. Before opening the volume,
it verifies the Modal token belongs to the pinned `aksha-shivam18` workspace.

The tracked `intake_enabled` switch is `false` on `main`. When separately
enabled, the control process uploads verified bytes with `overwrite=false` to the pinned
`mw0oh0v8` Cloudinary cloud as an **authenticated** video under
`history-last-hours/drafts/`. It re-downloads the asset through Cloudinary's
signed asset-download API and checks every byte. The pre-QA asset's URL cannot
be used for anonymous delivery, even though its hash appears in the public
producer repository. An ambiguous upload response is reconciled by looking up
the exact authenticated public ID and comparing all asset bytes before reuse.
A later approved release makes a separate public copy bound to this exact
authenticated asset.

The workflow writes a new, private, 14-day Actions artifact for each run. It
contains the exact MP4, committed evidence files, `draft.json`, a version 2
`subject.json` binding the producer commit and authenticated media, and a
control-owned `control_hold.json`. It never accepts a producer `hold.json` or
publisher credential. Run `37191082431` ingested ep064 and verified the exact
MP4 in its authenticated control asset. Its unsigned URL returned HTTP 401.

The manual independent QA workflow takes only the intake run ID, exact source
commit, and episode. It downloads the private artifact, compares every evidence
file and draft byte with the pinned producer commit, and checks the MP4 hash and
authenticated control URL. It fetches the cited source pages and requires exact
quotes for every claim from two independent live sites. It checks rights pages
for the claimed licenses. It decodes the entire MP4 with FFmpeg, checks duration,
shape, loudness, and peak, samples every second, transcribes the full mixed audio,
and obtains a structured claim, visual, and quality verdict from a separate
vision model. Every check must pass before it writes `approval.json`; a hold
writes a private `qa_report.json` with its stage and reason instead. The signer
revalidates the packet and signs the exact version 2 subject with a distinct
Ed25519 message context. The manual publisher
downloads the same artifact, repeats those checks, verifies the version 2
signature, reads the authenticated Cloudinary asset by its signed asset-download
API, and compares it byte for byte with the packet. It checks the pinned Buffer
organization, YouTube channel, post history, and queue before making a public
copy, then verifies that copy and rechecks the destination before scheduling.
The producer `hold.json` and producer public Cloudinary URL are not used on
this path.

The QA job uses its own `history-independent-qa` environment, which has no
signing key. It sends the passing report and generated approval through a
private, one-day artifact to a separate `history-review-signing` job. The signer
rechecks the packet and QA result before using its key. The QA job supports a separately provisioned
`HISTORY_QA_GEMINI_API_KEY` as its primary audio and vision reviewer, or
`HISTORY_QA_OPENAI_API_KEY` as a reviewed alternative. The provider and exact
model name are pinned together in tracked `policy.json` to Gemini 3.8 Flash.
Gemini calls use the documented
[generateContent API](https://ai.google.dev/api/generate-content) with inline
audio and frames and a JSON schema. Quota and capacity responses receive only
three bounded request retries before a hold. For unattended QA, a trusted
report that identifies only Gemini 429/503 exhaustion and binds the exact
episode, source commit, draft hash, media hash, provider, and model schedules
another complete QA run after a 12-hour cooldown. At most three complete QA
runs are permitted; the third operational failure holds the receipt. Missing
reports, source/media failures, and negative factual or visual verdicts remain
held without automatic retry. The OpenAI path uses documented
[Responses image input](https://platform.openai.com/docs/guides/images),
[structured output](https://platform.openai.com/docs/guides/structured-outputs),
and [audio transcription](https://platform.openai.com/docs/guides/speech-to-text)
APIs. In run `37191082431`, the pinned Gemini model transcribed ep064 after
source, rights, full decode, and loudness checks. Its multimodal review then
exhausted bounded 429/503 retries, so the private report held and no approval
was produced.
Automated evidence matching and model judgments can miss factual or rights
problems. Signing and publishing remain disabled until this gate passes a real
private draft and its failure cases are checked.

`HISTORY_INTAKE_MODAL_TOKEN_ID` and `HISTORY_INTAKE_MODAL_TOKEN_SECRET` are
configured in the existing control `history-publisher` environment. The intake
job references only those tokens and that environment's
existing `HISTORY_PUBLISHER_CLOUDINARY_URL`; it never injects the Buffer key.
The Modal token must access the History workspace and the Cloudinary credential
must belong to `mw0oh0v8`. A read-only probe resolved the Modal token to the
pinned `aksha-shivam18` workspace and found the outbox volume. The
intake switch is disabled on `main` while signing and publishing remain off.
The signing and publishing switches need separate live
QA, signer, and publisher checks before use.

The legacy `python -m control sign` re-downloads the hosted MP4 and recomputes every
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

The Buffer scheduling path has only synthetic and mocked create API tests. The
control environment has passed a read-only Buffer connection check and a live
temporary Cloudinary media probe, but it has not uploaded a real episode copy
or sent a Buffer create mutation. Buffer's create API
has no client idempotency key, so external
concurrent executors and ambiguous failures remain deployment risks.

An accepted Buffer release records a `scheduled` receipt. The separate
`post-due-monitor.yml` workflow checks it after the reserved 17:00 UTC slot and
a two-hour grace period. It reads the exact sent Buffer post, verifies the
control-owned media SHA-256, and compares the public YouTube watch page's video
ID, channel ID, title, description, and visibility with the approved source
commit. Only then does it advance the private receipt to `published`; failures
leave it scheduled and open or update a private delivery issue. This monitor
has not yet observed a real scheduled release. Read-only run `37192202771`
verified the sent-post GraphQL fields and the public Shorts player on an
existing channel video from an Actions runner.

## Work still required before activation

1. Run a corrected real draft through independent QA to a passing report and
   inspect its exact media and evidence. Ep064 remains held: the model review
   failed operationally, and its source claims and visuals need editorial work.
   Keep signing and publishing disabled during this trial.
2. After the private QA trial passes, enable signing in a separately reviewed
   control commit while publishing stays off. Test the signer job with the
   passing QA artifact and pin `reviewer.pub` in a reviewed History producer
   commit at `kit/independent-review.pub`. Align History's dormant policy to YouTube
   only while retaining `enabled: false`, then test the source-to-control
   handoff and signed review delivery.
3. Finish auditing producer Modal credentials and any other executors for
   surviving Buffer or Cloudinary access. The old Buffer key has already been
   rotated and returns HTTP 401; the direct producer GitHub secret inventory
   and local `.env` no longer contain Buffer, Cloudinary, or Google keys. The
   manual
   `check-publisher-connection.yml` workflow uses the control environment
   secret `HISTORY_PUBLISHER_BUFFER_API_KEY` for read-only exact organization
   and YouTube channel verification while both release switches stay off.
4. Verify the staged `HISTORY_PUBLISHER_CLOUDINARY_URL` credential for the
   pinned `mw0oh0v8` cloud in the `history-publisher` environment. This cloud
   is distinct from History's
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
   before activation and as either channel grows. The manual
   `check-publisher-media.yml` workflow creates a tiny MP4 under
   `history-last-hours/test/`, verifies its delivered bytes, and deletes it.
   It does not mount a Buffer key or enable either release switch.
5. Verify the actual Buffer create and readback contract in a controlled
   integration, establish durable receipt transfer to History, and prevent
   producer-owned code from publishing independently. Exercise the staged
   unattended trigger only after the QA, signer, private-to-public media, and
   Buffer path pass. Keep the producer `status/scheduling_hold.json` and both
   release switches off until those checks pass.

The local control code and cloud workflow installation do not activate any
release. This branch does not change credentials; the prior cutover rotated
the old Buffer key.

## Verification

```sh
uv sync --frozen --python 3.12
uv run --frozen python -m pytest -q tests
uv run --frozen python -m compileall -q control
```

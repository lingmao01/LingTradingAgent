# Dormant signed-update safety boundary

## Status

The desktop application still has **no updater**. There is no release feed,
download client, update UI, background check, installer launch, or configured
publisher identity. Updates remain disabled until issue #1016's signed
`0.3.0 -> 0.3.1` clean-Windows validation is completed.

This code makes two parts of that future review executable now:

1. `update-verification.ts` verifies a local candidate against explicit release
   and publisher policy.
2. `update-recovery.ts` records the narrow handoff phases and resolves an
   interrupted attempt safely on the next launch.

`BackendManager.stopForUpdate()` is the required handoff boundary. It returns
only after the exact owned backend PID, watchdog PID, and loopback listener are
gone. Listener evidence uses a TCP connection probe rather than HTTP health, so
a process that accepts connections but stops answering HTTP remains open. A
failed handoff retains the exact identifiers and log stream for a later cleanup
retry. It never terminates `python.exe` by image name.

## Verification order

The caller must supply the installed version, candidate version, expected
SHA-256 digest, and an allowlist containing at least one publisher certificate
SHA-256 fingerprint. The policy fails closed when the allowlist is empty.

Checks run in this order:

1. policy and release metadata are well formed;
2. the candidate semantic version is strictly newer;
3. the local file digest matches the expected SHA-256;
4. PowerShell opens the artifact with a lock that permits readers but denies
   writers and deletion, hashes those locked bytes, and inspects Authenticode;
5. the locked digest matches the initial and expected digests;
6. Windows reports a `Valid` Authenticode signature;
7. the SHA-256 hash of the signer certificate's raw bytes is allowlisted;
8. the file digest is confirmed again after the locked inspection.

The future launch path must call
`assertVerifiedUpdateArtifactUnchanged()` immediately before starting the
installer. Verification and PowerShell discovery are both timeout-bounded. The
pre-launch assertion is mandatory even though the artifact was already checked:
no result object makes a mutable path permanently trustworthy.

The expected digest and publisher allowlist must eventually come from a trusted
release configuration owned by the maintainers. That source is deliberately
not invented here.

## Rejection matrix

| Scenario | Verification evidence | Stable result code | Required behavior |
| --- | --- | --- | --- |
| Tampered artifact | Local SHA-256 differs from release metadata | `artifact-hash-mismatch` | Do not launch; discard/quarantine the staged file |
| Unsigned artifact | Authenticode status is `NotSigned` | `artifact-unsigned` | Do not launch, even when the digest matches |
| Invalid or damaged signature | Authenticode status is not `Valid` or `NotSigned` | `artifact-signature-invalid` | Do not launch; record the Windows status for diagnostics |
| Wrong publisher | Signature is valid but certificate SHA-256 is not allowlisted | `publisher-not-allowed` | Do not launch; never trust the subject name alone |
| Equal or downgraded version | Candidate is not strictly newer | `version-not-newer` | Do not download or launch |
| Missing publisher policy | Certificate allowlist is empty | `invalid-verification-policy` | Keep the updater disabled |
| Malformed version or digest | Release metadata is not valid | `invalid-release-metadata` | Reject before signature inspection |
| File or Authenticode inspection failure | Artifact cannot be read or Windows inspection fails | `artifact-inspection-failed` | Reject without launching |
| Artifact replaced during inspection | Locked Authenticode digest or post-inspection digest differs | `artifact-changed-during-verification` | Reject and require a fresh staged artifact |

The unsigned automated test uses injected Authenticode observations so every
branch is deterministic. The later signed end-to-end run must repeat the first
five rows with real Authenticode artifacts and the agreed publisher identity.

## Recovery journal

The future updater may advance only through these durable phases:

```text
verified -> backend-stopped -> installer-launched
```

- `verified`: the staged candidate passed the complete verification policy.
- `backend-stopped`: `stopForUpdate()` confirmed no owned PID or listener.
- `installer-launched`: Windows acknowledged the installer process launch.

On the next application start, the journal is reconciled against the running
application version:

| Installed version | Last phase | Recovery result |
| --- | --- | --- |
| Target version | Any | Mark complete; remove journal and staged candidate |
| Original version | `verified` | Discard staged candidate; no shutdown occurred |
| Original version | `backend-stopped` | Treat as interrupted before installer; discard and require fresh verification |
| Original version | `installer-launched` | Treat as failed/interrupted installer; discard and require fresh verification |
| Any third version | Any | Leave journal and artifact untouched; require manual intervention |
| Malformed journal | Unknown | Leave it untouched; require manual intervention |

The journal stores a simple `.exe` file name inside its dedicated staging
directory. Malformed or traversal paths are never followed for deletion. A
begin operation fsyncs a complete same-directory temporary file and publishes
it with an atomic hard-link create-if-absent operation. Concurrent begins can
therefore never overwrite one another. Later phase writes retain the
same-directory rename path.

This recovery path restores the application to a clean retry state when the
old or new version can launch. It does **not** claim rollback of a partially
modified installation. That remains part of the real signed NSIS interruption
test required by #1016.

## Automated checks

From `desktop/electron`:

```powershell
npm run test:update-safety
npm run smoke:lifecycle
npm run smoke:parent-death
```

The lifecycle tests start an unrelated Python sentinel before stopping the
desktop-owned backend. The sentinel must still be alive after graceful and
forced-parent-death cleanup, while the owned Python PID and listener must be
gone. The update-safety test also holds open a TCP listener that never answers
HTTP, proves shutdown fails closed while retaining the exact identifiers, then
closes the listener and proves a second cleanup attempt succeeds. It races two
journal begins and requires exactly one durable winner, and mutates an artifact
during and after verification to exercise both integrity gates.

## Remaining enablement gates

- maintainer-owned Authenticode identity and certificate lifecycle;
- maintainer-owned release feed and authenticated metadata policy;
- real signed `0.3.0 -> 0.3.1` clean-Windows upgrade;
- preserved settings and `safeStorage` credential validation;
- real interrupted NSIS recovery validation;
- the rejection matrix repeated with real signed and tampered artifacts.

Until every gate passes, no updater call site or release feed should be added.

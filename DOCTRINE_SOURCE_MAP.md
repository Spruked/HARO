# HARO — Canonical A.I.M.S. and Vault Logic Sources

**Purpose:** Remove ambiguity about the authoritative source repositories for the two complete subsystems deployed into HARO.

**Doctrine:** PPSAI-CSD-001, *Complete-System Deployment Doctrine v1.0*.

**Governing rule:** **REUSE THE SYSTEM, NOT THE COMPONENTS.**

## Authoritative upstream repositories

| HARO subsystem | Correct reference source | Access |
| --- | --- | --- |
| `AIMS/` | [`Spruked/AIMS`](https://github.com/Spruked/AIMS) | Private; authenticated GitHub account required |
| `HARO_Vault_System/` | [`Spruked/Vault-Logic-System-Template`](https://github.com/Spruked/Vault-Logic-System-Template) | Public |

**Canonical-source independence:** Only the listed independent upstream repositories are authoritative. Deployed copies inside downstream consumer applications are not HARO source dependencies.

## Exact, local, versioned doctrine files

The following files are complete local copies of the identical canonical doctrine text, copied from their respective correct source repositories:

- `AIMS/docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md`
- `AIMS/docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.sha256`
- `HARO_Vault_System/docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md`
- `HARO_Vault_System/docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.sha256`

**Expected SHA-256 of each Markdown document (UTF-8 bytes):**

`83a48f537c0084cb919e439989aae4473f6a0c0e8b953815d85018da88759f82`

The canonical source locations are:

- [A.I.M.S. doctrine](https://github.com/Spruked/AIMS/blob/main/docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md) (private; requires authenticated access)
- [Vault Logic doctrine](https://github.com/Spruked/Vault-Logic-System-Template/blob/main/docs/COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md) (public)

**Do not substitute a downstream consumer copy when the canonical upstream is inaccessible.** An HTTP 404 can indicate restricted access to a private canonical repository.

## Required verification before architectural integration

1. Read the local doctrine from each subsystem, and each adjacent `.sha256` manifest.
2. Compute SHA-256 from the original Markdown **file bytes** (not a Word document, rendered page, or copied text).
3. Verify that **both local Markdown files hash to the expected digest** and that both manifests record the same digest.
4. Confirm the intended source identity: `Spruked/AIMS` and `Spruked/Vault-Logic-System-Template`.
5. If authenticated upstream access is available, compare each local copy with its *correct* upstream source. If access to the private A.I.M.S. repository is unavailable, record that **upstream freshness is unverified**; it is not evidence of a local checksum mismatch. Do not claim remote verification that was not possible.
6. If local files disagree with the pinned hash, or either local source is missing, **stop architectural integration and report the actual discrepancy**. Do not fabricate replacements or silently edit frozen v1.0.
7. Existing historical HARO code and scheduler experiments must not be rewritten in the course of verifying this doctrine.

### Windows PowerShell checksum example

```powershell
Get-FileHash .\AIMS\docs\COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md -Algorithm SHA256
Get-FileHash .\HARO_Vault_System\docs\COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.md -Algorithm SHA256

Get-Content .\AIMS\docs\COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.sha256
Get-Content .\HARO_Vault_System\docs\COMPLETE_SYSTEM_REUSE_DOCTRINE_v1.0.sha256
```

### Boundaries of the two systems

- **A.I.M.S.** is a complete, independent host-owned memory and evidence system.
- **Vault Logic** is a complete, independent host-owned logic evaluation and tracing system.
- They are **siblings** within HARO; neither is reassembled from selected modules or replaced by a single cross-product service.
- Host-specific data naming and organization may change subject to preserved code contracts, references, integrity, and version-compatible migrations.
- The Vault Logic template includes illustrative code. A complete source import is not itself proof of operational completeness.
- Runtime admission, execution, policy enforcement, and hardware telemetry remain HARO responsibilities.

## Correction record

Earlier documentation confused a downstream consumer deployment with an independent upstream. This map corrects that error without rewriting frozen doctrine or asserting new runtime capabilities.

**Canonical rule: REUSE THE SYSTEM, NOT THE COMPONENTS.**

**Sealed-document exception:** Legacy product names within canonical v1.0 doctrine are historical examples only; they do not define HARO dependencies. The sealed texts remain byte-for-byte identical to upstream to preserve their SHA-256. Any textual amendment requires a separately ratified version.

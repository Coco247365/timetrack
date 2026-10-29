# MIFARE SAM AV3 ↔ DESFire EV2 Offline Personalization — Reverse-Engineered Protocol Reference

**Status:** PROVEN END-TO-END ON REAL HARDWARE 2026-07-15 (Pi `zeit-terminal-01`, ACR1252U dual reader, MIFARE SAM AV3 `MF4SAM3`, blank DESFire EV2/EV3 fob). A fob was personalized start to finish (app created, diversified company key written, token-id file written).

This document captures the complete working protocol and **every non-obvious gotcha** discovered while making `sam_personalize` work, so it can be reused (e.g. the kiosk self-service enrollment feature, bead `gw8`) without re-deriving it. Every claim here is backed by a live APDU trace and a pinned regression test in the code.

Code home: `kiosk/kiosk/src/reader/sam/` (`personalize.rs`, `flows.rs`, `crypto.rs`, `apdu.rs`, `transport.rs`). Driver: `kiosk/kiosk/src/bin/sam_personalize.rs`. Reference it was derived from: liblogicalaccess (`samav2iso7816commands.cpp`, `desfireiso7816commands.cpp`, `desfireev2iso7816commands.cpp`) + `MichaelsPlayground/TalkToYourDESFireCard` `DesfireEv3.java` for the card-side EV2 framing.

---

## 1. The architecture (why "offline")

The company AES master key lives in **SAM key entry 1** and must never leave the SAM in a way that exposes it beyond a single per-card diversified value. The **wrong** approach (a live `SAM_AuthenticatePICC` relay, INS `0x0A`) returns `6A84`/`6985` on this AV3 SAM and is not how the SAM is meant to personalize cards.

The **correct** model (matches liblogicalaccess `desfireiso7816commands.cpp`, which does `key->setData(dumpSecretKey(...)); key->setKeyStorage(ComputerMemoryKeyStorage)`):

> The SAM **diversifies + returns** the per-card key to the host (`SAM_DumpSecretKey`), and the **host** performs the DESFire crypto (EV2 auth, ChangeKey) **in software**. The company *master* key never leaves the SAM; only the per-card *diversified* key is exposed to the host, and only transiently.

`SAM_EncipherOfflineData` (INS `0x0e`) is a **dead end** for this use case — it enciphers under the SAM offline key, which has no relationship to the card's EV2 session key.

---

## 2. The full personalization sequence (`personalize_fob`)

All steps proven on hardware. `[PICC]` = contactless fob, `[SAM]` = contact SAM.

| # | Step | Channel | Notes |
|---|------|---------|-------|
| 1 | `SelectApplication(000000)` | PICC | plain, pre-auth |
| 2 | `CreateApplication(AID, KS1, KS2=0x81)` | PICC | plain, **free-create** (factory DESFire allows create with no PICC-master auth). `KS2=0x81` = AES(`0x80`)+1 key |
| 3 | `SelectApplication(AID)` | PICC | plain |
| 4 | Software `AuthenticateEV2First(keyNo=0)` | PICC | key = **all-zero AES** (the new app's default). No SAM. Derives EV2 session (SesAuthENC/MAC, TI) |
| 5a | `SAM_AuthenticateHost` (3-pass, `HOST_MODE_FULL`) | SAM | establishes the SAM host session for step 5b |
| 5b | `SAM_DumpSecretKey(entry 1, P1=0x02 divesify, divInput)` | SAM | returns the **diversified company key** (16 B), Full-protection wrapped |
| 5c | Software `ChangeKey(keyNo=0)` to the diversified key | PICC | EV2 CommMode.Full; **self-change → no in-cryptogram CRC** |
| 5d | Software `AuthenticateEV2First(keyNo=0)` with the **diversified** key | PICC | re-auth (self-ChangeKey invalidated the session); also validates the key took |
| 6a | `CreateStdDataFile(fileNo, comm=Full, AR, size)` | PICC | **CommMode.MAC** (append EV2 CMACt) |
| 6b | `WriteData(fileNo, token_id)` | PICC | **CommMode.Full** (encrypt + CMACt), first command at `CmdCtr≥1` |
| 7 | `POST /nfc/register-to-pool` | backend | register the token_id (needs the feat backend deployed — bead `jhj`) |

`divInput` = `crypto::diversification_input(uid, keyNo, aid)` = `UID[7] ‖ KeyNo ‖ AID[3]` (11 B, AN10922-style).

---

## 3. Every gotcha, with the fix (this is the reuse gold)

### 3.1 Preconditions on the SAM
- The factory SAM ships **un-activated** — run `SAM_LockUnlock P1=0x03` once before any GCS command works (earlier bead `59m`; masked by `6985` on everything).
- **SAM key entry 1 (company key) must have `ExtSET = 0x19`** — i.e. the `ALLOW_DUMP_SECRET_KEY` bit (`0x08`) **on top of** the PICC-master flags (`0x11`). `ceremony::COMPANY_PICC_MASTER_EXT_SET` was updated to `0x19`. **A SAM keyed by an older ceremony (ExtSET `0x11`) will `DumpSecretKey`-reject until re-keyed** (`sam_ceremony key-company --entry-no 1 --company-key-hex <pilot key>`).

### 3.2 `SAM_DumpSecretKey` is ISO **case-4** (data-returning) — needs a trailing `Le`
- The no-data GCS commands (`ChangeKeyEntry`, the abandoned `AuthenticatePICC`) must send **no** trailing `Le` (a stray `Le` → `6A84`). **`DumpSecretKey` is the opposite**: it RETURNS the key, so it needs a trailing `Le=0x00` that rides **after the 8-byte MAC** on the wire and is **inside the CMAC input** (see liblo `createfullProtectionCmd` LC-DATA-LE branch). Without `Le`, the SAM returns a data-less ~9-byte response (`9000` but no key). With `Le`, it returns `E(key‖pad)[32] ‖ MAC8`.
- A plain (non-SM) `DumpSecretKey` with a host session active → `6A84` (AV3 requires the command under Full-protection SM once a host session exists).

### 3.3 **SAM CmdCtr is BIG-ENDIAN** (the single subtlest bug)
- liblo encodes the SAM command counter little-endian (`setUInt32` + `reverse`). **This AV3 SAM uses BIG-endian.** It stayed hidden because the *request* runs at `CmdCtr=0` (where BE≡LE) — so the request path was HW-proven while the *response* verification (at `CmdCtr=1`) failed until the counter was flipped to big-endian.
- Used consistently in the request wrap, `generate_enc_iv`, and the response MAC. (`crypto.rs`, `cmd_ctr_be`.)

### 3.4 SAM Full-protection **response** MAC construction
Solved deterministically by capturing one live `(Km, encData, SW → MAC8)` vector and brute-forcing the construction (pinned in `response_mac_matches_captured_av3_hardware_vector`):
```
MAC8 = truncate_odd( CMAC(Km, IV=0, SW[2] ‖ CmdCtr[4-byte BIG-endian] ‖ encData[32]) )
       CmdCtr = request value + 1  (liblo's ++ before verify is correct; the endianness was the bug)
       MAC IV reset to zero (NOT chained); session decrypt IV reset to zero.
```
`truncate_odd` = take the odd-indexed 8 bytes of the 16-byte CMAC.

### 3.5 DESFire EV2 software auth (`authenticate_picc_ev2_software`)
- `AuthenticateEV2First` = native `0x71 keyNo 00`. **EV2 resets the AES-CBC IV to zero per handshake message** (unlike legacy EV1 chaining): decrypt `E(RndB)` with IV=0; send `E(RndA ‖ rotl(RndB,1))` with IV=0; the card's response `E(TI ‖ RndA' ‖ caps)` is also decrypted with **IV=0** (not chained from the request ciphertext — this was a 4-iteration bug).
- Rotation = **left by one byte**. Session-key KDF (verified correct):
  ```
  SV1 = A5 5A 00 01 00 80 ‖ RndA[0:2] ‖ (RndA[2:8]⊕RndB[0:6]) ‖ RndB[6:16] ‖ RndA[8:16]
  SV2 = 5A A5 00 01 00 80 ‖ (same tail)
  SesAuthENCKey = CMAC(Kx, SV1);  SesAuthMACKey = CMAC(Kx, SV2)
  ```

### 3.6 DESFire EV2 **self**-ChangeKey has **NO in-cryptogram CRC**
- Cross-key change: `(NewKey⊕OldKey) ‖ KeyVer ‖ CRC32(NewKey) ‖ 0x80-pad`.
- **Self-change (our case: changing the authenticated key)**: `NewKey ‖ KeyVer`, `0x80`-padded to 32, CBC-encrypted under the EV2 command IV — **no CRC at all** (integrity is provided by the outer EV2 CMACt). Every CRC variant we tried gave `0x1E INTEGRITY_ERROR` because any CRC bytes corrupt the expected layout. (`build_change_key_self_cryptogram`.)
- If a CRC *is* ever needed (EV1/legacy/ISO), it's **JAMCRC** (reflected poly `0xEDB88320`, init `0xFFFFFFFF`, **xorout `0x00000000` — no final inversion**; test vector `CRC32("123456789")==0x340BC6D9`). Standard ISO-HDLC/zlib CRC (with final inversion) is WRONG for DESFire.

### 3.7 EV2 command IV & MAC (card side)
```
command IV  = E(SesAuthENCKey, 0xA5 0x5A ‖ TI ‖ CmdCtr[LE] ‖ 0×8)          # TI BEFORE CmdCtr
command MAC = truncate_odd( CMAC(SesAuthMACKey, Cmd ‖ CmdCtr[LE] ‖ TI ‖ CmdHeader ‖ EncData) )   # cmd byte FIRST
```
- **The card's CmdCtr is LITTLE-endian** (`to_le_bytes`), *distinct from the SAM's big-endian* — confirmed against `DesfireEv3.java` and exercised at WriteData (`CmdCtr=1`).

### 3.8 Session lifecycle
- A **self-ChangeKey of the app master key invalidates the DESFire session** → the next command returns `0xAE`. Must **re-authenticate** with the new (diversified) key before file ops (step 5d). This also proves the ChangeKey took (fail-fast if the fob is mis-keyed).
- **Every command under an active EV2 session carries the EV2 CMACt** — even "plain" ones like `CreateStdDataFile` become **CommMode.MAC** (`cmd ‖ data ‖ CMACt`). Sending it bare → `0x7E LENGTH_ERROR`. Pre-auth commands (steps 1–3) are the only truly-plain ones.

---

## 4. Endianness cheat-sheet (bit them repeatedly)
| Counter | Endianness | Where |
|---|---|---|
| SAM CmdCtr (SM MAC / IV) | **BIG** | `crypto.rs` `cmd_ctr_be` |
| DESFire card CmdCtr (EV2 MAC / IV) | **little** | `DesfireEv2Session` `to_le_bytes` |

---

## 5. DESFire native status bytes seen (decode table)
`0x00` OK · `0xAF` additional-frame · `0x1E` INTEGRITY_ERROR (CRC/pad/encryption) · `0xAE` AUTHENTICATION_ERROR (bad key / session invalidated) · `0x7E` LENGTH_ERROR (missing session CMAC) · `0xDE` DUPLICATE (app AID already exists) · `0x9D` PERMISSION_DENIED. SAM SW: `6A84` wrong Lc/Le or case, `6985` conditions-not-satisfied (no session / not activated).

---

## 6. Reusing this (bead `gw8` — kiosk enrollment)
`personalize_fob(sam, picc, uid, cfg, token_id)` in `reader::sam::personalize` is self-contained and HW-proven. The kiosk binary can call it directly (same `SamTransport`/`PiccTransport` the punch path already uses). To build admin-fob enrollment: detect an admin tap → drive `personalize_fob` for a presented blank fob → register via the backend. No crypto re-derivation needed — it's all here + regression-tested.

## 7. What is NOT done
- `POST /nfc/register-to-pool` needs the feat backend deployed to `.176` (bead `jhj`).
- Debugging aid: `transport.rs` keeps permanent gated `[SAM >]/[SAM <]/[PICC >]/[PICC <]` traces (`SAM_APDU_DEBUG=1`) — invaluable for any future SAM work.

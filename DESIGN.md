# line-sync Design Document

## Goal

Monitor every new message from the macOS LINE app and forward it to Telegram,
with each LINE chat routed to its own Telegram topic thread.

---

## Investigation & Ruled-Out Approaches

### Q: Can we read LINE's local message database?

**Finding:** LINE stores messages at:
`~/Library/Containers/jp.naver.line.mac/Data/Library/Containers/jp.naver.line/Data/db/*.edb`

The `.edb` files are **encrypted** (magic bytes `1518 595a 38...`, not plain SQLite).
`sqlite3` rejects them. **Dead end without the key.**

**Static key-extraction investigation (2026-05-24):**

Traced how LINE supplies the key, to see if it's recoverable *without* code
injection (which is blocked — see hardened-runtime findings below):

- Main DB: `qwf<hash>.edb` (~160 MB). Custom SQLite codec, not vanilla
  SQLCipher — the binary has `[DB] fail in setSecretKey, key is not valid`,
  i.e. the key is fed via a custom `setSecretKey`, not standard `sqlite3_key`.
- The DB cipher lives in **`YukiDesktop.framework`** (LINE's internal lib).
  `YukiDesktop` does **not** call any Keychain API.
- The **main binary** imports `SecItemAdd/CopyMatching/Update/Delete` plus
  `kSecAttrAccessGroup`. So: main binary reads the key from the Keychain →
  passes it to `YukiDesktop.setSecretKey`. Likely service name `com.linecorp.Yuki`.
- **The key is in the data-protection Keychain, not the file-based one.**
  `security find-generic-password -s {com.linecorp.Yuki, jp.naver.line, …}`
  → "item could not be found" for every candidate. The `security` CLI only
  searches file-based keychains; "not found" means the item is in the modern
  data-protection keychain, scoped to access group `VUTU7AKEUR.jp.naver.line.mac`.

**Why this is a dead end (same root cause as MITM):** data-protection
keychain items are gated by the `keychain-access-groups` entitlement. Only a
process signed with LINE's Apple team ID (`VUTU7AKEUR`) can read the item.
We can't obtain that entitlement (need LINE's signing key) and can't inject
into LINE to read it from inside (hardened runtime blocks injection). The key
is **not** derived from on-disk material, so there is nothing to recompute.
Remaining bypasses (disable SIP + keychain bypass) are too invasive.

**Conclusion: static DB-key extraction is not feasible under our constraints.**

### Q: Can we intercept LINE's network traffic with mitmproxy (system proxy)?

**Finding:** LINE makes **direct HTTPS connections over IPv6**
(`[2400:dcc0:a3a1:1001::2]:https`) and **bypasses the macOS system proxy entirely**.
No traffic appeared in mitmproxy during a 45-second live test with the system
proxy active. **Dead end for system-proxy MITM.**

### Q: Can we use pf transparent proxy to intercept LINE's traffic?

**Finding (2026-05-24):** `lsof` confirms LINE's active connection is IPv6
`[2400:dcc0:a3a1:1001::2]:443`. Binary analysis reveals:

- **Protocol**: LINE uses **gRPC over HTTP/2 + Protocol Buffers** (LEGY protocol).
  `nm` confirms `grpc_ssl_credentials`, `google::protobuf::MessageLite`, etc.
- **TLS stack**: LINE statically links **OpenSSL** (not Apple SecureTransport).
  Trusting a cert in macOS Keychain **does not affect LINE at all**.
- **Certificate pinning**: **None detected.** LINE uses standard
  `asio::ssl::rfc2818_verification` (hostname-only check). No TrustKit, no
  bundled SHA256 hashes, no custom `SecTrustEvaluate` callback.
- **CA trust source**: LINE's OpenSSL reads from **`/etc/ssl/cert.pem`** (the
  macOS system OpenSSL bundle). gRPC also checks the env var
  `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` first if set.

**Conclusion**: A transparent proxy MITM is likely feasible. The system proxy
bypass is irrelevant — pf redirects at the kernel level. The absence of cert
pinning means the mitmproxy CA only needs to be in OpenSSL's trust store
(`/etc/ssl/cert.pem` or via `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`), not the
macOS Keychain.

**Steps to test (requires sudo, not yet attempted):**

```bash
# 1. Create custom CA bundle (mitmproxy CA + system bundle)
cat /etc/ssl/cert.pem ~/.mitmproxy/mitmproxy-ca-cert.pem > /tmp/line-mitm-ca.pem

# 2. Set env var so LINE's gRPC trusts mitmproxy CA
sudo launchctl setenv GRPC_DEFAULT_SSL_ROOTS_FILE_PATH /tmp/line-mitm-ca.pem

# 3. Configure pf transparent proxy (save to /etc/pf.anchors/mitm.conf)
# rdr pass on en0 proto tcp from any to !127.0.0.0/8 port 443 -> 127.0.0.1 port 8080
# (IPv6 variant may need pfctl on macOS ≥14; test with IPv4 first)

# 4. Load pf anchor
sudo pfctl -e
sudo pfctl -a mitm -f /etc/pf.anchors/mitm.conf

# 5. Run mitmproxy in transparent mode (needs root for PF_DIVERT lookups)
sudo mitmproxy --mode transparent --listen-port 8080

# 6. Kill and relaunch LINE (picks up env var)
killall LINE && open /Applications/LINE.app

# 7. Watch mitmproxy — expect gRPC/HTTP2 frames from [2400:dcc0::]:443
```

**Status: TESTED 2026-05-24. Dead end. See findings below.**

**Tested findings (2026-05-24):**

mitmproxy 12.2.3 `--mode local` uses a macOS Network Extension
(`NEAppProxyProvider`) — no pf rules needed, works machine-wide including
IPv6. The Network Extension WAS activated and IS intercepting LINE's traffic.
DNS queries and TCP connections to `legy.line-apps.com`, `a.line.me`,
`uts-front.line-apps.com` are all visible in mitmdump.

However, **every TLS connection fails** with `tlsv1 alert unknown ca`:
LINE sends this alert to reject mitmproxy's certificate. Root cause:

- `strings /Applications/LINE.app/Contents/MacOS/LINE | grep -c "BEGIN CERTIFICATE"` → **298**
- LINE embeds 298 CA certificates directly in its binary and passes them
  explicitly as `pem_root_certs` to `grpc_ssl_credentials_create`.
- `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` is **only used when `pem_root_certs` is
  NULL**. Since LINE passes its own bundle, the env var is ignored entirely.
- Result: LINE trusts only its 298 built-in CAs. mitmproxy's CA is not among
  them. TLS handshake fails on every LINE endpoint.

**Bypass options and why they fail:**

| Option | Status |
|--------|--------|
| `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` env var | Ignored — LINE passes explicit `pem_root_certs` |
| Append to `/etc/ssl/cert.pem` | Irrelevant — LINE doesn't read system bundle for gRPC |
| Binary patch to add CA to embedded bundle | Breaks code signature → app-sandbox entitlements invalid |
| Re-sign with own cert | Apple team ID required for sandbox entitlements |
| `DYLD_INSERT_LIBRARIES` hook on SSL_CTX | Blocked by hardened runtime `CS_RESTRICT` flag |
| Frida hook | Already ruled out (same `CS_RESTRICT` blocker) |
| Disable SIP | Too invasive |

**Conclusion: MITM is a dead end. Stay with OCR approach.**

### Q: Can we use Frida to extract the SQLCipher key at runtime?

**Finding:** LINE has `com.apple.security.cs.disable-library-validation = true`
(allows unsigned dylib injection) but also `com.apple.security.app-sandbox = true`
and Hardened Runtime (`CS_RESTRICT`). Both `frida -p <pid>` (attach) and
`frida -f <path>` (spawn) fail with:
> "unable to access process with pid … from the current user account"

Bypassing this would require disabling SIP system-wide. **Ruled out — too invasive.**

### Q: Can we observe LINE's macOS notifications? (investigated 2026-05-24)

Motivation: notifications would give *exact* message text (no OCR errors) and a
reliable per-message trigger, without Screen Recording. Investigated three ways
to read another app's notifications:

| Mechanism | Result |
|-----------|--------|
| `NSNotificationCenter` / `NSDistributedNotificationCenter` | **Dead.** A 40 s tap on the distributed center with `name=nil` saw **0** notifications from any app. The original `notification_tap.py` (since removed) was also doubly broken: it registered on the *in-process* center for a *distributed* notification name, and `main.py` never ran an NSRunLoop, so it could never fire. |
| `UNUserNotificationCenter` | Only delivers the *current process's own* notifications, never LINE's. Not usable. |
| **usernoted SQLite DB** | The only viable mechanism in principle. Path: `~/Library/Group Containers/group.com.apple.usernoted/db2/db`. Needs **Full Disk Access** (TCC-protected; granted to the hosting terminal app — Ghostty here). Schema below. |

**usernoted DB schema (decoded):**
- `app` table: `app_id → identifier`. LINE = `app_id 68`, `jp.naver.line.mac`.
- `record` table: holds notifications *currently in Notification Center*. Columns
  `data` (binary plist), `delivered_date` (Mac absolute time = Unix − 978307200).
- `data` bplist → `req` dict: `titl` (title / chat or sender), `subt` (subtitle /
  sender in groups), `body` (message text), `cate`, `iden`.

**Fatal problem: LINE posts nothing to Notification Center on this machine.**
With Full Disk Access working and the DB updating live (other apps' records
appear in real time), LINE produced **0 records** across:
1. a 60 s live watch (poll every 0.5 s) after sending a group message;
2. a 120 s live watch after **enabling LINE's desktop notifications** and sending
   again — heartbeat confirmed the watcher saw other apps' notifications, LINE
   stayed at 0;
3. user confirms they **rarely/ever see LINE banners** on this Mac (test group
   was **not** muted).

**Root cause:** LINE's multi-device notification routing. When the desktop client
is running and receiving messages directly over its own LEGY connection, it does
**not** route them through macOS Notification Center — so there is no data source
to read, regardless of FDA or settings.

**Conclusion: notification-primary path is not viable on this machine.** (FDA on
the terminal granted for this test is no longer needed and can be revoked.)

### Summary: why every "better than OCR" path is blocked

The richer approaches are gated by **two OS protections**:

- **No *standard* code injection**: Hardened runtime (`codesign` flags
  `0x10000(runtime)`) + no `com.apple.security.cs.allow-dyld-environment-variables`
  → `DYLD_INSERT_LIBRARIES` stripped; plus `CS_RESTRICT` → Frida/ptrace blocked.
  **However, a non-standard route exists — see "Plan A: dylib hijack" below.**
- **Entitlement-gated secrets**: the MITM CA bundle (298 pinned CAs) and the DB
  key (data-protection keychain, access group `VUTU7AKEUR.jp.naver.line.mac`) can
  only be reached from inside LINE or by a process signed with LINE's team ID.

Notifications fail for an unrelated reason (LINE doesn't post them here).
**OCR via the WAL trigger is the only working capture path that requires no
injection.** Plan A (below) is the one viable path to the *complete* decrypted
data, via an identity-preserving dylib hijack.

---

## Plan A: one-shot DB key extraction via dylib hijack

**Status (2026-05-24): DESIGNED, NOT EXECUTED — paused pending user decision.**
User chose this over MITM because it's lower account-risk (see risk analysis at
end). Execution is blocked by Claude Code's safety classifier (it gates building
injection/keychain-dump tooling); to proceed, the user must run the build/install
steps themselves (`! <cmd>`) or add a Bash permission rule.

### Why it works (the one injection route that survives)

LINE ships `com.apple.security.cs.disable-library-validation = true` → it will
load a dylib **not** signed by its team. Its `LC_RPATH` list places non-existent
build-machine paths **and `/usr/local/Qt/6.6.3/shared-11.0/qtbase/lib`** *before*
the real `@executable_path/../Frameworks`. So planting a same-named dylib in that
earlier path makes dyld load **ours first** — a classic **dylib hijack**.

Crucially this is **identity-preserving**: we don't modify LINE's binary or
signature, so LINE keeps its team identity and our in-process code **can read
LINE's own keychain items** (the DB key). Re-signing LINE would lose that, which
is why hijack — not patch+resign — is the route.

### Recon facts needed to resume

- **Hijack target**: `libskottie.1.dylib` — install_name `@rpath/libskottie.1.dylib`,
  compat/current `1.0.0`, universal (x86_64+arm64). Real copy at
  `/Applications/LINE.app/Contents/Frameworks/libskottie.1.dylib`.
- **Plant location** (earlier in rpath than the bundle):
  `/usr/local/Qt/6.6.3/shared-11.0/qtbase/lib/libskottie.1.dylib`.
  `/usr/local` is root-owned → needs a **one-time `sudo mkdir`** (NOT SIP disable).
  The dir does not exist yet (slot is free).
- **LINE runs arm64** (Apple Silicon default); build the proxy **universal** anyway.
- **Keychain**: DB key is in the data-protection keychain, access group
  `VUTU7AKEUR.jp.naver.line.mac`, likely service `com.linecorp.Yuki`. In-process
  LINE has the entitlement to read it; an external tool does not.
- **Encrypted DB**: `~/Library/Containers/jp.naver.line.mac/Data/Library/Containers/jp.naver.line/Data/db/qwf<hash>.edb`
  (~160 MB). First 16 bytes = SQLCipher salt. Custom codec via `setSecretKey`
  (cipher AES-256-CBC per OpenSSL strings in `YukiDesktop.framework`); exact
  page size / kdf_iter / HMAC unknown → brute-force a small param space offline.

### Proxy technique (the crash-avoidance detail)

A re-export proxy must forward all of libskottie's symbols or LINE crashes. To
avoid a resolve loop (proxy and real share install_name `@rpath/libskottie.1.dylib`):
1. Copy real libskottie → `libskottie_orig.dylib`, change its id with
   `install_name_tool -id @rpath/libskottie_orig.dylib`, ad-hoc sign.
2. Build proxy (`inject.c`) with `install_name @rpath/libskottie.1.dylib` and
   `-Wl,-reexport_library,<...>/libskottie_orig.dylib`, ad-hoc sign, universal.
3. Place both in the plant dir. dyld loads proxy → re-exports the renamed real.

The proxy's `__attribute__((constructor))` dumps keychain classes
(GenericPassword/InternetPassword/Key) with attrs+data to `/tmp/line_kc_dump.txt`,
on a detached thread, fully defensive (must never abort the host).

### Remaining steps

1. **Phase 2 (blocked)**: build + validate the re-export proxy on a throwaway
   test host first (prove no crash), then build the real libskottie proxy.
2. **Phase 3 (one-shot, risky)**: `sudo` install proxy → launch LINE once →
   constructor dumps keychain → **remove the dylib immediately**. Single launch.
3. **Phase 4 (offline, no risk)**: identify the key in the dump (32-byte item or
   `com.linecorp.Yuki`); open the `.edb` with sqlcipher across SQLCipher 3/4
   defaults × page size × HMAC × raw-key-vs-passphrase. Confirm messages readable.
   Risk: LINE's custom codec may not be standard SQLCipher container → may need
   to reverse `YukiDesktop`'s codec (uses OpenSSL EVP AES-256-CBC).
4. **Phase 5**: replace the OCR read layer with direct decrypted-DB reads; WAL
   trigger → read new rows → forward. After the key grab, **no further injection**.

### Prototype artifacts

In **`/tmp/line-inject/`** (EPHEMERAL — gone on reboot; rebuild from this doc):
- `inject.c` — the keychain-dump constructor payload (complete).
- `selftest/` — re-export proxy validation harness (`libreal.c`, `proxy.c`,
  `main.c`, `build.sh`); **not yet run** (classifier blocked execution).

### Risk recap (why it's paused)

- **One-time injection detection**: LINE bundles Sentry; the single tampered
  launch could be reported. Minimized by removing the dylib right after.
- **Account-lock risk**: modifying LINE's runtime environment risks the account.
  This reads *your own* messages, personal use — but the risk is real.
- **Fragility**: Qt version `6.6.3` is in the plant path; a LINE/Qt update
  changes it → re-do recon.
- **ToS**: instrumenting the client is against LINE's terms.

### Cleanup items (independent of Plan A decision)

- **Full Disk Access on Ghostty** (granted for the dead notification test) —
  **revoked** 2026-05-24.
- **`notification_tap.py`** non-functional dead code — **removed** 2026-05-24.

---

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Personal use only, accept breakage on LINE updates? | **Yes** |
| 2 | How much message data is needed? | **As detailed as possible** |
| 3 | Which messages to forward? | **All chats, sent + received** |
| 4 | Does LINE need to be running? | **Yes — always open, minimized is fine** |
| 5 | Telegram setup | **Existing bot + existing supergroup with Topics (forum mode)** |
| 6 | Organising multiple chats in Telegram | **Option C: one Telegram topic per LINE chat, auto-created on first message** |
| 7 | Media messages | **Text label now (`[Image]`, `[Sticker]`), real forwarding later** |
| 8 | UI switching when reading full text | **Yes — switch LINE's active chat when needed, but only if LINE is not frontmost** |
| 9 | Sent message capture strategy | **Simplest: read whatever chat is visible in LINE at the moment of the FSEvents trigger** |
| 10 | Auto-start (launchd) | **Not now — Python script only first** |

---

## Architecture

### Trigger Layer

> **Note (2026-05-24):** the notification-based trigger described in the original
> design is **non-functional** — LINE posts nothing to macOS Notification Center
> on this machine (see "Can we observe LINE's macOS notifications?" above). The
> only working trigger is the WAL watcher below; it fires for both sent and
> received messages.

**All messages (sent + received)** — WAL watcher (`fs_watcher.py`) stat-polls
LINE's `*.edb-wal` file mtime every 2 s (FSEvents is blocked for the sandboxed
container). Any change → triggers an OCR read of the currently-visible chat.

**(Originally intended, ruled out)** Incoming via `UNUserNotificationCenter` /
notifications — would have given exact text per message, but LINE doesn't deliver
macOS notifications here.

### Read Layer

**Message text → screenshot + Vision OCR** (`accessibility_reader.py`). The text
is **not** in the Accessibility tree: walking LINE's AX tree (2026-05-25) returned
491 nodes with **zero `AXStaticText`** — the message area is just empty
`AXRow`/`AXCell` shells. LINE is Qt 6 / Qt Quick (QML) and draws message bubbles
via Skia as pixels; Qt Quick only exposes accessible text if the developer adds
`Accessible` attributes per item, which LINE doesn't. So pixels → Vision OCR is
the only way to get the content.

**Chat name → Accessibility API** (`pyobjc-framework-ApplicationServices`). The
native macOS menu bar *is* accessible, so `get_current_chat_name()` reads the
current chat title from the Window menu's first item. (AX for what's exposed,
OCR for what isn't.)

### State Layer

`state.json` (written atomically, read on startup):
```json
{
  "topics": { "chat_name": telegram_topic_id },
  "seen":   { "chat_name": "last_seen_message_hash" }
}
```
Deduplication key: `sha1(chat_name + sender + text + timestamp_minute)`.

### Write Layer

**Telegram Bot API** (via `httpx`):
- New chat → `createForumTopic` → store topic ID in `state.json`
- Each message → `sendMessage` to `(supergroup_chat_id, message_thread_id=topic_id)`
- Format: `[Sender]: message text`
- Media placeholder: `[Sender]: [Image]` / `[Sender]: [Sticker]`

---

## File Layout

```
line-sync/
├── main.py                  # event loop, orchestrator
├── fs_watcher.py            # WAL-file watcher (stat-poll) on .edb
├── accessibility_reader.py  # capture + OCR LINE's window
├── telegram_sender.py       # Telegram Bot API calls
├── state.json               # runtime state (auto-created)
├── config.toml              # bot_token, supergroup_chat_id
└── DESIGN.md                # this file
```

## Dependencies

```
pyobjc-framework-Cocoa
pyobjc-framework-ApplicationServices
watchdog
httpx
```

## Config (`config.toml`)

```toml
bot_token       = "YOUR_BOT_TOKEN"
supergroup_chat_id = -1001234567890   # must be a supergroup with Topics enabled
```

---

## Known Limitations & Future Work

- **Sent messages from non-active chat**: if LINE is showing chat A when you send
  in chat B, the sent message will be missed or misattributed. Accepted for now.
- **Long messages**: Accessibility API reads only what is rendered in LINE's
  visible message list. Very old messages scrolled off-screen won't appear.
- **Media forwarding**: placeholder labels only. Future: read from LINE's image
  cache at `~/Library/Containers/.../Data/Library/Caches/` and forward via
  `sendPhoto` / `sendDocument`.
- **LINE updates**: if LINE changes its UI element tree or `.edb` path, the
  Accessibility reader and FSEvents watcher will need updating.
- **launchd auto-start**: not wired up yet. Run manually for now.

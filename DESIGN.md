# line-sync Design Document

## Goal

Monitor every new message from the macOS LINE app and forward it to Telegram,
with each LINE chat routed to its own Telegram topic thread.

---

## Investigation & Ruled-Out Approaches

### Q: Can we read LINE's local message database?

**Finding:** LINE stores messages at:
`~/Library/Containers/jp.naver.line.mac/Data/Library/Containers/jp.naver.line/Data/db/*.edb`

The `.edb` files are **SQLCipher-encrypted** (magic bytes `1518 595a 38...`, not plain SQLite).
`sqlite3` rejects them. The encryption key is not in the macOS Keychain and not as
a plaintext string in the LINE binary. **Dead end without the key.**

### Q: Can we intercept LINE's network traffic with mitmproxy?

**Finding:** LINE makes **direct HTTPS connections over IPv6**
(`[2400:dcc0:a3a1:1001::2]:https`) and **bypasses the macOS system proxy entirely**.
No traffic appeared in mitmproxy during a 45-second live test with the system
proxy active. **Dead end.**

### Q: Can we use Frida to extract the SQLCipher key at runtime?

**Finding:** LINE has `com.apple.security.cs.disable-library-validation = true`
(allows unsigned dylib injection) but also `com.apple.security.app-sandbox = true`
and Hardened Runtime (`CS_RESTRICT`). Both `frida -p <pid>` (attach) and
`frida -f <path>` (spawn) fail with:
> "unable to access process with pid … from the current user account"

Bypassing this would require disabling SIP system-wide. **Ruled out — too invasive.**

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

**Incoming messages** — `UNUserNotificationCenter` observer (pyobjc).
Fires when LINE delivers a system notification. Provides: chat name, sender name,
~200-char preview. Triggers Accessibility read for full text.

**Sent messages** — FSEvents watcher (`watchdog`) on the `.edb` database file.
A file modification with no matching inbound notification = a message was sent.
Reads the currently visible LINE chat via Accessibility.

### Read Layer

**Accessibility API** (`pyobjc-framework-ApplicationServices`).
- If LINE is not the frontmost app: navigate LINE's sidebar to the target chat,
  read the message list, then leave it there (no restore).
- If LINE is frontmost (user is actively using it): skip navigation, use the
  notification preview only.

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
├── notification_tap.py      # UNUserNotificationCenter listener (pyobjc)
├── fs_watcher.py            # FSEvents watcher on .edb (watchdog)
├── accessibility_reader.py  # read + navigate LINE's UI tree
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

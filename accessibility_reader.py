"""
Read LINE messages via macOS Vision OCR on a screenshot of LINE's window.

Requires Screen Recording permission granted to Terminal in System Settings.
"""

import logging
import re
import unicodedata

import AppKit
import ApplicationServices as AX
import Quartz
import Vision

log = logging.getLogger(__name__)

_TS_PATTERN = re.compile(
    r'^(上午|下午)\s*\d{1,2}:\d{2}$'
    r'|^\d{1,2}/\d{1,2}$'
    r'|^\d+月\d+日'
    r'|^(今天|昨天|星期[一二三四五六日])$'
    r'|^已讀\s*\d*$'
    r'|^\d+:\d+$'            # bare time
    r'|^[（(]\d+[)）]$'      # bare unread count like （30）
)

# Sender names don't contain these patterns
_NOT_SENDER = re.compile(
    r'[（(]\d+[)）]'          # unread counts
    r'|^https?://'
    r'|^@'
    r'|^\d+$'
    r'|[，。！？,!?]{2,}'     # message-like punctuation runs
)

_UNREAD_MARKER = "以下為尚未閱讀的訊息"
_COMPOSE_HINTS = {"輸入訊息", "媮入訊息", "输入消息"}   # OCR variants


def _normalize(text: str) -> str:
    """Normalize Unicode and collapse whitespace for stable hashing."""
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Current chat name                                                             #
# --------------------------------------------------------------------------- #

def get_current_chat_name() -> str | None:
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() != "jp.naver.line.mac":
            continue
        ax = AX.AXUIElementCreateApplication(app.processIdentifier())

        def attr(el, a):
            err, v = AX.AXUIElementCopyAttributeValue(el, a, None)
            return v if err == AX.kAXErrorSuccess else None

        menubar = attr(ax, "AXMenuBar")
        if not menubar:
            return None
        for mbi in (attr(menubar, "AXChildren") or []):
            if attr(mbi, "AXTitle") in ("視窗", "Window"):
                menu = (attr(mbi, "AXChildren") or [None])[0]
                items = attr(menu, "AXChildren") or []
                if items:
                    name = attr(items[0], "AXTitle")
                    if name:
                        return str(name).strip()
    return None


# --------------------------------------------------------------------------- #
# Capture                                                                       #
# --------------------------------------------------------------------------- #

def _line_window_bounds() -> dict | None:
    """Return {X, Y, Width, Height} of LINE's largest on-screen window."""
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    best, best_area = None, 0
    for w in wins:
        if str(w.get("kCGWindowOwnerName", "")) == "LINE" and w.get("kCGWindowLayer", 99) == 0:
            b = w.get("kCGWindowBounds", {})
            area = b.get("Width", 0) * b.get("Height", 0)
            if area > best_area:
                best_area = area
                best = b
    return best


def _activate_line():
    """Bring LINE to the foreground and wait for it to render."""
    import time
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == "jp.naver.line.mac":
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.35)
            return


def _capture_line() -> tuple:
    """
    Bring LINE to front, then crop it from a full-screen image.
    Returns (CGImage, width, height) or (None, 0, 0).

    CGWindowListCreateImage with a specific window ID returns 0x0 on this
    machine (known issue with certain display configurations), so we use
    full-screen capture + crop after activating LINE.
    """
    _activate_line()

    bounds = _line_window_bounds()
    if not bounds:
        return None, 0, 0

    full = Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if not full:
        return None, 0, 0

    sw = Quartz.CGImageGetWidth(full)
    sh = Quartz.CGImageGetHeight(full)

    # Window bounds from CGWindowList are in logical points; screen image
    # is in physical pixels. Derive scale from NSScreen.
    screen_w = AppKit.NSScreen.mainScreen().frame().size.width
    screen_h = AppKit.NSScreen.mainScreen().frame().size.height
    scale_x = sw / screen_w if screen_w else 1.0
    scale_y = sh / screen_h if screen_h else 1.0

    x = int(bounds["X"] * scale_x)
    y = int(bounds["Y"] * scale_y)
    w = int(bounds["Width"] * scale_x)
    h = int(bounds["Height"] * scale_y)

    img = Quartz.CGImageCreateWithImageInRect(full, Quartz.CGRectMake(x, y, w, h))
    if not img:
        return None, 0, 0
    return img, Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)


# --------------------------------------------------------------------------- #
# OCR                                                                           #
# --------------------------------------------------------------------------- #

def _ocr(img, width: int, height: int) -> list[dict]:
    observations = []

    def handler(req, err):
        for obs in (req.results() or []):
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            text = _normalize(str(cands[0].string()))
            bb = obs.boundingBox()
            x = int(bb.origin.x * width)
            y = int((1 - bb.origin.y - bb.size.height) * height)
            w = int(bb.size.width * width)
            h = int(bb.size.height * height)
            observations.append({"text": text, "x": x, "y": y, "w": w, "h": h})

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    req.setRecognitionLanguages_(["zh-Hant", "zh-Hans", "ja", "en"])

    Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, {}).performRequests_error_([req], None)
    return observations


# --------------------------------------------------------------------------- #
# Parse                                                                         #
# --------------------------------------------------------------------------- #

def _parse_messages(observations: list[dict], width: int) -> list[dict]:
    # Sidebar occupies the left ~30% of the window; message panel is the rest.
    left_boundary = int(width * 0.30)

    right = sorted(
        [o for o in observations if o["x"] + o["w"] // 2 > left_boundary],
        key=lambda o: o["y"],
    )
    texts = [o["text"] for o in right if o["text"]]

    # Find the unread marker — only process genuinely new messages.
    try:
        start = next(i for i, t in enumerate(texts) if _UNREAD_MARKER in t) + 1
    except StopIteration:
        # No unread marker: WAL fired for some other reason (sent msg, sync).
        # Take last 10 lines to catch sent messages without reprocessing history.
        start = max(0, len(texts) - 10)

    # Stop at compose box
    try:
        end = next(
            i for i, t in enumerate(texts)
            if i > start and any(hint in t for hint in _COMPOSE_HINTS)
        )
    except StopIteration:
        end = len(texts)

    segment = [t for t in texts[start:end] if not _TS_PATTERN.match(t)]

    messages = []
    i = 0
    while i < len(segment):
        line = segment[i]
        next_line = segment[i + 1] if i + 1 < len(segment) else None

        is_sender = (
            next_line is not None
            and len(line) <= 20
            and not _NOT_SENDER.search(line)
        )
        if is_sender:
            messages.append({"sender": line, "text": next_line})
            i += 2
        else:
            messages.append({"sender": "", "text": line})
            i += 1

    return messages


# --------------------------------------------------------------------------- #
# Public API                                                                    #
# --------------------------------------------------------------------------- #

def get_messages_current_chat() -> tuple[str | None, list[dict]]:
    chat_name = get_current_chat_name()
    img, width, height = _capture_line()
    if not img:
        log.warning("Could not capture LINE window")
        return chat_name, []

    observations = _ocr(img, width, height)
    messages = _parse_messages(observations, width)
    log.debug("OCR: %d segments → %d parsed for '%s'", len(observations), len(messages), chat_name)
    return chat_name, messages


def normalize_for_dedup(text: str) -> str:
    """Expose normalization so state.py uses the same form for hashing."""
    return _normalize(text)

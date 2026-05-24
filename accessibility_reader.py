"""
Read LINE messages via macOS Vision OCR on a screenshot of LINE's window.

Requires Screen Recording permission granted to Terminal in System Settings.
"""

import logging
import re
import time

import AppKit
import ApplicationServices as AX
import Quartz
import Vision

log = logging.getLogger(__name__)

# Timestamps LINE uses as separators — filter these out of message text
_TS_PATTERN = re.compile(
    r'^(上午|下午)\s*\d{1,2}:\d{2}$'
    r'|^\d{1,2}/\d{1,2}$'
    r'|^\d+月\d+日'
    r'|^(今天|昨天|星期[一二三四五六日])$'
    r'|^已讀\s*\d*$'
)

_UNREAD_MARKER = "以下為尚未閱讀的訊息"
_COMPOSE_HINT  = "輸入訊息"


# --------------------------------------------------------------------------- #
# Current chat name via LINE's Window menu                                     #
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
            title = attr(mbi, "AXTitle") or ""
            if title in ("視窗", "Window"):
                menu = (attr(mbi, "AXChildren") or [None])[0]
                items = attr(menu, "AXChildren") or []
                if items:
                    name = attr(items[0], "AXTitle")
                    if name:
                        return str(name).strip()
    return None


# --------------------------------------------------------------------------- #
# LINE's main window                                                             #
# --------------------------------------------------------------------------- #

def _line_window_id() -> int | None:
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    best_id, best_area = None, 0
    for w in wins:
        if str(w.get("kCGWindowOwnerName", "")) == "LINE" and w.get("kCGWindowLayer", 99) == 0:
            b = w.get("kCGWindowBounds", {})
            area = b.get("Width", 0) * b.get("Height", 0)
            if area > best_area:
                best_area = area
                best_id = w.get("kCGWindowNumber")
    return best_id


# --------------------------------------------------------------------------- #
# Capture + OCR                                                                 #
# --------------------------------------------------------------------------- #

def _capture_line() -> tuple:
    """Returns (CGImage, width, height) or (None, 0, 0)."""
    win_id = _line_window_id()
    if not win_id:
        return None, 0, 0
    img = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        win_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if not img:
        return None, 0, 0
    return img, Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)


def _ocr(img, width: int, height: int) -> list[dict]:
    """Run Vision OCR; return list of {text, x, y, w, h} in pixel coords."""
    observations = []

    def handler(req, err):
        for obs in (req.results() or []):
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            text = str(cands[0].string())
            # Vision bounding box: normalised, origin at BOTTOM-LEFT
            bb = obs.boundingBox()
            x = int(bb.origin.x * width)
            y = int((1 - bb.origin.y - bb.size.height) * height)  # flip to top-left
            w = int(bb.size.width * width)
            h = int(bb.size.height * height)
            observations.append({"text": text, "x": x, "y": y, "w": w, "h": h})

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    req.setRecognitionLanguages_(["zh-Hant", "zh-Hans", "ja", "en"])

    handler_obj = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, {})
    handler_obj.performRequests_error_([req], None)
    return observations


# --------------------------------------------------------------------------- #
# Parse messages from OCR output                                                #
# --------------------------------------------------------------------------- #

def _parse_messages(observations: list[dict], width: int) -> list[dict]:
    """
    Filter to the right-panel (message area) and extract {sender, text} pairs.

    LINE's layout: left sidebar ≈ 28% of window width, right panel = rest.
    The right panel contains sender names followed by message bubbles.

    Strategy:
      - Filter observations to x > left_boundary
      - Sort by y (top to bottom)
      - Find the unread marker; take everything after it up to compose hint
      - Group consecutive non-timestamp lines; first short line = sender, rest = text
    """
    # Sidebar is roughly the left 30% of the window
    left_boundary = int(width * 0.30)

    # Keep only right-panel text, sorted top-to-bottom
    right = sorted(
        [o for o in observations if o["x"] + o["w"] // 2 > left_boundary],
        key=lambda o: o["y"],
    )

    texts = [o["text"].strip() for o in right if o["text"].strip()]

    # Find unread marker to focus on new messages only
    try:
        start = next(i for i, t in enumerate(texts) if _UNREAD_MARKER in t) + 1
    except StopIteration:
        # No unread marker — take last 20 lines (recent messages)
        start = max(0, len(texts) - 20)

    # Stop at compose hint
    try:
        end = next(i for i, t in enumerate(texts) if _COMPOSE_HINT in t and i > start)
    except StopIteration:
        end = len(texts)

    segment = [t for t in texts[start:end] if not _TS_PATTERN.match(t)]

    # Pair sender lines with message lines.
    # Heuristic: a line is a sender if the NEXT line exists and is a short reply
    # or if this line looks like a name (no @, short, no punctuation-heavy start).
    # We use x-position stored in observations to tell sender (left-aligned name)
    # from message bubble text — but since we only have sorted text here, we fall
    # back to: group consecutive lines, treat first as sender when followed by text.
    messages = []
    i = 0
    while i < len(segment):
        line = segment[i]
        if i + 1 < len(segment):
            next_line = segment[i + 1]
            # Treat current as sender if it looks like a name (≤20 chars, no leading @)
            looks_like_sender = len(line) <= 20 and not line.startswith("@") and not line.startswith("http")
            if looks_like_sender:
                messages.append({"sender": line, "text": next_line})
                i += 2
                continue
        messages.append({"sender": "", "text": line})
        i += 1

    return messages


# --------------------------------------------------------------------------- #
# Public API                                                                    #
# --------------------------------------------------------------------------- #

def get_messages_current_chat() -> tuple[str | None, list[dict]]:
    """
    Screenshot LINE, OCR it, return (chat_name, [{'sender': ..., 'text': ...}]).
    """
    chat_name = get_current_chat_name()

    img, width, height = _capture_line()
    if not img:
        log.warning("Could not capture LINE window")
        return chat_name, []

    observations = _ocr(img, width, height)
    messages = _parse_messages(observations, width)

    log.debug("OCR: %d segments → %d messages for chat '%s'", len(observations), len(messages), chat_name)
    return chat_name, messages


def navigate_to_chat(chat_name: str):
    """Click the sidebar row for chat_name using CGEvent mouse click by position."""
    win_id = _line_window_id()
    if not win_id:
        return

    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, win_id
    )
    if not wins:
        return
    bounds = wins[0].get("kCGWindowBounds", {})
    win_x = bounds.get("X", 0)
    win_y = bounds.get("Y", 0)
    win_w = bounds.get("Width", 400)
    win_h = bounds.get("Height", 600)

    img, width, height = _capture_line()
    if not img:
        return

    observations = _ocr(img, width, height)
    left_boundary = int(width * 0.30)

    # Find the sidebar row for this chat (left panel, contains chat_name)
    sidebar = [o for o in observations if o["x"] + o["w"] // 2 < left_boundary]
    for obs in sidebar:
        if chat_name in obs["text"]:
            # Click centre of this text in screen coordinates
            cx = win_x + obs["x"] + obs["w"] // 2
            cy = win_y + obs["y"] + obs["h"] // 2
            src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
            pt = Quartz.CGPoint(cx, cy)
            dn = Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseDown, pt, 0)
            up = Quartz.CGEventCreateMouseEvent(src, Quartz.kCGEventLeftMouseUp, pt, 0)
            Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, dn)
            Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, up)
            time.sleep(0.4)
            return

    log.warning("Could not find '%s' in LINE sidebar via OCR", chat_name)

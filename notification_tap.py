"""
Listen for LINE's macOS notifications via UNUserNotificationCenter.

Each notification yields a dict: {chat_name, sender, preview}.
The callback is called on the main thread via NSNotificationCenter.

LINE notification structure (observed):
  title   → chat/group name  (or sender name for 1:1 DMs)
  subtitle→ sender name      (only in group chats)
  body    → message preview
"""

import logging
import threading

import Foundation
import objc
from Foundation import NSObject, NSNotificationCenter

log = logging.getLogger(__name__)

# LINE posts local notifications via NSUserNotificationCenter (legacy) or
# UNUserNotificationCenter. We observe the distributed notification that the
# Notification Center daemon re-broadcasts.
_LEGACY_NOTE = "com.apple.NotificationCenter.didDeliverNotification"


class _Observer(NSObject):
    def init(self):
        self = objc.super(_Observer, self).init()
        if self is None:
            return None
        self._callback = None
        return self

    def setCallback_(self, cb):
        self._callback = cb

    def handleNotification_(self, note):
        info = note.userInfo() or {}
        # Extract fields best-effort; structure varies by macOS version
        request = info.get("request") or info
        content = (request.get("content") if isinstance(request, dict) else None) or info

        app_id = str(info.get("app-id", "") or info.get("bundleIdentifier", ""))
        if "naver.line" not in app_id and "LINE" not in app_id:
            return

        title    = str(content.get("title", "")    or info.get("titl", "") or "")
        subtitle = str(content.get("subtitle", "") or info.get("subt", "") or "")
        body     = str(content.get("body", "")     or info.get("body", "") or "")

        if not body:
            return

        # 1:1 DM: title = sender name, subtitle = empty, body = text
        # Group:  title = group name, subtitle = sender, body = text
        if subtitle:
            chat_name = title
            sender = subtitle
        else:
            chat_name = title
            sender = title

        if self._callback:
            self._callback({"chat_name": chat_name, "sender": sender, "preview": body})


_observer_instance = None


def start(callback):
    """
    Register for LINE notifications. `callback(event: dict)` is called for
    each notification. Runs until the process exits.
    Must be called from the main thread (AppKit requirement).
    """
    global _observer_instance
    _observer_instance = _Observer.alloc().init()
    _observer_instance.setCallback_(callback)

    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        _observer_instance,
        "handleNotification:",
        _LEGACY_NOTE,
        None,
    )
    log.info("Notification tap registered for LINE")

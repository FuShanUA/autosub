"""macOS application icon helper.

Uses PyObjC to set the NSApplication icon and process name directly,
which reliably updates both the Dock and the Cmd+Tab app switcher.
Falls back to Tk's ``wm iconphoto -default`` when PyObjC is unavailable.
"""
import os
import sys


def set_macos_app_icon(root, icon_path):
    """Set the application icon for the current process on macOS.

    Call this as early as possible after ``tk.Tk()`` -- ideally before any
    widgets are created -- so the Cmd+Tab switcher picks up the new icon
    instead of caching the default Python rocket.
    """
    if sys.platform != "darwin":
        return

    if not os.path.exists(icon_path):
        return

    # --- Primary: PyObjC (NSApplication icon + process name) ---
    try:
        from AppKit import NSImage, NSData, NSApplication

        ns_app = NSApplication.sharedApplication()

        # Set process name so Cmd+Tab shows "AutoSub" instead of "Python"
        try:
            from Foundation import NSProcessInfo
            NSProcessInfo.processInfo().setProcessName_("AutoSub")
        except Exception:
            pass

        # Load icon from file and set as application icon
        data = NSData.dataWithContentsOfFile_(icon_path)
        if data:
            image = NSImage.alloc().initWithData_(data)
            if image:
                ns_app.setApplicationIconImage_(image)
    except ImportError:
        pass

    # --- Fallback: Tk wm iconphoto -default ---
    try:
        import tkinter as tk
        img = tk.PhotoImage(file=icon_path)
        root.tk.call('wm', 'iconphoto', root._w, '-default', img)
        # Keep a reference to prevent garbage collection
        if not hasattr(root, '_mac_icon_imgs'):
            root._mac_icon_imgs = []
        root._mac_icon_imgs.append(img)
    except Exception:
        pass

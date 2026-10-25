"""Native macOS Chrome adapter. No WebDriver, CDP, Playwright or page scripting.

PyAutoGUI sends OS input events; AppleScript reads foreground window bounds only.
Imports are lazy so the backend and unit tests need no graphical desktop.
"""
from __future__ import annotations

import base64
import ctypes
import time
import io
import json
import subprocess
import sys
from urllib.parse import urlparse


class DesktopError(RuntimeError):
    pass


def maps_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname in {'www.google.com', 'google.com'}
            and parsed.path.startswith('/maps') and not parsed.username and not parsed.password)


class MacChrome:
    def __init__(self):
        if sys.platform != 'darwin':
            raise DesktopError('The native capture adapter currently requires macOS with an unlocked graphical desktop.')
        try:
            import pyautogui
            import pyperclip
        except ImportError:
            raise DesktopError('Install desktop dependencies: cd backend && uv sync --extra capture') from None
        self.gui, self.clipboard = pyautogui, pyperclip
        self.gui.FAILSAFE = True
        self.gui.PAUSE = .2
        self.bounds = None

    def permissions(self):
        import Quartz
        accessibility = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        accessibility.AXIsProcessTrusted.restype = ctypes.c_bool
        accessibility.AXIsProcessTrusted.argtypes = []
        return {'accessibility': bool(accessibility.AXIsProcessTrusted()),
                'screen_recording': bool(Quartz.CGPreflightScreenCaptureAccess())}

    def script(self, code):
        try:
            return subprocess.run(['osascript', '-e', code], capture_output=True, text=True, timeout=8, check=True).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            raise DesktopError('Cannot inspect Chrome. Grant Accessibility/Automation permissions to the terminal running this worker.') from None

    def geometry(self):
        front = self.script('tell application "System Events" to get name of first application process whose frontmost is true')
        if front != 'Google Chrome':
            raise DesktopError('Chrome lost foreground focus; desktop input stopped.')
        try:
            bounds = tuple(int(v.strip()) for v in self.script('tell application "Google Chrome" to get bounds of front window').split(','))
            x, y, right, bottom = bounds
        except (ValueError, TypeError):
            raise DesktopError('Cannot read the Chrome window bounds') from None
        w, h = self.gui.size()
        if x < 0 or y < 0 or right > w or bottom > h or right-x < 800 or bottom-y < 600:
            raise DesktopError('Place a Chrome window at least 800×600 entirely on the primary display.')
        return bounds

    def start(self):
        self.script('tell application "Google Chrome" to activate')
        self.bounds = self.geometry()
        self.gui.hotkey('command', 't')

    def check(self):
        bounds = self.geometry()
        if self.bounds is not None and bounds != self.bounds:
            raise DesktopError('Chrome window moved or resized; desktop input stopped.')
        return bounds

    def open(self, url):
        if not maps_url(url):
            raise DesktopError('Shot URL is not a supported Google Maps HTTPS URL')
        self.check()
        self.clipboard.copy(url)
        self.gui.hotkey('command', 'l')
        self.gui.hotkey('command', 'v')
        self.gui.press('enter')

    def url(self):
        self.check()
        self.clipboard.copy('')
        self.gui.hotkey('command', 'l')
        self.gui.hotkey('command', 'c')
        value = self.clipboard.paste().strip()
        for _ in range(10):
            if value:
                break
            time.sleep(.1)
            value = self.clipboard.paste().strip()
        self.gui.press('escape')
        self.check()
        if not maps_url(value):
            parsed = urlparse(value)
            if not value or parsed.scheme not in ('http', 'https', 'chrome'):
                raise DesktopError('Could not copy the Chrome address. Enable Accessibility for Terminal in macOS Privacy & Security, then reopen Terminal and retry.')
            raise DesktopError(f'Chrome is showing {parsed.scheme}://{parsed.hostname or 'unknown'} instead of Google Maps. Finish Chrome setup and open a Maps tab before retrying.')
        return value

    def observe(self):
        url = self.url()
        x, y, right, bottom = self.check()
        screenshot = self.gui.screenshot()
        w, h = self.gui.size()
        sx, sy = screenshot.width/w, screenshot.height/h
        image = screenshot.crop((round(x*sx), round(y*sy), round(right*sx), round(bottom*sy))).convert('RGB')
        # Downscale only the model observation/saved capture, never the coordinate geometry.
        image.thumbnail((1920, 1440))
        if image.width < 640 or image.height < 400 or image.getextrema() == ((0, 0), (0, 0), (0, 0)):
            raise DesktopError('Screen capture is blank or too small; check Screen Recording permission.')
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=90)
        return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode(), url

    def drag(self, decision, expected_url):
        if self.url() != expected_url:
            raise DesktopError('Active camera/tab changed during assessment; drag cancelled.')
        x, y, right, bottom = self.check()
        self.gui.moveTo(x + decision.start_x*(right-x), y + decision.start_y*(bottom-y), duration=.2)
        self.gui.dragTo(x + decision.end_x*(right-x), y + decision.end_y*(bottom-y), duration=.6, button='left')

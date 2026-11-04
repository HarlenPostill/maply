"""Native macOS and Linux X11 Chrome adapters. No browser/page scripting.

PyAutoGUI sends OS input events; AppleScript reads foreground window bounds only.
Imports are lazy so the backend and unit tests need no graphical desktop.
"""
from __future__ import annotations

import base64
import ctypes
import time
import io
import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlparse


class DesktopError(RuntimeError):
    pass


class BlackLoadingFrame(DesktopError):
    def __init__(self, page_url):
        self.page_url = page_url
        super().__init__('Street View is still showing a black loading frame.')


class TransientNavigationError(DesktopError):
    pass


def maps_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname in {'www.google.com', 'google.com'}
            and parsed.path.startswith('/maps') and not parsed.username and not parsed.password)


class MacChrome:
    modifier = 'command'
    copy_attempts = 1
    address_focus_delay = 0
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
        self.activate()
        self.bounds = self.geometry()
        self.gui.hotkey(self.modifier, 't')

    def activate(self):
        self.script('tell application "Google Chrome" to activate')

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
        self.gui.hotkey(self.modifier, 'l')
        if self.address_focus_delay:
            time.sleep(self.address_focus_delay)
        self.gui.hotkey(self.modifier, 'v')
        self.gui.press('enter')

    def url(self):
        value = ''
        for attempt in range(self.copy_attempts):
            self.check()
            self.clipboard.copy('')
            self.focus_address()
            self.gui.hotkey(self.modifier, 'c')
            value = self.clipboard.paste().strip()
            for _ in range(10):
                if value:
                    break
                time.sleep(.1)
                value = self.clipboard.paste().strip()
            self.leave_address()
            self.check()
            # Retry the read-only shortcut if the clipboard update was missed.
            # A real non-Maps browser URL must still fail closed immediately.
            if value and urlparse(value).scheme in ('http', 'https', 'chrome'):
                break
        if not maps_url(value):
            parsed = urlparse(value)
            if not value or parsed.scheme not in ('http', 'https', 'chrome'):
                raise DesktopError(self.address_copy_error())
            if parsed.hostname in {'google.com', 'www.google.com'} and parsed.scheme == 'http' and parsed.path.startswith('/maps'):
                raise TransientNavigationError('Chrome returned an insecure Maps address; reopening the planned HTTPS shot.')
            if (parsed.scheme in ('http', 'https') and parsed.hostname in {'google.com', 'www.google.com'}
                    and parsed.path in ('', '/') and not parsed.query and not parsed.fragment):
                raise TransientNavigationError(f'Chrome is showing {parsed.scheme}://{parsed.hostname} instead of Street View; retrying the planned shot.')
            raise DesktopError(f'Chrome is showing {parsed.scheme}://{parsed.hostname or 'unknown'} instead of Google Maps. Finish Chrome setup and open a Maps tab before retrying.')
        return value

    def focus_address(self):
        self.gui.hotkey(self.modifier, 'l')
        if self.address_focus_delay:
            time.sleep(self.address_focus_delay)

    def leave_address(self):
        self.gui.press('escape')
        self.gui.press('escape')

    def address_copy_error(self):
        return 'Could not copy the Chrome address. Enable Accessibility for Terminal in macOS Privacy & Security, then reopen Terminal and retry.'

    def observe(self):
        url = self.url()
        x, y, right, bottom = self.check()
        screenshot = self.screenshot()
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

    def screenshot(self):
        return self.gui.screenshot()

    def drag(self, decision, expected_url):
        if self.url() != expected_url:
            raise DesktopError('Active camera/tab changed during assessment; drag cancelled.')
        x, y, right, bottom = self.check()
        self.gui.moveTo(x + decision.start_x*(right-x), y + decision.start_y*(bottom-y), duration=.2)
        self.gui.dragTo(x + decision.end_x*(right-x), y + decision.end_y*(bottom-y), duration=.6, button='left')


class LinuxChrome(MacChrome):
    """Chrome on a dedicated X11 desktop, including an Xvfb cloud display.

    Window identity/focus come from the window manager, never page scripting.
    The same screenshot, URL validation and guarded mouse input apply on both OSes.
    """
    modifier = 'ctrl'
    copy_attempts = 3
    address_focus_delay = .3

    def focus_address(self):
        x, y, right, _ = self.check()
        # The dedicated desktop uses Chrome's standard unscaled toolbar.
        # Maps can retain keyboard focus through Ctrl+L; focus the omnibox itself.
        self.gui.click(x + (right - x) // 2, y + 62)
        self.gui.hotkey('ctrl', 'a')
        time.sleep(self.address_focus_delay)

    def leave_address(self):
        x, y, right, _ = self.check()
        # Escape can reach Maps and close the panorama after the omnibox blurs.
        self.gui.click(right - 200, y + 20)

    def start(self):
        # This desktop is dedicated to Maply. Reuse its selected tab so each
        # resume does not leave another expensive Street View renderer running.
        self.activate()
        self.bounds = self.geometry()

    def open(self, url):
        if not maps_url(url):
            raise DesktopError('Shot URL is not a supported Google Maps HTTPS URL')
        for _ in range(3):
            self.check()
            self.focus_address()
            self.gui.press('backspace')
            self.clipboard.copy(url)
            self.gui.hotkey('ctrl', 'v')
            self.gui.hotkey('ctrl', 'a')
            self.clipboard.copy('')
            self.gui.hotkey('ctrl', 'c')
            value = self.clipboard.paste().strip()
            for _ in range(10):
                if value:
                    break
                time.sleep(.1)
                value = self.clipboard.paste().strip()
            self.check()
            if value == url:
                self.gui.press('enter')
                return
        raise TransientNavigationError('Chrome did not replace the address with the planned shot; retrying navigation.')

    def address_copy_error(self):
        return 'The cloud Chrome clipboard did not return an address after three attempts. Inspect the remote browser, then resume the run in Maply.'

    def url(self):
        for attempt in range(3):
            value = super().url()
            parsed = urlparse(value)
            requested = (parsed.path.rstrip('/') == '/maps/@'
                         and parse_qs(parsed.query).get('map_action') == ['pano'])
            if not requested:
                return value
            # Chrome can retain the typed Maps API URL in its omnibox even
            # after the panorama is visible. The first copy/escape resets that
            # edit state; reread the actual camera instead of submitting the plan.
            if attempt < 2:
                time.sleep(.3)
        raise TransientNavigationError('Chrome has not resolved the requested address to an observed camera URL; retrying the planned shot.')

    @staticmethod
    def loading_frame(image):
        width, height = image.size
        centre = image.crop((width*.25, height*.25, width*.85, height*.8)).convert('L').resize((48, 32))
        pixels = centre.tobytes()
        return sum(value < 16 for value in pixels) / len(pixels) > .95

    def observe(self):
        from PIL import Image
        for attempt in range(6):
            image, url = super().observe()
            with Image.open(io.BytesIO(base64.b64decode(image.split(',', 1)[1]))) as frame:
                if not self.loading_frame(frame):
                    return image, url
            if attempt < 5:
                time.sleep(2)
        raise BlackLoadingFrame(url)

    def __init__(self):
        if sys.platform != 'linux' or not os.environ.get('DISPLAY'):
            raise DesktopError('Linux capture requires an X11 DISPLAY with Chrome running.')
        try:
            import pyautogui
            import pyperclip
        except ImportError:
            raise DesktopError('Install desktop dependencies: pip install -e ".[capture]"') from None
        self.gui, self.clipboard = pyautogui, pyperclip
        self.gui.FAILSAFE = True
        self.gui.PAUSE = .2
        self.bounds = None
        self.window_id = None

    def xdo(self, *args):
        try:
            return subprocess.run(['xdotool', *args], capture_output=True, text=True,
                                  timeout=8, check=True).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            raise DesktopError('Cannot inspect the X11 Chrome desktop; check DISPLAY, openbox and xdotool.') from None

    def permissions(self):
        return {'x11_display': bool(os.environ.get('DISPLAY'))}

    def screenshot(self):
        # Grab X11 directly; PyScreeze assumes a full desktop's session variables
        # and otherwise rejects an Xvfb display even when scrot is installed.
        from PIL import ImageGrab
        return ImageGrab.grab(xdisplay=os.environ['DISPLAY'])

    def chrome_window(self, window):
        try:
            result = subprocess.run(['xprop', '-id', window, 'WM_CLASS'], capture_output=True,
                                    text=True, timeout=8, check=True).stdout
        except (subprocess.SubprocessError, OSError):
            raise DesktopError('Cannot read the browser window identity; install x11-utils.') from None
        classes = [part.lower() for part in result.split('"')[1::2]]
        return any(value in {'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'} for value in classes)

    def activate(self):
        windows = self.xdo('search', '--onlyvisible', '--class', 'Google-chrome|Chromium').splitlines()
        candidates = [window for window in windows if self.chrome_window(window)]
        if not candidates:
            raise DesktopError('Open Chrome on the dedicated X11 desktop before starting capture.')
        self.xdo('windowactivate', '--sync', candidates[-1])

    def geometry(self):
        window = self.xdo('getactivewindow')
        if not self.chrome_window(window):
            raise DesktopError('Chrome lost foreground focus; desktop input stopped.')
        if self.window_id is not None and self.window_id != window:
            raise DesktopError('The active Chrome window changed; desktop input stopped.')
        try:
            values = dict(line.split('=', 1) for line in self.xdo('getwindowgeometry', '--shell', window).splitlines() if '=' in line)
            x, y, width, height = (int(values[key]) for key in ('X', 'Y', 'WIDTH', 'HEIGHT'))
        except (ValueError, KeyError):
            raise DesktopError('Cannot read Chrome window bounds.') from None
        screen_width, screen_height = self.gui.size()
        if x < 0 or y < 0 or width < 800 or height < 600 or x + width > screen_width or y + height > screen_height:
            raise DesktopError('Place Chrome entirely on the primary display at a size of at least 800×600.')
        self.window_id = window
        return x, y, x + width, y + height

"""Linux adapter safety checks; no database, browser or model calls."""
from types import SimpleNamespace

import pytest

from app.capture_desktop import DesktopError, LinuxChrome, MacChrome


def desktop():
    obj = object.__new__(LinuxChrome)
    obj.window_id = None
    obj.bounds = None
    obj.gui = SimpleNamespace(size=lambda: (1600, 1200), click=lambda *args: None)
    obj.chrome_window = lambda window: window in {'42', '43'}
    obj.xdo = lambda *args: '42' if args[0] == 'getactivewindow' else 'X=0\nY=0\nWIDTH=1500\nHEIGHT=1100'
    return obj


def test_linux_refuses_focus_changes_even_between_chrome_windows():
    obj = desktop()
    assert obj.geometry() == (0, 0, 1500, 1100)
    obj.xdo = lambda *args: '43'
    with pytest.raises(DesktopError, match='window changed'):
        obj.geometry()
    obj.xdo = lambda *args: '99'
    with pytest.raises(DesktopError, match='lost foreground'):
        obj.geometry()


@pytest.mark.parametrize('geometry', ['X=-1\nY=0\nWIDTH=1500\nHEIGHT=1100',
                                     'X=0\nY=0\nWIDTH=1700\nHEIGHT=1100',
                                     'X=0\nY=0\nWIDTH=700\nHEIGHT=500'])
def test_linux_refuses_offscreen_or_small_browser(geometry):
    obj = desktop()
    obj.xdo = lambda *args: '42' if args[0] == 'getactivewindow' else geometry
    with pytest.raises(DesktopError, match='entirely'):
        obj.geometry()


def test_linux_rejects_unsupported_urls_before_input():
    obj = desktop()
    obj.gui.hotkey = lambda *_: pytest.fail('Unsupported URL must not send input')
    with pytest.raises(DesktopError, match='supported Google Maps'):
        obj.open('https://example.com/')


def test_linux_retries_missed_address_shortcut_and_does_not_leak_clipboard(monkeypatch):
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    obj = desktop()
    keys = []
    obj.gui.hotkey = lambda *args: keys.append(args)
    obj.gui.press = lambda _: None
    url = 'https://www.google.com/maps/@-33.8,151.2,3a'
    values = iter([''] * 11 + [url])
    obj.clipboard = SimpleNamespace(copy=lambda _: None, paste=lambda: next(values))
    assert obj.url() == url
    assert keys.count(('ctrl', 'c')) == 2
    obj.clipboard.paste = lambda: 'PRIVATE CLIPBOARD CONTENT'
    with pytest.raises(DesktopError) as error:
        obj.url()
    assert 'three attempts' in str(error.value)
    assert 'PRIVATE' not in str(error.value) and 'macOS' not in str(error.value)


def test_linux_never_retries_a_focus_loss_or_real_non_maps_url(monkeypatch):
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    obj = desktop()
    keys = []
    obj.gui.hotkey = lambda *args: keys.append(args)
    obj.gui.press = lambda _: None
    obj.clipboard = SimpleNamespace(copy=lambda _: None, paste=lambda: 'https://example.com/private')
    with pytest.raises(DesktopError, match='instead of Google Maps'):
        obj.url()
    assert keys.count(('ctrl', 'c')) == 1
    keys.clear()
    def lost_focus():
        raise DesktopError('Chrome lost foreground focus')
    obj.check = lost_focus
    with pytest.raises(DesktopError, match='lost foreground'):
        obj.url()
    assert not keys


def test_linux_resume_reuses_the_existing_browser_tab():
    obj = desktop()
    obj.activate = lambda: None
    obj.gui.hotkey = lambda *_: (_ for _ in ()).throw(AssertionError('Do not open more tabs'))
    obj.start()
    assert obj.bounds == (0, 0, 1500, 1100)


def test_google_home_redirect_is_transient_but_other_pages_remain_fatal(monkeypatch):
    from app import capture_desktop
    obj = desktop()
    obj.gui.hotkey = lambda *args: None
    obj.gui.press = lambda _: None
    obj.clipboard = SimpleNamespace(copy=lambda _: None, paste=lambda: 'http://google.com')
    with pytest.raises(capture_desktop.TransientNavigationError, match='retrying the planned shot'):
        obj.url()
    obj.clipboard.paste = lambda: 'https://example.com/private'
    with pytest.raises(DesktopError, match='instead of Google Maps') as error:
        obj.url()
    assert not isinstance(error.value, capture_desktop.TransientNavigationError)


def test_linux_rereads_the_camera_instead_of_returning_the_typed_plan(monkeypatch):
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    planned = 'https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=-33.8,151.2&heading=90'
    observed = 'https://www.google.com/maps/@-33.8,151.2,3a,80y,91h,82t/data=!3m3!1e1!1sPANO!2e0'
    values = iter([planned, observed])
    monkeypatch.setattr(MacChrome, 'url', lambda _: next(values))
    assert desktop().url() == observed
    monkeypatch.setattr(MacChrome, 'url', lambda _: planned)
    with pytest.raises(capture_desktop.TransientNavigationError, match='retrying the planned shot'):
        desktop().url()


def test_linux_waits_for_black_loading_frame_before_returning_observation(monkeypatch):
    import base64
    import io
    from PIL import Image, ImageDraw
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    def encoded(colour):
        frame = Image.new('RGB', (1000, 800), colour)
        ImageDraw.Draw(frame).rectangle((0, 0, 1000, 100), fill='white')
        output = io.BytesIO(); frame.save(output, format='JPEG')
        return 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode()
    black, ready = encoded('black'), encoded('green')
    observations = iter([(black, 'loading-url'), (ready, 'ready-url')])
    monkeypatch.setattr(MacChrome, 'observe', lambda _: next(observations))
    assert desktop().observe() == (ready, 'ready-url')
    monkeypatch.setattr(MacChrome, 'observe', lambda _: (black, 'loading-url'))
    with pytest.raises(capture_desktop.BlackLoadingFrame, match='black loading frame') as error:
        desktop().observe()
    assert error.value.page_url == 'loading-url'


@pytest.mark.parametrize('broken_paste', [False, True])
def test_linux_replaces_stale_address_and_verifies_before_navigation(monkeypatch, broken_paste):
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    obj = desktop()
    state = {'text': 'http://google.com/maps/old', 'selected': False, 'clipboard': ''}
    navigated = []
    def hotkey(*keys):
        if keys == ('ctrl', 'l'):
            state['selected'] = False  # Reproduce Chrome retaining an insertion point.
        elif keys == ('ctrl', 'a'):
            state['selected'] = True
        elif keys == ('ctrl', 'v'):
            state['text'] = ('' if state['selected'] else state['text']) + ('wrong' if broken_paste else state['clipboard'])
            state['selected'] = False
        elif keys == ('ctrl', 'c') and state['selected']:
            state['clipboard'] = state['text']
    def press(key):
        if key == 'backspace' and state['selected']:
            state['text'] = ''
        elif key == 'enter':
            navigated.append(state['text'])
    obj.gui.hotkey, obj.gui.press = hotkey, press
    obj.clipboard = SimpleNamespace(copy=lambda value: state.update(clipboard=value), paste=lambda: state['clipboard'])
    url = 'https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=-33.8,151.2'
    if broken_paste:
        with pytest.raises(capture_desktop.TransientNavigationError):
            obj.open(url)
        assert navigated == []
    else:
        obj.open(url)
        assert navigated == [url]


def test_linux_focuses_the_address_when_maps_ignores_the_shortcut(monkeypatch):
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    obj = desktop()
    state = {'focused': False, 'clipboard': ''}
    url = 'https://www.google.com/maps/@-33.8,151.2,3a'
    obj.gui.click = lambda *args: state.update(focused=True)
    def hotkey(*keys):
        if keys == ('ctrl', 'c') and state['focused']:
            state['clipboard'] = url
    obj.gui.hotkey = hotkey
    obj.gui.press = lambda _: None
    obj.clipboard = SimpleNamespace(copy=lambda value: state.update(clipboard=value), paste=lambda: state['clipboard'])
    assert obj.url() == url


def test_linux_reading_the_camera_does_not_send_escape_into_maps(monkeypatch):
    from app import capture_desktop
    monkeypatch.setattr(capture_desktop.time, 'sleep', lambda _: None)
    obj = desktop()
    state = {'address': False, 'pano': True}
    def click(x, y):
        state['address'] = y > 40
    def press(key):
        if key == 'escape':
            if state['address']:
                state['address'] = False
            else:
                state['pano'] = False
    obj.gui.click, obj.gui.press = click, press
    obj.gui.hotkey = lambda *args: None
    obj.clipboard = SimpleNamespace(copy=lambda _: None, paste=lambda: 'https://www.google.com/maps/@-33.8,151.2,3a')
    obj.url()
    assert state['pano'], 'Reading the camera must not exit Street View'

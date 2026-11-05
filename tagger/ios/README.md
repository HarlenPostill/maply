# Maply for iOS

SwiftUI app and WidgetKit lock screen widget for capture runs. It talks to the
local Maply API at `http://127.0.0.1:8000` by default. In the Simulator this is
the Mac's local API, with no gateway login or SSH tunnel. An explicitly configured
remote server can still use HTTP Basic Authentication.

- **App:** lists running/paused and recent runs (refreshes every 5 seconds while
  open), pauses, resumes and stops runs, and creates runs with the same model,
  goals and YOLO options as the web panel.
- **Widget:** lock screen rectangular, circular and inline widgets, plus a small
  home screen widget. Shows the running run, else a paused run, else the newest.
  WidgetKit refreshes it about every 5 minutes while a run is running (30 minutes
  otherwise) and immediately after you act in the app.

## Build and install

```bash
cd ios
xcodegen generate        # project.yml is the source of truth; Maply.xcodeproj is generated
open Maply.xcodeproj
```

Choose your iPhone and run. Signing is automatic with team `Z43R7TX7Z9`; Xcode
registers `com.harlenpostill.maply`, its widget and the app group
`group.com.harlenpostill.maply` on first device build. Open the app, enter the
server address in Settings, then add the widget from the lock screen editor.
A physical phone's loopback address refers to the phone, so it needs a separately
configured reachable server address. The local backend stays bound to the Mac's
loopback interface by default.

The password is kept in the keychain (after-first-unlock, this device only) and
shared with the widget through the app group, so the widget can refresh while the
phone is locked.

## Simulator development

Start `./scripts/dev.sh` from the repository root, then build and run the app in
the Simulator through Xcode. The default server is the Mac's local API at
`http://127.0.0.1:8000`. No password is required. Debug builds can select another
server through the `MAPLY_SERVER` scheme environment variable.

The former public VPS default is replaced with the local address when loading
old saved settings. App and widget requests now use the local database. Pausing,
resuming, or creating runs changes that local data.

# Petal Path

A cosy location-based iOS game in Metal 4. The player stands on a stylised map of the real world. First
playable draft: a joystick walks a fake location around Sydney, and a wardrobe changes the character.

Open `PetalPath.xcodeproj`, or regenerate it with `xcodegen generate` from `project.yml`.
Bundle id `com.harlenpostill.PetalPath`, team `Z43R7TX7Z9`, iOS 26 or later.

## How it fits together

| Folder | What it does |
|---|---|
| `Sources/Map/` | `MVT` reads vector tiles, `Earcut` triangulates polygons, `TileMesher` builds the stylised meshes, `TileSource` fetches and caches, `MapStreamer` loads tiles around the player. |
| `Sources/Game/` | `GameScene` runs the player and camera and builds the draw list. `LocationSource` is where the player is. |
| `Sources/Render/` | `Metal4Renderer` is the real renderer, `SimulatorRenderer` a classic Metal preview. They share `Shaders.metal`, `RenderStates` and `GameScene.mapDraws()`. |
| `Sources/Character/` | `GLB`, `Animator` and `Wardrobe`, copied from `player-character/`. |
| `Sources/App/` | SwiftUI: HUD, joystick, settings sheet. |

The character source lives in `../player-character/`. After re-exporting there, copy `export/player.glb` and any
new textures to `PetalPath/Resources/`.

## The map

Data is OpenFreeMap's public instance: OpenMapTiles-schema vector tiles of OpenStreetMap, with no API key or
account. The tile URL comes from TileJSON at runtime, so a new planet build needs no app update.

Rules kept to, from [openfreemap.org](https://openfreemap.org) and its [terms](https://openfreemap.org/tos/):

- Attribution stays on screen: `MapAttribution.text`, linked to the OpenStreetMap copyright page. The settings
  sheet carries the longer credit. The data is under the Open Database License.
- A unique `User-Agent` and at most 4 parallel requests.
- Tiles are fetched only around the player. A pre-download or "save this area" feature is capped at 3,000 tiles or
  50 MB a download, and would need a chat with OpenFreeMap beyond that.

Tiles are z14, about 2 km square in Sydney, the most detailed OpenMapTiles offers. They are cached in
`Caches/PetalPathTiles` for 30 days, and a stale tile is used if the network fails.

Things in the data that are easy to get wrong:

- `suburb` and `neighbourhood` landuse blanket whole tiles and must be skipped.
- Sydney Harbour is not class `ocean`, so every `water` polygon is treated the same.
- Buildings arrive as multipolygons, many footprints to a feature, so colour is chosen per footprint.
- A tower on a podium is overlapping `building:part` footprints. These are grouped with union-find so they read as
  one building. `TileMesher.overlaps` keeps terraces, which only share a wall, separate.
- Tiles repeat each other's edges. A footprint wholly in the buffer belongs to the neighbour and is skipped. One
  straddling the edge is drawn by both, so its look comes from `crossingKey`, which both tiles compute the same.
- Tiles overlap their neighbours by a buffer. Ground is painted layer by layer across all tiles to keep this hidden.

## Rendering

- The world is drawn around the player at real size, with the player at the origin. Each tile's corner is
  worked out in doubles, so precision holds anywhere on Earth.
- Flat ground is painted back to front with no depth test. Only buildings, trees and the character use depth.
- The ground bends away from the player in the vertex shader for a soft horizon, so ground triangles are
  subdivided to 48 m or less at build time. Ground has no depth test, so anything past the horizon is discarded
  in the fragment shaders, and the fog distance follows the horizon as the camera moves.
- Ground patterns use `PatternAnchor`, which holds the Mercator scale still. Using the player's own latitude
  makes every pattern slide under them.
- Building shadows are painted quads. A stencil test stops overlapping shadows doubling up.
- Buildings between the camera and the player dither away, as do buildings beside a low camera.
- The sky is a full-screen procedural pass drawn first. Its horizon colour is also the fog colour, set in both
  `GameScene.sky` and `skyColour`. Noise is skipped below the horizon, where the map covers it anyway.

Scale is deliberately not literal. `GameScene.playerScale` makes the character 30 m tall, like a Pokémon Go avatar,
and `TileMesher.toyHeight` squashes towers so 200 m becomes 61 m. Roads are wider than life for the same reason.
Walk speed follows from the scale: `walkSpeed` × 30 keeps the feet locked, about 9 m/s of real ground.

## Location

`LocationSource` has two implementations. `JoystickLocationSource` is the fake location, starting at Circular Quay.
`GPSLocationSource` follows the device, easing between fixes and ignoring moves within GPS error. The HUD button
switches between them and the choice is saved. The player stays put until the first fix arrives. The joystick also works
while following the device: it adds a wander on top of the real position, and "Back to me" clears it. At real walking pace the walk cycle plays at its slowest rate, so her feet outrun the ground.

## Testing

    xcodebuild test -project PetalPath.xcodeproj -scheme PetalPath -destination 'platform=iOS Simulator,name=iPhone 18 Pro'

The Simulator has no Metal 4. To run the real renderer, build the Mac Catalyst destination and launch the binary:

    MTL_DEBUG_LAYER=1 METAL_DEVICE_WRAPPER_TYPE=1 MTL_SHADER_VALIDATION=1 \
      PP_AUTOWALK=3.14 PP_FAST=1 PP_LOG_POSITION=1 \
      build/cat/Build/Products/Debug-maccatalyst/PetalPath.app/Contents/MacOS/PetalPath

Debug hooks: `PP_START=lat,lon`, `PP_AUTOWALK=<radians from screen-up>`, `PP_FAST=1`, `PP_OUTFIT=girl`, `PP_YAW=<radians>`, `PP_PITCH=<radians>`,
`PP_SETTINGS=1`, `PP_LOG_POSITION=1`. Dragging in the Catalyst window turns the camera, and that also turns an autowalk.
Memoryless textures must stay out of residency sets. Metal 4 does not retain buffers for in-flight work,
so `Metal4Renderer` holds unloaded tiles until their frames finish.

Debug builds use `-O` because tile meshing is too slow without it.

## Trip planning

"Plan a trip" searches places with MapKit (free, no key) and asks the Maply route service for a
route preview (`maply-route-preview/1`, see `../atlas/ROUTING_API.md`). Every route is an
unverified planning preview with navigation off. There is no live guidance and no route on the
game map. Code is in `Sources/Trip/` (models, search, client, wording) and `Sources/App/Trip*.swift`.

- Apple's terms (Developer Program License Agreement, Attachment 6) require search results to
  appear with an Apple map, and Apple map data not to be stored, so the sheet shows a MapKit map
  beside results and nothing is saved.
- "My location" is the raw device fix, never the eased or joystick-moved character. It needs a
  fix within 50 m and 30 s. While device location is enabled, an unavailable fix never silently
  becomes a character start. Character starts are labelled simulated.
- Where routes come from (`RouteServiceConfiguration.endpoint`):
  - Release builds use only the deployed HTTPS service and ignore every debug hook.
  - Debug builds on a phone use the deployed service, because 127.0.0.1 there is the phone itself.
  - Debug builds in the Simulator or on Mac use the local service at `http://127.0.0.1:8765`, started with
    `cd ../atlas && uv run python -m atlas serve`.
  - `PP_ROUTE_API` overrides this in Debug: a loopback `http://` URL, an `https://` URL, or `off`. Anything else is
    refused, and route planning says it isn't connected, never quietly using another service.
- The deployed URL is the `MAPLY_ROUTE_API_URL` build setting in `project.yml`, currently
  `https://atlas-api-production-662d.up.railway.app` (Railway, `maply-atlas` / `atlas-api`). It's written into
  Info.plist as `MaplyRouteAPIURL`. It must be a plain `https://host` base URL with no path, credentials or query.
  Empty means "not connected". It's a public address, not a secret: the app carries no keys or database details.
- `PetalPath/Config/Debug-Info.plist` allows loopback HTTP in Debug only. `PetalPath/Config/Info.plist` (Release)
  has no transport exception.
- Each request to the service waits up to 120 s (`MaplyRouteService.requestTimeout`), once, with no retry loop. The
  free-plan host sleeps when idle and takes about 30–90 s to wake. Planning shows its progress state the whole
  time, and changing the start or the destination cancels the request.
- A 429, or a gateway page with no API body (502, 503, 504), shows the existing "busy" or "can't reach the service"
  message with Try again. A request that times out shows "can't reach the service" with Try again.

Trip hooks in Debug, as environment variables or `PP_NAME=value` launch arguments: `PP_ROUTE_API=<url>|off`,
`PP_ROUTE_SAMPLE=1` (labelled sample layout data), `PP_TRIP=1`, `PP_DEST=lat,lon[,name]`, `PP_TRIP_PLAN=1`.

### On an iPhone

Signing is automatic with team `Z43R7TX7Z9`. Build for the connected phone, then install. The deployed URL comes
from `project.yml`; add `MAPLY_ROUTE_API_URL=https://<host>` to the build command to point one build elsewhere.

    xcodebuild build -project PetalPath.xcodeproj -scheme PetalPath -configuration Debug \
      -destination 'id=<device UDID>' -derivedDataPath build/trip-device -allowProvisioningUpdates
    xcrun devicectl device install app --device <device UDID> build/trip-device/Build/Products/Debug-iphoneos/PetalPath.app

The first launch needs the developer certificate trusted on the phone (Settings › General › VPN & Device Management).
A real iPhone runs the Metal 4 renderer, which until then had only been tested through Mac Catalyst.

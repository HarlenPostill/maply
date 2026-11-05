import CoreLocation
import simd

/// Where the player is. The game only reads this protocol, so fake and real location are interchangeable.
protocol LocationSource: AnyObject {
    var coordinate: GeoCoordinate { get }
    /// Metres per second, east and north. Drives facing and the walk cycle.
    var velocity: SIMD2<Float> { get }
    /// The stick's wish, east and north. Sources driven by the world ignore it.
    func steer(_ desired: SIMD2<Float>)
    func tick(dt: Float)
    func teleport(to c: GeoCoordinate)
}

/// One raw reading from the device's location hardware.
struct LocationFix: Equatable {
    var coordinate: GeoCoordinate
    /// Metres, as Core Location reports it.
    var horizontalAccuracy: Double
    var timestamp: Date
}

/// Fake location: the debug joystick walks a coordinate across the real map.
final class JoystickLocationSource: LocationSource {
    private(set) var coordinate: GeoCoordinate
    private(set) var velocity = SIMD2<Float>(0, 0)
    private var desired = SIMD2<Float>(0, 0)

    init(start: GeoCoordinate) { coordinate = start }

    func steer(_ d: SIMD2<Float>) { desired = d }
    func teleport(to c: GeoCoordinate) { coordinate = c; velocity = .zero }

    func tick(dt: Float) {
        velocity += (desired - velocity) * min(1, dt * 10)
        if simd_length_squared(velocity) < 0.0004 { velocity = .zero; return }
        let scale = Mercator.groundScale(latitude: coordinate.latitude)
        let step = SIMD2<Double>(Double(velocity.x), -Double(velocity.y)) * Double(dt) / scale
        coordinate = Mercator.unproject(Mercator.project(coordinate) + step)
    }
}

/// Real location. Fixes arrive every second or two, so the avatar eases towards the latest one, and
/// its speed and facing come from that easing. Small moves within GPS error are ignored, otherwise she
/// would shuffle about while you stand still.
///
/// The debug joystick adds a wander on top of the real position, so you can roam from where you are.
/// New fixes keep moving the player by the same amount until `returnToFix`.
final class GPSLocationSource: NSObject, LocationSource, CLLocationManagerDelegate {
    enum Status { case idle, locating, active, denied, failed }

    /// Further than this between fixes is a jump, not a walk.
    private static let snapDistance: Float = 150
    private static let worstAccuracy = 200.0

    private(set) var coordinate: GeoCoordinate
    private(set) var velocity = SIMD2<Float>(0, 0)
    private(set) var status = Status.idle
    private(set) var hasFix = false
    private var target: SIMD2<Double>?
    private var fixed = SIMD2<Double>(0, 0)        // eased real position, Mercator
    private var wander = SIMD2<Double>(0, 0)       // joystick offset, Mercator
    private var wish = SIMD2<Float>(0, 0), stick = SIMD2<Float>(0, 0)
    private let manager = CLLocationManager()
    /// The last raw fix from the device, exactly as reported. Never eased and never moved by the
    /// joystick wander, so trip planning can tell the real position from the character's.
    private(set) var latestFix: LocationFix?

    var isWandering: Bool { wander != .zero }

    init(start: GeoCoordinate) {
        coordinate = start
        super.init()
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.activityType = .fitness
    }

    func begin() {
        manager.delegate = self
        status = .locating
        hasFix = false; target = nil; wander = .zero
        // The delegate's authorisation callback does the rest, including on first launch.
        if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
        else { locationManagerDidChangeAuthorization(manager) }
    }

    func stop() {
        manager.stopUpdatingLocation()
        manager.delegate = nil
        status = .idle; hasFix = false; target = nil; velocity = .zero; wander = .zero; stick = .zero; wish = .zero
        latestFix = nil
    }

    func steer(_ desired: SIMD2<Float>) { wish = desired }
    func teleport(to c: GeoCoordinate) {}
    func returnToFix() { wander = .zero; stick = .zero }

    /// Restarts updates so a still device reports a current reading. Core Location can go quiet
    /// when nothing moves, which would leave the last fix to grow stale.
    func requestFreshFix() {
        guard status == .active || status == .locating,
              [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus) else { return }
        manager.stopUpdatingLocation()
        manager.startUpdatingLocation()
    }

    func tick(dt: Float) {
        guard hasFix else { velocity = .zero; return }
        let g = Mercator.groundScale(latitude: coordinate.latitude)

        stick += (wish - stick) * min(1, dt * 10)
        if simd_length_squared(stick) < 0.0004 { stick = .zero }
        wander += SIMD2<Double>(Double(stick.x), -Double(stick.y)) * Double(dt) / g

        var eased = SIMD2<Float>(0, 0)
        if let target {
            let delta = SIMD2<Float>((target - fixed) * g)        // real metres, east and south
            if simd_length(delta) > Self.snapDistance {
                fixed = target
            } else {
                let step = delta * min(1, dt * 2.5)
                fixed += SIMD2<Double>(step) / g
                eased = SIMD2(step.x, -step.y) / max(dt, 0.001)
                if simd_length(eased) < 0.2 { eased = .zero }
            }
        }
        coordinate = Mercator.unproject(fixed + wander)
        velocity = eased + stick
    }

    /// One fix from Core Location. Internal so tests can feed it.
    func receive(_ c: GeoCoordinate, accuracy: Double, at time: Date = Date()) {
        guard accuracy >= 0 else { return }
        latestFix = LocationFix(coordinate: c, horizontalAccuracy: accuracy, timestamp: time)
        guard hasFix else {
            hasFix = true; status = .active
            coordinate = c; target = Mercator.project(c); fixed = target!
            return
        }
        guard accuracy <= Self.worstAccuracy, let old = target else { return }
        let m = Mercator.project(c), g = Mercator.groundScale(latitude: c.latitude)
        if simd_length((m - old) * g) >= max(3, accuracy * 0.4) { target = m }
    }

    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        switch m.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways: m.startUpdatingLocation()
        case .denied, .restricted: status = .denied
        default: break
        }
    }

    func locationManager(_ m: CLLocationManager, didUpdateLocations l: [CLLocation]) {
        guard let last = l.last else { return }
        receive(GeoCoordinate(latitude: last.coordinate.latitude, longitude: last.coordinate.longitude),
                accuracy: last.horizontalAccuracy, at: last.timestamp)
    }

    func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        if (error as? CLError)?.code == .denied { status = .denied }
        else if !hasFix { status = .failed }
    }
}

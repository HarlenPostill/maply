import Foundation
import CoreLocation

// The app-side model of a trip. Route facts come from the Maply route service and are mapped into
// these types by an adapter; nothing here calculates a route. Wire formats belong to the adapter.

extension GeoCoordinate {
    init(_ c: CLLocationCoordinate2D) { self.init(latitude: c.latitude, longitude: c.longitude) }
    var clCoordinate: CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: latitude, longitude: longitude) }

    /// Great-circle distance in metres.
    func distance(to o: GeoCoordinate) -> Double {
        CLLocation(latitude: latitude, longitude: longitude).distance(from: CLLocation(latitude: o.latitude, longitude: o.longitude))
    }
}

// MARK: Where the trip starts

/// What the game knows about position at one moment. The device fix and the character are kept
/// apart: the character can be walked by the joystick, the fix never is.
struct TripLocationSnapshot: Equatable {
    var followingDevice: Bool
    var deviceFix: LocationFix?
    var character: GeoCoordinate
    /// True whenever the character is not standing on the real device position.
    var characterSimulated: Bool
}

/// Whether "My location" can start a trip, and if not, why.
enum MyLocationStatus: Equatable {
    case off
    case finding
    case rough(accuracy: Double)
    case stale
    case ready(LocationFix)

    /// A fix this far off or this old is not a trip start.
    static let worstAccuracy: Double = 50
    static let maximumAge: TimeInterval = 30

    static func evaluate(_ s: TripLocationSnapshot, now: Date) -> MyLocationStatus {
        guard s.followingDevice else { return .off }
        guard let fix = s.deviceFix else { return .finding }
        if now.timeIntervalSince(fix.timestamp) > maximumAge { return .stale }
        if fix.horizontalAccuracy > worstAccuracy { return .rough(accuracy: fix.horizontalAccuracy) }
        return .ready(fix)
    }

    var fix: LocationFix? { if case .ready(let f) = self { f } else { nil } }
}

enum OriginChoice: Equatable {
    case myLocation
    case character
    case place
}

/// One end of a trip, ready to send.
struct TripEndpoint: Equatable {
    enum Source: Equatable {
        /// A raw device fix and its reported accuracy.
        case device(accuracy: Double)
        /// The character's position, walked by the joystick. Not the person's real location.
        case simulatedCharacter
        case searchedPlace
    }
    var coordinate: GeoCoordinate
    var label: String
    var source: Source
}

/// A place found through search. Apple Maps data: kept in memory for this trip only, never saved.
struct Place: Equatable, Identifiable {
    var id: String { "\(name)|\(coordinate.latitude),\(coordinate.longitude)" }
    var name: String
    var address: String?
    var coordinate: GeoCoordinate
}

// MARK: Request

enum RouteProfile: Equatable {
    /// A manual wheelchair user who can manage slopes. Slopes are reported, never avoided.
    case manualChair
}

struct RoutePlanRequest: Equatable {
    var origin: TripEndpoint
    var destination: TripEndpoint
    var profile: RouteProfile = .manualChair
}


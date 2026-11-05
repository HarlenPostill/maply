import Foundation
import MapKit

/// One search suggestion: text only, with no coordinate until it is resolved.
struct PlaceSuggestion: Identifiable, Equatable {
    var id: String { title + "\n" + subtitle }
    var title: String
    var subtitle: String
    /// The MapKit completion behind it. Nil in tests.
    var completion: MKLocalSearchCompletion?

    static func == (a: Self, b: Self) -> Bool { a.id == b.id }
}

enum PlaceSearchError: Error, Equatable {
    case busy
    case offline
    case notFound
}

/// Destination search. MapKit in the app, a stub in tests.
@MainActor
protocol PlaceSearching: AnyObject {
    func suggestions(for query: String, near region: MKCoordinateRegion) async throws -> [PlaceSuggestion]
    func resolve(_ suggestion: PlaceSuggestion) async throws -> Place
}

/// Apple Maps search through MapKit: free, no key. Apple's terms (Attachment 6 of the developer
/// agreement) require these results to be shown with an Apple map, and allow keeping them only
/// temporarily, so the sheet shows them beside a MapKit map and the app never saves them.
@MainActor
final class AppleMapsPlaceSearch: NSObject, PlaceSearching, MKLocalSearchCompleterDelegate {
    private let completer = MKLocalSearchCompleter()
    private var pending: CheckedContinuation<[PlaceSuggestion], Error>?

    override init() {
        super.init()
        completer.delegate = self
        completer.resultTypes = [.address, .pointOfInterest]
    }

    func suggestions(for query: String, near region: MKCoordinateRegion) async throws -> [PlaceSuggestion] {
        pending?.resume(throwing: CancellationError()); pending = nil
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { c in
                pending = c
                completer.region = region
                completer.queryFragment = query
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.pending?.resume(throwing: CancellationError()); self?.pending = nil
            }
        }
    }

    func resolve(_ suggestion: PlaceSuggestion) async throws -> Place {
        guard let completion = suggestion.completion else { throw PlaceSearchError.notFound }
        let response: MKLocalSearch.Response
        do { response = try await MKLocalSearch(request: MKLocalSearch.Request(completion: completion)).start() }
        catch { throw Self.map(error) }
        guard let item = response.mapItems.first else { throw PlaceSearchError.notFound }
        return Place(name: item.name ?? suggestion.title,
                     address: item.address?.shortAddress ?? (suggestion.subtitle.isEmpty ? nil : suggestion.subtitle),
                     coordinate: GeoCoordinate(item.location.coordinate))
    }

    nonisolated func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
        let results = completer.results.map { PlaceSuggestion(title: $0.title, subtitle: $0.subtitle, completion: $0) }
        MainActor.assumeIsolated {
            pending?.resume(returning: results); pending = nil
        }
    }

    nonisolated func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
        let mapped = Self.map(error)
        MainActor.assumeIsolated {
            if mapped == .notFound { pending?.resume(returning: []) } else { pending?.resume(throwing: mapped) }
            pending = nil
        }
    }

    nonisolated static func map(_ error: Error) -> PlaceSearchError {
        if let mk = error as? MKError {
            switch mk.code {
            case .loadingThrottled: return .busy
            case .placemarkNotFound, .directionsNotFound: return .notFound
            default: return .offline
            }
        }
        return .offline
    }
}

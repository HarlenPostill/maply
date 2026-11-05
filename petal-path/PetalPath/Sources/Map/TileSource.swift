import Foundation

/// Fetches OpenMapTiles vector tiles from OpenFreeMap's public instance, with a disk cache.
///
/// OpenFreeMap needs no key or account. Its terms ask for attribution (see `MapAttribution`), an
/// identifying User-Agent, and no more than 4 parallel requests. Bulk or offline-area downloads are
/// capped, so this only ever fetches tiles around the player, and only when they are needed.
final class TileSource {
    enum Failure: Error { case http(Int), noTemplate }

    /// TileJSON gives the current versioned tile URL, so a new planet build needs no app update.
    private static let tileJSON = URL(string: "https://tiles.openfreemap.org/planet")!
    private static let templateAge: TimeInterval = 7 * 86_400
    private static let tileAge: TimeInterval = 30 * 86_400
    static let maxParallel = 4

    private let session: URLSession
    private let directory: URL
    private let lock = NSLock()
    private var template: String?

    init() {
        let c = URLSessionConfiguration.ephemeral
        c.urlCache = nil
        c.httpMaximumConnectionsPerHost = Self.maxParallel
        c.timeoutIntervalForRequest = 20
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        c.httpAdditionalHeaders = ["User-Agent": "PetalPath/\(v) (com.harlenpostill.PetalPath)"]
        session = URLSession(configuration: c)
        directory = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("PetalPathTiles")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    private func file(_ id: TileID) -> URL { directory.appendingPathComponent("\(TileID.zoom)_\(id.x)_\(id.y).pbf") }
    private var templateFile: URL { directory.appendingPathComponent("template.txt") }

    private func age(_ url: URL) -> TimeInterval? {
        guard let d = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date else { return nil }
        return Date().timeIntervalSince(d)
    }

    /// Calls back on a background queue with decompressed tile bytes. A stale cached tile is
    /// better than an empty world, so it is used whenever the network fails.
    func fetch(_ id: TileID, completion: @escaping (Result<Data, Error>) -> Void) {
        let url = file(id)
        if let a = age(url), a < Self.tileAge, let d = try? Data(contentsOf: url) { completion(.success(d)); return }
        withTemplate { [self] result in
            guard case .success(let t) = result else {
                if let d = try? Data(contentsOf: url) { completion(.success(d)) }
                else if case .failure(let e) = result { completion(.failure(e)) }
                return
            }
            let path = t.replacingOccurrences(of: "{z}", with: "\(TileID.zoom)")
                .replacingOccurrences(of: "{x}", with: "\(id.x)").replacingOccurrences(of: "{y}", with: "\(id.y)")
            session.dataTask(with: URL(string: path)!) { data, response, error in
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if code == 200, let data {
                    try? data.write(to: url, options: .atomic)
                    completion(.success(data))
                } else if code == 204 || code == 404 {
                    completion(.success(Data()))      // open sea has no tile
                } else if let d = try? Data(contentsOf: url) {
                    completion(.success(d))
                } else {
                    completion(.failure(error ?? Failure.http(code)))
                }
            }.resume()
        }
    }

    private func withTemplate(_ done: @escaping (Result<String, Error>) -> Void) {
        lock.lock(); let known = template; lock.unlock()
        if let known { done(.success(known)); return }
        if let a = age(templateFile), a < Self.templateAge, let s = try? String(contentsOf: templateFile, encoding: .utf8) {
            lock.lock(); template = s; lock.unlock()
            done(.success(s)); return
        }
        session.dataTask(with: Self.tileJSON) { [self] data, _, error in
            struct TileJSON: Decodable { var tiles: [String] }
            if let data, let t = (try? JSONDecoder().decode(TileJSON.self, from: data))?.tiles.first {
                lock.lock(); template = t; lock.unlock()
                try? t.write(to: templateFile, atomically: true, encoding: .utf8)
                done(.success(t))
            } else if let s = try? String(contentsOf: templateFile, encoding: .utf8) {
                done(.success(s))
            } else {
                done(.failure(error ?? Failure.noTemplate))
            }
        }.resume()
    }
}

enum MapAttribution {
    /// The wording OpenFreeMap asks non-MapLibre clients to show.
    static let text = "OpenFreeMap © OpenMapTiles  Data from OpenStreetMap"
    static let copyright = URL(string: "https://www.openstreetmap.org/copyright")!
    static let openFreeMap = URL(string: "https://openfreemap.org")!
    static let openMapTiles = URL(string: "https://openmaptiles.org")!
}

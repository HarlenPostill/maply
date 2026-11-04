import Foundation
import Security

/// Server address and gateway login, shared with the widget through the app group.
/// The password lives in the keychain and stays readable after first unlock so the
/// lock screen widget can refresh while the phone is locked.
struct Credentials: Equatable {
    static let appGroup = "group.com.harlenpostill.maply"
    static let defaultServer = "http://127.0.0.1:8000"

    var server: String
    var username: String
    var password: String

    var isLocal: Bool {
        guard let host = URL(string: server)?.host else { return false }
        return ["127.0.0.1", "localhost", "[::1]", "::1"].contains(host)
    }

    var isComplete: Bool {
        URL(string: server)?.host != nil && (isLocal || (!username.isEmpty && !password.isEmpty))
    }

    private static var defaults: UserDefaults { UserDefaults(suiteName: appGroup) ?? .standard }
    private static let service = "com.harlenpostill.maply.gateway"

    static func load() -> Credentials {
        #if DEBUG
        // Debug builds can select a local API through MAPLY_SERVER.
        if let server = ProcessInfo.processInfo.environment["MAPLY_SERVER"] {
            return Credentials(server: server, username: "", password: "")
        }
        #endif
        let savedServer = defaults.string(forKey: "server")
        if let savedServer, URL(string: savedServer)?.host != "maply.hrln-interactive.com" {
            return Credentials(server: savedServer,
                    username: defaults.string(forKey: "username") ?? "harlen",
                    password: readPassword() ?? "")
        }
        return Credentials(server: defaultServer, username: "", password: "")
    }

    func save() {
        Self.defaults.set(server.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "server")
        Self.defaults.set(username.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "username")
        Self.writePassword(password)
    }

    private static func query(group: Bool) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: "gateway"]
        if group { q[kSecAttrAccessGroup as String] = appGroup }
        return q
    }

    private static func readPassword() -> String? {
        for group in [true, false] {
            var q = query(group: group)
            q[kSecReturnData as String] = true
            q[kSecMatchLimit as String] = kSecMatchLimitOne
            var item: CFTypeRef?
            if SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
               let data = item as? Data, let value = String(data: data, encoding: .utf8) {
                return value
            }
        }
        return nil
    }

    private static func writePassword(_ password: String) {
        for group in [true, false] { SecItemDelete(query(group: group) as CFDictionary) }
        guard !password.isEmpty else { return }
        for group in [true, false] {
            var q = query(group: group)
            q[kSecValueData as String] = Data(password.utf8)
            q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            // The shared access group needs the app group entitlement; fall back to app-only storage.
            if SecItemAdd(q as CFDictionary, nil) == errSecSuccess { return }
        }
    }
}

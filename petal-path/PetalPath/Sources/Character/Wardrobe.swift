import SwiftUI

// Wardrobe catalogue. To add an item: mesh variants get a case naming their SK_ mesh in player.glb,
// texture variants get a case naming a PNG in Resources that uses the same UVs as the mesh.

enum HairStyle: String, CaseIterable, Identifiable, Codable {
    case short = "Short", bob = "Bob"
    var id: String { rawValue }
    /// A saved outfit may name a style that has since been removed.
    init(from decoder: Decoder) throws {
        self = HairStyle(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .short
    }
    var mesh: String {
        switch self {
        case .short: return "SK_Hair"
        case .bob: return "SK_Hair_Bob"
        }
    }
}

enum TopDesign: String, CaseIterable, Identifiable, Codable {
    case argyle = "Argyle", daisy = "Daisy"
    var id: String { rawValue }
    var texture: String? { self == .argyle ? nil : "T_Top_Daisy" }
}

enum Bottoms: String, CaseIterable, Identifiable, Codable {
    case shorts = "Shorts", skirt = "Skirt", dress = "Dress"
    var id: String { rawValue }
    /// The dress is a whole outfit: it replaces the top too and brings its own tights.
    var meshes: [String] {
        switch self {
        case .shorts: return ["SK_Bottoms"]
        case .skirt: return ["SK_Skirt"]
        case .dress: return ["SK_Dress", "SK_Tights"]
        }
    }
}

enum ShoeStyle: String, CaseIterable, Identifiable, Codable {
    case loafers = "Loafers", maryJanes = "Mary Janes"
    var id: String { rawValue }
    var mesh: String { self == .loafers ? "SK_Shoes" : "SK_Shoes_MaryJane" }
}

enum ShoeColour: String, CaseIterable, Identifiable, Codable {
    case brown = "Brown", terracotta = "Terracotta", olive = "Olive", mustard = "Mustard", blue = "Blue"
    var id: String { rawValue }
    var texture: String? { self == .brown ? nil : "T_Shoes_\(rawValue)" }
    var swatch: Color {
        switch self {
        case .brown: return Color(red: 0.42, green: 0.26, blue: 0.17)
        case .terracotta: return Color(red: 0.71, green: 0.38, blue: 0.24)
        case .olive: return Color(red: 0.46, green: 0.48, blue: 0.27)
        case .mustard: return Color(red: 0.79, green: 0.61, blue: 0.25)
        case .blue: return Color(red: 0.43, green: 0.52, blue: 0.59)
        }
    }
}

/// One player's look. Saved to UserDefaults by `GameState`.
struct Outfit: Equatable, Codable {
    var hair = HairStyle.short
    var top = TopDesign.argyle
    var bottoms = Bottoms.shorts
    var shoeStyle = ShoeStyle.loafers
    var shoes = ShoeColour.brown

    var meshes: Set<String> {
        var m = Set([hair.mesh, shoeStyle.mesh] + bottoms.meshes)
        if bottoms != .dress { m.formUnion(["SK_Top", "SK_Socks"]) }
        return m
    }

    /// Picking a top while wearing the dress swaps back to separates.
    mutating func wear(top t: TopDesign) {
        top = t
        if bottoms == .dress { bottoms = .shorts }
    }

    func textureOverride(part: String) -> String? {
        switch part {
        case "SK_Top": return top.texture
        case "SK_Shoes", "SK_Shoes_MaryJane": return shoes.texture   // both styles share the sheet; socks stay cream
        default: return nil
        }
    }
}

enum Wardrobe {
    /// Meshes where exactly one option is worn at a time.
    static let slottedParts: Set<String> = Set(HairStyle.allCases.map(\.mesh) + Bottoms.allCases.flatMap(\.meshes) + ShoeStyle.allCases.map(\.mesh))
        .union(["SK_Top", "SK_Socks"])
        .union(["SK_Hair_Bunches"])      // still in player.glb, must stay listed here or it is always drawn
    static let allTextures: [String] = TopDesign.allCases.compactMap(\.texture) + ShoeColour.allCases.compactMap(\.texture)
}

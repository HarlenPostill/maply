import SwiftUI

struct Chip: View {
    let title: String; let on: Bool; let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(.subheadline, design: .rounded, weight: .semibold))
                .frame(maxWidth: .infinity, minHeight: 40)
                .foregroundStyle(on ? cream : ink)
                .background(on ? olive : Color.white.opacity(0.7), in: Capsule())
        }.buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// Round colour swatch for shoe colours.
struct Swatch: View {
    let colour: Color; let on: Bool; let label: String; let action: () -> Void
    var body: some View {
        Button(action: action) {
            Circle().fill(colour).frame(width: 36, height: 36)
                .overlay(Circle().stroke(on ? olive : Color.white.opacity(0.7), lineWidth: on ? 3 : 1.5).padding(on ? -4 : 0))
                .frame(maxWidth: .infinity, minHeight: 44)
        }.buttonStyle(.plain).accessibilityLabel(label).accessibilityAddTraits(on ? .isSelected : [])
    }
}

struct SlotRow<Content: View>: View {
    let title: String; @ViewBuilder let content: Content
    var body: some View {
        HStack(spacing: 8) {
            Text(title).font(.system(.caption, design: .rounded, weight: .bold)).foregroundStyle(ink.opacity(0.7)).frame(width: 50, alignment: .leading)
            content
        }
    }
}

/// A half sheet, so she stays in view above it while you dress her. Everything saves as you tap.
struct SettingsView: View {
    @ObservedObject var state: GameState
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Wardrobe").font(.system(.title3, design: .rounded, weight: .bold)).foregroundStyle(ink)
                    Spacer()
                    Button("Done") { dismiss() }.font(.system(.body, design: .rounded, weight: .semibold)).tint(olive)
                }
                .padding(.bottom, 2)
                SlotRow(title: "Hair") { ForEach(HairStyle.allCases) { o in Chip(title: o.rawValue, on: state.outfit.hair == o) { state.outfit.hair = o } } }
                SlotRow(title: "Top") { ForEach(TopDesign.allCases) { o in Chip(title: o.rawValue, on: state.outfit.bottoms != .dress && state.outfit.top == o) { state.outfit.wear(top: o) } } }
                SlotRow(title: "Bottom") { ForEach(Bottoms.allCases) { o in Chip(title: o.rawValue, on: state.outfit.bottoms == o) { state.outfit.bottoms = o } } }
                SlotRow(title: "Shoes") { ForEach(ShoeStyle.allCases) { o in Chip(title: o.rawValue, on: state.outfit.shoeStyle == o) { state.outfit.shoeStyle = o } } }
                SlotRow(title: "Colour") { ForEach(ShoeColour.allCases) { o in Swatch(colour: o.swatch, on: state.outfit.shoes == o, label: o.rawValue) { state.outfit.shoes = o } } }

                Divider().overlay(ink.opacity(0.15)).padding(.vertical, 6)
                Text("Debug").font(.system(.subheadline, design: .rounded, weight: .bold)).foregroundStyle(ink)
                Toggle(isOn: $state.fastWalk) {
                    Text("Fast walk (5×, feet will slide)").font(.system(.subheadline, design: .rounded)).foregroundStyle(ink)
                }.tint(olive)

                Divider().overlay(ink.opacity(0.15)).padding(.vertical, 6)
                Text("Map data").font(.system(.subheadline, design: .rounded, weight: .bold)).foregroundStyle(ink)
                Text("Tiles from OpenFreeMap, in the OpenMapTiles schema. Map data © OpenStreetMap contributors, available under the Open Database License.")
                    .font(.system(.footnote, design: .rounded)).foregroundStyle(ink.opacity(0.8))
                HStack(spacing: 16) {
                    Link("OpenStreetMap", destination: MapAttribution.copyright)
                    Link("OpenFreeMap", destination: MapAttribution.openFreeMap)
                    Link("OpenMapTiles", destination: MapAttribution.openMapTiles)
                }
                .font(.system(.footnote, design: .rounded, weight: .semibold)).tint(olive)
            }
            .padding(18)
        }
    }
}

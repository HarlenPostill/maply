import SwiftUI
import MetalKit

@main
struct PlayerDemoApp: App {
    var body: some Scene { WindowGroup { ContentView() } }
}

struct MetalView: UIViewRepresentable {
    @ObservedObject var state: DemoState
    final class Coordinator { var renderer: MTKViewDelegate?; var scene: CharacterScene? }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MTKView {
        let v = MTKView(frame: .zero, device: MTLCreateSystemDefaultDevice())
        v.colorPixelFormat = .bgra8Unorm_srgb
        v.preferredFramesPerSecond = 60
        v.isOpaque = true
        do {
            let scene = try CharacterScene(state: state)
            context.coordinator.scene = scene
            #if targetEnvironment(simulator)
            let r = try SimulatorRenderer(view: v, scene: scene)
            let name = "Simulator preview · classic Metal"
            #else
            let r = try Metal4Renderer(view: v, scene: scene)
            let name = "Metal 4"
            #endif
            guard let r else { throw NSError(domain: "Player", code: 1, userInfo: [NSLocalizedDescriptionKey: "This device does not support Metal 4."]) }
            v.delegate = r
            context.coordinator.renderer = r
            r.mtkView(v, drawableSizeWillChange: v.drawableSize)
            DispatchQueue.main.async { state.backend = name }
        } catch {
            DispatchQueue.main.async { state.error = error.localizedDescription }
        }
        return v
    }
    func updateUIView(_ v: MTKView, context: Context) {}
}

private let cream = Color(red: 0.98, green: 0.95, blue: 0.89)
private let ink = Color(red: 0.29, green: 0.20, blue: 0.14)
private let olive = Color(red: 0.42, green: 0.42, blue: 0.22)

struct Chip: View {
    let title: String; let on: Bool; let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(.subheadline, design: .rounded, weight: .semibold))
                .frame(maxWidth: .infinity, minHeight: 40)
                .foregroundStyle(on ? cream : ink)
                .background(on ? olive : Color.white.opacity(0.55), in: Capsule())
        }.buttonStyle(.plain)
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
        }.buttonStyle(.plain).accessibilityLabel(label)
    }
}

struct SlotRow<Content: View>: View {
    let title: String; @ViewBuilder let content: Content
    var body: some View {
        HStack(spacing: 8) {
            Text(title).font(.system(.caption, design: .rounded, weight: .bold)).foregroundStyle(ink.opacity(0.7)).frame(width: 46, alignment: .leading)
            content
        }
    }
}

enum Panel: String, CaseIterable, Identifiable { case moves = "Moves", wardrobe = "Wardrobe"; var id: String { rawValue } }

struct ContentView: View {
    @StateObject private var state = DemoState()
    @State private var panel: Panel = .wardrobe
    @State private var dragStart: (Float, Float)?
    @State private var zoomStart: Float?

    var body: some View {
        ZStack(alignment: .bottom) {
            MetalView(state: state)
                .ignoresSafeArea()
                .gesture(DragGesture().onChanged { g in
                    let s = dragStart ?? (state.yaw, state.pitch); dragStart = s
                    state.yaw = s.0 - Float(g.translation.width) * 0.008
                    state.pitch = min(1.1, max(-0.05, s.1 + Float(g.translation.height) * 0.005))
                }.onEnded { _ in dragStart = nil })
                .simultaneousGesture(MagnifyGesture().onChanged { g in
                    let s = zoomStart ?? state.distance; zoomStart = s
                    state.distance = min(6, max(1, s / Float(g.magnification)))
                }.onEnded { _ in zoomStart = nil })

            VStack {
                HStack {
                    Text(state.backend).font(.system(.caption, design: .rounded, weight: .semibold))
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(cream.opacity(0.85), in: Capsule()).foregroundStyle(ink)
                    Spacer()
                }.padding(.horizontal, 16)
                Spacer()
            }
            if let e = state.error {
                Text(e).padding().background(cream, in: RoundedRectangle(cornerRadius: 14)).foregroundStyle(ink).padding(.bottom, 300)
            }

            VStack(spacing: 8) {
                HStack(spacing: 8) { ForEach(Panel.allCases) { p in Chip(title: p.rawValue, on: panel == p) { panel = p; state.wardrobeOpen = p == .wardrobe } } }
                Divider().overlay(ink.opacity(0.15))
                if panel == .moves {
                    HStack(spacing: 8) { ForEach(Move.allCases) { m in Chip(title: m.rawValue, on: state.move == m) { state.move = m } } }
                    HStack(spacing: 8) { ForEach(Expression.allCases) { x in Chip(title: x.rawValue, on: state.expression == x) { state.expression = x } } }
                } else {
                    SlotRow(title: "Hair") { ForEach(HairStyle.allCases) { o in Chip(title: o.rawValue, on: state.outfit.hair == o) { state.outfit.hair = o } } }
                    SlotRow(title: "Top") { ForEach(TopDesign.allCases) { o in Chip(title: o.rawValue, on: state.outfit.bottoms != .dress && state.outfit.top == o) { state.outfit.wear(top: o) } } }
                    SlotRow(title: "Bottom") { ForEach(Bottoms.allCases) { o in Chip(title: o.rawValue, on: state.outfit.bottoms == o) { state.outfit.bottoms = o } } }
                    SlotRow(title: "Shoes") { ForEach(ShoeStyle.allCases) { o in Chip(title: o.rawValue, on: state.outfit.shoeStyle == o) { state.outfit.shoeStyle = o } } }
                    SlotRow(title: "Colour") { ForEach(ShoeColour.allCases) { o in Swatch(colour: o.swatch, on: state.outfit.shoes == o, label: o.rawValue) { state.outfit.shoes = o } } }
                }
            }
            .padding(12)
            .background(cream.opacity(0.9), in: RoundedRectangle(cornerRadius: 22))
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
    }
}

import SwiftUI
import simd

let cream = Color(red: 0.98, green: 0.95, blue: 0.89)
let ink = Color(red: 0.29, green: 0.20, blue: 0.14)
let olive = Color(red: 0.42, green: 0.42, blue: 0.22)

/// The map, with the HUD laid over it.
struct GameView: View {
    @StateObject private var state = GameState()
    @State private var dragStart: (yaw: Float, pitch: Float)?
    @State private var zoomStart: Float?

    var body: some View {
        ZStack {
            MetalView(state: state)
                .ignoresSafeArea()
                .gesture(DragGesture().onChanged { g in
                    let s = dragStart ?? (state.cameraYaw, state.cameraPitch); dragStart = s
                    state.cameraYaw = s.yaw - Float(g.translation.width) * 0.007
                    state.cameraPitch = min(1.25, max(0.5, s.pitch + Float(g.translation.height) * 0.003))
                }.onEnded { _ in dragStart = nil })
                .simultaneousGesture(MagnifyGesture().onChanged { g in
                    let s = zoomStart ?? state.cameraDistance; zoomStart = s
                    state.cameraDistance = min(520, max(130, s / Float(g.magnification)))
                }.onEnded { _ in zoomStart = nil })
                .accessibilityHidden(true)

            VStack(spacing: 0) {
                HStack(alignment: .top, spacing: 8) {
                    VStack(alignment: .leading, spacing: 6) {
                        if let street = state.street {
                            Pill(text: street, weight: .bold).accessibilityIdentifier("street")
                        }
                        Pill(text: "\(state.backend) · \(state.fps) fps", weight: .medium).opacity(0.8)
                    }
                    Spacer()
                    Button { state.settingsOpen = true } label: {
                        Image(systemName: "tshirt.fill").font(.title3).foregroundStyle(ink)
                            .frame(width: 48, height: 48).background(cream.opacity(0.92), in: Circle())
                    }
                    .accessibilityLabel("Settings").accessibilityIdentifier("settings")
                }
                .padding(.horizontal, 16).padding(.top, 6)
                Spacer()
                if let notice = state.mapNotice ?? state.error { Pill(text: notice, weight: .semibold).padding(.bottom, 12) }
                HStack(alignment: .bottom) {
                    if !state.useMyLocation { Joystick(state: state) }
                    Spacer()
                    Button {
                        state.stick = .zero
                        state.useMyLocation.toggle()
                    } label: {
                        Label(state.useMyLocation ? "Back to Sydney" : "Use my location",
                              systemImage: state.useMyLocation ? "building.2.fill" : "location.fill")
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .padding(.horizontal, 14).padding(.vertical, 11)
                            .foregroundStyle(ink).background(cream.opacity(0.92), in: Capsule())
                    }
                    .accessibilityIdentifier("locationMode")
                }
                .padding(.horizontal, 20).padding(.bottom, 6)
                Link(destination: MapAttribution.copyright) {
                    Text(MapAttribution.text).font(.system(size: 11, weight: .medium, design: .rounded))
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .foregroundStyle(ink).background(cream.opacity(0.85), in: Capsule())
                }
                .accessibilityIdentifier("attribution")
            }
            .opacity(state.settingsOpen ? 0 : 1)
            .animation(.easeOut(duration: 0.2), value: state.settingsOpen)
        }
        .alert("Location is off", isPresented: $state.locationBlocked) {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            Button("Stay in Sydney", role: .cancel) {}
        } message: {
            Text("Allow Petal Path to use your location to walk where you are.")
        }
        .sheet(isPresented: $state.settingsOpen) {
            SettingsView(state: state)
                .presentationDetents([.fraction(0.56), .large])
                .presentationBackgroundInteraction(.enabled(upThrough: .fraction(0.56)))
                .presentationBackground(cream)
                .presentationCornerRadius(26)
        }
    }
}

struct Pill: View {
    let text: String; let weight: Font.Weight
    var body: some View {
        Text(text).font(.system(.caption, design: .rounded, weight: weight)).lineLimit(1)
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(cream.opacity(0.9), in: Capsule()).foregroundStyle(ink)
    }
}

/// Debug movement control. Writes `state.stick` directly, the scene reads it every frame.
struct Joystick: View {
    @ObservedObject var state: GameState
    @State private var knob = CGSize.zero
    private let radius: CGFloat = 58

    var body: some View {
        ZStack {
            Circle().fill(cream.opacity(0.5)).overlay(Circle().stroke(cream.opacity(0.9), lineWidth: 3))
            Circle().fill(cream).frame(width: 58, height: 58)
                .overlay(Circle().fill(olive.opacity(0.35)).frame(width: 26, height: 26))
                .shadow(color: ink.opacity(0.25), radius: 4, y: 2)
                .offset(knob)
        }
        .frame(width: radius * 2, height: radius * 2)
        .contentShape(Circle())
        .gesture(DragGesture(minimumDistance: 0).onChanged { g in
            var v = SIMD2(Float(g.location.x - radius), Float(g.location.y - radius))
            let l = simd_length(v), r = Float(radius)
            if l > r { v *= r / l }
            knob = CGSize(width: CGFloat(v.x), height: CGFloat(v.y))
            state.stick = SIMD2(v.x, -v.y) / r
        }.onEnded { _ in
            withAnimation(.spring(response: 0.22, dampingFraction: 0.7)) { knob = .zero }
            state.stick = .zero
        })
        .accessibilityLabel("Joystick").accessibilityIdentifier("joystick")
    }
}

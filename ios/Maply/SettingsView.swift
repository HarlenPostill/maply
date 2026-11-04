import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: RunStore
    @Environment(\.dismiss) private var dismiss
    @State private var draft = Credentials.load()
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Server", text: $draft.server)
                        .keyboardType(.URL).textContentType(.URL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Username", text: $draft.username)
                        .textContentType(.username)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Password", text: $draft.password)
                        .textContentType(.password)
                } header: {
                    Text("Maply gateway")
                } footer: {
                    Text("The local server needs no login. For an authenticated server, the password is stored in this iPhone's keychain and shared with the lock screen widget.")
                }
                if let error = store.error, !saving {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        saving = true
                        Task {
                            await store.save(draft)
                            saving = false
                            if store.error == nil { dismiss() }
                        }
                    }
                    .disabled(!draft.isComplete || saving)
                }
            }
        }
    }
}

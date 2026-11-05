import SwiftUI

struct NewRunView: View {
    @EnvironmentObject private var store: RunStore
    @Environment(\.dismiss) private var dismiss
    @State private var jobs: [Job] = []
    @State private var models: [ModelOption] = []
    @State private var job: Job?
    @State private var config = RunConfig()
    @State private var fallbacks = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Job") {
                    if jobs.isEmpty && error == nil {
                        ProgressView()
                    }
                    Picker("Job", selection: $job) {
                        Text("Choose a job").tag(Job?.none)
                        ForEach(jobs) { job in
                            Text(job.name).tag(Job?.some(job))
                        }
                    }
                    if let pending = job?.counts?.pending {
                        Text("\(pending.formatted()) photos pending").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Section {
                    NavigationLink {
                        ModelPicker(models: models, selection: $config.model)
                    } label: {
                        LabeledContent("Model", value: config.model)
                    }
                    TextField("Fallback models, comma separated", text: $fallbacks)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                } header: {
                    Text("Capture and tagging model")
                } footer: {
                    Text("Fallbacks handle provider failures.")
                }
                Section("Goals") {
                    number("Photos to handle", value: $config.maxPhotos)
                    number("Max model requests", value: $config.maxRequests)
                    decimal("Stop after cost (USD)", value: $config.maxCostUsd)
                    decimal("Run duration (hours)", value: $config.hours)
                }
                Section("Per photo") {
                    Stepper("Maximum assessments: \(config.maxSteps)", value: $config.maxSteps, in: 1...20)
                    Stepper("Minimum confidence: \(config.minConfidence.formatted(.percent.precision(.fractionLength(0))))",
                            value: $config.minConfidence, in: 0.5...1, step: 0.01)
                    Stepper("Loading wait: \(Int(config.loadSeconds)) s", value: $config.loadSeconds, in: 2...30, step: 1)
                }
                Section {
                    Toggle("YOLO mode", isOn: Binding(get: { config.yolo ?? false }, set: { config.yolo = $0 }))
                } footer: {
                    Text("YOLO runs never pause themselves: errors, worker crashes, service restarts and model stop requests are logged and the run continues. Only the photo, request, cost and duration goals, or your own Pause/Stop, end it.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("New capture run")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") { Task { await create() } }
                        .disabled(job == nil || saving || config.model.isEmpty)
                }
            }
            .disabled(saving)
            .task { await load() }
        }
    }

    private func number(_ title: String, value: Binding<Int>) -> some View {
        LabeledContent(title) {
            TextField(title, value: value, format: .number)
                .keyboardType(.numberPad).multilineTextAlignment(.trailing)
        }
    }

    private func decimal(_ title: String, value: Binding<Double>) -> some View {
        LabeledContent(title) {
            TextField(title, value: value, format: .number)
                .keyboardType(.decimalPad).multilineTextAlignment(.trailing)
        }
    }

    private func load() async {
        do {
            jobs = try await store.api.jobs()
            if job == nil { job = jobs.first }
        } catch {
            self.error = error.localizedDescription
        }
        // The model catalogue is optional; a full OpenRouter ID can be typed instead.
        models = (try? await store.api.models()) ?? []
    }

    private func create() async {
        guard let job else { return }
        saving = true
        defer { saving = false }
        var value = config
        value.model = value.model.trimmingCharacters(in: .whitespaces)
        value.fallbackModels = fallbacks.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        do {
            _ = try await store.api.create(NewRun(jobId: job.id, config: value))
            await store.refresh()
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct ModelPicker: View {
    let models: [ModelOption]
    @Binding var selection: String
    @State private var query = ""
    @Environment(\.dismiss) private var dismiss

    private var filtered: [ModelOption] {
        query.isEmpty ? models : models.filter { $0.id.localizedCaseInsensitiveContains(query) || $0.name.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        List {
            Section {
                TextField("OpenRouter model ID", text: $selection)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
            } footer: {
                Text(models.isEmpty ? "Model list unavailable. Enter a full OpenRouter model ID." : "Or choose an image model with structured output.")
            }
            ForEach(filtered) { model in
                Button {
                    selection = model.id
                    dismiss()
                } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(model.name).foregroundStyle(.primary)
                            Text(model.id).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if model.id == selection { Image(systemName: "checkmark") }
                    }
                }
            }
        }
        .searchable(text: $query)
        .navigationTitle("Model")
    }
}

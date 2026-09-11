import AppKit
import Combine
import SwiftUI

struct BridgeConfiguration: Decodable {
    let python: String
}

enum BridgeError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let message): return message }
    }
}

func callBridge(_ payload: [String: String]) throws -> Reply {
    let process = Process()
    let runtime = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/ds-runtime/ds-runtime")
    if FileManager.default.isExecutableFile(atPath: runtime.path) {
        process.executableURL = runtime
        process.arguments = ["--desktop-bridge"]
    } else if let configURL = Bundle.main.url(forResource: "bridge", withExtension: "json") {
        let configuration = try JSONDecoder().decode(
            BridgeConfiguration.self, from: Data(contentsOf: configURL)
        )
        process.executableURL = URL(fileURLWithPath: configuration.python)
        process.arguments = ["-m", "devin_switch.desktop"]
    } else {
        throw BridgeError.message("The app is missing its CLI runtime. Reinstall the app or run make app.")
    }
    process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
    let input = Pipe()
    let output = Pipe()
    process.standardInput = input
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try process.run()
    input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: payload))
    try input.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard !data.isEmpty else {
        throw BridgeError.message("The local CLI could not respond. Reinstall with make app.")
    }
    return try JSONDecoder().decode(Reply.self, from: data)
}

@MainActor final class AppModel: ObservableObject {
    @Published var snapshot = Snapshot()
    @Published var focus: String?
    @Published var working = false
    @Published var message = ""
    @Published var failed = false
    @Published var adding = false
    @Published var addError = ""
    @Published var refreshingUsage = false
    @Published var removing: Account?
    @Published var resuming: Account?
    @Published var autoRefreshUsage: Bool {
        didSet {
            guard autoRefreshUsage != oldValue else { return }
            defaults.set(autoRefreshUsage, forKey: "autoRefreshUsage")
            if autoRefreshUsage { refreshUsage(force: true) }
        }
    }
    @AppStorage var project: String
    private let defaults: UserDefaults
    private let bridge: ([String: String]) async throws -> Reply
    private var refreshSubscriptions = Set<AnyCancellable>()
    private var refreshingState = false
    private var stateRevision = 0
    private var lastUsageRefresh: Date?
    nonisolated static let usageRefreshInterval: TimeInterval = 60

    init(
        defaults: UserDefaults = .standard,
        bridge: @escaping ([String: String]) async throws -> Reply = { payload in
            try await Task.detached { try callBridge(payload) }.value
        }
    ) {
        self.defaults = defaults
        autoRefreshUsage = defaults.object(forKey: "autoRefreshUsage") as? Bool ?? true
        _project = AppStorage(wrappedValue: "", "projectFolder", store: defaults)
        self.bridge = bridge
    }

    var account: Account? { snapshot.accounts.first { $0.name == focus } }
    var blocked: Bool { working || snapshot.busy }
    var openChats: [SessionRun] { snapshot.runs.filter { $0.active && $0.kind == "chat" } }
    func openCount(_ account: Account) -> Int { openChats.filter { $0.account == account.name }.count }
    func inUse(_ account: Account) -> Bool { snapshot.runs.contains { $0.active && $0.account == account.name } }

    func useForNewChats(_ account: Account) {
        perform(["action": "select", "account": account.name])
    }

    func chooseChat(_ account: Account) {
        failed = false
        resuming = account
    }

    func startRefreshing(stateInterval: TimeInterval = 4, usageInterval: TimeInterval = AppModel.usageRefreshInterval) {
        guard refreshSubscriptions.isEmpty else { return }
        Timer.publish(every: stateInterval, on: .main, in: .common).autoconnect()
            .sink { [weak self] _ in self?.refresh() }.store(in: &refreshSubscriptions)
        Timer.publish(every: usageInterval, on: .main, in: .common).autoconnect()
            .sink { [weak self] _ in
                guard let self, self.autoRefreshUsage else { return }
                self.refreshUsage(force: true)
            }.store(in: &refreshSubscriptions)
        NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)
            .merge(with: NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification))
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.refreshAfterActivation() }.store(in: &refreshSubscriptions)
        refreshAfterActivation()
    }

    func refreshAfterActivation(now: Date = Date()) {
        refresh()
        if autoRefreshUsage && (lastUsageRefresh.map { now.timeIntervalSince($0) >= Self.usageRefreshInterval } ?? true) {
            refreshUsage(force: true)
        }
    }

    private func apply(_ state: Snapshot) {
        if snapshot != state { snapshot = state }
        if let focus, !state.accounts.contains(where: { $0.name == focus }) { self.focus = nil }
    }

    func refresh() {
        guard !working, !refreshingState else { return }
        refreshingState = true
        let revision = stateRevision
        Task {
            defer {
                refreshingState = false
                if revision != stateRevision { refresh() }
            }
            do {
                let reply = try await bridge(["action": "state"])
                guard revision == stateRevision, !working else { return }
                if reply.ok {
                    if let state = reply.state { apply(state) }
                } else {
                    failed = true
                    message = reply.message ?? "Could not refresh accounts."
                }
            } catch {
                guard revision == stateRevision, !working else { return }
                failed = true
                message = error.localizedDescription
            }
        }
    }

    func perform(_ payload: [String: String]) {
        let operation = payload["action"] ?? "state"
        if operation == "state" { refresh(); return }
        guard !working else { return }
        working = true
        stateRevision += 1
        Task {
            do {
                let reply = try await bridge(payload)
                if !reply.ok {
                    if operation == "add" {
                        addError = reply.message ?? "Could not add the account."
                    } else {
                        failed = true
                        message = reply.message ?? "Could not complete this action."
                    }
                } else {
                    if let state = reply.state { apply(state) }
                    if let newFocus = reply.focus { focus = newFocus }
                    message = reply.message ?? ""
                    failed = false
                    if operation == "add" {
                        adding = false
                        addError = ""
                        focus = payload["account"]
                    }
                    if let path = reply.launcher {
                        if operation == "resume_session" { resuming = nil }
                        openTerminal(path)
                    }
                }
            } catch {
                failed = true
                message = error.localizedDescription
            }
            working = false
            refresh(); refreshUsage()
        }
    }

    func refreshUsage(force: Bool = false) {
        guard !refreshingUsage else { return }
        refreshingUsage = true
        lastUsageRefresh = Date()
        Task {
            do {
                let payload = ["action": "usage", "force": force ? "true" : "false"]
                let reply = try await bridge(payload)
                if reply.ok {
                    if let state = reply.state {
                        stateRevision += 1
                        var updatedSnapshot = snapshot
                        for index in updatedSnapshot.accounts.indices {
                            if let updated = state.accounts.first(where: { $0.name == updatedSnapshot.accounts[index].name }) {
                                updatedSnapshot.accounts[index].usage = updated.usage
                            }
                        }
                        apply(updatedSnapshot)
                    }
                } else {
                    failed = true
                    message = reply.message ?? "Could not refresh usage."
                }
            } catch {
                failed = true
                message = error.localizedDescription
            }
            refreshingUsage = false
            refresh()
        }
    }

    func email(_ account: Account) -> String {
        account.usage.email ?? snapshot.profiles.first { $0.directory == account.chrome_profile }?.email ?? ""
    }

    func suggestedName(_ profile: ChromeProfile) -> String {
        let names = Set(snapshot.accounts.map(\.name))
        return (1...(names.count + 1)).map { "\(profile.prefix)-\($0)" }.first { !names.contains($0) }!
    }

    func openTerminal(_ path: String) {
        guard let terminal = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Terminal") else {
            failed = true
            message = "Terminal could not be found on this Mac."
            return
        }
        NSWorkspace.shared.open(
            [URL(fileURLWithPath: path)], withApplicationAt: terminal,
            configuration: NSWorkspace.OpenConfiguration()
        ) { _, error in
            if let error {
                Task { @MainActor in
                    self.failed = true
                    self.message = error.localizedDescription
                }
            }
        }
    }

    func act(_ action: String) {
        guard let account else { return }
        perform(["action": action, "account": account.name, "project": project])
    }

    func chooseProject() {
        let panel = NSOpenPanel()
        panel.title = "Choose your project folder"
        panel.prompt = "Use folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if !project.isEmpty { panel.directoryURL = URL(fileURLWithPath: project) }
        panel.begin { result in
            if result == .OK, let url = panel.url { self.project = url.path }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var hoveredAccount: String?
    @AppStorage("sidebarVisible") private var sidebarVisible = true

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 224)
                .frame(width: sidebarVisible ? 224 : 0, alignment: .leading).clipped()
                .allowsHitTesting(sidebarVisible).accessibilityHidden(!sidebarVisible)
            Rectangle().fill(Palette.line).frame(width: sidebarVisible ? 1 : 0)
            VStack(spacing: 0) {
                toolbar
                Rectangle().fill(Palette.line).frame(height: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 26) {
                        if let account = model.account { detail(account) }
                        else { overview }
                    }.padding(30).frame(maxWidth: .infinity, alignment: .topLeading)
                }
                footer
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(minWidth: 1040, minHeight: 700)
        .background(Palette.background).foregroundStyle(Palette.text)
        .preferredColorScheme(.dark).tint(Color.gray)
        .sheet(isPresented: $model.adding) { AddAccountView().environmentObject(model) }
        .sheet(item: $model.resuming) { account in ResumeSessionView(account: account).environmentObject(model) }
        .alert("Remove local profile?", isPresented: Binding(
            get: { model.removing != nil }, set: { if !$0 { model.removing = nil } }
        ), presenting: model.removing) { account in
            Button("Remove \(account.name)", role: .destructive) {
                model.perform(["action": "remove", "account": account.name, "confirmed": "true"])
            }
            Button("Cancel", role: .cancel) { model.removing = nil }
        } message: { account in
            Text("This removes \(account.name)’s saved login, settings and quota cache from Devin Switch. Shared chats, repository files and the Chrome profile are kept. You will need to sign in again if you add it back.")
        }
        .task { model.startRefreshing() }
    }

    var toolbar: some View {
        HStack(spacing: 10) {
            Button {
                withAnimation(.easeInOut(duration: 0.18)) { sidebarVisible.toggle() }
            } label: {
                Image(systemName: "sidebar.left").frame(width: 28, height: 28).contentShape(Rectangle())
            }.buttonStyle(.plain)
                .help(sidebarVisible ? "Collapse sidebar" : "Expand sidebar")
                .accessibilityLabel(sidebarVisible ? "Collapse sidebar" : "Expand sidebar")
                .keyboardShortcut("s", modifiers: [.command, .control])
            Image(systemName: model.focus == nil ? "square.grid.2x2" : "person.crop.circle")
                .foregroundStyle(Palette.secondary)
            Text(model.focus ?? "All accounts").font(.system(size: 13, weight: .medium))
            Spacer()
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("Reading usage…").font(.system(size: 11)).foregroundStyle(Palette.secondary)
            }.opacity(model.refreshingUsage ? 1 : 0).accessibilityHidden(!model.refreshingUsage)
            Toggle("Auto refresh", isOn: $model.autoRefreshUsage)
                .toggleStyle(.switch).controlSize(.mini).tint(Palette.green)
                .font(.system(size: 11)).foregroundStyle(Palette.secondary)
                .help("Refresh usage every minute while this app is open")
            Button { model.refreshUsage(force: true) } label: {
                Label("Refresh usage", systemImage: "arrow.clockwise")
            }.buttonStyle(MonoButton()).disabled(model.refreshingUsage)
        }.padding(.horizontal, 24).frame(height: 60)
    }

    var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "arrow.triangle.swap").font(.system(size: 18, weight: .medium))
                Text("Devin Switch").font(.system(size: 15, weight: .semibold))
                Spacer()
            }.padding(.horizontal, 20).padding(.top, 34).padding(.bottom, 25)
            Button { model.focus = nil } label: {
                HStack(spacing: 10) {
                    Image(systemName: "square.grid.2x2")
                    Text("All accounts")
                    Spacer()
                    Text("\(model.snapshot.accounts.count)").foregroundStyle(Palette.secondary)
                }.font(.system(size: 12, weight: .medium)).padding(11)
                    .background(model.focus == nil ? Palette.hover : .clear, in: RoundedRectangle(cornerRadius: 7))
            }.buttonStyle(.plain).padding(.horizontal, 10)
            HStack {
                Text("Accounts").font(.system(size: 11, weight: .medium))
                Spacer()
                Button { model.addError = ""; model.adding = true } label: { Image(systemName: "plus") }
                    .buttonStyle(.plain).disabled(model.blocked).accessibilityLabel("Add account")
            }.foregroundStyle(Palette.secondary).padding(.horizontal, 20).padding(.top, 26).padding(.bottom, 10)
            ScrollView {
                VStack(spacing: 3) {
                    ForEach(model.snapshot.accounts) { account in
                        Button { model.focus = account.name } label: {
                            HStack(spacing: 10) {
                                Image(systemName: "person.crop.circle").foregroundStyle(Palette.secondary)
                                Text(account.name).font(.system(size: 12)).lineLimit(1)
                                Spacer(minLength: 0)
                                if model.snapshot.selected == account.name {
                                    Circle().fill(Palette.green).frame(width: 5, height: 5)
                                        .accessibilityLabel("Default for new chats")
                                }
                            }.padding(.horizontal, 11).padding(.vertical, 10)
                                .contentShape(Rectangle())
                                .background(model.focus == account.name ? Palette.hover : .clear, in: RoundedRectangle(cornerRadius: 7))
                        }.buttonStyle(.plain).accessibilityLabel("Account \(account.name)")
                    }
                }.padding(.horizontal, 10)
            }
            VStack(alignment: .leading, spacing: 16) {
                Button { model.perform(["action": "import"]) } label: {
                    Label("Import Chrome profiles", systemImage: "square.and.arrow.down")
                }.buttonStyle(.plain).font(.system(size: 11)).disabled(model.blocked)
                    .help("Add missing profiles. Nayanshi accounts use nayanshi-* names. Sign in to each new account once.")
                HStack(spacing: 7) {
                    Image(systemName: "internaldrive")
                    Text("Local workspace")
                }.font(.system(size: 10)).foregroundStyle(Palette.secondary)
            }.padding(20)
        }.background(Palette.sidebar)
    }

    var overview: some View {
        VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Sessions & accounts").font(.system(size: 27, weight: .semibold))
                Text("Run chats across repositories and manage the account each launch uses.")
                    .font(.system(size: 13)).foregroundStyle(Palette.secondary)
            }
            HStack(spacing: 14) {
                summary("Accounts", value: model.snapshot.accounts.count, symbol: "person.2")
                summary("Open chats", value: model.openChats.count, symbol: "terminal")
                summary("Quota available", value: model.snapshot.accounts.filter { $0.usage.available }.count, symbol: "chart.bar")
            }
            OpenSessionsPanel()
            VStack(spacing: 0) {
                HStack(spacing: 22) {
                    Text("ACCOUNT").frame(maxWidth: .infinity, alignment: .leading)
                    Text("DAILY LEFT").frame(width: 124, alignment: .leading)
                    Text("WEEKLY LEFT").frame(width: 124, alignment: .leading)
                    Text("STATUS").frame(width: 118, alignment: .leading)
                    Text("ACTIONS").frame(width: AccountRowActions.width, alignment: .trailing)
                }.font(.system(size: 9, weight: .medium)).tracking(0.8)
                    .foregroundStyle(Palette.secondary).padding(.horizontal, 18).padding(.vertical, 14)
                Rectangle().fill(Palette.line).frame(height: 1)
                if model.snapshot.accounts.isEmpty {
                    Text("Import your Chrome profiles or add your first account.")
                        .font(.system(size: 13)).foregroundStyle(Palette.secondary).padding(35)
                }
                ForEach(model.snapshot.accounts) { account in
                    accountRow(account)
                        .onHover { hoveredAccount = $0 ? account.name : nil }
                    if account.id != model.snapshot.accounts.last?.id {
                        Rectangle().fill(Palette.line).frame(height: 1).padding(.horizontal, 18)
                    }
                }
            }.background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
            Text("Quota is shared by chats on the same account. Percentages show allowance remaining, not a per-session budget. Hover for quick actions, or use the actions menu.")
                .font(.system(size: 11)).foregroundStyle(Palette.secondary)
        }
    }

    func summary(_ label: String, value: Int, symbol: String) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(label).font(.system(size: 11))
                Spacer()
                Image(systemName: symbol).font(.system(size: 12))
            }.foregroundStyle(Palette.secondary)
            Text("\(value)").font(.system(size: 26, weight: .medium)).monospacedDigit()
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
    }

    func accountRow(_ account: Account) -> some View {
        HStack(spacing: 22) {
            Button { model.focus = account.name } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(account.name).font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Text(model.email(account)).font(.system(size: 10)).foregroundStyle(Palette.secondary).lineLimit(1)
                    HStack(spacing: 7) {
                        if model.snapshot.selected == account.name {
                            Text("Default").foregroundStyle(Palette.green)
                        }
                        Text("\(model.openCount(account)) open").foregroundStyle(Palette.secondary)
                    }.font(.system(size: 10))
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityLabel("Open profile \(account.name)")
            QuotaMeter(window: account.usage.daily, stale: account.usage.status == "stale", compact: true).frame(width: 124)
            QuotaMeter(window: account.usage.weekly, stale: account.usage.status == "stale", compact: true).frame(width: 124)
            StatusPill(label: account.usage.label, active: account.usage.available).frame(width: 118, alignment: .leading)
            AccountRowActions(account: account, revealed: hoveredAccount == account.name)
        }.padding(.horizontal, 18).padding(.vertical, 19).contentShape(Rectangle())
            .background(hoveredAccount == account.name ? Palette.hover.opacity(0.35) : .clear)
    }

    func detail(_ account: Account) -> some View {
        VStack(alignment: .leading, spacing: 25) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 9) {
                    Text(account.name).font(.system(size: 27, weight: .semibold)).textSelection(.enabled)
                    Text(model.email(account)).font(.system(size: 12)).foregroundStyle(Palette.secondary).textSelection(.enabled)
                    Text([account.usage.plan, account.chrome_profile].compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 10) {
                    StatusPill(label: account.usage.label, active: account.usage.available)
                    if model.snapshot.selected == account.name { StatusPill(label: "Default for new chats", active: true) }
                    Text("\(model.openCount(account)) open chats").font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
            }
            HStack(spacing: 10) {
                if account.saved_login {
                    Button(model.snapshot.selected == account.name ? "Default for new chats" : "Use for new chats") { model.useForNewChats(account) }
                        .buttonStyle(MonoButton(primary: true)).disabled(model.snapshot.selected == account.name)
                    Button("Resume chat with…") { model.chooseChat(account) }.buttonStyle(MonoButton())
                    Button("Check login") { model.act("check") }.buttonStyle(MonoButton())
                } else {
                    Button("Sign in") { model.act("login") }.buttonStyle(MonoButton(primary: true))
                    Text("One sign-in to save this account on your Mac.").font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
                Spacer()
                Button { model.removing = account } label: { Image(systemName: "trash") }
                    .buttonStyle(MonoButton()).disabled(model.inUse(account))
                    .help(model.inUse(account) ? "Close this profile’s sessions before removing it" : "Remove local profile…")
                    .accessibilityLabel("Remove local profile")
            }.disabled(model.blocked)
            OpenSessionsPanel(account: account.name)
            HStack(spacing: 16) {
                QuotaCard(title: "Daily allowance left", window: account.usage.daily, stale: account.usage.status == "stale")
                QuotaCard(title: "Weekly allowance left", window: account.usage.weekly, stale: account.usage.status == "stale")
            }
            VStack(alignment: .leading, spacing: 6) {
                if !account.usage.message.isEmpty {
                    Text(account.usage.message).font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
                if let fetched = account.usage.fetched_at {
                    Text("Last read \(Date(timeIntervalSince1970: fetched).formatted(date: .abbreviated, time: .shortened)) · Percentages show allowance remaining")
                        .font(.system(size: 10)).foregroundStyle(Palette.secondary)
                }
            }
            Rectangle().fill(Palette.line).frame(height: 1)
            VStack(alignment: .leading, spacing: 17) {
                HStack {
                    Text("Project").font(.system(size: 15, weight: .semibold))
                    Spacer()
                    Label("Opens in Terminal", systemImage: "terminal").font(.system(size: 10)).foregroundStyle(Palette.secondary)
                }
                HStack(spacing: 12) {
                    Image(systemName: "folder").font(.system(size: 21)).foregroundStyle(Palette.secondary)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(model.project.isEmpty ? "Choose a project" : URL(fileURLWithPath: model.project).lastPathComponent)
                            .font(.system(size: 12, weight: .medium))
                        Text(model.project.isEmpty ? "Select the folder you want to work in." : model.project)
                            .font(.system(size: 10)).foregroundStyle(Palette.secondary).lineLimit(2)
                    }
                    Spacer()
                    Button("Choose folder…") { model.chooseProject() }.buttonStyle(MonoButton()).disabled(model.blocked)
                }.padding(17).background(Palette.panel, in: RoundedRectangle(cornerRadius: 9))
                HStack(spacing: 10) {
                    Button { model.act("start") } label: { Label("Start new", systemImage: "plus") }
                        .buttonStyle(MonoButton(primary: true))
                    Button { model.act("resume") } label: { Label("Resume latest", systemImage: "arrow.uturn.right") }
                        .buttonStyle(MonoButton())
                    Spacer()
                    Button { model.perform(["action": "next"]) } label: { Label("Next account", systemImage: "arrow.right") }
                        .buttonStyle(MonoButton()).disabled(model.snapshot.accounts.filter(\.saved_login).count < 2)
                }.disabled(model.blocked || !account.saved_login || model.project.isEmpty)
                Text("Start new can run alongside other chats. These buttons launch with \(account.name). Use Resume chat with… to hand off a specific stopped chat.")
                    .font(.system(size: 11)).foregroundStyle(Palette.secondary)
            }
        }
    }

    var footer: some View {
        VStack(spacing: 0) {
            Rectangle().fill(Palette.line).frame(height: 1)
            HStack(spacing: 8) {
                Image(systemName: model.failed ? "exclamationmark.circle" : "terminal")
                Text(model.snapshot.busy ? "Another command holds the legacy lock. If an older session is open, exit it once to enable concurrent launches." : model.message.isEmpty ? "\(model.openChats.count) open chats · Usage refreshes every minute when Auto refresh is on" : model.message)
                    .lineLimit(2)
                Spacer()
                Text("LOCAL").font(.system(size: 9, weight: .medium)).tracking(0.7)
            }.font(.system(size: 10)).foregroundStyle(Palette.secondary).padding(.horizontal, 24).padding(.vertical, 12)
        }
    }
}

struct AddAccountView: View {
    @EnvironmentObject var model: AppModel
    @State private var name = ""
    @State private var profile = ""
    @State private var previousSuggestion = ""
    @FocusState private var nameFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("Add account").font(.system(size: 23, weight: .semibold))
            Text("Choose a Chrome profile and give this login a name.")
                .font(.system(size: 12)).foregroundStyle(Palette.secondary)
            VStack(alignment: .leading, spacing: 8) {
                Text("Chrome profile").font(.system(size: 12, weight: .medium))
                Picker("Chrome profile", selection: $profile) {
                    Text("Default browser").tag("")
                    ForEach(model.snapshot.profiles) { item in Text(item.label).tag(item.directory) }
                }.labelsHidden().frame(maxWidth: .infinity)
                    .onChange(of: profile) { _, value in
                        if let selected = model.snapshot.profiles.first(where: { $0.directory == value }) {
                            let suggestion = model.suggestedName(selected)
                            if name.isEmpty || name == previousSuggestion { name = suggestion }
                            previousSuggestion = suggestion
                        }
                    }
                if let error = model.snapshot.chrome_error {
                    Text(error).font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Account name").font(.system(size: 12, weight: .medium))
                TextField("ansuman-3 or nayanshi-1", text: $name).textFieldStyle(.roundedBorder).focused($nameFocused)
                Text("Nayanshi’s profiles use nayanshi-*. Other profiles use ansuman-*.")
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary)
            }
            if !model.addError.isEmpty {
                Label(model.addError, systemImage: "exclamationmark.circle").font(.system(size: 11))
            }
            Text("Sign in once through Terminal and Chrome. Daily and weekly usage will appear after the login is saved.")
                .font(.system(size: 12)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { model.adding = false }.buttonStyle(MonoButton()).keyboardShortcut(.cancelAction)
                Button("Add account") {
                    var request = ["action": "add", "account": name.trimmingCharacters(in: .whitespaces)]
                    if !profile.isEmpty { request["chrome_profile"] = profile }
                    model.perform(request)
                }.buttonStyle(MonoButton(primary: true)).keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || model.blocked)
            }
        }.padding(30).frame(width: 490).background(Palette.background).foregroundStyle(Palette.text)
            .preferredColorScheme(.dark).tint(.gray).onAppear { nameFocused = true }
    }
}

#if !APP_MODEL_TESTS
@main struct DevinSwitchApp: App {
    @StateObject private var model = AppModel()
    var body: some Scene {
        WindowGroup("Devin Switch") {
            ContentView().environmentObject(model)
                .onAppear { NSApplication.shared.activate(ignoringOtherApps: true) }
        }
        .defaultSize(width: 1180, height: 820)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Add Account…") { model.addError = ""; model.adding = true }
                    .keyboardShortcut("n").disabled(model.blocked)
                Button("Refresh Usage") { model.refreshUsage(force: true) }.keyboardShortcut("r")
                    .disabled(model.refreshingUsage)
            }
        }
    }
}
#endif

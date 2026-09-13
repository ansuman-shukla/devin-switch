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
    @Published var renaming: Account?
    @Published var renameError = ""
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

    func showAllAccounts() { focus = nil }

    func displayName(for alias: String) -> String {
        snapshot.accounts.first { $0.name == alias }?.label ?? alias
    }

    func beginRenaming(_ account: Account) {
        renameError = ""
        renaming = account
    }

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
                    actionFailed(reply.message ?? "Could not complete this action.", operation: operation)
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
                    if operation == "rename_display" {
                        renaming = nil
                        renameError = ""
                    }
                    if let path = reply.launcher {
                        if operation == "resume_session" { resuming = nil }
                        openTerminal(path)
                    }
                }
            } catch {
                actionFailed(error.localizedDescription, operation: operation)
            }
            working = false
            refresh()
            if operation != "rename_display" { refreshUsage() }
        }
    }

    private func actionFailed(_ text: String, operation: String) {
        if operation == "add" { addError = text }
        else if operation == "rename_display" { renameError = text }
        else { failed = true; message = text }
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

struct WorkspaceLayout {
    let width: CGFloat
    let sidebarVisible: Bool
    let preferredSidebarWidth: Double
    static let minimumSize = CGSize(width: 640, height: 520)
    static let railWidth: CGFloat = 56

    var maximumSidebarWidth: CGFloat { max(190, min(320, width - 380)) }
    var sidebarWidth: CGFloat {
        sidebarVisible ? min(maximumSidebarWidth, max(190, preferredSidebarWidth)) : Self.railWidth
    }
    var mainWidth: CGFloat { max(0, width - sidebarWidth - 1) }
    var padding: CGFloat { mainWidth < 650 ? 20 : 30 }
    var contentWidth: CGFloat { max(0, mainWidth - padding * 2) }
    var compactAccounts: Bool { contentWidth < 820 }

    static func columns(width: CGFloat, minimum: CGFloat, count: Int, spacing: CGFloat) -> [GridItem] {
        Array(repeating: GridItem(.flexible(), spacing: spacing, alignment: .leading), count: max(1, min(count, Int((width + spacing) / (minimum + spacing)))))
    }
}

struct SidebarToggle: View {
    @Binding var expanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovered = false
    @FocusState private var focused: Bool

    var body: some View {
        Button {
            withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.18)) { expanded.toggle() }
        } label: {
            Image(systemName: expanded || hovered || focused ? "sidebar.left" : "arrow.triangle.swap")
                .font(.system(size: 18, weight: .medium))
                .frame(width: 32, height: 32).contentShape(Rectangle())
        }.buttonStyle(SidebarButton()).focused($focused)
            .onHover { hovered = $0 }
            .help(expanded ? "Collapse sidebar" : "Expand sidebar")
            .accessibilityLabel(expanded ? "Collapse sidebar" : "Expand sidebar")
            .keyboardShortcut("s", modifiers: [.command, .control])
    }
}

struct SidebarResizer: NSViewRepresentable {
    let width: Double
    let maximumWidth: Double
    let onResize: (Double) -> Void

    func makeNSView(context: Context) -> ResizeHandleView {
        let view = ResizeHandleView()
        view.setAccessibilityElement(true)
        view.setAccessibilityRole(.splitter)
        view.setAccessibilityLabel("Sidebar width")
        return view
    }

    func updateNSView(_ view: ResizeHandleView, context: Context) {
        view.width = width
        view.maximumWidth = maximumWidth
        view.onResize = onResize
        view.setAccessibilityValue("\(Int(width)) points")
    }

    final class ResizeHandleView: NSView {
        var width = 224.0
        var maximumWidth = 320.0
        var onResize: (Double) -> Void = { _ in }
        private var dragOrigin: (CGFloat, Double)?

        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
        override func resetCursorRects() { addCursorRect(bounds, cursor: .resizeLeftRight) }
        override func mouseDown(with event: NSEvent) {
            if event.clickCount == 2 { resize(224); dragOrigin = nil }
            else { dragOrigin = (event.locationInWindow.x, width) }
        }
        override func mouseDragged(with event: NSEvent) {
            guard let (start, width) = dragOrigin else { return }
            resize(width + event.locationInWindow.x - start)
        }
        override func mouseUp(with event: NSEvent) { dragOrigin = nil }
        override func accessibilityPerformIncrement() -> Bool { resize(width + 20); return true }
        override func accessibilityPerformDecrement() -> Bool { resize(width - 20); return true }
        private func resize(_ value: Double) { onResize(min(maximumWidth, max(190, value))) }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var hoveredAccount: String?
    @AppStorage("sidebarVisible") private var sidebarVisible = true
    @AppStorage("sidebarWidth") private var sidebarWidth = 224.0

    var body: some View {
        GeometryReader { geometry in
            let layout = WorkspaceLayout(width: geometry.size.width, sidebarVisible: sidebarVisible, preferredSidebarWidth: sidebarWidth)
            HStack(spacing: 0) {
                sidebar.frame(width: layout.sidebarWidth).clipped()
                    .overlay(alignment: .trailing) {
                        if sidebarVisible { resizeHandle(layout) }
                    }
                Rectangle().fill(Palette.line).frame(width: 1)
                VStack(spacing: 0) {
                    toolbar(stacked: layout.mainWidth < 650)
                    Rectangle().fill(Palette.line).frame(height: 1)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 26) {
                            if let account = model.account { detail(account, width: layout.contentWidth) }
                            else { overview(layout: layout) }
                        }.padding(layout.padding).frame(maxWidth: .infinity, alignment: .topLeading)
                    }.id(model.focus)
                    footer
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: WorkspaceLayout.minimumSize.width, minHeight: WorkspaceLayout.minimumSize.height)
        .background(Palette.background).foregroundStyle(Palette.text)
        .preferredColorScheme(.dark).tint(Color.gray)
        .sheet(isPresented: $model.adding) { AddAccountView().environmentObject(model) }
        .sheet(item: $model.resuming) { account in ResumeSessionView(account: account).environmentObject(model) }
        .sheet(item: $model.renaming) { account in RenameAccountView(account: account).environmentObject(model) }
        .alert("Remove local profile?", isPresented: Binding(
            get: { model.removing != nil }, set: { if !$0 { model.removing = nil } }
        ), presenting: model.removing) { account in
            Button("Remove \(account.label)", role: .destructive) {
                model.perform(["action": "remove", "account": account.name, "confirmed": "true"])
            }
            Button("Cancel", role: .cancel) { model.removing = nil }
        } message: { account in
            Text("This removes \(account.label)’s saved login, settings and quota cache from Devin Switch. Shared chats, repository files and the Chrome profile are kept. You will need to sign in again if you add it back.")
        }
        .task { model.startRefreshing() }
    }

    func resizeHandle(_ layout: WorkspaceLayout) -> some View {
        SidebarResizer(width: layout.sidebarWidth, maximumWidth: layout.maximumSidebarWidth) { sidebarWidth = $0 }
            .frame(width: 6).frame(maxHeight: .infinity)
            .help("Drag to resize the sidebar. Double-click to reset.")
    }

    var navigation: some View {
        HStack(spacing: 9) {
            if let account = model.account {
                Button { model.showAllAccounts() } label: {
                    Label("All accounts", systemImage: "chevron.left").fixedSize()
                }.buttonStyle(.plain).help("Back to all accounts (⌘[)").accessibilityLabel("Back to all accounts")
                    .keyboardShortcut("[", modifiers: .command)
                Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(Palette.secondary)
                Text(account.label).lineLimit(1).help(account.label)
            } else {
                Image(systemName: "square.grid.2x2").foregroundStyle(Palette.secondary)
                Text("All accounts")
            }
        }.font(.system(size: 13, weight: .medium)).frame(maxWidth: .infinity, alignment: .leading)
    }

    func toolbar(stacked: Bool) -> some View {
        let layout = stacked ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12)) : AnyLayout(HStackLayout(spacing: 16))
        return layout {
            navigation
            HStack(spacing: 12) {
                if model.refreshingUsage {
                    ProgressView().controlSize(.small).help("Reading usage…").accessibilityLabel("Reading usage")
                }
                Toggle("Auto refresh", isOn: $model.autoRefreshUsage)
                    .toggleStyle(.switch).controlSize(.mini).tint(Palette.green)
                    .font(.system(size: 11)).foregroundStyle(Palette.secondary)
                    .help("Refresh usage every minute while this app is open")
                Button { model.refreshUsage(force: true) } label: {
                    Label("Refresh usage", systemImage: "arrow.clockwise")
                }.buttonStyle(MonoButton()).disabled(model.refreshingUsage)
            }.fixedSize(horizontal: true, vertical: false)
        }.padding(.horizontal, 20).padding(.vertical, 14)
    }

    var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                if sidebarVisible {
                    Image(systemName: "arrow.triangle.swap").font(.system(size: 18, weight: .medium))
                    Text("Devin Switch").font(.system(size: 14, weight: .semibold)).lineLimit(1)
                    Spacer(minLength: 0)
                }
                SidebarToggle(expanded: $sidebarVisible)
            }.padding(.horizontal, sidebarVisible ? 16 : 12).padding(.top, 18).padding(.bottom, 20)
            Button { model.showAllAccounts() } label: {
                HStack(spacing: 10) {
                    Image(systemName: "square.grid.2x2").frame(width: 16)
                    if sidebarVisible {
                        Text("All accounts")
                        Spacer(minLength: 0)
                        Text("\(model.snapshot.accounts.count)").foregroundStyle(Palette.secondary)
                    }
                }.font(.system(size: 12, weight: .medium)).padding(10)
            }.buttonStyle(SidebarButton(selected: model.focus == nil)).padding(.horizontal, 10)
                .help("All accounts").accessibilityLabel("All accounts")
            HStack {
                if sidebarVisible {
                    Text("Accounts").font(.system(size: 11, weight: .medium))
                    Spacer()
                }
                Button { model.addError = ""; model.adding = true } label: {
                    Image(systemName: "plus").frame(width: 32, height: 32).contentShape(Rectangle())
                }.buttonStyle(SidebarButton()).disabled(model.blocked).accessibilityLabel("Add account").help("Add account")
            }.foregroundStyle(Palette.secondary).padding(.horizontal, sidebarVisible ? 20 : 12).padding(.top, 14).padding(.bottom, 6)
            ScrollView {
                LazyVStack(spacing: 3) {
                    ForEach(model.snapshot.accounts) { account in
                        Button { model.focus = account.name } label: {
                            HStack(spacing: sidebarVisible ? 10 : 0) {
                                if sidebarVisible {
                                    Image(systemName: "person.crop.circle").foregroundStyle(Palette.secondary)
                                    Text(account.label).font(.system(size: 12)).lineLimit(1)
                                    Spacer(minLength: 0)
                                } else {
                                    Text(String(account.label.prefix(1)).uppercased()).font(.system(size: 12, weight: .medium))
                                        .frame(width: 16)
                                }
                                if model.snapshot.selected == account.name {
                                    Circle().fill(Palette.green).frame(width: 5, height: 5)
                                        .accessibilityLabel("Default for new chats")
                                }
                            }.padding(.horizontal, sidebarVisible ? 11 : 8).padding(.vertical, 10)
                                .frame(maxWidth: .infinity, alignment: sidebarVisible ? .leading : .center).contentShape(Rectangle())
                        }.buttonStyle(SidebarButton(selected: model.focus == account.name))
                            .accessibilityLabel("Account \(account.label)").help("\(account.label) · \(account.name)")
                            .contextMenu {
                                Button("Rename display name…") { model.beginRenaming(account) }.disabled(model.blocked)
                            }
                    }
                }.padding(.horizontal, 10)
            }
            VStack(alignment: .leading, spacing: 16) {
                Button { model.perform(["action": "import"]) } label: {
                    Label("Import Chrome profiles", systemImage: "square.and.arrow.down")
                        .labelStyle(SidebarLabelStyle(expanded: sidebarVisible))
                }.buttonStyle(SidebarButton()).font(.system(size: 11)).disabled(model.blocked)
                    .help("Import missing Chrome profiles. Sign in to each new account once.")
                    .accessibilityLabel("Import Chrome profiles")
                if sidebarVisible {
                    Label("Local workspace", systemImage: "internaldrive")
                        .font(.system(size: 10)).foregroundStyle(Palette.secondary)
                }
            }.padding(sidebarVisible ? 20 : 12)
        }.frame(maxHeight: .infinity).background(Palette.sidebar)
    }

    func overview(layout: WorkspaceLayout) -> some View {
        let compact = layout.compactAccounts
        return VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Sessions & accounts").font(.system(size: 27, weight: .semibold))
                Text("Run chats across repositories and manage the account each launch uses.")
                    .font(.system(size: 13)).foregroundStyle(Palette.secondary)
            }
            LazyVGrid(columns: WorkspaceLayout.columns(width: layout.contentWidth, minimum: 145, count: 3, spacing: 14), spacing: 14) {
                summary("Accounts", value: model.snapshot.accounts.count, symbol: "person.2")
                summary("Open chats", value: model.openChats.count, symbol: "terminal")
                summary("With quota", value: model.snapshot.accounts.filter { $0.usage.available }.count, symbol: "chart.bar")
            }
            GlobalQuotaPanel(width: layout.contentWidth)
            OpenSessionsPanel()
            VStack(spacing: 0) {
                if !compact {
                    HStack(spacing: 22) {
                        Text("ACCOUNT").frame(maxWidth: .infinity, alignment: .leading)
                        Text("DAILY LEFT").frame(width: 124, alignment: .leading)
                        Text("WEEKLY LEFT").frame(width: 124, alignment: .leading)
                        Text("STATUS").frame(width: 118, alignment: .leading)
                        Text("ACTIONS").frame(width: AccountRowActions.width, alignment: .trailing)
                    }.font(.system(size: 9, weight: .medium)).tracking(0.8)
                        .foregroundStyle(Palette.secondary).padding(.horizontal, 18).padding(.vertical, 14)
                    Rectangle().fill(Palette.line).frame(height: 1)
                }
                if model.snapshot.accounts.isEmpty {
                    VStack(spacing: 16) {
                        Text("Import your Chrome profiles or add your first account.")
                            .font(.system(size: 13)).foregroundStyle(Palette.secondary)
                        Button("Add account") { model.addError = ""; model.adding = true }
                            .buttonStyle(MonoButton(primary: true)).disabled(model.blocked)
                    }.padding(28).frame(maxWidth: .infinity)
                }
                ForEach(model.snapshot.accounts) { account in
                    Group {
                        if compact { compactAccountRow(account) }
                        else { accountRow(account) }
                    }.onHover { hoveredAccount = $0 ? account.name : nil }
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

    func accountIdentity(_ account: Account) -> some View {
        Button { model.focus = account.name } label: {
            VStack(alignment: .leading, spacing: 6) {
                Text(account.label).font(.system(size: 13, weight: .medium)).lineLimit(1).help(account.label)
                Text(model.email(account).isEmpty ? account.name : model.email(account))
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary).lineLimit(1)
                HStack(spacing: 7) {
                    if model.snapshot.selected == account.name {
                        Text("Default").foregroundStyle(Palette.green)
                    }
                    Text("\(model.openCount(account)) open").foregroundStyle(Palette.secondary)
                }.font(.system(size: 10))
            }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Open profile \(account.label)")
    }

    func compactAccountRow(_ account: Account) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                accountIdentity(account)
                AccountRowActions(account: account, revealed: true)
            }
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("DAILY LEFT").font(.system(size: 9, weight: .medium)).foregroundStyle(Palette.secondary)
                    QuotaMeter(window: account.usage.daily, stale: account.usage.status == "stale", compact: true)
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text("WEEKLY LEFT").font(.system(size: 9, weight: .medium)).foregroundStyle(Palette.secondary)
                    QuotaMeter(window: account.usage.weekly, stale: account.usage.status == "stale", compact: true)
                }
            }
            StatusPill(label: account.usage.label, active: account.usage.available)
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
    }

    func accountRow(_ account: Account) -> some View {
        HStack(spacing: 22) {
            accountIdentity(account)
            QuotaMeter(window: account.usage.daily, stale: account.usage.status == "stale", compact: true).frame(width: 124)
            QuotaMeter(window: account.usage.weekly, stale: account.usage.status == "stale", compact: true).frame(width: 124)
            StatusPill(label: account.usage.label, active: account.usage.available).frame(width: 118, alignment: .leading)
            AccountRowActions(account: account, revealed: hoveredAccount == account.name)
        }.padding(.horizontal, 18).padding(.vertical, 19).contentShape(Rectangle())
            .background(hoveredAccount == account.name ? Palette.hover.opacity(0.35) : .clear)
    }

    func detail(_ account: Account, width: CGFloat) -> some View {
        let compact = width < 540
        let headerLayout = compact ? AnyLayout(VStackLayout(alignment: .leading, spacing: 18)) : AnyLayout(HStackLayout(alignment: .top, spacing: 18))
        return VStack(alignment: .leading, spacing: 25) {
            headerLayout {
                VStack(alignment: .leading, spacing: 9) {
                    Text(account.label).font(.system(size: 27, weight: .semibold)).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("CLI alias: \(account.name)").font(.system(size: 11)).foregroundStyle(Palette.secondary).textSelection(.enabled)
                    if !model.email(account).isEmpty {
                        Text(model.email(account)).font(.system(size: 12)).foregroundStyle(Palette.secondary).textSelection(.enabled)
                    }
                    let metadata = [account.usage.plan, account.chrome_profile].compactMap { $0 }.joined(separator: " · ")
                    if !metadata.isEmpty {
                        Text(metadata).font(.system(size: 11)).foregroundStyle(Palette.secondary)
                    }
                    Button { model.beginRenaming(account) } label: {
                        Label("Rename display name", systemImage: "pencil")
                    }.buttonStyle(.plain).font(.system(size: 11)).disabled(model.blocked)
                }.frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: compact ? .leading : .trailing, spacing: 10) {
                    StatusPill(label: account.usage.label, active: account.usage.available)
                    if model.snapshot.selected == account.name { StatusPill(label: "Default for new chats", active: true) }
                    Text("\(model.openCount(account)) open chats").font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
            }
            LazyVGrid(columns: WorkspaceLayout.columns(width: width, minimum: 160, count: account.saved_login ? 4 : 2, spacing: 10), alignment: .leading, spacing: 10) {
                if account.saved_login {
                    Button(model.snapshot.selected == account.name ? "Default for new chats" : "Use for new chats") { model.useForNewChats(account) }
                        .buttonStyle(MonoButton(primary: true)).disabled(model.snapshot.selected == account.name)
                    Button("Resume chat with…") { model.chooseChat(account) }.buttonStyle(MonoButton())
                    Button("Check login") { model.act("check") }.buttonStyle(MonoButton())
                } else {
                    Button("Sign in") { model.act("login") }.buttonStyle(MonoButton(primary: true))
                }
                Button { model.removing = account } label: { Label("Remove profile", systemImage: "trash") }
                    .buttonStyle(MonoButton()).disabled(model.inUse(account))
                    .help(model.inUse(account) ? "Close this profile’s sessions before removing it" : "Remove local profile…")
                    .accessibilityLabel("Remove local profile")
            }.disabled(model.blocked)
            OpenSessionsPanel(account: account.name)
            LazyVGrid(columns: WorkspaceLayout.columns(width: width, minimum: 230, count: 2, spacing: 16), spacing: 16) {
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
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        projectLabel
                        Button("Choose folder…") { model.chooseProject() }.buttonStyle(MonoButton()).fixedSize()
                    }
                    VStack(alignment: .leading, spacing: 12) {
                        projectLabel
                        Button("Choose folder…") { model.chooseProject() }.buttonStyle(MonoButton())
                    }
                }.disabled(model.blocked).padding(17).background(Palette.panel, in: RoundedRectangle(cornerRadius: 9))
                LazyVGrid(columns: WorkspaceLayout.columns(width: width, minimum: 145, count: 3, spacing: 10), alignment: .leading, spacing: 10) {
                    Button { model.act("start") } label: { Label("Start new", systemImage: "plus") }
                        .buttonStyle(MonoButton(primary: true)).disabled(!account.saved_login || model.project.isEmpty)
                    Button { model.act("resume") } label: { Label("Resume latest", systemImage: "arrow.uturn.right") }
                        .buttonStyle(MonoButton()).disabled(!account.saved_login || model.project.isEmpty)
                    Button { model.perform(["action": "next"]) } label: { Label("Next account", systemImage: "arrow.right") }
                        .buttonStyle(MonoButton()).disabled(model.snapshot.accounts.filter(\.saved_login).count < 2)
                }.disabled(model.blocked)
                Text("Start new can run alongside other chats. These buttons launch with \(account.label). Use Resume chat with… to hand off a specific stopped chat.")
                    .font(.system(size: 11)).foregroundStyle(Palette.secondary)
            }
        }
    }

    var projectLabel: some View {
        HStack(spacing: 12) {
            Image(systemName: "folder").font(.system(size: 21)).foregroundStyle(Palette.secondary)
            VStack(alignment: .leading, spacing: 5) {
                Text(model.project.isEmpty ? "Choose a project" : URL(fileURLWithPath: model.project).lastPathComponent)
                    .font(.system(size: 12, weight: .medium)).lineLimit(1)
                Text(model.project.isEmpty ? "Select the folder you want to work in." : model.project)
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary).lineLimit(2).help(model.project)
            }.frame(maxWidth: .infinity, alignment: .leading)
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
                Text("CLI alias").font(.system(size: 12, weight: .medium))
                TextField("ansuman-3 or nayanshi-1", text: $name).textFieldStyle(.roundedBorder).focused($nameFocused)
                Text("Use 1–48 lowercase letters, digits, underscores or hyphens. You can rename the display name later.")
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary)
            }
            if !model.addError.isEmpty {
                Label(model.addError, systemImage: "exclamationmark.circle").font(.system(size: 11))
            }
            Text("Sign in once through Terminal and Chrome. Daily and weekly usage will appear after the login is saved.")
                .font(.system(size: 12)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { model.adding = false }.buttonStyle(MonoButton()).keyboardShortcut(.cancelAction).disabled(model.working)
                Button("Add account") {
                    var request = ["action": "add", "account": name.trimmingCharacters(in: .whitespaces)]
                    if !profile.isEmpty { request["chrome_profile"] = profile }
                    model.perform(request)
                }.buttonStyle(MonoButton(primary: true)).keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || model.blocked)
            }
        }.padding(30).frame(width: 490).background(Palette.background).foregroundStyle(Palette.text)
            .preferredColorScheme(.dark).tint(.gray).onAppear { nameFocused = true }
            .interactiveDismissDisabled(model.working)
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
        .windowResizability(.contentMinSize)
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

import AppKit
import SwiftUI

struct Account: Decodable, Identifiable {
    let name: String
    let chrome_profile: String?
    let saved_login: Bool
    var id: String { name }
}

struct ChromeProfile: Decodable, Identifiable {
    let directory: String
    let name: String
    let email: String
    var id: String { directory }
    var label: String { "\(name) · \(directory)\(email.isEmpty ? "" : " · \(email)")" }
}

struct Snapshot: Decodable {
    var accounts: [Account] = []
    var selected: String?
    var profiles: [ChromeProfile] = []
    var chrome_error: String?
    var busy: Bool = false
}

struct Reply: Decodable {
    let ok: Bool
    let state: Snapshot?
    let message: String?
    let launcher: String?
    let focus: String?
}

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
    guard let configURL = Bundle.main.url(forResource: "bridge", withExtension: "json") else {
        throw BridgeError.message("The app is missing its local CLI connection. Run make app again.")
    }
    let configuration = try JSONDecoder().decode(
        BridgeConfiguration.self, from: Data(contentsOf: configURL)
    )
    let process = Process()
    process.executableURL = URL(fileURLWithPath: configuration.python)
    process.arguments = ["-m", "devin_switch.desktop"]
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
    @AppStorage("projectFolder") var project = ""

    var account: Account? { snapshot.accounts.first { $0.name == focus } }
    var blocked: Bool { working || snapshot.busy }

    func refresh() {
        guard !working else { return }
        perform(["action": "state"], quiet: true)
    }

    func perform(_ payload: [String: String], quiet: Bool = false) {
        guard !working else { return }
        working = true
        let operation = payload["action"] ?? "state"
        Task {
            do {
                let reply = try await Task.detached { try callBridge(payload) }.value
                if !reply.ok {
                    if operation == "add" {
                        addError = reply.message ?? "Could not add the account."
                    } else {
                        failed = true
                        message = reply.message ?? "Could not complete this action."
                    }
                } else {
                    if let state = reply.state {
                        snapshot = state
                        if focus == nil || !state.accounts.contains(where: { $0.name == focus }) {
                            focus = state.selected ?? state.accounts.first?.name
                        }
                    }
                    if let newFocus = reply.focus { focus = newFocus }
                    if !quiet {
                        message = reply.message ?? ""
                        failed = false
                    }
                    if operation == "add" {
                        adding = false
                        addError = ""
                        focus = payload["account"]
                    }
                    if let path = reply.launcher { openTerminal(path) }
                }
            } catch {
                failed = true
                message = error.localizedDescription
            }
            working = false
            if operation != "state" { refresh() }
        }
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

let accent = Color(red: 0.20, green: 0.57, blue: 0.45)

struct StatusPill: View {
    let label: String
    let active: Bool
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(active ? accent : Color.secondary).frame(width: 6, height: 6)
            Text(label).font(.system(size: 12, weight: .medium))
        }
        .padding(.horizontal, 9).padding(.vertical, 5)
        .background((active ? accent : Color.secondary).opacity(0.10), in: Capsule())
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    private let timer = Timer.publish(every: 4, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 252)
            Divider()
            VStack(alignment: .leading, spacing: 24) {
                HStack {
                    Text("Account workspace").font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary)
                    Spacer()
                    if model.working { ProgressView().controlSize(.small) }
                    Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(.plain).help("Refresh accounts").accessibilityLabel("Refresh accounts")
                        .disabled(model.working)
                }
                if let account = model.account {
                    detail(account)
                } else {
                    Spacer()
                    VStack(spacing: 14) {
                        Image(systemName: "person.crop.circle.badge.plus").font(.system(size: 42)).foregroundStyle(accent)
                        Text("Add your first account").font(.title2.weight(.semibold))
                        Text("Pick a Chrome profile and sign in once to get started.")
                            .foregroundStyle(.secondary)
                        Button("Add account") { model.adding = true }.buttonStyle(.borderedProminent)
                    }.frame(maxWidth: .infinity)
                    Spacer()
                }
                if model.snapshot.busy {
                    notice("A CLI session is active. Exit it in Terminal before switching accounts.", symbol: "terminal", error: false)
                } else if !model.message.isEmpty {
                    notice(model.message, symbol: model.failed ? "exclamationmark.circle" : "checkmark.circle", error: model.failed)
                }
            }
            .padding(32).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 900, minHeight: 650)
        .tint(accent)
        .sheet(isPresented: $model.adding) { AddAccountView().environmentObject(model) }
        .task { model.refresh() }
        .onReceive(timer) { _ in model.refresh() }
    }

    var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "arrow.triangle.swap").font(.system(size: 23, weight: .semibold)).foregroundStyle(accent)
                Text("Devin Switch").font(.system(size: 19, weight: .semibold))
            }.padding(.horizontal, 22).padding(.top, 35).padding(.bottom, 30)
            HStack {
                Text("ACCOUNTS").font(.system(size: 11, weight: .semibold)).tracking(1.2)
                Spacer()
                Text("\(model.snapshot.accounts.count)").font(.system(size: 12, design: .monospaced))
            }.foregroundStyle(.secondary).padding(.horizontal, 24).padding(.bottom, 12)
            ScrollView {
                VStack(spacing: 6) {
                    ForEach(model.snapshot.accounts) { account in
                        Button { model.focus = account.name } label: {
                            HStack(spacing: 11) {
                                ZStack {
                                    RoundedRectangle(cornerRadius: 10).fill(accent.opacity(0.12)).frame(width: 36, height: 36)
                                    Text(String(account.name.prefix(1)).uppercased()).font(.system(size: 16, weight: .semibold)).foregroundStyle(accent)
                                }
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(account.name).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                                    Text(account.saved_login ? "Login saved" : "Sign-in needed").font(.system(size: 12)).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 0)
                                if model.snapshot.selected == account.name {
                                    Image(systemName: "checkmark.circle.fill").foregroundStyle(accent).help("Active account")
                                }
                            }
                            .padding(11).contentShape(Rectangle())
                            .background(model.focus == account.name ? Color.primary.opacity(0.06) : Color.clear, in: RoundedRectangle(cornerRadius: 12))
                        }.buttonStyle(.plain).accessibilityLabel("Account \(account.name)")
                    }
                }.padding(.horizontal, 12)
            }
            Button { model.addError = ""; model.adding = true } label: {
                Label("Add account", systemImage: "plus").frame(maxWidth: .infinity).padding(.vertical, 4)
            }.buttonStyle(.bordered).disabled(model.blocked).padding(20)
            Divider().padding(.horizontal, 20)
            HStack(spacing: 6) {
                Image(systemName: "internaldrive")
                Text("Saved on this Mac")
            }.font(.system(size: 12)).foregroundStyle(.secondary).padding(22)
        }.background(Color(nsColor: .controlBackgroundColor).opacity(0.55))
    }

    func detail(_ account: Account) -> some View {
        VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center) {
                    Text(account.name).font(.system(size: 30, weight: .semibold)).textSelection(.enabled)
                    Spacer()
                    if model.snapshot.selected == account.name { StatusPill(label: "Active", active: true) }
                }
                HStack(spacing: 8) {
                    Image(systemName: account.saved_login ? "checkmark.shield" : "person.badge.key")
                    Text(account.saved_login ? "Login saved" : "Sign in to use this account")
                    Text("·").foregroundStyle(.tertiary)
                    Text(account.chrome_profile ?? "Default browser")
                }.font(.system(size: 13)).foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    if !account.saved_login {
                        Button("Sign in") { model.act("login") }.buttonStyle(.borderedProminent)
                    } else {
                        Button(model.snapshot.selected == account.name ? "Account active" : "Use account") { model.act("select") }
                            .buttonStyle(.borderedProminent).disabled(model.snapshot.selected == account.name)
                        Button("Check login") { model.act("check") }.buttonStyle(.bordered)
                    }
                }.controlSize(.large).disabled(model.blocked)
            }
            Divider()
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text("Project").font(.system(size: 17, weight: .semibold))
                    Spacer()
                    Label("Opens in Terminal", systemImage: "terminal").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                HStack(spacing: 14) {
                    Image(systemName: "folder").font(.system(size: 26)).foregroundStyle(accent)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(model.project.isEmpty ? "Choose a project folder" : URL(fileURLWithPath: model.project).lastPathComponent)
                            .font(.system(size: 14, weight: .semibold))
                        Text(model.project.isEmpty ? "Start with the folder you want to work in." : model.project)
                            .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2).textSelection(.enabled)
                    }
                    Spacer(minLength: 10)
                    Button("Choose folder…") { model.chooseProject() }.disabled(model.blocked)
                }.padding(18).background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
                HStack(spacing: 10) {
                    Button { model.act("start") } label: {
                        Label("Start new", systemImage: "plus").frame(maxWidth: .infinity)
                    }.buttonStyle(.borderedProminent)
                    Button { model.act("resume") } label: {
                        Label("Resume latest", systemImage: "arrow.uturn.right").frame(maxWidth: .infinity)
                    }.buttonStyle(.bordered)
                }.controlSize(.large)
                    .disabled(model.blocked || !account.saved_login || model.project.isEmpty)
                Text("First time in this folder? Start new and send a message. After that, you can resume with another account.")
                    .font(.system(size: 13)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 7) {
                    Text("Ready to switch?").font(.system(size: 15, weight: .semibold))
                    Text("Choose another saved login when you need it. Remaining quota isn’t checked.")
                        .font(.system(size: 13)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Button { model.perform(["action": "next"]) } label: { Label("Next account", systemImage: "arrow.right") }
                    .controlSize(.large).disabled(model.blocked || model.snapshot.accounts.count < 2)
            }
            Spacer(minLength: 0)
        }
    }

    func notice(_ text: String, symbol: String, error: Bool) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: symbol)
            Text(text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }.font(.system(size: 13)).foregroundStyle(error ? Color.red : .secondary)
            .padding(13).background((error ? Color.red : accent).opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct AddAccountView: View {
    @EnvironmentObject var model: AppModel
    @State private var name = ""
    @State private var profile = ""
    @FocusState private var nameFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("Add an account").font(.system(size: 23, weight: .semibold))
            Text("Give it a name, then choose the Chrome profile you use to sign in.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 8) {
                Text("Account name").fontWeight(.medium)
                TextField("e.g. ansuman-3", text: $name).textFieldStyle(.roundedBorder).focused($nameFocused)
                Text("Lowercase letters, numbers, hyphens or underscores.").font(.system(size: 12)).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Chrome profile").fontWeight(.medium)
                Picker("Chrome profile", selection: $profile) {
                    Text("Default browser").tag("")
                    ForEach(model.snapshot.profiles) { item in Text(item.label).tag(item.directory) }
                }.labelsHidden().frame(maxWidth: .infinity)
                if let error = model.snapshot.chrome_error {
                    Text(error).font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            if !model.addError.isEmpty { Text(model.addError).foregroundStyle(.red).font(.system(size: 13)) }
            Text("Sign-in opens in Terminal. Complete Google sign-in and paste the returned code there once.")
                .font(.system(size: 13)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { model.adding = false }.keyboardShortcut(.cancelAction)
                Button("Add account") {
                    var request = ["action": "add", "account": name.trimmingCharacters(in: .whitespaces)]
                    if !profile.isEmpty { request["chrome_profile"] = profile }
                    model.perform(request)
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || model.blocked)
            }
        }.padding(30).frame(width: 490).controlSize(.large).tint(accent)
            .onAppear { nameFocused = true }
    }
}

@main struct DevinSwitchApp: App {
    @StateObject private var model = AppModel()
    var body: some Scene {
        WindowGroup("Devin Switch") {
            ContentView().environmentObject(model)
                .onAppear { NSApplication.shared.activate(ignoringOtherApps: true) }
        }
        .defaultSize(width: 980, height: 700)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Add Account…") { model.addError = ""; model.adding = true }
                    .keyboardShortcut("n").disabled(model.blocked)
                Button("Refresh Accounts") { model.refresh() }.keyboardShortcut("r")
            }
        }
    }
}

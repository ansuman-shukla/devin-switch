import SwiftUI

struct QuotaWindow: Decodable, Equatable {
    let used_percent: Double?
    let resets_at: Double?
    let state: String
}

struct AccountUsage: Decodable, Equatable {
    let status: String
    let daily: QuotaWindow
    let weekly: QuotaWindow
    let email: String?
    let plan: String?
    let fetched_at: Double?
    let message: String

    var exhausted: Bool { [daily, weekly].contains { ($0.used_percent ?? 0) >= 100 } }
    var available: Bool { status == "ok" && !exhausted }
    var label: String {
        if status == "sign_in" { return "Sign in needed" }
        if status == "stale" { return "Stale reading" }
        if status != "ok" { return "Unavailable" }
        if (weekly.used_percent ?? 0) >= 100 { return "Weekly limit" }
        if (daily.used_percent ?? 0) >= 100 { return "Daily limit" }
        return "Quota available"
    }
}

struct Account: Decodable, Equatable, Identifiable {
    let name: String
    let chrome_profile: String?
    let saved_login: Bool
    var usage: AccountUsage
    var display_name: String? = nil
    var id: String { name }
    var label: String { display_name.flatMap { $0.isEmpty ? nil : $0 } ?? name }
}

struct CombinedQuota: Equatable {
    private(set) var includedCount = 0
    private(set) var unavailableCount = 0
    private(set) var notApplicableCount = 0
    private var remainingSum = 0.0
    var totalCount: Int { includedCount + unavailableCount + notApplicableCount }
    var remainingPercent: Double? { includedCount == 0 ? nil : remainingSum / Double(includedCount) }

    init(accounts: [Account], window: KeyPath<AccountUsage, QuotaWindow>, now: Double = Date().timeIntervalSince1970) {
        for account in accounts {
            let usage = account.usage
            guard account.saved_login, usage.status == "ok", let fetched = usage.fetched_at,
                  fetched.isFinite, (0...120).contains(now - fetched) else {
                unavailableCount += 1
                continue
            }
            let quota = usage[keyPath: window]
            if quota.state == "not_applicable" {
                notApplicableCount += 1
                continue
            }
            let applicable = [usage.daily, usage.weekly].filter { $0.state != "not_applicable" }
            guard applicable.allSatisfy({ window in
                guard window.state == "available", let used = window.used_percent,
                      used.isFinite, (0...100).contains(used), let reset = window.resets_at else { return false }
                return reset.isFinite && reset > now
            }), let used = quota.used_percent else {
                unavailableCount += 1
                continue
            }
            includedCount += 1
            remainingSum += applicable.contains { $0.used_percent == 100 } ? 0 : 100 - used
        }
    }
}

struct ChromeProfile: Decodable, Equatable, Identifiable {
    let directory: String
    let name: String
    let email: String
    var id: String { directory }
    var label: String { "\(name) · \(directory)\(email.isEmpty ? "" : " · \(email)")" }
    var prefix: String { "\(name) \(email)".lowercased().contains("nayanshi") ? "nayanshi" : "ansuman" }
}

struct SessionRun: Decodable, Equatable, Identifiable {
    let id: String
    let account: String
    let project: String
    let kind: String
    let started_at: Double
    let session_id: String?
    let active: Bool
}

struct SavedSession: Decodable, Equatable, Identifiable {
    let id: String
    let title: String?
    let project: String
    let account: String?
    let active: Bool
    let resume_blocked: String?
    var name: String { title.flatMap { $0.isEmpty ? nil : $0 } ?? id }
}

struct Snapshot: Decodable, Equatable {
    var accounts: [Account] = []
    var selected: String?
    var profiles: [ChromeProfile] = []
    var chrome_error: String?
    var busy = false
    var runs: [SessionRun] = []
    var sessions: [SavedSession] = []
    var history_error: String?
}

struct Reply: Decodable {
    let ok: Bool
    let state: Snapshot?
    let message: String?
    let launcher: String?
    let focus: String?
}

enum Palette {
    static let background = Color(white: 0.075)
    static let sidebar = Color(white: 0.095)
    static let panel = Color(white: 0.115)
    static let hover = Color(white: 0.17)
    static let text = Color(white: 0.94)
    static let secondary = Color(white: 0.58)
    static let line = Color.white.opacity(0.09)
    static let green = Color(red: 0.35, green: 0.78, blue: 0.49)
}

struct SidebarButton: ButtonStyle {
    var selected = false
    @Environment(\.isEnabled) private var enabled
    @State private var hovered = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(selected || (hovered && enabled) ? Palette.hover : .clear, in: RoundedRectangle(cornerRadius: 7))
            .opacity(!enabled ? 0.35 : configuration.isPressed ? 0.65 : 1)
            .onHover { hovered = $0 }
    }
}

struct SidebarLabelStyle: LabelStyle {
    let expanded: Bool
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 7) {
            configuration.icon
            if expanded { configuration.title }
        }.frame(minWidth: expanded ? nil : 32, minHeight: 32).contentShape(Rectangle())
    }
}

struct MonoButton: ButtonStyle {
    var primary = false
    @Environment(\.isEnabled) private var enabled
    @State private var hovered = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 13).padding(.vertical, 9)
            .foregroundStyle(primary ? Palette.background : Palette.text)
            .background(primary ? Palette.text : Palette.hover, in: RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color.white.opacity(hovered && enabled ? 0.22 : 0)))
            .opacity(!enabled ? 0.35 : configuration.isPressed ? 0.65 : 1)
            .onHover { hovered = $0 }
    }
}

struct RowActionLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(spacing: 4) {
            configuration.icon
            configuration.title.font(.system(size: 10, weight: .medium)).lineLimit(1)
        }.frame(width: 48, height: 40).contentShape(Rectangle())
    }
}

struct AccountRowActions: View {
    @EnvironmentObject var model: AppModel
    let account: Account
    let revealed: Bool
    static let width: CGFloat = 128

    var body: some View {
        HStack(spacing: 8) {
            Group {
                Button { model.useForNewChats(account) } label: {
                    Label("Switch", systemImage: "arrow.triangle.swap")
                }.disabled(model.snapshot.selected == account.name || !account.saved_login)
                    .help(!account.saved_login ? "Switch — sign in to \(account.label) first" : model.snapshot.selected == account.name ? "Switch — \(account.label) is already the default for new chats" : "Switch — use \(account.label) for new chats; existing sessions stay unchanged")
                    .accessibilityLabel("Switch to \(account.label) for new chats")
                Button { model.chooseChat(account) } label: {
                    Label("Resume", systemImage: "arrow.uturn.right")
                }.disabled(!account.saved_login)
                    .help(account.saved_login ? "Resume — choose a stopped chat to continue with \(account.label)" : "Resume — sign in to \(account.label) first")
                    .accessibilityLabel("Resume chat with \(account.label)")
            }.labelStyle(RowActionLabelStyle()).opacity(revealed ? 1 : 0).allowsHitTesting(revealed)
            Menu {
                Button("Open profile") { model.focus = account.name }
                Button("Rename display name…") { model.beginRenaming(account) }
                Button("Use for new chats") { model.useForNewChats(account) }
                    .disabled(!account.saved_login || model.snapshot.selected == account.name)
                Button("Resume chat with…") { model.chooseChat(account) }.disabled(!account.saved_login)
                Divider()
                Button("Remove profile…", role: .destructive) { model.removing = account }
                    .disabled(model.inUse(account))
            } label: { Image(systemName: "ellipsis") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                .accessibilityLabel("Actions for \(account.label)")
        }.buttonStyle(.plain).font(.system(size: 12)).frame(width: Self.width, alignment: .trailing)
            .disabled(model.blocked)
    }
}

struct StatusPill: View {
    let label: String
    var active = false
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(active ? Palette.green : Palette.secondary).frame(width: 5, height: 5)
            Text(label).font(.system(size: 11, weight: .medium)).lineLimit(1).help(label)
        }
        .foregroundStyle(active ? Palette.green : Palette.secondary)
        .padding(.horizontal, 9).padding(.vertical, 5)
        .background((active ? Palette.green : Color.white).opacity(0.07), in: Capsule())
    }
}

func resetDescription(_ timestamp: Double?) -> String {
    guard let timestamp else { return "Reset time unavailable" }
    let seconds = timestamp - Date().timeIntervalSince1970
    if seconds <= 0 { return "Reset due · refresh usage" }
    let minutes = max(1, Int(ceil(seconds / 60)))
    if minutes >= 1440 { return "Resets in \(minutes / 1440)d \((minutes % 1440) / 60)h" }
    if minutes >= 60 { return "Resets in \(minutes / 60)h \(minutes % 60)m" }
    return "Resets in \(minutes)m"
}

func exactReset(_ timestamp: Double?) -> String {
    guard let timestamp else { return "Reset time unavailable" }
    return Date(timeIntervalSince1970: timestamp).formatted(date: .abbreviated, time: .shortened)
}

struct QuotaMeter: View {
    let window: QuotaWindow
    var stale = false
    var compact = false
    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 7 : 12) {
            if let used = window.used_percent {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("\(stale ? "≈ " : "")\((100 - used).formatted(.number.precision(.fractionLength(0...1))))%")
                        .font(.system(size: compact ? 15 : 32, weight: .medium, design: .rounded))
                        .monospacedDigit()
                    Text("left").font(.system(size: 11)).foregroundStyle(Palette.secondary)
                    Spacer(minLength: 0)
                }
                GeometryReader { geometry in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.white.opacity(0.09))
                        Capsule().fill(Color(white: stale ? 0.38 : 0.76))
                            .frame(width: geometry.size.width * min(100, max(0, 100 - used)) / 100)
                    }
                }.frame(height: compact ? 3 : 5)
                Text(resetDescription(window.resets_at))
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary)
                    .help(exactReset(window.resets_at))
            } else {
                Text(window.state == "not_applicable" ? "Not applicable" : "—")
                    .font(.system(size: compact ? 15 : 26, weight: .medium))
                Text(window.state == "not_applicable" ? "No quota window for this plan" : "No usage reading")
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

struct GlobalQuotaPanel: View {
    @EnvironmentObject var model: AppModel
    let width: CGFloat

    var body: some View {
        let now = Date().timeIntervalSince1970
        let daily = CombinedQuota(accounts: model.snapshot.accounts, window: \.daily, now: now)
        let weekly = CombinedQuota(accounts: model.snapshot.accounts, window: \.weekly, now: now)
        return VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Label("Global quota", systemImage: "chart.pie")
                    .font(.system(size: 16, weight: .semibold))
                Spacer(minLength: 0)
                if daily.unavailableCount > 0 || weekly.unavailableCount > 0 {
                    Text(daily.includedCount + weekly.includedCount == 0 ? "Needs refresh" : "Partial readings")
                        .font(.system(size: 10)).foregroundStyle(Palette.secondary)
                }
            }
            LazyVGrid(columns: WorkspaceLayout.columns(width: width - 36, minimum: 200, count: 2, spacing: 24), alignment: .leading, spacing: 22) {
                CombinedQuotaMeter(title: "Daily left", quota: daily)
                CombinedQuotaMeter(title: "Weekly left", quota: weekly)
            }
            Text("All accounts combined · Equal-weight averages, not a shared balance. Accounts at either limit count as 0% in both windows. Plan limits and reset times can differ.")
                .font(.system(size: 10)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
    }
}

struct CombinedQuotaMeter: View {
    let title: String
    let quota: CombinedQuota
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var coverage: String {
        if quota.totalCount == 0 { return "Add accounts to see quota" }
        if quota.notApplicableCount == quota.totalCount { return "No quota window for these plans" }
        return "\(quota.includedCount) of \(quota.totalCount - quota.notApplicableCount) accounts included"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(Palette.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(quota.remainingPercent.map { "\($0.formatted(.number.precision(.fractionLength(0...1))))%" } ?? "—")
                    .font(.system(size: 28, weight: .medium, design: .rounded)).monospacedDigit()
                if quota.remainingPercent != nil {
                    Text("remaining").font(.system(size: 11)).foregroundStyle(Palette.secondary)
                }
            }
            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.09))
                    Capsule().fill(Palette.green)
                        .frame(width: geometry.size.width * (quota.remainingPercent ?? 0) / 100)
                        .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: quota.remainingPercent)
                }
            }.frame(height: 4)
            VStack(alignment: .leading, spacing: 4) {
                Text(coverage)
                if quota.unavailableCount > 0 { Text("\(quota.unavailableCount) missing or stale · excluded") }
                if quota.notApplicableCount > 0 && quota.notApplicableCount < quota.totalCount {
                    Text("\(quota.notApplicableCount) not applicable · excluded")
                }
            }.font(.system(size: 10)).foregroundStyle(Palette.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading).accessibilityElement(children: .combine)
    }
}

struct OpenSessionsPanel: View {
    @EnvironmentObject var model: AppModel
    var account: String?
    var openRuns: [SessionRun] {
        model.snapshot.runs.filter { $0.active && (account == nil || $0.account == account) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Open sessions").font(.system(size: 16, weight: .semibold))
                Text("\(openRuns.filter { $0.kind == "chat" }.count)")
                    .font(.system(size: 12)).foregroundStyle(Palette.secondary)
                Spacer()
            }
            if openRuns.isEmpty {
                Text("No tracked CLI sessions are open. Start a chat from any profile.")
                    .font(.system(size: 12)).foregroundStyle(Palette.secondary)
            }
            ForEach(openRuns) { run in
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 16) {
                        sessionInfo(run).frame(minWidth: 180)
                        sessionStatus(run).frame(minWidth: 130, maxWidth: 180)
                    }
                    VStack(alignment: .leading, spacing: 14) {
                        sessionInfo(run)
                        sessionStatus(run)
                    }
                }.padding(14).background(Palette.background, in: RoundedRectangle(cornerRadius: 7))
            }
            Text("Open means the CLI process is alive, not necessarily that the agent is working. Accounts stay fixed per launch; in-terminal chat changes are not tracked.")
                .font(.system(size: 10)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
    }

    func sessionInfo(_ run: SessionRun) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "terminal").foregroundStyle(Palette.green).padding(.top, 3)
            VStack(alignment: .leading, spacing: 6) {
                Text(URL(fileURLWithPath: run.project).lastPathComponent)
                    .font(.system(size: 13, weight: .medium)).lineLimit(1)
                Text(run.project).font(.system(size: 10)).foregroundStyle(Palette.secondary)
                    .lineLimit(1).truncationMode(.middle).help(run.project)
                Text(run.session_id.map { "Launched chat: \($0)" } ?? (run.kind == "chat" ? "New chat · launch \(run.id.prefix(8))" : "\(run.kind.capitalized) · \(run.id.prefix(8))"))
                    .font(.system(size: 10)).foregroundStyle(Palette.secondary).textSelection(.enabled)
                    .lineLimit(1).truncationMode(.middle).help(run.session_id ?? run.id)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    func sessionStatus(_ run: SessionRun) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            StatusPill(label: model.displayName(for: run.account), active: true)
            Text("Opened \(Date(timeIntervalSince1970: run.started_at).formatted(date: .omitted, time: .shortened))")
                .font(.system(size: 10)).foregroundStyle(Palette.secondary)
        }
    }
}

struct RenameAccountView: View {
    @EnvironmentObject var model: AppModel
    let account: Account
    @State private var displayName: String
    @FocusState private var nameFocused: Bool

    init(account: Account) {
        self.account = account
        _displayName = State(initialValue: account.display_name ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Rename account").font(.system(size: 23, weight: .semibold))
            Text("Change how this account appears in Devin Switch. Its CLI alias, saved login and open sessions stay unchanged.")
                .font(.system(size: 12)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 8) {
                Text("Display name").font(.system(size: 12, weight: .medium))
                TextField(account.name, text: $displayName).textFieldStyle(.roundedBorder)
                    .focused($nameFocused).disabled(model.working).accessibilityLabel("Display name")
                Text("Leave blank to use \(account.name). Up to 80 characters.")
                    .font(.system(size: 11)).foregroundStyle(Palette.secondary)
            }
            if !model.renameError.isEmpty {
                Label(model.renameError, systemImage: "exclamationmark.circle")
                    .font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Reset to alias") { displayName = "" }.buttonStyle(.plain).disabled(model.working)
                Spacer()
                Button("Cancel") { model.renaming = nil }.buttonStyle(MonoButton())
                    .keyboardShortcut(.cancelAction).disabled(model.working)
                Button("Save") {
                    model.perform(["action": "rename_display", "account": account.name, "display_name": displayName])
                }.buttonStyle(MonoButton(primary: true)).keyboardShortcut(.defaultAction)
                    .disabled(model.blocked || displayName.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars.count > 80)
            }
        }.padding(28).frame(width: 460).background(Palette.background).foregroundStyle(Palette.text)
            .preferredColorScheme(.dark).tint(.gray).onAppear { nameFocused = true }
            .interactiveDismissDisabled(model.working)
    }
}

struct ResumeSessionView: View {
    @EnvironmentObject var model: AppModel
    let account: Account
    @State private var search = ""
    @State private var selected: String?
    var chats: [SavedSession] {
        model.snapshot.sessions.filter {
            search.isEmpty || "\($0.name) \($0.project) \($0.id)".localizedCaseInsensitiveContains(search)
        }
    }
    var chosen: SavedSession? { model.snapshot.sessions.first { $0.id == selected } }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Resume chat with \(model.displayName(for: account.name))").font(.system(size: 22, weight: .semibold))
                .fixedSize(horizontal: false, vertical: true)
            Text("Choose the exact saved conversation. Its original repo is used automatically; your default profile and other sessions stay unchanged.")
                .font(.system(size: 12)).foregroundStyle(Palette.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Search chats, IDs or repositories", text: $search).textFieldStyle(.roundedBorder)
            if let error = model.snapshot.history_error {
                Text(error).font(.system(size: 12)).foregroundStyle(Palette.secondary)
            }
            if model.failed && !model.message.isEmpty {
                Text(model.message).font(.system(size: 12)).foregroundStyle(Palette.secondary)
            }
            ScrollView {
                LazyVStack(spacing: 6) {
                    if chats.isEmpty {
                        Text("No matching saved chats. Send a message in a new chat first.")
                            .font(.system(size: 12)).foregroundStyle(Palette.secondary).padding(24)
                    }
                    ForEach(chats) { chat in
                        Button { selected = chat.id } label: {
                            HStack(alignment: .top, spacing: 12) {
                                Image(systemName: selected == chat.id ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(selected == chat.id ? Palette.green : Palette.secondary)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(chat.name).font(.system(size: 13, weight: .medium)).lineLimit(2)
                                    Text(chat.project).font(.system(size: 10)).foregroundStyle(Palette.secondary).lineLimit(1)
                                    Text(chat.resume_blocked ?? "\(chat.id) · Shared history")
                                        .font(.system(size: 10)).foregroundStyle(Palette.secondary).lineLimit(2)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.padding(12).contentShape(Rectangle())
                                .background(selected == chat.id ? Palette.hover : Palette.panel, in: RoundedRectangle(cornerRadius: 7))
                        }.buttonStyle(.plain).disabled(chat.resume_blocked != nil)
                    }
                }
            }.frame(minHeight: 150, idealHeight: 290, maxHeight: .infinity)
            Text("Exit open CLI sessions in the target repo before resuming. Review interrupted tool work before asking Devin to continue; this does not hot-swap a running agent.")
                .font(.system(size: 11)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Refresh") { model.refresh() }.buttonStyle(MonoButton()).disabled(model.working)
                Spacer()
                Button("Cancel") { model.resuming = nil }.buttonStyle(MonoButton()).keyboardShortcut(.cancelAction).disabled(model.working)
                Button("Resume selected chat") {
                    guard let chosen else { return }
                    model.perform(["action": "resume_session", "account": account.name, "session": chosen.id])
                }.buttonStyle(MonoButton(primary: true)).keyboardShortcut(.defaultAction)
                    .disabled(model.blocked || chosen == nil || chosen?.resume_blocked != nil)
            }
        }.padding(28).frame(minWidth: 460, idealWidth: 600, maxWidth: 740, minHeight: 460, idealHeight: 620, maxHeight: 780)
            .background(Palette.background).foregroundStyle(Palette.text)
            .preferredColorScheme(.dark).tint(.gray).onAppear { model.refresh() }
            .interactiveDismissDisabled(model.working)
    }
}

struct QuotaCard: View {
    let title: String
    let window: QuotaWindow
    let stale: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(Palette.secondary)
            QuotaMeter(window: window, stale: stale)
            if window.resets_at != nil {
                Text(exactReset(window.resets_at)).font(.system(size: 10)).foregroundStyle(Palette.secondary)
            }
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
    }
}

import SwiftUI

struct QuotaWindow: Decodable {
    let used_percent: Double?
    let resets_at: Double?
    let state: String
}

struct AccountUsage: Decodable {
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

struct Account: Decodable, Identifiable {
    let name: String
    let chrome_profile: String?
    let saved_login: Bool
    var usage: AccountUsage
    var id: String { name }
}

struct ChromeProfile: Decodable, Identifiable {
    let directory: String
    let name: String
    let email: String
    var id: String { directory }
    var label: String { "\(name) · \(directory)\(email.isEmpty ? "" : " · \(email)")" }
    var prefix: String { "\(name) \(email)".lowercased().contains("nayanshi") ? "nayanshi" : "ansuman" }
}

struct SessionRun: Decodable, Identifiable {
    let id: String
    let account: String
    let project: String
    let kind: String
    let started_at: Double
    let session_id: String?
    let active: Bool
}

struct SavedSession: Decodable, Identifiable {
    let id: String
    let title: String?
    let project: String
    let account: String?
    let active: Bool
    let resume_blocked: String?
    var name: String { title.flatMap { $0.isEmpty ? nil : $0 } ?? id }
}

struct Snapshot: Decodable {
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

struct MonoButton: ButtonStyle {
    var primary = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 13).padding(.vertical, 9)
            .foregroundStyle(primary ? Palette.background : Palette.text)
            .background(primary ? Palette.text : Palette.hover, in: RoundedRectangle(cornerRadius: 7))
            .opacity(!enabled ? 0.35 : configuration.isPressed ? 0.65 : 1)
    }
}

struct StatusPill: View {
    let label: String
    var active = false
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(active ? Palette.green : Palette.secondary).frame(width: 5, height: 5)
            Text(label).font(.system(size: 11, weight: .medium))
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
                HStack(alignment: .top, spacing: 16) {
                    Image(systemName: "terminal").foregroundStyle(Palette.green).padding(.top, 3)
                    VStack(alignment: .leading, spacing: 6) {
                        Text(URL(fileURLWithPath: run.project).lastPathComponent)
                            .font(.system(size: 13, weight: .medium))
                        Text(run.project).font(.system(size: 10)).foregroundStyle(Palette.secondary)
                            .lineLimit(1).help(run.project)
                        Text(run.session_id.map { "Launched chat: \($0)" } ?? (run.kind == "chat" ? "New chat · launch \(run.id.prefix(8))" : "\(run.kind.capitalized) · \(run.id.prefix(8))"))
                            .font(.system(size: 10)).foregroundStyle(Palette.secondary).textSelection(.enabled)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .trailing, spacing: 7) {
                        StatusPill(label: run.account, active: true)
                        Text("Opened \(Date(timeIntervalSince1970: run.started_at).formatted(date: .omitted, time: .shortened))")
                            .font(.system(size: 10)).foregroundStyle(Palette.secondary)
                    }
                }.padding(14).background(Palette.background, in: RoundedRectangle(cornerRadius: 7))
            }
            Text("Open means the CLI process is alive, not necessarily that the agent is working. Accounts stay fixed per launch; in-terminal chat changes are not tracked.")
                .font(.system(size: 10)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Palette.line))
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
            Text("Resume chat with \(account.name)").font(.system(size: 22, weight: .semibold))
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
            }.frame(height: 290)
            Text("Exit open CLI sessions in the target repo before resuming. Review interrupted tool work before asking Devin to continue; this does not hot-swap a running agent.")
                .font(.system(size: 11)).foregroundStyle(Palette.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Refresh") { model.refresh() }.buttonStyle(MonoButton()).disabled(model.working)
                Spacer()
                Button("Cancel") { model.resuming = nil }.buttonStyle(MonoButton()).keyboardShortcut(.cancelAction)
                Button("Resume selected chat") {
                    guard let chosen else { return }
                    model.perform(["action": "resume_session", "account": account.name, "session": chosen.id])
                }.buttonStyle(MonoButton(primary: true)).keyboardShortcut(.defaultAction)
                    .disabled(model.blocked || chosen == nil || chosen?.resume_blocked != nil)
            }
        }.padding(28).frame(width: 640).background(Palette.background).foregroundStyle(Palette.text)
            .preferredColorScheme(.dark).tint(.gray).onAppear { model.refresh() }
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

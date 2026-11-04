import Foundation
import GRDB
@preconcurrency import UserNotifications

/// 主动消息的本地通知：App 退到后台时，把主角"将在未来送达"的消息
/// 登记成定时通知；回到前台时取消（届时消息已在聊天里浮现）。
enum ProactiveNotifier {
    private static let identifierPrefix = "orbit.proactive."

    static func requestAuthorization() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            return true
        case .notDetermined:
            return (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        default:
            return false
        }
    }

    static func cancelPending() async {
        let center = UNUserNotificationCenter.current()
        let requests = await center.pendingNotificationRequests()
        let ids = requests.map(\.identifier).filter { $0.hasPrefix(identifierPrefix) }
        center.removePendingNotificationRequests(withIdentifiers: ids)
    }

    /// 把库里所有未送达的主角消息登记为定时通知（最多 3 条）
    static func schedulePendingMessages(db: DatabaseQueue, protagonistName: String) async {
        guard await requestAuthorization() else { return }
        await cancelPending()

        let future = (try? await db.read { db in
            try ChatMessage
                .filter(Column("role") == ChatMessage.Role.protagonist.rawValue && Column("sentAt") > Date())
                .order(Column("sentAt"))
                .limit(3)
                .fetchAll(db)
        }) ?? []

        let center = UNUserNotificationCenter.current()
        for message in future {
            guard let id = message.id else { continue }
            let interval = message.sentAt.timeIntervalSinceNow
            guard interval > 5 else { continue }
            let content = UNMutableNotificationContent()
            content.title = protagonistName
            content.body = message.content
            content.sound = .default
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: interval, repeats: false)
            try? await center.add(UNNotificationRequest(
                identifier: identifierPrefix + String(id),
                content: content, trigger: trigger))
        }
    }
}

import SwiftUI

struct RootView: View {
    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                Image(systemName: "moon.stars.circle.fill")
                    .font(.system(size: 64))
                    .foregroundStyle(.secondary)
                Text("世界尚未点火")
                    .font(.title2)
                Text("完成人设向导后，TA 的世界将开始运转。")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .navigationTitle("Orbit")
        }
    }
}

#Preview {
    RootView()
}

import SwiftUI

/// Brand tokens — mirrors the web app's GitHub-dark surface + green accent and
/// the "self-weaving knowledge constellation" identity.
enum Theme {
    static let bg       = Color(red: 0x0d/255, green: 0x11/255, blue: 0x17/255)
    static let surface  = Color(red: 0x16/255, green: 0x1b/255, blue: 0x22/255)
    static let panel    = Color(red: 0x21/255, green: 0x26/255, blue: 0x2d/255)
    static let border   = Color(red: 0x30/255, green: 0x36/255, blue: 0x3d/255)
    static let text     = Color(red: 0xe6/255, green: 0xed/255, blue: 0xf3/255)
    static let muted    = Color(red: 0x8b/255, green: 0x94/255, blue: 0x9e/255)
    static let accent   = Color(red: 0x3f/255, green: 0xb9/255, blue: 0x50/255) // green
    static let blue     = Color(red: 0x58/255, green: 0xa6/255, blue: 0xff/255)
    static let cite     = Color(red: 0x79/255, green: 0xc0/255, blue: 0xff/255)

    static func levelColor(_ l: LogLine.Level) -> Color {
        switch l {
        case .info:    return blue
        case .log:     return muted
        case .success: return accent
        case .warn:    return Color(red: 0xd2/255, green: 0x99/255, blue: 0x22/255)
        case .error:   return Color(red: 0xff/255, green: 0x7b/255, blue: 0x72/255)
        case .done:    return accent
        }
    }
}

/// Reusable styled text field on the dark surface.
struct ReptileField: View {
    let placeholder: String
    @Binding var text: String
    var secure = false

    var body: some View {
        Group {
            if secure { SecureField(placeholder, text: $text) }
            else { TextField(placeholder, text: $text) }
        }
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .padding(10)
        .background(Theme.panel)
        .foregroundColor(Theme.text)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.border))
        .cornerRadius(8)
    }
}

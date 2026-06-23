// swift-tools-version: 5.9

// Reptile — Apple-native port (iPhone / iPad).
//
// This is a Swift Playgrounds "App Playground" (.swiftpm). Open it directly in
// Swift Playgrounds on iPad/Mac, or in Xcode 15+. `import AppleProductTypes` is
// provided by the Swift Playgrounds / Xcode toolchain and is NOT available on a
// plain Linux SwiftPM — that is expected; build it on an Apple device.
//
// Why a native rewrite (not a Python/FastAPI port): iOS cannot run Python or
// Playwright/Chromium. The crawler is reimplemented on WKWebView (the system
// WebKit) — load page offscreen, let JS render, inject JS to extract content.
// Everything else (DeepSeek over URLSession, the two-stage distillation wiki,
// file-based storage) ports faithfully from the web project.

import PackageDescription
import AppleProductTypes

let package = Package(
    name: "Reptile",
    platforms: [.iOS("16.0")],
    products: [
        .iOSApplication(
            name: "Reptile",
            targets: ["AppModule"],
            bundleIdentifier: "com.reptile.knowledge",
            teamIdentifier: "",
            displayVersion: "1.0",
            bundleVersion: "1",
            appIcon: .placeholder(icon: .leaf),
            accentColor: .presetColor(.green),
            supportedDeviceFamilies: [.pad, .phone],
            supportedInterfaceOrientations: [
                .portrait,
                .landscapeRight,
                .landscapeLeft,
                .portraitUpsideDown(.when(deviceFamilies: [.pad])),
            ]
        )
    ],
    targets: [
        .executableTarget(
            name: "AppModule",
            path: "."
        )
    ]
)

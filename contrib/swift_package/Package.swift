// swift-tools-version: 5.7
// The swift-tools-version declares the minimum version of Swift required to build this package.

import PackageDescription

let package = Package(
    name: "LibtelioSwift",
    platforms: [
        .iOS(.v13),
        .macOS(.v10_15)
    ],
    products: [
        .library(
            name: "LibtelioSwift",
            targets: ["LibtelioSwift", "telioFFI"]),
        .library(
            name: "sqlite3",
            targets: ["sqlite3"]),
    ],
    targets: [
        .target(
            name: "LibtelioSwift",
            dependencies: [
                "telioFFI"
            ],
            path: "Sources"
            ),
        .binaryTarget(
            name: "telioFFI",
            url: "$XCFRAMEWORK_URL",
            checksum: "$XCFRAMEWORK_CHECKSUM"
        ),
        .binaryTarget(
            name: "sqlite3",
            url: "https://bucket.digitalarsenal.net/api/v4/projects/5585/packages/generic/sqlite_amalgamation/v3.41.2-apple-linux/apple_universal_sqlite3.zip",
            checksum: "202b3d21067bb32aabe2575a782ff5de0f705980602421fe486f1bd50e15bb55"
        )
    ]
)

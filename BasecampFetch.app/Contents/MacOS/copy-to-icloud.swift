#!/usr/bin/env swift
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("usage: copy-to-icloud <src> <dst>\n", stderr)
    exit(1)
}

let src = CommandLine.arguments[1]
let dst = NSString(CommandLine.arguments[2] as NSString).expandingTildeInPath

let fm = FileManager.default
do {
    if fm.fileExists(atPath: dst) {
        try fm.removeItem(atPath: dst)
    }
    try fm.copyItem(atPath: src, toPath: dst)
} catch {
    fputs("copy failed: \(error)\n", stderr)
    exit(1)
}

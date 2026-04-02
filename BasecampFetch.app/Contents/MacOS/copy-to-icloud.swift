#!/usr/bin/env swift
import Foundation

let src = "/Users/garza/Development-vpaa/basecamp-skill/basecamp-tasks.md"
let dst = NSString("~/Library/Mobile Documents/27N4MQEA55~pro~writer/Documents/asc/basecamp-tasks.md").expandingTildeInPath

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

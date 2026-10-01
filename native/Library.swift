import Foundation

/// Confirmed boundaries partition the original selection without sorting or dropping files.
struct ClassGrouping {
    var starts: [Bool]
    var titles: [String]
    var dates: [String?]
    var explicitDates: [Int: String] = [:]

    init(recordings: [[String: Any]], suggestions: [[String: Any]]) {
        starts = Array(repeating: false, count: recordings.count)
        titles = Array(repeating: "Class recordings", count: recordings.count)
        dates = recordings.map { $0["date"] as? String }
        if !starts.isEmpty { starts[0] = true }
        for group in suggestions {
            guard let indexes = group["indices"] as? [Int], let first = indexes.first,
                  starts.indices.contains(first) else { continue }
            starts[first] = true
            titles[first] = group["title"] as? String ?? "Class recordings"
        }
    }

    var groups: [[String: Any]] {
        var result: [[String: Any]] = []
        for i in starts.indices {
            if i == 0 || starts[i] {
                let title = titles[i].trimmingCharacters(in: .whitespacesAndNewlines)
                result.append(["indices": [i], "title": title.isEmpty ? "Class recordings" : title,
                               "confirmed": true])
            } else {
                var indexes = result[result.count - 1]["indices"] as! [Int]
                indexes.append(i); result[result.count - 1]["indices"] = indexes
            }
        }
        for i in result.indices {
            let indexes = result[i]["indices"] as! [Int]
            if let first = indexes.first {
                if let date = explicitDates[first] {
                    result[i]["date"] = date.isEmpty ? NSNull() : date as Any
                } else if let date = dates[first], indexes.allSatisfy({ dates[$0] == date }) {
                    result[i]["date"] = date
                }
            }
        }
        return result
    }

    static func validDate(_ value: String) -> Bool {
        if value.isEmpty { return true }
        guard value.range(of: "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", options: .regularExpression) != nil else { return false }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian); formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"; formatter.isLenient = false
        guard let date = formatter.date(from: value) else { return false }
        return formatter.string(from: date) == value
    }
}

enum LibraryRequestError: Error { case unavailable, malformed }

enum LibraryDisplayMode: Int {
    case recent = 0, recorded = 1, trash = 2
    var backendView: String { self == .trash ? "trash" : "active" }
    var backendSort: String { self == .recorded ? "recorded" : "recent" }
    var usesDateGroups: Bool { self == .recorded }
}

enum LibraryDisplayText {
    static func generatedTime(_ iso: String?) -> String {
        guard let iso else { return "生成时间未知" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: iso)
        if date == nil { parser.formatOptions = [.withInternetDateTime]; date = parser.date(from: iso) }
        guard let date else { return "生成时间未知" }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "yyyy-MM-dd HH:mm"; formatter.timeZone = .current
        return "生成 " + formatter.string(from: date)
    }
    static func filenames(_ values: [String]) -> String {
        guard let first = values.first else { return "录音文件名未记录" }
        return values.count == 1 ? first : "\(first) 等 \(values.count) 个文件"
    }
}

enum ReportPresentation {
    /// Remove only the recognized generated preamble, never class/source content.
    /// Edited or unfamiliar reports remain fully visible.
    static func readingBody(_ markdown: String, integrity: String) -> String? {
        guard integrity == "intact" else { return nil }
        let lines = markdown.components(separatedBy: "\n")
        guard lines.first == "# Transcript Report",
              let start = lines.indices.dropFirst().first(where: { lines[$0].hasPrefix("# ") || lines[$0].hasPrefix("## ") }) else { return nil }
        let preamble = lines[..<start]
        guard preamble.contains(where: { $0.hasPrefix("Selected files: ") }),
              preamble.contains(where: { $0.hasPrefix("Timestamps restart at zero for each source.") }),
              preamble.contains(where: { $0.hasPrefix("All stored ASR text is retained.") }) else { return nil }
        return lines[start...].joined(separator: "\n")
    }
}

/// Library requests never occupy the main thread or share the transcription pipe.
final class LibraryClient {
    func request(_ value: [String: Any], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result: Result<[String: Any], Error>
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("AudioTranscribe-library-" + UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: directory) }
            do {
                guard let root = Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeCodeRoot") as? String else {
                    throw LibraryRequestError.unavailable
                }
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                let request = directory.appendingPathComponent("request.json")
                try JSONSerialization.data(withJSONObject: value).write(to: request, options: .atomic)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: request.path)
                let process = Process(); let output = Pipe()
                process.executableURL = URL(fileURLWithPath: root).appendingPathComponent(".venv/bin/python")
                process.arguments = ["-m", "audio_transcribe", "app-library", "--request", request.path]
                process.currentDirectoryURL = URL(fileURLWithPath: root)
                var environment = ProcessInfo.processInfo.environment
                if let path = Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeSettingsPath") as? String { environment["AUDIO_TRANSCRIBE_SETTINGS"] = path }
                process.environment = environment
                process.standardInput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
                process.standardOutput = output
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard process.terminationStatus == 0,
                      let response = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw LibraryRequestError.malformed
                }
                result = .success(response)
            } catch { result = .failure(error) }
            DispatchQueue.main.async { completion(result) }
        }
    }
}

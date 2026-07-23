import Foundation
import XCTest

@testable import AudioTeeCore

final class StderrJSONLoggerTests: XCTestCase {
  func testConcurrentMessagesRemainWholeJSONLines() throws {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(
      "audiotee-logger-\(UUID().uuidString).jsonl"
    )
    XCTAssertTrue(FileManager.default.createFile(atPath: url.path, contents: nil))
    defer { try? FileManager.default.removeItem(at: url) }

    let handle = try FileHandle(forWritingTo: url)
    let logger = StderrJSONLogger(fileHandle: handle)
    DispatchQueue.concurrentPerform(iterations: 100) { index in
      logger.info("message-\(index)", context: ["index": String(index)])
    }
    try handle.close()

    let data = try Data(contentsOf: url)
    let lines = data.split(separator: 0x0A)
    XCTAssertEqual(lines.count, 100)
    for line in lines {
      XCTAssertNoThrow(try JSONSerialization.jsonObject(with: Data(line)))
    }
  }
}

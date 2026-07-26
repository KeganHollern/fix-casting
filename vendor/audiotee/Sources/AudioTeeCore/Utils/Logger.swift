import Foundation

/// Default logger implementation that writes JSON messages to stderr.
/// This is the CLI-appropriate logger; library consumers can replace it
/// via AudioTeeLogging.logger.
public class StderrJSONLogger: AudioTeeLogger {
  private let lock = NSLock()
  private let fileHandle: FileHandle
  private let dateFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [
      .withInternetDateTime,
      .withFractionalSeconds,
    ]
    return formatter
  }()

  private let jsonEncoder: JSONEncoder = {
    let encoder = JSONEncoder()
    return encoder
  }()

  public init(fileHandle: FileHandle = .standardError) {
    self.fileHandle = fileHandle
    // Configured in init because stored property initializers can't
    // reference other instance properties (self.dateFormatter).
    jsonEncoder.dateEncodingStrategy = .custom { [dateFormatter] date, encoder in
      var container = encoder.singleValueContainer()
      try container.encode(dateFormatter.string(from: date))
    }
  }

  // Write any message with the unified envelope to stderr
  public func writeMessage<T: Codable>(_ type: MessageType, data: T?) {
    let message = Message(type: type, data: data)
    lock.lock()
    defer { lock.unlock() }
    do {
      var jsonData = try jsonEncoder.encode(message)
      jsonData.append(0x0A)
      fileHandle.write(jsonData)
    } catch {
      // TODO: handle at some point
    }
  }

  // Convenience methods for different message types
  public func info(_ message: String, context: [String: String]? = nil) {
    let logData = LogData(message: message, context: context)
    writeMessage(.info, data: logData)
  }

  public func error(_ message: String, context: [String: String]? = nil) {
    let logData = LogData(message: message, context: context)
    writeMessage(.error, data: logData)
  }

  public func debug(_ message: String, context: [String: String]? = nil) {
    let logData = LogData(message: message, context: context)
    writeMessage(.debug, data: logData)
  }
}

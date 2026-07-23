import Darwin
import Foundation
import XCTest

@testable import AudioTeeCore

private final class SilentLogger: AudioTeeLogger {
  func debug(_ message: String, context: [String: String]?) {}
  func info(_ message: String, context: [String: String]?) {}
  func error(_ message: String, context: [String: String]?) {}
}

private final class HeartbeatLogger: AudioTeeLogger {
  private let lock = NSLock()
  private var storedSequences: [UInt64] = []

  var sequences: [UInt64] {
    lock.lock()
    defer { lock.unlock() }
    return storedSequences
  }

  func debug(_ message: String, context: [String: String]?) {}
  func info(_ message: String, context: [String: String]?) {}
  func error(_ message: String, context: [String: String]?) {}

  func writeMessage<T: Codable>(_ type: MessageType, data: T?) {
    guard type == .heartbeat, let heartbeat = data as? AudioHeartbeat else { return }
    lock.lock()
    storedSequences.append(heartbeat.producerSequence)
    lock.unlock()
  }
}

final class NonblockingAudioFileOutputTests: XCTestCase {
  private var originalLogger: AudioTeeLogger!

  override func setUp() {
    super.setUp()
    originalLogger = AudioTeeLogging.logger
    AudioTeeLogging.logger = SilentLogger()
  }

  override func tearDown() {
    AudioTeeLogging.logger = originalLogger
    super.tearDown()
  }

  func testWriterPreservesEveryByteInOrder() {
    let pipe = Pipe()
    var fatalMessages: [String] = []
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      onFatalError: { fatalMessages.append($0) }
    )
    output.handleMetadata(metadata())

    let chunks = [Data([1, 2, 3]), Data([4, 5]), Data(repeating: 6, count: 256)]
    for chunk in chunks {
      chunk.withUnsafeBytes { bytes in
        output.handleAudioData(bytes.baseAddress!, count: bytes.count)
      }
    }

    output.handleStreamStop()
    pipe.fileHandleForWriting.closeFile()
    let received = pipe.fileHandleForReading.readDataToEndOfFile()

    XCTAssertEqual(received, chunks.reduce(into: Data()) { $0.append($1) })
    XCTAssertTrue(fatalMessages.isEmpty)
    XCTAssertNil(output.fatalError)
  }

  func testStalledConsumerNeverBlocksProducerAndFailsOnce() {
    let pipe = Pipe()
    let fatalReported = expectation(description: "terminal overflow reported")
    let callbackLock = NSLock()
    var fatalMessages: [String] = []
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      queueSlotCount: 2,
      pollTimeoutMilliseconds: 20,
      writerStopTimeout: 0.5,
      onFatalError: { message in
        callbackLock.lock()
        fatalMessages.append(message)
        let isFirst = fatalMessages.count == 1
        callbackLock.unlock()
        if isFirst { fatalReported.fulfill() }
      }
    )
    output.handleMetadata(metadata())

    let pcm = Data(repeating: 0xA5, count: 4096)
    let started = Date()
    for _ in 0..<256 {
      pcm.withUnsafeBytes { bytes in
        output.handleAudioData(bytes.baseAddress!, count: bytes.count)
      }
    }
    let producerElapsed = Date().timeIntervalSince(started)

    wait(for: [fatalReported], timeout: 1.0)
    let stopStarted = Date()
    output.handleStreamStop()
    let stopElapsed = Date().timeIntervalSince(stopStarted)

    callbackLock.lock()
    let messages = fatalMessages
    callbackLock.unlock()
    XCTAssertLessThan(producerElapsed, 0.25)
    XCTAssertLessThan(stopElapsed, 0.75)
    XCTAssertEqual(messages.count, 1)
    XCTAssertTrue(messages[0].contains("timeline is discontinuous"))
    XCTAssertEqual(output.fatalError, messages[0])

    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testDefaultQueueSurvivesBlockedConsumerAcrossRelaunchWindow() {
    let pipe = Pipe()
    let writeFD = pipe.fileHandleForWriting.fileDescriptor
    let flags = fcntl(writeFD, F_GETFL)
    XCTAssertGreaterThanOrEqual(flags, 0)
    XCTAssertEqual(fcntl(writeFD, F_SETFL, flags | O_NONBLOCK), 0)
    let fill = Data(repeating: 0xCC, count: 4096)
    fill.withUnsafeBytes { bytes in
      while Darwin.write(writeFD, bytes.baseAddress!, bytes.count) > 0 {}
    }
    XCTAssertTrue(errno == EAGAIN || errno == EWOULDBLOCK)

    let callbackLock = NSLock()
    var fatalMessages: [String] = []
    let output = NonblockingAudioFileOutput(
      fileDescriptor: writeFD,
      chunkDuration: 0.1,
      pollTimeoutMilliseconds: 10,
      writerStopTimeout: 0.5,
      captureStallTimeout: 1.0,
      onFatalError: { message in
        callbackLock.lock()
        fatalMessages.append(message)
        callbackLock.unlock()
      }
    )
    XCTAssertGreaterThanOrEqual(output.queueSlotCount, 100)
    output.handleMetadata(metadata())

    let pcm = Data(repeating: 0xA5, count: 4096)
    let started = Date()
    for _ in 0..<35 {
      pcm.withUnsafeBytes { bytes in
        output.handleAudioData(bytes.baseAddress!, count: bytes.count)
      }
      Thread.sleep(forTimeInterval: 0.1)
    }
    XCTAssertGreaterThan(Date().timeIntervalSince(started), 3.0)

    callbackLock.lock()
    let messages = fatalMessages
    callbackLock.unlock()
    XCTAssertTrue(messages.isEmpty)
    XCTAssertNil(output.fatalError)

    output.handleStreamStop()
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testExplicitProducerDiscontinuityIsFatal() {
    let pipe = Pipe()
    let fatalReported = expectation(description: "discontinuity reported")
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      onFatalError: { _ in fatalReported.fulfill() }
    )
    output.handleMetadata(metadata())

    output.handleAudioDiscontinuity()
    wait(for: [fatalReported], timeout: 1.0)
    output.handleStreamStop()

    XCTAssertNotNil(output.fatalError)
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testMissingPCMCallbacksTerminateAStalledCapture() {
    let pipe = Pipe()
    let fatalReported = expectation(description: "capture stall reported")
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      pollTimeoutMilliseconds: 10,
      captureStallTimeout: 0.05,
      onFatalError: { _ in fatalReported.fulfill() }
    )
    output.handleMetadata(metadata())

    let firstPCM = Data([0])
    firstPCM.withUnsafeBytes { bytes in
      output.handleAudioData(bytes.baseAddress!, count: bytes.count)
    }

    wait(for: [fatalReported], timeout: 1.0)
    output.handleStreamStop()

    XCTAssertTrue(output.fatalError?.contains("producer sequence did not advance") == true)
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testWatchdogDoesNotArmBeforeFirstPCM() {
    let pipe = Pipe()
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      pollTimeoutMilliseconds: 10,
      captureStallTimeout: 0.05,
      onFatalError: { _ in XCTFail("pre-first-PCM watchdog fired") }
    )
    output.handleMetadata(metadata())

    Thread.sleep(forTimeInterval: 0.2)
    XCTAssertNil(output.fatalError)
    output.handleStreamStop()
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testZeroFilledPCMCountsAsHealthyProducerProgress() {
    let pipe = Pipe()
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      pollTimeoutMilliseconds: 10,
      captureStallTimeout: 0.2,
      onFatalError: { _ in XCTFail("silent PCM was treated as a stall") }
    )
    output.handleMetadata(metadata())
    let silence = Data(repeating: 0, count: 16)

    for _ in 0..<12 {
      silence.withUnsafeBytes { bytes in
        output.handleAudioData(bytes.baseAddress!, count: bytes.count)
      }
      Thread.sleep(forTimeInterval: 0.05)
    }

    XCTAssertNil(output.fatalError)
    output.handleStreamStop()
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testIndependentMonitorDetectsProducerStallWhileWriterIsBlocked() {
    let pipe = Pipe()
    let writeFD = pipe.fileHandleForWriting.fileDescriptor
    let flags = fcntl(writeFD, F_GETFL)
    XCTAssertGreaterThanOrEqual(flags, 0)
    XCTAssertEqual(fcntl(writeFD, F_SETFL, flags | O_NONBLOCK), 0)
    let fill = Data(repeating: 0xCC, count: 4096)
    fill.withUnsafeBytes { bytes in
      while Darwin.write(writeFD, bytes.baseAddress!, bytes.count) > 0 {}
    }
    XCTAssertTrue(errno == EAGAIN || errno == EWOULDBLOCK)

    let fatalReported = expectation(description: "independent monitor fired")
    let output = NonblockingAudioFileOutput(
      fileDescriptor: writeFD,
      chunkDuration: 0.01,
      pollTimeoutMilliseconds: 10,
      captureStallTimeout: 0.05,
      onFatalError: { _ in fatalReported.fulfill() }
    )
    output.handleMetadata(metadata())
    let pcm = Data([1])
    pcm.withUnsafeBytes { bytes in
      output.handleAudioData(bytes.baseAddress!, count: bytes.count)
    }

    wait(for: [fatalReported], timeout: 1.0)
    XCTAssertTrue(output.fatalError?.contains("producer sequence") == true)
    output.handleStreamStop()
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testDefaultStallWindowAllowsThreeLargeChunks() {
    let pipe = Pipe()
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 5.0,
      onFatalError: { _ in }
    )

    XCTAssertEqual(output.captureStallTimeout, 15.0)
    output.handleStreamStop()
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  func testHeartbeatsReportMonotonicProducerProgress() {
    let logger = HeartbeatLogger()
    AudioTeeLogging.logger = logger
    let pipe = Pipe()
    let output = NonblockingAudioFileOutput(
      fileDescriptor: pipe.fileHandleForWriting.fileDescriptor,
      chunkDuration: 0.01,
      captureStallTimeout: 2.0,
      onFatalError: { _ in XCTFail("producer unexpectedly stalled") }
    )
    output.handleMetadata(metadata())
    let pcm = Data([1])

    for _ in 0..<20 {
      pcm.withUnsafeBytes { bytes in
        output.handleAudioData(bytes.baseAddress!, count: bytes.count)
      }
      Thread.sleep(forTimeInterval: 0.1)
    }
    output.handleStreamStop()

    let sequences = logger.sequences
    XCTAssertGreaterThanOrEqual(sequences.count, 2)
    for (previous, current) in zip(sequences, sequences.dropFirst()) {
      XCTAssertLessThan(previous, current)
    }
    pipe.fileHandleForWriting.closeFile()
    pipe.fileHandleForReading.closeFile()
  }

  private func metadata() -> AudioStreamMetadata {
    return AudioStreamMetadata(
      sampleRate: 1_000,
      channelsPerFrame: 1,
      bitsPerChannel: 8,
      isFloat: false,
      captureMode: "audio",
      deviceName: nil,
      deviceUID: nil,
      encoding: "pcm_u8"
    )
  }
}

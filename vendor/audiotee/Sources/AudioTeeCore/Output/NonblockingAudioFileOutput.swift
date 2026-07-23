import Darwin
import Foundation

/// Writes PCM on a dedicated thread so the CoreAudio IO proc never waits on
/// stdout. The producer queue is bounded: overflow is a terminal timeline
/// discontinuity, not permission to discard samples and continue out of sync.
public final class NonblockingAudioFileOutput: AudioOutputHandler {
  private let fileDescriptor: Int32
  private let chunkDuration: Double
  let queueSlotCount: Int
  private let pollTimeoutMilliseconds: Int32
  private let writerStopTimeout: TimeInterval
  let captureStallTimeout: TimeInterval
  private let onFatalError: (String) -> Void

  private let fatalLock = NSLock()
  private var storedFatalError: String?

  // Written before recording starts and read by the single real-time producer.
  // AudioRecorder stops that producer before handleStreamStop mutates them.
  private var queue: RealtimeAudioQueue?
  private var writerFinished = DispatchSemaphore(value: 0)
  private var monitorFinished = DispatchSemaphore(value: 0)
  private var originalDescriptorFlags: Int32?
  private var streamStopped = false

  public init(
    fileDescriptor: Int32 = STDOUT_FILENO,
    chunkDuration: Double,
    queueSlotCount: Int? = nil,
    pollTimeoutMilliseconds: Int32 = 100,
    writerStopTimeout: TimeInterval = 2.0,
    captureStallTimeout: TimeInterval? = nil,
    onFatalError: @escaping (String) -> Void
  ) {
    precondition(chunkDuration > 0)
    precondition(queueSlotCount == nil || queueSlotCount! > 0)
    precondition(pollTimeoutMilliseconds > 0)
    precondition(writerStopTimeout > 0)
    precondition(captureStallTimeout == nil || captureStallTimeout! > 0)
    self.fileDescriptor = fileDescriptor
    self.chunkDuration = chunkDuration
    // A receiver/encoder relaunch can leave stdout without a consumer for the
    // full bounded ffmpeg TERM+KILL window. Preserve every sample for ten
    // seconds so the real-time producer never blocks or drops PCM during that
    // handoff. Python drains this uncommitted backlog before the new PTS-zero.
    self.queueSlotCount = queueSlotCount ?? max(8, Int(ceil(10.0 / chunkDuration)))
    self.pollTimeoutMilliseconds = pollTimeoutMilliseconds
    self.writerStopTimeout = writerStopTimeout
    // Startup owns the pre-first-byte timeout. Once PCM begins, tolerate at
    // least three full chunks so deliberately large chunk settings remain
    // valid while the production 100ms setting still detects a stall quickly.
    self.captureStallTimeout =
      captureStallTimeout ?? max(5.0, chunkDuration * 3.0)
    self.onFatalError = onFatalError
  }

  public var fatalError: String? {
    fatalLock.lock()
    defer { fatalLock.unlock() }
    return storedFatalError
  }

  public func handleMetadata(_ metadata: AudioStreamMetadata) {
    guard queue == nil else {
      reportFatal("Audio output received duplicate stream metadata")
      return
    }

    let bytesPerSample = Int((metadata.bitsPerChannel + 7) / 8)
    let bytesPerFrame = Int(metadata.channelsPerFrame) * bytesPerSample
    let framesPerChunk = Int(ceil(metadata.sampleRate * chunkDuration))
    let nominalChunkBytes = framesPerChunk.multipliedReportingOverflow(by: bytesPerFrame)
    guard bytesPerFrame > 0, framesPerChunk > 0, !nominalChunkBytes.overflow else {
      reportFatal("Audio output received an invalid PCM format")
      return
    }

    let doubledCapacity = nominalChunkBytes.partialValue.multipliedReportingOverflow(by: 2)
    guard !doubledCapacity.overflow else {
      reportFatal("Audio output PCM chunk capacity overflowed")
      return
    }

    let flags = fcntl(fileDescriptor, F_GETFL)
    guard flags >= 0 else {
      reportFatal(descriptorError("Could not inspect audio output descriptor"))
      return
    }
    guard fcntl(fileDescriptor, F_SETFL, flags | O_NONBLOCK) == 0 else {
      reportFatal(descriptorError("Could not make audio output nonblocking"))
      return
    }
    originalDescriptorFlags = flags

    let configuredQueue = RealtimeAudioQueue(
      slotCapacity: max(4096, doubledCapacity.partialValue),
      slotCount: queueSlotCount
    )
    queue = configuredQueue
    startWriter(for: configuredQueue)
    startProducerMonitor(for: configuredQueue)
    AudioTeeLogging.logger.writeMessage(.metadata, data: metadata)
  }

  public func handleStreamStart() {
    AudioTeeLogging.logger.writeMessage(.streamStart, data: Optional<String>.none)
  }

  public func handleAudioData(_ pointer: UnsafeRawPointer, count: Int) {
    guard let queue else { return }
    _ = queue.enqueue(pointer, count: count)
  }

  public func handleAudioDiscontinuity() {
    queue?.markDiscontinuity()
  }

  public func handleStreamStop() {
    guard !streamStopped else { return }
    streamStopped = true
    queue?.stop()

    let writerStopped =
      queue == nil
      || writerFinished.wait(timeout: .now() + writerStopTimeout) == .success
    if !writerStopped {
      reportFatal("Timed out stopping the audio output writer")
    }
    let monitorStopped =
      queue == nil
      || monitorFinished.wait(timeout: .now() + writerStopTimeout) == .success
    if !monitorStopped {
      reportFatal("Timed out stopping the audio capture monitor")
    }
    if writerStopped, monitorStopped, let originalDescriptorFlags {
      _ = fcntl(fileDescriptor, F_SETFL, originalDescriptorFlags)
    }

    AudioTeeLogging.logger.writeMessage(.streamStop, data: Optional<String>.none)
  }

  private func startWriter(for queue: RealtimeAudioQueue) {
    let thread = Thread { [self] in
      let scratch = UnsafeMutableRawPointer.allocate(
        byteCount: queue.slotCapacity,
        alignment: MemoryLayout<UInt64>.alignment
      )
      defer {
        scratch.deallocate()
        writerFinished.signal()
      }
      writerLoop(queue: queue, scratch: scratch)
    }
    thread.name = "AudioTee PCM writer"
    thread.qualityOfService = .userInitiated
    thread.start()
  }

  private func writerLoop(queue: RealtimeAudioQueue, scratch: UnsafeMutableRawPointer) {
    while true {
      if queue.isOverflowed {
        reportFatal("Audio PCM queue overflowed; capture timeline is discontinuous")
        return
      }

      if let count = queue.dequeue(into: scratch, capacity: queue.slotCapacity) {
        guard writeAll(scratch, count: count, queue: queue) else { return }
        continue
      }

      if queue.isStopped { return }
      _ = queue.waitForData(timeout: .now() + .milliseconds(Int(pollTimeoutMilliseconds)))
    }
  }

  private func startProducerMonitor(for queue: RealtimeAudioQueue) {
    let thread = Thread { [self] in
      defer { monitorFinished.signal() }
      monitorProducer(queue: queue)
    }
    thread.name = "AudioTee capture monitor"
    thread.qualityOfService = .utility
    thread.start()
  }

  private func monitorProducer(queue: RealtimeAudioQueue) {
    var previousSequence: UInt64 = 0
    var lastProgressAt: TimeInterval?
    var lastHeartbeatAt: TimeInterval = 0

    while !queue.isStopped {
      Thread.sleep(forTimeInterval: 0.25)
      if queue.isStopped { return }

      let now = ProcessInfo.processInfo.systemUptime
      let sequence = queue.producerSequence
      if sequence != previousSequence {
        previousSequence = sequence
        lastProgressAt = now
      }

      // Arm only after the first published PCM chunk. The caller's startup
      // readiness timeout is authoritative before that point.
      if sequence > 0, now - lastHeartbeatAt >= 1.0 {
        AudioTeeLogging.logger.writeMessage(
          .heartbeat,
          data: AudioHeartbeat(producerSequence: sequence)
        )
        lastHeartbeatAt = now
      }

      if let lastProgressAt, now - lastProgressAt >= captureStallTimeout {
        reportFatal(
          "Audio capture stalled; producer sequence did not advance for "
            + String(format: "%.1fs", captureStallTimeout)
        )
        return
      }
    }
  }

  private func writeAll(
    _ pointer: UnsafeRawPointer, count: Int, queue: RealtimeAudioQueue
  ) -> Bool {
    var written = 0
    while written < count {
      let result = Darwin.write(
        fileDescriptor,
        pointer.advanced(by: written),
        count - written
      )
      if result > 0 {
        written += result
        continue
      }
      if result == -1, errno == EINTR { continue }

      if result == -1, errno == EAGAIN || errno == EWOULDBLOCK {
        if queue.isOverflowed {
          reportFatal("Audio PCM queue overflowed; capture timeline is discontinuous")
          return false
        }
        // Shutdown must stay bounded even when the consumer stopped reading.
        if queue.isStopped { return false }

        var descriptor = pollfd(fd: fileDescriptor, events: Int16(POLLOUT), revents: 0)
        let pollResult = Darwin.poll(&descriptor, 1, pollTimeoutMilliseconds)
        if pollResult > 0 {
          let terminalEvents = Int16(POLLERR | POLLHUP | POLLNVAL)
          if descriptor.revents & terminalEvents != 0 {
            reportFatal("Audio output consumer disconnected")
            return false
          }
          continue
        }
        if pollResult == 0 || errno == EINTR { continue }
        reportFatal(descriptorError("Audio output poll failed"))
        return false
      }

      reportFatal(descriptorError("Audio output write failed"))
      return false
    }
    return true
  }

  private func reportFatal(_ message: String) {
    fatalLock.lock()
    let isFirst = storedFatalError == nil
    if isFirst { storedFatalError = message }
    fatalLock.unlock()
    if isFirst { onFatalError(message) }
  }

  private func descriptorError(_ prefix: String) -> String {
    return "\(prefix): \(String(cString: strerror(errno)))"
  }
}

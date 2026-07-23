import CAudioTeeAtomics
import Foundation

/// Preallocated single-producer/single-consumer PCM queue.
///
/// The CoreAudio producer performs one memcpy, release-publishes a sequence,
/// and signals a semaphore. It never allocates, logs, waits, or writes to a
/// file descriptor. Overflow is terminal because dropping PCM would shorten
/// the sample-count timeline and permanently desynchronize audio from video.
public final class RealtimeAudioQueue {
  private let storage: UnsafeMutableRawPointer
  private let sizes: UnsafeMutablePointer<Int>
  private let state: OpaquePointer
  private let ready = DispatchSemaphore(value: 0)

  public let slotCapacity: Int
  public let slotCount: Int

  public init(slotCapacity: Int, slotCount: Int) {
    precondition(slotCapacity > 0)
    precondition(slotCount > 0)
    guard let state = ATQueueStateCreate() else {
      fatalError("Could not allocate audio queue atomics")
    }
    self.state = state
    self.slotCapacity = slotCapacity
    self.slotCount = slotCount
    self.storage = UnsafeMutableRawPointer.allocate(
      byteCount: slotCapacity * slotCount,
      alignment: MemoryLayout<UInt64>.alignment
    )
    self.sizes = UnsafeMutablePointer<Int>.allocate(capacity: slotCount)
    self.sizes.initialize(repeating: 0, count: slotCount)
  }

  deinit {
    ATQueueStateDestroy(state)
    sizes.deinitialize(count: slotCount)
    sizes.deallocate()
    storage.deallocate()
  }

  @discardableResult
  public func enqueue(_ pointer: UnsafeRawPointer, count: Int) -> Bool {
    if isStopped {
      return false
    }
    guard count > 0, count <= slotCapacity, !isOverflowed else {
      ATQueueMarkOverflow(state)
      ready.signal()
      return false
    }
    let writeSequence = ATQueueLoadWriteAcquire(state)
    let readSequence = ATQueueLoadReadAcquire(state)
    guard writeSequence - readSequence < UInt64(slotCount) else {
      ATQueueMarkOverflow(state)
      ready.signal()
      return false
    }

    let index = Int(writeSequence % UInt64(slotCount))
    storage.advanced(by: index * slotCapacity).copyMemory(from: pointer, byteCount: count)
    sizes[index] = count
    ATQueuePublishWrite(state, writeSequence + 1)
    ready.signal()
    return true
  }

  public func dequeue(into destination: UnsafeMutableRawPointer, capacity: Int) -> Int? {
    let readSequence = ATQueueLoadReadAcquire(state)
    let writeSequence = ATQueueLoadWriteAcquire(state)
    guard readSequence < writeSequence else { return nil }

    let index = Int(readSequence % UInt64(slotCount))
    let count = sizes[index]
    guard count > 0, count <= capacity else {
      ATQueueMarkOverflow(state)
      ready.signal()
      return nil
    }
    destination.copyMemory(
      from: storage.advanced(by: index * slotCapacity),
      byteCount: count
    )
    ATQueuePublishRead(state, readSequence + 1)
    return count
  }

  public var isOverflowed: Bool { ATQueueIsOverflowed(state) }
  public var isStopped: Bool { ATQueueIsStopped(state) }
  public var producerSequence: UInt64 { ATQueueLoadWriteAcquire(state) }

  public func waitForData(timeout: DispatchTime) -> DispatchTimeoutResult {
    return ready.wait(timeout: timeout)
  }

  public func stop() {
    ATQueueMarkStopped(state)
    ready.signal()
  }

  /// Marks a producer-side discontinuity that happened before enqueueing.
  /// The writer treats this exactly like queue overflow: terminal, never a
  /// recoverable dropped chunk.
  public func markDiscontinuity() {
    ATQueueMarkOverflow(state)
    ready.signal()
  }
}

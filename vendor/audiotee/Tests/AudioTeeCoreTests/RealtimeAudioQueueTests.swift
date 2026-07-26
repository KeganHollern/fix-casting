import Foundation
import XCTest

@testable import AudioTeeCore

final class RealtimeAudioQueueTests: XCTestCase {
  func testPreservesPCMOrder() {
    let queue = RealtimeAudioQueue(slotCapacity: 8, slotCount: 2)
    let first = Data([1, 2, 3])
    let second = Data([4, 5])

    XCTAssertTrue(enqueue(first, into: queue))
    XCTAssertTrue(enqueue(second, into: queue))
    XCTAssertEqual(dequeue(from: queue), first)
    XCTAssertEqual(dequeue(from: queue), second)
    XCTAssertNil(dequeue(from: queue))
  }

  func testOverflowIsTerminalInsteadOfDroppingPCM() {
    let queue = RealtimeAudioQueue(slotCapacity: 4, slotCount: 2)
    XCTAssertTrue(enqueue(Data([1]), into: queue))
    XCTAssertTrue(enqueue(Data([2]), into: queue))

    XCTAssertFalse(enqueue(Data([3]), into: queue))
    XCTAssertTrue(queue.isOverflowed)
    XCTAssertFalse(enqueue(Data([4]), into: queue))

    XCTAssertEqual(dequeue(from: queue), Data([1]))
    XCTAssertEqual(dequeue(from: queue), Data([2]))
  }

  func testGracefulStopDoesNotMasqueradeAsOverflow() {
    let queue = RealtimeAudioQueue(slotCapacity: 4, slotCount: 1)
    queue.stop()

    XCTAssertFalse(enqueue(Data([1]), into: queue))
    XCTAssertTrue(queue.isStopped)
    XCTAssertFalse(queue.isOverflowed)
  }

  private func enqueue(_ data: Data, into queue: RealtimeAudioQueue) -> Bool {
    return data.withUnsafeBytes { bytes in
      queue.enqueue(bytes.baseAddress!, count: bytes.count)
    }
  }

  private func dequeue(from queue: RealtimeAudioQueue) -> Data? {
    let scratch = UnsafeMutableRawPointer.allocate(byteCount: queue.slotCapacity, alignment: 8)
    defer { scratch.deallocate() }
    guard let count = queue.dequeue(into: scratch, capacity: queue.slotCapacity) else {
      return nil
    }
    return Data(bytes: scratch, count: count)
  }
}

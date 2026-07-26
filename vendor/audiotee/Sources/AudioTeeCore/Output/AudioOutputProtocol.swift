import Foundation

/// Protocol for handling audio output in different formats
public protocol AudioOutputHandler {
  /// Called with a pointer to raw PCM audio data. The pointer is only
  /// valid for the duration of this call.
  func handleAudioData(_ pointer: UnsafeRawPointer, count: Int)
  /// Called from the real-time producer when source PCM could not be retained.
  /// Implementations must not block, allocate, or log from this callback.
  func handleAudioDiscontinuity()
  func handleMetadata(_ metadata: AudioStreamMetadata)
  func handleStreamStart()
  func handleStreamStop()
}

extension AudioOutputHandler {
  public func handleAudioDiscontinuity() {}
}

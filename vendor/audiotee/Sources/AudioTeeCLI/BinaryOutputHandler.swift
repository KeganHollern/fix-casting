import AudioTeeCore
import Foundation

/// CLI-specific output handler that writes raw PCM audio to stdout
/// and lifecycle messages to stderr via the logger.
class BinaryAudioOutputHandler: AudioOutputHandler {
  private let output: NonblockingAudioFileOutput

  init(chunkDuration: Double, onFatalError: @escaping (String) -> Void) {
    output = NonblockingAudioFileOutput(
      chunkDuration: chunkDuration,
      onFatalError: onFatalError
    )
  }

  var fatalError: String? { output.fatalError }

  func handleAudioData(_ pointer: UnsafeRawPointer, count: Int) {
    output.handleAudioData(pointer, count: count)
  }

  func handleAudioDiscontinuity() {
    output.handleAudioDiscontinuity()
  }

  func handleMetadata(_ metadata: AudioStreamMetadata) {
    output.handleMetadata(metadata)
  }

  func handleStreamStart() {
    output.handleStreamStart()
  }

  func handleStreamStop() {
    output.handleStreamStop()
  }
}

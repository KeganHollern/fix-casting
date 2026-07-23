import Foundation

public struct AudioStreamMetadata: Codable {
  public static let currentProtocolVersion = 2

  public let protocolVersion: Int
  public let sampleRate: Double
  public let channelsPerFrame: UInt32
  public let bitsPerChannel: UInt32
  public let isFloat: Bool
  public let captureMode: String
  public let deviceName: String?
  public let deviceUID: String?
  public let encoding: String

  public enum CodingKeys: String, CodingKey {
    case protocolVersion = "protocol_version"
    case sampleRate = "sample_rate"
    case channelsPerFrame = "channels_per_frame"
    case bitsPerChannel = "bits_per_channel"
    case isFloat = "is_float"
    case captureMode = "capture_mode"
    case deviceName = "device_name"
    case deviceUID = "device_uid"
    case encoding
  }

  public init(
    sampleRate: Double, channelsPerFrame: UInt32, bitsPerChannel: UInt32, isFloat: Bool,
    captureMode: String, deviceName: String?, deviceUID: String?, encoding: String,
    protocolVersion: Int = AudioStreamMetadata.currentProtocolVersion
  ) {
    self.protocolVersion = protocolVersion
    self.sampleRate = sampleRate
    self.channelsPerFrame = channelsPerFrame
    self.bitsPerChannel = bitsPerChannel
    self.isFloat = isFloat
    self.captureMode = captureMode
    self.deviceName = deviceName
    self.deviceUID = deviceUID
    self.encoding = encoding
  }
}

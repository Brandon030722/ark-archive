// Local screenshot OCR. No network, persistence or model credentials.
import Foundation
import Vision
import ImageIO

do {
    guard CommandLine.arguments.count == 2 else { throw NSError(domain: "input", code: 1) }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          CGImageSourceGetCount(source) == 1,
          let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = props[kCGImagePropertyPixelWidth] as? Int,
          let height = props[kCGImagePropertyPixelHeight] as? Int,
          width > 0, height > 0, width <= 16000, height <= 16000,
          width * height <= 40000000 else {
        throw NSError(domain: "image", code: 2)
    }
    // Bound decoded OCR work; apply EXIF rotation before recognition.
    let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceThumbnailMaxPixelSize: 2600]
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
        throw NSError(domain: "image", code: 3)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image).perform([request])
    let rows = (request.results ?? []).compactMap { result -> [String: Any]? in
        guard let best = result.topCandidates(1).first else { return nil }
        return ["text": best.string, "confidence": best.confidence]
    }
    let data = try JSONSerialization.data(withJSONObject: rows)
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("Cannot recognize this image.\n".utf8))
    exit(1)
}

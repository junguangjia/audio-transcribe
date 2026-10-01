// Convert the owner's approved opaque artwork into a transparent icon master.
// Only the black exterior connected to each scanline edge is removed. The
// original asset and every interior artwork pixel remain unchanged.
import AppKit
import Foundation

guard CommandLine.arguments.count == 3 else {
    fatalError("Usage: prepare-icon.swift approved-icon.png AppIcon-master.png")
}
let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
guard let source = NSBitmapImageRep(data: try Data(contentsOf: input)) else {
    fatalError("Approved icon is not a readable PNG")
}
let width = source.pixelsWide
let height = source.pixelsHigh
guard width == height, width >= 1024 else {
    fatalError("Approved icon must be a square high-resolution image")
}
guard source.bitsPerSample == 8, source.bitsPerPixel == 32,
      source.samplesPerPixel == 3, !source.hasAlpha,
      source.bytesPerRow == width * 4,
      let sourceBytes = source.bitmapData else {
    fatalError("Expected the approved opaque RGBA-byte PNG")
}
guard let destination = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else {
    fatalError("Could not create RGBA icon master")
}
guard destination.bytesPerRow >= width * 4,
      let destinationBytes = destination.bitmapData else {
    fatalError("Could not access icon master pixels")
}
let threshold: UInt8 = 7
for y in 0..<height {
    var left = width
    var right = -1
    for x in 0..<width {
        let pixel = sourceBytes.advanced(by: y * source.bytesPerRow + x * 4)
        if max(pixel[0], pixel[1], pixel[2]) > threshold {
            left = min(left, x)
            right = x
        }
    }
    for x in 0..<width {
        let pixel = destinationBytes.advanced(by: y * destination.bytesPerRow + x * 4)
        if x < left || x > right {
            pixel[0] = 0
            pixel[1] = 0
            pixel[2] = 0
            pixel[3] = 0
        } else {
            let original = sourceBytes.advanced(by: y * source.bytesPerRow + x * 4)
            pixel[0] = original[0]
            pixel[1] = original[1]
            pixel[2] = original[2]
            pixel[3] = 255
        }
    }
}
guard let png = destination.representation(using: .png, properties: [:]) else {
    fatalError("Could not encode RGBA icon master")
}
try png.write(to: output, options: .atomic)

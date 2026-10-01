import Cocoa

// Public default artwork is drawn locally; it contains no personal image asset.
guard CommandLine.arguments.count == 2 else { fatalError("Usage: make-icon.swift OUTPUT.png") }
let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
let background = NSBezierPath(roundedRect: NSRect(x: 32, y: 32, width: 960, height: 960),
                             xRadius: 220, yRadius: 220)
NSGradient(starting: NSColor(srgbRed: 0.08, green: 0.35, blue: 0.90, alpha: 1),
           ending: NSColor(srgbRed: 0.12, green: 0.65, blue: 0.98, alpha: 1))!
    .draw(in: background, angle: 75)
NSColor.white.setFill()
for (index, height) in [160, 300, 490, 650, 430, 260, 120].enumerated() {
    NSBezierPath(roundedRect: NSRect(x: 224 + index * 88, y: (1024 - height) / 2,
                                   width: 48, height: height), xRadius: 24, yRadius: 24).fill()
}
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!
    .write(to: URL(fileURLWithPath: CommandLine.arguments[1]))

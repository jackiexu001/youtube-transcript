import AppKit
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: MakeAppIcon output.png output.icns\n", stderr)
    exit(2)
}

let size = NSSize(width: 1024, height: 1024)
let image = NSImage(size: size)
image.lockFocus()
guard let context = NSGraphicsContext.current?.cgContext else { exit(3) }
context.setAllowsAntialiasing(true)
context.setShouldAntialias(true)

// Soft, neutral app tile. macOS applies the final icon mask.
let tile = NSBezierPath(roundedRect: NSRect(x: 52, y: 52, width: 920, height: 920), xRadius: 214, yRadius: 214)
NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.20)
shadow.shadowBlurRadius = 34
shadow.shadowOffset = NSSize(width: 0, height: -16)
shadow.set()
NSColor(calibratedRed: 0.88, green: 0.40, blue: 0.28, alpha: 1).setFill()
tile.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
tile.addClip()
let background = NSGradient(colors: [
    NSColor(calibratedRed: 0.92, green: 0.46, blue: 0.33, alpha: 1),
    NSColor(calibratedRed: 0.82, green: 0.34, blue: 0.23, alpha: 1),
])!
background.draw(in: tile, angle: -68)
NSGraphicsContext.restoreGraphicsState()

let play = NSBezierPath()
play.move(to: NSPoint(x: 372, y: 322))
play.line(to: NSPoint(x: 372, y: 702))
play.line(to: NSPoint(x: 690, y: 512))
play.close()
NSColor.white.setFill()
play.fill()

image.unlockFocus()
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else { exit(4) }
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]), options: .atomic)

func pngRepresentation(of source: NSImage, pixels: Int) -> Data? {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pixels,
        pixelsHigh: pixels,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else { return nil }
    bitmap.size = NSSize(width: pixels, height: pixels)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSGraphicsContext.current?.imageInterpolation = .high
    source.draw(
        in: NSRect(x: 0, y: 0, width: pixels, height: pixels),
        from: NSRect(origin: .zero, size: source.size),
        operation: .copy,
        fraction: 1
    )
    NSGraphicsContext.restoreGraphicsState()
    return bitmap.representation(using: .png, properties: [:])
}

func bigEndianBytes(_ value: UInt32) -> [UInt8] {
    let encoded = value.bigEndian
    return withUnsafeBytes(of: encoded) { Array($0) }
}

let iconEntries: [(String, Int)] = [
    ("icp4", 16),
    ("icp5", 32),
    ("icp6", 64),
    ("ic07", 128),
    ("ic08", 256),
    ("ic09", 512),
    ("ic10", 1024),
]
var payload = Data()
for (type, pixels) in iconEntries {
    guard let typeData = type.data(using: .ascii),
          let iconData = pngRepresentation(of: image, pixels: pixels) else { exit(5) }
    payload.append(typeData)
    payload.append(contentsOf: bigEndianBytes(UInt32(iconData.count + 8)))
    payload.append(iconData)
}
var icns = Data("icns".utf8)
icns.append(contentsOf: bigEndianBytes(UInt32(payload.count + 8)))
icns.append(payload)
try icns.write(to: URL(fileURLWithPath: CommandLine.arguments[2]), options: .atomic)

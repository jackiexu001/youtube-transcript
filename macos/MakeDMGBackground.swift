import AppKit
import Foundation

guard CommandLine.arguments.count == 2 else {
    fputs("usage: MakeDMGBackground output.png\n", stderr)
    exit(2)
}

let size = NSSize(width: 920, height: 440)
let image = NSImage(size: size)
image.lockFocus()

let bounds = NSRect(origin: .zero, size: size)
NSGradient(colors: [
    NSColor(calibratedRed: 0.035, green: 0.075, blue: 0.17, alpha: 1),
    NSColor(calibratedRed: 0.055, green: 0.18, blue: 0.34, alpha: 1),
])!.draw(in: bounds, angle: -18)

NSColor.white.withAlphaComponent(0.055).setFill()
for x in stride(from: 18.0, through: 710.0, by: 28.0) {
    for y in stride(from: 18.0, through: 430.0, by: 28.0) {
        NSBezierPath(ovalIn: NSRect(x: x, y: y, width: 2, height: 2)).fill()
    }
}

let glow = NSGradient(colors: [
    NSColor(calibratedRed: 0.16, green: 0.55, blue: 1, alpha: 0.20),
    NSColor.clear,
])!
glow.draw(in: NSBezierPath(ovalIn: NSRect(x: 180, y: 70, width: 560, height: 310)), relativeCenterPosition: .zero)

func drawCentered(_ text: String, y: CGFloat, font: NSFont, color: NSColor) {
    let style = NSMutableParagraphStyle()
    style.alignment = .center
    let attributes: [NSAttributedString.Key: Any] = [
        .font: font,
        .foregroundColor: color,
        .paragraphStyle: style,
    ]
    NSAttributedString(string: text, attributes: attributes)
        .draw(in: NSRect(x: 30, y: y, width: size.width - 60, height: 40))
}

drawCentered("安装 YouTube Transcript", y: 366, font: .systemFont(ofSize: 26, weight: .bold), color: .white)
drawCentered("把应用拖到“应用程序”文件夹", y: 334, font: .systemFont(ofSize: 15, weight: .medium), color: NSColor.white.withAlphaComponent(0.78))

let arrow = NSBezierPath()
arrow.lineWidth = 8
arrow.lineCapStyle = .round
arrow.lineJoinStyle = .round
arrow.move(to: NSPoint(x: 390, y: 215))
arrow.line(to: NSPoint(x: 530, y: 215))
arrow.move(to: NSPoint(x: 500, y: 244))
arrow.line(to: NSPoint(x: 530, y: 215))
arrow.line(to: NSPoint(x: 500, y: 186))
NSColor(calibratedRed: 0.25, green: 0.72, blue: 1, alpha: 0.9).setStroke()
arrow.stroke()

drawCentered("安装完成后，可推出此磁盘并删除下载的 DMG 文件", y: 40, font: .systemFont(ofSize: 13, weight: .regular), color: NSColor.white.withAlphaComponent(0.67))

image.unlockFocus()
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else {
    fputs("failed to render background\n", stderr)
    exit(1)
}
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))

import AppKit

let destination = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)

func drawIcon(pixels: Int, at url: URL) throws {
    let image = NSImage(size: NSSize(width: pixels, height: pixels))
    image.lockFocus()
    let scale = CGFloat(pixels) / 1024
    let transform = NSAffineTransform()
    transform.scale(by: scale)
    transform.concat()
    let background = NSBezierPath(roundedRect: NSRect(x: 45, y: 45, width: 934, height: 934), xRadius: 210, yRadius: 210)
    NSGradient(colors: [
        NSColor(srgbRed: 0.09, green: 0.19, blue: 0.17, alpha: 1),
        NSColor(srgbRed: 0.17, green: 0.40, blue: 0.33, alpha: 1)
    ])!.draw(in: background, angle: 65)
    let symbol = NSImage(systemSymbolName: "arrow.triangle.swap", accessibilityDescription: nil)!
        .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 490, weight: .semibold))!
    let colored = NSImage(size: symbol.size)
    colored.lockFocus()
    NSColor(srgbRed: 0.69, green: 0.91, blue: 0.78, alpha: 1).setFill()
    NSRect(origin: .zero, size: symbol.size).fill()
    symbol.draw(at: .zero, from: .zero, operation: .destinationIn, fraction: 1)
    colored.unlockFocus()
    let ratio = min(480 / symbol.size.width, 540 / symbol.size.height)
    let width = symbol.size.width * ratio
    let height = symbol.size.height * ratio
    colored.draw(in: NSRect(x: (1024-width)/2, y: (1024-height)/2, width: width, height: height))
    image.unlockFocus()
    let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
    try bitmap.representation(using: .png, properties: [:])!.write(to: url)
}

for size in [16, 32, 128, 256, 512] {
    try drawIcon(pixels: size, at: destination.appendingPathComponent("icon_\(size)x\(size).png"))
    try drawIcon(pixels: size*2, at: destination.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}

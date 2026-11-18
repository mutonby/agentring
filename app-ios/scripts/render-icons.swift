// Renders the app icons from the website mark (same geometry as the Android
// ic_launcher_foreground.xml: a ring and two arcs on a 64-unit grid, turned -30°).
// Usage: swift app-ios/scripts/render-icons.swift <Assets.xcassets dir>
import AppKit
import CoreGraphics

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "."
let coral = CGColor(red: 0xE8 / 255.0, green: 0x59 / 255.0, blue: 0x0C / 255.0, alpha: 1) // launcher_background

func render(size: Int, background: CGColor?, mark: CGColor, scale markScale: CGFloat) -> Data {
    let cs = CGColorSpace(name: CGColorSpace.sRGB)!
    let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                        bitmapInfo: background == nil ? CGImageAlphaInfo.premultipliedLast.rawValue : CGImageAlphaInfo.noneSkipLast.rawValue)!
    let s = CGFloat(size)
    if let background {
        ctx.setFillColor(background)
        ctx.fill(CGRect(x: 0, y: 0, width: s, height: s))
    }
    // 64-unit grid centered, scaled so the mark spans `markScale` of the canvas.
    let unit = s * markScale / 64
    ctx.translateBy(x: s / 2, y: s / 2)
    ctx.scaleBy(x: unit, y: -unit) // y down, like the vector
    ctx.rotate(by: -30 * .pi / 180)
    ctx.translateBy(x: -32, y: -32)
    ctx.setStrokeColor(mark)
    ctx.setLineCap(.round)
    ctx.setLineWidth(8)
    ctx.strokeEllipse(in: CGRect(x: 21, y: 21, width: 22, height: 22))
    ctx.setLineWidth(6)
    // Arcs of radius 21.5 around the center, ±40° on the right and left.
    let r: CGFloat = 21.5, a: CGFloat = 40 * .pi / 180
    ctx.addArc(center: CGPoint(x: 32, y: 32), radius: r, startAngle: -a, endAngle: a, clockwise: false)
    ctx.strokePath()
    ctx.addArc(center: CGPoint(x: 32, y: 32), radius: r, startAngle: .pi - a, endAngle: .pi + a, clockwise: false)
    ctx.strokePath()
    let img = ctx.makeImage()!
    return NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])!
}

func write(_ data: Data, _ path: String) {
    let url = URL(fileURLWithPath: out).appendingPathComponent(path)
    try! FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try! data.write(to: url)
}

let white = CGColor(red: 1, green: 1, blue: 1, alpha: 1)
// App icon: opaque, the mark at the Android safe-zone size (66/108 of the canvas × 1.15 scale).
write(render(size: 1024, background: coral, mark: white, scale: 0.62), "AppIcon.appiconset/icon-1024.png")
write(render(size: 512, background: coral, mark: white, scale: 0.62), "Logo.imageset/logo.png")
// CallKit button icon: template (alpha only), 40 pt.
write(render(size: 120, background: nil, mark: white, scale: 0.9), "CallKitIcon.imageset/callkit@3x.png")
print("icons written to \(out)")

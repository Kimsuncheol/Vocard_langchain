import Foundation
import PDFKit
import Vision
import AppKit
let args = CommandLine.arguments
let url = URL(fileURLWithPath: args[1])
let out = args[2]
let doc = PDFDocument(url: url)!
let start = args.count > 3 ? Int(args[3])! : 0
let end = args.count > 4 ? Int(args[4])! : doc.pageCount
let languages = try VNRecognizeTextRequest().supportedRecognitionLanguages()
print("Supported Korean: \(languages.contains("ko-KR")); pages: \(doc.pageCount)")
for i in start..<min(end, doc.pageCount) {
 autoreleasepool {
  do {
   let page = doc.page(at:i)!
   let box = page.bounds(for: .mediaBox)
   let scale: CGFloat = 3.5
   let w = Int(box.width * scale), h = Int(box.height * scale)
   let cs = CGColorSpaceCreateDeviceRGB()
   let ctx = CGContext(data:nil,width:w,height:h,bitsPerComponent:8,bytesPerRow:w*4,space:cs,bitmapInfo:CGImageAlphaInfo.noneSkipLast.rawValue)!
   ctx.setFillColor(CGColor(red:1,green:1,blue:1,alpha:1));ctx.fill(CGRect(x:0,y:0,width:w,height:h))
   ctx.scaleBy(x:scale,y:scale);page.draw(with:.mediaBox,to:ctx)
   let image = ctx.makeImage()!
   let request = VNRecognizeTextRequest()
   request.recognitionLevel = .accurate
   request.recognitionLanguages = ["ko-KR","en-US"]
   request.usesLanguageCorrection = false
   try VNImageRequestHandler(cgImage:image).perform([request])
   let rows: [[String:Any]] = (request.results ?? []).enumerated().map { j,o in
     let candidates = o.topCandidates(3)
     let b = o.boundingBox
     return ["observation_index":j,"text":candidates.first?.string ?? "", "confidence":Double(candidates.first?.confidence ?? 0),"bbox":["x":Double(b.minX),"y":Double(1-b.maxY),"width":Double(b.width),"height":Double(b.height)],"alternatives":candidates.dropFirst().map{["text":$0.string,"confidence":Double($0.confidence)]}]
   }
   let result:[String:Any] = ["pdf_page_index":i,"pdf_page_number":i+1,"page_size_points":[Double(box.width),Double(box.height)],"observations":rows]
   let data = try JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys,.withoutEscapingSlashes])
   try data.write(to: URL(fileURLWithPath:out).appendingPathComponent(String(format:"page-%03d.json",i)))
   if i % 10 == 0 || i == end-1 {print("Completed page index \(i)");fflush(stdout)}
  } catch {print("ERROR page \(i): \(error)");fflush(stdout)}
 }
}

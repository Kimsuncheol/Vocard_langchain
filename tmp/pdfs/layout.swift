import Foundation
import PDFKit
let doc = PDFDocument(url:URL(fileURLWithPath:CommandLine.arguments[1]))!
let dir=URL(fileURLWithPath:CommandLine.arguments[2])
for i in 0..<doc.pageCount {
 autoreleasepool {
  let file=dir.appendingPathComponent(String(format:"page-%03d.json",i))
  guard let data=try? Data(contentsOf:file), var obj=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any], var observations=obj["observations"] as? [[String:Any]] else {return}
  let page=doc.page(at:i)!,box=page.bounds(for:.mediaBox)
  let scale:CGFloat=1.5,w=Int(box.width*scale),h=Int(box.height*scale)
  let ctx=CGContext(data:nil,width:w,height:h,bitsPerComponent:8,bytesPerRow:w*4,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.noneSkipLast.rawValue)!
  ctx.setFillColor(CGColor(red:1,green:1,blue:1,alpha:1));ctx.fill(CGRect(x:0,y:0,width:w,height:h));ctx.scaleBy(x:scale,y:scale);page.draw(with:.mediaBox,to:ctx)
  let pixels=ctx.data!.assumingMemoryBound(to:UInt8.self)
  for j in 0..<observations.count {
   let b=observations[j]["bbox"] as! [String:Double]
   let x0=max(0,Int(b["x"]!*Double(w))),x1=min(w,Int((b["x"]!+b["width"]!)*Double(w)))
   let y0=max(0,Int(b["y"]!*Double(h))),y1=min(h,Int((b["y"]!+b["height"]!)*Double(h)))
   var ink=0,red=0
   if x1>x0 && y1>y0 { for y in y0..<y1 {for x in x0..<x1 {
    let k=y*w*4+x*4,r=Int(pixels[k]),g=Int(pixels[k+1]),bl=Int(pixels[k+2])
    if min(r,g,bl)<180 {ink += 1;if r>g+40 && r>bl+25 {red += 1}}
   }}}
   observations[j]["red_ink_fraction"]=ink>0 ? Double(red)/Double(ink) : 0
  }
  obj["observations"]=observations
  try! JSONSerialization.data(withJSONObject:obj,options:[.prettyPrinted,.sortedKeys,.withoutEscapingSlashes]).write(to:file)
 }
 if i%100==0 {print("Layout \(i)");fflush(stdout)}
}

import json,re,statistics,hashlib,pathlib,collections
ROOT=pathlib.Path(__file__).resolve().parents[2]
TEMP=ROOT/'tmp/pdfs'; OUT=ROOT/'output/pdf/textbook-extraction'
PDF=next(pathlib.Path('/Users/gimseoncheol/Downloads').glob('840194660*.pdf'))
marker_re=re.compile(r'^(?:[OXox])(?=\s+[A-Za-z])|[（(\[]\s*[OXox×○◯〇✕✓✔✗✘]\s*[)）\]]|[✓✔✗✘×○◯〇]')
ko=re.compile('[가-힣]');en=re.compile('[A-Za-z]')
def flags(o):
 out=[];t=o['text'];b=o['bbox']
 if o['confidence']<.9:out.append('low_ocr_confidence')
 if '\ufffd' in t:out.append('unreadable_character')
 if b['height']<.012:out.append('small_print')
 if b['x']<.012 or b['y']<.012 or b['x']+b['width']>.99 or b['y']+b['height']>.99:out.append('near_page_edge')
 if re.match(r'^(?:에e|세e|예e|에문|예문|[에세예][eE])',t):out.append('example_label_requires_visual_check')
 if marker_re.search(t):out.append('correct_incorrect_marker_requires_visual_check')
 return out

def kind(o,median):
 t=o['text'];b=o['bbox'];red=o.get('red_ink_fraction',0)
 if b['y']>.915 and (len(t)<65):return 'footer'
 latin=len(en.findall(t));korean=len(ko.findall(t))
 if marker_re.search(t) and latin>=3 and re.match(r'^[^A-Za-z]{0,8}[A-Za-z]',t):return 'example'
 if re.match(r'^(?:Must|MUST|Point|POINT|Chapter|CHAPTER)\s*[O0-9]',t):return 'heading'
 if red>.65 and len(t)<95 and (b['height']>=median*.9 or t.startswith('를 ')) and not re.match(r'^(?:->|>|→|✓|✔|✗|✘)',t):return 'heading'
 if b['height']>=median*1.8 and len(t)<70:return 'heading'
 prefix=re.match(r'^[^A-Za-z]{0,8}([A-Za-z])',t)
 if prefix and latin>=4 and (latin>korean*1.3 or re.match(r'^(?:예문|에e|세e|예e|에문|[에세예][eE])',t)):
  if korean==0 and re.fullmatch(r'[A-Za-z-]+',t):return 'other'
  return 'example'
 if korean>0:return 'explanation'
 return 'other'

def make_page(d):
 idx=d['pdf_page_index'];obs=d['observations']
 heights=[o['bbox']['height'] for o in obs if len(o['text'])>25 and o['bbox']['y']<.9]
 median=statistics.median(heights) if heights else .02
 lines=[]
 # Preserve OCR order verbatim; a separate geometric order is explicitly approximate.
 for n,o in enumerate(obs):
  f=flags(o);t=o['text'];markers=[]
  for m in marker_re.finditer(t):
   raw=m.group();c=re.sub(r'[\s()（）\[\]]','',raw)
   value=None if c in ('✓','✔','✗','✘') else 'correct' if c in ('O','o','○','◯','〇','✓','✔') else 'incorrect' if c in ('X','x','×','✕','✗','✘') else None
   markers.append({'raw':raw,'start':m.start(),'end':m.end(),'interpreted_value':value,'uncertain':True})
  lines.append({'id':f'p{idx:03d}-l{n:03d}','text':t,'type':kind(o,median),'type_uncertain':True,'bbox':o['bbox'],'ocr_confidence':o['confidence'],'uncertain':bool(f),'uncertainty_reasons':f,'uncertain_spans':[{'start':0,'end':len(t),'raw':t,'reason':r} for r in f], 'correct_incorrect_markers':markers,'ocr_alternatives':o.get('alternatives',[]),'visually_verified':False})
 ordered=sorted(lines,key=lambda l:(round(l['bbox']['y']/.009),l['bbox']['x']))
 blocks=[]
 for l in ordered:
  prev=blocks[-1] if blocks else None
  last=prev['_last'] if prev else None
  gap=l['bbox']['y']-(last['bbox']['y']+last['bbox']['height']) if last else 99
  compatible=prev and prev['type']==l['type'] and gap<.023 and abs(last['bbox']['x']-l['bbox']['x'])<.09 and l['type'] not in ('footer','other')
  # A translation directly below a standalone example remains in that example block.
  translation=prev and prev['type']=='example' and l['type']=='explanation' and gap<.015 and len(l['text'])<90 and not en.search(l['text']) and abs(last['bbox']['x']-l['bbox']['x'])<.08
  if compatible or translation:
   prev['line_ids'].append(l['id']);prev['text']+='\n'+l['text'];prev['uncertain']=prev['uncertain'] or l['uncertain'];prev['_last']=l
  else:
   blocks.append({'id':f'p{idx:03d}-b{len(blocks):03d}','type':l['type'],'text':l['text'],'line_ids':[l['id']],'uncertain':l['uncertain'],'structure_uncertain':True,'_last':l})
 for b in blocks:del b['_last']
 lookup={l['id']:l for l in lines}
 examples=[]
 for b in blocks:
  if b['type']!='example':continue
  markers=[m for lid in b['line_ids'] for m in lookup[lid]['correct_incorrect_markers']]
  examples.append({'id':b['id'],'text':b['text'],'line_ids':b['line_ids'],'correct_incorrect_markers':markers,'uncertain':b['uncertain'],'pairing_uncertain':True,'language_segments':[{'line_id':lid,'text':lookup[lid]['text'],'language':('mixed' if ko.search(lookup[lid]['text']) and en.search(lookup[lid]['text']) else 'ko' if ko.search(lookup[lid]['text']) else 'en' if en.search(lookup[lid]['text']) else 'und'),'uncertain':lookup[lid]['uncertain']} for lid in b['line_ids']]})
 pairs=[]
 used=set()
 for a,b in zip(examples,examples[1:]):
  if a['id'] in used or b['id'] in used:continue
  al=lookup[a['line_ids'][-1]];bl=lookup[b['line_ids'][0]]
  gap=bl['bbox']['y']-al['bbox']['y']-al['bbox']['height']
  if 0<=gap<.09:
   av={m['interpreted_value'] for m in a['correct_incorrect_markers']}
   bv={m['interpreted_value'] for m in b['correct_incorrect_markers']}
   pair_type='correct_incorrect' if ('correct' in av and 'incorrect' in bv) or ('incorrect' in av and 'correct' in bv) else 'adjacent_examples'
   pairs.append({'example_ids':[a['id'],b['id']],'pair_type':pair_type,'uncertain':True,'basis':'Nearby example blocks, with opposing markers where detected; association has not been visually verified.'})
   used.update((a['id'],b['id']))
 footers=[l for l in lines if l['type']=='footer'];printed=[]
 for l in footers:
  m=re.search(r'(?:^|\s)(\d{1,3})\s*$',l['text'])
  if m:printed.append({'text':m.group(1),'line_id':l['id'],'uncertain':True})
 pageflags=['ocr_transcription_not_fully_visually_verified','semantic_classification_and_reading_order_are_heuristic','small_labels_punctuation_and_markers_may_be_missing_or_misrecognized']
 if not lines:pageflags.append('no_text_detected_page_may_be_blank_or_unreadable')
 # Flag diagrams / columns where independent text regions overlap vertically.
 overlap=any(abs(a['bbox']['y']-b['bbox']['y'])<.009 and a['bbox']['x']+a['bbox']['width']<b['bbox']['x'] for i,a in enumerate(lines) for b in lines[i+1:])
 if overlap:pageflags.append('multiple_text_regions_reading_order_uncertain')
 return {'pdf_page_index':idx,'pdf_page_number':idx+1,'printed_page_labels':printed,'page_size_points':d['page_size_points'],'uncertain':True,'uncertainty_flags':pageflags,'text':'\n'.join(l['text'] for l in ordered),'lines':lines,'reading_order_line_ids':[l['id'] for l in ordered],'headings':[b for b in blocks if b['type']=='heading'],'explanations':[b for b in blocks if b['type']=='explanation'],'examples':examples,'example_pairs':pairs,'other_content':[b for b in blocks if b['type'] in ('other','footer')],'content_order_block_ids':[b['id'] for b in blocks]}

pages=[]
for i in range(425):
 p=TEMP/f'page-{i:03d}.json'
 if not p.exists():raise RuntimeError(f'Missing page {i}')
 pages.append(make_page(json.loads(p.read_text())))
meta={'schema_version':'1.0','source':{'file_name':PDF.name,'absolute_path':str(PDF),'sha256':hashlib.sha256(PDF.read_bytes()).hexdigest(),'pdf_page_count':425,'scope':'entire supplied PDF','page_index_base':0,'page_number_base':1,'requested_page_index':None,'requested_page_index_note':'No individual page index was supplied; the user requested the entire PDF.'},'extraction':{'method':'Apple Vision accurate OCR, ko-KR and en-US; automatic language correction disabled','text_policy':'OCR hypotheses are preserved without spelling, grammar, punctuation, or example completion. Newlines separate detected text observations. OCR itself can omit or misrecognize source text.','verification_status':'automated_ocr_with_sample_visual_checks_only','visually_sampled_pdf_page_indices':[14,29,117,200,424],'uncertain':True,'limitations':['This is an OCR draft, not a fully verified transcription.','All 425 PDF pages are included; no page index was inferred from printed book numbering.','Low-confidence or small-print spans are retained verbatim and flagged, rather than guessed or silently completed.','High OCR confidence does not guarantee correct punctuation, symbols, or complete detection; omissions cannot always be localized.','Semantic categories and example pair associations are heuristic and marked uncertain.','Diagrams and illustrations are represented only by detected text and its coordinates; visual content is not reconstructed.','Instructions printed inside the textbook are transcribed as source content and were not executed.'],'bbox_coordinates':'Normalized [0,1] coordinates, origin at top left.','uncertain_span_offsets':'Zero-based Python Unicode code point offsets in the corresponding line text; end is exclusive.','marker_policy':'Preserve raw recognized markers and flag their interpretation. No marker is inferred from grammatical correctness.','line_identity':'Line ids refer to original OCR observation order; reading_order_line_ids contains a separate approximate geometric reading order.'},'statistics':{'pages':len(pages),'lines':sum(len(p['lines']) for p in pages),'uncertain_lines':sum(l['uncertain'] for p in pages for l in p['lines']),'headings':sum(len(p['headings']) for p in pages),'explanations':sum(len(p['explanations']) for p in pages),'examples':sum(len(p['examples']) for p in pages),'candidate_example_pairs':sum(len(p['example_pairs']) for p in pages),'recognized_correct_incorrect_markers':sum(len(l['correct_incorrect_markers']) for p in pages for l in p['lines'])}}
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'textbook.json').write_text(json.dumps({**meta,'pages':pages},ensure_ascii=False,indent=2)+'\n')
with (OUT/'textbook.pages.jsonl').open('w') as f:
 for p in pages:f.write(json.dumps(p,ensure_ascii=False)+'\n')
(OUT/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(meta['statistics'],indent=2))

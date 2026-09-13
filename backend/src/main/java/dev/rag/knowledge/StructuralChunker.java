package dev.rag.knowledge;
import java.util.*;

/** Unicode code point windows are explicitly estimates, not a model tokenizer. */
public final class StructuralChunker {
 public record Span(String unitKey,int start,int end){}
 public record Chunk(String key,String parent,String kind,String text,List<Span> spans){}
 public static List<Chunk> chunk(List<StructuredParser.Unit> units,int childCp,int parentCp){if(childCp<64||parentCp<childCp)throw new IllegalArgumentException("Invalid chunk limits");List<Chunk> out=new ArrayList<>();List<StructuredParser.Unit> section=new ArrayList<>();int n=0;for(var unit:units){if(unit.kind().equals("HEADING")&&!section.isEmpty()){add(out,section,n++,childCp,parentCp);section.clear();}section.add(unit);}if(!section.isEmpty())add(out,section,n,childCp,parentCp);return out;}
 private static void add(List<Chunk> out,List<StructuredParser.Unit> units,int section,int childCp,int parentCp){List<Chunk> children=new ArrayList<>();int index=0;for(var u:units){int count=u.text().codePointCount(0,u.text().length());if(u.kind().equals("TABLE")&&count>childCp){for(var part:table(u,childCp))children.add(new Chunk(section+"-c"+index++,null,"CHILD",part.text(),part.spans()));continue;}boolean atomic=u.kind().equals("TABLE")||u.kind().equals("CODE")||Boolean.TRUE.equals(u.location().get("faq"));int step=atomic?count:childCp;for(int s=0;s<count;){int end=Math.min(count,s+step);if(!atomic&&end<count){String window=slice(u.text(),s,end);int boundary=Math.max(window.lastIndexOf('\n'),Math.max(window.lastIndexOf('。'),window.lastIndexOf('；')));if(boundary>window.length()/2)end=s+window.codePointCount(0,boundary+1);}String text=slice(u.text(),s,end);children.add(new Chunk(section+"-c"+index++,null,"CHILD",text,List.of(new Span(u.key(),s,end))));s=end;}}
  int group=0;List<Chunk> batch=new ArrayList<>();int count=0;for(var c:children){int size=c.text().codePointCount(0,c.text().length());if(!batch.isEmpty()&&count+size>parentCp){parent(out,batch,section+"-p"+group++);batch.clear();count=0;}batch.add(c);count+=size;}if(!batch.isEmpty())parent(out,batch,section+"-p"+group);
 }
 private static void parent(List<Chunk> out,List<Chunk> children,String key){out.add(new Chunk(key,null,"PARENT",String.join("\n",children.stream().map(Chunk::text).toList()),children.stream().flatMap(c->c.spans().stream()).toList()));for(var c:children)out.add(new Chunk(c.key(),key,c.kind(),c.text(),c.spans()));}
 public static String slice(String text,int s,int e){return text.substring(text.offsetByCodePoints(0,s),text.offsetByCodePoints(0,e));}
 private record Row(int start,int end,String text){}
 private static List<Chunk> table(StructuredParser.Unit unit,int max){
  List<Row> lines=new ArrayList<>();int offset=0;
  for(String line:unit.text().split("(?<=\\n)",-1))if(!line.isEmpty()){int end=offset+line.codePointCount(0,line.length());lines.add(new Row(offset,end,line));offset=end;}
  List<Row> rows=new ArrayList<>();
  boolean docling=!lines.isEmpty()&&lines.getFirst().text().matches("(?s)^\\[r\\d+c\\d+].*");
  if(docling){
   int current=-1;Row accumulated=null;
   for(var line:lines){var matcher=java.util.regex.Pattern.compile("^\\[r(\\d+)c\\d+]").matcher(line.text());if(!matcher.find())throw tableLimit();int row=Integer.parseInt(matcher.group(1));if(row==current&&accumulated!=null)accumulated=new Row(accumulated.start(),line.end(),accumulated.text()+line.text());else{if(accumulated!=null)rows.add(accumulated);accumulated=line;current=row;}}
   if(accumulated!=null)rows.add(accumulated);
  }else rows.addAll(lines);
  int headerRows=1;
  if(!docling&&rows.size()>1&&rows.get(1).text().strip().matches("[| :\\-]+"))headerRows=2;
  Object explicit=unit.location().get("headerRowCount");if(explicit instanceof Number n)headerRows=Math.max(headerRows,n.intValue());
  if(docling&&unit.location().get("table") instanceof Map<?,?> metadata&&metadata.get("table_cells") instanceof List<?> cells){for(Object item:cells)if(item instanceof Map<?,?> cell&&Boolean.TRUE.equals(cell.get("column_header"))&&cell.get("end_row_offset_idx") instanceof Number n)headerRows=Math.max(headerRows,n.intValue());}
  headerRows=Math.min(headerRows,rows.size());int headerEnd=rows.get(headerRows-1).end();
  String header=slice(unit.text(),0,headerEnd);
  if(headerEnd>=max||headerRows==rows.size())throw tableLimit();
  List<Chunk> chunks=new ArrayList<>();int start=-1,end=-1,size=headerEnd;
  for(int i=headerRows;i<rows.size();i++){
   Row row=rows.get(i);int length=row.end()-row.start();
   if(length+headerEnd>max)throw tableLimit();
   if(start>=0&&size+length>max){chunks.add(tablePart(unit,header,headerEnd,start,end));start=-1;size=headerEnd;}
   if(start<0)start=row.start();end=row.end();size+=length;
  }
  if(start>=0)chunks.add(tablePart(unit,header,headerEnd,start,end));return chunks;
 }
 private static Chunk tablePart(StructuredParser.Unit unit,String header,int headerEnd,int start,int end){return new Chunk("",null,"CHILD",header+slice(unit.text(),start,end),List.of(new Span(unit.key(),0,headerEnd),new Span(unit.key(),start,end)));}
 private static dev.rag.common.ApiException tableLimit(){return new dev.rag.common.ApiException(422,"TABLE_ROW_TOO_LARGE","表头或单行超过分块上限，需拆分原表或调整构建长度；不能忽略后发布");}
 private StructuralChunker(){}
}

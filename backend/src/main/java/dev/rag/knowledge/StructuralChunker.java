package dev.rag.knowledge;
import java.util.*;

/** Unicode code point windows are explicitly estimates, not a model tokenizer. */
public final class StructuralChunker {
 public record Span(String unitKey,int start,int end){}
 public record Chunk(String key,String parent,String kind,String text,List<Span> spans){}
 public static List<Chunk> chunk(List<StructuredParser.Unit> units,int childCp,int parentCp){if(childCp<64||parentCp<childCp)throw new IllegalArgumentException("Invalid chunk limits");List<Chunk> out=new ArrayList<>();List<StructuredParser.Unit> section=new ArrayList<>();int n=0;for(var unit:units){if(unit.kind().equals("HEADING")&&!section.isEmpty()){add(out,section,n++,childCp,parentCp);section.clear();}section.add(unit);}if(!section.isEmpty())add(out,section,n,childCp,parentCp);return out;}
 private static void add(List<Chunk> out,List<StructuredParser.Unit> units,int section,int childCp,int parentCp){List<Chunk> children=new ArrayList<>();int index=0;for(var u:units){int count=u.text().codePointCount(0,u.text().length());boolean atomic=u.kind().equals("TABLE")||u.kind().equals("CODE")||Boolean.TRUE.equals(u.location().get("faq"));int step=atomic?count:childCp;for(int s=0;s<count;){int end=Math.min(count,s+step);if(!atomic&&end<count){String window=slice(u.text(),s,end);int boundary=Math.max(window.lastIndexOf('\n'),Math.max(window.lastIndexOf('。'),window.lastIndexOf('；')));if(boundary>window.length()/2)end=s+window.codePointCount(0,boundary+1);}String text=slice(u.text(),s,end);children.add(new Chunk(section+"-c"+index++,null,"CHILD",text,List.of(new Span(u.key(),s,end))));s=end;}}
  int group=0;List<Chunk> batch=new ArrayList<>();int count=0;for(var c:children){int size=c.text().codePointCount(0,c.text().length());if(!batch.isEmpty()&&count+size>parentCp){parent(out,batch,section+"-p"+group++);batch.clear();count=0;}batch.add(c);count+=size;}if(!batch.isEmpty())parent(out,batch,section+"-p"+group);
 }
 private static void parent(List<Chunk> out,List<Chunk> children,String key){out.add(new Chunk(key,null,"PARENT",String.join("\n",children.stream().map(Chunk::text).toList()),children.stream().flatMap(c->c.spans().stream()).toList()));for(var c:children)out.add(new Chunk(c.key(),key,c.kind(),c.text(),c.spans()));}
 public static String slice(String text,int s,int e){return text.substring(text.offsetByCodePoints(0,s),text.offsetByCodePoints(0,e));}
 private StructuralChunker(){}
}

package dev.rag.knowledge;
import dev.rag.common.ApiException;
import org.junit.jupiter.api.Test;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class TableChunkingTest {
 @Test void markdownRepeatsUnitsAndHeaderWithOriginalSpans(){String header="|商品|价格（元）|\n|---|---|\n";String text=header+String.join("\n",java.util.stream.IntStream.range(0,25).mapToObj(i->"|商品"+i+"|"+(i+10)+"|").toList());var units=new StructuredParser().parse(text.getBytes(StandardCharsets.UTF_8),"table.md").units();var chunks=StructuralChunker.chunk(units,64,128).stream().filter(c->c.kind().equals("CHILD")).toList();assertTrue(chunks.size()>1);for(var c:chunks){assertTrue(c.text().startsWith(header));assertTrue(c.text().codePointCount(0,c.text().length())<=64);for(var span:c.spans())assertTrue(c.text().contains(StructuralChunker.slice(units.getFirst().text(),span.start(),span.end())));}for(int i=0;i<25;i++){String row="|商品"+i+"|"+(i+10)+"|";assertEquals(1,chunks.stream().filter(c->c.text().contains(row)).count());}}
 @Test void doclingCellsAreNotSplitAcrossRowGroups(){String text="[r0c0]名称\n[r0c1]单位元\n"+String.join("",java.util.stream.IntStream.range(1,16).mapToObj(i->"[r"+i+"c0]商品"+i+"\n[r"+i+"c1]"+i+"\n").toList());var u=new StructuredParser.Unit("table","TABLE",text,Map.of("table",Map.of("table_cells",List.of(Map.of("column_header",true,"end_row_offset_idx",1)))),false,null);var chunks=StructuralChunker.chunk(List.of(u),64,128).stream().filter(c->c.kind().equals("CHILD")).toList();for(var c:chunks){assertTrue(c.text().startsWith("[r0c0]名称\n[r0c1]单位元\n"));assertTrue(c.text().codePointCount(0,c.text().length())<=64);for(int i=1;i<16;i++)assertEquals(c.text().contains("[r"+i+"c0]"),c.text().contains("[r"+i+"c1]"));}}
 @Test void refusesUnsplittableRowsInsteadOfPublishingOversizeChunks(){var u=new StructuredParser.Unit("t","TABLE","表头\n"+"超长".repeat(100),Map.of(),false,null);var e=assertThrows(ApiException.class,()->StructuralChunker.chunk(List.of(u),64,128));assertEquals("TABLE_ROW_TOO_LARGE",e.code);}
}

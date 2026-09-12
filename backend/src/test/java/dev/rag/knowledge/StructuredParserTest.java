package dev.rag.knowledge;
import org.junit.jupiter.api.Test;
import dev.rag.common.ApiException;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class StructuredParserTest {
 private final StructuredParser parser=new StructuredParser();
 @Test void preservesRuleExceptionCodeAndSourceSpans(){String md="# 重试规则\n\n订单失败时重试。\n但是已退款订单禁止重试。\n\n```java\nif (refunded) return;\n```\n";var result=parser.parse(md.getBytes(StandardCharsets.UTF_8),"rule.md");assertEquals(List.of("HEADING","TEXT","CODE"),result.units().stream().map(StructuredParser.Unit::kind).toList());assertTrue(result.units().get(1).text().contains("禁止重试"));var chunks=StructuralChunker.chunk(result.units(),64,256);for(var c:chunks)for(var span:c.spans()){var unit=result.units().stream().filter(u->u.key().equals(span.unitKey())).findFirst().orElseThrow();assertTrue(c.text().contains(StructuralChunker.slice(unit.text(),span.start(),span.end())));}assertTrue(chunks.stream().filter(c->c.kind().equals("CHILD")).allMatch(c->c.parent()!=null));}
 @Test void unicodeRangesDoNotSplitSurrogates(){String text="😀".repeat(150);var r=parser.parse(text.getBytes(StandardCharsets.UTF_8),"a.txt");var children=StructuralChunker.chunk(r.units(),64,128).stream().filter(c->c.kind().equals("CHILD")).toList();assertEquals(text,String.join("",children.stream().map(StructuralChunker.Chunk::text).toList()));assertEquals(64,children.getFirst().text().codePointCount(0,children.getFirst().text().length()));}
 @Test void htmlScriptsNotIndexedAndTableKeepsMergedCellMetadata(){var r=parser.parse("<script>secret()</script><table><tr><th colspan='2'>单位：元</th></tr><tr><td>订单</td><td>20</td></tr></table>".getBytes(StandardCharsets.UTF_8),"wiki.html");assertEquals("TABLE",r.units().getFirst().kind());assertFalse(r.units().toString().contains("secret()"));assertTrue(r.units().getFirst().location().containsKey("cells"));}
 @Test void faqIsAtomicAndInvalidEncodingFails(){String faq="{\"question\":\""+"问".repeat(100)+"\",\"answer\":\"禁止退款后重试\"}";var r=parser.parse(faq.getBytes(StandardCharsets.UTF_8),"faq.jsonl");assertEquals(1,StructuralChunker.chunk(r.units(),64,256).stream().filter(c->c.kind().equals("CHILD")).count());assertThrows(ApiException.class,()->parser.parse(new byte[]{(byte)0xC3,0x28},"a.txt"));assertThrows(ApiException.class,()->parser.parse("  ".getBytes(StandardCharsets.UTF_8),"a.txt"));}
}

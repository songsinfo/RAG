package dev.rag.knowledge;
import org.junit.jupiter.api.Test;
import dev.rag.common.ApiException;
import java.util.*;
import java.util.zip.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import static org.junit.jupiter.api.Assertions.*;
class WikiPackageTest {
 static byte[] zip(Map<String,byte[]> entries)throws Exception{var out=new ByteArrayOutputStream();try(var z=new ZipOutputStream(out)){for(var e:entries.entrySet()){z.putNextEntry(new ZipEntry(e.getKey()));z.write(e.getValue());z.closeEntry();}}return out.toByteArray();}
 @Test void pagesOwnOnlyTheirReferencedImages()throws Exception{byte[] image={(byte)0x89,'P','N','G',13,10,26,10,0};byte[] bytes=zip(Map.of("a.md","# A\n\n![图](assets/a.png)".getBytes(StandardCharsets.UTF_8),"b.md","# 私有资料B\n\n密码提示".getBytes(StandardCharsets.UTF_8),"assets/a.png",image));var pages=WikiPackage.split(bytes);assertEquals(2,pages.size());var a=pages.stream().filter(p->p.name().equals("a.md.wiki.zip")).findFirst().orElseThrow();var entries=InputValidation.archive(a.bytes());assertFalse(entries.containsKey("b.md"));assertTrue(entries.containsKey("assets/a.png"));assertEquals(1,WikiPackage.parse(a.bytes(),new StructuredParser()).units().stream().filter(u->u.kind().equals("IMAGE")&&u.imageData()!=null).count());}
 @Test void refusesRemoteAssetsTraversalAndZipBomb()throws Exception{for(String path:List.of("https://example.invalid/image.png","../outside.png","%2e%2e/outside.png")){byte[] bytes=zip(Map.of("a.md",("# A\n\n![图]("+path+")").getBytes(StandardCharsets.UTF_8)));assertThrows(ApiException.class,()->WikiPackage.split(bytes));}byte[] traversal=zip(Map.of("../escape.md","bad".getBytes(StandardCharsets.UTF_8)));assertThrows(ApiException.class,()->InputValidation.archive(traversal));byte[] bomb=zip(Map.of("a.md",new byte[100000]));assertThrows(ApiException.class,()->InputValidation.archive(bomb));}
}

package dev.rag.search;
import com.sun.net.httpserver.HttpServer;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import dev.rag.model.ModelGateway;
import org.junit.jupiter.api.Test;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.function.Supplier;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
class ParentBudgetTest {
 @Test @SuppressWarnings("unchecked") void oversizedChineseParentFallsBackToMatchedChild()throws Exception{
  var server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);server.createContext("/test-original/_search",x->{byte[]body="{\"hits\":{\"hits\":[{\"_id\":\"child\",\"_score\":1,\"_source\":{\"canonical_chunk_id\":\"child\"}}]}}".getBytes(StandardCharsets.UTF_8);x.sendResponseHeaders(200,body.length);x.getResponseBody().write(body);x.close();});server.start();
  try{var db=mock(Db.class);var access=mock(AccessPolicy.class);var model=mock(ModelGateway.class);var actor=new Actor("tenant","user",false);String parent="规".repeat(3000),child=parent.substring(0,900);
   when(access.readableBuilds(actor,List.of())).thenReturn(List.of("build"));when(access.guard(any(),anyCollection(),any())).thenAnswer(i->((Supplier<Object>)i.getArgument(2)).get());
   when(db.one(anyString(),anyMap())).thenAnswer(i->{Map<String,Object>p=i.getArgument(1);boolean isParent=p.get("id").equals("parent");return Db.params("id",p.get("id"),"parent_id",isParent?null:"parent","build_id","build","document_id","doc","version_id","v1","text_content",isParent?parent:child,"token_count",isParent?3000:900);});
   when(db.list(anyString(),anyMap())).thenAnswer(i->{Map<String,Object>p=i.getArgument(1);boolean isParent=p.get("c").equals("parent");return List.of(Db.params("unit_id","unit","text_content",parent,"start_cp",0,"end_cp",isParent?3000:900,"kind","TEXT","machine_generated",false,"location","{}"));});
   var search=new SearchService(db,access,model,"http://127.0.0.1:"+server.getAddress().getPort(),"test");var result=search.search(actor,null,"规则",List.of(),Map.of("post",Map.of("parent",true),"context",Map.of("maxTokens",6000)),List.of());
   assertEquals(1,result.evidence().size());assertEquals(child,result.evidence().getFirst().get("text"));assertEquals("child",result.evidence().getFirst().get("chunkId"));assertTrue(result.warnings().contains("PARENT_TRIMMED"));verifyNoInteractions(model);
  }finally{server.stop(0);}
 }
}

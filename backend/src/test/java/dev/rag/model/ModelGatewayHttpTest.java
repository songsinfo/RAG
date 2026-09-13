package dev.rag.model;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.junit.jupiter.api.*;
import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.math.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.function.Supplier;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
class ModelGatewayHttpTest {
 HttpServer server;Db db;BudgetService budgets;BudgetAuthority authority;AccessPolicy access;Actor actor=new Actor("tenant","user",false);Map<String,Object>received;String response;
 @BeforeEach @SuppressWarnings("unchecked") void setup()throws Exception{
  db=mock(Db.class);budgets=mock(BudgetService.class);authority=mock(BudgetAuthority.class);access=mock(AccessPolicy.class);
  when(db.one(anyString(),anyMap())).thenAnswer(i->{String sql=i.getArgument(0);if(sql.contains("SELECT * FROM job"))return Map.of("status","RUNNING","kind","INGEST","target_id","build");if(sql.contains("SELECT b.document_id"))return Map.of("status","LIVE","document_id","doc","fence",0L,"lifecycle_epoch",0L);return Map.of("calls",0,"amount",0);});when(access.guard(any(),anyCollection(),any())).thenAnswer(i->((Supplier<Object>)i.getArgument(2)).get());
  var prices=Map.<String,Object>of("configId","cfg","currency","CNY","inputPricePerMillion",1,"outputPricePerMillion",2);when(budgets.pricing(actor)).thenReturn(prices);when(authority.reserve(any(),anyString(),anyLong(),anyInt(),anyMap())).thenReturn(new BudgetAuthority.Reservation("res","test",new BigDecimal("1"),prices,""));
  server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);server.createContext("/embeddings",x->{received=Json.object(new String(x.getRequestBody().readAllBytes(),StandardCharsets.UTF_8));byte[]bytes=response.getBytes(StandardCharsets.UTF_8);x.sendResponseHeaders(200,bytes.length);x.getResponseBody().write(bytes);x.close();});server.createContext("/chat/completions",x->{received=Json.object(new String(x.getRequestBody().readAllBytes(),StandardCharsets.UTF_8));byte[]bytes=response.getBytes(StandardCharsets.UTF_8);x.sendResponseHeaders(200,bytes.length);x.getResponseBody().write(bytes);x.close();});server.start();
 }
 @AfterEach void close(){server.stop(0);}
 ModelGateway gateway(String key){return new ModelGateway(db,budgets,authority,access,"http://127.0.0.1:"+server.getAddress().getPort(),key,"chat","embed","vision","judge","rerank",2,"test");}
 @Test void embeddingUsesActualHttpAndRestoresProviderOrder(){response="{\"object\":\"list\",\"model\":\"embed\",\"data\":[{\"object\":\"embedding\",\"index\":1,\"embedding\":[0,1]},{\"object\":\"embedding\",\"index\":0,\"embedding\":[1,0]}],\"usage\":{\"prompt_tokens\":4,\"total_tokens\":4}}";var vectors=gateway("test-only").embed(actor,"JOB","job",List.of("甲","乙"));assertEquals(List.of(List.of(1d,0d),List.of(0d,1d)),vectors);assertEquals(List.of("甲","乙"),received.get("input"));verify(authority).settle(eq(actor),any(),eq("SETTLED"),eq(4L),eq(0L));}
 @Test void absentKeyNeverCallsProviderOrBudget(){var e=assertThrows(ApiException.class,()->gateway("").embed(actor,"JOB","job",List.of("甲")));assertEquals("MODEL_NOT_CONFIGURED",e.code);assertNull(received);verifyNoInteractions(authority);}
 @Test void missingUsageIsNotFree(){response="{\"object\":\"list\",\"model\":\"embed\",\"data\":[{\"object\":\"embedding\",\"index\":0,\"embedding\":[1,0]}]}";assertEquals("INVALID_MODEL_RESPONSE",assertThrows(ApiException.class,()->gateway("test-only").embed(actor,"JOB","job",List.of("甲"))).code);verify(authority).settle(eq(actor),any(),eq("UNKNOWN"),isNull(),isNull());}
 @Test void malformedDimensionsAreRejected(){response="{\"object\":\"list\",\"model\":\"embed\",\"data\":[{\"object\":\"embedding\",\"index\":0,\"embedding\":[1]}],\"usage\":{\"prompt_tokens\":1,\"total_tokens\":1}}";assertEquals("INVALID_MODEL_RESPONSE",assertThrows(ApiException.class,()->gateway("test-only").embed(actor,"JOB","job",List.of("甲"))).code);}
 @Test void cancelledJobNeverContactsProvider(){when(db.one(contains("SELECT * FROM job"),anyMap())).thenReturn(Map.of("status","CANCELLED"));assertEquals("CANCELLED",assertThrows(ApiException.class,()->gateway("test-only").embed(actor,"JOB","job",List.of("甲"))).code);assertNull(received);verifyNoInteractions(authority);}
 @Test void remoteReservationAmountIsCheckedBeforeSending(){
  doThrow(new ApiException(429,"BUDGET_EXCEEDED","cap")).when(budgets).recordBatchCall(eq(actor),eq("job"),argThat(c->new BigDecimal(c.get("reserved_amount").toString()).compareTo(BigDecimal.ONE)==0));
  assertEquals("BUDGET_EXCEEDED",assertThrows(ApiException.class,()->gateway("test-only").embed(actor,"JOB","job",List.of("甲"))).code);assertNull(received);verify(authority).settle(eq(actor),any(),eq("NOT_SENT"),isNull(),isNull());
 }
 @Test void nestedRunCallUsesDurableEvaluationJobBeforeProviderRequest(){
  when(db.one(contains("SELECT * FROM qa_run"),anyMap())).thenReturn(Map.of("status","RUNNING","mode","AGENT","budget_json","{\"evaluationJobId\":\"batch\"}"));
  when(db.one(contains("input_total"),anyMap())).thenReturn(Map.of("calls",0,"input_total",0,"output_total",0));
  doThrow(new ApiException(429,"BUDGET_EXCEEDED","cap")).when(budgets).recordBatchCall(eq(actor),eq("batch"),anyMap());
  assertEquals("BUDGET_EXCEEDED",assertThrows(ApiException.class,()->gateway("test-only").embed(actor,"RUN","qa",List.of("甲"))).code);assertNull(received);verify(budgets).requireBatchRunning(actor,"batch");
 }
 @Test void chatUsesSpringAiAndSettlesUsage(){response="{\"id\":\"chat-1\",\"object\":\"chat.completion\",\"created\":1,\"model\":\"chat\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"回答\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":1,\"total_tokens\":4}}";var reply=gateway("test-only").chat(actor,"JOB","job","ANSWER",List.of(Map.of("role","user","content","问题")),100);assertEquals("回答",reply.get("content"));assertEquals("chat",received.get("model"));verify(authority).settle(eq(actor),any(),eq("SETTLED"),eq(3L),eq(1L));}
 @Test @SuppressWarnings("unchecked") void toolCallsRoundTripThroughSpringAiProtocol(){response="{\"id\":\"chat-2\",\"object\":\"chat.completion\",\"created\":1,\"model\":\"chat\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":null,\"tool_calls\":[{\"id\":\"call-1\",\"type\":\"function\",\"function\":{\"name\":\"search_knowledge\",\"arguments\":\"{\\\"query\\\":\\\"退款\\\"}\"}}]},\"finish_reason\":\"tool_calls\"}],\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":2,\"total_tokens\":7}}";var tool=Map.<String,Object>of("type","function","function",Map.of("name","search_knowledge","description","检索原文","parameters",Map.of("type","object","properties",Map.of("query",Map.of("type","string")),"required",List.of("query"))));var reply=gateway("test-only").chatTools(actor,"JOB","job","AGENT",List.of(Map.of("role","user","content","查退款")),100,List.of(tool));var calls=(List<Map<String,Object>>)reply.get("tool_calls");assertEquals("search_knowledge",((Map<?,?>)calls.getFirst().get("function")).get("name"));assertEquals(1,((List<?>)received.get("tools")).size());}
}

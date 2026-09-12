package dev.rag.qa;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import dev.rag.model.ModelGateway;
import dev.rag.search.SearchService;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.datasource.*;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.Supplier;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
class QaLifecycleTest {
 Db db;QaService service;SearchService search;ModelGateway model;AccessPolicy access;Actor actor=new Actor("tenant","user",false);
 @BeforeEach @SuppressWarnings("unchecked") void setup(){var ds=new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";MODE=MySQL;DB_CLOSE_DELAY=-1","sa","");db=new Db(new NamedParameterJdbcTemplate(ds));
  String[]ddl={
   "CREATE TABLE tenant(id VARCHAR PRIMARY KEY,active_retrieval_config_id VARCHAR,security_epoch BIGINT)",
   "CREATE TABLE app_user(tenant_id VARCHAR,id VARCHAR)",
   "CREATE TABLE config_revision(tenant_id VARCHAR,id VARCHAR,kind VARCHAR,status VARCHAR,payload VARCHAR)",
   "CREATE TABLE conversation(tenant_id VARCHAR,id VARCHAR,owner_id VARCHAR,title VARCHAR,status VARCHAR DEFAULT 'ACTIVE',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
   "CREATE TABLE context_segment(tenant_id VARCHAR,id VARCHAR,conversation_id VARCHAR,segment_no INT,scope_mode VARCHAR)",
   "CREATE TABLE segment_kb(tenant_id VARCHAR,id VARCHAR,segment_id VARCHAR,kb_id VARCHAR)",
   "CREATE TABLE qa_run(tenant_id VARCHAR,id VARCHAR,owner_id VARCHAR,conversation_id VARCHAR,segment_id VARCHAR,turn_no INT,config_id VARCHAR,mode VARCHAR,strict_mode BOOLEAN,status VARCHAR,stage VARCHAR,idempotency_key VARCHAR,request_hash BINARY(32),question VARCHAR,result_json VARCHAR,security_epoch BIGINT,budget_json VARCHAR,terminal_reason VARCHAR,cancel_requested_at TIMESTAMP,started_at TIMESTAMP,finished_at TIMESTAMP,UNIQUE(tenant_id,owner_id,idempotency_key))",
   "CREATE TABLE run_source(tenant_id VARCHAR,id VARCHAR,run_id VARCHAR,document_id VARCHAR,build_id VARCHAR,purpose VARCHAR)",
   "CREATE TABLE run_dependency(tenant_id VARCHAR,id VARCHAR,run_id VARCHAR,prior_run_id VARCHAR)",
   "CREATE TABLE run_event(tenant_id VARCHAR,id VARCHAR,run_id VARCHAR,sequence_no INT,stage VARCHAR,payload VARCHAR)",
   "CREATE TABLE model_call(tenant_id VARCHAR,id VARCHAR,run_id VARCHAR,purpose VARCHAR,status VARCHAR,input_tokens BIGINT,output_tokens BIGINT,settled_amount DECIMAL,reserved_amount DECIMAL)",
   "CREATE TABLE document(tenant_id VARCHAR,id VARCHAR,active_build_id VARCHAR)"};
  for(String sql:ddl)db.exec(sql,Map.of());db.exec("INSERT INTO tenant VALUES('tenant','config',0)",Map.of());db.exec("INSERT INTO app_user VALUES('tenant','user')",Map.of());db.exec("INSERT INTO config_revision VALUES('tenant','config','RETRIEVAL','PUBLISHED','{\"retrieval\":{\"mode\":\"BM25\"}}')",Map.of());
  access=mock(AccessPolicy.class);search=mock(SearchService.class);model=mock(ModelGateway.class);when(access.guard(any(),anyCollection(),any())).thenAnswer(i->((Supplier<Object>)i.getArgument(2)).get());when(access.canReadDocument(any(),anyString())).thenReturn(true);service=new QaService(db,access,search,model,new DataSourceTransactionManager(ds));
 }
 @AfterEach void stop(){service.stop();}
 Map<String,Object>conversation(){return service.createConversation(actor,Map.of("title","test"));}
 Map<String,Object>body(Map<String,Object>c){return Map.of("segmentId",c.get("currentSegmentId"),"question","如何排查","mode","RAG");}
 @Test void cancellingBeforeRetrievalCompletesNeverPublishesLateAnswer()throws Exception{
  CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),returned=new CountDownLatch(1);when(search.search(any(),anyString(),anyString(),anyList(),anyMap(),anyList())).thenAnswer(i->{entered.countDown();try{release.await();}catch(InterruptedException ignored){}finally{returned.countDown();}return new SearchService.Context(List.of(),Map.of(),List.of());});
  var c=conversation();String run=service.submit(actor,c.get("id").toString(),body(c),"cancel-key").get("runId").toString();assertTrue(entered.await(5,TimeUnit.SECONDS));var cancelled=service.cancel(actor,run);assertEquals("CANCELLED",cancelled.get("status"));release.countDown();assertTrue(returned.await(5,TimeUnit.SECONDS));assertEquals("CANCELLED",service.get(actor,run).get("status"));assertFalse(service.get(actor,run).containsKey("answer"));verifyNoInteractions(model);
 }
 @Test void sameKeyReturnsOriginalAndChangedPayloadConflicts()throws Exception{
  CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);when(search.search(any(),anyString(),anyString(),anyList(),anyMap(),anyList())).thenAnswer(i->{entered.countDown();try{release.await();}catch(InterruptedException ignored){}return new SearchService.Context(List.of(),Map.of(),List.of());});
  try{var c=conversation();var body=body(c);var first=service.submit(actor,c.get("id").toString(),body,"key");assertTrue(entered.await(5,TimeUnit.SECONDS));assertEquals(first.get("runId"),service.submit(actor,c.get("id").toString(),body,"key").get("runId"));var changed=new HashMap<>(body);changed.put("question","另一个问题");assertEquals("IDEMPOTENCY_CONFLICT",assertThrows(ApiException.class,()->service.submit(actor,c.get("id").toString(),changed,"key")).code);assertEquals("ACTIVE_RUN_EXISTS",assertThrows(ApiException.class,()->service.submit(actor,c.get("id").toString(),body,"key2")).code);}finally{release.countDown();}
 }
 @Test void otherUserCannotReadOrCancelKnownRun(){var c=conversation();db.exec("INSERT INTO qa_run(tenant_id,id,owner_id,conversation_id,segment_id,turn_no,status,stage) VALUES('tenant','r','user',:c,:s,1,'COMPLETED','FINISHED')",Db.params("c",c.get("id"),"s",c.get("currentSegmentId")));Actor other=new Actor("tenant","someone",false);assertThrows(ApiException.class,()->service.get(other,"r"));assertThrows(ApiException.class,()->service.cancel(other,"r"));}
 @Test void revokedSourceHidesEntireHistoricalAnswer(){var c=conversation();db.exec("INSERT INTO qa_run(tenant_id,id,owner_id,conversation_id,segment_id,turn_no,status,stage,question,result_json) VALUES('tenant','r','user',:c,:s,1,'COMPLETED','FINISHED','secret question','{\"answer\":\"secret answer\"}')",Db.params("c",c.get("id"),"s",c.get("currentSegmentId")));db.exec("INSERT INTO run_source VALUES('tenant','s','r','secret-doc','b','RERANK')",Map.of());when(access.canReadDocument(actor,"secret-doc")).thenReturn(false);var visible=service.get(actor,"r");assertEquals("HIDDEN",visible.get("visibility"));assertFalse(visible.containsKey("answer"));assertFalse(visible.containsKey("question"));}
 @Test void clientCannotOverrideRetrievalConfig(){var c=conversation();var body=new HashMap<>(body(c));body.put("configId","config");assertEquals("INVALID_INPUT",assertThrows(ApiException.class,()->service.submit(actor,c.get("id").toString(),body,"key")).code);}
}

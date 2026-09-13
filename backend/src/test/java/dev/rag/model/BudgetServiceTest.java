package dev.rag.model;
import dev.rag.common.*;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import java.math.*;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.jupiter.api.Assertions.*;
public class BudgetServiceTest {
 BudgetService service;Db db;Actor actor=new Actor("tenant","user",true);
 @BeforeEach void create(){var ds=new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";MODE=MySQL;DB_CLOSE_DELAY=-1","sa","");db=new Db(new NamedParameterJdbcTemplate(ds));db.exec("CREATE TABLE tenant_budget(tenant_id VARCHAR(36),id VARCHAR(36),period_month VARCHAR(7),currency VARCHAR(3),limit_amount DECIMAL(18,6),reserved_amount DECIMAL(18,6) DEFAULT 0,settled_amount DECIMAL(18,6) DEFAULT 0,UNIQUE(tenant_id,period_month))",Map.of());db.exec("CREATE TABLE budget_reservation(tenant_id VARCHAR(36),id VARCHAR(36),budget_id VARCHAR(36),caller_environment VARCHAR(128),caller_call_id VARCHAR(36),status VARCHAR(16),reserved_amount DECIMAL(18,6),settled_amount DECIMAL(18,6),expires_at TIMESTAMP,UNIQUE(tenant_id,caller_environment,caller_call_id))",Map.of());service=new BudgetService(db,new DataSourceTransactionManager(ds));}
 @Test void unknownRetainsReservationUntilReconciled(){var r=service.reserve(actor,"test","call",new BigDecimal("60"));service.settle(actor,r.get("id").toString(),"UNKNOWN",null);assertThrows(ApiException.class,()->service.reserve(actor,"test","call2",new BigDecimal("50")));service.settle(actor,r.get("id").toString(),"SETTLED",new BigDecimal("10"));assertDoesNotThrow(()->service.reserve(actor,"test","call3",new BigDecimal("50")));}
 @Test void settledCallCannotBeChargedAgain(){var r=service.reserve(actor,"test","call",new BigDecimal("60"));service.settle(actor,r.get("id").toString(),"SETTLED",new BigDecimal("10"));service.settle(actor,r.get("id").toString(),"SETTLED",new BigDecimal("10"));var b=service.summary(actor).getFirst();assertEquals(0,new BigDecimal(b.get("SETTLEDAMOUNT").toString()).compareTo(BigDecimal.TEN));}
 @Test void concurrentCallsCannotOverspend()throws Exception{try(var executor=Executors.newFixedThreadPool(2)){var barrier=new CyclicBarrier(2);List<Future<Boolean>>f=new ArrayList<>();for(int i=0;i<2;i++){final String id="call"+i;f.add(executor.submit(()->{barrier.await();try{service.reserve(actor,"test",id,new BigDecimal("60"));return true;}catch(ApiException e){assertEquals("BUDGET_RESERVATION_DENIED",e.code);return false;}}));}int passed=0;for(var x:f)if(x.get(10,TimeUnit.SECONDS))passed++;assertEquals(1,passed);}}
 @Test void priceBoundNeverSilentlyTreatsUnknownAsFree(){assertThrows(ApiException.class,()->BudgetService.estimate(Map.of(),100,10));assertEquals(new BigDecimal("0.000300"),BudgetService.estimate(Map.of("inputPricePerMillion",1,"outputPricePerMillion",2),100,100));}
 public static String jsonExtract(String json,String path){return Objects.toString(Json.object(json).get(path.substring(2)),null);}
 public static String jsonUnquote(String value){return value;}
 void batchSchema(String checkpoint){
  db.exec("CREATE ALIAS JSON_EXTRACT FOR 'dev.rag.model.BudgetServiceTest.jsonExtract'",Map.of());db.exec("CREATE ALIAS JSON_UNQUOTE FOR 'dev.rag.model.BudgetServiceTest.jsonUnquote'",Map.of());
  db.exec("CREATE TABLE job(tenant_id VARCHAR,id VARCHAR,status VARCHAR,cancel_requested_at TIMESTAMP,checkpoint VARCHAR)",Map.of());
  db.exec("CREATE TABLE qa_run(tenant_id VARCHAR,id VARCHAR,budget_json VARCHAR)",Map.of());
  db.exec("CREATE TABLE model_call(tenant_id VARCHAR,id VARCHAR,run_id VARCHAR,job_id VARCHAR,reserved_amount DECIMAL(18,6),settled_amount DECIMAL(18,6),status VARCHAR)",Map.of());
  db.exec("INSERT INTO job VALUES('tenant','batch','RUNNING',NULL,:c)",Db.params("c",checkpoint));
 }
 Map<String,Object> call(String id,String run,String job,String amount){return Db.params("tenant_id","tenant","id",id,"run_id",run,"job_id",job,"reserved_amount",new BigDecimal(amount),"status","UNKNOWN");}
 @Test void batchUsesMaxCostCnyAndCountsPriorQaTurns(){batchSchema("{\"maxCostCny\":0.5}");for(String id:List.of("old-turn","new-turn"))db.exec("INSERT INTO qa_run VALUES('tenant',:id,'{\"evaluationJobId\":\"batch\"}')",Db.params("id",id));
  service.recordBatchCall(actor,"batch",call("old","old-turn",null,"0.4"));
  assertEquals("BUDGET_EXCEEDED",assertThrows(ApiException.class,()->service.recordBatchCall(actor,"batch",call("new","new-turn",null,"0.2"))).code);
  assertEquals("BUDGET_EXCEEDED",assertThrows(ApiException.class,()->service.recordBatchCall(actor,"batch",call("judge",null,"batch","0.2"))).code);
  assertEquals(1,((Number)db.one("SELECT COUNT(*) AS n FROM model_call",Map.of()).get("n")).intValue());
 }
 @Test void concurrentBatchAttemptsReserveCapAtomically()throws Exception{batchSchema("{\"maxCostCny\":1}");try(var pool=Executors.newFixedThreadPool(2)){var start=new CyclicBarrier(2);List<Future<Boolean>>calls=new ArrayList<>();for(int i=0;i<2;i++){String id="c"+i;calls.add(pool.submit(()->{start.await();try{service.recordBatchCall(actor,"batch",call(id,null,"batch","0.6"));return true;}catch(ApiException e){assertEquals("BUDGET_EXCEEDED",e.code);return false;}}));}int accepted=0;for(var f:calls)if(f.get(5,TimeUnit.SECONDS))accepted++;assertEquals(1,accepted);}}
 @Test void lastAllowedAttemptIsRecordedButNextOneStops(){batchSchema("{\"maxCostCny\":1,\"maxModelCalls\":1}");assertDoesNotThrow(()->service.recordBatchCall(actor,"batch",call("c1",null,"batch","0.1")));assertThrows(ApiException.class,()->service.recordBatchCall(actor,"batch",call("c2",null,"batch","0.1")));}
}

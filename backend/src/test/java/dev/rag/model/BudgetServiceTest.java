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
class BudgetServiceTest {
 BudgetService service;Db db;Actor actor=new Actor("tenant","user",true);
 @BeforeEach void create(){var ds=new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";MODE=MySQL;DB_CLOSE_DELAY=-1","sa","");db=new Db(new NamedParameterJdbcTemplate(ds));db.exec("CREATE TABLE tenant_budget(tenant_id VARCHAR(36),id VARCHAR(36),period_month VARCHAR(7),currency VARCHAR(3),limit_amount DECIMAL(18,6),reserved_amount DECIMAL(18,6) DEFAULT 0,settled_amount DECIMAL(18,6) DEFAULT 0,UNIQUE(tenant_id,period_month))",Map.of());db.exec("CREATE TABLE budget_reservation(tenant_id VARCHAR(36),id VARCHAR(36),budget_id VARCHAR(36),caller_environment VARCHAR(128),caller_call_id VARCHAR(36),status VARCHAR(16),reserved_amount DECIMAL(18,6),settled_amount DECIMAL(18,6),expires_at TIMESTAMP,UNIQUE(tenant_id,caller_environment,caller_call_id))",Map.of());service=new BudgetService(db,new DataSourceTransactionManager(ds));}
 @Test void unknownRetainsReservationUntilReconciled(){var r=service.reserve(actor,"test","call",new BigDecimal("60"));service.settle(actor,r.get("id").toString(),"UNKNOWN",null);assertThrows(ApiException.class,()->service.reserve(actor,"test","call2",new BigDecimal("50")));service.settle(actor,r.get("id").toString(),"SETTLED",new BigDecimal("10"));assertDoesNotThrow(()->service.reserve(actor,"test","call3",new BigDecimal("50")));}
 @Test void settledCallCannotBeChargedAgain(){var r=service.reserve(actor,"test","call",new BigDecimal("60"));service.settle(actor,r.get("id").toString(),"SETTLED",new BigDecimal("10"));service.settle(actor,r.get("id").toString(),"SETTLED",new BigDecimal("10"));var b=service.summary(actor).getFirst();assertEquals(0,new BigDecimal(b.get("SETTLEDAMOUNT").toString()).compareTo(BigDecimal.TEN));}
 @Test void concurrentCallsCannotOverspend()throws Exception{try(var executor=Executors.newFixedThreadPool(2)){var barrier=new CyclicBarrier(2);List<Future<Boolean>>f=new ArrayList<>();for(int i=0;i<2;i++){final String id="call"+i;f.add(executor.submit(()->{barrier.await();try{service.reserve(actor,"test",id,new BigDecimal("60"));return true;}catch(ApiException e){assertEquals("BUDGET_RESERVATION_DENIED",e.code);return false;}}));}int passed=0;for(var x:f)if(x.get(10,TimeUnit.SECONDS))passed++;assertEquals(1,passed);}}
 @Test void priceBoundNeverSilentlyTreatsUnknownAsFree(){assertThrows(ApiException.class,()->BudgetService.estimate(Map.of(),100,10));assertEquals(new BigDecimal("0.000300"),BudgetService.estimate(Map.of("inputPricePerMillion",1,"outputPricePerMillion",2),100,100));}
}

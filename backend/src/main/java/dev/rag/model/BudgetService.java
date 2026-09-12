package dev.rag.model;
import dev.rag.common.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import java.math.*;
import java.time.*;
import java.util.*;
import static dev.rag.common.Db.params;
@Service
public class BudgetService {
 private final Db db;private final TransactionTemplate tx;
 public BudgetService(Db db,PlatformTransactionManager manager){this.db=db;this.tx=new TransactionTemplate(manager);}
 public Map<String,Object> pricing(Actor a){
  var row=db.optional("SELECT * FROM config_revision WHERE tenant_id=:t AND kind='MODEL' AND status='PUBLISHED' ORDER BY created_at DESC LIMIT 1",params("t",a.tenantId())).orElseThrow(()->new ApiException(409,"MODEL_PRICING_NOT_CONFIGURED","请发布含 CNY 单价的模型配置"));
  var p=Json.object(row.get("payload").toString());if(!"CNY".equals(p.get("currency")))throw new ApiException(409,"MODEL_PRICING_NOT_CONFIGURED","模型价格必须换算为 CNY");
  price(p,"inputPricePerMillion");price(p,"outputPricePerMillion");p.put("configId",row.get("id"));return p;
 }
 private static BigDecimal price(Map<String,Object>p,String key){try{var n=new BigDecimal(p.get(key).toString());if(n.signum()<0)throw new NumberFormatException();return n;}catch(Exception e){throw new ApiException(409,"MODEL_PRICING_NOT_CONFIGURED","缺少明确模型单价或上界");}}
 public static BigDecimal estimate(Map<String,Object>p,long input,long output){return price(p,"inputPricePerMillion").multiply(BigDecimal.valueOf(input)).add(price(p,"outputPricePerMillion").multiply(BigDecimal.valueOf(output))).divide(BigDecimal.valueOf(1_000_000),6,RoundingMode.CEILING);}
 public Map<String,Object> reserve(Actor actor,String environment,String callId,BigDecimal amount){
  if(amount.signum()<0)throw new ApiException(400,"INVALID_INPUT","预算不能为负");return tx.execute(s->{
   var existing=db.optional("SELECT * FROM budget_reservation WHERE tenant_id=:t AND caller_environment=:e AND caller_call_id=:c",params("t",actor.tenantId(),"e",environment,"c",callId));
   if(existing.isPresent()){if(new BigDecimal(existing.get().get("reserved_amount").toString()).compareTo(amount)!=0)throw new ApiException(409,"IDEMPOTENCY_CONFLICT","预留参数发生变化");return existing.get();}
   String month=YearMonth.now(ZoneOffset.UTC).toString();
   db.exec("INSERT IGNORE INTO tenant_budget(tenant_id,id,period_month,currency,limit_amount) VALUES(:t,:id,:m,'CNY',100)",params("t",actor.tenantId(),"id",Json.id(),"m",month));
   var budget=db.one("SELECT * FROM tenant_budget WHERE tenant_id=:t AND period_month=:m FOR UPDATE",params("t",actor.tenantId(),"m",month));
   var limit=new BigDecimal(budget.get("limit_amount").toString());var total=new BigDecimal(budget.get("reserved_amount").toString()).add(new BigDecimal(budget.get("settled_amount").toString())).add(amount);
   if(total.compareTo(limit)>0)throw new ApiException(429,"BUDGET_RESERVATION_DENIED","月度预算不足");
   String id=Json.id();db.exec("UPDATE tenant_budget SET reserved_amount=reserved_amount+:a WHERE tenant_id=:t AND id=:id",params("t",actor.tenantId(),"id",budget.get("id"),"a",amount));
   var row=params("tenant_id",actor.tenantId(),"id",id,"budget_id",budget.get("id"),"caller_environment",environment,"caller_call_id",callId,"status","RESERVED","reserved_amount",amount,"expires_at",java.sql.Timestamp.from(Instant.now().plusSeconds(3600)));db.insert("budget_reservation",row);return row;
  });
 }
 public void settle(Actor actor,String id,String status,BigDecimal actual){
  if(!Set.of("SETTLED","NOT_SENT","UNKNOWN").contains(status)||actual!=null&&actual.signum()<0)throw new ApiException(400,"INVALID_INPUT","结算参数无效");
  tx.executeWithoutResult(s->{var r=db.one("SELECT * FROM budget_reservation WHERE tenant_id=:t AND id=:id FOR UPDATE",params("t",actor.tenantId(),"id",id));
   if(Set.of("SETTLED","NOT_SENT").contains(r.get("status").toString())){if(!status.equals(r.get("status"))||actual!=null&&new BigDecimal(r.get("settled_amount").toString()).compareTo(actual)!=0)throw new ApiException(409,"IDEMPOTENCY_CONFLICT","结算已固定");return;}
   if(status.equals("UNKNOWN")){db.exec("UPDATE budget_reservation SET status='UNKNOWN' WHERE tenant_id=:t AND id=:id",params("t",actor.tenantId(),"id",id));return;}
   BigDecimal amount=status.equals("NOT_SENT")?BigDecimal.ZERO:Objects.requireNonNull(actual);
   db.exec("UPDATE tenant_budget SET reserved_amount=reserved_amount-:r,settled_amount=settled_amount+:a WHERE tenant_id=:t AND id=:id",params("r",r.get("reserved_amount"),"a",amount,"t",actor.tenantId(),"id",r.get("budget_id")));
   db.exec("UPDATE budget_reservation SET status=:s,settled_amount=:a WHERE tenant_id=:t AND id=:id",params("s",status,"a",amount,"t",actor.tenantId(),"id",id));
  });
 }
 public List<Map<String,Object>> summary(Actor a){return db.list("SELECT period_month AS periodMonth,currency,limit_amount AS limitAmount,reserved_amount AS reservedAmount,settled_amount AS settledAmount FROM tenant_budget WHERE tenant_id=:t ORDER BY period_month DESC LIMIT 12",params("t",a.tenantId()));}
}

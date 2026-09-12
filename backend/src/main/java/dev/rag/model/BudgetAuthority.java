package dev.rag.model;
import dev.rag.common.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.math.BigDecimal;
import java.util.*;
import static dev.rag.common.Db.params;
/** A remote evaluator sends token metadata, never prompts or document contents. */
@Component
public class BudgetAuthority {
 public record Reservation(String id,String authority,BigDecimal amount,Map<String,Object>price,String signature){}
 private final BudgetService local;private final String url,token,environment;
 private final HttpClient http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
 public BudgetAuthority(BudgetService local,@Value("${rag.budget-authority-url:}")String url,@Value("${rag.internal-budget-token:}")String token,@Value("${rag.environment:demo}")String environment){this.local=local;this.url=url.replaceAll("/$","");this.token=token;this.environment=environment;}
 @SuppressWarnings("unchecked") public Reservation reserve(Actor a,String callId,long input,int output,Map<String,Object>localPrice){
  if(url.isBlank()){var amount=BudgetService.estimate(localPrice,input,output);var row=local.reserve(a,environment,callId,amount);return new Reservation(row.get("id").toString(),environment,amount,localPrice,"");}
  var r=post("/reservations",params("callerEnvironment",environment,"callId",callId,"maxInputTokens",input,"maxOutputTokens",output));return new Reservation(Json.str(r,"reservationId"),url,new BigDecimal(r.get("reservedAmount").toString()),(Map<String,Object>)r.get("pricingSnapshot"),Json.str(r,"pricingSignature"));
 }
 public void settle(Actor a,Reservation r,String status,Long input,Long output){
  if(url.isBlank()){local.settle(a,r.id(),status,status.equals("SETTLED")?BudgetService.estimate(r.price(),input,output):status.equals("NOT_SENT")?BigDecimal.ZERO:null);return;}
  post("/reservations/"+r.id()+"/settle",params("status",status,"inputTokens",input,"outputTokens",output,"pricingSnapshot",r.price(),"pricingSignature",r.signature()));
 }
 private Map<String,Object>post(String path,Map<String,Object>body){
  if(token.isBlank())throw new ApiException(503,"BUDGET_AUTHORITY_UNAVAILABLE","远程预算认证未配置");try{var request=HttpRequest.newBuilder(URI.create(url+"/api/v1/internal/budget"+path)).timeout(Duration.ofSeconds(10)).header("Authorization","Bearer "+token).header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString(Json.write(body))).build();var r=http.send(request,HttpResponse.BodyHandlers.ofString());if(r.statusCode()==429)throw new ApiException(429,"BUDGET_RESERVATION_DENIED","共用预算已耗尽");if(r.statusCode()!=200)throw new ApiException(503,"BUDGET_AUTHORITY_UNAVAILABLE","共用预算服务拒绝调用");return Json.object(r.body());}catch(ApiException e){throw e;}catch(InterruptedException e){Thread.currentThread().interrupt();throw new ApiException(503,"BUDGET_AUTHORITY_UNAVAILABLE","预算调用中断");}catch(Exception e){throw new ApiException(503,"BUDGET_AUTHORITY_UNAVAILABLE","共用预算服务不可用");}
 }
}

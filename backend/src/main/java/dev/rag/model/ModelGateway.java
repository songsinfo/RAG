package dev.rag.model;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.math.*;
import java.util.*;
import java.util.concurrent.Semaphore;
import static dev.rag.common.Db.params;
@Service
public class ModelGateway {
 private final Db db;private final BudgetService budgets;private final BudgetAuthority authority;private final AccessPolicy access;
 private final String base,key,chat,embedding,vision,judge,rerank,environment;private final int dimensions;
 private final Semaphore slots=new Semaphore(3,true);private final HttpClient http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
 public ModelGateway(Db db,BudgetService budgets,BudgetAuthority authority,AccessPolicy access,@Value("${rag.model.base-url:}")String base,@Value("${rag.model.api-key:}")String key,@Value("${rag.model.chat:}")String chat,@Value("${rag.model.embedding:}")String embedding,@Value("${rag.model.vision:}")String vision,@Value("${rag.model.judge:}")String judge,@Value("${rag.model.rerank:}")String rerank,@Value("${rag.model.embedding-dimensions:1024}")int dimensions,@Value("${rag.environment:demo}")String environment){this.db=db;this.budgets=budgets;this.authority=authority;this.access=access;this.base=base.replaceAll("/$","");this.key=key;this.chat=chat;this.embedding=embedding;this.vision=vision;this.judge=judge;this.rerank=rerank;this.dimensions=dimensions;this.environment=environment;}
 public String embeddingModel(){return embedding;}public int dimensions(){return dimensions;}
 @SuppressWarnings("unchecked")
 public List<List<Double>> embed(Actor actor,String ownerKind,String ownerId,List<String>texts){
  if(texts.isEmpty()||texts.size()>100)throw new ApiException(400,"INVALID_INPUT","Embedding 批量大小无效");var result=call(actor,ownerKind,ownerId,"EMBEDDING",embedding,"/embeddings",params("model",embedding,"input",texts,"dimensions",dimensions),0);
  if(!(result.get("data") instanceof List<?>data)||data.size()!=texts.size())throw invalid();List<Map<String,Object>>rows=new ArrayList<>();for(Object item:(List<?>)result.get("data"))rows.add((Map<String,Object>)item);rows.sort(Comparator.comparingInt(r->Json.integer(r,"index",-1)));
  List<List<Double>>out=new ArrayList<>();for(int i=0;i<rows.size();i++){var row=rows.get(i);if(Json.integer(row,"index",-1)!=i||!(row.get("embedding") instanceof List<?>v)||v.size()!=dimensions)throw invalid();List<Double> vector=new ArrayList<>();double norm=0;for(Object n:(List<?>)row.get("embedding")){double value=((Number)n).doubleValue();if(!Double.isFinite(value))throw invalid();vector.add(value);norm+=value*value;}if(norm==0)throw invalid();out.add(vector);}return out;
 }
 public Map<String,Object> chat(Actor actor,String ownerKind,String ownerId,String purpose,List<Map<String,Object>>messages,int maxOutputTokens){return chatTools(actor,ownerKind,ownerId,purpose,messages,maxOutputTokens,List.of());}
 @SuppressWarnings("unchecked")
 public Map<String,Object> chatTools(Actor actor,String ownerKind,String ownerId,String purpose,List<Map<String,Object>>messages,int maxOutputTokens,List<Map<String,Object>>tools){
  String model=purpose.equals("VISION")?vision:purpose.equals("JUDGE")?judge:chat;var body=params("model",model,"messages",messages,"max_tokens",maxOutputTokens,"temperature",0);
  if(!tools.isEmpty())body.put("tools",tools);
  var result=call(actor,ownerKind,ownerId,purpose,model,"/chat/completions",body,maxOutputTokens);
  try{return (Map<String,Object>)((Map<?,?>)((List<?>)result.get("choices")).getFirst()).get("message");}catch(Exception e){throw invalid();}
 }
 @SuppressWarnings("unchecked")
 public List<Double> rerank(Actor a,String runId,String query,List<String>documents){
  var result=call(a,"RUN",runId,"RERANK",rerank,"/rerank",params("model",rerank,"query",query,"documents",documents,"top_n",documents.size(),"return_documents",false),0);
  List<Double>scores=new ArrayList<>(Collections.nCopies(documents.size(),null));try{for(Object item:(List<?>)result.get("results")){var row=(Map<String,Object>)item;int index=Json.integer(row,"index",-1);double score=((Number)row.get("relevance_score")).doubleValue();if(index<0||index>=scores.size()||scores.get(index)!=null||!Double.isFinite(score))throw invalid();scores.set(index,score);}if(scores.contains(null))throw invalid();return scores;}catch(RuntimeException e){throw invalid();}
 }
 private static ApiException invalid(){return new ApiException(502,"INVALID_MODEL_RESPONSE","模型协议响应无效");}
 private Map<String,Object> call(Actor a,String kind,String owner,String purpose,String model,String path,Map<String,Object>body,int output){
  if(base.isBlank()||key.isBlank()||model.isBlank())throw new ApiException(503,"MODEL_NOT_CONFIGURED","未配置对应真实模型服务");
  if(!Set.of("RUN","JOB").contains(kind)||output<0||output>6000)throw new ApiException(400,"INVALID_INPUT","模型调用参数无效");
  String json=Json.write(body);long input=json.getBytes(StandardCharsets.UTF_8).length; // Conservative bound, not a tokenizer claim.
  if(input>120000)throw new ApiException(413,"INPUT_TOO_LARGE","模型输入超过请求硬限制");
  if(!slots.tryAcquire())throw new ApiException(429,"RATE_LIMITED","模型在途并发已满");String callId=Json.id();BudgetAuthority.Reservation reservation=null;boolean sent=false;
  try{
   var prices=budgets.pricing(a);BigDecimal estimate=BudgetService.estimate(prices,input,output);
   List<String>docs=new ArrayList<>();Duration timeout=Duration.ofSeconds(90);
   if(kind.equals("RUN")){
    var run=db.one("SELECT * FROM qa_run WHERE tenant_id=:t AND id=:id AND owner_id=:u",params("t",a.tenantId(),"id",owner,"u",a.userId()));
    if(!Set.of("QUEUED","RUNNING").contains(run.get("status").toString()))throw new ApiException(409,"CANCELLED","请求已停止");
    int maxCalls=run.get("mode").equals("AGENT")?12:8;
    var usage=db.one("SELECT COUNT(*) AS calls,COALESCE(SUM(COALESCE(input_tokens,JSON_EXTRACT(usage_detail,'$.inputUpperBound'))),0) AS input_total,COALESCE(SUM(COALESCE(output_tokens,JSON_EXTRACT(usage_detail,'$.outputUpperBound'))),0) AS output_total FROM model_call WHERE tenant_id=:t AND run_id=:r",params("t",a.tenantId(),"r",owner));
    if(((Number)usage.get("calls")).intValue()>=maxCalls||new BigDecimal(usage.get("input_total").toString()).longValue()+input>30000||new BigDecimal(usage.get("output_total").toString()).longValue()+output>6000)throw new ApiException(429,"BUDGET_EXCEEDED","请求全局调用或 token 上界已耗尽");
    var budget=Json.object(run.get("budget_json").toString());long left=Json.integer(budget,"deadlineSeconds",run.get("mode").equals("AGENT")?60:45)*1000L-(System.currentTimeMillis()-Long.parseLong(budget.getOrDefault("startedEpochMs",System.currentTimeMillis()).toString()));if(left<=0)throw new ApiException(429,"BUDGET_EXCEEDED","请求时限已耗尽");timeout=Duration.ofMillis(Math.min(45000,left));
    for(var r:db.list("SELECT DISTINCT document_id FROM run_source WHERE tenant_id=:t AND run_id=:r",params("t",a.tenantId(),"r",owner)))docs.add(r.get("document_id").toString());
   }else{var job=db.one("SELECT status FROM job WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",owner));if(Set.of("CANCELLED","FAILED","COMPLETED").contains(job.get("status").toString()))throw new ApiException(409,"CANCELLED","入库任务已停止");}
   reservation=authority.reserve(a,callId,input,output,prices);estimate=reservation.amount();
   db.insert("model_call",params("tenant_id",a.tenantId(),"id",callId,"run_id",kind.equals("RUN")?owner:null,"job_id",kind.equals("JOB")?owner:null,"purpose",purpose,"model_config_id",prices.get("configId"),"attempt_no",1,"status","RESERVED","reserved_amount",estimate,"pricing_snapshot",Json.write(reservation.price()),"usage_detail",Json.write(params("inputUpperBound",input,"outputUpperBound",output)),"reservation_authority",reservation.authority(),"reservation_id",reservation.id()));
   var request=HttpRequest.newBuilder(URI.create(base+path)).timeout(timeout).header("Authorization","Bearer "+key).header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString(json)).build();
   // Authority gate includes every source sent previously, including rerank and history dependencies.
   final boolean[] dispatched={false};HttpResponse<String>response;
   try{response=access.guard(a,docs,()->{dispatched[0]=true;try{return http.send(request,HttpResponse.BodyHandlers.ofString());}catch(InterruptedException e){Thread.currentThread().interrupt();throw new ApiException(503,"MODEL_TIMEOUT","模型请求中断");}catch(Exception e){throw new ApiException(503,"MODEL_TIMEOUT","模型请求超时或连接失败");}});}finally{sent=dispatched[0];}
   if(response.statusCode()<200||response.statusCode()>=300)throw new ApiException(502,"MODEL_PROVIDER_ERROR","模型供应商请求失败");
   var result=Json.object(response.body());Object usageRaw=result.get("usage");
   if(usageRaw instanceof Map<?,?>usage&&usage.get("prompt_tokens") instanceof Number in){long out=usage.get("completion_tokens") instanceof Number n?n.longValue():0;BigDecimal actual=BudgetService.estimate(reservation.price(),in.longValue(),out);authority.settle(a,reservation,"SETTLED",in.longValue(),out);db.exec("UPDATE model_call SET status='SETTLED',settled_amount=:a,input_tokens=:i,output_tokens=:o,provider_request_id=:p,usage_detail=:d WHERE tenant_id=:t AND id=:id",params("a",actual,"i",in.longValue(),"o",out,"p",response.headers().firstValue("x-request-id").orElse(null),"d",Json.write(usage),"t",a.tenantId(),"id",callId));}
   else{authority.settle(a,reservation,"UNKNOWN",null,null);db.exec("UPDATE model_call SET status='UNKNOWN' WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",callId));}
   if(kind.equals("RUN"))db.exec("UPDATE run_source SET send_status='SENT' WHERE tenant_id=:t AND run_id=:id AND send_status='PREPARED'",params("t",a.tenantId(),"id",owner));return result;
  }catch(RuntimeException e){if(reservation!=null){authority.settle(a,reservation,sent?"UNKNOWN":"NOT_SENT",null,null);db.exec("UPDATE model_call SET status=:s WHERE tenant_id=:t AND id=:id AND status='RESERVED'",params("s",sent?"UNKNOWN":"NOT_SENT","t",a.tenantId(),"id",callId));}throw e;}finally{slots.release();}
 }
}
